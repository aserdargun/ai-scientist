"""Independently inspect native lifecycle foundations without loading a model.

Freeze the current draft in a private review directory while Luna edits it.
Systemd inspection uses one real bounded CPU unit; SQL checks use the actual
scheduler and binding tables with explicitly synthetic unit/GPU observations.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
from lab.llm.gpu_scheduler import PrincipalReceipt, SharedGpuScheduler, _current_process_identity, _process_identity_alive


class FixturePrincipal:
    def resolve(self, owner):
        return PrincipalReceipt(owner,_current_process_identity(),f'swapp-{owner}-gpu-review.service','a'*32)

    def verify(self, receipt):
        return receipt.owner in {'lab','aos'} and _process_identity_alive(receipt.identity)


def main(output: Path) -> int:
    if output.exists():
        raise RuntimeError('refusing to overwrite foundation evidence')
    review_id=uuid.uuid4().hex
    private=ROOT/'data/runtime/native-foundation-review'/review_id
    private.mkdir(parents=True,mode=0o700)
    source=ROOT/'lab/llm/native_runtime.py'
    frozen=private/'native_runtime.py'
    frozen.write_bytes(source.read_bytes())
    (private/'review_driver.py').write_bytes(Path(__file__).read_bytes())
    spec=importlib.util.spec_from_file_location('review_native_frozen',frozen)
    module=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=module
    spec.loader.exec_module(module)
    scheduler_source=ROOT/'lab/llm/gpu_scheduler.py'
    record={'schema':'native-foundation-review.v1','scope':'Real systemd CPU property inspection plus actual SQLite/scheduler with fixture principal, unit and GPU observations; no model or GPU.','frozen_source_path':str(frozen.relative_to(ROOT)),'source_sha256':hashlib.sha256(frozen.read_bytes()).hexdigest(),'gpu_scheduler_sha256':hashlib.sha256(scheduler_source.read_bytes()).hexdigest(),'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'checks':{},'observations':{}}

    def probe(name, call):
        started=time.monotonic()
        try:
            value=call()
            record['checks'][name]=True
            record['observations'][name]={'result':str(value),'seconds':time.monotonic()-started}
        except Exception as error:
            record['checks'][name]=False
            record['observations'][name]={'error':f'{type(error).__name__}: {error}','seconds':time.monotonic()-started}

    probe('aggregate_slice_inspection',lambda:module.SystemdUnitManager._show_properties(module.GPU_SLICE,('LoadState','ControlGroup')))
    unit=f'swapp-lab-gpu-turn-{review_id}.service'
    command=['/usr/bin/systemd-run','--user','--quiet','--wait','--pipe','--collect',f'--unit={unit}','--service-type=exec','--property=MemoryMax=128M','--property=MemorySwapMax=0','--property=CPUQuota=25%','--property=TasksMax=8','--property=RuntimeMaxSec=5','--property=TimeoutStopSec=1','--property=KillMode=control-group',str(ROOT/'.venv/bin/python'),'-c','import time; time.sleep(2)']
    with (private/'stdout.log').open('w') as stdout,(private/'stderr.log').open('w') as stderr:
        process=subprocess.Popen(command,stdout=stdout,stderr=stderr)
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            raw=subprocess.run(['/usr/bin/systemctl','--user','show','--property=MainPID','--property=CPUQuotaPerSecUSec','--property=RuntimeMaxUSec',unit],capture_output=True,text=True,timeout=3,check=True)
            properties=dict(line.split('=',1) for line in raw.stdout.splitlines() if '=' in line)
            if properties.get('MainPID','0')!='0':
                break
            time.sleep(.02)
        record['real_unit_properties']=properties
        record['real_unit_command']=command
        probe('real_active_unit_numeric_limits',lambda:module.SystemdUnitManager().inspect(unit))
        record['cpu_unit_process_exit_code']=process.wait(timeout=8)
        probe('real_collected_unit_inspection',lambda:module.SystemdUnitManager().inspect(unit))

    class FixtureUnits:
        def inspect(self,unit):
            return module.UnitSnapshot('not-found','inactive','','',0,'','',0,0,0,0,0)
    class FixtureGpu:
        def snapshot(self):
            return module.GpuSnapshot({},0,16376)
    class IsolateSqlRuntime(module.OwnedVllmRuntime):
        @staticmethod
        def _expected_control_group(unit,observed):
            return observed.endswith('/'+unit)

    def runtime_case(name, *, initialize_scheduler_first=True):
        parent=private/name
        parent.mkdir(mode=0o700)
        if initialize_scheduler_first:
            SharedGpuScheduler(parent/'runtime.sqlite',principal_resolver=FixturePrincipal(),drain_verifier=lambda _lease:False)
        runtime=IsolateSqlRuntime(parent/'runtime.sqlite',principal_resolver=FixturePrincipal(),pin=module.ModelPin(parent,'fixture','b'*64,()),unit_manager=FixtureUnits(),gpu_observer=FixtureGpu())
        runtime.scheduler.submit('lab',name,b'fixture')
        lease=runtime.scheduler.try_acquire('lab',name)
        if lease is None:
            raise RuntimeError('fixture could not acquire its CPU-only lease')
        row=runtime._prepare_unit(lease)
        runtime._set_launch_result(lease,'created')
        return runtime,lease,row

    probe('fresh_runtime_database_initialization',lambda:runtime_case('fresh-database',initialize_scheduler_first=False)[0])

    def bind_created():
        runtime,lease,row=runtime_case('bind-created')
        snapshot=module.UnitSnapshot('loaded','active','c'*32,'/fixture/'+row['unit'],os.getpid(),row['expected_description'],'SWAPP_GPU_TURN_NONCE='+row['nonce'],module.MODEL_UNIT_MEMORY_BYTES,0,module.MODEL_UNIT_CPU_PERCENT*10000,module.MODEL_UNIT_TASKS,module.MODEL_RUNTIME_MAX_SECONDS*1000000)
        runtime._bind_unit(lease,row,snapshot)
        return 'binding succeeded'
    probe('created_launch_can_bind_identity',bind_created)

    def release_without_lock():
        runtime,lease,row=runtime_case('release-drained')
        runtime.scheduler.release(lease)
        return 'verified absent unit released without SQLite writer conflict'
    probe('scheduler_release_with_runtime_verifier',release_without_lock)
    record['gpu_scheduler_sha256_after']=hashlib.sha256(scheduler_source.read_bytes()).hexdigest()
    record['sources_unchanged']=record['gpu_scheduler_sha256']==record['gpu_scheduler_sha256_after'] and record['source_sha256']==hashlib.sha256(source.read_bytes()).hexdigest()
    record['passed']=all(record['checks'].values()) and record['sources_unchanged']
    output.write_text(json.dumps(record,indent=2,allow_nan=False)+'\n')
    print(json.dumps({'output':str(output),'source_sha256':record['source_sha256'],'checks':record['checks'],'observations':record['observations'],'passed':record['passed']}))
    return 0 if record['passed'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,type=Path)
    raise SystemExit(main(parser.parse_args().output))
