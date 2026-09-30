"""Real PostgreSQL queue idempotency/claim fencing, limited to owned UUID fixtures."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC,datetime,timedelta
import hashlib
import json
from pathlib import Path
import tempfile
from uuid import uuid4
from sqlalchemy import create_engine,delete,insert,select,update

from lab.db.schema import dataset_labels,dataset_profiles,run_tasks,runs,score_jobs,task_scores
from lab.scorer.jobs import enqueue_score_job,claim_score_job
from lab.scorer.service import IndependentScorer

ROOT=Path(__file__).resolve().parents[3]


def engine(role):
    return create_engine((ROOT/f'data/runtime/postgres/{role}.dsn').read_text().strip(),pool_size=2,max_overflow=0)


def main():
    migrator,planner,scorer=engine('migrator'),engine('planner'),engine('scorer')
    run_id=uuid4();dataset=f'queue-review-{uuid4()}';candidate=hashlib.sha256(b'owned queue review candidate').hexdigest()
    paths=[ROOT/'lab/scorer/jobs.py',ROOT/'lab/scorer/service.py',ROOT/'lab/db/schema.py']
    hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    record={'checked_at':datetime.now(UTC).isoformat(),'source_sha256':hashes,'checks':{},
            'scope':'Real PostgreSQL and independent Planner/Scorer roles; CPU-only tiny metric fixture. Tests concurrent enqueue and score commit fencing, not OS worker limits, automatic restart/drain, Director end-to-end or GPU coexistence.'}
    request={'idempotency_key':f'queue-review-{uuid4()}','track':'anomaly','suite':'synthetic.queue-review.v1',
             'budget':{'experiments':1,'wall_seconds':30,'model_tokens':0},'program_version':'review-only'}
    payload=json.dumps({'schema':'candidate-scores.v1','sample_indices':list(range(16)),
                        'scores':[float(i) for i in range(16)]},separators=(',',':')).encode()
    arguments={'run_id':run_id,'experiment_id':'exp-review','evaluation_kind':'primary','task_id':'task-review','seed':0,
               'candidate_sha256':candidate,'candidate_output':payload}
    try:
        with migrator.begin() as connection:
            connection.execute(insert(runs).values(run_id=run_id,origin='local',owner_id=dataset,
                idempotency_key=request['idempotency_key'],payload_sha256=hashlib.sha256(json.dumps(request,sort_keys=True).encode()).hexdigest(),
                request_json=request,state='running',stop_requested=False,created_at=datetime.now(UTC),updated_at=datetime.now(UTC)))
            connection.execute(insert(dataset_profiles).values(dataset_id=dataset,split_id='synthetic-v1',session_id='entity-001',
                sample_count=16,sliding_window=4,profile_sha256=hashlib.sha256(dataset.encode()).hexdigest()))
            connection.execute(insert(run_tasks).values(**{key:value for key,value in arguments.items() if key!='candidate_output'},
                dataset_id=dataset,split_id='synthetic-v1',session_id='entity-001'))
            connection.execute(insert(dataset_labels),[{'dataset_id':dataset,'split_id':'synthetic-v1','session_id':'entity-001',
                'sample_index':i,'is_anomaly':i>=8} for i in range(16)])
        with tempfile.TemporaryDirectory(prefix='queue-review-',dir=ROOT/'data/runtime') as temporary:
            def enqueue():return enqueue_score_job(planner,**arguments,artifact_root=Path(temporary))
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures=[pool.submit(enqueue) for _ in range(2)]
                ids=[future.result() for future in futures]
            record['checks']['concurrent_same_payload_same_job']=ids[0]==ids[1]
            with scorer.connect() as connection:
                count=len(connection.execute(select(score_jobs.c.job_id).where(score_jobs.c.run_id==run_id)).all())
            record['checks']['one_queue_row']=count==1
            changed=json.loads(payload);changed['scores'][0]=0.25
            try:
                enqueue_score_job(planner,**{**arguments,'candidate_output':json.dumps(changed).encode()},artifact_root=Path(temporary))
            except ValueError:
                record['checks']['different_artifact_rejected']=True
            else:record['checks']['different_artifact_rejected']=False
            first=claim_score_job(scorer,job_id=ids[0]);assert first is not None
            record['checks']['active_claim_not_reclaimed']=claim_score_job(scorer,job_id=ids[0]) is None
            service=IndependentScorer(scorer,harness_sha256='a'*64)
            with scorer.begin() as connection:
                connection.execute(update(score_jobs).where(score_jobs.c.job_id==ids[0]).values(lease_until=datetime.now(UTC)-timedelta(seconds=1)))
            try:service.score_task(**arguments,score_job_id=ids[0],claim_token=first.claim_token)
            except ValueError:record['checks']['expired_claim_cannot_commit']=True
            else:record['checks']['expired_claim_cannot_commit']=False
            second=claim_score_job(scorer,job_id=ids[0]);assert second is not None
            record['checks']['reclaimed_token_is_new']=second.claim_token!=first.claim_token
            try:service.score_task(**arguments,score_job_id=ids[0],claim_token=first.claim_token)
            except ValueError:record['checks']['stale_claim_cannot_commit']=True
            else:record['checks']['stale_claim_cannot_commit']=False
            try:service.score_task(**{**arguments,'candidate_output':json.dumps(changed).encode()},score_job_id=ids[0],claim_token=second.claim_token)
            except ValueError:record['checks']['claimed_artifact_digest_enforced']=True
            else:record['checks']['claimed_artifact_digest_enforced']=False
            with scorer.connect() as connection:
                score_count=len(connection.execute(select(task_scores.c.task_id).where(task_scores.c.run_id==run_id)).all())
            record['checks']['rejected_claims_wrote_no_scores']=score_count==0
            result=service.score_task(**arguments,score_job_id=ids[0],claim_token=second.claim_token)
            with scorer.connect() as connection:
                state=connection.execute(select(score_jobs.c.state).where(score_jobs.c.job_id==ids[0])).scalar_one()
            record['checks']['valid_claim_score_and_job_complete']=state=='completed' and result['candidate_sha256']==candidate
    except Exception as error:
        record['error']={'type':type(error).__name__,'message':str(error)[:1000]}
    finally:
        with migrator.begin() as connection:
            connection.execute(delete(runs).where(runs.c.run_id==run_id))
            connection.execute(delete(dataset_profiles).where(dataset_profiles.c.dataset_id==dataset))
        for item in (migrator,planner,scorer):item.dispose()
    record['source_unchanged']=all(hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==sha for path,sha in hashes.items())
    record['all_passed']=not record.get('error') and len(record['checks'])==10 and all(record['checks'].values()) and record['source_unchanged']
    record['command']='.venv/bin/python docs/ai-scientist/review-evidence/review_scorer_queue.py'
    record['script_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    Path(__file__).with_name('scorer-queue-review.json').write_text(json.dumps(record,indent=2)+'\n')
    print(json.dumps(record,indent=2))
    if not record['all_passed']:raise SystemExit(1)


if __name__=='__main__':main()
