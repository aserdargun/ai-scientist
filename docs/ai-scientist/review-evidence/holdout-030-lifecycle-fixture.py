#!/usr/bin/env python3
"""Provision and coordinate one disposable 0.30 Scorer interruption fixture.

This is an execution-capable review harness. It does nothing without the
explicit --execute-reviewed-fixture flag. It must itself run under a bounded
user scope; see the companion handoff before review/approval.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess  # nosec B404 -- fixed absolute tools and bounded argv vectors
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[3]
RUNTIME = ROOT / "data/runtime/holdout-030-lifecycle-fixture"
LABEL = "swapp.review.holdout030.lifecycle"
DB_PREFIX = "swapp_lab_m0_holdout_030_"
ROLES = ("migrator", "director", "planner", "scorer")
IMAGE_TAG = "postgres:16-bookworm"
MAX_OUTPUT = 48 * 1024
WORKER_SECONDS = 180
OUTER_SECONDS = 420
CLEANUP_RESERVE_SECONDS = 45
_DEADLINE: float | None = None

_FAILURE_CODES = {
    "local PostgreSQL 16 image is unavailable or unpinned": "postgres_image_unavailable",
    "disposable lifecycle PostgreSQL container creation failed": "postgres_create_failed",
    "PostgreSQL container ID is malformed": "postgres_container_identity_invalid",
    "disposable lifecycle PostgreSQL container did not start": "postgres_start_failed",
    "database container ownership inspection failed": "postgres_inspection_failed",
    "database container image, limits, or ownership differ from fixture": (
        "postgres_container_identity_mismatch"
    ),
    "database port is not bound exclusively to loopback": "postgres_port_binding_invalid",
    "isolated PostgreSQL readiness deadline expired": "postgres_readiness_timeout",
    "fixture migration cannot fit before cleanup reserve": "migration_deadline_unavailable",
    "isolated 0.30 schema migration failed": "migration_command_failed",
    "generated role DSN has an unexpected driver": "role_dsn_driver_invalid",
    "role DSN server identity is not the exact disposable container": (
        "role_server_identity_mismatch"
    ),
    "role DSNs do not resolve to one PostgreSQL server generation": (
        "role_server_generation_mismatch"
    ),
    "fixture pytest child exited before worker readiness": "fixture_child_exited_before_ready",
    "synthetic fixture did not reach the live candidate phase": "fixture_readiness_timeout",
    "snapshot source fingerprint could not be read": "source_fingerprint_failed",
    "private snapshot harness fingerprint differs from trusted source": (
        "snapshot_fingerprint_mismatch"
    ),
}


def _safe_failure_code(exc: BaseException) -> str:
    """Return a fixed diagnostic code without persisting exception text."""
    message = exc.args[0] if len(exc.args) == 1 and isinstance(exc.args[0], str) else ""
    if message in _FAILURE_CODES:
        return _FAILURE_CODES[message]
    if isinstance(exc, subprocess.TimeoutExpired):
        return "bounded_command_timeout"
    if isinstance(exc, TimeoutError):
        return "bounded_phase_timeout"
    return "unclassified_" + type(exc).__name__


def _record_pytest_output(
    record: dict[str, object],
    stdout: str,
    stderr: str,
    container: dict[str, object] | None,
    *,
    stdout_truncated: bool = False,
    stderr_truncated: bool = False,
) -> None:
    secrets_to_redact: list[str] = []
    if container is not None:
        admin_password = container.get("admin_password")
        role_passwords = container.get("role_passwords")
        if isinstance(admin_password, str):
            secrets_to_redact.append(admin_password)
        if isinstance(role_passwords, dict):
            secrets_to_redact.extend(
                value for value in role_passwords.values() if isinstance(value, str)
            )

    def sanitize(value: str) -> str:
        for secret in secrets_to_redact:
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(
            r"(postgres(?:ql)?(?:\+[^:/@\s]+)?://[^:/@\s]+:)[^@\s]+(@)",
            r"\1[REDACTED]\2",
            value,
        )
        return value

    safe_stdout = sanitize(stdout)
    safe_stderr = sanitize(stderr)
    record["fixture_pytest_stdout"] = safe_stdout[-MAX_OUTPUT:]
    record["fixture_pytest_stderr"] = safe_stderr[-MAX_OUTPUT:]
    record["fixture_pytest_stdout_truncated"] = stdout_truncated or len(safe_stdout) > MAX_OUTPUT
    record["fixture_pytest_stderr_truncated"] = stderr_truncated or len(safe_stderr) > MAX_OUTPUT


def _read_bounded_log(path: Path) -> tuple[str, bool]:
    if not path.exists() or path.is_symlink() or not path.is_file():
        return "", False
    size = path.stat().st_size
    with path.open("rb") as stream:
        if size <= MAX_OUTPUT:
            return stream.read(MAX_OUTPUT + 1).decode("utf-8", errors="replace"), False
        stream.seek(size - MAX_OUTPUT - 1)
        # The bounded seek can start inside a password or URI. Drop that whole
        # partial line before decoding or sanitizing the retained tail.
        first_line = stream.readline(MAX_OUTPUT + 1)
        if not first_line.endswith(b"\n"):
            return "", True
        available = min(MAX_OUTPUT, size - stream.tell())
        return stream.read(available).decode("utf-8", errors="replace"), True


def _command(argv: list[str], *, timeout: int = 30, **kwargs) -> subprocess.CompletedProcess[str]:
    if _DEADLINE is not None:
        cleanup = bool(kwargs.pop("cleanup_window", False))
        reserve = 0 if cleanup else CLEANUP_RESERVE_SECONDS
        remaining = int(_DEADLINE - time.monotonic() - reserve)
        if remaining < 1:
            raise TimeoutError("aggregate fixture deadline reached its cleanup reserve")
        timeout = min(timeout, remaining)
    result = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        **kwargs,
    )
    if len(result.stdout) > MAX_OUTPUT or len(result.stderr) > MAX_OUTPUT:
        raise RuntimeError("bounded review command returned oversized output")
    return result


def _snapshot(source: Path, target: Path) -> dict[str, str]:
    include_files = (
        "alembic.ini",
        "Dockerfile.sandbox",
        "docker/sandbox/requirements.txt",
        "pyproject.toml",
        "uv.lock",
        "ops/sandbox-image.lock",
    )
    selected = [source / item for item in include_files]
    for directory in ("harness", "lab", "tests", "vendor"):
        selected.extend(
            path
            for path in sorted((source / directory).rglob("*"))
            if path.is_file()
            and not path.is_symlink()
            and "__pycache__" not in path.parts
            and path.suffix != ".pyc"
        )
    selected.extend(
        source / "docs/ai-scientist/review-evidence" / item
        for item in (
            "holdout-030-lifecycle-review.py",
            "holdout-030-lifecycle-review.md",
            "holdout-030-lifecycle-fixture.py",
        )
    )
    hashes: dict[str, str] = {}
    for original in selected:
        relative = original.relative_to(source)
        data = original.read_bytes()
        destination = target / relative
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.write_bytes(data)
        destination.chmod(0o600 if relative.suffix in {".py", ".md", ".toml", ".ini"} else 0o644)
        hashes[relative.as_posix()] = hashlib.sha256(data).hexdigest()
    python = source / ".venv/bin/python"
    if not python.is_file():
        raise RuntimeError("reviewed source virtualenv is unavailable")
    (target / ".venv").symlink_to(source / ".venv", target_is_directory=True)
    if (target / ".venv/bin/python").resolve(strict=True) != python.resolve(strict=True):
        raise RuntimeError("snapshot virtualenv does not resolve to the reviewed runtime")
    for path, digest in hashes.items():
        if hashlib.sha256((target / path).read_bytes()).hexdigest() != digest:
            raise RuntimeError("private source snapshot failed byte parity")
    return hashes


def _compute_harness_fingerprint(root: Path) -> dict[str, object]:
    code = (
        "import json; from pathlib import Path; "
        "from harness.fingerprint import compute_harness_hash; "
        "value = compute_harness_hash(Path.cwd()); "
        "print(json.dumps({'sha256': value.sha256, 'file_count': value.file_count}))"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root)
    result = _command(
        [str(ROOT / ".venv/bin/python"), "-c", code],
        cwd=root,
        env=env,
        timeout=20,
    )
    if result.returncode:
        raise RuntimeError("trusted harness fingerprint computation failed")
    try:
        fingerprint = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("trusted harness fingerprint output is malformed") from exc
    if (
        not isinstance(fingerprint, dict)
        or set(fingerprint) != {"sha256", "file_count"}
        or not isinstance(fingerprint.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", fingerprint["sha256"]) is None
        or type(fingerprint.get("file_count")) is not int
        or fingerprint["file_count"] <= 0
    ):
        raise RuntimeError("trusted harness fingerprint output is malformed")
    return fingerprint


def _plugin_text(directory: Path) -> str:
    plugin_path = directory / "fixture_plugin.py"
    source = r'''from __future__ import annotations
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
import time
from uuid import uuid4
import pytest

READY = Path(os.environ["HOLDOUT_FIXTURE_READY"])
RELEASE = Path(os.environ["HOLDOUT_FIXTURE_RELEASE"])
RESERVED = Path(os.environ["HOLDOUT_FIXTURE_RESERVATION"])
SETUP = Path(os.environ["HOLDOUT_FIXTURE_SETUP"])
ROOT = Path.cwd()
TOKEN = "r" * 32
OWNER = "fixture:holdout-abandoned-ordinal"
SLOW_SOURCE = b"""from time import sleep
import numpy as np
from harness.contracts import AlarmPolicy
class Pipeline:
    def fit(self, train, ctx):
        if len(train) >= 64:
            sleep(45)
        self.center = float(train.iloc[:, 0].mean())
    def score(self, data):
        values = data.iloc[:, 0].to_numpy(dtype=float)
        return np.abs(values - self.center) + np.linspace(0.001, 0.002, len(values))
    def alarm_policy(self, train_scores):
        return AlarmPolicy(float(np.max(train_scores)), float(np.min(train_scores)), 1)
