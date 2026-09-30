"""Actual isolated registered-run tamper path; production methods, no monkeypatches."""
import hashlib,json,os,sys
from pathlib import Path
from uuid import UUID
from sqlalchemy import create_engine,text

ROOT=Path(__file__).resolve().parents[1]
HERE=ROOT/'data/runtime/tamper057'
STATE=HERE/'state.json'
MUTATIONS=('harness/VERSION','lab/scorer/service.py','ops/sandbox-image.lock','docker/sandbox/requirements.txt')
def save(name,data):
 (HERE/name).write_text(json.dumps(data,indent=2,default=str)+'\n')
def engine(role):
 return create_engine((ROOT/'data/runtime/postgres'/f'{role}.dsn').read_text().strip(),hide_parameters=True,connect_args={'options':'-c statement_timeout=5000 -c lock_timeout=5000'})
def snapshot(director,planner,run):
 out={}
 with director.connect() as c:
  for name,sql in {
   'run':'SELECT run_id,state,request_json,payload_sha256,stop_requested FROM lab.runs WHERE run_id=:run',
   'execution':'SELECT * FROM lab.director_execution_contracts WHERE run_id=:run',
   'control':'SELECT * FROM lab.director_execution_control WHERE run_id=:run',
   'owners':'SELECT * FROM lab.director_owner_generations WHERE run_id=:run ORDER BY generation',
   'scores':'SELECT * FROM lab.dev_task_results WHERE run_id=:run ORDER BY experiment_id,evaluation_kind,task_id,seed',
   'experiments':'SELECT experiment_id,sequence,status FROM lab.experiments WHERE run_id=:run ORDER BY sequence',
   'checkpoints':"SELECT event_json FROM lab.run_events WHERE run_id=:run AND event_type='director.checkpoint' ORDER BY (event_json->>'sequence')::integer",
  }.items():out[name]=[dict(x) for x in c.execute(text(sql),{'run':run}).mappings()]
 with planner.connect() as c:out['jobs']=[dict(x) for x in c.execute(text('SELECT job_id,state,experiment_id,evaluation_kind,task_id,seed FROM scorer.score_jobs WHERE run_id=:run ORDER BY job_id'),{'run':run}).mappings()]
 return out

