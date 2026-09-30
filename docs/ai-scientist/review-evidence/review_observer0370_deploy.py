"""Reviewed ten-file 0370 deployment; DB0031 and existing ledger rows stay unchanged."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path('/home/cachyos/ai-scientist')
SOURCE = ROOT / 'data/runtime/parallel-m0/observer-integration-053'
HERE = SOURCE / 'review-evidence'
COMMON = ROOT / 'data/runtime/parallel-m0/stopped-proposal-055/docs/ai-scientist/review-evidence/common055_release.py'
TUNNEL = 'swapp-ai-scientist-aserdargun-tunnel.service'
EXPECTED_HEAD = '8cbf4bf3ba5c517727449b84af986b341bb4b4d3'
PAYLOADS = {'lab/sandbox/docker_runner.py', 'lab/sandbox/evaluation.py',
            'lab/sandbox/sandbox_wrapper.py', 'lab/sandbox/syscall_observer.py',
            'tests/test_sandbox_runner.py', 'tests/test_syscall_observer.py',
            'tests/test_syscall_observer_live.py', 'tests/test_syscall_observer_compatibility049.py',
            'harness/VERSION', 'ops/sandbox-image.lock'}
TABLES = ('lab.runs', 'lab.experiments', 'scorer.score_jobs', 'scorer.task_scores',
          'lab.reports', 'lab.director_execution_control', 'lab.director_stop_closures')
COMMON_SHA256 = '87a5a5f631768475b1a8d4807d1e78490492250ca9db2042e9026b702d720ffa'


def load_common():
    sys.path.insert(0, str(ROOT))
    import hashlib
    assert hashlib.sha256(COMMON.read_bytes()).hexdigest() == COMMON_SHA256
    spec = importlib.util.spec_from_file_location('common0370_reviewed', COMMON)
    assert spec is not None and spec.loader is not None
    common = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(common)
    assert set(common.UNITS) == {'swapp-ai-scientist-api.service',
                                'swapp-ai-scientist-director-drain.service',
                                'swapp-ai-scientist-console.service'}
    return common


def tunnel(common):
    value = dict(line.split('=', 1) for line in common.command([
        'systemctl', '--user', 'show', TUNNEL,
        '--property=LoadState,ActiveState,MainPID,InvocationID,ControlGroup,WorkingDirectory',
    ]).splitlines())
    assert value['LoadState'] == 'loaded' and value['ActiveState'] == 'active'
    assert int(value['MainPID']) > 0 and value['InvocationID']
    return value


def snapshot(common):
    from sqlalchemy import text
    database = common.engine(readonly=True)
    try:
        with database.connect() as connection:
            connection.exec_driver_sql('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            revision = connection.execute(text('SELECT version_num FROM lab.alembic_version')).scalar_one()
            jobs = connection.execute(text(
                "SELECT count(*) FROM scorer.score_jobs WHERE state IN ('queued','running')"
            )).scalar_one()
            fac_state = connection.execute(text(
                "SELECT state FROM lab.runs WHERE run_id::text=:run"
            ), {'run': common.TARGET}).scalar_one()
            tables = {}
            for table in TABLES:
                # Identifiers are fixed constants; retain counts and digests, never raw row content.
                rows = connection.exec_driver_sql(
                    f'SELECT to_jsonb(t)::text FROM {table} t ORDER BY to_jsonb(t)::text'
                ).scalars().all()
                tables[table] = {'count': len(rows), 'sha256': common.digest(rows)}
            budgets = connection.execute(text(
                "SELECT to_jsonb(t)::text FROM lab.run_events t "
                "WHERE event_type='director.checkpoint' AND "
                "(event_json->>'key' LIKE '%budget%' OR event_json->>'phase' LIKE '%budget%') "
                "ORDER BY to_jsonb(t)::text"
            )).scalars().all()
        assert revision == '0031_stopped_proposal' and jobs == 0 and fac_state == 'stopped'
        return {'revision': revision, 'active_jobs': jobs, 'fac053_state': fac_state,
                'migrator_dsn_sha256': common.sha(ROOT / 'data/runtime/postgres/migrator.dsn'),
                'tables': tables, 'budgets': {'count': len(budgets), 'sha256': common.digest(budgets)}}
    finally:
        database.dispose()


def main():
    if sys.argv[1:] != ['--execute-reviewed-deploy']:
        print(json.dumps({'inert': True, 'files': sorted(PAYLOADS), 'source': str(SOURCE),
                          'target': '0.37.0', 'database_migration': False,
                          'tunnel_read_only': TUNNEL}))
        return 2
    os.umask(0o077)
    common = load_common()
    output, backup, dump = (HERE / 'deployment0370.json', HERE / 'pre-0370-source',
                            HERE / 'pre-0370-ledger.dump')
    assert not output.exists() and not backup.exists() and not dump.exists()
    manifest_path = HERE / 'observer053-promotion-manifest.json'
    manifest = json.loads(manifest_path.read_text())
    entries = {item['path']: item for item in manifest['files']}
    assert set(entries) == PAYLOADS and len(manifest['files']) == 10
    for evidence in manifest['evidence']:
        assert common.sha(ROOT / evidence['path']) == evidence['sha256']
    checks = json.loads((ROOT / 'data/runtime/parallel-m0/observer-integration-053-checks/gate-4dfa4ee66420-commands.json').read_text())
    gate = json.loads((ROOT / 'data/runtime/parallel-m0/observer-integration-053-checks/gate-4dfa4ee66420.json').read_text())
    supplement = json.loads((HERE / 'probe-r5-strict-supplement.json').read_text())
    parity = json.loads((SOURCE / 'docs/ai-scientist/review-evidence/observer-integration-053-image-8fe98fcc6912.json').read_text())
    assert gate['exit_code'] == 0 and gate['source_unchanged'] and checks['overall_exit_code'] == 0
    assert all(step['exit_code'] == 0 for step in checks['commands'])
    assert supplement['passed'] and supplement['containers'] == 6 and supplement['phases'] == 15
    assert parity['all_passed'] and parity['source_unchanged'] and parity['image'] == manifest['image']
    assert (ROOT / 'harness/VERSION').read_text().strip() == '0.36.3'
    assert common.command(['git', 'rev-parse', 'HEAD'], cwd=ROOT).strip() == EXPECTED_HEAD
    for name, item in entries.items():
        assert not (ROOT / name).is_symlink() and not (SOURCE / name).is_symlink()
        assert common.sha(SOURCE / name) == item['proposed_sha256']
        assert (common.sha(ROOT / name) if (ROOT / name).is_file() else None) == item['current_main_sha256']
    for name, digest in parity['source_sha256'].items():
        if name not in PAYLOADS:
            assert common.sha(ROOT / name) == digest, 'unrelated runtime mismatch: ' + name
    record = {'schema': 'observer0370-deployment.v1', 'scope': common.verify_scope(),
              'manifest_sha256': common.sha(manifest_path), 'helper_sha256': common.sha(Path(__file__)),
              'common_sha256': COMMON_SHA256, 'main_head': EXPECTED_HEAD, 'steps': []}
    locks, old = [], {}
    stopped = copy_started = False
    before = None
    try:
        locks.append(common.lock('data/runtime/parallel-m0/cpu-check.lock'))
        locks.append(common.lock('data/runtime/director-dispatch.lock'))
        identity = common.units()
        before = snapshot(common)
        postgres, original_tunnel = common.container(), tunnel(common)
        record.update(before=before, before_units=identity, postgres=postgres, tunnel_before=original_tunnel)
        common.save(output, record)
        for name in common.UNITS:
            assert common.unit_record(name) == identity[name]
            stopped = True
            common.command(['systemctl', '--user', 'stop', name], timeout=25)
            assert snapshot(common) == before and common.container() == postgres
            assert tunnel(common) == original_tunnel
        record['steps'].append('exact_three_owned_cpu_services_stopped')
        backup.mkdir(mode=0o700)
        for name in sorted(PAYLOADS):
            target = ROOT / name
            old[name] = common.sha(target) if target.is_file() else None
            assert old[name] == entries[name]['current_main_sha256']
            if target.exists():
                (backup / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup / name)
        common.save(backup / 'manifest.json', old)
        with dump.open('xb') as stream:
            dump.chmod(0o600)
            result = subprocess.run(['docker', 'exec', postgres['id'], 'pg_dump', '-U',
                                     'swapp_lab_admin', '-d', 'swapp_lab', '-Fc'],
                                    stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.PIPE,
                                    check=False, timeout=60)
            assert result.returncode == 0 and dump.stat().st_size > 100000
        record['database_backup'] = {'path': str(dump), 'bytes': dump.stat().st_size,
                                     'sha256': common.sha(dump)}
        assert snapshot(common) == before
        copy_started = True
        for name, item in entries.items():
            assert common.sha(SOURCE / name) == item['proposed_sha256']
            (ROOT / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SOURCE / name, ROOT / name)
            assert common.sha(ROOT / name) == item['proposed_sha256']
        assert snapshot(common) == before and common.container() == postgres
        record['steps'].append('exact_ten_source_files_copied_no_database_write')
        record['after_units'] = common.restore()
        stopped = False
        assert snapshot(common) == before and common.container() == postgres
        assert tunnel(common) == original_tunnel
        record.update(status='deployed', version='0.37.0', after=snapshot(common),
                      tunnel_after=tunnel(common), migration_applied=False,
                      changed_files={name: item['proposed_sha256'] for name, item in entries.items()})
        common.save(output, record)
        print(json.dumps({'status': 'deployed', 'version': '0.37.0', 'files': 10,
                          'migration_applied': False, 'evidence': str(output)}))
        return 0
    except BaseException as error:
        record['failure_type'] = type(error).__name__
        restore_allowed = not copy_started
        if copy_started:
            # Stop only the same owned services before restoring old source, even after partial restart.
            try:
                stopped = True
                for name in common.UNITS:
                    common.unit_record(name)
                    common.command(['systemctl', '--user', 'stop', name], timeout=25)
                stopped = True
                for name, digest in old.items():
                    if digest is None:
                        (ROOT / name).unlink(missing_ok=True)
                    else:
                        assert common.sha(backup / name) == digest
                        shutil.copy2(backup / name, ROOT / name)
                        assert common.sha(ROOT / name) == digest
                assert set(old) == PAYLOADS
                for name, digest in old.items():
                    actual = common.sha(ROOT / name) if (ROOT / name).is_file() else None
                    assert actual == digest, 'incomplete source rollback: ' + name
                record['source_rolled_back'] = True
                record['rollback_full_byte_equality_verified'] = True
                restore_allowed = True
            except BaseException as rollback_error:
                record['rollback_failure_type'] = type(rollback_error).__name__
        if stopped and restore_allowed:
            try:
                record['after_restore_units'] = common.restore()
            except BaseException as restore_error:
                record['restore_failure_type'] = type(restore_error).__name__
        elif stopped:
            record['normal_restart_blocked_for_incomplete_source_rollback'] = True
            record['owned_services_require_root_inspection'] = list(common.UNITS)
        try:
            record['failure_snapshot'] = snapshot(common)
            record['database_unchanged'] = before is not None and record['failure_snapshot'] == before
            record['tunnel_after_failure'] = tunnel(common)
        except BaseException as inspection_error:
            record['failure_inspection_type'] = type(inspection_error).__name__
        common.save(output, record)
        print(json.dumps({'status': 'failed', 'failure_type': type(error).__name__,
                          'evidence': str(output)}))
        return 1
    finally:
        for descriptor in reversed(locks):
            os.close(descriptor)


if __name__ == '__main__':
    raise SystemExit(main())
