"""Prove plan/terminal-report fencing with real PG and owned tiny fixtures."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from threading import Event, current_thread
import time
from uuid import uuid4
from sqlalchemy import delete, event, insert, select, text, update
from sqlalchemy.exc import DBAPIError

from lab.db.schema import dataset_labels, dataset_profiles, runs, run_tasks
from lab.director.task_plan import RunTaskAssignment, plan_run_tasks, seal_run_task_plan
from lab.scorer.service import IndependentScorer
from review_scorer_queue import engine

ROOT=Path(__file__).resolve().parents[3]

def main():
    migrator,planner,scorer=engine('migrator'),engine('planner'),engine('scorer')
    run_id=uuid4();dataset=f'plan-seal-review-{uuid4()}'
    candidate=hashlib.sha256(b'owned plan-seal review candidate').hexdigest()
    paths=['lab/director/task_plan.py','lab/db/task_plan.py','lab/db/schema.py',
           'lab/scorer/service.py','lab/db/migrations/versions/0007_seal_task_plan.py']
    hashes={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in paths}
    record={'checked_at':datetime.now(UTC).isoformat(),'source_sha256':hashes,'checks':{},
        'scope':'Actual PostgreSQL plan closure and report race, independent Planner/Scorer credentials, own tiny synthetic fixtures. No GPU/AOS/public data or model acceptance.'}
    observed,resume,sealer_entered=Event(),Event(),Event()
    sealer_pid=[]
    def before_query(connection,cursor,statement,parameters,context,executemany):
        if current_thread().name.startswith('seal-review') and 'pg_advisory_xact_lock' in statement:
            sealer_pid.append(connection.connection.driver_connection.info.backend_pid)
            sealer_entered.set()
    def after_query(connection,cursor,statement,parameters,context,executemany):
        if current_thread().name.startswith('append-review') and statement.startswith('SELECT lab.runs.state') and not observed.is_set():
            observed.set()
            if not resume.wait(10):raise TimeoutError('append barrier timeout')
    common=dict(experiment_id='exp-review',evaluation_kind='primary',seed=0,candidate_sha256=candidate,
                dataset_id=dataset,split_id='synthetic-v1',session_id='entity-001')
    first=RunTaskAssignment(task_id='first-task',**common)
    second=RunTaskAssignment(task_id='later-task',**common)
    third=RunTaskAssignment(task_id='forbidden-task',**common)
    payload=json.dumps({'schema':'candidate-scores.v1','sample_indices':list(range(16)),
                       'scores':[float(i) for i in range(16)]}).encode()
    def score(service,assignment):
        return service.score_task(run_id=run_id,experiment_id=assignment.experiment_id,
            evaluation_kind=assignment.evaluation_kind,task_id=assignment.task_id,seed=assignment.seed,
            candidate_sha256=candidate,candidate_output=payload)
    try:
        with migrator.begin() as connection:
            connection.execute(insert(runs).values(run_id=run_id,origin='local',owner_id=dataset,
                idempotency_key=dataset,payload_sha256='a'*64,request_json={'review_only':True},
                state='running',stop_requested=False))
            connection.execute(insert(dataset_profiles).values(dataset_id=dataset,split_id='synthetic-v1',
                session_id='entity-001',sample_count=16,sliding_window=4,profile_sha256='b'*64))
            connection.execute(insert(dataset_labels),[dict(dataset_id=dataset,split_id='synthetic-v1',
                session_id='entity-001',sample_index=i,is_anomaly=i>=8) for i in range(16)])
        plan_run_tasks(planner,run_id=run_id,assignments=(first,))
        service=IndependentScorer(scorer,harness_sha256='c'*64)
        score(service,first)
        record['checks']['all_current_tasks_scored_but_open_plan_not_finalized']=service.finalize_if_ready(run_id=run_id) is None
        event.listen(planner,'before_cursor_execute',before_query)
        event.listen(planner,'after_cursor_execute',after_query)
        with ThreadPoolExecutor(max_workers=1,thread_name_prefix='append-review') as append_pool, ThreadPoolExecutor(max_workers=1,thread_name_prefix='seal-review') as seal_pool:
            append_future=append_pool.submit(plan_run_tasks,planner,run_id=run_id,assignments=(second,))
            try:
                if not observed.wait(3):raise TimeoutError('no append state-read barrier')
                try:service.complete_run(run_id=run_id)
                except ValueError:record['checks']['old_terminal_report_race_rejected']=True
                else:record['checks']['old_terminal_report_race_rejected']=False
                seal_future=seal_pool.submit(seal_run_task_plan,planner,run_id=run_id)
                if not sealer_entered.wait(3):raise TimeoutError('no sealer lock query')
                waiting=False;deadline=time.monotonic()+3
                while time.monotonic()<deadline:
                    with scorer.connect() as connection:
                        waiting=bool(connection.execute(text("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=:pid AND locktype='advisory' AND NOT granted)"),{'pid':sealer_pid[0]}).scalar_one())
                    if waiting:break
                    time.sleep(.02)
                record['checks']['sealer_actually_waited_for_append_db_lock']=waiting
            finally:resume.set()
            append_future.result(timeout=5)
            seal=seal_future.result(timeout=5)
        record['checks']['seal_includes_concurrent_append']=seal.task_count==2
        record['checks']['same_seal_idempotent']=seal_run_task_plan(planner,run_id=run_id)==seal
        try:plan_run_tasks(planner,run_id=run_id,assignments=(third,))
        except ValueError:record['checks']['library_append_after_seal_rejected']=True
        else:record['checks']['library_append_after_seal_rejected']=False
        try:
            with planner.begin() as connection:
                connection.execute(insert(run_tasks).values(run_id=run_id,**third.model_dump()))
        except DBAPIError as error:
            record['checks']['direct_sql_append_after_seal_rejected']=getattr(error.orig,'sqlstate',None)=='P0001'
        else:record['checks']['direct_sql_append_after_seal_rejected']=False
        try:
            with planner.begin() as connection:
                connection.execute(update(runs).where(runs.c.run_id==run_id).values(task_plan_sha256=None,task_plan_count=None))
        except DBAPIError as error:
            record['checks']['seal_cannot_be_cleared']=getattr(error.orig,'sqlstate',None)=='P0001'
        else:record['checks']['seal_cannot_be_cleared']=False
        record['checks']['sealed_incomplete_plan_not_finalized']=service.finalize_if_ready(run_id=run_id) is None
        score(service,second)
        result=service.finalize_if_ready(run_id=run_id)
        record['checks']['sealed_complete_plan_finalized']=result is not None
        if result is None:raise ValueError('no terminal report')
        report,digest=result
        record['checks']['report_contains_both_tasks']=sorted(row['task_id'] for row in report['task_scores'])==['first-task','later-task']
        with scorer.connect() as connection:
            row=connection.execute(select(runs.c.state,runs.c.report_sha256,runs.c.task_plan_count).where(runs.c.run_id==run_id)).one()
        record['checks']['terminal_state_and_digest_bound']=row.state=='completed' and row.report_sha256==digest and row.task_plan_count==2
        record['checks']['completed_report_idempotent']=service.complete_run(run_id=run_id)==result
    except Exception as error:
        record['error']={'type':type(error).__name__}
    finally:
        resume.set()
        for name,callback in [('before_cursor_execute',before_query),('after_cursor_execute',after_query)]:
            if event.contains(planner,name,callback):event.remove(planner,name,callback)
        with migrator.begin() as connection:
            connection.execute(delete(runs).where(runs.c.run_id==run_id))
            connection.execute(delete(dataset_profiles).where(dataset_profiles.c.dataset_id==dataset))
        for item in (migrator,planner,scorer):item.dispose()
    record['source_unchanged']=all(hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==sha for path,sha in hashes.items())
    record['all_passed']=not record.get('error') and bool(record['checks']) and all(record['checks'].values()) and record['source_unchanged']
    record['command']='.venv/bin/python docs/ai-scientist/review-evidence/review_task_plan_seal.py'
    record['script_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    Path(__file__).with_name('task-plan-seal-review.json').write_text(json.dumps(record,indent=2)+'\n')
    print(json.dumps(record,indent=2))
    if not record['all_passed']:raise SystemExit(1)

if __name__=='__main__':main()
