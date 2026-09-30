#!/usr/bin/env python3
"""Reviewed disposable PostgreSQL, actual CPU baseline/tamper, production CLI proof.
Inert without --execute-reviewed-pg; all cleanup is restricted to proof identities.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess  # nosec B404 -- fixed Docker, Python, and Alembic argv vectors
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

os.umask(0o077)

import psycopg
from psycopg import sql

ROOT = Path("/home/cachyos/ai-scientist")
SOURCE = ROOT / "data/runtime/parallel-m0/tamper-057/source"
TARGETS = ('tests/test_tamper057_pg_cli.py',)
TEST_NODES = ('tests/test_tamper057_pg_cli.py::test_actual_pg_rejections_and_full_cli_restart',)
EXPECTED_HEAD = "0031_stopped_proposal"
DB_PREFIX = "swapp_lab_tamper057_"
RUNTIME = SOURCE / "data/runtime/pg-smoke"
EVIDENCE = ROOT / "data/runtime/parallel-m0/tamper-057/review-evidence"
LABEL = "swapp.review.tamper057"
ROLES = ("migrator", "director", "planner", "scorer")
IMAGE = "postgres:16-bookworm"
EXPECTED_IMAGE_ID = 'sha256:efedf3595f1d6f415c08568ba171029bf54052e754cc9f030e3f2412b21f3d67'
OUTER_SECONDS = 1050
CLEANUP_RESERVE_SECONDS = 45
MAX_OUTPUT = 64 * 1024
_DEADLINE: float | None = None

# Record actual collection and all execution phases in the same pytest process.
# This observer contains no DB, worker, sandbox or service operations.
PYTEST_OBSERVER = '''import json
import os
from pathlib import Path

_record = {"collected": [], "deselected": [], "phases": []}

def pytest_sessionstart(session):
    import lab.director.holdout
    import harness
    source = Path(os.environ["LAB_REVIEW_SOURCE_ROOT"]).resolve(strict=True)
    for module in (lab.director.holdout, harness):
        assert Path(module.__file__).resolve(strict=True).is_relative_to(source)

def pytest_collection_finish(session):
    _record["collected"] = [item.nodeid for item in session.items]

def pytest_deselected(items):
    _record["deselected"].extend(item.nodeid for item in items)

def pytest_runtest_logreport(report):
    phase = {
        "nodeid": report.nodeid, "when": report.when, "outcome": report.outcome,
        "wasxfail": getattr(report, "wasxfail", None),
    }
    if report.failed:
        crash = getattr(report.longrepr, "reprcrash", None)
        phase["failure"] = {
            "path": str(getattr(crash, "path", ""))[:1024],
            "line": getattr(crash, "lineno", None),
            "message": str(getattr(crash, "message", report.longrepr))[:4096],
        }
    _record["phases"].append(phase)

def pytest_sessionfinish(session, exitstatus):
    _record["exit_code"] = int(exitstatus)
    path = Path(os.environ["LAB_REVIEW_PYTEST_RECEIPT"])
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(_record, sort_keys=True) + "\\n")
    path.chmod(0o600)
'''


def _execution_summary(payload: dict[str, object]) -> dict[str, object]:
    collected = payload.get("collected")
    phases = payload.get("phases")
    deselected = payload.get("deselected")
    if not isinstance(collected, list) or not isinstance(phases, list):
        raise RuntimeError("pytest observer returned invalid test identities")
    if not all(isinstance(item, str) for item in collected):
        raise RuntimeError("pytest observer returned non-string test identities")
    grouped: dict[str, list[dict[str, object]]] = {}
    for phase in phases:
        if not isinstance(phase, dict) or not isinstance(phase.get("nodeid"), str):
            raise RuntimeError("pytest observer returned invalid execution phase")
        grouped.setdefault(phase["nodeid"], []).append(phase)
    targets_present = all(
        any(node.startswith(target + "::") for node in collected) for target in TARGETS
    )
    known_nodes = all(
        any(node.startswith(target + "::") for target in TARGETS) for node in collected
    )
    all_phases_passed = all(
        len(items) == 3
        and {item.get("when") for item in items} == {"setup", "call", "teardown"}
        and all(item.get("outcome") == "passed" and not item.get("wasxfail") for item in items)
        for items in grouped.values()
    )
    complete = (
        bool(collected)
        and len(set(collected)) == len(collected)
        and set(grouped) == set(collected)
        and targets_present
        and known_nodes
        and deselected == []
        and payload.get("exit_code") == 0
        and all_phases_passed
    )
    return {
        "collected_test_ids": collected,
        "collected_test_count": len(collected),
        "executed_test_ids": sorted(grouped),
        "executed_test_count": len(grouped),
        "deselected_test_ids": deselected,
        "phase_outcomes": phases,
        "all_collected_tests_passed": complete,
    }


def _command(argv: list[str], *, timeout: int = 30, **kwargs) -> subprocess.CompletedProcess[str]:
    if _DEADLINE is not None:
        cleanup = bool(kwargs.pop("cleanup_window", False))
        reserve = 0 if cleanup else CLEANUP_RESERVE_SECONDS
        remaining = int(_DEADLINE - time.monotonic() - reserve)
        if remaining < 1:
            raise TimeoutError("holdout ownership SQL runner reached its cleanup reserve")
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
        raise RuntimeError("bounded holdout ownership review command returned oversized output")
    return result


def _source_files() -> list[str]:
    fixed = (
        "alembic.ini", "pyproject.toml", "uv.lock", "ops/sandbox-image.lock",
        "Dockerfile.sandbox", "docker/sandbox/requirements.txt",
    )
    selected = [SOURCE / item for item in fixed]
    for directory in ("lab", "harness", "vendor", "tests"):
        selected.extend(
            path
            for path in sorted((SOURCE / directory).rglob("*"))
            if path.is_file()
            and not path.is_symlink()
            and "__pycache__" not in path.parts
            and path.suffix != ".pyc"
        )
    relative = sorted({path.relative_to(SOURCE).as_posix() for path in selected})
    forbidden = (
        "run_care_baseline_calibration_process",
        "run_care_baseline_cell_process(",
        "run_care_baseline_claim(",
        "run_candidate_fit_score",
        "run_scorer_process(",
        "run_holdout_process(",
        "systemd-run",
        "docker run",
    )
    for target in TARGETS:
        if target not in relative:
            raise RuntimeError("fixed holdout ownership PostgreSQL test target is missing")
        target_source = (SOURCE / target).read_text(encoding="utf-8")
        if "pytest.mark.live" not in target_source:
            raise RuntimeError("fixed holdout ownership PostgreSQL test is not marked live")
        # Reviewed fixed test intentionally launches only its own UUID-bound CPU workers.
        if "SWAPP_GPU_RUNTIME_DB" not in target_source:
            raise RuntimeError("proof must remove all inherited GPU principal configuration")
    return relative


def _capture_source(snapshot: Path) -> dict[str, str]:
    paths = _source_files()
    initial = {path: hashlib.sha256((SOURCE / path).read_bytes()).hexdigest() for path in paths}
    for relative in paths:
        original = SOURCE / relative
        destination = snapshot / relative
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.write_bytes(original.read_bytes())
        destination.chmod(0o600 if destination.suffix in {".py", ".toml", ".ini", ".md"} else 0o644)
    python = ROOT / ".venv/bin/python"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise RuntimeError("reviewed repository virtualenv is unavailable")
    for relative, digest in initial.items():
        if hashlib.sha256((snapshot / relative).read_bytes()).hexdigest() != digest:
            raise RuntimeError("holdout ownership source snapshot failed byte parity")
        if hashlib.sha256((SOURCE / relative).read_bytes()).hexdigest() != digest:
            raise RuntimeError("holdout ownership source changed during snapshot capture")
    return initial


def _verify_outer_cgroup() -> dict[str, int]:
    unified = next(
        (line.split(":", 2)[2] for line in Path("/proc/self/cgroup").read_text().splitlines()
         if line.startswith("0::")),
        None,
    )
    if not unified or ".." in Path(unified).parts:
        raise RuntimeError("runner requires an identifiable delegated cgroup")
    group = Path("/sys/fs/cgroup") / unified.lstrip("/")
    memory = int((group / "memory.max").read_text().strip())
    swap = int((group / "memory.swap.max").read_text().strip())
    quota_text, period_text = (group / "cpu.max").read_text().split()
    pids = int((group / "pids.max").read_text().strip())
    quota, period = int(quota_text), int(period_text)
    if (
        memory > 1024**3
        or swap != 0
        or quota < 0
        or period <= 0
        or quota / period > 0.5
        or pids > 64
    ):
        raise RuntimeError("runner parent exceeds 1GiB/50% CPU/64 tasks/no-swap cap")
    return {"memory_max_bytes": memory, "swap_max_bytes": swap, "cpu_quota_us": quota,
            "cpu_period_us": period, "tasks_max": pids}


def _server_identity(connection, database: str, role: str, container_ips: set[str]):
    identity = connection.execute(
        "SELECT current_database(),session_user,host(inet_server_addr()),inet_server_port(),"
        "pg_postmaster_start_time()::text"
    ).fetchone()
    if (identity[0] != database or identity[1] != f"swapp_lab_{role}"
            or identity[2] not in container_ips or identity[3] != 5432):
        raise RuntimeError("role DSN is not bound to the exact private PostgreSQL instance")
    return identity[0], identity[2], identity[3], identity[4]


def _exact_container_absent(container_id: str | None, name: str) -> bool:
    if container_id is not None:
        query = _command(
            ["docker", "ps", "--all", "--no-trunc", "--filter", f"id={container_id}",
             "--format", "{{.ID}}"],
            timeout=3,
            cleanup_window=True,
        )
    else:
        query = _command(
            ["docker", "ps", "--all", "--filter", f"name=^/{name}$", "--format", "{{.ID}}"],
            timeout=3,
            cleanup_window=True,
        )
    return query.returncode == 0 and not any(line.strip() for line in query.stdout.splitlines())


def _cleanup_container(container_id: str | None, name: str, identifier: str) -> bool:
    if container_id is None or re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
        captured = _command(
            ["docker", "inspect", "--format", "{{.Id}} {{index .Config.Labels \"" + LABEL
             + "\"}}", name],
            timeout=5,
            cleanup_window=True,
        )
        if captured.returncode:
            return _exact_container_absent(None, name)
        values = captured.stdout.strip().split()
        if len(values) != 2 or values[1] != identifier:
            return False
        container_id = values[0]
    inspected = _command(
        ["docker", "inspect", "--format", "{{.Id}} {{index .Config.Labels \"" + LABEL
         + "\"}}", container_id],
        timeout=5,
        cleanup_window=True,
    )
    if inspected.returncode:
        return _exact_container_absent(container_id, name)
    if inspected.stdout.strip() != f"{container_id} {identifier}":
        return False
    stopped = _command(["docker", "stop", "--time", "3", container_id], timeout=5,
                       cleanup_window=True)
    if stopped.returncode:
        return _exact_container_absent(container_id, name)
    removed = _command(["docker", "rm", container_id], timeout=5, cleanup_window=True)
    return removed.returncode == 0 or _exact_container_absent(container_id, name)


def _cleanup_owned_execution(snapshot: Path) -> bool:
    import importlib.util
    helper = Path(__file__).with_name("cleanup_tamper057.py")
    spec = importlib.util.spec_from_file_location("tamper057_cleanup", helper)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.cleanup(snapshot)
    (snapshot.parent / "owned-execution-cleanup.json").write_text(json.dumps(result, indent=2) + "\n")
    return result["all_owned_execution_drained"]


def _main() -> int:
    global _DEADLINE
    _DEADLINE = time.monotonic() + OUTER_SECONDS
    outer = _verify_outer_cgroup()
    # Never ask production Scorer to create/change shared resource configuration.
    scope = _command(["/usr/bin/systemctl", "--user", "show", "swapp-ai-scientist-scorer.slice", "--property=LoadState,ActiveState,ControlGroup"], timeout=5)
    props = dict(line.split("=", 1) for line in scope.stdout.splitlines() if "=" in line)
    if scope.returncode or props.get("LoadState") != "loaded" or props.get("ActiveState") != "active":
        raise RuntimeError("existing Scorer slice must already be active")
    group = Path("/sys/fs/cgroup") / props["ControlGroup"].lstrip("/")
    for name, expected in {"memory.max": "2147483648", "memory.swap.max": "0", "cpu.max": "100000 100000", "pids.max": "32"}.items():
        if (group / name).read_text().strip() != expected:
            raise RuntimeError("existing shared Scorer slice cap differs")
    if (SOURCE / "harness/VERSION").read_text().strip() != "0.37.0":
        raise RuntimeError("proof requires exact reviewed 0.37.0 source")
    sandbox_pin = (SOURCE / "ops/sandbox-image.lock").read_text().strip()
    sandbox_inspect = _command(["/usr/bin/docker", "image", "inspect", sandbox_pin, "--format", "{{.Id}}"], timeout=5)
    if sandbox_inspect.returncode or sandbox_inspect.stdout.strip() != sandbox_pin:
        raise RuntimeError("reviewed sandbox image must already exist by exact image ID")
    if SOURCE.resolve(strict=True) != Path(
        "/home/cachyos/ai-scientist/data/runtime/parallel-m0/tamper-057/source"
    ):
        raise RuntimeError("holdout ownership source path is not the fixed private worktree")
    if shutil.disk_usage(ROOT).free < 20 * 1024**3:
        raise RuntimeError("runner requires at least 20 GiB free disk")
    if int(Path("/proc/meminfo").read_text().split("MemAvailable:")[1].split()[0]) < 6 * 1024**2:
        raise RuntimeError("runner requires 6 GiB available memory for bounded sibling scopes")

    identifier = uuid4().hex
    private = RUNTIME / identifier
    private.mkdir(mode=0o700, parents=True)
    if private.stat().st_uid != os.getuid() or private.stat().st_mode & 0o077:
        raise RuntimeError("runner private directory is not owner-only")
    snapshot = private / "source"
    snapshot.mkdir(mode=0o700)
    source_hashes = _capture_source(snapshot)
    reviewed_paths = (Path(__file__), Path(__file__).with_name("cleanup_tamper057.py"))
    reviewed_hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in reviewed_paths}
    runner_bytes = Path(__file__).read_bytes()
    runner_copy = private / "runner.py"
    runner_copy.write_bytes(runner_bytes)
    runner_copy.chmod(0o600)
    target_hashes = {target: source_hashes[target] for target in TARGETS}
    observer_dir = private / "pytest-observer"
    observer_dir.mkdir(mode=0o700)
    observer_path = observer_dir / "research046_pytest_observer.py"
    observer_bytes = PYTEST_OBSERVER.encode("utf-8")
    observer_path.write_bytes(observer_bytes)
    observer_path.chmod(0o600)
    pytest_receipt = private / "pytest-execution.json"
    database = DB_PREFIX + identifier[:12]
    container_name = "swapp-tamper057-pg-" + identifier[:12]
    admin_password = secrets.token_urlsafe(32)
    role_passwords = {role: secrets.token_urlsafe(32) for role in ROLES}
    env_file = private / "postgres.env"
    env_file.write_text(
        f"POSTGRES_USER=review_admin\nPOSTGRES_PASSWORD={admin_password}\nPOSTGRES_DB={database}\n",
        encoding="ascii",
    )
    env_file.chmod(0o600)
    dsn_dir = private / "dsns"
    dsn_dir.mkdir(mode=0o700)
    container_id: str | None = None
    creation_attempted = False
    credentials_removed = False
    successful = False
    record: dict[str, object] = {
        "schema": "tamper-057-expiry-pg-review.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "fixture_id": identifier,
        "source": str(SOURCE),
        "test_targets": list(TARGETS),
        "test_sha256": target_hashes,
        "pytest_observer_sha256": hashlib.sha256(observer_bytes).hexdigest(),
        "pytest_observer_path": str(observer_path.relative_to(ROOT)),
        "runner_sha256": hashlib.sha256(runner_bytes).hexdigest(),
        "reviewed_driver_cleanup_sha256": reviewed_hashes,
        "proof_source_sha256": {p: source_hashes[p] for p in ("tests/test_tamper057_pg_cli.py", "tests/tamper057_worker.py")},
        "runner_snapshot": str(runner_copy.relative_to(ROOT)),
        "base_runner_sha256": "e5b4b46bd4d8f611d1127ebec8199f22fded047adf92d022b062c6718d665455",
        "expected_migration_head": EXPECTED_HEAD,
        "outer_cgroup": outer,
        "scope": (
            "Actual PostgreSQL registered-run source tamper, real CPU sandbox/Scorer baseline measurements, "
            "typed no-score candidate rejections and full production CLI restart rejection; no model/GPU/AOS"
        ),
        "source_sha256": source_hashes,
    }
    try:
        image = _command(["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"], timeout=15)
        if image.returncode or re.fullmatch(r"sha256:[0-9a-f]{64}\n?", image.stdout) is None:
            raise RuntimeError("local PostgreSQL 16 image is unavailable or unpinned")
        image_id = image.stdout.strip()
        if image_id != EXPECTED_IMAGE_ID:
            raise RuntimeError("PostgreSQL image differs from the reviewed immutable image")
        creation_attempted = True
        created = _command(
            ["docker", "create", "--name", container_name, "--label", f"{LABEL}={identifier}",
             "--memory", "512m", "--memory-swap", "512m", "--cpus", "0.5",
             "--pids-limit", "64", "--shm-size", "64m", "--tmpfs",
             "/var/lib/postgresql/data:rw,size=268435456", "--publish", "127.0.0.1::5432",
             "--env-file", str(env_file), image_id, "postgres", "-c", "shared_buffers=32MB",
             "-c", "max_connections=30", "-c", "statement_timeout=20000", "-c",
             "lock_timeout=5000"],
            timeout=30,
        )
        if created.returncode:
            raise RuntimeError("disposable holdout ownership PostgreSQL container creation failed")
        container_id = created.stdout.strip()
        if re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
            raise RuntimeError("PostgreSQL container id is malformed")
        record.update(
            container_id=container_id,
            container_name=container_name,
            database=database,
            image_id=image_id,
        )
        if _command(["docker", "start", container_id], timeout=15).returncode:
            raise RuntimeError("disposable holdout ownership database did not start")
        inspected = _command(
            ["docker", "inspect", "--format",
             "{{.Id}}|{{.Image}}|{{.HostConfig.Memory}}|{{.HostConfig.MemorySwap}}|"
             "{{.HostConfig.NanoCpus}}|{{.HostConfig.PidsLimit}}|{{.State.Running}}|"
             '{{index .Config.Labels "' + LABEL + '"}}|{{json .NetworkSettings.Ports}}|'
             "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}", container_id],
        )
        values = inspected.stdout.strip().split("|", 9)
        if inspected.returncode or len(values) != 10:
            raise RuntimeError("database container ownership inspection failed")
        if (values[0] != container_id or values[1] != image_id
                or values[2] != str(512 * 1024**2) or values[3] != str(512 * 1024**2)
                or values[4] != "500000000" or values[5] != "64"
                or values[6].lower() != "true" or values[7] != identifier):
            raise RuntimeError("database identity, label, or resource limits differ")
        ports = json.loads(values[8]).get("5432/tcp")
        container_ips = {value for value in values[9].split() if value}
        if (not isinstance(ports, list) or len(ports) != 1
                or ports[0].get("HostIp") != "127.0.0.1" or not container_ips):
            raise RuntimeError("database port or network binding is not private")
        port = int(ports[0]["HostPort"])
        record.update(
            container_id=container_id,
            container_name=container_name,
            database=database,
            image_id=image_id,
            endpoint={
                "published_host": "127.0.0.1",
                "published_port": port,
                "container_port": 5432,
                "container_ips": sorted(container_ips),
            },
        )
        admin = None
        ready_deadline = min(_DEADLINE - CLEANUP_RESERVE_SECONDS, time.monotonic() + 45)
        while time.monotonic() < ready_deadline:
            try:
                admin = psycopg.connect(host="127.0.0.1", port=port, user="review_admin",
                    password=admin_password, dbname=database, autocommit=True, connect_timeout=1)
                break
            except psycopg.OperationalError:
                time.sleep(0.25)
        if admin is None:
            raise RuntimeError("private PostgreSQL readiness deadline expired")
        with admin:
            for role in ROLES:
                role_name = "swapp_lab_" + role
                admin.execute(sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
                    "CONNECTION LIMIT 10 PASSWORD {}"
                ).format(sql.Identifier(role_name), sql.Literal(role_passwords[role])))
                admin.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(database), sql.Identifier(role_name)))
                (dsn_dir / f"{role}.dsn").write_text(
                    f"postgresql+psycopg://{role_name}:{role_passwords[role]}@127.0.0.1:{port}/{database}\n",
                    encoding="ascii",
                )
                (dsn_dir / f"{role}.dsn").chmod(0o600)
            admin.execute(sql.SQL("GRANT CREATE ON DATABASE {} TO swapp_lab_migrator").format(
                sql.Identifier(database)))
            for schema in ("lab", "scorer"):
                admin.execute(sql.SQL("CREATE SCHEMA {} AUTHORIZATION swapp_lab_migrator").format(
                    sql.Identifier(schema)))
            admin.execute("GRANT USAGE ON SCHEMA lab TO swapp_lab_director, swapp_lab_scorer")
            admin.execute("GRANT USAGE ON SCHEMA scorer TO swapp_lab_planner")
        admin.close()
        env = {key: value for key, value in os.environ.items()
               if key not in {"LAB_MIGRATOR_DSN_FILE", "LAB_DIRECTOR_DSN_FILE",
                              "LAB_PLANNER_DSN_FILE", "LAB_SCORER_DSN_FILE",
                              "LAB_HOLDOUT_TEST_DSN_DIR"}}
        env.update(PYTHONPATH=str(snapshot), LAB_MIGRATOR_DSN_FILE=str(dsn_dir / "migrator.dsn"))
        migration_budget = int(_DEADLINE - time.monotonic() - CLEANUP_RESERVE_SECONDS)
        if migration_budget < 1:
            raise TimeoutError("migration cannot fit before cleanup reserve")
        migrated = _command([str(ROOT / ".venv/bin/python"), "-m", "alembic", "upgrade", "head"],
                            timeout=min(90, migration_budget), cwd=snapshot, env=env)
        for secret in (admin_password, *role_passwords.values()):
            # Captured output is never recorded; redact before the bounded record in case a
            # driver error includes a credential.
            migrated = subprocess.CompletedProcess(migrated.args, migrated.returncode,
                migrated.stdout.replace(secret, "[REDACTED]"),
                migrated.stderr.replace(secret, "[REDACTED]"))
        record["migration_exit_code"] = migrated.returncode
        record["migration_output"] = (migrated.stdout + migrated.stderr)[-MAX_OUTPUT:]
        if migrated.returncode:
            raise RuntimeError("holdout ownership source migration failed")
        identities = set()
        for role in ROLES:
            uri = (dsn_dir / f"{role}.dsn").read_text(encoding="ascii").strip()
            with psycopg.connect(uri.replace("postgresql+psycopg://", "postgresql://", 1),
                                 connect_timeout=3) as connection:
                identities.add(_server_identity(connection, database, role, container_ips))
        if len(identities) != 1:
            raise RuntimeError("role DSNs do not resolve to the same server generation")
        with psycopg.connect(
            host="127.0.0.1",
            port=port,
            user="review_admin",
            password=admin_password,
            dbname=database,
            autocommit=True,
        ) as admin_check:
            head = admin_check.execute("SELECT version_num FROM lab.alembic_version").fetchone()[0]
        record.update(migration_head=head, sql_instance=next(iter(identities)))
        if head != EXPECTED_HEAD:
            raise RuntimeError("migration head does not match research resume and closure 046 expectation")
        test_env = dict(env, LAB_HOLDOUT_TEST_DSN_DIR=str(dsn_dir),
                        OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1")
        test_env.update(
            PYTHONPATH=os.pathsep.join((str(observer_dir), str(snapshot))),
            LAB_REVIEW_SOURCE_ROOT=str(snapshot),
            LAB_REVIEW_PYTEST_RECEIPT=str(pytest_receipt),
        )
        test_tmp = snapshot / "data/runtime/review-pytest"
        test_tmp.mkdir(mode=0o700, parents=True)
        test_budget = int(_DEADLINE - time.monotonic() - CLEANUP_RESERVE_SECONDS)
        if test_budget < 1:
            raise TimeoutError("holdout ownership SQL test cannot fit before cleanup reserve")
        tested = _command([str(ROOT / ".venv/bin/python"), "-m", "pytest", "-q", "--tb=short",
                           "--maxfail=0",
                           "-p", "research046_pytest_observer",
                           "--basetemp", str(test_tmp), "-m", "live", *TEST_NODES],
                          timeout=min(900, test_budget), cwd=snapshot, env=test_env)
        test_output = tested.stdout + tested.stderr
        for secret in (admin_password, *role_passwords.values()):
            test_output = test_output.replace(secret, "[REDACTED]")
        record["test_exit_code"] = tested.returncode
        record["test_output"] = test_output[-MAX_OUTPUT:]
        match = re.search(r"(\d+) passed", test_output)
        passed = int(match.group(1)) if match else 0
        skipped = bool(re.search(r"\d+ (?:skipped|deselected)", test_output))
        record["test_passed_count"] = passed
        record["test_skipped_or_deselected"] = skipped
        if pytest_receipt.stat().st_size > MAX_OUTPUT:
            raise RuntimeError("pytest observer receipt exceeds fixed output budget")
        receipt_bytes = pytest_receipt.read_bytes()
        receipt_text = receipt_bytes.decode("utf-8")
        for secret in (admin_password, *role_passwords.values()):
            receipt_text = receipt_text.replace(secret, "[REDACTED]")
        execution_summary = _execution_summary(json.loads(receipt_text))
        record["pytest_receipt_path"] = str(pytest_receipt.relative_to(ROOT))
        record["pytest_receipt_sha256"] = hashlib.sha256(receipt_bytes).hexdigest()
        record["test_execution"] = execution_summary
        successful = (
            tested.returncode == 0
            and passed == execution_summary["collected_test_count"]
            and not skipped
            and execution_summary["all_collected_tests_passed"] is True
        )
    except Exception as exc:
        record["error_type"] = type(exc).__name__
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            children_removed = _cleanup_owned_execution(snapshot)
            record["owned_execution_removed"] = children_removed
            cleanup_ok = (
                _cleanup_container(container_id, container_name, identifier)
                if creation_attempted and children_removed
                else not creation_attempted
            )
        except Exception as exc:
            cleanup_ok = False
            record["container_cleanup_error_type"] = type(exc).__name__
        record["owned_container_removed"] = cleanup_ok
        if cleanup_ok:
            try:
                for role in ROLES:
                    duplicate = snapshot / "data/runtime/postgres" / f"{role}.dsn"
                    if duplicate.is_symlink():
                        raise RuntimeError("duplicate role file became a symlink")
                    duplicate.unlink(missing_ok=True)
                env_file.unlink(missing_ok=True)
                for path in dsn_dir.glob("*.dsn"):
                    if path.is_symlink() or not path.is_file():
                        raise RuntimeError("unexpected credential path during cleanup")
                    path.unlink(missing_ok=True)
                dsn_dir.rmdir()
                credentials_removed = True
            except Exception as exc:
                record["credential_cleanup_error_type"] = type(exc).__name__
        record["credentials_removed"] = credentials_removed
        record["source_now_matches_snapshot"] = all(
            (SOURCE / path).is_file()
            and hashlib.sha256((SOURCE / path).read_bytes()).hexdigest() == digest
            for path, digest in source_hashes.items()
        )
        record["reviewed_driver_cleanup_unchanged"] = all(hashlib.sha256((ROOT / p).read_bytes()).hexdigest() == digest for p, digest in reviewed_hashes.items())
        record["runner_unchanged"] = (
            Path(__file__).read_bytes() == runner_bytes == runner_copy.read_bytes()
        )
        record["pytest_observer_unchanged"] = observer_path.read_bytes() == observer_bytes
        record["snapshot_unchanged"] = all(
            (snapshot / path).is_file()
            and hashlib.sha256((snapshot / path).read_bytes()).hexdigest() == digest
            for path, digest in source_hashes.items()
        )
        record["exit_code"] = int(not (successful and cleanup_ok and credentials_removed
                                        and record["source_now_matches_snapshot"]
                                        and record["snapshot_unchanged"]
                                        and record["runner_unchanged"]
                                        and record["reviewed_driver_cleanup_unchanged"]
                                        and record["pytest_observer_unchanged"]))
        evidence = EVIDENCE / f"tamper-057-pg-{identifier[:12]}.json"
        temporary_evidence = evidence.with_suffix(".json.tmp")
        temporary_evidence.write_text(
            json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        temporary_evidence.chmod(0o600)
        temporary_evidence.replace(evidence)
        evidence.chmod(0o600)
        print(
            json.dumps(
                {
                    "evidence": str(evidence.relative_to(ROOT)),
                    "exit_code": record["exit_code"],
                    "revision": record.get("migration_head"),
                    "test_exit_code": record.get("test_exit_code"),
                    "container_removed": cleanup_ok,
                    "source_matches_snapshot": record["source_now_matches_snapshot"],
                }
            ),
            flush=True,
        )
    return int(record["exit_code"])


def main() -> int:
    if sys.argv[1:] == ["--print-plan"]:
        print(json.dumps({"source": str(SOURCE), "test_targets": list(TARGETS),
                          "expected_head": EXPECTED_HEAD, "database_prefix": DB_PREFIX,
                          "outer_seconds": OUTER_SECONDS,
                          "cleanup_reserve_seconds": CLEANUP_RESERVE_SECONDS,
                          "starts_nothing": True}, sort_keys=True))
        return 0
    if sys.argv[1:] != ["--execute-reviewed-pg"]:
        print("Refusing PostgreSQL execution without --execute-reviewed-pg", file=sys.stderr)
        return 2
    def unwind_on_signal(_signum, _frame):
        raise KeyboardInterrupt("holdout ownership SQL review received termination")

    signal.signal(signal.SIGTERM, unwind_on_signal)
    signal.signal(signal.SIGINT, unwind_on_signal)
    import fcntl
    with (ROOT / "data/runtime/parallel-m0/cpu-check.lock").open("a") as cpu_lock:
        fcntl.flock(cpu_lock, fcntl.LOCK_EX)
        return _main()


if __name__ == "__main__":
    raise SystemExit(main())
