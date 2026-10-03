"""Read-only checks for the installed, bounded local research configuration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = Path.home() / ".config/swapp-ai-scientist"
RUNTIME = ROOT / "data/runtime"


def private_file(path: Path) -> bytes:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or not 1 <= metadata.st_size <= 256 * 1024
    ):
        raise ValueError("research configuration must be a private regular file")
    return path.read_bytes()


def read_environment(path: Path) -> dict[str, str]:
    values = {}
    for line in private_file(path).decode().splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if (
            not separator
            or key in values
            or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
            or any(c in value for c in "\x00\r\n")
        ):
            raise ValueError("unsupported deployment environment syntax")
        values[key] = value
    return values


def validate_unit_name(value: str) -> None:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.@-]{0,180}\.service", value):
        raise ValueError("GPU principal must be a fixed service unit")


def installed_configuration(*, verify_database: bool = False, pending: bool = False) -> dict | None:
    path = CONFIG / ("research.pending.json" if pending else "research.json")
    if not path.exists():
        return None
    from lab.api.registry import load_principals, load_suite_registry
    from lab.director.local_llm import provider_config_sha256

    document = json.loads(private_file(path))
    if document.get("schema") != "self-service-research.v1" or not document.get(
        "holdout_installed"
    ):
        raise ValueError("unsupported or incomplete research installation")
    registry_path = Path(document["registry_file"])
    if registry_path != RUNTIME / "console-bootstrap-034/registry.json":
        raise ValueError("research registry differs from main deployment")
    registry = load_suite_registry(registry_path, RUNTIME)
    entry = registry.get(document["suite_id"])
    registry.verify_entry(entry)
    if (
        entry.provider != "local-qwen"
        or entry.provider_config_sha256 != provider_config_sha256("research")
        or registry.entry_sha256(entry) != document["provider_registry_entry_sha256"]
        or entry.suite_manifest_sha256 != document["suite_manifest_sha256"]
    ):
        raise ValueError("research model/provider/manifest drift requires new setup")
    principals = Path(document["principals_file"])
    if principals != RUNTIME / "console-bootstrap-034/principals.json":
        raise ValueError("research principal differs from main deployment")
    if hashlib.sha256(private_file(principals)).hexdigest() != document["principal_sha256"]:
        raise ValueError("principal changed; repeat research setup")
    if len([p for p in load_principals(principals) if p.origin == "local"]) != 1:
        raise ValueError("single local principal required")
    worker = read_environment(
        CONFIG / ("research-worker.pending.env" if pending else "lab-worker.env")
    )
    if worker != document["worker_environment"]:
        raise ValueError("worker configuration changed; repeat setup")
    if worker.get(
        "SWAPP_LAB_GPU_UNIT"
    ) != "swapp-ai-scientist-director-drain.service" or worker.get("SWAPP_GPU_RUNTIME_DB") != str(
        Path.home() / ".local/state/swapp-gpu/arbiter.sqlite3"
    ):
        raise ValueError("canonical arbiter or drain principal differs")
    validate_unit_name(worker["SWAPP_AOS_GPU_UNIT"])
    if verify_database:
        from sqlalchemy import create_engine, text
        from sqlalchemy.engine import make_url

        credential = Path(document["scorer_dsn_file"])
        url = make_url(private_file(credential).decode().strip())
        if (
            url.host != "127.0.0.1"
            or url.port != 55432
            or url.database != document["expected_database"]
            or url.username != "swapp_lab_scorer"
        ):
            raise ValueError("Scorer deployment differs")
        engine = create_engine(
            url,
            hide_parameters=True,
            connect_args={
                "connect_timeout": 5,
                "options": "-c default_transaction_read_only=on -c statement_timeout=5000",
            },
        )
        try:
            suite = json.loads(registry.verify_entry(entry)[0].read_bytes())
            with engine.connect() as connection:
                row = (
                    connection.execute(
                        text(
                            "SELECT manifest_sha256,development_manifest_sha256,task_count "
                            "FROM scorer.holdout_suite_versions WHERE suite_id=:suite "
                            "AND suite_version=:version"
                        ),
                        {"suite": entry.suite_id, "version": suite["suite_version"]},
                    )
                    .mappings()
                    .one()
                )
            if (
                row["manifest_sha256"] != document["holdout_manifest_sha256"]
                or row["development_manifest_sha256"] != entry.suite_manifest_sha256
                or row["task_count"] != document["holdout_task_count"]
            ):
                raise ValueError("actual installed holdout binding differs")
        finally:
            engine.dispose()
    return document


def assert_activation_quiescent() -> None:
    """Require absent owned services and read-only proof; never perform recovery.

    Live activation is unsupported. Users must finish/stop their finite work and
    explicitly stop their owned services first. Legacy rows without owner evidence
    stay unresolved and unchanged; they cannot serve as resource cleanup proof.
    """
    from ops.start_lab import SERVICES, check_unit

    for name, _, _, _, _, port, args in SERVICES:
        state = check_unit(f"swapp-ai-scientist-{name}.service", args[0], port)
        if (
            state.get("ActiveState") not in {"inactive", "failed"}
            or state.get("MainPID", "0") != "0"
        ):
            raise ValueError(
                "activation requires all owned services absent; live activation unsupported"
            )
    # Standalone Director/Scorer jobs need not belong to one of the three services.
    for process in Path("/proc").iterdir():
        if not process.name.isdecimal() or int(process.name) == os.getpid():
            continue
        try:
            if process.stat().st_uid != os.getuid() or process.joinpath("cwd").resolve() != ROOT:
                continue
            argv = process.joinpath("cmdline").read_bytes().split(b"\0")
        except FileNotFoundError:
            continue
        if (
            any(value.startswith((b"lab.", b"console.")) for value in argv)
            or str(ROOT / ".venv/bin/lab").encode() in argv
        ):
            raise ValueError("standalone owned Lab process still active; activation denied")
    from lab.sandbox.docker_runner import OWNED_CONTAINER_LABEL

    containers = subprocess.run(
        ["docker", "ps", "--filter", f"label={OWNED_CONTAINER_LABEL}", "--format", "{{.ID}}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    if containers.stdout.strip():
        raise ValueError("owned sandbox process still active; activation denied")
    from ops.install_synthetic_research import engine_for

    engine = engine_for(
        RUNTIME / "postgres/migrator.dsn",
        role="swapp_lab_migrator",
        database="swapp_lab",
        readonly=True,
    )
    try:
        with engine.connect() as connection:
            assert_activation_ledger_quiescent(connection)
    finally:
        engine.dispose()
    database = Path.home() / ".local/state/swapp-gpu/arbiter.sqlite3"
    _private_database(database)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5) as connection:
        if connection.execute(
            "SELECT 1 FROM gpu_turn_requests "
            "WHERE owner='lab' AND state IN ('queued','active') LIMIT 1"
        ).fetchone():
            raise ValueError("Lab GPU request still inflight; activation denied")
        if connection.execute("SELECT 1 FROM gpu_turn_state WHERE active_owner='lab'").fetchone():
            raise ValueError("Lab GPU cleanup unresolved; activation denied")
        for (unit,) in connection.execute(
            "SELECT unit FROM gpu_runtime_bindings WHERE owner='lab'"
        ):
            validate_unit_name(unit)
            result = subprocess.run(
                ["systemctl", "--user", "show", unit, "--property=ActiveState,MainPID"],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
            state = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
            if (
                state.get("ActiveState") not in {"inactive", "failed"}
                or state.get("MainPID") != "0"
            ):
                raise ValueError("owned GPU runtime cleanup unproven; activation denied")


def assert_activation_ledger_quiescent(connection) -> None:
    """Read-only current execution evidence; legacy ownerless rows are untouched."""
    from sqlalchemy import text

    for query, reason in (
        (
            "SELECT EXISTS(SELECT 1 FROM lab.runs r WHERE r.state='queued' OR "
            "(r.state IN ('running','stop_requested') AND "
            "(EXISTS(SELECT 1 FROM lab.director_run_owners o WHERE o.run_id=r.run_id) OR "
            "EXISTS(SELECT 1 FROM lab.director_owner_generations g WHERE g.run_id=r.run_id))))",
            "current nonterminal run",
        ),
        (
            "SELECT EXISTS(SELECT 1 FROM scorer.score_jobs WHERE state IN ('queued','running'))",
            "Scorer inflight job",
        ),
        (
            "SELECT EXISTS(SELECT 1 FROM scorer.holdout_reservations "
            "WHERE state IN ('reserved','running'))",
            "holdout inflight job",
        ),
        (
            "SELECT EXISTS(SELECT 1 FROM scorer.care_baseline_calibration_claims "
            "WHERE state IN ('reserved','running'))",
            "calibration inflight job",
        ),
        (
            "SELECT EXISTS(SELECT 1 FROM lab.director_stop_closures WHERE state='pending')",
            "unresolved stop cleanup",
        ),
        (
            "SELECT EXISTS(SELECT 1 FROM lab.director_restart_requests "
            "WHERE state IN ('pending','drained'))",
            "unresolved restart cleanup",
        ),
        (
            "SELECT EXISTS(SELECT 1 FROM lab.director_recoveries "
            "WHERE state IN ('started','pending'))",
            "unresolved recovery cleanup",
        ),
    ):
        if connection.execute(text(query)).scalar_one():
            raise ValueError(f"{reason}; activation denied")


def _private_database(path: Path) -> None:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise ValueError("GPU database must be private and owned")


def activate_pending_configuration() -> None:
    """Publish only after absent-service and cleanup proof under start-lab.lock."""
    from ops.install_synthetic_research import atomic_json

    document = installed_configuration(verify_database=True, pending=True)
    if document is None:
        raise ValueError("pending research setup is absent")
    worker = private_file(CONFIG / "research-worker.pending.env")
    assert_activation_quiescent()
    # Revalidate after the proof; changed pending input fails closed.
    if (
        installed_configuration(verify_database=True, pending=True) != document
        or private_file(CONFIG / "research-worker.pending.env") != worker
    ):
        raise ValueError("pending configuration changed during activation")
    # Keep the pending file retryable if the following receipt publication fails.
    from tempfile import mkstemp

    descriptor, temporary = mkstemp(prefix=".activate-worker-", dir=CONFIG)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(worker)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, CONFIG / "lab-worker.env")
    finally:
        Path(temporary).unlink(missing_ok=True)
    atomic_json(CONFIG / "research.json", document)


def assert_service_configuration(document: dict) -> None:
    """Inspect only named owned units; never stop/restart a process."""
    from ops.start_lab import SERVICES, check_unit

    for name, _, _, _, _, port, args in SERVICES:
        check_unit(f"swapp-ai-scientist-{name}.service", args[0], port)
    expected = {
        "swapp-ai-scientist-api.service": read_environment(CONFIG / "lab-api.env"),
        "swapp-ai-scientist-director-drain.service": document["worker_environment"],
        "swapp-ai-scientist-console.service": {
            "LAB_CONSOLE_SUITE_REGISTRY_FILE": document["registry_file"],
            "LAB_CONSOLE_TOKEN_FILE": document["principals_file"],
            "MODEL_RUNS_ENABLED": "true",
        },
    }
    for unit, environment in expected.items():
        result = subprocess.run(
            ["systemctl", "--user", "show", unit, "--property=MainPID,ActiveState"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        status = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if status.get("ActiveState") in {"inactive", "failed"}:
            continue
        if status.get("ActiveState") != "active":
            raise ValueError("owned service generation is not stable")
        pid = int(status["MainPID"])
        actual = dict(
            item.split("=", 1)
            for item in Path(f"/proc/{pid}/environ").read_bytes().decode().split("\x00")
            if "=" in item
        )
        if any(actual.get(key) != value for key, value in environment.items()):
            raise ValueError(
                f"{unit}: configuration drift; explicitly restart this owned service "
                "after all active runs and cleanup finish"
            )
        # Registry is loaded at startup; a later atomic merge requires restart.
        stat_fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        boot_epoch = time.time() - time.clock_gettime(time.CLOCK_BOOTTIME)
        started = int(
            (boot_epoch + int(stat_fields[19]) / os.sysconf("SC_CLK_TCK")) * 1_000_000_000
        )
        if Path(document["registry_file"]).stat().st_mtime_ns > started:
            raise ValueError(
                f"{unit}: registry changed after startup; explicit owned-service restart required"
            )
