"""Disposable PostgreSQL migration smoke; never uses an existing Lab database."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
from psycopg import sql

ROOT = Path('/home/cachyos/ai-scientist')
SOURCE = ROOT / 'data/runtime/parallel-m0/holdout-030'
LABEL = 'swapp.review.holdout030'
ROLES = ('migrator', 'director', 'planner', 'scorer')


def command(args: list[str], *, timeout: int = 30, **kwargs):
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False, **kwargs)


def main() -> int:
    identifier = uuid4().hex
    private = ROOT / 'data/runtime/holdout-030-pg-smoke' / identifier
    private.mkdir(mode=0o700)
    snapshot = private / 'source'
    paths = ['alembic.ini', 'lab/__init__.py', 'lab/db/__init__.py', 'lab/db/schema.py']
    paths += [str(p.relative_to(SOURCE)) for p in sorted((SOURCE / 'lab/db/migrations').rglob('*'))
              if p.is_file() and '__pycache__' not in p.parts]
    captured = {p: (SOURCE / p).read_bytes() for p in paths}
    for p, data in captured.items():
        target = snapshot / p
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    if any((SOURCE / p).read_bytes() != data for p, data in captured.items()):
        raise RuntimeError('Migration source changed while snapshot was captured')
    frozen_driver = private / 'review_driver.py'
    frozen_driver.write_bytes(Path(__file__).read_bytes())
    record = {
        'schema': 'holdout-030-migration-smoke.v2',
        'created_at': datetime.now(UTC).isoformat(),
        'scope': 'Disposable PostgreSQL 16 migration compilation and role checks only; no holdout scoring, wrapper crash proof, candidate container, GPU or AOS execution.',
        'snapshot': str(snapshot.relative_to(ROOT)),
        'frozen_driver': str(frozen_driver.relative_to(ROOT)),
        'source_sha256': {p: hashlib.sha256(data).hexdigest() for p, data in captured.items()},
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'checks': [],
    }
    database = 'swapp_lab_m0_holdout_030_' + identifier[:12]
    name = 'swapp-holdout-030-pg-smoke-' + identifier[:12]
    admin_password = secrets.token_hex(32)
    passwords = {role: secrets.token_hex(32) for role in ROLES}
    env_file = private / 'postgres.env'
    env_file.touch(mode=0o600)
    env_file.write_text(f'POSTGRES_USER=review_admin\nPOSTGRES_PASSWORD={admin_password}\nPOSTGRES_DB={database}\n')
    dsns = private / 'dsns'
    dsns.mkdir(mode=0o700)
    container_id = None
    attempted_creation = False
    successful = False
    cleanup_ok = False
    try:
        image = command(['docker', 'image', 'inspect', 'postgres:16-bookworm', '--format', '{{.Id}}'])
        if image.returncode:
            raise RuntimeError('Pinned local PostgreSQL image unavailable')
        image_id = image.stdout.strip()
        record['image_id'] = image_id
        attempted_creation = True
        created = command([
            'docker', 'create', '--name', name, '--label', f'{LABEL}={identifier}',
            '--memory', '512m', '--memory-swap', '512m', '--cpus', '0.5', '--pids-limit', '64',
            '--shm-size', '64m', '--tmpfs', '/var/lib/postgresql/data:rw,size=268435456',
            '--publish', '127.0.0.1::5432', '--env-file', str(env_file),
            image_id, 'postgres', '-c', 'shared_buffers=32MB', '-c', 'max_connections=30',
        ])
        if created.returncode:
            raise RuntimeError('Disposable PostgreSQL container was not created')
        container_id = created.stdout.strip()
        started = command(['docker', 'start', container_id])
        if started.returncode:
            raise RuntimeError('Disposable PostgreSQL container did not start')
        inspected = command(['docker', 'inspect', '--format', '{{json .NetworkSettings.Ports}}', container_id])
        if inspected.returncode:
            raise RuntimeError('Disposable PostgreSQL port inspection failed')
        ports = json.loads(inspected.stdout)['5432/tcp']
        if len(ports) != 1 or ports[0]['HostIp'] != '127.0.0.1':
            raise RuntimeError('PostgreSQL port is not isolated to loopback')
        port = int(ports[0]['HostPort'])
        record.update(container_id=container_id, container_name=name, database=database, port=port)
        connection = None
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            try:
                connection = psycopg.connect(host='127.0.0.1', port=port, user='review_admin',
                    password=admin_password, dbname=database, autocommit=True, connect_timeout=1)
                break
            except psycopg.OperationalError:
                time.sleep(0.25)
        if connection is None:
            raise RuntimeError('PostgreSQL readiness deadline expired')
        with connection:
            for role in ROLES:
                name_sql = sql.Identifier('swapp_lab_' + role)
                connection.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT CONNECTION LIMIT 10 PASSWORD {}').format(name_sql, sql.Literal(passwords[role])))
                connection.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(database), name_sql))
                dsn_path = dsns / f'{role}.dsn'
                dsn_path.touch(mode=0o600)
                dsn_path.write_text(f'postgresql+psycopg://swapp_lab_{role}:{passwords[role]}@127.0.0.1:{port}/{database}\n')
            connection.execute(sql.SQL('GRANT CREATE ON DATABASE {} TO swapp_lab_migrator').format(sql.Identifier(database)))
            for schema in ('lab', 'scorer'):
                connection.execute(sql.SQL('CREATE SCHEMA {} AUTHORIZATION swapp_lab_migrator').format(sql.Identifier(schema)))
            connection.execute('GRANT USAGE ON SCHEMA lab TO swapp_lab_director, swapp_lab_scorer')
            connection.execute('GRANT USAGE ON SCHEMA scorer TO swapp_lab_planner')
        environment = dict(os.environ, PYTHONPATH=str(snapshot), LAB_MIGRATOR_DSN_FILE=str(dsns / 'migrator.dsn'))
        migrated = command([str(ROOT / '.venv/bin/python'), '-m', 'alembic', 'upgrade', 'head'],
                           timeout=90, cwd=snapshot, env=environment)
        output = migrated.stdout + migrated.stderr
        for value in [admin_password, *passwords.values()]:
            output = output.replace(value, '[REDACTED]')
        record['migration_exit_code'] = migrated.returncode
        record['migration_output'] = output[-40000:]
        if migrated.returncode:
            raise RuntimeError('Snapshot migration failed; see sanitized output')
        with psycopg.connect(host='127.0.0.1', port=port, user='review_admin', password=admin_password,
                            dbname=database, autocommit=True, connect_timeout=2) as connection:
            revision = connection.execute('SELECT version_num FROM lab.alembic_version').fetchone()[0]
            record['revision'] = revision
            if revision != '0020_holdout_recovery':
                raise RuntimeError('Unexpected migration head')
            functions = {
                'lab.read_holdout_recovery_target(uuid)': 'scorer',
                'lab.check_holdout_admission(uuid,uuid,integer,bigint,text,text,text,text)': 'scorer',
                'lab.recover_holdout_failure(uuid,uuid,integer,bigint,text,text,text,text)': 'scorer',
                'lab.recover_unclaimed_holdout_failure(uuid,uuid)': 'scorer',
                'lab.list_holdout_recovery_targets(uuid)': 'scorer',
                'lab.fence_missing_holdout_run_end(uuid,text,integer,text,text)': 'director',
                'lab.read_holdout_run_end_fence(uuid)': 'director',
            }
            for signature, owner in functions.items():
                for role in ('director', 'planner', 'scorer'):
                    allowed = connection.execute('SELECT has_function_privilege(%s,%s,%s)',
                        ('swapp_lab_' + role, signature, 'EXECUTE')).fetchone()[0]
                    passed = allowed == (role == owner)
                    record['checks'].append({'role': role, 'function': signature, 'allowed': allowed, 'passed': passed})
            for role in ('director', 'planner', 'scorer'):
                allowed = connection.execute('SELECT has_table_privilege(%s,%s,%s)',
                    ('swapp_lab_' + role, 'lab.holdout_run_end_fences', 'SELECT')).fetchone()[0]
                record['checks'].append({'role': role, 'table': 'lab.holdout_run_end_fences', 'direct_select': allowed, 'passed': not allowed})
            record['trigger_exists'] = bool(connection.execute("SELECT 1 FROM pg_trigger WHERE tgname='holdout_run_end_fence_guard' AND NOT tgisinternal").fetchone())
        absent = uuid4()
        probes = (
            ('read_holdout_recovery_target', 'scorer', (absent,), 'P0001'),
            ('check_holdout_admission', 'scorer', (absent, absent, 1, 1, 'missing', 'missing', 'missing', 'missing'), None),
            ('recover_holdout_failure', 'scorer', (absent, absent, 1, 1, 'missing', 'missing', 'missing', 'missing'), 'P0001'),
            ('recover_unclaimed_holdout_failure', 'scorer', (absent, absent), 'P0001'),
            ('list_holdout_recovery_targets', 'scorer', (absent,), 'P0001'),
            ('fence_missing_holdout_run_end', 'director', (absent, 'a' * 64, 1, 'holdout-budget-reconciled:' + str(absent), 'b' * 64), 'P0001'),
            ('read_holdout_run_end_fence', 'director', (absent,), None),
        )
        for role in ('director', 'planner', 'scorer'):
            with psycopg.connect(host='127.0.0.1', port=port, user='swapp_lab_' + role,
                                password=passwords[role], dbname=database, autocommit=True,
                                connect_timeout=2) as connection:
                authenticated = connection.execute('SELECT session_user').fetchone()[0]
                if authenticated != 'swapp_lab_' + role:
                    raise RuntimeError('Independent database role authentication differs')
                for function, owner, params, owner_state in probes:
                    observed_state = None
                    value = None
                    try:
                        statement = sql.SQL('SELECT lab.{}({})').format(
                            sql.Identifier(function), sql.SQL(',').join(sql.Placeholder() for _ in params))
                        value = connection.execute(statement, params).fetchone()[0]
                    except psycopg.Error as error:
                        observed_state = error.sqlstate
                    expected_state = owner_state if role == owner else '42501'
                    record['checks'].append({'authenticated_role': authenticated,
                        'function': function, 'scope': 'Absent resource; permission and safe failure path only',
                        'sqlstate': observed_state, 'expected_sqlstate': expected_state,
                        'passed': observed_state == expected_state and value in (None, False)})
                observed_state = None
                try:
                    connection.execute('SELECT * FROM lab.holdout_run_end_fences LIMIT 0')
                except psycopg.Error as error:
                    observed_state = error.sqlstate
                record['checks'].append({'authenticated_role': authenticated,
                    'table': 'lab.holdout_run_end_fences', 'sqlstate': observed_state,
                    'passed': observed_state == '42501'})
        successful = all(c['passed'] for c in record['checks']) and record['trigger_exists']
    except Exception as error:
        record['error_type'] = type(error).__name__
    finally:
        if container_id is None and attempted_creation:
            captured_identity = command(['docker', 'inspect', '--format', '{{.Id}} {{index .Config.Labels "' + LABEL + '"}}', name])
            values = captured_identity.stdout.strip().split()
            if captured_identity.returncode == 0 and len(values) == 2 and values[1] == identifier:
                container_id = values[0]
        if container_id is not None:
            check = command(['docker', 'inspect', '--format', '{{.Id}} {{index .Config.Labels "' + LABEL + '"}}', container_id])
            if check.returncode == 0 and check.stdout.strip() == f'{container_id} {identifier}':
                stopped = command(['docker', 'stop', '--time', '3', container_id], timeout=15)
                if stopped.returncode == 0:
                    removed = command(['docker', 'rm', container_id], timeout=15)
                    cleanup_ok = removed.returncode == 0
            record['owned_container_removed'] = cleanup_ok
        else:
            cleanup_ok = True
            record['owned_container_removed'] = None
        env_file.unlink(missing_ok=True)
        for path in dsns.glob('*.dsn'):
            path.unlink()
        dsns.rmdir()
        record['credentials_removed'] = True
        record['source_now_matches_snapshot'] = all((SOURCE / p).read_bytes() == data for p, data in captured.items())
        record['exit_code'] = 0 if successful and cleanup_ok else 1
        evidence = ROOT / 'docs/ai-scientist/review-evidence' / f'holdout-030-migration-smoke-{identifier[:12]}.json'
        evidence.write_text(json.dumps(record, indent=2) + '\n')
        print(json.dumps({'evidence': str(evidence.relative_to(ROOT)), 'exit_code': record['exit_code'],
                          'revision': record.get('revision'), 'migration_exit_code': record.get('migration_exit_code'),
                          'checks': len(record['checks']), 'owned_container_removed': record.get('owned_container_removed'),
                          'source_now_matches_snapshot': record['source_now_matches_snapshot']}), flush=True)
    return record['exit_code']


if __name__ == '__main__':
    raise SystemExit(main())
