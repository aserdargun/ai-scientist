from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path('/home/cachyos/ai-scientist')
SOURCE = ROOT
PRIVATE = ROOT / 'data/runtime/parallel-m0/release-052-checks'

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def sources():
    paths = [path for name in ('harness','lab','vendor','tests','scripts','console','ops')
             for path in sorted((SOURCE/name).rglob('*'))
             if path.is_file() and not path.is_symlink() and '__pycache__' not in path.parts
             and path.suffix not in {'.pyc','.pyo'}
             and not set(path.relative_to(SOURCE).parts).intersection({
                 'node_modules', '.pytest_cache', '.mypy_cache', '.ruff_cache',
                 '.vite', 'playwright-report', 'test-results', 'coverage'})]
    paths += [SOURCE/name for name in ('harness/VERSION','CONTRACT.md','alembic.ini','pyproject.toml',
              'uv.lock','Dockerfile.sandbox','docker/sandbox/requirements.txt','ops/sandbox-image.lock','ops/public-four-source-050.json')]
    return {str(path.relative_to(SOURCE)):digest(path) for path in paths}

def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser()
    parser.add_argument('--mode',choices=('gate','image'),required=True)
    args=parser.parse_args()
    ident=uuid4().hex[:12]
    output=PRIVATE/f'{args.mode}-{ident}.json'
    log=PRIVATE/f'{args.mode}-{ident}.log'
    parity=SOURCE/f'docs/ai-scientist/review-evidence/stop-closure-051-image-{ident}.json'
    timeout=360 if args.mode=='image' else 300
    inner = ('import pathlib,runpy,lab,harness; '
             'root=pathlib.Path.cwd().resolve(); '
             'assert pathlib.Path(lab.__file__).resolve().is_relative_to(root); '
             'assert pathlib.Path(harness.__file__).resolve().is_relative_to(root); '
             + ("runpy.run_path('scripts/quality_gate.py',run_name='__main__')" if args.mode=='gate'
                else "import sys; sys.argv=['review_director_image.py','--output',"+repr(str(parity))+"]; runpy.run_path('docs/ai-scientist/review-evidence/review_director_image.py',run_name='__main__')"))
    command=['systemd-run','--user','--wait','--collect','--quiet','--pipe',
             f'--unit=swapp-review-release-052-{args.mode}-{ident}.service','--slice=swapp-gpu.slice',
             '--property=MemoryMax=3G','--property=MemorySwapMax=0','--property=CPUQuota=200%',
             '--property=TasksMax=128',f'--property=RuntimeMaxSec={timeout}',
             '--property=TimeoutStopSec=20','--property=KillMode=control-group',
             f'--working-directory={SOURCE}','--setenv=OPENBLAS_NUM_THREADS=1',
             '--setenv=OMP_NUM_THREADS=1','--setenv=MKL_NUM_THREADS=1',
             '--setenv=TMPDIR=/home/cachyos/.m0r10',f'--setenv=PYTHONPATH={SOURCE}',
             f'--setenv=PATH={ROOT}/.venv/bin:/home/cachyos/.local/bin:/usr/local/bin:/usr/bin:/bin',
             str(ROOT/'.venv/bin/python'),'-c',inner]
    with (ROOT/'data/runtime/parallel-m0/cpu-check.lock').open('a') as lock:
        print('Waiting for shared CPU verification lock.',flush=True)
        fcntl.flock(lock,fcntl.LOCK_EX)
        start=time.monotonic()
        before=sources()
        stamp=datetime.now(UTC).isoformat()
        with log.open('wb') as stream:
            result=subprocess.run(command,stdout=stream,stderr=subprocess.STDOUT,
                                  check=False,timeout=timeout+30)
        after=sources()
        record={'schema':'release-052-bound-verification.v1','mode':args.mode,
                'started_at':stamp,'command':command,'source_worktree':str(SOURCE),
                'exit_code':result.returncode,'elapsed_seconds':time.monotonic()-start,
                'source_before':before,'source_after':after,'source_unchanged':before==after,
                'log_path':str(log.relative_to(ROOT)),'log_sha256':digest(log)}
        if args.mode=='gate':
            gate=SOURCE/'docs/ai-scientist/evidence/quality-gate-latest.json'
            if gate.exists():
                archive=PRIVATE/f'gate-{ident}-commands.json'
                archive.write_bytes(gate.read_bytes())
                record['command_record']=str(archive.relative_to(ROOT))
                record['command_record_sha256']=digest(archive)
        else:
            record['image_parity_receipt']=str(parity)
        output.write_text(json.dumps(record,indent=2)+'\n')
        print(json.dumps({key:record[key] for key in ('mode','exit_code','elapsed_seconds','source_unchanged','log_path')}),flush=True)
        print('binding='+str(output),flush=True)
        if result.returncode:
            print(log.read_text()[-18000:],flush=True)
        return result.returncode

if __name__=='__main__':
    sys.exit(main())
