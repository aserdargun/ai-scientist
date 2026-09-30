import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from lab.api.registry import load_principals
from lab.director.artifacts import read_director_artifact
from ops.start_lab import check_unit

ROOT = Path('/home/cachyos/ai-scientist')
HERE = ROOT/'data/runtime/resume-proof-053'
STATE = HERE/'state.json'
PYTHON = str(ROOT/'.venv/bin/python')
DRAIN = 'swapp-ai-scientist-director-drain.service'
os.umask(0o077)

def save(path, data):
    path.write_text(json.dumps(data, indent=2, default=str)+'\n')

def engine(role):
    return create_engine((ROOT/f'data/runtime/postgres/{role}.dsn').read_text().strip(),
        hide_parameters=True, connect_args={'options': '-c statement_timeout=5000 -c default_transaction_read_only=on'})

def query(role, sql, values=None):
    e=engine(role)
    try:
        with e.connect() as c:
            return [dict(row) for row in c.execute(text(sql), values or {}).mappings()]
    finally:
        e.dispose()

def api(method, path, payload=None):
    principals=load_principals(ROOT/'data/runtime/console-bootstrap-034/principals.json')
    local=[p for p in principals if p.origin=='local']
    assert len(local)==1
    request=urllib.request.Request('http://127.0.0.1:8766'+path,
        data=None if payload is None else json.dumps(payload).encode(), method=method,
        headers={'Authorization':'Bearer '+local[0].token,'Content-Type':'application/json'})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=20) as response:
        return response.status,json.load(response)

def unit(name):
    out=subprocess.check_output(['systemctl','--user','show',name,
        '--property=LoadState,ActiveState,MainPID,InvocationID,ControlGroup,WorkingDirectory'],text=True)
    return dict(line.split('=',1) for line in out.splitlines())

def jobs(run):
    return query('planner',"SELECT job_id::text,state FROM scorer.score_jobs WHERE run_id=:run AND state IN ('queued','running')",{'run':run})

def snapshot(run):
    out={}
    out['run']=query('director','SELECT run_id::text,state,request_json,payload_sha256,stop_requested,report_sha256 FROM lab.runs WHERE run_id=:run',{'run':run})[0]
    out['execution']=query('director','SELECT * FROM lab.director_execution_contracts WHERE run_id=:run',{'run':run})
    out['owners']=query('director','SELECT * FROM lab.director_owner_generations WHERE run_id=:run ORDER BY generation',{'run':run})
    out['control']=query('director','SELECT * FROM lab.director_execution_control WHERE run_id=:run',{'run':run})
    out['scores']=query('director','SELECT * FROM lab.dev_task_results WHERE run_id=:run ORDER BY experiment_id,evaluation_kind,task_id,seed',{'run':run})
    out['checkpoints']=query('director',"SELECT event_json FROM lab.run_events WHERE run_id=:run AND event_type='director.checkpoint' ORDER BY (event_json->>'sequence')::integer",{'run':run})
    out['jobs']=jobs(run)
    return out

def restore_drain():
    result=subprocess.run(['bash','ops/start-lab.sh'],cwd=ROOT,capture_output=True,text=True,timeout=60)
    save(HERE/'restore.json',{'exit_code':result.returncode,'stdout':result.stdout,'stderr':result.stderr})
    assert result.returncode==0

