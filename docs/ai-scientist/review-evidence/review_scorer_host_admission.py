"""Prove a fresh Scorer honors its host lock before credential/DB access."""
from __future__ import annotations
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4
from lab.scorer.worker import _try_process_admission_lock

ROOT=Path(__file__).resolve().parents[3]

def main():
    tag=uuid4().hex
    unit=f'swapp-review-scorer-admission-{tag}'
    missing_dsn=ROOT/'data/runtime'/f'absent-review-credential-{tag}.dsn'
    source=ROOT/'lab/scorer/worker.py'
    source_sha=hashlib.sha256(source.read_bytes()).hexdigest()
    record={'checked_at':datetime.now(UTC).isoformat(),'source_sha256':{'lab/scorer/worker.py':source_sha},
        'scope':'Actual fresh CPU worker with host admission lock held. Credential path is deliberately nonexistent. Proves backpressure precedes credential/DB access; does not simulate an established DB connection breaking mid-computation or a GPU workload.', 'checks':{}}
    descriptor=_try_process_admission_lock()
    if descriptor is None:raise RuntimeError('Scorer is busy; review will not interfere')
    started=time.monotonic()
    try:
        command=['systemd-run','--user','--quiet','--wait','--collect','--pipe',f'--unit={unit}',
            '--property=MemoryMax=64M','--property=MemorySwapMax=0','--property=CPUQuota=25%',
            '--property=TasksMax=8','--property=RuntimeMaxSec=5','--property=TimeoutStopSec=1',
            '--property=KillMode=control-group','--property=OOMPolicy=stop','--property=NoNewPrivileges=yes',
            f'--setenv=LAB_SCORER_DSN_FILE={missing_dsn}',f'--working-directory={ROOT}',
            str(ROOT/'.venv/bin/python'),'-m','lab.scorer.worker','--job-id',str(uuid4())]
        result=subprocess.run(command,capture_output=True,text=True,timeout=10,check=False)
        record.update(exit_code=result.returncode,elapsed_seconds=round(time.monotonic()-started,4),
            status=json.loads(result.stdout) if result.returncode==0 else None)
        record['checks']['fresh_worker_returned_capacity_busy']=result.returncode==0 and record['status']=={'state':'capacity_busy'}
        record['checks']['invalid_credentials_not_accessed']=not missing_dsn.exists() and not result.stderr.strip() and record['checks']['fresh_worker_returned_capacity_busy']
    finally:
        subprocess.run(['systemctl','--user','stop',unit],capture_output=True,timeout=5,check=False)
        os.close(descriptor)
    descriptor=_try_process_admission_lock()
    record['checks']['slot_reacquired_after_owner_release']=descriptor is not None
    if descriptor is not None:os.close(descriptor)
    record['source_unchanged']=hashlib.sha256(source.read_bytes()).hexdigest()==source_sha
    record['all_passed']=all(record['checks'].values()) and record['source_unchanged']
    record['script_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    record['command']='.venv/bin/python docs/ai-scientist/review-evidence/review_scorer_host_admission.py'
    Path(__file__).with_name('scorer-host-admission-review.json').write_text(json.dumps(record,indent=2)+'\n')
    print(json.dumps(record,indent=2))
    if not record['all_passed']:raise SystemExit(1)

if __name__=='__main__':main()
