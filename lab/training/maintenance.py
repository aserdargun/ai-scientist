"""Durable receipts and bounded dispatch for the M0 training maintenance profile.

The public CLI never trains in its own process. It dispatches a fixed worker in
an owned transient systemd unit; that worker must authenticate as the existing
Lab lane in the shared AOS/Lab GPU scheduler before starting any GPU child.
"""

# pylint: disable=missing-function-docstring,too-many-arguments,too-many-locals,import-outside-toplevel,broad-exception-caught,too-many-instance-attributes

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess  # nosec B404 -- fixed systemd-run binary and argv
import sys
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from lab.training.runtime_paths import validated_gpu_runtime_database

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TRAIN_RUNTIME = PROJECT_ROOT / "data/runtime/train"
TRAIN_PYTHON = TRAIN_RUNTIME / ".venv/bin/python"
TRAIN_STATE_ROOT = TRAIN_RUNTIME / "maintenance"
GPU_RUNTIME = PROJECT_ROOT / "data/runtime/gpu"
NATIVE_DOCTOR_RUNTIME = PROJECT_ROOT / "data/runtime/native-doctor"
TRAINING_LEDGER = TRAIN_STATE_ROOT / "maintenance-ledger.sqlite3"
SYSTEMD_RUN = "/usr/bin/systemd-run"
SYSTEMCTL = "/usr/bin/systemctl"
GPU_SLICE = "swapp-gpu.slice"
TRAIN_SEQUENCE_LENGTH = 24_576
TRAIN_ADAPTER_RANK = 32
TRAIN_STEPS = 3
TRAIN_RUNTIME_SECONDS = 900
TRAIN_MEMORY_BYTES = 12 * 1024**3
TRAIN_PARENT_MEMORY_BYTES = 2 * 1024**3
TRAIN_CPU_PERCENT = 100
TRAIN_PARENT_CPU_PERCENT = 50
TRAIN_TASKS = 64
TRAIN_PARENT_TASKS = 32
MINIMUM_FREE_DISK_BYTES = 20 * 1024**3
HOST_RAM_RESERVE_BYTES = 6 * 1024**3
LOCAL_AGENT_VERSION = "director.v0.17.0"
TRAINING_UNIT = "swapp-lab-gpu-maintenance.service"
UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
Operation = Literal["train_noop", "train_dry_run"]
REQUIRED_TRAINING_PACKAGES = {
    "accelerate": "1.15.0",
    "bitsandbytes": "0.50.2",
    "peft": "0.21.0",
    "torch": "2.12.1+cu130",
    "transformers": "5.5.0",
    "trl": "0.24.0",
    "unsloth": "2026.9.11",
}
EXPECTED_TRAINING_LOCK_SHA256 = "cdf0390c761fa7170f62453cc03fabfba82a02b432f7f8090b3d1d3d118e8e0b"


class MaintenanceError(RuntimeError):
    """A maintenance request failed closed before a trusted completion receipt."""


@dataclass(frozen=True, slots=True)
class TrainingProfile:
    """Fixed M0.14 measurement profile; no caller-controlled training knobs."""

    model_repository: str = "Qwen/Qwen3.5-9B"
    sequence_length: int = TRAIN_SEQUENCE_LENGTH
    steps: int = TRAIN_STEPS
    adapter_rank: int = TRAIN_ADAPTER_RANK
    load_in_4bit: bool = True
    gradient_checkpointing: str = "unsloth"
    cpu_offload_embeddings: bool = True
    synthetic_data_only: bool = True
    save_adapter: bool = False

    def document(self) -> dict[str, object]:
        return {
            "adapter_rank": self.adapter_rank,
            "gradient_checkpointing": self.gradient_checkpointing,
            "load_in_4bit": self.load_in_4bit,
            "model_repository": self.model_repository,
            "cpu_offload_embeddings": self.cpu_offload_embeddings,
            "save_adapter": self.save_adapter,
            "sequence_length": self.sequence_length,
            "steps": self.steps,
            "synthetic_data_only": self.synthetic_data_only,
        }


DEFAULT_TRAINING_PROFILE = TrainingProfile()