def prepare():
    assert not STATE.exists(), 'test already admitted/prepared; inspect existing state'
    assert (ROOT/'harness/VERSION').read_text().strip()=='0.36.1'
    payload={'idempotency_key':'resume053-'+uuid4().hex,
        'snapshot_sha256':'3d51ac1fc162901b479e94c47d908ab473ff517180935eab0ab6cba57887e359',
        'configurations':[{'method':'lsh','seed':seed} for seed in range(35)],'wall_seconds':3600}
    from lab.director.parameter_grid import grid_document
    grid_document(payload['snapshot_sha256'],payload['configurations'])
    stopped=False
    with (ROOT/'data/runtime/director-dispatch.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        rows=query('director',"SELECT run_id::text,state FROM lab.runs WHERE state IN ('queued','running','stop_requested')")
        assert {r['run_id']:r['state'] for r in rows}=={'75642033-6e46-4a73-82a9-9e1ecb6066d3':'stop_requested','b54c9282-ca69-4deb-9e28-ef50e36bf21c':'running'}
        assert not query('planner',"SELECT job_id FROM scorer.score_jobs WHERE state IN ('queued','running')")
        current=check_unit(DRAIN,'lab.cli',None)
        assert current['ActiveState']=='active'
        before=unit(DRAIN)
        save(HERE/'preparation.json',{'request':payload,'drain_before':before,'existing_runs':rows})
        try:
            assert unit(DRAIN)==before
            subprocess.run(['systemctl','--user','stop',DRAIN],check=True,timeout=25)
            stopped=True
            assert unit(DRAIN)['ActiveState']=='inactive'
            status, result=api('POST','/v1/mode-experiments',payload)
            assert status==202 and result['state']=='queued'
            record={'run_id':result['run_id'],'restart_id':str(uuid4()),'admission':result,
                'request':payload,'drain_before':before}
            save(STATE,record)
            print(json.dumps({'admitted':record['run_id'],'restart_id':record['restart_id'],'configurations':35}),flush=True)
        except BaseException:
            if stopped: restore_drain()
            raise

def launch(kind):
    state=json.loads(STATE.read_text()); run=state['run_id']
    assert kind in ('dispatch','resume')
    environment=os.environ|{
        'LAB_DIRECTOR_DSN_FILE':str(ROOT/'data/runtime/postgres/director.dsn'),
        'LAB_PLANNER_DSN_FILE':str(ROOT/'data/runtime/postgres/planner.dsn'),
        'LAB_SUITE_REGISTRY_FILE':str(ROOT/'data/runtime/console-bootstrap-034/registry.json'),
        'OPENBLAS_NUM_THREADS':'1','OMP_NUM_THREADS':'1','MKL_NUM_THREADS':'1'}
    for name in ['SWAPP_AOS_GPU_UNIT','SWAPP_LAB_GPU_UNIT','SWAPP_GPU_RUNTIME_DB']:
        environment.pop(name,None)
    command=[PYTHON,'-m','lab.cli','director','dispatch-one' if kind=='dispatch' else 'resume','--run-id',run]
    if kind=='resume': command+=['--restart-id',state['restart_id']]
    start=time.monotonic()
    with (HERE/f'{kind}.stdout').open('wb') as out,(HERE/f'{kind}.stderr').open('wb') as err:
        result=subprocess.run(command,cwd=ROOT,env=environment,stdout=out,stderr=err,timeout=4300)
    receipt={'command':command,'exit_code':result.returncode,'elapsed_seconds':time.monotonic()-start}
    save(HERE/f'{kind}-execution.json',receipt)
    print(json.dumps(receipt),flush=True)
    print((HERE/f'{kind}.stdout').read_text()[-1800:],flush=True)
    return result.returncode

def crash():
    state=json.loads(STATE.read_text()); run=state['run_id']
    expected=f'swapp-ai-scientist-director-dispatch-{UUID(run).hex}.service'
    deadline=time.monotonic()+600; prior=None
    while time.monotonic()<deadline:
        snap=snapshot(run)
        if snap['run']['state'] in {'completed','stopped','failed'}:
            raise RuntimeError('run terminal before test interruption')
        score_count=len(snap['scores'])
        if score_count!=prior:
            print(json.dumps({'scores':score_count,'state':snap['run']['state']}),flush=True); prior=score_count
        cps=[item['event_json'] for item in snap['checkpoints']]
        if not any(cp.get('key')=='director-state:0' for cp in cps) or snap['jobs']:
            time.sleep(.5); continue
        assert len([s for s in snap['scores'] if s['evaluation_kind']=='baseline'])==9
        assert snap['control'][0]['current_generation']==1 and len(snap['owners'])==1
        owner=snap['owners'][0]; live=unit(expected)
        assert owner['worker_unit']==expected
        assert live['ActiveState']=='active' and int(live['MainPID'])==owner['worker_pid']
        assert live['InvocationID']==owner['worker_invocation_id'] and live['ControlGroup']==owner['worker_cgroup']
        assert Path('/proc/sys/kernel/random/boot_id').read_text().strip()==owner['worker_boot_id']
        process=Path('/proc')/str(owner['worker_pid'])
        assert int((process/'stat').read_text().rsplit(')',1)[1].split()[19])==owner['worker_start_ticks']
        assert (process/'cwd').resolve()==ROOT
        assert (process/'cmdline').read_bytes().split(b'\0')[:-1]==[PYTHON.encode(),b'-m',b'lab.cli',b'director',b'dispatch-one',b'--run-id',run.encode()]
        assert (process/'cgroup').read_text().strip()=='0::'+owner['worker_cgroup']
        snap['calibration']=query('director','SELECT lab.baseline_calibration_receipt(:run) AS receipt',{'run':run})[0]['receipt']
        assert snap['calibration']['run_id']==run
        verified=[]
        for cp in cps:
            raw=read_director_artifact(cp['blob_sha256'],artifact_root=ROOT/'data/runtime/director-artifacts'/run)
            assert hashlib.sha256(raw).hexdigest()==cp['payload_sha256']==cp['blob_sha256']
            verified.append({'key':cp['key'],'sha256':cp['blob_sha256']})
        snap['verified_checkpoints']=verified; snap['unit']=live
        save(HERE/'before-crash.json',snap)
        # Recheck exact invocation immediately before signaling only the new run unit.
        assert unit(expected)==live
        subprocess.run(['systemctl','--user','kill','--signal=SIGKILL','--kill-whom=all',expected],check=True,timeout=10)
        for _ in range(40):
            if not process.exists() and unit(expected)['ActiveState'] in {'failed','inactive'}: break
            time.sleep(.25)
        assert not process.exists()
        save(HERE/'after-crash.json',{'unit':unit(expected),'snapshot':snapshot(run)})
        print(json.dumps({'interrupted':run,'unit':expected,'baseline_scores':9,'verified_checkpoints':len(verified)}),flush=True)
        return
    raise RuntimeError('interruption checkpoint window not observed within test bound')

if __name__=='__main__':
    action=sys.argv[1]
    if action=='prepare': prepare()
    elif action in ('dispatch','resume'): raise SystemExit(launch(action))
    elif action=='crash': crash()
    elif action=='inspect': print(json.dumps(snapshot(json.loads(STATE.read_text())['run_id']),default=str))
    elif action=='restore': restore_drain()
    else: raise ValueError(action)
