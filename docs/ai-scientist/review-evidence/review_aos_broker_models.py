"""Opt-in real AOS model calls through isolated UDS broker and owned units.

This checks actual Decider and Bonsai recovery calls with synthetic task input.
The optional Qwen mode observes two-owner model progress and queue alternation.
Neither mode runs a Lab research/Scorer suite or a full desktop acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback
from uuid import uuid4

from review_gpu_host import GIB, snapshot as host_snapshot

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "data/runtime/aos-coexistence/rebase-f16d3ced7e6d430eb9b2e11913dac720/merged"
DRAFT = ROOT / "data/runtime/gpu-next-draft"
AOS_PYTHON = ROOT / "data/runtime/aos-coexistence/.venv/bin/python"
BROKER_UNIT = "swapp-lab-gpu-broker.service"
SYSTEMCTL = "/usr/bin/systemctl"

PEER = r'''
import asyncio, hashlib, json, os, sys, time, traceback
from pathlib import Path
from aos.contracts import State, Option, digest
from aos.reusable_decider import ReusableDeciderEngine
from aos.supervisor import BonsaiSupervisor, RecoveryPlan
from aos.vision import BonsaiVisionSupervisor, VisionScene
from aos.gpu_turn import AOSGpuTurnClient
root=Path(sys.argv[1]); source=Path(sys.argv[2]); mode=sys.argv[3]
manifest=Path('/home/cachyos/aos/models/decider-manifest.json')
bonsai_manifest=Path('/home/cachyos/aos/models/bonsai-manifest.json')
decider=ReusableDeciderEngine(manifest, Path('/home/cachyos/.venv/bin/python'))
bonsai=BonsaiSupervisor(bonsai_manifest)
vision=BonsaiVisionSupervisor(bonsai_manifest)
if mode == 'prepare':
 entries={}
 for key,kind,engine,mf,schema in [
  ('aos.decider.turn.v1','decider',decider,manifest,None),
  ('aos.bonsai.recovery.v1','bonsai-recovery',bonsai,bonsai_manifest,RecoveryPlan),
  ('aos.bonsai.vision.v1','bonsai-vision',vision,bonsai_manifest,VisionScene)]:
  pins=json.loads(mf.read_bytes()); roots=('model_path','code_path') if kind=='decider' else ('model_path','runtime_path')
  entries[key]={'kind':kind,'manifest':str(mf),'manifest_sha256':hashlib.sha256(mf.read_bytes()).hexdigest(),
   'deployment_digest':digest(engine.pins),'python':str(Path('/home/cachyos/.venv/bin/python') if kind=='decider' else Path(sys.executable)),
   'model_paths':[pins[k] for k in roots],
   'budgets':{'activation_seconds':180,'inference_seconds':60,'total_seconds':240,'queue_seconds':900},
   'response_schema_sha256':None if schema is None else digest(schema.model_json_schema()),
   'temperature':pins.get('temperature',0.0),'max_output_tokens':pins.get('max_output_tokens',512),
   'context_tokens':pins.get('context_tokens',1536)}
 config={'schema':'swapp-aos-gpu-profiles.v1','source_root':str(source),'profiles':entries}
 (root/'profiles.json').write_text(json.dumps(config,indent=2));(root/'profiles.json').chmod(0o600)
 raise SystemExit(0)
async def main():
 client=AOSGpuTurnClient(Path('/run/user')/str(os.getuid())/'swapp-gpu/broker.sock',timeout_seconds=300)
 decider.gpu_turn_client=client; bonsai.gpu_turn_client=client
 result={'scope':'real local models, synthetic task input; no tool execution','cases':[]}
 (root/'peer-ready').write_text(str(os.getpid()))
 deadline=time.monotonic()+120
 while not (root/'start').exists() and time.monotonic()<deadline: await asyncio.sleep(.1)
 if not (root/'start').exists(): raise TimeoutError('review parent did not admit start')
 try:
  for kind in ['decider','bonsai']:
   start=time.monotonic();case={'kind':kind};result['cases'].append(case)
   try:
    if kind=='decider':
     state=State(task_id='broker-model-review',run_id=root.name,step_id='decision',runtime_id='isolated-review',
      deployment_id=decider.identity['deployment_id'],owner_lease_id='synthetic-only',
      observation='The authorized hello.txt file is missing. No tool has run.')
     options=[Option(id='write',label='Create the authorized hello.txt file with the exact requested content.'),
      Option(id='ask',label='Ask the human to clarify the request.')]
     answer=await decider.decide(state,options);answer.validate_options(options);metrics=decider.last_metrics
    else:
     evidence=[{'id':'e1','observation':'The authorized hello.txt file is missing. No tool has run.'}]
     answer=await bonsai.plan('Recover the authorized hello.txt creation task using only the allowed observe, create, verify sequence.',evidence)
     answer.validate_evidence(evidence);metrics=bonsai.last_metrics
    case.update({'answer':answer.model_dump(mode='json'),'metrics':metrics,'passed':True})
   except Exception as exc:
    case.update({'error':type(exc).__name__+': '+str(exc),'traceback':traceback.format_exc(),'passed':False})
   case['elapsed_seconds']=time.monotonic()-start
   (root/'peer-result.json').write_text(json.dumps(result,indent=2))
   if not case['passed']: break
 finally: await decider.close()
 return 0 if len(result['cases'])==2 and all(c['passed'] for c in result['cases']) else 1
raise SystemExit(asyncio.run(main()))
'''

QWEN_PEER = r'''
import concurrent.futures, dataclasses, json, os, sqlite3, sys, time, traceback
from pathlib import Path
from uuid import uuid4
from lab.llm.gpu_scheduler import SystemdPrincipalResolver
from lab.llm.native_runtime import OwnedVllmRuntime, DIAGNOSTIC_S1_PROFILE, DIAGNOSTIC_S2_PROFILE
root=Path(sys.argv[1]);database=Path(sys.argv[2]);aos_unit=sys.argv[3];lab_unit=sys.argv[4]
resolver=SystemdPrincipalResolver({'aos':aos_unit,'lab':lab_unit})
def call(profile):
 started=time.monotonic(); result={'profile_id':profile.profile_id,'request_id':uuid4().hex}
 try:
  engine=OwnedVllmRuntime(database,principal_resolver=resolver,profile=profile)
  kwargs={}
  if profile.system=='S2':
   kwargs={'response_schema_name':'shared_gpu_arithmetic', 'response_schema':{
    'type':'object','properties':{'answer':{'type':'integer'}},'required':['answer'],'additionalProperties':False}}
  reply=engine.run_turn('lab',result['request_id'],[
   {'role':'system','content':'Answer the arithmetic question exactly in the requested format.'},
   {'role':'user','content': 'What is 1 + 1? Return only '+('the integer 2.' if profile.system=='S1' else 'a JSON object with the integer field answer.')}],
   enable_thinking=profile.enable_thinking,profile=profile,**kwargs)
  result['reply']=dataclasses.asdict(reply)
  result['passed']=reply.text.strip()=='2' if profile.system=='S1' else json.loads(reply.text)=={'answer':2}
 except Exception as exc:
  result.update({'passed':False,'error':type(exc).__name__+': '+str(exc),'traceback':traceback.format_exc()})
 result['elapsed_seconds']=time.monotonic()-started
 (root/(profile.system.lower()+'-qwen-result.json')).write_text(json.dumps(result,indent=2))
 return result
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
 futures=[pool.submit(call,DIAGNOSTIC_S1_PROFILE)]
 deadline=time.monotonic()+60; active=False
 while time.monotonic()<deadline:
  with sqlite3.connect(database) as db:
   active=db.execute('SELECT active_owner FROM gpu_turn_state WHERE singleton=1').fetchone()[0]=='lab'
  if active or futures[0].done(): break
  time.sleep(.1)
 if active: futures.append(pool.submit(call,DIAGNOSTIC_S2_PROFILE))
 results=[future.result() for future in futures]
(root/'qwen-result.json').write_text(json.dumps({'scope':'two real Qwen diagnostic calls, no research/Scorer run','cases':results},indent=2))
raise SystemExit(0 if len(results)==2 and all(r['passed'] for r in results) else 1)
'''


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def show(unit: str) -> dict[str, str]:
    result = subprocess.run([SYSTEMCTL, "--user", "show", unit, "--no-pager",
        "--property=LoadState,ActiveState,MainPID,InvocationID,Description,ControlGroup"],
        capture_output=True, text=True, timeout=5, check=True)
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def launch(unit: str, description: str, argv: list[str], directory: Path, environment: dict[str, str], *, memory: str, seconds: int, gpu_slice: bool = False) -> None:
    if show(unit).get("LoadState") != "not-found":
        raise RuntimeError("review service name is already occupied")
    command = ["/usr/bin/systemd-run", "--user", "--quiet", "--collect", "--service-type=exec",
        "--unit=" + unit, "--description=" + description,
        "--property=MemoryMax=" + memory, "--property=MemorySwapMax=0", "--property=CPUQuota=100%",
        "--property=TasksMax=64", "--property=RuntimeMaxSec=" + str(seconds),
        "--property=TimeoutStopSec=5", "--property=KillMode=control-group",
        "--property=StandardOutput=append:" + str(directory / (unit + ".stdout")),
        "--property=StandardError=append:" + str(directory / (unit + ".stderr")),
        "--property=WorkingDirectory=" + str(ROOT)]
    command += ["--setenv=" + key + "=" + value for key, value in environment.items()]
    if gpu_slice:
        command.append('--slice=swapp-gpu.slice')
    subprocess.run(command + argv, check=True, timeout=15, capture_output=True)


def read_bindings(connection: sqlite3.Connection, include_lab: bool) -> list[dict]:
    connection.row_factory=sqlite3.Row
    rows=[dict(row) for row in connection.execute('SELECT * FROM aos_gpu_child_bindings')]
    for row in rows: row['expected_description']='SWAPP AOS GPU turn '+row['nonce']
    if include_lab and connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='gpu_runtime_bindings'").fetchone():
        for value in connection.execute('SELECT * FROM gpu_runtime_bindings'):
            row=dict(value);row.update({'profile_id':'qwen-local-diagnostic','workdir':row['uds_directory']});rows.append(row)
    return rows


def run(output: Path, execute: bool, with_qwen: bool = False, production_broker: bool = False) -> int:
    if output.exists():
        raise ValueError("refusing to replace evidence")
    os.umask(0o077)
    identity = uuid4().hex
    directory = ROOT / "data/runtime" / ("gpu-model-review-" + identity)
    directory.mkdir(mode=0o700)
    for name in ("lab_gpu_broker.py", "lab_gpu_executor.py", "lab_gpu_service.py"):
        shutil.copyfile(DRAFT / name, directory / name)
    (directory / "peer.py").write_text(PEER)
    if with_qwen: (directory/'qwen_peer.py').write_text(QWEN_PEER)
    tracked = [Path(__file__).resolve(), Path(__file__).with_name("review_gpu_host.py")] + list(SOURCE.rglob("*.py")) + [ROOT / "lab/llm" / name for name in (
        "native_runtime.py", "gpu_scheduler.py", "netns_exec.py")]
    if production_broker:
        tracked += [ROOT/'lab/llm'/name for name in ('aos_gpu_broker.py','aos_gpu_executor.py','aos_gpu_service.py')]
    source_hashes = {str(path): sha(path) for path in tracked if "__pycache__" not in path.parts}
    frozen_hashes = {path.name: sha(path) for path in directory.glob("*.py")}
    env = {**os.environ, "PYTHONPATH": str(SOURCE / "src"), "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}
    prepared = subprocess.run([str(AOS_PYTHON), str(directory / "peer.py"), str(directory), str(SOURCE), "prepare"],
        env=env, capture_output=True, text=True, timeout=30, check=False)
    record = {"schema": "aos-real-broker-model-review.v1", "scope": __doc__, "directory": str(directory.relative_to(ROOT)),
        "source_sha256": source_hashes, "frozen_sha256": frozen_hashes,
        "prepare_exit_code": prepared.returncode, "prepare_stdout": prepared.stdout, "prepare_stderr": prepared.stderr,
        "with_qwen":with_qwen,"production_broker":production_broker,"executed_models":False,"checks": {"profiles_prepared": prepared.returncode == 0}, "samples": [], "cleanup": []}
    if prepared.returncode or not execute:
        record["executed_models"] = False
        output.write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps({"output": str(output), "prepared": prepared.returncode == 0, "models_executed": False}))
        return prepared.returncode
    sys.path.insert(0, str(ROOT))
    from lab.llm.native_runtime import NvidiaSmiObserver, SystemdUnitManager
    observer, units = NvidiaSmiObserver(), SystemdUnitManager()
    runtime = Path(f"/run/user/{os.getuid()}")
    shared = runtime / "swapp-gpu"
    state_dir=Path.home()/'.local/state/swapp-gpu' if production_broker else shared
    record['state_directory']=str(state_dir)
    peer_unit = "swapp-aos-gpu-model-review-" + identity + ".service"
    lab_unit = "swapp-lab-gpu-model-review-" + identity + ".service"
    descriptions = {BROKER_UNIT: "SWAPP isolated broker model review " + identity,
                    peer_unit: "SWAPP isolated AOS peer model review " + identity,
                    lab_unit: "SWAPP isolated Qwen peer model review " + identity}
    owned: dict[str, dict[str, str]] = {}
    database = state_dir / "arbiter.sqlite3"
    started = time.monotonic()
    try:
        preflight = host_snapshot()
        record["host_preflight"] = preflight
        if (preflight["memory_available_bytes"] < 16 * GIB
                or preflight["disk_available_bytes"] < 20 * GIB
                or preflight["gpu"]["temperature_c"] >= 83):
            raise RuntimeError("fresh host preflight refuses model activation")
        gpu = observer.snapshot()
        if set(gpu.process_memory_mib) - set(gpu.exempt_display_pids):
            raise RuntimeError("external GPU process present at preflight")
        if shared.exists() or shared.is_symlink() or show(BROKER_UNIT).get("LoadState") != "not-found":
            raise RuntimeError("shared runtime is already occupied; no existing service changed")
        if production_broker and (state_dir.exists() or state_dir.is_symlink()):
            raise RuntimeError('persistent GPU state already exists; no existing state changed')
        shared.mkdir(mode=0o700)
        (shared / "review-owner").write_text(identity)
        if production_broker:
            state_dir.mkdir(mode=0o700,parents=True)
            (state_dir/'review-owner').write_text(identity)
            units.ensure_slice()
        model_env = {"XDG_RUNTIME_DIR": str(runtime), "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + str(runtime / "bus"),
            "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}
        broker_argv=[str(ROOT/'.venv/bin/python'),'-m','lab.llm.aos_gpu_service'] if production_broker else [str(ROOT / ".venv/bin/python"), str(directory / "lab_gpu_service.py")]
        launch(BROKER_UNIT, descriptions[BROKER_UNIT], broker_argv, directory,
            {**model_env, "PYTHONPATH": str(ROOT), "SWAPP_GPU_PROFILE_CONFIG": str(directory / "profiles.json"),
             'SWAPP_GPU_STATE_DIR':str(state_dir),
             "SWAPP_AOS_GPU_UNIT": peer_unit, "SWAPP_LAB_GPU_UNIT": lab_unit}, memory="512M", seconds=650)
        owned[BROKER_UNIT] = show(BROKER_UNIT)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (shared / "broker.sock").exists(): time.sleep(.1)
        if not (shared / "broker.sock").exists(): raise RuntimeError("broker did not publish its socket")
        launch(peer_unit, descriptions[peer_unit], [str(AOS_PYTHON), str(directory / "peer.py"), str(directory), str(SOURCE), "execute"], directory,
            {**model_env, "PYTHONPATH": str(SOURCE / "src")}, memory="1G", seconds=620,gpu_slice=production_broker)
        owned[peer_unit] = show(peer_unit)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (directory / "peer-ready").exists(): time.sleep(.1)
        if not (directory / "peer-ready").exists(): raise RuntimeError("AOS peer did not become ready")
        if with_qwen:
            launch(lab_unit,descriptions[lab_unit],[str(ROOT/'.venv/bin/python'),str(directory/'qwen_peer.py'),str(directory),str(database),peer_unit,lab_unit],directory,
                {**model_env,'PYTHONPATH':str(ROOT)},memory='1G',seconds=610,gpu_slice=True)
            owned[lab_unit]=show(lab_unit)
        else:
            (directory / "start").write_text(identity)
        record["executed_models"] = True
        while time.monotonic() - started < 615:
            host = host_snapshot()
            reserves = (host["memory_available_bytes"] >= 6 * GIB
                and host["disk_available_bytes"] >= 20 * GIB
                and host["gpu"]["temperature_c"] < 83)
            gpu = observer.snapshot()
            bindings = []
            tickets = []
            if database.exists():
                with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
                    db.row_factory = sqlite3.Row
                    bindings = read_bindings(db,with_qwen)
                    tickets=[dict(row) for row in db.execute('SELECT * FROM gpu_turn_requests')]
                    if with_qwen and not (directory/'start').exists() and len([row for row in tickets if row['owner']=='lab'])>=2:
                        (directory/'start').write_text(identity)
            owned_pids = set()
            resources = {}
            for row in bindings:
                state = show(row["unit"])
                if state["LoadState"] != "not-found" and state["Description"] == row['expected_description']:
                    if row["invocation_id"] and state["InvocationID"] != row["invocation_id"]:
                        raise RuntimeError("owned model generation changed")
                    if state["ControlGroup"]:
                        owned_pids.update(units.cgroup_pids(state["ControlGroup"]))
                        group=Path('/sys/fs/cgroup')/state['ControlGroup'].lstrip('/')
                        try:
                            resources[row['unit']]={
                                key:(group/key).read_text().strip()
                                for key in ('memory.current','memory.peak','memory.max','memory.swap.max','cpu.max','cpu.stat','pids.max')}
                        except FileNotFoundError:
                            pass  # A naturally exited child may disappear between observations.
                log=Path(row['workdir'])/'worker.log'
                try:
                    record.setdefault('observed_worker_logs',{})[row['request_id']]=log.read_bytes()[-8192:].decode(errors='replace')
                except FileNotFoundError:
                    pass
            foreign = set(gpu.process_memory_mib) - set(gpu.exempt_display_pids) - owned_pids
            if foreign:
                # Avoid attributing a GPU PID sampled just before natural exit
                # as foreign merely because its cgroup disappeared afterward.
                fresh_gpu=observer.snapshot()
                foreign &= set(fresh_gpu.process_memory_mib)
            record["samples"].append({"elapsed_seconds": time.monotonic()-started, "gpu_mib": gpu.used_memory_mib,
                "host": host, "host_reserves_maintained": reserves,
                "gpu_pids": dict(gpu.process_memory_mib), "foreign_pids": sorted(foreign),
                "model_resources": resources,
                "queue":[{k:row[k] for k in ('owner','request_id','sequence','state')} for row in tickets],
                "turns": [{k:row[k] for k in ("request_id","launch_state","profile_id","unit","invocation_id")} for row in bindings]})
            if foreign: raise RuntimeError("external unadmitted GPU process appeared; preserving it")
            if not reserves: raise RuntimeError("host reserve or GPU temperature threshold reached")
            state = show(peer_unit)
            peer_done=state["LoadState"] == "not-found" or state["MainPID"] == "0"
            lab_done=not with_qwen or show(lab_unit)['MainPID']=='0'
            if with_qwen and lab_done and not (directory/'start').exists():
                raise RuntimeError('Qwen controller ended before two-owner admission')
            if peer_done and lab_done: break
            time.sleep(.5)
        else: raise TimeoutError("actual model review deadline expired")
        if (directory / "peer-result.json").exists():
            record["peer_result"] = json.loads((directory / "peer-result.json").read_bytes())
        cases = record.get("peer_result", {}).get("cases", [])
        record["checks"]["two_real_typed_model_results"] = len(cases) == 2 and all(case.get("passed") for case in cases)
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            record["bindings"] = [dict(row) for row in db.execute("SELECT * FROM aos_gpu_child_bindings")]
            record["results"] = [dict(row) for row in db.execute("SELECT * FROM aos_gpu_turn_results")]
            record["queue"] = [dict(row) for row in db.execute("SELECT * FROM gpu_turn_requests")]
            if with_qwen:
                record['lab_bindings']=[dict(row) for row in db.execute('SELECT * FROM gpu_runtime_bindings')]
        if with_qwen:
            if (directory/'qwen-result.json').exists(): record['qwen_result']=json.loads((directory/'qwen-result.json').read_bytes())
            cases=record.get('qwen_result',{}).get('cases',[])
            record['checks']['two_real_qwen_answers']=len(cases)==2 and all(case.get('passed') for case in cases)
            all_bindings=record['bindings']+record['lab_bindings']
            record['checks']['alternating_actual_model_generations']=[row['owner'] for row in sorted(all_bindings,key=lambda x:x['fencing_token'])]==['lab','aos','lab','aos']
            record['checks']['four_tickets_completed']=len(record['queue'])==4 and all(row['state']=='done' for row in record['queue'])
        record["checks"]["two_turns_drained"] = len(record["bindings"]) == 2 and all(row["launch_state"] == "drained" for row in record["bindings"])
        record["checks"]["two_results_completed"] = len(record["results"]) == 2 and all(row["state"] == "completed" for row in record["results"])
    except Exception as exc:
        record["error"] = {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
        record["checks"]["review_completed"] = False
    finally:
        if (database.exists() and (shared/'review-owner').is_file()
                and (state_dir/'review-owner').is_file()
                and (shared / "review-owner").read_text() == identity
                and (state_dir/'review-owner').read_text()==identity):
            try:
                with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
                    db.row_factory = sqlite3.Row
                    bindings = read_bindings(db,with_qwen)
                for row in bindings:
                    state = show(row["unit"])
                    if state["LoadState"] != "not-found" and state["MainPID"] != "0":
                        if state["Description"] != row['expected_description'] or (row["invocation_id"] and state["InvocationID"] != row["invocation_id"]):
                            raise RuntimeError("model cleanup identity mismatch; left untouched")
                        units.stop(row["unit"], timeout_seconds=10)
                    record["cleanup"].append({"unit":row["unit"],"state":show(row["unit"])})
                    log=Path(row["workdir"])/"worker.log"
                    if log.exists():
                        record.setdefault("worker_logs",{})[row["request_id"]]=log.read_bytes()[-8192:].decode(errors="replace")
            except Exception as exc: record["cleanup_error"] = repr(exc)
        for unit, binding in reversed(list(owned.items())):
            state=show(unit)
            if state["LoadState"] != "not-found" and state["MainPID"] != "0":
                if state["Description"] == descriptions[unit] and state["InvocationID"] == binding["InvocationID"]:
                    subprocess.run([SYSTEMCTL,"--user","stop",unit],check=True,timeout=10,capture_output=True)
                else: record["cleanup_error"]="service generation mismatch; left untouched"
            record["cleanup"].append({"unit":unit,"state":show(unit)})
        record["checks"]["source_stable"] = all(path.is_file() and sha(path)==checksum for name,checksum in source_hashes.items() for path in [Path(name)])
        record["checks"]["frozen_source_stable"] = all(sha(directory/name)==checksum for name,checksum in frozen_hashes.items())
        final=observer.snapshot();record["final_gpu_mib"]=final.used_memory_mib
        record["checks"]["no_compute_gpu_remaining"] = not (set(final.process_memory_mib)-set(final.exempt_display_pids))
        record["checks"]["owned_process_cleanup"] = not record.get("cleanup_error") and all(item["state"]["MainPID"]=="0" for item in record["cleanup"])
        record["checks"]["host_reserves_maintained"] = bool(record["samples"]) and all(item["host_reserves_maintained"] for item in record["samples"])
        record["elapsed_seconds"]=time.monotonic()-started
        record["overall_exit_code"]=0 if all(record["checks"].values()) else 1
        for path in directory.glob("*.stderr"):
            record.setdefault("service_logs",{})[path.name]=path.read_bytes()[-8192:].decode(errors="replace")
        output.write_text(json.dumps(record,indent=2,sort_keys=True)+"\n")
    print(json.dumps({"output":str(output),"checks":record["checks"],"exit_code":record["overall_exit_code"]}))
    return record["overall_exit_code"]


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--execute",action="store_true")
    parser.add_argument('--with-qwen',action='store_true',help='Queue two real Qwen diagnostic turns against the two AOS calls.')
    parser.add_argument('--production-broker',action='store_true',help='Execute the tracked lab.llm.aos_gpu_service package.')
    args=parser.parse_args()
    if args.with_qwen and not args.production_broker:
        parser.error('--with-qwen requires --production-broker')
    raise SystemExit(run(args.output,args.execute,args.with_qwen,args.production_broker))