def _utc_now() -> str:
    return datetime.now(UTC).strftime(UTC_FORMAT)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def agent_version_identity() -> dict[str, str]:
    """Return the immutable local agent label plus its source digest."""
    source = PROJECT_ROOT / "lab/director/runner.py"
    if source.is_symlink() or not source.is_file():
        raise MaintenanceError("trusted Director agent source is unavailable")
    return {
        "agent_version": LOCAL_AGENT_VERSION,
        "agent_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }


class MaintenanceLedger:
    """Private SQLite operation/event ledger with immutable request identity."""

    def __init__(self, path: Path = TRAINING_LEDGER) -> None:
        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = path.parent
        if parent.is_symlink() or parent.stat().st_uid != os.getuid():
            raise MaintenanceError("training runtime directory is not private and owned")
        os.chmod(parent, 0o700)
        if path.exists() and (path.is_symlink() or not path.is_file()):
            raise MaintenanceError("training ledger path is not a regular file")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        os.chmod(self.path, 0o600)
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS training_maintenance (
                    operation_id TEXT PRIMARY KEY CHECK(length(operation_id)=32),
                    operation TEXT NOT NULL CHECK(operation IN ('train_noop','train_dry_run')),
                    request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
                    state TEXT NOT NULL CHECK(state IN (
                        'created','queued','active','training',
                        'drained','smoke','completed','failed')),
                    request_json TEXT NOT NULL,
                    receipt_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS training_maintenance_events (
                    event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL REFERENCES training_maintenance(operation_id),
                    event TEXT NOT NULL CHECK(event IN (
                        'created','queued','lease_acquired','noop_completed',
                        'training_started','training_unit_bound','training_finished',
                        'lease_drained','smoke_started','smoke_finished','completed','failed')),
                    observed_at TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS training_maintenance_events_by_operation
                    ON training_maintenance_events(operation_id,event_sequence);
                """
            )

    def create(self, operation_id: str, operation: Operation, request: dict[str, object]) -> str:
        if re.fullmatch(r"[0-9a-f]{32}", operation_id) is None:
            raise ValueError("operation ID must be 32 lowercase hexadecimal characters")
        encoded = _canonical_json(request).decode("utf-8")
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        now = _utc_now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT operation,request_sha256 FROM training_maintenance WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if row is not None:
                if row["operation"] != operation or row["request_sha256"] != digest:
                    connection.rollback()
                    raise MaintenanceError("maintenance idempotency key changed request terms")
                connection.commit()
                return digest
            connection.execute(
                "INSERT INTO training_maintenance(operation_id,operation,request_sha256,state,"
                "request_json,created_at,updated_at) VALUES(?,?,?,'created',?,?,?)",
                (operation_id, operation, digest, encoded, now, now),
            )
            self._event(connection, operation_id, "created", {"request_sha256": digest}, now)
            connection.commit()
        return digest

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        operation_id: str,
        event: str,
        details: dict[str, object],
        timestamp: str,
    ) -> None:
        connection.execute(
            "INSERT INTO training_maintenance_events(operation_id,event,observed_at,details_json) "
            "VALUES(?,?,?,?)",
            (operation_id, event, timestamp, _canonical_json(details).decode("utf-8")),
        )

    def advance(
        self,
        operation_id: str,
        *,
        expected: tuple[str, ...],
        state: str,
        event: str,
        details: dict[str, object],
    ) -> None:
        if state not in {"queued", "active", "training", "drained", "smoke", "completed", "failed"}:
            raise ValueError("invalid training maintenance state")
        now = _utc_now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM training_maintenance WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise MaintenanceError("training maintenance state transition was fenced")
            if row["state"] in {"completed", "failed"}:
                connection.rollback()
                raise MaintenanceError("training maintenance operation is terminal")
            if row["state"] not in expected:
                connection.rollback()
                raise MaintenanceError("training maintenance state transition was fenced")
            connection.execute(
                "UPDATE training_maintenance SET state=?,updated_at=? WHERE operation_id=?",
                (state, now, operation_id),
            )
            self._event(connection, operation_id, event, details, now)
            connection.commit()

    def append_event(self, operation_id: str, event: str, details: dict[str, object]) -> None:
        """Durably bind facts that must precede an external side effect."""
        now = _utc_now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM training_maintenance WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None or row["state"] in {"completed", "failed"}:
                connection.rollback()
                raise MaintenanceError("cannot append evidence to a terminal operation")
            self._event(connection, operation_id, event, details, now)
            connection.commit()

    def child_generation(self, operation_id: str) -> dict[str, object] | None:
        """Return the immutable first child binding, rejecting conflicting retries."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT details_json FROM training_maintenance_events "
                "WHERE operation_id=? AND event='training_unit_bound' ORDER BY event_sequence",
                (operation_id,),
            ).fetchall()
        if not rows:
            return None
        try:
            first = json.loads(rows[0]["details_json"])
            if any(json.loads(row["details_json"]) != first for row in rows[1:]):
                raise MaintenanceError("child unit generation changed within one operation")
        except (json.JSONDecodeError, TypeError) as exc:
            raise MaintenanceError("durable child unit binding is malformed") from exc
        if not isinstance(first, dict):
            raise MaintenanceError("durable child unit binding is malformed")
        return first

    def unfinished_children(self) -> list[tuple[str, str, dict[str, object] | None]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT operation_id,state FROM training_maintenance "
                "WHERE state IN ('training','active','queued') ORDER BY created_at"
            ).fetchall()
        result: list[tuple[str, str, dict[str, object] | None]] = []
        for row in rows:
            result.append(
                (
                    str(row["operation_id"]),
                    str(row["state"]),
                    self.child_generation(str(row["operation_id"])),
                )
            )
        return result

    def verify_request(
        self, operation_id: str, operation: Operation, request: dict[str, object]
    ) -> str:
        encoded = _canonical_json(request).decode("utf-8")
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT operation,state,request_sha256,request_json FROM training_maintenance "
                "WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        if (
            row is None
            or row["operation"] != operation
            or row["state"] != "created"
            or row["request_sha256"] != digest
            or row["request_json"] != encoded
        ):
            raise MaintenanceError("worker request differs from its immutable maintenance ledger")
        return digest

    def finish(self, operation_id: str, *, success: bool, receipt: dict[str, object]) -> None:
        now = _utc_now()
        target = "completed" if success else "failed"
        event = target
        encoded = _canonical_json(receipt).decode("utf-8")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM training_maintenance WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None or row["state"] in {"completed", "failed"}:
                connection.rollback()
                raise MaintenanceError("maintenance operation is missing or already terminal")
            connection.execute(
                "UPDATE training_maintenance SET state=?,receipt_json=?,updated_at=? "
                "WHERE operation_id=?",
                (target, encoded, now, operation_id),
            )
            self._event(connection, operation_id, event, receipt, now)
            connection.commit()

    def read_completed(self, operation_id: str, request_sha256: str) -> dict[str, object]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT state,request_sha256,receipt_json FROM training_maintenance "
                "WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        if (
            row is None
            or row["state"] != "completed"
            or row["request_sha256"] != request_sha256
            or not isinstance(row["receipt_json"], str)
        ):
            raise MaintenanceError("worker did not persist a completed trusted receipt")
        try:
            value = json.loads(row["receipt_json"])
        except json.JSONDecodeError as exc:
            raise MaintenanceError("worker completion receipt is malformed") from exc
        if not isinstance(value, dict) or value.get("operation_id") != operation_id:
            raise MaintenanceError("worker completion receipt identity is inconsistent")
        return value


