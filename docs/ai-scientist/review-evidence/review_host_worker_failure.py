"""Confirm bounded worker timeout/OOM cleanup without touching other processes."""
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
    try:raw=Path(f'/proc/{pid}/stat').read_text()
    except FileNotFoundError:return None
    fields=raw[raw.rfind(')')+2:].split()
    return {'pid':pid,'state':fields[0],'start_ticks':fields[19]}


def child(path,mode):
    descendant=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],start_new_session=True)
    path.write_text(json.dumps({'parent':identity(os.getpid()),'descendant':identity(descendant.pid),
                                'cgroup':Path('/proc/self/cgroup').read_text().strip().split('0::',1)[1]}))
    print('fixture-ready',flush=True)
    if mode=='oom':
        allocation=bytearray(256*1024**2)
        for offset in range(0,len(allocation),4096):allocation[offset]=1
        raise RuntimeError('intentional allocation unexpectedly survived the 64 MiB cgroup')
    time.sleep(30)


def main():
    if len(sys.argv)==4 and sys.argv[1]=='child':
        child(Path(sys.argv[2]),sys.argv[3]);return
    evidence={'checked_at':datetime.now(UTC).isoformat(),'cases':[],
              'scope':'Intentional failure of two probe-owned 64 MiB CPU workers only; independent CPU sentinel preserved. No GPU, AOS, application DB, model or live service mutation. These are isolation fixtures, not Scorer/model throughput measurements.',
              'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    with tempfile.TemporaryDirectory(prefix='worker-failure-review-',dir=ROOT/'data/runtime') as directory:
        sentinel=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],start_new_session=True)
        try:
            for mode in ('timeout','oom'):
                output=Path(directory)/f'{mode}.json';unit=f'swapp-lab-worker-failure-{uuid.uuid4().hex}'
                command=['systemd-run','--user','--wait','--collect','--quiet','--pipe',f'--unit={unit}',
                         '--property=MemoryMax=64M','--property=MemorySwapMax=0','--property=CPUQuota=25%',
                         '--property=TasksMax=16','--property=RuntimeMaxSec=2','--property=TimeoutStopSec=1',
                         '--property=KillMode=control-group','--property=OOMPolicy=stop','--property=NoNewPrivileges=yes',
                         '--setenv=CUDA_VISIBLE_DEVICES=',str(ROOT/'.venv/bin/python'),str(Path(__file__).resolve()),'child',str(output),mode]
                started=time.monotonic();result=subprocess.run(command,capture_output=True,text=True,timeout=10,check=False)
                row={'mode':mode,'command':command,'exit_code':result.returncode,'elapsed_seconds':time.monotonic()-started,
                     'stdout':result.stdout,'stderr':result.stderr,'sentinel_alive':sentinel.poll() is None}
                if output.exists():
                    identities=json.loads(output.read_text());row['identities']=identities
                    row['survivors']=[]
                    for kind in ('parent','descendant'):
                        old=identities[kind];current=identity(old['pid'])
                        if current and current['start_ticks']==old['start_ticks'] and current['state']!='Z':row['survivors'].append(current)
                    row['cgroup_removed']=not (Path('/sys/fs/cgroup')/identities['cgroup'].lstrip('/')).exists()
                journal_command=['journalctl','--user',f'--unit={unit}.service','--no-pager','--output=cat','-n','12']
                journal=subprocess.run(journal_command,capture_output=True,text=True,timeout=5,check=False)
                row['service_journal']={'command':journal_command,'exit_code':journal.returncode,'stdout':journal.stdout,'stderr':journal.stderr}
                expected_reason='oom-kill' if mode=='oom' else 'timeout'
                row['failure_reason_verified']=f"Failed with result '{expected_reason}'" in journal.stdout
                row['passed']=(result.returncode!=0 and not row.get('survivors',[True]) and row.get('cgroup_removed',False)
                               and row['sentinel_alive'] and row['elapsed_seconds']<8 and row['failure_reason_verified'])
                evidence['cases'].append(row);print(json.dumps({key:row[key] for key in ('mode','exit_code','elapsed_seconds','passed')}),flush=True)
        finally:
            sentinel.terminate();sentinel.wait(timeout=5)
    evidence['all_passed']=all(row['passed'] for row in evidence['cases'])
    evidence['command']='.venv/bin/python docs/ai-scientist/review-evidence/review_host_worker_failure.py'
    Path(__file__).with_name('host-worker-failure-review.json').write_text(json.dumps(evidence,indent=2)+'\n')
    if not evidence['all_passed']:raise SystemExit(1)


if __name__=='__main__':main()
