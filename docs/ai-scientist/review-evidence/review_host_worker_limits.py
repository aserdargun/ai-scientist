"""Probe only our transient user service: cgroup limits and setsid child cleanup."""
from __future__ import annotations
from datetime import UTC,datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

ROOT=Path(__file__).resolve().parents[3]


def identity(pid):
    try:
        raw=Path(f'/proc/{pid}/stat').read_text()
    except FileNotFoundError:
        return None
    fields=raw[raw.rfind(')')+2:].split()
    return {'pid':pid,'state':fields[0],'start_ticks':fields[19]}


def main():
    if len(sys.argv)==3 and sys.argv[1]=='child':
        output=Path(sys.argv[2])
        relative=Path('/proc/self/cgroup').read_text().strip().split('0::',1)[1]
        cgroup=Path('/sys/fs/cgroup')/relative.lstrip('/')
        limits={name:(cgroup/name).read_text().strip() for name in ('memory.max','memory.swap.max','cpu.max','pids.max','memory.current')}
        descendant=subprocess.Popen([sys.executable,'-c','import time; time.sleep(45)'],start_new_session=True)
        allocation=bytearray(16*1024**2)
        for offset in range(0,len(allocation),4096):allocation[offset]=1
        output.write_text(json.dumps({'pid':os.getpid(),'descendant':identity(descendant.pid),'cgroup':relative,'limits':limits,'allocation_bytes':len(allocation)}))
        print('probe-ready',flush=True)
        time.sleep(0.2)
        return
    unit=f'swapp-lab-worker-limit-review-{uuid.uuid4().hex}'
    with tempfile.TemporaryDirectory(prefix='worker-limit-review-',dir=ROOT/'data/runtime') as directory:
        record_path=Path(directory)/'child.json'
        command=['systemd-run','--user','--wait','--collect','--quiet','--pipe',f'--unit={unit}',
                 '--property=MemoryMax=128M','--property=MemorySwapMax=0','--property=CPUQuota=25%',
                 '--property=TasksMax=16','--property=RuntimeMaxSec=10','--property=TimeoutStopSec=2',
                 '--property=KillMode=control-group','--property=OOMPolicy=stop','--property=NoNewPrivileges=yes',
                 '--setenv=CUDA_VISIBLE_DEVICES=',str(ROOT/'.venv/bin/python'),str(Path(__file__).resolve()),'child',str(record_path)]
        started=time.monotonic()
        result=subprocess.run(command,capture_output=True,text=True,timeout=20,check=False)
        evidence={'checked_at':datetime.now(UTC).isoformat(),'command':command,'exit_code':result.returncode,
                  'elapsed_seconds':time.monotonic()-started,'stdout':result.stdout,'stderr':result.stderr,
                  'scope':'One 128 MiB / 0.25 CPU / 16 task transient CPU fixture; no model, DB, AOS or GPU mutation. Not a measured Scorer/model memory budget.',
                  'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        if record_path.exists():
            child=json.loads(record_path.read_text());evidence['child']=child
            observed=identity(child['descendant']['pid'])
            evidence['descendant_after_service_exit']=observed
            evidence['descendant_not_running']=(observed is None or observed['start_ticks']!=child['descendant']['start_ticks'] or observed['state']=='Z')
            evidence['cgroup_removed']=not (Path('/sys/fs/cgroup')/child['cgroup'].lstrip('/')).exists()
            quota,period=child['limits']['cpu.max'].split()
            evidence['limits_passed']=(child['limits']['memory.max']==str(128*1024**2)
                                      and child['limits']['memory.swap.max']=='0'
                                      and int(quota)/int(period)==0.25 and child['limits']['pids.max']=='16')
        else:
            evidence.update(limits_passed=False,descendant_not_running=False,cgroup_removed=False)
        evidence['all_passed']=(result.returncode==0 and evidence['limits_passed']
                                and evidence['descendant_not_running'] and evidence['cgroup_removed'])
        Path(__file__).with_name('host-worker-limits-review.json').write_text(json.dumps(evidence,indent=2)+'\n')
        print(json.dumps(evidence,indent=2))
        if not evidence['all_passed']:raise SystemExit(1)


if __name__=='__main__':main()