def main():
 from harness.fingerprint import compute_harness_hash
 from lab.api.registry import load_suite_registry
 from lab.cli import _validate_dispatch_target
 from lab.director.recovery import capture_current_owner
 from lab.director.ownership import OwnerProcessIdentity,claim_initial_execution,owned_execution
 from lab.director.suite_manifest import load_suite_manifest
 from lab.director.baseline_runner import run_baseline_suite
 from lab.director.journal import DirectorRunLease
 from lab.director.budget import RunBudget
 from lab.director.parameter_grid import ParameterGridProvider
 from lab.director.fake_llm import AgentContext
 from lab.director.runner import run_one_proposal,_candidate_git_tree
 from lab.director.baselines import baseline_candidate_source
 from lab.reporting import read_run_pairs
 from lab.sandbox.docker_runner import LocalDockerRunner,DEFAULT_SANDBOX_IMAGE
 import lab.cli,lab.director.runner,lab.scorer.service
 for module in (lab.cli,lab.director.runner,lab.scorer.service):assert Path(module.__file__).resolve().is_relative_to(ROOT.resolve())
 state=json.loads(STATE.read_text());run=UUID(state['run_id']);director=engine('director');planner=engine('planner')
 registry=load_suite_registry(ROOT/'data/runtime/tamper057/registry.json',ROOT/'data/runtime')
 with director.connect() as c:row=dict(c.execute(text('SELECT state,request_json,payload_sha256,origin,owner_id FROM lab.runs WHERE run_id=:run'),{'run':run}).mappings().one())
 target=_validate_dispatch_target(run,row,registry);observed=capture_current_owner(row['payload_sha256'],run)
 owner=claim_initial_execution(director,contract=target.contract,process=OwnerProcessIdentity(**{k:getattr(observed,k) for k in observed.__dataclass_fields__}))
 pin=compute_harness_hash(ROOT).sha256;image=DEFAULT_SANDBOX_IMAGE.rsplit('@sha256:',1)[-1].removeprefix('sha256:');runtime=ROOT/'data/runtime/director-artifacts'/str(run)
 runtime.mkdir(parents=True,mode=0o700);runner=LocalDockerRunner(image=DEFAULT_SANDBOX_IMAGE,work_root=ROOT/'data/runtime/tamper057/sandbox',admission_lock=HERE/'admission/sandbox.lock',docker_binary=str(HERE/'docker-audit'))
 manifest,tasks,_=load_suite_manifest(target.suite_path,planner,study_snapshot_sha256=state['snapshot_sha256'])
 provider=ParameterGridProvider.load(target.scenario_path,configuration_sha256=target.entry.provider_config_sha256,registry_entry_sha256=target.entry_sha256)
 budget=RunBudget(proposal_limit=4,wall_limit=900,token_limit=0)
 with owned_execution(owner),DirectorRunLease(director,run) as lease:
  calibration=run_baseline_suite(director,planner,run_id=run,tasks=tasks,lease=lease,runner=runner,suite_id=manifest.suite_id,suite_version=manifest.suite_version,harness_sha256=pin,image_sha256=image,budget=budget,artifact_root=runtime,seed_wall_seconds=120)
  before=snapshot(director,planner,run);save('before-tamper.json',before);assert len(before['scores'])==9
  assert all(x['evaluation_kind']=='baseline' for x in before['scores'])
  pairs=read_run_pairs(director,run,artifact_root=runtime);parent=next(p.document for p in pairs if p.document.baseline_name=='robust_z');source=baseline_candidate_source('robust_z');tree=_candidate_git_tree(source)
  results=[]
  for ordinal,relative in enumerate(MUTATIONS,1):
   p=ROOT/relative;original=p.read_bytes();mutated=original+b'\n'
   p.write_bytes(mutated)
   try:
    changed=compute_harness_hash(ROOT).sha256;assert changed!=pin
    admissions_before=(HERE/'docker-calls.jsonl').read_bytes()
    prior=snapshot(director,planner,run);save(f'{ordinal}-mutated-before.json',{'path':relative,'original_sha256':hashlib.sha256(original).hexdigest(),'mutated_sha256':hashlib.sha256(mutated).hexdigest(),'harness_before':pin,'harness_changed':changed,'snapshot':prior})
    result=run_one_proposal(director,planner,lease=lease,runner=runner,run_id=run,ordinal=ordinal,parent_experiment_id=parent.experiment_id,parent_tree_sha256=tree,parent_source=source,suite_id=manifest.suite_id,suite_version=manifest.suite_version,calibration=calibration,harness_sha256=pin,image_sha256=image,system='S1',context=AgentContext(phase='LOOP',experiment_number=ordinal,task_cards=('One isolated synthetic snapshot tamper safety proof',),champion_source=source.decode(),recent_feedback=()),provider=provider,tasks=tasks,budget=budget,artifact_root=runtime,best_suite=0.0)
    after=snapshot(director,planner,run);strict=read_run_pairs(director,run,artifact_root=runtime);doc=strict[-1].document
    assert doc.decision.verdict=='REJECT' and doc.decision.reason=='harness_hash_mismatch'
    assert doc.suite_score is None and doc.fit_seconds==0.0 and doc.score_seconds==0.0 and not doc.per_task
    assert after['scores']==prior['scores'] and after['jobs']==prior['jobs']
    assert admissions_before==(HERE/'docker-calls.jsonl').read_bytes()
    assert not runner.admission_marker.exists()
    assert after['execution']==prior['execution'] and after['control']==prior['control'] and after['owners']==prior['owners']
    save(f'{ordinal}-rejected-after.json',{'result':result,'snapshot':after,'experiment':doc.model_dump(mode='json',by_alias=True),'strict_pair_count':len(strict)})
    results.append({'path':relative,'reason':doc.decision.reason,'verdict':doc.decision.verdict,'no_new_scores_or_jobs':True})
   finally:p.write_bytes(original)
   assert compute_harness_hash(ROOT).sha256==pin
  save('worker-result.json',{'run_id':str(run),'baseline_actual_scores':9,'rejections':results,'strict_pairs':len(read_run_pairs(director,run,artifact_root=runtime)),'harness_restored':True,'owner':{k:getattr(owner,k) for k in owner.__dataclass_fields__}})
 director.dispose();planner.dispose()
if __name__=='__main__':main()
