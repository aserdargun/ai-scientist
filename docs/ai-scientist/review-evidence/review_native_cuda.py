"""Measure a tiny CUDA operation inside an owned private-network systemd unit.

This checks host capabilities only. No model, production runtime or AOS is run.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from review_gpu_host import GIB, ROOT, snapshot

WORKER = '''
import json,os,pathlib,subprocess,sys,time
runtime=pathlib.Path(sys.argv[1])
parent_namespace=sys.argv[2]
namespace=os.readlink('/proc/self/ns/net')
if namespace==parent_namespace: raise RuntimeError('host namespace must not change')
subprocess.run(['/usr/bin/ip','link','set','dev','lo','up'],check=True,timeout=3)
import torch
torch.set_num_threads(2)
tensor=torch.ones(262144,dtype=torch.float32,device='cuda')
result=float((tensor*2).sum().item())
torch.cuda.synchronize()
record={'pid':os.getpid(),'namespace':namespace,'torch':torch.__version__,
        'cuda':torch.version.cuda,'device':torch.cuda.get_device_name(0),
        'capability':list(torch.cuda.get_device_capability(0)),
        'result':result,'allocation_bytes':tensor.numel()*tensor.element_size(),
        'peak_allocated_bytes':torch.cuda.max_memory_allocated(),
        'peak_reserved_bytes':torch.cuda.max_memory_reserved(),
        'cgroup':pathlib.Path('/proc/self/cgroup').read_text().strip(),
        'no_default_route':not any(line.split()[1]=='00000000' for line in pathlib.Path('/proc/net/route').read_text().splitlines()[1:])}
temporary=runtime/'ready.tmp'
temporary.write_text(json.dumps(record))
temporary.replace(runtime/'ready.json')
deadline=time.monotonic()+10
while not (runtime/'release').exists():
    if time.monotonic()>=deadline: raise RuntimeError('review release timed out')
    time.sleep(.05)
print(json.dumps(record),flush=True)
'''


def show(unit: str) -> dict[str, str]:
    fields = ('LoadState','ActiveState','MainPID','InvocationID','ControlGroup',
              'MemoryMax','MemorySwapMax','CPUQuotaPerSecUSec','TasksMax',
              'RuntimeMaxUSec','KillMode','MemoryPeak','Result','ExecMainStatus')
    result = subprocess.run(
        ['/usr/bin/systemctl','--user','show',*[f'--property={field}' for field in fields],unit],
        capture_output=True,text=True,timeout=5,check=True,
    )
    return dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)


def main(output: Path) -> int:
    if output.exists():
        raise RuntimeError('refusing to overwrite CUDA capability evidence')
    task_id = uuid.uuid4().hex
    unit = f'swapp-review-native-cuda-{task_id}.service'
    runtime = Path(f'/run/user/{os.getuid()}') / f'swapp-cuda-{task_id}'
    runtime.mkdir(mode=0o700)
    worker = runtime / 'worker.py'
    worker.write_text(WORKER)
    worker.chmod(0o600)
    parent_namespace = os.readlink('/proc/self/ns/net')
    record = {
        'schema':'native-cuda-capability-review.v1',
        'observed_at':datetime.now(timezone.utc).isoformat(),
        'scope':'Tiny Torch CUDA operation in a private user/network namespace; no model, AOS, scheduler or production lifecycle acceptance.',
        'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'worker_sha256':hashlib.sha256(WORKER.encode()).hexdigest(),
        'unit':unit,'checks':{},'samples':[],
    }
    process = None
    invocation = None
    control_group = None
    ready = {}
    log_out = (runtime/'stdout.log').open('w+')
    log_err = (runtime/'stderr.log').open('w+')
    try:
        before = snapshot()
        record['before'] = before
        safe = (before['memory_available_bytes'] >= 16*GIB
                and before['disk_available_bytes'] >= 20*GIB
                and before['gpu']['temperature_c'] < 83
                and all(item['existing_display_exemption'] for item in before['gpu_consumers']))
        record['checks']['host_preflight_passed'] = safe
        if not safe:
            raise RuntimeError('host preflight failed; no CUDA unit launched')
        argv = [
            '/usr/bin/systemd-run','--user','--quiet','--wait','--pipe','--collect',
            f'--unit={unit}','--service-type=exec','--property=MemoryMax=2G',
            '--property=MemorySwapMax=0','--property=CPUQuota=200%',
            '--property=TasksMax=64','--property=RuntimeMaxSec=30',
            '--property=TimeoutStopSec=2','--property=KillMode=control-group',
            '--property=NoNewPrivileges=yes','--property=UMask=0077',
            '--setenv=CUDA_VISIBLE_DEVICES=0','--setenv=OMP_NUM_THREADS=2',
            '--setenv=OPENBLAS_NUM_THREADS=2','--setenv=HF_HUB_OFFLINE=1',
            '--setenv=VLLM_NO_USAGE_STATS=1',
            '/usr/bin/unshare','--user','--map-root-user','--net',
            str(ROOT/'data/runtime/vllm/.venv/bin/python'),str(worker),str(runtime),parent_namespace,
        ]
        record['command'] = argv
        process = subprocess.Popen(argv,stdout=log_out,stderr=log_err)
        deadline = time.monotonic()+25
        while time.monotonic()<deadline:
            properties = show(unit)
            if properties.get('InvocationID'):
                invocation = properties['InvocationID']
                control_group = properties.get('ControlGroup')
                record['unit_running'] = properties
            observed = snapshot()
            record['samples'].append(observed)
            allowed_pid = int(properties.get('MainPID','0'))
            if any(not item['existing_display_exemption'] and item['pid']!=allowed_pid
                   for item in observed['gpu_consumers']):
                raise RuntimeError('outside GPU consumer appeared; stop only owned probe')
            if (runtime/'ready.json').exists():
                ready = json.loads((runtime/'ready.json').read_text())
                break
            if process.poll() is not None:
                raise RuntimeError('CUDA worker exited before ready')
            time.sleep(.25)
        if not ready:
            raise RuntimeError('CUDA worker readiness deadline exceeded')
        record['worker'] = ready
        record['unit_running'] = show(unit)
        sample = snapshot()
        record['samples'].append(sample)
        checks = record['checks']
        checks['private_namespace'] = ready['namespace'] != parent_namespace
        checks['no_default_route'] = ready['no_default_route'] is True
        checks['exact_cuda_result'] = ready['result'] == 524288.0
        checks['one_mib_tensor'] = ready['allocation_bytes'] == 1024**2
        checks['sm89_device'] = ready['capability'] == [8,9]
        checks['same_host_pid_and_cgroup'] = (str(ready['pid']) == record['unit_running']['MainPID']
                                             and ready['cgroup'] == '0::'+control_group)
        checks['gpu_pid_visible_to_host'] = any(item['pid']==ready['pid'] for item in sample['gpu_consumers'])
        checks['actual_unit_limits'] = all(record['unit_running'].get(key)==value for key,value in {
            'MemoryMax':str(2*GIB),'MemorySwapMax':'0','CPUQuotaPerSecUSec':'2s',
            'TasksMax':'64','RuntimeMaxUSec':'30s','KillMode':'control-group',
        }.items())
        (runtime/'release').write_text('release owned probe')
        record['process_exit_code'] = process.wait(timeout=10)
        checks['worker_exited_zero'] = record['process_exit_code'] == 0
    except Exception as error:
        record['error'] = f'{type(error).__name__}: {error}'
        record['checks']['execution_without_error'] = False
    finally:
        if process is not None and process.poll() is None:
            current = show(unit)
            if invocation and current.get('InvocationID') == invocation:
                stopped = subprocess.run(['/usr/bin/systemctl','--user','stop',unit],capture_output=True,text=True,timeout=8,check=False)
                record['cleanup_stop_exit_code'] = stopped.returncode
            # Unknown generation is never stopped. The unit's own 30 s deadline
            # and control-group termination bound an interrupted launch.
            record['process_exit_code'] = process.wait(timeout=35)
        if process is not None:
            final = show(unit)
            record['unit_terminal'] = final
            record['after'] = snapshot()
            record['checks']['unit_terminal'] = final.get('MainPID')=='0' and final.get('ActiveState') in {'inactive','failed'}
            record['checks']['owned_gpu_pid_gone'] = bool(ready) and all(item['pid']!=ready['pid'] for item in record['after']['gpu_consumers'])
            if control_group:
                path = Path('/sys/fs/cgroup')/control_group.lstrip('/')
                record['checks']['cgroup_drained'] = not path.exists() or (
                    not (path/'cgroup.procs').read_text().split()
                    and 'populated 0' in (path/'cgroup.events').read_text().splitlines())
        record['checks']['host_namespace_unchanged'] = os.readlink('/proc/self/ns/net') == parent_namespace
        log_out.seek(0)
        log_err.seek(0)
        record['stdout'] = log_out.read(8192)
        record['stderr'] = log_err.read(8192)
        log_out.close()
        log_err.close()
        for path in runtime.iterdir():
            path.unlink()
        runtime.rmdir()
    record['passed'] = all(record['checks'].values())
    output.write_text(json.dumps(record,indent=2,allow_nan=False)+'\n')
    print(json.dumps({'output':str(output),'checks':record['checks'],'passed':record['passed'],'error':record.get('error')}))
    return 0 if record['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    raise SystemExit(main(parser.parse_args().output))
