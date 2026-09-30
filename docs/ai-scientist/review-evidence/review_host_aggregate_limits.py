"""Verify aggregate user-slice limits over two owned transient CPU services."""
from __future__ import annotations
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]

def call(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=10, check=False)


def main():
    tag = uuid4().hex
    slice_name = f'swapp_review_aggregate_{tag}.slice'
    units = [f'swapp-review-aggregate-{tag}-{i}.service' for i in range(2)]
    record = {'checked_at':datetime.now(UTC).isoformat(), 'slice':slice_name, 'units':units,
              'scope':'Only two owned CPU fixture services. Measures effective parent cgroup limits; no AOS, model, GPU or database changes.', 'checks':{}}
    processes = []
    cgroup_path = None
    try:
        create = call(['busctl','--user','call','org.freedesktop.systemd1', '/org/freedesktop/systemd1',
            'org.freedesktop.systemd1.Manager','StartTransientUnit','ssa(sv)a(sa(sv))',slice_name,'fail','7',
            'Description','s','Owned AI Scientist aggregate limit review',
            'MemoryMax','t',str(128*1024**2),'MemorySwapMax','t','0',
            'CPUQuotaPerSecUSec','t','250000','TasksMax','t','24',
            'CollectMode','s','inactive-or-failed','CPUAccounting','b','true','0'])
        record['create_exit_code'] = create.returncode
        if create.returncode:
            raise RuntimeError(create.stderr.strip())
        with tempfile.TemporaryDirectory(prefix='aggregate-review-', dir=ROOT/'data/runtime') as work:
            work_path = Path(work)
            child = '''import json,os,pathlib,sys,time
root=pathlib.Path(sys.argv[1]); index=sys.argv[2]
allocation=bytearray(16*1024*1024)
for i in range(0,len(allocation),4096): allocation[i]=1
(root/(index+".json")).write_text(json.dumps({"pid":os.getpid(),"cgroup":pathlib.Path("/proc/self/cgroup").read_text().strip().split("::",1)[1]}))
deadline=time.monotonic()+8
while not (root/"release").exists() and time.monotonic()<deadline: time.sleep(.02)
'''
            for index, unit in enumerate(units):
                args = ['systemd-run','--user','--quiet','--wait','--collect','--pipe',f'--unit={unit}',
                    f'--slice={slice_name}','--property=MemoryMax=96M','--property=MemorySwapMax=0',
                    '--property=CPUQuota=100%','--property=TasksMax=16','--property=RuntimeMaxSec=10',
                    '--property=TimeoutStopSec=1','--property=KillMode=control-group',
                    '--property=OOMPolicy=stop','--property=NoNewPrivileges=yes',sys.executable,'-c',child,work,str(index)]
                processes.append(subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True))
            deadline = time.monotonic()+6
            while not all((work_path/f'{i}.json').exists() for i in range(2)) and time.monotonic()<deadline:
                time.sleep(.03)
            workers = [json.loads((work_path/f'{i}.json').read_text()) for i in range(2)]
            show = call(['systemctl','--user','show',slice_name,'--property=ControlGroup','--value'])
            if show.returncode or not show.stdout.strip(): raise RuntimeError('own slice cgroup unavailable')
            cgroup = show.stdout.strip()
            cgroup_path = Path('/sys/fs/cgroup') / cgroup.lstrip('/')
            values = {name:(cgroup_path/name).read_text().strip() for name in ('memory.max','memory.swap.max','cpu.max','pids.max','memory.current','pids.current')}
            record.update(workers=workers, aggregate_cgroup=cgroup, aggregate_values=values)
            record['checks'].update(parent_memory_cap=values['memory.max']==str(128*1024**2),
                parent_swap_disabled=values['memory.swap.max']=='0',parent_cpu_cap=values['cpu.max']=='25000 100000',
                parent_tasks_cap=values['pids.max']=='24', distinct_workers=workers[0]['pid']!=workers[1]['pid'],
                both_share_capped_parent=all(row['cgroup'].startswith(cgroup+'/') for row in workers),
                both_alive=all(Path(f'/proc/{row["pid"]}').exists() for row in workers))
            (work_path/'release').write_text('done')
            exits=[]
            for process in processes:
                stdout,stderr=process.communicate(timeout=10)
                exits.append(process.returncode)
            record['worker_exit_codes']=exits
            record['checks']['workers_finished']=exits==[0,0]
    except Exception as error:
        record['error']={'type':type(error).__name__,'message':str(error)[:800]}
    finally:
        for unit in units:
            call(['systemctl','--user','stop',unit])
        call(['systemctl','--user','stop',slice_name])
        for process in processes:
            if process.poll() is None:
                process.communicate(timeout=10)
        if cgroup_path is not None:
            record['checks']['own_cgroup_removed']=not cgroup_path.exists()
    record['all_passed']=not record.get('error') and bool(record['checks']) and all(record['checks'].values())
    record['command']='.venv/bin/python docs/ai-scientist/review-evidence/review_host_aggregate_limits.py'
    record['script_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    Path(__file__).with_name('host-aggregate-limits-review.json').write_text(json.dumps(record,indent=2)+'\n')
    print(json.dumps(record,indent=2))
    if not record['all_passed']: raise SystemExit(1)

if __name__=='__main__':main()
