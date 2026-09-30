"""Drain only disposable proof unit identities and its private admission intent."""
import json,re,subprocess,sys,time
from pathlib import Path
from uuid import UUID
from sqlalchemy import create_engine,text

_deadline=None
def command(argv,timeout=8):
 if _deadline is not None:
  timeout=min(timeout,int(_deadline-time.monotonic()))
  if timeout<1:raise TimeoutError("owned cleanup reserve expired")
 return subprocess.run(argv,capture_output=True,text=True,timeout=timeout,stdin=subprocess.DEVNULL)
def show(unit):
 p=command(['/usr/bin/systemctl','--user','show',unit,'--property=LoadState,ActiveState,MainPID,InvocationID,ControlGroup,WorkingDirectory,ExecStart'])
 if p.returncode:raise RuntimeError('owned unit inspection failed')
 return dict(x.split('=',1) for x in p.stdout.splitlines() if '=' in x)
def cleanup(snapshot):
 global _deadline
 _deadline=time.monotonic()+35
 snapshot=Path(snapshot).resolve(strict=True);here=snapshot/'data/runtime/tamper057';record={'units':[],'all_owned_execution_drained':False}
 expected={}
 state=here/'state.json'
 if state.exists():
  run=UUID(json.loads(state.read_text())['run_id']);expected[f'swapp-ai-scientist-director-dispatch-{run.hex}.service']='dispatch'
  for p in here.glob('*-cli-intent.json'):
   value=json.loads(p.read_text());restart=UUID(value['restart_id'])
   assert UUID(value['run_id'])==run and value['source']==str(snapshot)
   unit=f'swapp-ai-scientist-director-resume-{run.hex}-{restart.hex}.service';assert value['unit']==unit;expected[unit]='resume'
  dsn=snapshot/'data/runtime/postgres/planner.dsn'
  if dsn.exists():
   db=create_engine(dsn.read_text().strip(),hide_parameters=True,connect_args={'options':'-c statement_timeout=5000 -c default_transaction_read_only=on'})
   try:
    with db.connect() as c:
     for job in c.execute(text('SELECT job_id FROM scorer.score_jobs WHERE run_id=:run'),{'run':run}).scalars():expected[f'swapp-ai-scientist-scorer-{job.hex}.service']='scorer'
   finally:db.dispose()
 for unit,kind in expected.items():
  before=show(unit);pid=int(before.get('MainPID','0'))
  if pid:
   assert before['WorkingDirectory']==str(snapshot)
   assert re.fullmatch('[0-9a-f]{32}',before['InvocationID'])
   cmdline=Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0');args=[x.decode() for x in cmdline if x]
   if kind=='dispatch':assert args[-1]==str(snapshot/'tests/tamper057_worker.py')
   elif kind=='resume':assert args[1:5]==['-m','lab.cli','director','resume'] and str(run) in args
   else:assert args[1:3]==['-m','lab.scorer.worker'] and '--job-id' in args
   assert show(unit)==before
   stopped=command(['/usr/bin/systemctl','--user','stop',unit],timeout=12);assert stopped.returncode==0
  after=show(unit);assert int(after.get('MainPID','0'))==0 and after.get('ActiveState') in ('inactive','failed')
  record['units'].append({'unit':unit,'kind':kind,'before':before,'after':after})
 # The production reconciler sees only this proof's fresh private intent. It
 # verifies dead PID/start/boot, exact ID/label/mounts before removing a container.
 marker=here/'admission/sandbox.intent'
 if marker.exists():
  sys.path.insert(0,str(snapshot))
  from lab.sandbox.docker_runner import LocalDockerRunner,DEFAULT_SANDBOX_IMAGE
  runner=LocalDockerRunner(image=DEFAULT_SANDBOX_IMAGE,work_root=here/'sandbox',admission_lock=here/'admission/sandbox.lock',docker_binary='/usr/bin/docker')
  runner._reconcile_owned_containers()
 assert not marker.exists()
 record['private_admission_marker_absent']=True;record['all_owned_execution_drained']=True
 return record