def _private_runtime(path: Path) -> Path:
    if path.is_symlink():
        raise MaintenanceError("training runtime path cannot be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = path.resolve(strict=True)
    if path != TRAIN_RUNTIME.resolve() and TRAIN_RUNTIME.resolve() not in path.parents:
        raise MaintenanceError("training artifacts must stay under the private runtime root")
    if path.stat().st_uid != os.getuid() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise MaintenanceError("training runtime directory permissions are too broad")
    return path


def _prepare_native_doctor_runtime() -> Path:
    """Prepare the exact private output root used by the post-lease S1 smoke."""
    root = NATIVE_DOCTOR_RUNTIME
    if root.is_symlink():
        raise MaintenanceError("native diagnostic runtime cannot be a symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    logs = root / "logs"
    if logs.is_symlink():
        raise MaintenanceError("native diagnostic log root cannot be a symlink")
    logs.mkdir(mode=0o700, exist_ok=True)
    for directory in (root, logs):
        info = directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
        ):
            raise MaintenanceError("native diagnostic runtime identity is unsafe")
        os.chmod(directory, 0o700)
    return root


def _unit_name(operation_id: str) -> str:
    if re.fullmatch(r"[0-9a-f]{32}", operation_id) is None:
        raise ValueError("operation ID is invalid")
    return TRAINING_UNIT


def build_dispatch_argv(operation_id: str, operation: Operation) -> list[str]:
    """Construct the only accepted transient service launch for training work."""
    unit = _unit_name(operation_id)
    return [
        SYSTEMD_RUN,
        "--user",
        "--wait",
        "--collect",
        "--quiet",
        f"--unit={unit}",
        f"--slice={GPU_SLICE}",
        f"--property=Description=SWAPP Lab bounded {operation}",
        f"--property=MemoryMax={TRAIN_PARENT_MEMORY_BYTES}",
        "--property=MemorySwapMax=0",
        f"--property=CPUQuota={TRAIN_PARENT_CPU_PERCENT}%",
        f"--property=TasksMax={TRAIN_PARENT_TASKS}",
        f"--property=RuntimeMaxSec={TRAIN_RUNTIME_SECONDS}",
        "--property=KillMode=control-group",
        "--property=OOMPolicy=stop",
        "--property=NoNewPrivileges=yes",
        f"--working-directory={PROJECT_ROOT}",
        f"--setenv=PYTHONPATH={PROJECT_ROOT}",
        f"--setenv=SWAPP_TRAIN_OPERATION_ID={operation_id}",
        f"--setenv=SWAPP_TRAIN_OPERATION={operation}",
        f"--setenv=SWAPP_LAB_GPU_UNIT={unit}",
        "--setenv=HF_HUB_OFFLINE=1",
        "--setenv=TRANSFORMERS_OFFLINE=1",
        "--setenv=HF_HUB_DISABLE_TELEMETRY=1",
        "--setenv=WANDB_DISABLED=true",
        f"--setenv=HF_HOME={TRAIN_STATE_ROOT / (operation_id + '.hf')}",
        f"--setenv=TRITON_CACHE_DIR={TRAIN_STATE_ROOT / (operation_id + '.triton')}",
        f"--setenv=TORCHINDUCTOR_CACHE_DIR={TRAIN_STATE_ROOT / (operation_id + '.inductor')}",
        "--setenv=OPENBLAS_NUM_THREADS=1",
        "--setenv=OMP_NUM_THREADS=1",
        "--setenv=MKL_NUM_THREADS=1",
        "--setenv=NUMEXPR_NUM_THREADS=1",
        sys.executable,
        "-m",
        "lab.training.worker",
    ]


def installed_training_versions() -> dict[str, str]:
    """Read pinned distributions with stdlib metadata only (no torch/Unsloth import)."""
    try:
        interpreter = TRAIN_PYTHON.resolve(strict=True)
    except OSError as exc:
        raise MaintenanceError("pinned training virtualenv interpreter is unavailable") from exc
    uv_python_root = Path.home() / ".local/share/uv/python"
    if (
        not TRAIN_PYTHON.is_file()
        or not interpreter.is_file()
        or not interpreter.is_relative_to(uv_python_root)
        or not os.access(interpreter, os.X_OK)
    ):
        raise MaintenanceError("pinned training virtualenv interpreter is unavailable")
    script = (
        "import importlib.metadata,json; names="
        + repr(sorted(REQUIRED_TRAINING_PACKAGES))
        + "; print(json.dumps({n: importlib.metadata.version(n) for n in names},sort_keys=True))"
    )
    try:
        result = subprocess.run(  # nosec B603 -- pinned executable and fixed metadata-only script.
            [str(TRAIN_PYTHON), "-I", "-c", script],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env={"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MaintenanceError("pinned training metadata check failed") from exc
    if result.returncode != 0:
        raise MaintenanceError("pinned training package metadata is incomplete")
    try:
        values = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MaintenanceError("pinned training metadata response is malformed") from exc
    if not isinstance(values, dict) or not all(
        isinstance(name, str) and isinstance(version, str) for name, version in values.items()
    ):
        raise MaintenanceError("training environment metadata has an invalid shape")
    if values != REQUIRED_TRAINING_PACKAGES:
        raise MaintenanceError("training environment differs from its pinned package profile")
    return cast(dict[str, str], values)


def training_environment_identity(
    *, metadata_checker: Callable[[], dict[str, str]] = installed_training_versions
) -> dict[str, object]:
    lock_path = PROJECT_ROOT / "docs/ai-scientist/review-evidence/training-requirements.lock"
    if lock_path.is_symlink() or not lock_path.is_file():
        raise MaintenanceError("pinned training requirements lock is unavailable")
    lock_sha256 = hashlib.sha256(lock_path.read_bytes()).hexdigest()
    if lock_sha256 != EXPECTED_TRAINING_LOCK_SHA256:
        raise MaintenanceError("training requirements lock differs from the reviewed profile")
    return {"packages": metadata_checker(), "requirements_lock_sha256": lock_sha256}


def dispatch(
    operation: Operation,
    *,
    runner: Any = subprocess.run,
    metadata_checker: Callable[[], dict[str, str]] = installed_training_versions,
) -> dict[str, object]:
    """Write immutable intent, then ask systemd to start the trusted worker."""
    aos_unit = os.environ.get("SWAPP_AOS_GPU_UNIT", "")
    configured_lab_unit = os.environ.get("SWAPP_LAB_GPU_UNIT", "")
    database_text = os.environ.get("SWAPP_GPU_RUNTIME_DB", "")
    if (
        re.fullmatch(r"swapp-aos-gpu-[a-z0-9_.@-]+\.service", aos_unit) is None
        or configured_lab_unit != TRAINING_UNIT
        or not database_text
    ):
        raise MaintenanceError("trusted fixed maintenance principal configuration is unavailable")
    if operation not in {"train_noop", "train_dry_run"}:
        raise ValueError("unknown training operation")
    _private_runtime(TRAIN_STATE_ROOT)
    _prepare_native_doctor_runtime()
    environment_identity = (
        training_environment_identity(metadata_checker=metadata_checker)
        if operation == "train_dry_run"
        else {}
    )
    operation_id = uuid4().hex
    agent_identity = agent_version_identity()
    request: dict[str, object] = {
        "agent_version_before": agent_identity["agent_version"],
        "agent_source_sha256_before": agent_identity["agent_source_sha256"],
        "operation": operation,
        "profile": DEFAULT_TRAINING_PROFILE.document() if operation == "train_dry_run" else None,
        "scheduler_owner": "lab",
        "unit": _unit_name(operation_id),
        "training_environment": environment_identity,
    }
    ledger = MaintenanceLedger(TRAINING_LEDGER)
    request_sha256 = ledger.create(operation_id, operation, request)
    argv = build_dispatch_argv(operation_id, operation)
    database = Path(database_text).expanduser()
    try:
        resolved_database = validated_gpu_runtime_database(
            database, project_root=PROJECT_ROOT, gpu_runtime_root=GPU_RUNTIME
        )
    except ValueError as exc:
        raise MaintenanceError("shared GPU runtime database path is unsafe") from exc
    try:
        gpu_runtime_info = GPU_RUNTIME.lstat()
    except OSError as exc:
        raise MaintenanceError("private GPU runtime directory is unavailable") from exc
    if (
        GPU_RUNTIME.is_symlink()
        or not GPU_RUNTIME.is_dir()
        or gpu_runtime_info.st_uid != os.getuid()
        or gpu_runtime_info.st_mode & 0o077
    ):
        raise MaintenanceError("private GPU runtime directory is unavailable")
    python_index = argv.index(sys.executable)
    argv[python_index:python_index] = [
        f"--setenv=SWAPP_AOS_GPU_UNIT={aos_unit}",
        f"--setenv=SWAPP_LAB_GPU_UNIT={TRAINING_UNIT}",
        f"--setenv=SWAPP_GPU_RUNTIME_DB={resolved_database}",
        f"--setenv=LAB_TRAINING_LEDGER={TRAINING_LEDGER}",
    ]
    environment = {
        **os.environ,
        "SWAPP_TRAIN_OPERATION_ID": operation_id,
        "SWAPP_TRAIN_OPERATION": operation,
    }
    try:
        result = runner(
            argv,
            capture_output=True,
            text=True,
            timeout=TRAIN_RUNTIME_SECONDS + 30,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MaintenanceError("bounded training service could not be dispatched") from exc
    if result.returncode != 0:
        raise MaintenanceError(
            "bounded training service failed; inspect private maintenance ledger"
        )
    receipt = ledger.read_completed(operation_id, request_sha256)
    return {
        "status": "completed",
        "operation": operation,
        "operation_id": operation_id,
        "request_sha256": request_sha256,
        "unit": _unit_name(operation_id),
        "receipt": receipt,
    }


def doctor_train_main(argv: list[str] | None = None) -> int:
    """CLI entry point for the coordinated, few-step 24k synthetic measurement."""
    import argparse

    parser = argparse.ArgumentParser(prog="lab doctor train")
    parser.add_argument("--dry-run", action="store_true", required=True)
    args = parser.parse_args(argv)
    if args.dry_run is not True:
        parser.error("--dry-run is required")
    try:
        result = dispatch("train_dry_run")
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


def gpu_lease_main(argv: list[str] | None = None) -> int:
    """CLI entry point for `lab gpu lease TRAIN --noop`."""
    import argparse

    parser = argparse.ArgumentParser(prog="lab gpu lease")
    parser.add_argument("mode", choices=("TRAIN",))
    parser.add_argument("--noop", action="store_true", required=True)
    args = parser.parse_args(argv)
    if args.mode != "TRAIN" or args.noop is not True:
        parser.error("only the bounded TRAIN --noop maintenance profile is available")
    try:
        result = dispatch("train_noop")
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0
