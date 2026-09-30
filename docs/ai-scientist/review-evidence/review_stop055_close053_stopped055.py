"""Production stop recovery for the fixed historical target; explicit execution only."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from uuid import UUID
from sqlalchemy import text
from common055_release import (ROOT,SOURCE,HERE,TARGET,EXPECTED,sha,save,verify_scope,
 lock,units,container,engine,snapshot,assert_prestate)

def worker_environment():
 path=Path('/home/cachyos/.config/swapp-ai-scientist/lab-worker.env')
 assert path.is_file() and not path.is_symlink() and not path.stat().st_mode&0o077
 result=dict(os.environ)
 for line in path.read_text().splitlines():
  if not line.strip() or line.lstrip().startswith('#'): continue
  key,value=line.split('=',1); assert re.fullmatch('[A-Z][A-Z0-9_]*',key)
  parsed=shlex.split(value); assert len(parsed)==1
  result[key]=parsed[0]
 for role in ('director','planner'):
  bound=engine(role); bound.dispose()
 result.update(PYTHONPATH=str(ROOT),OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1',MKL_NUM_THREADS='1',
  LAB_DIRECTOR_DSN_FILE=str(ROOT/'data/runtime/postgres/director.dsn'),
  LAB_PLANNER_DSN_FILE=str(ROOT/'data/runtime/postgres/planner.dsn'))
 return result

def verify_pairs_report():
 from lab.director.artifacts import read_director_artifact
 from lab.director.contracts import ExperimentDocument,TrajectoryDocument
 from lab.director.ledger import canonical_json_bytes
 from lab.director.journal import canonical_bytes
 e=engine(); artifacts=ROOT/'data/runtime/director-artifacts'/TARGET
 try:
  with e.connect() as c:
   rows=c.execute(text('SELECT e.experiment_id,e.kind,e.status,r.experiment_json,r.experiment_sha256,r.experiment_blob_sha256,t.trajectory_json,t.trajectory_sha256,t.trajectory_blob_sha256,t.messages_blob_sha256 FROM lab.experiments e JOIN lab.experiment_records r USING(experiment_id) JOIN lab.trajectory_records t USING(experiment_id) WHERE e.run_id=:run ORDER BY e.sequence'),{'run':UUID(TARGET)}).mappings().all()
   report=c.execute(text('SELECT p.report_json,p.report_sha256,r.report_sha256 AS run_report_sha256 FROM lab.reports p JOIN lab.runs r USING(run_id) WHERE p.run_id=:run'),{'run':UUID(TARGET)}).mappings().one()
  assert len(rows)==4
  proof=[]
  for row in rows:
   pair=[]
   for prefix,model in (('experiment',ExperimentDocument),('trajectory',TrajectoryDocument)):
    raw=read_director_artifact(row[prefix+'_blob_sha256'],artifact_root=artifacts)
    doc=model.model_validate_json(raw,strict=True)
    assert canonical_json_bytes(doc)==raw and json.loads(raw)==row[prefix+'_json']
    assert hashlib.sha256(raw).hexdigest()==row[prefix+'_sha256']==row[prefix+'_blob_sha256']
    assert doc.run_id==UUID(TARGET) and doc.experiment_id==row['experiment_id']
    pair.append(doc)
   assert pair[0].status==row['status']
   assert pair[0].infrastructure_stop==pair[1].infrastructure_stop
   assert pair[1].messages_blob_sha256==row['messages_blob_sha256']
   read_director_artifact(row['messages_blob_sha256'],artifact_root=artifacts)
   source=read_director_artifact(pair[0].candidate_blob_sha256,artifact_root=artifacts)
   assert hashlib.sha256(source).hexdigest()==pair[0].candidate_sha256
   if row['kind']=='proposal':
    assert row['status']=='abandoned' and pair[0].decision is pair[1].outcome is None
    assert pair[0].infrastructure_stop is not None and pair[0].infrastructure_stop.generation==2
   else: assert row['status']=='scored'
   proof.append({'experiment_id':row['experiment_id'],'kind':row['kind'],'status':row['status'],
    'experiment_sha256':row['experiment_sha256'],'trajectory_sha256':row['trajectory_sha256']})
  assert hashlib.sha256(canonical_bytes(report['report_json'])).hexdigest()==report['report_sha256']==report['run_report_sha256']
  assert report['report_json']['schema']=='lab.report.v1' and report['report_json']['status']=='stopped'
  assert str(report['report_json']['run_id'])==TARGET
  return {'strict_pairs':proof,'report_sha256':report['report_sha256'],'report_status':'stopped'}
 finally: e.dispose()

def main():
 if sys.argv[1:]!=['--execute-reviewed-close']:
  print(json.dumps({'inert':True,'run_id':TARGET,'mode':'production CLI recovery apply --close-unattempted-proposal',
   'seconds':120,'no_direct_sql_terminalization':True,'dispatch_lock':'owned by production CLI'})); return 2
 os.umask(0o077)
 output=HERE/'actual053-closure0363.json'; assert not output.exists()
 record={'schema':'actual053-stopped055-closure.v1','scope':verify_scope(),'run_id':TARGET}
 fd=lock('data/runtime/parallel-m0/cpu-check.lock')
 try:
  deployed=json.loads((HERE/'deployment0363.json').read_text()); assert deployed['status']=='deployed'
  assert (ROOT/'harness/VERSION').read_text().strip()=='0.36.3'
  for name,value in deployed['changed_files'].items(): assert sha(ROOT/name)==value
  record['units_before']=units(); record['postgres']=container()
  before=snapshot(); assert_prestate(before,'0031_stopped_proposal'); record['before']=before
  from lab.director.recovery import _recovery_id,_request_sha256
  run=UUID(TARGET); recovery=_recovery_id(run,_request_sha256(run,'stop_and_finalize'))
  args=[str(ROOT/'.venv/bin/python'),'-m','lab.cli','director','recovery','apply',
   '--run-id',TARGET,'--recovery-id',str(recovery),'--remaining-seconds','120','--close-unattempted-proposal']
  record['command']=args; save(output,record)
  result=subprocess.run(args,cwd=ROOT,env=worker_environment(),stdin=subprocess.DEVNULL,
   capture_output=True,text=True,timeout=155,check=False)
  record['cli_exit_code']=result.returncode
  # Retain only typed CLI JSON. Error text/configuration are not exposed or published.
  try: record['cli_result']=json.loads(result.stdout)
  except ValueError: record['cli_stdout_unparsed']=True
  record['cli_stderr_sha256']=hashlib.sha256(result.stderr.encode()).hexdigest()
  after=snapshot(); record['after']=after
  for key in ('generation','score_count','scores_sha256','budgets_count','budgets_sha256','proposal_jobs'):
   assert after[key]==before[key], 'immutable target evidence changed: '+key
  for other,state in EXPECTED.items():
   if other!=TARGET: assert after['states'][other]==before['states'][other]==state
  assert after['jobs']==0 and container()==record['postgres']
  record['units_after']=units()
  if result.returncode!=0 or after['states'][TARGET]!='stopped':
   record['status']='pending'; save(output,record)
   print(json.dumps({'status':'pending','exit_code':result.returncode,'evidence':str(output)})); return 1
  record.update(verify_pairs_report()); record['status']='stopped_and_independently_verified'
  save(output,record)
  print(json.dumps({'status':record['status'],'strict_pairs':4,'report_sha256':record['report_sha256'],
   'evidence':str(output)})); return 0
 except BaseException as exc:
  record.update(status='failed',failure_type=type(exc).__name__); save(output,record)
  print(json.dumps({'status':'failed','failure_type':type(exc).__name__,'evidence':str(output)})); return 1
 finally: os.close(fd)
if __name__=='__main__': sys.exit(main())