def build_candidate():
    return Pipeline()
"""

class FixturePaused(Exception):
    pass

def pytest_collection_modifyitems(session, config, items):
    from lab.director.holdout import reserve_holdout_check
    from lab.scorer.holdout_supervisor import run_holdout_process
    from lab.scorer.holdout import (
        _process_has_holdout_identity, _read_holdout_recovery_target,
        _worker_identity_from_target,
    )
    from lab.scorer import worker as worker_module
    from sqlalchemy import create_engine
    from lab.sandbox.docker_runner import DEFAULT_SANDBOX_IMAGE
    from lab.sandbox.docker_runner import DEFAULT_ADMISSION_LOCK

    def stop_owned_run(engine, run_id):
        from fastapi.testclient import TestClient
        from lab.api.app import create_app
        from lab.api.registry import ApiPrincipal
        app = create_app(director_engine=engine, principals=(
            ApiPrincipal(token=TOKEN, origin="local", owner_id=OWNER),))
        with TestClient(app, raise_server_exceptions=False) as client:
            return client.post(f"/v1/runs/{run_id}/stop",
                headers={"Authorization": f"Bearer {TOKEN}"})

    for item in items:
        if item.name != "test_run_end_accepts_resumed_abandoned_ordinal_without_experiment_row":
            continue
        module = item.module
        module.baseline_candidate_source = lambda _name: SLOW_SOURCE

        setup = {
            "run_id": None,
            "score_job_ids": [],
            "starting_job_id": None,
            "worker_results": [],
        }

        def persist_setup():
            temporary = SETUP.with_suffix(".tmp")
            temporary.write_text(json.dumps(setup), encoding="ascii")
            temporary.chmod(0o600)
            os.replace(temporary, SETUP)

        original_enqueue = module.enqueue_score_job

        def tracked_enqueue(*args, **kwargs):
            job_id = original_enqueue(*args, **kwargs)
            run_id = kwargs.get("run_id")
            if run_id is None and args:
                run_id = args[0]
            setup["run_id"] = str(run_id)
            setup["score_job_ids"].append(str(job_id))
            persist_setup()
            return job_id

        original_scorer_process = module.run_scorer_process

        def tracked_scorer_process(job_id, *, remaining_seconds=600, **kwargs):
            worker_record = {"job_id": str(job_id), "state": "exception", "exit_code": None}
            setup["starting_job_id"] = str(job_id)
            persist_setup()
            try:
                deadline = float(os.environ["HOLDOUT_FIXTURE_DEADLINE_MONOTONIC"])
                available = int(deadline - time.monotonic() - 45)
                if available < 1:
                    worker_record["state"] = "not_started"
                    raise TimeoutError("aggregate fixture deadline reached before Scorer start")
                bounded = min(90, remaining_seconds, available)
                result = original_scorer_process(
                    job_id, remaining_seconds=bounded, **kwargs
                )
                exit_code = getattr(result, "exit_code", None)
                if type(exit_code) is int and -255 <= exit_code <= 255:
                    worker_record["exit_code"] = exit_code
                result_document = getattr(result, "result", None)
                state = result_document.get("state") if isinstance(result_document, dict) else None
                worker_record["state"] = (
                    state
                    if state in {
                        "capacity_busy",
                        "claim_lost",
                        "completed",
                        "failed",
                        "idle",
                        "retrying",
                        "terminal",
                    }
                    else "unknown"
                )
                return result
            finally:
                setup["worker_results"].append(worker_record)
                setup["starting_job_id"] = None
                persist_setup()

        module.enqueue_score_job = tracked_enqueue
        module.run_scorer_process = tracked_scorer_process

        def holdout_start(engine, *, run_id, candidate_experiment_id, trigger_kind,
                          trigger_index, remaining_seconds=600):
            key = hashlib.sha256(candidate_experiment_id.encode()).hexdigest()
            proposed = uuid4()
            receipt = reserve_holdout_check(
                engine, run_id=run_id, candidate_experiment_id=candidate_experiment_id,
                trigger_kind=trigger_kind, trigger_index=trigger_index,
                request_key=f"{trigger_kind}:{trigger_index}:{key}", reservation_id=proposed)
            reservation_id = receipt.reservation_id
            RESERVED.write_text(json.dumps({
                "run_id": str(run_id), "reservation_id": str(reservation_id),
                "state": receipt.state, "candidate_experiment_id": candidate_experiment_id,
            }), encoding="ascii")
            result = {}

            def run_worker():
                try:
                    deadline = float(os.environ["HOLDOUT_FIXTURE_DEADLINE_MONOTONIC"])
                    available = int(deadline - time.monotonic() - 45)
                    if available < 1:
                        raise TimeoutError(
                            "aggregate fixture deadline reached before holdout start"
                        )
                    result["value"] = run_holdout_process(
                        reservation_id, remaining_seconds=min(180, available))
                except Exception as exc:
                    result["error_type"] = type(exc).__name__

            thread = threading.Thread(target=run_worker, daemon=True)
            thread.start()
            scorer = create_engine(worker_module._secret(worker_module.DEFAULT_DSN_FILE),
                pool_size=1, max_overflow=0, pool_timeout=5)
            try:
                deadline = time.monotonic() + 100
                while time.monotonic() < deadline:
                    if (
                        RELEASE.exists()
                        and RELEASE.read_text(encoding="ascii").strip() == "cleanup"
                    ):
                        stop_owned_run(engine, run_id)
                        thread.join(timeout=20)
                        raise FixturePaused("exact reserved fixture stopped during cleanup")
                    target = _read_holdout_recovery_target(scorer, reservation_id)
                    if target["state"] == "running":
                        identity = _worker_identity_from_target(target, reservation_id)
                        if _process_has_holdout_identity(identity):
                            marker_path = DEFAULT_ADMISSION_LOCK.with_suffix(".intent")
                            if marker_path.is_file() and not marker_path.is_symlink():
                                driver_path = (
                                    ROOT / "docs/ai-scientist/review-evidence/"
                                    "holdout-030-lifecycle-review.py"
                                )
                                spec = importlib.util.spec_from_file_location(
                                    "lifecycle_review_driver", driver_path)
                                driver = importlib.util.module_from_spec(spec)
                                sys.modules[spec.name] = driver
                                spec.loader.exec_module(driver)
                                try:
                                    marker = driver._read_marker(
                                        identity, reservation_id, DEFAULT_SANDBOX_IMAGE)
                                    container = driver._verify_live_container(
                                        marker, DEFAULT_SANDBOX_IMAGE)
                                except Exception:
                                    marker = None
                                    container = None
                                if marker is not None and container is not None:
                                    READY.write_text(json.dumps({
                                        "run_id": str(run_id),
                                        "reservation_id": str(reservation_id),
                                        "worker_pid": identity["worker_pid"],
                                        "worker_unit": identity["worker_unit"],
                                        "worker_invocation_id": identity["worker_invocation_id"],
                                        "worker_cgroup": identity["worker_cgroup"],
                                        "marker_path": str(marker_path),
                                        "container_id": container["container_id"],
                                        "phase": "active-fit-candidate",
                                    }), encoding="ascii")
                                    break
                    time.sleep(0.1)
                else:
                    raise RuntimeError("fixture worker did not reach a marked live generation")
                release_deadline = time.monotonic() + 180
                while not RELEASE.exists() and time.monotonic() < release_deadline:
                    time.sleep(0.1)
                if not RELEASE.exists():
                    raise TimeoutError("fixture controller did not release the paused test")
                if RELEASE.read_text(encoding="ascii").strip() == "cleanup":
                    stop_owned_run(engine, run_id)
                thread.join(timeout=20)
                if thread.is_alive():
                    raise RuntimeError("holdout worker supervisor thread did not return")
                raise FixturePaused("fixture captured the production run/reservation")
            finally:
                scorer.dispose()

        module.evaluate_holdout_check = holdout_start

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    outcome = yield
    if outcome.excinfo is not None and isinstance(outcome.excinfo[1], FixturePaused):
        outcome.force_result(None)
'''
    plugin_path.write_text(source, encoding="utf-8")
    plugin_path.chmod(0o600)
    return str(plugin_path)


def _engine_start(
    private: Path, snapshot: Path, ident: str, ownership: dict[str, object], deadline: float
) -> dict[str, object]:
    ownership["stage"] = "postgres_image_check"
    database = DB_PREFIX + ident[:12]
    container_name = "swapp-holdout030-lifecycle-pg-" + ident[:12]
    admin_password = secrets.token_urlsafe(32)
    passwords = {role: secrets.token_urlsafe(32) for role in ROLES}
    env_file = private / "postgres.env"
    env_file.write_text(
        f"POSTGRES_USER=review_admin\nPOSTGRES_PASSWORD={admin_password}\nPOSTGRES_DB={database}\n",
        encoding="ascii",
    )
    env_file.chmod(0o600)
    image = _command(
        ["docker", "image", "inspect", IMAGE_TAG, "--format", "{{.Id}}"],
        timeout=max(1, min(15, int(deadline - time.monotonic() - CLEANUP_RESERVE_SECONDS))),
    )
    if image.returncode or not re.fullmatch(r"sha256:[0-9a-f]{64}\n?", image.stdout):
        raise RuntimeError("local PostgreSQL 16 image is unavailable or unpinned")
    image_id = image.stdout.strip()
    ownership.update(
        container_name=container_name, container_label_value=ident, creation_attempted=True
    )
    ownership["stage"] = "postgres_container_create"
    create = _command(
        [
            "docker",
            "create",
            "--name",
            container_name,
            "--label",
            f"{LABEL}={ident}",
            "--memory",
            "512m",
            "--memory-swap",
            "512m",
            "--cpus",
            "0.5",
            "--pids-limit",
            "64",
            "--shm-size",
            "64m",
            "--tmpfs",
            "/var/lib/postgresql/data:rw,size=268435456",
            "--publish",
            "127.0.0.1::5432",
            "--env-file",
            str(env_file),
            image_id,
            "postgres",
            "-c",
            "shared_buffers=32MB",
            "-c",
            "max_connections=30",
            "-c",
            "statement_timeout=20000",
            "-c",
            "lock_timeout=5000",
        ],
        timeout=max(1, min(30, int(deadline - time.monotonic() - CLEANUP_RESERVE_SECONDS))),
    )
    if create.returncode:
        raise RuntimeError("disposable lifecycle PostgreSQL container creation failed")
    container_id = create.stdout.strip()
    if re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
        raise RuntimeError("PostgreSQL container ID is malformed")
    ownership.update(
        container_id=container_id, container_name=container_name, container_label_value=ident
    )
    ownership["stage"] = "postgres_container_start"
    if _command(["docker", "start", container_id]).returncode:
        raise RuntimeError("disposable lifecycle PostgreSQL container did not start")
    ownership["stage"] = "postgres_container_inspection"
    inspection = _command(
        [
            "docker",
            "inspect",
            "--format",
            "{{.Id}}|{{.Image}}|{{.HostConfig.Memory}}|{{.HostConfig.MemorySwap}}|"
            "{{.HostConfig.NanoCpus}}|{{.HostConfig.PidsLimit}}|{{.State.Running}}|"
            '{{index .Config.Labels "' + LABEL + '"}}|{{json .NetworkSettings.Ports}}|'
            "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
            container_id,
        ]
    )
    values = inspection.stdout.strip().split("|", 9)
    if inspection.returncode or len(values) != 10:
        raise RuntimeError("database container ownership inspection failed")
    if (
        values[0] != container_id
        or values[1] != image_id
        or values[2] != str(512 * 1024**2)
        or values[3] != str(512 * 1024**2)
        or values[4] != "500000000"
        or values[5] != "64"
        or values[6].lower() != "true"
        or values[7] != ident
    ):
        raise RuntimeError("database container image, limits, or ownership differ from fixture")
    ports = json.loads(values[8]).get("5432/tcp")
    container_ips = {item for item in values[9].split() if item}
    if not isinstance(ports, list) or len(ports) != 1 or ports[0].get("HostIp") != "127.0.0.1":
        raise RuntimeError("database port is not bound exclusively to loopback")
    port = int(ports[0]["HostPort"])
    conn = None
    ownership["stage"] = "postgres_readiness"
    until = min(deadline - CLEANUP_RESERVE_SECONDS, time.monotonic() + 45)
    while time.monotonic() < until:
        try:
            conn = psycopg.connect(
                host="127.0.0.1",
                port=port,
                user="review_admin",
                password=admin_password,
                dbname=database,
                autocommit=True,
                connect_timeout=1,
            )
            break
        except psycopg.OperationalError:
            time.sleep(0.25)
    if conn is None:
        raise RuntimeError("isolated PostgreSQL readiness deadline expired")
    dsn_dir = private / "dsns"
    ownership["stage"] = "postgres_role_setup"
    dsn_dir.mkdir(mode=0o700)
    with conn:
        for role in ROLES:
            role_name = "swapp_lab_" + role
            conn.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
                    "CONNECTION LIMIT 10 PASSWORD {}"
                ).format(sql.Identifier(role_name), sql.Literal(passwords[role]))
            )
            conn.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(database), sql.Identifier(role_name)
                )
            )
            path = dsn_dir / f"{role}.dsn"
            path.write_text(
                f"postgresql+psycopg://{role_name}:{passwords[role]}@127.0.0.1:{port}/{database}\n",
                encoding="ascii",
            )
            path.chmod(0o600)
        conn.execute(
            sql.SQL("GRANT CREATE ON DATABASE {} TO swapp_lab_migrator").format(
                sql.Identifier(database)
            )
        )
        for schema in ("lab", "scorer"):
            conn.execute(
                sql.SQL("CREATE SCHEMA {} AUTHORIZATION swapp_lab_migrator").format(
                    sql.Identifier(schema)
                )
            )
        conn.execute("GRANT USAGE ON SCHEMA lab TO swapp_lab_director, swapp_lab_scorer")
        conn.execute("GRANT USAGE ON SCHEMA scorer TO swapp_lab_planner")
    conn.close()
    env = dict(os.environ)
    env.update(PYTHONPATH=str(snapshot), LAB_MIGRATOR_DSN_FILE=str(dsn_dir / "migrator.dsn"))
    migration_timeout = int(deadline - time.monotonic() - CLEANUP_RESERVE_SECONDS)
    if migration_timeout < 1:
        raise TimeoutError("fixture migration cannot fit before cleanup reserve")
    ownership["stage"] = "schema_migration"
    migrated = _command(
        [str(ROOT / ".venv/bin/python"), "-m", "alembic", "upgrade", "head"],
        cwd=snapshot,
        env=env,
        timeout=min(90, migration_timeout),
    )
    ownership["migration_exit_code"] = migrated.returncode
    if migrated.returncode:
        raise RuntimeError("isolated 0.30 schema migration failed")
    with psycopg.connect(
        host="127.0.0.1",
        port=port,
        user="review_admin",
        password=admin_password,
        dbname=database,
        autocommit=True,
    ) as check:
        head = check.execute("SELECT version_num FROM lab.alembic_version").fetchone()[0]
    server_identities = set()
    ownership["stage"] = "role_identity_validation"
    for role in ROLES:
        uri = (dsn_dir / f"{role}.dsn").read_text(encoding="ascii").strip()
        if not uri.startswith("postgresql+psycopg://"):
            raise RuntimeError("generated role DSN has an unexpected driver")
        with psycopg.connect(
            uri.replace("postgresql+psycopg://", "postgresql://", 1), connect_timeout=3
        ) as role_connection:
            server_identity = role_connection.execute(
                "SELECT current_database(),session_user,host(inet_server_addr()),"
                "inet_server_port(),pg_postmaster_start_time()::text"
            ).fetchone()
        if (
            server_identity[0] != database
            or server_identity[1] != "swapp_lab_" + role
            or server_identity[2] not in container_ips
            or server_identity[3] != 5432
        ):
            raise RuntimeError("role DSN server identity is not the exact disposable container")
        # The authenticated SQL role intentionally differs between engines;
        # compare only the shared PostgreSQL instance generation tuple.
        server_identities.add(
            (
                server_identity[0],
                server_identity[2],
                server_identity[3],
                server_identity[4],
            )
        )
    if len(server_identities) != 1:
        raise RuntimeError("role DSNs do not resolve to one PostgreSQL server generation")
    ownership.pop("stage", None)
    return {
        "container_name": container_name,
        "container_id": container_id,
        "container_label_value": ident,
        "image_id": image_id,
        "database": database,
        "port": port,
        "dsn_dir": str(dsn_dir),
        "admin_password": admin_password,
        "role_passwords": passwords,
        "migration_head": head,
        "migration_exit": migrated.returncode,
        "endpoint": {
            "host": "127.0.0.1",
            "published_port": port,
            "container_port": 5432,
            "container_ip": sorted(container_ips),
        },
    }


def _owned_database_for_cleanup(
    container: dict[str, object] | None, ownership: dict[str, object]
) -> dict[str, object]:
    if container is not None:
        return container
    if ownership.get("creation_attempted") is True:
        return ownership
    return {}


def _wait_event(path: Path, deadline: float, child: subprocess.Popen[str]) -> dict[str, object]:
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError("fixture pytest child exited before worker readiness")
        if path.is_file() and not path.is_symlink():
            info = path.stat()
            if info.st_size > 8192 or info.st_uid != os.getuid():
                raise RuntimeError("fixture readiness receipt is oversized or unowned")
            value = json.loads(path.read_text(encoding="ascii"))
            if isinstance(value, dict):
                return value
            raise RuntimeError("fixture readiness receipt is malformed")
        time.sleep(0.1)
    raise TimeoutError("synthetic fixture did not reach the live candidate phase")


def _cleanup_reserved_run(
    snapshot: Path,
    private: Path,
    dsn_dir: Path,
    run_id: str,
    reservation_id: str | None,
) -> int:
    cleanup_script = private / "cleanup-owned-run.py"
    cleanup_script.write_text(
        r"""from uuid import UUID
