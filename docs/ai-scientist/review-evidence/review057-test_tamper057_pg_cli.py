"""Actual PG + production CLI bytes proof on private registered mode-study run."""
import hashlib,json,os,secrets,shutil,subprocess,sys,time
from pathlib import Path
from uuid import UUID,uuid4
import pytest
from sqlalchemy import create_engine,text
from fastapi.testclient import TestClient
from harness.fingerprint import compute_harness_hash
from lab.api.registry import ApiPrincipal,SuiteEntry,SuiteRegistryFile,load_suite_registry
from lab.api.mode_experiments import ModeSnapshotStore,SyntheticSnapshotRequest,canonical_document
from lab.director.parameter_grid import grid_document
from lab.scorer.mode_snapshot import install_snapshot
from lab.api.app import create_app
from tamper057_worker import MUTATIONS,snapshot,engine

ROOT=Path(__file__).resolve().parents[1]
HERE=ROOT/'data/runtime/tamper057'
def save(name,data):(HERE/name).write_text(json.dumps(data,indent=2,default=str)+'\n')
def command(argv,env,timeout):
 result=subprocess.run(argv,cwd=ROOT,env=env,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=timeout)
 assert len(result.stdout)+len(result.stderr)<65536
 return result
@pytest.mark.live
def test_actual_pg_rejections_and_full_cli_restart():
 os.umask(0o077);HERE.mkdir(parents=True,mode=0o700)
 dsn=Path(os.environ['LAB_HOLDOUT_TEST_DSN_DIR']);role_root=ROOT/'data/runtime/postgres';role_root.mkdir(parents=True,mode=0o700)
 for role in ('migrator','director','planner','scorer'):shutil.copyfile(dsn/f'{role}.dsn',role_root/f'{role}.dsn');(role_root/f'{role}.dsn').chmod(0o600)
 (ROOT/'.venv').symlink_to('/home/cachyos/ai-scientist/.venv',target_is_directory=True)
 director=engine('director');planner=engine('planner');scorer=engine('scorer');store=ModeSnapshotStore(ROOT/'data/runtime/mode-snapshots')
 info=store.create_synthetic(SyntheticSnapshotRequest(scenario='step',seed=57,train_rows=192,evaluation_rows=96));digest=info['snapshot_sha256'];install_snapshot(scorer,store,digest)
 grid=canonical_document(grid_document(digest,[{'method':'lsh','seed':i} for i in range(4)]));grid_sha=hashlib.sha256(grid).hexdigest();directory=store.directory(digest);grid_path=directory/'grid.json';grid_path.write_bytes(grid)
 suite=json.loads((directory/'suite.json').read_bytes());suite['suite_id']='tamper057-'+grid_sha[:40];suite_path=directory/'registered-suite.json';suite_path.write_bytes(canonical_document(suite))
 entry=SuiteEntry(suite_id=suite['suite_id'],track='mode',program_version='mode-grid.v1',suite_manifest_path=str(suite_path.relative_to(ROOT/'data/runtime')),suite_manifest_sha256=hashlib.sha256(suite_path.read_bytes()).hexdigest(),provider='mode-grid',scenario_path=str(grid_path.relative_to(ROOT/'data/runtime')),scenario_sha256=grid_sha,provider_config_sha256=grid_sha,proposal_limit=4)
 registry_path=HERE/'registry.json';registry_path.write_bytes(canonical_document(SuiteRegistryFile(schema='lab-suite-registry.v1',suites=(entry,)).model_dump(mode='json',by_alias=True)));registry=load_suite_registry(registry_path,ROOT/'data/runtime')
 token=secrets.token_hex(32);app=create_app(director_engine=director,principals=(ApiPrincipal(token=token,origin='local',owner_id='tamper057-private'),),suite_registry=registry)
 with TestClient(app) as client:
  admitted=client.post('/v1/runs',json={'idempotency_key':'tamper057-'+uuid4().hex,'track':'mode','suite':entry.suite_id,'program_version':entry.program_version,'budget':{'experiments':4,'wall_seconds':900,'model_tokens':0}},headers={'Authorization':'Bearer '+token});assert admitted.status_code==202,admitted.text
 run=UUID(admitted.json()['run_id']);save('state.json',{'run_id':str(run),'snapshot_sha256':digest,'registry_entry_sha256':registry.entry_sha256(entry)})
 env={**os.environ,'LAB_DIRECTOR_DSN_FILE':str(role_root/'director.dsn'),'LAB_PLANNER_DSN_FILE':str(role_root/'planner.dsn'),'LAB_SUITE_REGISTRY_FILE':str(registry_path),'OPENBLAS_NUM_THREADS':'1','OMP_NUM_THREADS':'1','MKL_NUM_THREADS':'1'}
 for k in ('SWAPP_AOS_GPU_UNIT','SWAPP_LAB_GPU_UNIT','SWAPP_GPU_RUNTIME_DB'):env.pop(k,None)
 audit=HERE/'docker-audit'
 audit.write_text('#!/usr/bin/python\nimport json,os,sys\nfrom pathlib import Path\np=Path(__file__).with_name("docker-calls.jsonl")\nf=os.open(p,os.O_WRONLY|os.O_APPEND|os.O_CREAT,0o600)\nos.write(f,(json.dumps(sys.argv[1:])+"\\n").encode())\nos.fsync(f);os.close(f)\nos.execv("/usr/bin/docker",["/usr/bin/docker",*sys.argv[1:]])\n')
 audit.chmod(0o700)
 unit=f'swapp-ai-scientist-director-dispatch-{run.hex}.service'
 argv=['systemd-run','--user','--wait','--collect','--pipe','--unit='+unit,'--property=MemoryMax=2G','--property=MemorySwapMax=0','--property=CPUQuota=100%','--property=TasksMax=128','--property=KillMode=control-group','--property=RuntimeMaxSec=650','--working-directory='+str(ROOT),'--setenv=PYTHONPATH='+str(ROOT),'--setenv=OPENBLAS_NUM_THREADS=1','--setenv=OMP_NUM_THREADS=1','--setenv=MKL_NUM_THREADS=1',sys.executable,str(ROOT/'tests/tamper057_worker.py')]
 begin=time.monotonic();result=command(argv,env,660);save('worker-execution.json',{'argv':argv,'exit_code':result.returncode,'elapsed_seconds':time.monotonic()-begin,'stdout':result.stdout,'stderr':result.stderr});assert result.returncode==0,result.stderr
 dead=command(['systemctl','--user','show',unit,'--property=ActiveState,MainPID,InvocationID,ControlGroup'],env,5);save('owner-after-exit.json',{'stdout':dead.stdout,'exit_code':dead.returncode});assert dead.returncode==0 and 'MainPID=0' in dead.stdout
 from lab.cli import _validate_dispatch_target
 from lab.director.resume import _read_target
 with director.connect() as c:run_row=dict(c.execute(text('SELECT state,request_json,payload_sha256,origin,owner_id FROM lab.runs WHERE run_id=:run'),{'run':run}).mappings().one())
 valid=_validate_dispatch_target(run,run_row,registry)
 _read_target(director,valid.contract)
 original=compute_harness_hash(ROOT).sha256;before=snapshot(director,planner,run);save('before-cli-restarts.json',before);assert len(before['scores'])==9
 results=[]
 for ordinal,relative in enumerate(MUTATIONS,1):
  p=ROOT/relative;raw=p.read_bytes();mutated=raw+b'\n';p.write_bytes(mutated)
  try:
   changed=compute_harness_hash(ROOT).sha256;assert changed!=original
   changed_target=_validate_dispatch_target(run,run_row,registry)
   with pytest.raises(ValueError,match='resume contract changed'):_read_target(director,changed_target.contract)
   restart=uuid4();argv=[sys.executable,'-m','lab.cli','director','resume','--run-id',str(run),'--restart-id',str(restart)]
   save(f'{ordinal}-cli-intent.json',{'run_id':str(run),'restart_id':str(restart),'unit':f'swapp-ai-scientist-director-resume-{run.hex}-{restart.hex}.service','source':str(ROOT)})
   result=command(argv,env,50);after=snapshot(director,planner,run)
   save(f'{ordinal}-cli-restart.json',{'path':relative,'original_sha256':hashlib.sha256(raw).hexdigest(),'mutated_sha256':hashlib.sha256(mutated).hexdigest(),'harness_before':original,'harness_changed':changed,'argv':argv,'exit_code':result.returncode,'stdout':result.stdout,'stderr':result.stderr,'before':before,'after':after})
   assert result.returncode==1
   assert after['execution']==before['execution'] and after['control']==before['control'] and after['owners']==before['owners']
   assert after['scores']==before['scores'] and after['jobs']==before['jobs'] and after['experiments']==before['experiments']
   results.append({'path':relative,'cli_exit_code':result.returncode,'original_execution_deadline_budget_generation_unchanged':True,'no_new_scores_or_jobs':True})
  finally:p.write_bytes(raw)
  assert compute_harness_hash(ROOT).sha256==original
 save('cli-result.json',{'run_id':str(run),'cases':results,'harness_restored':True,'actual_baseline_scores':9,'all_passed':True})
 director.dispose();planner.dispose();scorer.dispose()
