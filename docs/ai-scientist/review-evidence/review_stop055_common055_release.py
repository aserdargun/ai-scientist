"""Private, fixed-target release guards. Never print configuration or DB credentials."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
from uuid import UUID
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from ops.start_lab import check_unit

ROOT = Path('/home/cachyos/ai-scientist')
SOURCE = ROOT/'data/runtime/parallel-m0/stopped-proposal-055'
HERE = SOURCE/'review-evidence'
TARGET = 'fac65252-e14d-4014-9744-333b88083963'
EXPECTED = {'75642033-6e46-4a73-82a9-9e1ecb6066d3':'stop_requested',
 'b54c9282-ca69-4deb-9e28-ef50e36bf21c':'running', TARGET:'stop_requested'}
UNITS = {'swapp-ai-scientist-api.service':('lab.cli',8766),
 'swapp-ai-scientist-director-drain.service':('lab.cli',None),
 'swapp-ai-scientist-console.service':('console.server',8788)}
PAYLOADS = {'harness/VERSION','ops/sandbox-image.lock','lab/cli.py','lab/db/schema.py',
 'lab/director/contracts.py','lab/director/ledger.py','lab/director/recovery.py',
 'lab/reporting.py','lab/scorer/stop_recovery.py','tests/test_postgres_resume046_chain.py',
 'lab/director/stopped_proposal.py','lab/db/migrations/versions/0031_stopped_proposal.py',
 'tests/test_stopped_proposal055.py'}

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def digest(value): return hashlib.sha256(json.dumps(value,sort_keys=True,default=str).encode()).hexdigest()
def save(path,value):
 path.write_text(json.dumps(value,indent=2,default=str)+'\n'); path.chmod(0o600)
def command(argv,timeout=30,**kwargs):
 result=subprocess.run(argv,stdin=subprocess.DEVNULL,capture_output=True,text=True,
  check=False,timeout=timeout,**kwargs)
 if result.returncode: raise RuntimeError('fixed command failed: '+argv[0]+'; exit='+str(result.returncode))
 return result.stdout

def verify_scope():
 relative=next(line[3:] for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::'))
 group=Path('/sys/fs/cgroup')/relative.lstrip('/')
 memory=int((group/'memory.max').read_text()); swap=int((group/'memory.swap.max').read_text())
 quota,period=map(int,(group/'cpu.max').read_text().split()); tasks=int((group/'pids.max').read_text())
 assert memory<=2*1024**3 and swap==0 and quota/period<=1 and tasks<=64
 assert all(os.environ.get(name)=='1' for name in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'))
 return dict(memory=memory,swap=swap,cpu_quota=quota,cpu_period=period,tasks=tasks)

def lock(relative):
 path=ROOT/relative; assert not path.is_symlink()
 fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_CLOEXEC,0o600)
 try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BaseException: os.close(fd); raise
 return fd

def unit_record(name):
 module,port=UNITS[name]
 caps=check_unit(name,module,port)
 identity=dict(line.split('=',1) for line in command(['systemctl','--user','show',name,
  '--property=LoadState,ActiveState,MainPID,InvocationID,ControlGroup,WorkingDirectory']).splitlines())
 return caps|identity

def units():
 result={name:unit_record(name) for name in UNITS}
 assert all(value['ActiveState']=='active' for value in result.values())
 return result

def container():
 info=json.loads(command(['docker','inspect','swapp-lab-postgres-m0']))[0]
 host=info['HostConfig']
 assert info['Config']['Labels'].get('org.swapp.component')=='ai-scientist-m0-ledger'
 assert host['PortBindings']=={'5432/tcp':[{'HostIp':'127.0.0.1','HostPort':'55432'}]}
 assert host['Memory']==512*1024**2 and host['MemorySwap']==host['Memory']
 assert host['NanoCpus']==1000000000 and host['PidsLimit']==128 and info['State']['Status']=='running'
 return {'id':info['Id'],'memory':host['Memory'],'port':55432,'label':'ai-scientist-m0-ledger'}

def engine(role='migrator',readonly=True):
 path=ROOT/'data/runtime/postgres'/f'{role}.dsn'
 assert path.is_file() and not path.is_symlink() and not path.stat().st_mode&0o077
 value=path.read_text().strip(); url=make_url(value)
 assert url.drivername=='postgresql+psycopg' and url.host=='127.0.0.1' and url.port==55432
 assert url.database=='swapp_lab' and url.username=='swapp_lab_'+role
 return create_engine(value,hide_parameters=True,pool_size=1,max_overflow=0,
  connect_args={'connect_timeout':5,'options':'-c statement_timeout=5000'+(' -c default_transaction_read_only=on' if readonly else '')})

def snapshot():
 e=engine()
 try:
  with e.connect() as c:
   revision=c.execute(text('SELECT version_num FROM lab.alembic_version')).scalar_one()
   active=dict(c.execute(text("SELECT run_id::text,state FROM lab.runs WHERE state IN ('queued','running','stop_requested')")).all())
   jobs=c.execute(text("SELECT count(*) FROM scorer.score_jobs WHERE state IN ('queued','running')")).scalar_one()
   generation=c.execute(text('SELECT current_generation FROM lab.director_execution_control WHERE run_id=:run'),{'run':UUID(TARGET)}).scalar_one()
   states=dict(c.execute(text('SELECT run_id::text,state FROM lab.runs WHERE run_id::text IN (:a,:b,:target)'),
    {'a':next(iter(EXPECTED)),'b':'b54c9282-ca69-4deb-9e28-ef50e36bf21c','target':TARGET}).all())
   scores=c.execute(text('SELECT task_id,seed,score::text,score_job_id::text,worker_invocation_id FROM scorer.task_scores WHERE run_id=:run ORDER BY experiment_id,task_id,seed'),{'run':UUID(TARGET)}).all()
   budgets=c.execute(text("SELECT event_json::text FROM lab.run_events WHERE run_id=:run AND event_type='director.checkpoint' AND (event_json->>'key' LIKE '%budget%' OR event_json->>'phase' LIKE '%budget%') ORDER BY created_at,event_id"),{'run':UUID(TARGET)}).all()
   experiments=c.execute(text('SELECT experiment_id,kind,status FROM lab.experiments WHERE run_id=:run ORDER BY sequence'),{'run':UUID(TARGET)}).all()
   proposed_jobs=c.execute(text("SELECT count(*) FROM scorer.score_jobs j JOIN lab.experiments e USING(experiment_id) WHERE j.run_id=:run AND e.kind='proposal'"),{'run':UUID(TARGET)}).scalar_one()
   stop_rows=c.execute(text('SELECT recovery_id::text,created_at::text FROM lab.director_stop_closures WHERE run_id=:run'),{'run':UUID(TARGET)}).all()
  return dict(revision=revision,migrator_dsn_sha256=sha(ROOT/'data/runtime/postgres/migrator.dsn'),active=active,states=states,jobs=jobs,generation=generation,
   score_count=len(scores),scores_sha256=digest([list(row) for row in scores]),
   budgets_count=len(budgets),budgets_sha256=digest([list(row) for row in budgets]),
   experiments=[list(row) for row in experiments],proposal_jobs=proposed_jobs,
   stop_closures=[list(row) for row in stop_rows])
 finally: e.dispose()

def assert_prestate(state,revision):
 assert state['revision']==revision and state['active']==EXPECTED and state['jobs']==0
 assert state['generation']==2 and state['score_count']==9 and state['proposal_jobs']==0
 assert len(state['experiments'])==4
 assert [row[1:] for row in state['experiments']]==[['baseline','scored']]*3+[['proposal','proposed']]
 assert state['budgets_count']>0 and not state['stop_closures']

def restore():
 command(['bash','ops/start-lab.sh'],timeout=90,cwd=ROOT,
  env=os.environ|{'OPENBLAS_NUM_THREADS':'1','OMP_NUM_THREADS':'1','MKL_NUM_THREADS':'1'})
 return units()