from sqlalchemy import create_engine, text
from fastapi.testclient import TestClient
from lab.api.app import create_app
from lab.api.registry import ApiPrincipal
from lab.scorer.holdout import (
    _process_has_holdout_identity, _read_holdout_recovery_target,
    _worker_identity_from_target,
)
from uuid import UUID
from lab.scorer.supervisor import _systemctl_show, _cgroup_is_absent_or_empty, _expected_unit_cgroup
from pathlib import Path
import json, os, re
from lab.sandbox.docker_runner import DEFAULT_ADMISSION_LOCK
run_id = UUID(os.environ["FIXTURE_RUN_ID"])
raw_reservation_id = os.environ.get("FIXTURE_RESERVATION_ID")
reservation_id = UUID(raw_reservation_id) if raw_reservation_id else None
root = Path(os.environ["LAB_HOLDOUT_TEST_DSN_DIR"])
director = create_engine((root / "director.dsn").read_text().strip(), hide_parameters=True)
scorer = create_engine((root / "scorer.dsn").read_text().strip(), hide_parameters=True)
try:
    reservation_ids = []
    with director.connect() as connection:
        row = connection.execute(
            text("SELECT owner_id,origin FROM lab.runs WHERE run_id=:id"), {"id":run_id}
        ).first()
    if row is None and reservation_id is not None:
        raise RuntimeError("recorded reservation has no fixture run row")
    if row is not None:
        if row != ("fixture:holdout-abandoned-ordinal", "local"):
            raise RuntimeError("fixture run ownership mismatch")
        with scorer.connect() as connection:
            open_targets = connection.execute(
                text("SELECT lab.list_holdout_recovery_targets(:run_id)"), {"run_id": run_id}
            ).scalar_one()
        if not isinstance(open_targets, list) or len(open_targets) > 128:
            raise RuntimeError("fixture recovery target list is invalid")
        for value in open_targets:
            if not isinstance(value, dict) or set(value) != {"reservation_id", "state"}:
                raise RuntimeError("fixture recovery target identity is invalid")
            if value["state"] not in {"reserved", "running"}:
                raise RuntimeError("fixture returned a non-open recovery target")
            reservation_ids.append(UUID(str(value["reservation_id"])))
        if reservation_id is not None and reservation_id not in reservation_ids:
            reservation_ids.append(reservation_id)
    token = "r" * 32
    if row is not None:
        app = create_app(director_engine=director, principals=(ApiPrincipal(
            token=token, origin="local", owner_id="fixture:holdout-abandoned-ordinal"),))
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(f"/v1/runs/{run_id}/stop",
                headers={"Authorization":f"Bearer {token}"})
        if response.status_code != 200:
            raise RuntimeError("production stop route did not accept the exact fixture run")
    for current_reservation_id in reservation_ids:
        target = _read_holdout_recovery_target(scorer, current_reservation_id)
        if target["run_id"] != run_id or target["state"] in {"reserved", "running"}:
            raise RuntimeError("fixture reservation remains open after run stop")
        if target.get("worker_pid") is not None:
            identity = _worker_identity_from_target(target, current_reservation_id)
            if _process_has_holdout_identity(identity):
                raise RuntimeError("fixture worker process remains live after recovery")
            unit = identity["worker_unit"]
            group = identity["worker_cgroup"]
            properties = _systemctl_show(unit)
            if properties.get("LoadState") == "not-found":
                if not _cgroup_is_absent_or_empty(group):
                    raise RuntimeError("collected fixture cgroup is not empty")
            elif (properties.get("InvocationID","").lower() != identity["worker_invocation_id"]
                  or properties.get("MainPID") != "0" or not _cgroup_is_absent_or_empty(group)):
                raise RuntimeError("fixture worker generation did not drain")
    marker = DEFAULT_ADMISSION_LOCK.with_suffix(".intent")
    if marker.exists() or marker.is_symlink():
        raise RuntimeError("fixture sandbox marker remains after exact recovery")
