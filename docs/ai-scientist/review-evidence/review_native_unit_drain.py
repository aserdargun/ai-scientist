"""Exercise production drain logic with real CPU units and fixture GPU readings.

No model/CUDA is used. Fixed principal and GPU fixtures are explicit; systemd
InvocationID, MainPID, process start time and cgroup observations are real.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

from review_native_foundation import FixturePrincipal, ROOT


def main(output: Path) -> int:
    if output.exists():
        raise RuntimeError('refusing to overwrite unit-drain evidence')
    private=ROOT/'data/runtime/native-unit-review'/uuid.uuid4().hex
    private.mkdir(parents=True,mode=0o700)
    frozen=private/'native_runtime.py'
    frozen.write_bytes((ROOT/'lab/llm/native_runtime.py').read_bytes())
    (private/'review_driver.py').write_bytes(Path(__file__).read_bytes())
    spec=importlib.util.spec_from_file_location('native_unit_frozen',frozen)
    module=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=module
    spec.loader.exec_module(module)
    units=module.SystemdUnitManager()
    units.ensure_slice()
    scheduler_source=ROOT/'lab/llm/gpu_scheduler.py'
    record={'schema':'native-unit-drain-review.v1','scope':'Actual production drain and SQLite/scheduler against real bounded systemd CPU units; explicit fixture principal and GPU observations, no model/CUDA.','source_sha256':hashlib.sha256(frozen.read_bytes()).hexdigest(),'gpu_scheduler_sha256':hashlib.sha256(scheduler_source.read_bytes()).hexdigest(),'frozen_source_path':str(frozen.relative_to(ROOT)),'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'checks':{},'cases':{},'unit_commands':[]}
    known={}

    class FixtureGpu:
        def __init__(self):
            self.unit=None
            self.pid=0
            self.retain=False
            self.foreign=0
        def snapshot(self):
            processes={}
            if self.pid and (self.retain or (self.unit and units.inspect(self.unit).main_pid==self.pid)):
                processes[self.pid]=64
            if self.foreign:
                processes[self.foreign]=64
            return module.GpuSnapshot(processes,62+sum(processes.values()),16376)

    def launch(row):
        command=[
            '/usr/bin/systemd-run','--user','--quiet','--collect',f"--unit={row['unit']}",
            '--slice=swapp-gpu.slice','--service-type=exec',
            f"--description={row['expected_description']}",
            f'--property=MemoryMax={module.MODEL_UNIT_MEMORY_BYTES}',
            '--property=MemorySwapMax=0',f'--property=CPUQuota={module.MODEL_UNIT_CPU_PERCENT}%',
            f'--property=TasksMax={module.MODEL_UNIT_TASKS}',
            f'--property=RuntimeMaxSec={module.MODEL_RUNTIME_MAX_SECONDS}',
            '--property=TimeoutStopSec=2','--property=KillMode=control-group',
            '--property=NoNewPrivileges=yes',f"--setenv=SWAPP_GPU_TURN_NONCE={row['nonce']}",
            str(ROOT/'.venv/bin/python'),'-c','import time; time.sleep(120)',
        ]
        result=subprocess.run(command,capture_output=True,text=True,timeout=10,check=True)
        snapshot=units.inspect(row['unit'])
        known[row['unit']]=snapshot.invocation_id
        record['unit_commands'].append({'command':command,'exit_code':result.returncode,'invocation_id':snapshot.invocation_id,'main_pid':snapshot.main_pid,'control_group':snapshot.control_group})
        return snapshot

    def setup(name):
        directory=private/name
        directory.mkdir(mode=0o700)
        observer=FixtureGpu()
        runtime=module.OwnedVllmRuntime(directory/'runtime.sqlite',principal_resolver=FixturePrincipal(),pin=module.ModelPin(directory,'fixture','a'*64,()),unit_manager=units,gpu_observer=observer)
        runtime.scheduler.submit('lab',name,b'fixture')
        lease=runtime.scheduler.try_acquire('lab',name)
        if lease is None:
            raise RuntimeError('fixture principal could not acquire a lease')
        row=runtime._prepare_unit(lease)
        runtime._set_launch_result(lease,'created')
        snapshot=launch(row)
        runtime._bind_unit(lease,row,snapshot)
        observer.unit=row['unit']; observer.pid=snapshot.main_pid
        return runtime,lease,row,snapshot,observer

    def terminal(row):
        current=units.inspect(row['unit'])
        return current.main_pid==0 and current.active_state in {'inactive','failed'}

    sentinel=None
    try:
        runtime,lease,row,snapshot,observer=setup('normal')
        runtime.scheduler.release(lease)
        with sqlite3.connect(runtime._database) as connection:
            active=connection.execute('SELECT active_owner FROM gpu_turn_state WHERE singleton=1').fetchone()[0]
        record['checks']['normal_release_clears_scheduler']=active is None
        record['checks']['normal_release_stops_exact_unit']=terminal(row)
        record['checks']['normal_release_cgroup_empty']=units.cgroup_empty(snapshot.control_group)
        record['cases']['normal']={'unit':row['unit'],'invocation_id':snapshot.invocation_id,'main_pid':snapshot.main_pid,'active_owner_after':active}

        runtime,lease,row,snapshot,observer=setup('retained-gpu')
        observer.retain=True
        first=runtime.verify_drained(lease)
        second=runtime.verify_drained(lease)
        record['checks']['retained_gpu_pid_rejects_first_drain']=first is False
        record['checks']['retained_gpu_pid_rejects_repeated_drain']=second is False
        record['checks']['retained_gpu_does_not_leave_cpu_unit_running']=terminal(row)
        observer.retain=False
        recovered=runtime.verify_drained(lease)
        record['checks']['drain_succeeds_after_gpu_pid_released']=recovered is True
        record['cases']['retained_gpu']={'unit':row['unit'],'simulated_retained_gpu_pid':snapshot.main_pid,'first_drain':first,'repeated_drain':second,'after_pid_release':recovered,'binding_has_no_saved_gpu_pids':runtime._binding(lease)['observed_gpu_pids_json']=='[]','scope':'Simulates a supervisor crash after binding and before readiness saved observed GPU PIDs.'}

        runtime,lease,row,snapshot,observer=setup('generation-reuse')
        units.stop(row['unit'],timeout_seconds=10)
        replacement=launch(row)
        observer.pid=replacement.main_pid
        refused=runtime.verify_drained(lease)
        after=units.inspect(row['unit'])
        record['checks']['replacement_has_distinct_invocation']=replacement.invocation_id!=snapshot.invocation_id
        record['checks']['wrong_generation_refused']=refused is False
        record['checks']['replacement_generation_preserved']=after.main_pid==replacement.main_pid and after.active_state=='active'
        record['cases']['generation_reuse']={'unit':row['unit'],'old_invocation':snapshot.invocation_id,'replacement_invocation':replacement.invocation_id,'replacement_pid':replacement.main_pid,'drain_result':refused}
        units.stop(row['unit'],timeout_seconds=10)

        runtime,lease,row,snapshot,observer=setup('foreign-consumer')
        sentinel=subprocess.Popen([str(ROOT/'.venv/bin/python'),'-c','import time; time.sleep(30)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        observer.foreign=sentinel.pid
        refused=runtime.verify_drained(lease)
        record['checks']['foreign_gpu_blocks_handoff']=refused is False
        record['checks']['foreign_gpu_does_not_prevent_owned_stop']=terminal(row)
        record['checks']['foreign_sentinel_preserved']=sentinel.poll() is None
        record['cases']['foreign_consumer']={'unit':row['unit'],'owned_pid':snapshot.main_pid,'fixture_gpu_foreign_pid':sentinel.pid,'drain_result':refused,'owned_unit_terminal':terminal(row),'scope':'Foreign GPU activity is simulated by reporting an owned CPU sentinel PID; no CUDA.'}
    except Exception as error:
        record['error']=f'{type(error).__name__}: {error}'
        record['checks']['execution_without_error']=False
    finally:
        if sentinel is not None and sentinel.poll() is None:
            sentinel.terminate()
            sentinel.wait(timeout=3)
        cleanup=[]
        for unit,invocation in known.items():
            current=units.inspect(unit)
            if current.main_pid:
                if current.invocation_id!=invocation:
                    cleanup.append({'unit':unit,'safe':False,'reason':'unknown replacement left untouched'})
                    continue
                units.stop(unit,timeout_seconds=10)
            final=units.inspect(unit)
            cleanup.append({'unit':unit,'safe':final.main_pid==0 and final.active_state in {'inactive','failed'}})
        record['cleanup']=cleanup
        record['checks']['all_owned_review_units_terminal']=all(item['safe'] for item in cleanup)
    record['gpu_scheduler_sha256_after']=hashlib.sha256(scheduler_source.read_bytes()).hexdigest()
    record['sources_unchanged']=record['gpu_scheduler_sha256']==record['gpu_scheduler_sha256_after'] and record['source_sha256']==hashlib.sha256((ROOT/'lab/llm/native_runtime.py').read_bytes()).hexdigest()
    record['passed']=all(record['checks'].values()) and record['sources_unchanged']
    output.write_text(json.dumps(record,indent=2,allow_nan=False)+'\n')
    print(json.dumps({'output':str(output),'source_sha256':record['source_sha256'],'checks':record['checks'],'error':record.get('error'),'passed':record['passed']}))
    return 0 if record['passed'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    raise SystemExit(main(parser.parse_args().output))
