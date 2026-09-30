"""Reproduce a Planner append crossing Scorer finalization on owned PG fixtures."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from threading import Event
from uuid import uuid4
from sqlalchemy import delete, event, insert, select

from lab.db.schema import dataset_labels, dataset_profiles, runs, run_tasks
from lab.director.task_plan import RunTaskAssignment, plan_run_tasks
from lab.scorer.service import IndependentScorer
from review_scorer_queue import engine

ROOT = Path(__file__).resolve().parents[3]


def main():
    migrator, planner, scorer = engine('migrator'), engine('planner'), engine('scorer')
    run_id = uuid4()
    dataset = f'plan-race-review-{uuid4()}'
    candidate = hashlib.sha256(b'owned plan-race review candidate').hexdigest()
    paths = ['lab/director/task_plan.py', 'lab/scorer/service.py', 'lab/scorer/worker.py']
    hashes = {p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in paths}
    record = {'checked_at': datetime.now(UTC).isoformat(), 'source_sha256': hashes,
              'scope': 'Actual PostgreSQL using own tiny synthetic fixture; exact state-read versus terminal-report interleaving. No GPU/AOS/public data.'}
    observed, resume = Event(), Event()
    def pause_after_state_read(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith('SELECT lab.runs.state'):
            observed.set()
            if not resume.wait(10):
                raise TimeoutError('review state-read barrier timed out')
    common = dict(experiment_id='exp-review', evaluation_kind='primary', seed=0,
                  candidate_sha256=candidate, dataset_id=dataset,
                  split_id='synthetic-v1', session_id='entity-001')
    first = RunTaskAssignment(task_id='first-task', **common)
    second = RunTaskAssignment(task_id='later-task', **common)
    try:
        with migrator.begin() as connection:
            connection.execute(insert(runs).values(run_id=run_id, origin='local', owner_id=dataset,
                idempotency_key=dataset, payload_sha256='a'*64, request_json={'review_only': True},
                state='running', stop_requested=False))
            connection.execute(insert(dataset_profiles).values(dataset_id=dataset, split_id='synthetic-v1',
                session_id='entity-001', sample_count=16, sliding_window=4, profile_sha256='b'*64))
            connection.execute(insert(dataset_labels), [dict(dataset_id=dataset, split_id='synthetic-v1',
                session_id='entity-001', sample_index=i, is_anomaly=i>=8) for i in range(16)])
        plan_run_tasks(planner, run_id=run_id, assignments=(first,))
        service = IndependentScorer(scorer, harness_sha256='c'*64)
        service.score_task(run_id=run_id, experiment_id=first.experiment_id,
            evaluation_kind=first.evaluation_kind, task_id=first.task_id, seed=first.seed,
            candidate_sha256=candidate, candidate_output=json.dumps({'schema':'candidate-scores.v1',
                'sample_indices':list(range(16)), 'scores':[float(i) for i in range(16)]}).encode())
        event.listen(planner, 'after_cursor_execute', pause_after_state_read)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(plan_run_tasks, planner, run_id=run_id, assignments=(second,))
            try:
                if not observed.wait(5):
                    raise TimeoutError('Planner did not reach state-read barrier')
                report, _ = service.complete_run(run_id=run_id)
            finally:
                resume.set()
            try:
                future.result(timeout=5)
                record['late_plan_rejected'] = False
            except ValueError:
                record['late_plan_rejected'] = True
        with scorer.connect() as connection:
            state = connection.execute(select(runs.c.state).where(runs.c.run_id==run_id)).scalar_one()
            planned = [row[0] for row in connection.execute(select(run_tasks.c.task_id).where(run_tasks.c.run_id==run_id)).all()]
        reported = [row['task_id'] for row in report['task_scores']]
        record.update(final_run_state=state, planned_tasks=sorted(planned), reported_tasks=sorted(reported),
            violation_reproduced=state=='completed' and set(planned)!=set(reported))
    except Exception as error:
        record['error'] = {'type': type(error).__name__}
    finally:
        resume.set()
        if event.contains(planner, 'after_cursor_execute', pause_after_state_read):
            event.remove(planner, 'after_cursor_execute', pause_after_state_read)
        with migrator.begin() as connection:
            connection.execute(delete(runs).where(runs.c.run_id==run_id))
            connection.execute(delete(dataset_profiles).where(dataset_profiles.c.dataset_id==dataset))
        for item in (migrator, planner, scorer):
            item.dispose()
    record['source_unchanged'] = all(hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==sha for path,sha in hashes.items())
    record['command'] = '.venv/bin/python docs/ai-scientist/review-evidence/review_task_plan_finalization.py'
    record['script_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    Path(__file__).with_name('task-plan-finalization-before.json').write_text(json.dumps(record,indent=2)+'\n')
    print(json.dumps(record,indent=2))
    if record.get('error') or not record['source_unchanged'] or not record.get('violation_reproduced'):
        raise SystemExit(1)


if __name__=='__main__':
    main()