finally:
    director.dispose()
    scorer.dispose()
""",
        encoding="utf-8",
    )
    cleanup_script.chmod(0o600)
    env = dict(os.environ)
    env.update(
        PYTHONPATH=str(snapshot),
        LAB_HOLDOUT_TEST_DSN_DIR=str(dsn_dir),
        FIXTURE_RUN_ID=run_id,
        XDG_RUNTIME_DIR=os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
    )
    if reservation_id is not None:
        env["FIXTURE_RESERVATION_ID"] = reservation_id
    result = _command(
        [str(ROOT / ".venv/bin/python"), str(cleanup_script)],
        cwd=snapshot,
        env=env,
        timeout=35,
        cleanup_window=True,
    )
    return result.returncode


def _cleanup_setup_workers(
    snapshot: Path, private: Path, dsn_dir: Path, setup_path: Path
) -> int:
    """Verify and drain only the setup job generations recorded by this fixture."""
    if not setup_path.is_file() or setup_path.is_symlink():
        return 0
    cleanup_script = private / "cleanup-setup-workers.py"
    cleanup_script.write_text(
        r'''from pathlib import Path
from uuid import UUID
import json, os, re
from sqlalchemy import create_engine, text
from lab.scorer.supervisor import (
    _cgroup_is_absent_or_empty, _cgroup_is_empty, _expected_unit_cgroup,
    _owned_unit_cgroup, _systemctl_show, stop_owned_scorer_unit,
)
root = Path(os.environ["LAB_HOLDOUT_TEST_DSN_DIR"])
snapshot = Path(os.environ["FIXTURE_SNAPSHOT"]).resolve(strict=True)
setup_path = Path(os.environ["FIXTURE_SETUP_RECEIPT"])
setup = json.loads(setup_path.read_text(encoding="ascii"))
run_id = UUID(setup["run_id"])
job_ids = [UUID(value) for value in setup["score_job_ids"]]
engine = create_engine((root / "scorer.dsn").read_text().strip(), hide_parameters=True)
expected_python = (snapshot / ".venv/bin/python").resolve(strict=True)

def proc_identity(pid, job_id, control_group):
    process = Path("/proc") / str(pid)
    if not process.is_dir():
        raise RuntimeError("owned Scorer PID disappeared before identity verification")
    boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    stat_text = (process / "stat").read_text(encoding="ascii")
    start_ticks = int(stat_text.rsplit(")", 1)[1].split()[19])
    cwd = Path(os.readlink(process / "cwd")).resolve(strict=True)
    executable = Path(os.readlink(process / "exe")).resolve(strict=True)
    argv = (process / "cmdline").read_bytes().split(b"\0")
    argv = [value.decode("utf-8", errors="strict") for value in argv if value]
    groups = [line.split(":", 2)[2] for line in (process / "cgroup").read_text().splitlines()
              if line.startswith("0::")]
    if (not boot or start_ticks <= 0 or cwd != snapshot or executable != expected_python
            or groups != [control_group]
            or ["-m", "lab.scorer.worker", "--job-id", str(job_id)] != argv[-4:]):
        raise RuntimeError("Scorer worker PID is not the exact snapshot job process")
    return {"boot_id": boot, "start_ticks": start_ticks}

try:
    with engine.connect() as connection:
        owner = connection.execute(text(
            "SELECT owner_id,origin FROM lab.runs WHERE run_id=:run_id"),
            {"run_id": run_id},
        ).first()
        if owner is not None and owner != ("fixture:holdout-abandoned-ordinal", "local"):
            raise RuntimeError("setup Scorer run ownership mismatch")
        for job_id in job_ids:
            row = connection.execute(text(
                "SELECT run_id,state,claim_unit,claim_invocation_id FROM scorer.score_jobs "
                "WHERE job_id=:job_id"), {"job_id": job_id},
            ).first()
            if row is not None and row[0] != run_id:
                raise RuntimeError("setup score job does not belong to the exact fixture run")
            unit = f"swapp-ai-scientist-scorer-{job_id.hex}.service"
            properties = _systemctl_show(unit)
            if properties.get("LoadState") == "not-found":
                if (
                    properties.get("ActiveState") != "inactive"
                    or properties.get("MainPID") != "0"
                    or properties.get("InvocationID") != ""
                    or properties.get("ControlGroup") != ""
                    or not _cgroup_is_absent_or_empty(_expected_unit_cgroup(unit))
                ):
                    raise RuntimeError("absent setup Scorer unit has residual process state")
                continue
            if properties.get("LoadState") != "loaded":
                raise RuntimeError("setup Scorer unit has unknown systemd load state")
            invocation = properties.get("InvocationID", "")
            if re.fullmatch(r"[0-9a-f]{32}", invocation) is None:
                raise RuntimeError("setup Scorer invocation identity is unavailable")
            group = _owned_unit_cgroup(unit, properties)
            if row is not None and row[1] == "running" and (row[2] != unit or row[3] != invocation):
                raise RuntimeError("database claim does not match the setup Scorer generation")
            pid = properties.get("MainPID", "0")
            if properties.get("ActiveState") == "active":
                if not pid.isdecimal() or int(pid) <= 0:
                    raise RuntimeError("active setup Scorer service has no exact main PID")
                proc_identity(int(pid), job_id, group)
                if not stop_owned_scorer_unit(job_id, invocation_id=invocation, timeout_seconds=10):
                    raise RuntimeError("setup Scorer unit did not drain")
            elif (
                properties.get("ActiveState") != "inactive"
                or pid != "0"
                or not _cgroup_is_empty(group)
            ):
                raise RuntimeError("setup Scorer unit state is unresolved")
            final = _systemctl_show(unit)
            if final.get("LoadState") == "not-found":
                if not _cgroup_is_absent_or_empty(group):
                    raise RuntimeError("collected setup Scorer cgroup is not empty")
            elif (final.get("ActiveState") != "inactive" or final.get("MainPID") != "0"
                  or final.get("InvocationID") != invocation or not _cgroup_is_empty(group)):
                raise RuntimeError("setup Scorer generation remains active after drain")
finally:
    engine.dispose()
''',
        encoding="utf-8",
    )
    cleanup_script.chmod(0o600)
    env = dict(os.environ)
    env.update(
        PYTHONPATH=str(snapshot),
        LAB_HOLDOUT_TEST_DSN_DIR=str(dsn_dir),
        FIXTURE_SETUP_RECEIPT=str(setup_path),
        FIXTURE_SNAPSHOT=str(snapshot),
        XDG_RUNTIME_DIR=os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
    )
    result = _command(
        [str(ROOT / ".venv/bin/python"), str(cleanup_script)],
        cwd=snapshot,
        env=env,
        timeout=35,
        cleanup_window=True,
    )
    return result.returncode


def _owned_container_cleanup(container: dict[str, object]) -> bool:
    label = str(container["container_label_value"])
    ident_value = container.get("container_id")

    def exact_absence(*, container_id: str | None = None, name: str | None = None) -> bool:
        if container_id is not None:
            query = _command(
                [
                    "docker",
                    "ps",
                    "--all",
                    "--no-trunc",
                    "--filter",
                    f"id={container_id}",
                    "--format",
                    "{{.ID}}",
                ],
                cleanup_window=True,
            )
        elif name is not None:
            query = _command(
                [
                    "docker",
                    "ps",
                    "--all",
                    "--filter",
                    f"name=^/{name}$",
                    "--format",
                    "{{.ID}}",
                ],
                cleanup_window=True,
            )
        else:
            return False
        if query.returncode != 0:
            return False
        found = [line.strip() for line in query.stdout.splitlines() if line.strip()]
        return not found

    if not isinstance(ident_value, str):
        name = container.get("container_name")
        if not isinstance(name, str):
            return False
        found = _command(
            [
                "docker",
                "inspect",
                "--format",
                '{{.Id}} {{index .Config.Labels "' + LABEL + '"}}',
                name,
            ],
            cleanup_window=True,
        )
        fields = found.stdout.strip().split()
        if found.returncode:
            return exact_absence(name=name)
        if len(fields) != 2 or fields[1] != label:
            return False
        ident_value = fields[0]
    ident = ident_value
    inspected = _command(
        [
            "docker",
            "inspect",
            "--format",
            '{{.Id}} {{index .Config.Labels "' + LABEL + '"}}',
            ident,
        ],
        cleanup_window=True,
    )
    if inspected.returncode:
        return exact_absence(container_id=ident)
    if inspected.stdout.strip() != f"{ident} {label}":
        return False
    stopped = _command(
        ["docker", "stop", "--time", "3", ident], timeout=15, cleanup_window=True
    )
    if stopped.returncode:
        return exact_absence(container_id=ident)
    removed = _command(["docker", "rm", ident], timeout=15, cleanup_window=True)
    return removed.returncode == 0 or exact_absence(container_id=ident)


def _verify_outer_cgroup() -> dict[str, int]:
    cgroup_lines = Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines()
    unified = next((line.split(":", 2)[2] for line in cgroup_lines if line.startswith("0::")), None)
    if not unified or ".." in Path(unified).parts:
        raise RuntimeError("fixture requires an identifiable delegated cgroup")
    group = Path("/sys/fs/cgroup") / unified.lstrip("/")
    memory = int((group / "memory.max").read_text(encoding="ascii").strip())
    swap = int((group / "memory.swap.max").read_text(encoding="ascii").strip())
    quota_text, period_text = (group / "cpu.max").read_text(encoding="ascii").split()
    pids = int((group / "pids.max").read_text(encoding="ascii").strip())
    quota = int(quota_text)
    period = int(period_text)
    if (
        memory > 1024**3
        or swap != 0
        or quota < 0
        or period <= 0
        or quota / period > 0.5
        or pids > 64
    ):
        raise RuntimeError("fixture parent cgroup exceeds the reviewed 1GiB/50%/64/no-swap scope")
    return {
        "memory_max_bytes": memory,
        "memory_swap_max_bytes": swap,
        "cpu_quota_us": quota,
        "cpu_period_us": period,
        "pids_max": pids,
    }


def _stop_owned_child_group(child: subprocess.Popen[str]) -> bool:
    """Terminate and reap only the new-session pytest process group we created."""
    if child.poll() is not None:
        try:
            child.communicate(timeout=0)
        except subprocess.TimeoutExpired:
            return False
        return True
    try:
        os.killpg(child.pid, signal.SIGCONT)
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        child.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            child.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            return False
    return child.poll() is not None


def _execute() -> int:
    global _DEADLINE
    _DEADLINE = time.monotonic() + OUTER_SECONDS
    outer_scope = _verify_outer_cgroup()
    if ROOT.resolve(strict=True) != Path("/home/cachyos/ai-scientist").resolve(strict=True):
        raise RuntimeError("fixture must run from the approved repository root")
    if (ROOT / "harness/VERSION").read_text(encoding="ascii").strip() != "0.30.0":
        raise RuntimeError("fixture requires integrated 0.30.0 sources")
    if shutil.disk_usage(ROOT).free < 20 * 1024**3:
        raise RuntimeError("fixture requires the 20 GiB sandbox disk reserve")
    available_kib = int(Path("/proc/meminfo").read_text().split("MemAvailable:")[1].split()[0])
    if available_kib < 7 * 1024**2:
        raise RuntimeError("fixture requires 7 GiB MemAvailable for bounded sibling cgroups")
    if RUNTIME.is_symlink():
        raise RuntimeError("fixture runtime directory cannot be a symlink")
    ident = uuid4().hex
    private = RUNTIME / ident
    private.mkdir(mode=0o700, parents=True)
    if private.stat().st_uid != os.getuid() or private.stat().st_mode & 0o077:
        raise RuntimeError("fixture directory is not privately owned")
    snapshot = private / "source"
    snapshot.mkdir(mode=0o700)
    hashes = _snapshot(ROOT, snapshot)
    ownership: dict[str, object] = {}
    container: dict[str, object] | None = None
    alias: Path | None = None
    private_scorer_dsn: bytes | None = None
    pytest_proc: subprocess.Popen[str] | None = None
    pytest_output_captured = False
    pytest_stdout_path = private / "pytest.stdout"
    pytest_stderr_path = private / "pytest.stderr"
    outcome = 1
    record: dict[str, object] = {
        "schema": "holdout-030-lifecycle-fixture.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "fixture_id": ident,
        "scope": (
            "synthetic baseline ledger; real typed CPU candidate, Scorer worker, "
            "and sandbox lifecycle; no model-quality claim"
        ),
        "outer_cgroup": outer_scope,
        "snapshot_path": str(snapshot),
        "snapshot_sha256": hashes,
    }
    stage = "fixture_setup"
    private.mkdir(exist_ok=True)
    release_path = private / "fixture-release"
    reservation_path = private / "fixture-reserved.json"
    setup_path = private / "fixture-setup.json"
    try:
        stage = "harness_fingerprint_preflight"
        source_fingerprint = _compute_harness_fingerprint(ROOT)
        snapshot_fingerprint = _compute_harness_fingerprint(snapshot)
        if source_fingerprint != snapshot_fingerprint:
            raise RuntimeError("private snapshot harness fingerprint differs from trusted source")
        record["harness_fingerprint"] = source_fingerprint
        plugin = _plugin_text(private)
        compile(Path(plugin).read_text(encoding="utf-8"), plugin, "exec")
        stage = "postgres_provisioning"
        container = _engine_start(private, snapshot, ident, ownership, _DEADLINE)
        stage = "fixture_child_start"
        dsn_dir = Path(str(container["dsn_dir"]))
        alias = snapshot / "data/runtime/postgres/scorer.dsn"
        alias.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        private_scorer_dsn = (dsn_dir / "scorer.dsn").read_bytes()
        alias.write_bytes(private_scorer_dsn)
        alias.chmod(0o600)
        receipt_root = snapshot / "data/runtime/holdout-lifecycle-receipts"
        receipt_root.mkdir(mode=0o700)
        ready_path = private / "fixture-ready.json"
        env = dict(os.environ)
        env.update(
            PYTHONPATH=f"{private}:{snapshot}",
            LAB_HOLDOUT_TEST_DSN_DIR=str(dsn_dir),
            HOLDOUT_FIXTURE_READY=str(ready_path),
            HOLDOUT_FIXTURE_RELEASE=str(release_path),
            HOLDOUT_FIXTURE_RESERVATION=str(reservation_path),
            HOLDOUT_FIXTURE_SETUP=str(setup_path),
            HOLDOUT_FIXTURE_DEADLINE_MONOTONIC=str(_DEADLINE),
            XDG_RUNTIME_DIR=os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
            OPENBLAS_NUM_THREADS="1",
            OMP_NUM_THREADS="1",
            MKL_NUM_THREADS="1",
        )
        record["snapshot_scorer_alias_sha256"] = hashlib.sha256(private_scorer_dsn).hexdigest()
        test_command = [
            str(ROOT / ".venv/bin/python"),
            "-m",
            "pytest",
            "-q",
            "--tb=short",
            "-p",
            Path(plugin).stem,
            "tests/test_postgres_holdout_api_identity.py::"
            "test_run_end_accepts_resumed_abandoned_ordinal_without_experiment_row",
        ]
        with pytest_stdout_path.open("xb") as pytest_stdout_file, pytest_stderr_path.open(
            "xb"
        ) as pytest_stderr_file:
            pytest_stdout_path.chmod(0o600)
            pytest_stderr_path.chmod(0o600)
            pytest_proc = subprocess.Popen(
                test_command,
                cwd=snapshot,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=pytest_stdout_file,
                stderr=pytest_stderr_file,
                start_new_session=True,
            )
        readiness_deadline = min(_DEADLINE - CLEANUP_RESERVE_SECONDS, time.monotonic() + 150)
        stage = "candidate_readiness"
        ready = _wait_event(ready_path, readiness_deadline, pytest_proc)
        run_id, reservation_id = str(ready["run_id"]), str(ready["reservation_id"])
        stage = "source_fingerprint"
        fingerprint = _command(
            [
                str(ROOT / ".venv/bin/python"),
                "docs/ai-scientist/review-evidence/holdout-030-lifecycle-review.py",
                "--print-source-fingerprint",
            ],
            cwd=snapshot,
            env=env,
            timeout=15,
        )
        if fingerprint.returncode:
            raise RuntimeError("snapshot source fingerprint could not be read")
        pin = json.loads(fingerprint.stdout)
        record.update(
            {
                "database": container["database"],
                "ready": ready,
                "source_fingerprint": pin,
                "fixture_setup": (
                    "existing authenticated API/Director/Planner/Scorer setup "
                    "with synthetic baseline records"
                ),
            }
        )
        receipt = receipt_root / f"lifecycle-{ident}.json"
        review_env = dict(env)
        review_env.update(
            LAB_HOLDOUT_LIFECYCLE_API_TOKEN="r" * 32,
            LAB_HOLDOUT_LIFECYCLE_OWNER_ID="fixture:holdout-abandoned-ordinal",
        )
        review_command = [
            str(ROOT / ".venv/bin/python"),
            "docs/ai-scientist/review-evidence/holdout-030-lifecycle-review.py",
            "--run-id",
            run_id,
            "--reservation-id",
            reservation_id,
            "--receipt",
            str(receipt),
            "--expected-source-sha256",
            pin["source_sha256"],
            "--expected-image-lock-sha256",
            pin["image_lock_sha256"],
            "--expected-alembic-head",
            str(container["migration_head"]),
            "--execute-reviewed-stop",
        ]
        review_timeout = int(_DEADLINE - time.monotonic() - CLEANUP_RESERVE_SECONDS)
        if review_timeout < 1:
            raise TimeoutError("review stop has no remaining bounded execution window")
        stage = "reviewed_stop"
        reviewed = _command(
            review_command, cwd=snapshot, env=review_env, timeout=min(75, review_timeout)
        )
        record["review_driver_exit"] = reviewed.returncode
        record["review_driver_stdout"] = reviewed.stdout[-MAX_OUTPUT:]
        record["review_driver_stderr_category"] = "nonempty" if reviewed.stderr else ""
        release_path.write_text(
            ("review-finished" if reviewed.returncode == 0 else "cleanup") + "\n", encoding="ascii"
        )
        stage = "fixture_child_unwind"
        child_timeout = int(_DEADLINE - time.monotonic() - CLEANUP_RESERVE_SECONDS)
        if child_timeout < 1:
            raise TimeoutError("fixture child did not unwind before cleanup reserve")
        pytest_proc.wait(timeout=child_timeout)
        pytest_output_captured = True
        record["fixture_pytest_exit"] = pytest_proc.returncode
        stdout, stdout_truncated = _read_bounded_log(pytest_stdout_path)
        stderr, stderr_truncated = _read_bounded_log(pytest_stderr_path)
        _record_pytest_output(
            record,
            stdout,
            stderr,
            container,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )
        if reviewed.returncode or pytest_proc.returncode:
            raise RuntimeError("review driver or fixture child returned a nonzero exit")
        record["receipt_path"] = str(receipt)
        record["receipt_sha256"] = hashlib.sha256(receipt.read_bytes()).hexdigest()
        outcome = 0
    except KeyboardInterrupt as exc:
        # SIGTERM/SIGINT handlers raise this so the normal exact-owner cleanup
        # path runs before the process exits.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        record["failure_type"] = type(exc).__name__
        record["failure_stage"] = str(ownership.get("stage", stage))
        record["failure_code"] = _safe_failure_code(exc)
        record["interrupted"] = True
        if "migration_exit_code" in ownership:
            record["migration_exit_code"] = ownership["migration_exit_code"]
        if pytest_proc is not None:
            release_path.write_text("cleanup\n", encoding="ascii")
    except Exception as exc:
        record["failure_type"] = type(exc).__name__
        record["failure_stage"] = str(ownership.get("stage", stage))
        record["failure_code"] = _safe_failure_code(exc)
        if "migration_exit_code" in ownership:
            record["migration_exit_code"] = ownership["migration_exit_code"]
        if pytest_proc is not None:
            release_path.write_text("cleanup\n", encoding="ascii")
    finally:
        child_unresolved = False
        if pytest_proc is not None and not pytest_output_captured:
            if pytest_proc.poll() is None:
                release_path.write_text("cleanup\n", encoding="ascii")
            try:
                pytest_proc.wait(timeout=8 if pytest_proc.poll() is None else 3)
                pytest_output_captured = True
            except subprocess.TimeoutExpired:
                child_unresolved = not _stop_owned_child_group(pytest_proc)
                record["fixture_pytest_terminated_before_worker_cleanup"] = not child_unresolved
                try:
                    pytest_proc.wait(timeout=3)
                    pytest_output_captured = True
                except subprocess.TimeoutExpired:
                    record["fixture_pytest_output_capture"] = "bounded_wait_expired"
            if pytest_output_captured:
                record["fixture_pytest_exit"] = pytest_proc.returncode
            else:
                record["fixture_pytest_output_capture"] = "unavailable_after_child_timeout"
        recovery_ok = True
        if outcome != 0 and setup_path.is_file() and not setup_path.is_symlink():
            try:
                setup_data = json.loads(setup_path.read_text(encoding="ascii"))
                run_id = str(setup_data["run_id"])
                reservation_id = None
                if reservation_path.is_file() and not reservation_path.is_symlink():
                    receipt_data = json.loads(reservation_path.read_text(encoding="ascii"))
                    if str(receipt_data["run_id"]) != run_id:
                        raise RuntimeError("reservation receipt does not match setup run")
                    reservation_id = str(receipt_data["reservation_id"])
                record["cleanup_run_id"] = run_id
                if reservation_id is not None:
                    record["cleanup_reservation_id"] = reservation_id
                if container is None:
                    raise RuntimeError("private database setup did not finish")
                cleanup_exit = _cleanup_reserved_run(
                    snapshot, private, Path(str(container["dsn_dir"])), run_id, reservation_id
                )
                record["production_cleanup_exit"] = cleanup_exit
                recovery_ok = cleanup_exit == 0
            except Exception as exc:
                record["cleanup_failure_type"] = type(exc).__name__
                recovery_ok = False
        setup_ok = True
        if setup_path.is_file() and not setup_path.is_symlink() and container is not None:
            try:
                setup_exit = _cleanup_setup_workers(
                    snapshot, private, Path(str(container["dsn_dir"])), setup_path
                )
                record["setup_worker_cleanup_exit"] = setup_exit
                setup_ok = setup_exit == 0
            except Exception as exc:
                record["setup_worker_cleanup_failure_type"] = type(exc).__name__
                setup_ok = False
        recovery_ok = recovery_ok and setup_ok
        if pytest_proc is not None and pytest_proc.poll() is None:
            child_unresolved = not _stop_owned_child_group(pytest_proc)
            if child_unresolved:
                record["fixture_pytest_child_unresolved"] = True
            else:
                record["fixture_pytest_exit"] = pytest_proc.returncode
                record["fixture_pytest_reaped_after_worker_cleanup"] = True
        if pytest_proc is not None:
            if pytest_proc.poll() is not None:
                record["fixture_pytest_exit"] = pytest_proc.returncode
                record["fixture_pytest_output_capture"] = "complete"
                stdout, stdout_truncated = _read_bounded_log(pytest_stdout_path)
                stderr, stderr_truncated = _read_bounded_log(pytest_stderr_path)
                _record_pytest_output(
                    record,
                    stdout,
                    stderr,
                    container,
                    stdout_truncated=stdout_truncated,
                    stderr_truncated=stderr_truncated,
                )
            else:
                record["fixture_pytest_output_capture"] = "partial_child_unresolved"
                record["fixture_pytest_output_omitted_while_child_unresolved"] = True
                record["fixture_pytest_stdout"] = ""
                record["fixture_pytest_stderr"] = ""
                record["fixture_pytest_stdout_truncated"] = pytest_stdout_path.stat().st_size > 0
                record["fixture_pytest_stderr_truncated"] = pytest_stderr_path.stat().st_size > 0
        pytest_stdout_path.unlink(missing_ok=True)
        pytest_stderr_path.unlink(missing_ok=True)
        safe_cleanup = not child_unresolved and recovery_ok
        owned_database = _owned_database_for_cleanup(container, ownership)
        database_removed = not bool(owned_database)
        if owned_database and safe_cleanup:
            try:
                database_removed = _owned_container_cleanup(owned_database)
                record["owned_database_container_removed"] = database_removed
            except Exception as exc:
                record["owned_database_cleanup_error_type"] = type(exc).__name__
                record["owned_database_container_removed"] = False
        elif owned_database:
            record["owned_database_container_removed"] = False
            record["cleanup_held_for_unresolved_worker"] = True
        if safe_cleanup and database_removed:
            if (
                alias is not None
                and alias.exists()
                and not alias.is_symlink()
                and private_scorer_dsn is not None
                and alias.read_bytes() == private_scorer_dsn
            ):
                alias.unlink()
            (private / "postgres.env").unlink(missing_ok=True)
            dsn_dir_value = container.get("dsn_dir") if container is not None else private / "dsns"
            dsn_dir_cleanup = Path(str(dsn_dir_value))
            if dsn_dir_cleanup.is_dir() and not dsn_dir_cleanup.is_symlink():
                for path in dsn_dir_cleanup.glob("*.dsn"):
                    path.unlink(missing_ok=True)
                dsn_dir_cleanup.rmdir()
            record["credentials_removed"] = True
        else:
            record["credentials_removed"] = False
        record["source_snapshot_retained_for_review"] = True
        if (
            child_unresolved
            or not recovery_ok
            or not database_removed
            or record.get("owned_database_container_removed") is False
        ):
            outcome = 1
        record["exit_code"] = outcome
        (private / "fixture-receipt.json").write_text(
            json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        (private / "fixture-receipt.json").chmod(0o600)
    return outcome


def main() -> int:
    if "--execute-reviewed-fixture" not in sys.argv[1:]:
        if sys.argv[1:] != ["--print-plan"]:
            print("Refusing execution without --execute-reviewed-fixture", file=sys.stderr)
            return 2
        print(
            json.dumps(
                {
                    "fixture": (
                        "one disposable PostgreSQL 16 run-end reservation and one real "
                        "typed-candidate worker stop"
                    ),
                    "database_prefix": DB_PREFIX,
                    "worker_seconds": WORKER_SECONDS,
                    "outer_seconds": OUTER_SECONDS,
                    "requires_explicit_flag": True,
                    "starts_nothing_in_plan_mode": True,
                },
                sort_keys=True,
            )
        )
        return 0

    def unwind_on_signal(_signum, _frame):
        raise KeyboardInterrupt("fixture received termination request")

    signal.signal(signal.SIGTERM, unwind_on_signal)
    signal.signal(signal.SIGINT, unwind_on_signal)
    return _execute()


if __name__ == "__main__":
    raise SystemExit(main())
