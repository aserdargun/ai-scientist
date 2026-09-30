"""Deploy only the gated 0.36.2 timing fix; preserve concurrent documentation edits."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from sqlalchemy import create_engine, text
from ops.start_lab import check_unit

ROOT = Path('/home/cachyos/ai-scientist')
SOURCE = ROOT / 'data/runtime/parallel-m0/resume-fix-054'
HERE = ROOT / 'data/runtime/parallel-m0/resume-fix-054-checks'
BACKUP = HERE / 'pre-0362-source'
UNITS = {
    'swapp-ai-scientist-api.service': ('lab.cli', 8766),
    'swapp-ai-scientist-director-drain.service': ('lab.cli', None),
    'swapp-ai-scientist-console.service': ('console.server', 8788),
}
DELTA = {'harness/VERSION', 'ops/sandbox-image.lock', 'lab/director/runner.py', 'lab/director/loop.py',
         'tests/test_proposal_completion054.py'}
EXPECTED_RUNS = {
    '75642033-6e46-4a73-82a9-9e1ecb6066d3': 'stop_requested',
    'b54c9282-ca69-4deb-9e28-ef50e36bf21c': 'running',
    'fac65252-e14d-4014-9744-333b88083963': 'stop_requested',
}
record = {'schema': 'release0362-deployment.v1', 'steps': [],
          'migration_applied': False, 'database_rows_modified': False}

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def save():
    (HERE/'deployment0362.json').write_text(json.dumps(record, indent=2)+'\n')

def unit(name):
    output = subprocess.check_output(['systemctl','--user','show',name,
        '--property=LoadState,ActiveState,MainPID,InvocationID,ControlGroup,WorkingDirectory'], text=True)
    return dict(line.split('=',1) for line in output.splitlines())

def db_state():
    e = create_engine((ROOT/'data/runtime/postgres/director.dsn').read_text().strip(),
        hide_parameters=True, connect_args={'options':'-c statement_timeout=5000 -c default_transaction_read_only=on'})
    p = create_engine((ROOT/'data/runtime/postgres/planner.dsn').read_text().strip(),
        hide_parameters=True, connect_args={'options':'-c statement_timeout=5000 -c default_transaction_read_only=on'})
    try:
        with e.connect() as c:
            rows = dict(c.execute(text("SELECT run_id::text,state FROM lab.runs WHERE state IN ('queued','running','stop_requested')")).all())
        with p.connect() as c:
            jobs = c.execute(text("SELECT job_id FROM scorer.score_jobs WHERE state IN ('queued','running')")).all()
        assert rows == EXPECTED_RUNS and not jobs, 'new work admitted; deployment deferred'
        return rows
    finally:
        e.dispose(); p.dispose()

def restore_services():
    result = subprocess.run(['bash','ops/start-lab.sh'], cwd=ROOT,
        capture_output=True, text=True, timeout=90)
    record['launcher_exit_code'] = result.returncode
    record['launcher_stdout'] = result.stdout
    assert result.returncode == 0, 'owned services require inspection'
    for name, (module, port) in UNITS.items():
        assert check_unit(name,module,port)['ActiveState'] == 'active'

os.umask(0o077)
assert len(sys.argv) == 2 and re.fullmatch(r'gate-[0-9a-f]{12}\.json',sys.argv[1])
gate_path = HERE/sys.argv[1]
gate = json.loads(gate_path.read_text())
assert gate['exit_code'] == 0 and gate['source_unchanged']
commands = json.loads((ROOT/gate['command_record']).read_text())
assert len(commands['commands']) == 7 and all(c['exit_code'] == 0 for c in commands['commands'])
mapping = gate['source_after']
base = json.loads((SOURCE/'review-evidence/resume054-base.json').read_text())['files']
assert DELTA <= mapping.keys()
assert not BACKUP.exists() and not (HERE/'deployment0362.json').exists()
assert (ROOT/'harness/VERSION').read_text().strip() == '0.36.1'
assert (SOURCE/'harness/VERSION').read_text().strip() == '0.36.2'
for name, digest in mapping.items():
    assert sha(SOURCE/name) == digest, name
    if name not in DELTA:
        assert sha(ROOT/name) == digest, 'unrelated source mismatch: '+name
for name in DELTA:
    if name in base:
        assert sha(ROOT/name) == base[name], 'concurrent runtime edit: '+name
    else:
        assert not (ROOT/name).exists(), 'new test already exists: '+name
record['gate_sha256'] = sha(gate_path)
record['preserved_runs'] = db_state()
locks = []
stopped = False
copied = False
try:
    for path in [ROOT/'data/runtime/parallel-m0/cpu-check.lock', ROOT/'data/runtime/director-dispatch.lock']:
        assert not path.is_symlink()
        fd = os.open(path,os.O_RDWR|os.O_CREAT|os.O_CLOEXEC,0o600)
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        locks.append(fd)
    before = {}
    for name,(module,port) in UNITS.items():
        assert check_unit(name,module,port)['ActiveState'] == 'active'
        before[name] = unit(name)
    record['before_units'] = before
    assert db_state() == record['preserved_runs']
    for name in UNITS:
        assert unit(name) == before[name]
        subprocess.run(['systemctl','--user','stop',name],check=True,timeout=25)
        stopped = True
        assert unit(name)['ActiveState'] == 'inactive'
        assert db_state() == record['preserved_runs']
    record['steps'].append('owned_idle_services_stopped'); save()
    BACKUP.mkdir(mode=0o700)
    old = {}
    for name in sorted(DELTA):
        path = ROOT/name
        assert not path.is_symlink()
        old[name] = sha(path) if path.is_file() else None
        if path.exists():
            (BACKUP/name).parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(path,BACKUP/name)
    (BACKUP/'manifest.json').write_text(json.dumps(old,indent=2)+'\n')
    record['copy_started'] = True
    for name in sorted(DELTA):
        shutil.copy2(SOURCE/name,ROOT/name)
        assert sha(ROOT/name) == mapping[name]
    copied = True
    record['steps'].append('five_gated_files_transplanted'); save()
    restore_services()
    assert db_state() == record['preserved_runs']
    assert all(sha(ROOT/name)==digest for name,digest in mapping.items())
    record.update(status='deployed',version='0.36.2',source_after=mapping,
                  changed_files={name:mapping[name] for name in sorted(DELTA)},
                  after_units={name:unit(name) for name in UNITS})
    save()
    print(json.dumps({'status':'deployed','version':'0.36.2','changed_files':len(DELTA),
        'gated_files':len(mapping),'database_rows_modified':False}),flush=True)
except BaseException as exc:
    record['failure_type'] = type(exc).__name__
    if record.get('copy_started') and not copied:
        for name,digest in old.items():
            if digest is None:
                (ROOT/name).unlink(missing_ok=True)
            else:
                shutil.copy2(BACKUP/name,ROOT/name)
                assert sha(ROOT/name)==digest
        record['partial_copy_rolled_back'] = True
    # Once all files match, retain the verified source for inspection on startup failure.
    record['source_transplanted'] = copied
    if stopped:
        try:
            restore_services()
            record['owned_services_restored'] = True
        except Exception as recovery:
            record['service_restore_error_type'] = type(recovery).__name__
    save()
    raise
finally:
    for fd in reversed(locks):
        os.close(fd)
