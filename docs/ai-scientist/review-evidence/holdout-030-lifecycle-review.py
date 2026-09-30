#!/usr/bin/env python3
"""Stop one already-running, source-pinned holdout worker and verify its drain.

This is a manual review driver, not a test fixture or a worker launcher. It
refuses to act unless the operator names one real active reservation and opts
in with ``--execute-reviewed-stop``. It never creates runs, reservations,
workers, containers, databases, images, or credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess  # nosec B404 -- fixed read-only inspection commands only
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from lab.api.app import PROJECT_ROOT, create_app
from lab.api.registry import ApiPrincipal
from lab.sandbox.docker_runner import (
    DEFAULT_ADMISSION_LOCK,
    DEFAULT_SANDBOX_IMAGE,
    OWNED_CONTAINER_LABEL,
)
from lab.scorer.holdout import (
    _process_has_holdout_identity,
    _read_holdout_recovery_target,
    _worker_identity_from_target,
)
from lab.scorer.supervisor import (
    SCORER_SLICE,
    SYSTEMCTL,
    _cgroup_is_absent_or_empty,
    _cgroup_is_empty,
    _expected_unit_cgroup,
    _systemctl_show,
)
from lab.scorer.worker import DEFAULT_DSN_FILE

EXPECTED_HARNESS_VERSION = "0.30.0"
EXPECTED_SANDBOX_IMAGE = "sha256:2ad07bb30f69a9a97ed05acbc402ebafacd06566209d0e70819432c62540484e"
EXPECTED_DATABASE_PREFIX = "swapp_lab_m0_holdout_030_"
EXPECTED_ROLE_NAMES = {
    "migrator": "swapp_lab_migrator",
    "director": "swapp_lab_director",
    "planner": "swapp_lab_planner",
    "scorer": "swapp_lab_scorer",
}
SOURCE_FILES = (
    "harness/VERSION",
    "lab/api/app.py",
    "lab/director/holdout.py",
    "lab/director/loop.py",
    "lab/scorer/holdout.py",
    "lab/scorer/holdout_recovery.py",
    "lab/scorer/holdout_supervisor.py",
    "lab/scorer/holdout_worker.py",
    "lab/scorer/worker.py",
    "lab/scorer/supervisor.py",
    "lab/sandbox/docker_runner.py",
    "lab/sandbox/candidate_entrypoint.py",
    "lab/sandbox/sandbox_wrapper.py",
    "lab/db/migrations/versions/0019_bounded_holdout.py",
    "lab/db/migrations/versions/0020_holdout_recovery.py",
    "lab/db/migrations/versions/0021_run_end_unavailable.py",
)
MAX_CAPTURE_BYTES = 64 * 1024


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _source_fingerprint() -> tuple[str, str, dict[str, str]]:
    version_path = PROJECT_ROOT / "harness/VERSION"
    version = version_path.read_text(encoding="ascii").strip()
    lock_candidates = (
        PROJECT_ROOT / "lab/sandbox/image.lock",
        PROJECT_ROOT / "ops/sandbox-image.lock",
    )
    image_lock = next((path for path in lock_candidates if path.is_file()), None)
    if image_lock is None or image_lock.is_symlink():
        raise RuntimeError("trusted sandbox image lock is missing or unsafe")
    files = (*SOURCE_FILES, image_lock.relative_to(PROJECT_ROOT).as_posix())
    digests: dict[str, str] = {}
    for relative in files:
        path = PROJECT_ROOT / relative
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"required source file is missing or unsafe: {relative}")
        digests[relative] = _sha256(path.read_bytes())
    canonical = json.dumps(digests, sort_keys=True, separators=(",", ":")).encode("ascii")
    return (
        version,
        digests[image_lock.relative_to(PROJECT_ROOT).as_posix()],
        digests | {"__bundle_sha256__": _sha256(canonical)},
    )


def _secure_dsn_directory() -> Path:
    value = os.environ.get("LAB_HOLDOUT_TEST_DSN_DIR")
    if not value:
        raise RuntimeError("LAB_HOLDOUT_TEST_DSN_DIR is required")
    path = Path(value)
    if not path.is_absolute() or _contains_symlink(path):
        raise RuntimeError("DSN directory path must be absolute and contain no symlinks")
    resolved = path.resolve(strict=True)
    if resolved != path or not resolved.is_dir():
        raise RuntimeError("DSN directory must be a real directory")
    info = resolved.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError("DSN directory must be owned by this user and private")
    return resolved


def _contains_symlink(path: Path) -> bool:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def _create_engines(dsn_dir: Path) -> dict[str, Engine]:
    engines: dict[str, Engine] = {}
    for role in EXPECTED_ROLE_NAMES:
        path = dsn_dir / f"{role}.dsn"
        if _contains_symlink(path) or not path.is_file():
            raise RuntimeError(f"missing safe {role} DSN file")
        info = path.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise RuntimeError(f"{role} DSN file must be owned by this user and private")
        dsn = path.read_text(encoding="utf-8").strip()
        if not dsn.startswith("postgresql+psycopg://"):
            raise RuntimeError(f"{role} DSN must use PostgreSQL psycopg")
        engines[role] = create_engine(
            dsn,
            pool_size=1,
            max_overflow=0,
            pool_timeout=5,
            pool_pre_ping=True,
            hide_parameters=True,
            connect_args={"options": "-c statement_timeout=20000 -c lock_timeout=5000"},
        )
    return engines


def _verify_database_scope(engines: dict[str, Engine], expected_head: str) -> str:
    database: str | None = None
    server_identity: tuple[str, str, int, str] | None = None
    published_endpoint: tuple[str, int] | None = None
    for role, engine in engines.items():
        parsed_dsn = urlsplit(str(engine.url))
        try:
            dsn_address = ipaddress.ip_address(parsed_dsn.hostname or "")
            dsn_port = parsed_dsn.port
        except ValueError as exc:
            raise RuntimeError("role DSN endpoint must use a numeric loopback address") from exc
        if not dsn_address.is_loopback or not isinstance(dsn_port, int):
            raise RuntimeError("role DSN endpoint must use a numeric loopback address and port")
        endpoint = (str(dsn_address), dsn_port)
        if published_endpoint is not None and endpoint != published_endpoint:
            raise RuntimeError("role DSNs do not share one published loopback endpoint")
        published_endpoint = endpoint
        with engine.connect() as connection:
            current_database, session_user, address, port, postmaster = connection.execute(
                text(
                    "SELECT current_database(), session_user, host(inet_server_addr()), "
                    "inet_server_port(), pg_postmaster_start_time()::text"
                )
            ).one()
            if not str(current_database).startswith(EXPECTED_DATABASE_PREFIX):
                raise RuntimeError("database name is outside the disposable 030 prefix")
            if session_user != EXPECTED_ROLE_NAMES[role]:
                raise RuntimeError(f"{role} DSN is not bound to its expected database role")
            if database is not None and current_database != database:
                raise RuntimeError("role DSNs do not point at one disposable database")
            if not isinstance(address, str) or not isinstance(port, int):
                raise RuntimeError("database server endpoint address is malformed")
            try:
                ipaddress.ip_address(address)
            except ValueError as exc:
                raise RuntimeError("database server endpoint address is malformed") from exc
            identity = (str(current_database), address, port, str(postmaster))
            if server_identity is not None and identity != server_identity:
                raise RuntimeError(
                    "role DSNs do not point at the same PostgreSQL server generation"
                )
            database = str(current_database)
            server_identity = identity
    if database is None:
        raise RuntimeError("no verified database role connections were opened")
    with engines["migrator"].connect() as connection:
        heads = connection.execute(
            text("SELECT version_num FROM lab.alembic_version")
        ).scalars().all()
    if heads != [expected_head]:
        raise RuntimeError("database migration head does not match the reviewed source")
    return database


def _verify_worker_source(identity: dict[str, Any], reservation_id: UUID) -> dict[str, str]:
    pid = int(identity["worker_pid"])
    proc = Path("/proc") / str(pid)
    try:
        cwd = (proc / "cwd").resolve(strict=True)
        executable = (proc / "exe").resolve(strict=True)
        argv = (proc / "cmdline").read_bytes().split(b"\0")
    except OSError as exc:
        raise RuntimeError("recorded worker process source identity is unavailable") from exc
    expected_python = (PROJECT_ROOT / ".venv/bin/python").resolve(strict=True)
    decoded = [item.decode("utf-8", errors="strict") for item in argv if item]
    expected_tail = [
        "-m",
        "lab.scorer.holdout_worker",
        "--reservation-id",
        str(reservation_id),
    ]
    if (
        cwd != PROJECT_ROOT.resolve(strict=True)
        or executable != expected_python
        or decoded[:1] != [str(PROJECT_ROOT / ".venv/bin/python")]
        or decoded[1:5] != expected_tail
    ):
        raise RuntimeError(
            "live worker executable, cwd, or command is not bound to reviewed source"
        )
    return {
        "cwd": str(cwd),
        "executable": str(executable),
        "module": "lab.scorer.holdout_worker",
        "reservation_id": str(reservation_id),
    }


def _verify_snapshot_scorer_alias(dsn_dir: Path) -> str:
    alias = DEFAULT_DSN_FILE
    if _contains_symlink(alias) or not alias.is_file():
        raise RuntimeError("snapshot Scorer DSN alias is missing or contains a symlink")
    info = alias.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError("snapshot Scorer DSN alias must be private and owned by this user")
    role_dsn = (dsn_dir / "scorer.dsn").read_text(encoding="utf-8").strip()
    alias_dsn = alias.read_text(encoding="utf-8").strip()
    if not role_dsn or alias_dsn != role_dsn:
        raise RuntimeError("snapshot Scorer alias does not match the verified private Scorer DSN")
    return _sha256(alias_dsn.encode("utf-8"))


def _assert_expected_fingerprint(expected_source: str, expected_image_lock: str) -> dict[str, str]:
    version, lock_hash, details = _source_fingerprint()
    if version != EXPECTED_HARNESS_VERSION:
        raise RuntimeError(f"expected harness {EXPECTED_HARNESS_VERSION}; found {version}")
    if details["__bundle_sha256__"] != expected_source:
        raise RuntimeError("runtime source fingerprint differs from the reviewed value")
    if lock_hash != expected_image_lock:
        raise RuntimeError("sandbox image lock hash differs from the reviewed value")
    lock_path = next(
        path
        for path in (
            PROJECT_ROOT / "lab/sandbox/image.lock",
            PROJECT_ROOT / "ops/sandbox-image.lock",
        )
        if path.is_file()
    )
    locked_image = lock_path.read_text(encoding="ascii").strip()
    if (
        locked_image != DEFAULT_SANDBOX_IMAGE
        or locked_image != EXPECTED_SANDBOX_IMAGE
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", locked_image)
    ):
        raise RuntimeError("loaded sandbox image does not match a full trusted digest pin")
    return {
        "harness_version": version,
        "source_sha256": details["__bundle_sha256__"],
        "image_lock_sha256": lock_hash,
        "image": locked_image,
    }


def _run_readonly(argv: list[str], *, timeout: int = 5) -> str:
    result = subprocess.run(  # nosec B603 -- only fixed systemd/Docker inspection vectors
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if len(result.stdout) > MAX_CAPTURE_BYTES or len(result.stderr) > MAX_CAPTURE_BYTES:
        raise RuntimeError("bounded host inspection returned oversized output")
    if result.returncode != 0:
        raise RuntimeError("read-only host inspection failed")
    return result.stdout.strip()


def _systemd_resource_limits(unit: str) -> dict[str, str]:
    output = _run_readonly(
        [
            SYSTEMCTL,
            "--user",
            "show",
            "--no-pager",
            "--property=MemoryMax",
            "--property=MemorySwapMax",
            "--property=CPUQuotaPerSecUSec",
            "--property=TasksMax",
            unit,
        ]
    )
    values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    if values != {
        "MemoryMax": str(2 * 1024**3),
        "MemorySwapMax": "0",
        "CPUQuotaPerSecUSec": "1s",
        "TasksMax": "32",
    }:
        raise RuntimeError("worker systemd limits are not the reviewed bounded profile")
    return values


def _read_marker(identity: dict[str, Any], reservation_id: UUID, image: str) -> dict[str, Any]:
    marker = DEFAULT_ADMISSION_LOCK.with_suffix(".intent")
    if marker.is_symlink() or not marker.is_file():
        raise RuntimeError("no exact live sandbox ownership marker is present")
    info = marker.stat()
    if info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError("sandbox ownership marker permissions are unsafe")
    if info.st_size > 16 * 1024:
        raise RuntimeError("sandbox ownership marker exceeds its size bound")
    intent = json.loads(marker.read_text(encoding="ascii"))
    expected_work_root = PROJECT_ROOT / "data/runtime/holdout-work" / reservation_id.hex
    if expected_work_root.is_symlink() or not expected_work_root.is_dir():
        raise RuntimeError("reservation work root is missing or unsafe")
    expected = {
        "owner_label": OWNED_CONTAINER_LABEL,
        "owner_pid": identity["worker_pid"],
        "owner_start": str(identity["worker_start_ticks"]),
        "boot_id": identity["worker_boot_id"],
        "work_root": str(expected_work_root.resolve(strict=True)),
        "image": image,
    }
    if not isinstance(intent, dict) or any(
        intent.get(key) != value for key, value in expected.items()
    ):
        raise RuntimeError("sandbox marker does not belong to the exact reservation worker")
    label_value = intent.get("owner_label_value")
    container_name = intent.get("container_name")
    container_id = intent.get("container_id")
    if (
        not isinstance(label_value, str)
        or re.fullmatch(r"[0-9a-f]{32}", label_value) is None
        or not isinstance(container_name, str)
        or container_name.rsplit("-", 1)[-1] != label_value
        or not isinstance(container_id, str)
        or re.fullmatch(r"[0-9a-f]{64}", container_id) is None
    ):
        raise RuntimeError("sandbox marker has no exact container generation")
    return {
        "marker_path": str(marker),
        "container_id": container_id,
        "container_name": container_name,
        "owner_label_value": label_value,
    }


def _verify_live_container(marker: dict[str, str], image: str) -> dict[str, Any]:
    format_string = (
        "{{.Id}}|{{.Name}}|{{.Config.Image}}|{{.State.Running}}|{{.HostConfig.Memory}}|"
        "{{.HostConfig.NanoCpus}}|{{.HostConfig.PidsLimit}}|{{.HostConfig.NetworkMode}}"
    )
    output = _run_readonly(
        ["docker", "container", "inspect", "--format", format_string, marker["container_id"]]
    )
    fields = output.split("|")
    if len(fields) != 8:
        raise RuntimeError("container inspection did not return the bounded identity tuple")
    actual_id, name, actual_image, running, memory, nano_cpus, pids, network = fields
    if (
        actual_id != marker["container_id"]
        or name != "/" + marker["container_name"]
        or actual_image != image
        or running.lower() != "true"
        or memory != str(2 * 1024**3)
        or nano_cpus != "1000000000"
        or pids != "64"
        or network != "none"
    ):
        raise RuntimeError("live container identity or resource limits do not match the profile")
    label_filter = f"label={OWNED_CONTAINER_LABEL}={marker['owner_label_value']}"
    listed = _run_readonly(
        ["docker", "container", "ls", "--all", "--quiet", "--no-trunc", "--filter", label_filter]
    )
    if listed.splitlines() != [marker["container_id"]]:
        raise RuntimeError("container owner label does not identify exactly one container")
    return {
        "container_id": actual_id,
        "container_name": name,
        "image": actual_image,
        "running_before_stop": True,
        "memory_bytes": int(memory),
        "nano_cpus": int(nano_cpus),
        "pids_limit": int(pids),
        "network_mode": network,
        "owner_label_value": marker["owner_label_value"],
    }


def _snapshot(engine: Engine, run_id: UUID, reservation_id: UUID) -> dict[str, Any]:
    queries = {
        "experiments": "SELECT count(*) FROM lab.experiments WHERE run_id=:run_id",
        "score_jobs": "SELECT count(*) FROM scorer.score_jobs WHERE run_id=:run_id",
        "task_scores": "SELECT count(*) FROM scorer.task_scores WHERE run_id=:run_id",
        "task_completions": "SELECT count(*) FROM scorer.task_completions WHERE run_id=:run_id",
        "reservations": "SELECT count(*) FROM lab.holdout_reservations WHERE run_id=:run_id",
        "holdout_results": (
            "SELECT count(*) FROM scorer.holdout_results r "
            "JOIN lab.holdout_reservations h USING (reservation_id) WHERE h.run_id=:run_id"
        ),
        "approvals": "SELECT count(*) FROM lab.holdout_approvals WHERE run_id=:run_id",
    }
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT r.state,r.stop_requested,r.owner_id,r.origin,"
                    "h.state AS reservation_state,"
                    "h.result_bit,h.result_sha256,h.error_code,h.suite_id,h.suite_version "
                    "FROM lab.runs r JOIN lab.holdout_reservations h USING (run_id) "
                    "WHERE r.run_id=:run_id AND h.reservation_id=:reservation_id"
                ),
                {"run_id": run_id, "reservation_id": reservation_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise RuntimeError("target run/reservation pair is unavailable")
        data: dict[str, Any] = {
            "run_state": row["state"],
            "stop_requested": row["stop_requested"],
            "owner_id": row["owner_id"],
            "origin": row["origin"],
            "reservation_state": row["reservation_state"],
            "result_bit": row["result_bit"],
            "result_sha256": row["result_sha256"],
            "error_code": row["error_code"],
            "suite_id": row["suite_id"],
            "suite_version": row["suite_version"],
        }
        for key, query in queries.items():
            data[key] = int(connection.execute(text(query), {"run_id": run_id}).scalar_one())
        quota = (
            connection.execute(
                text(
                    "SELECT rq.used AS run_used,sq.used AS suite_used "
                    "FROM lab.holdout_run_quotas rq JOIN lab.holdout_suite_quotas sq "
                    "ON (sq.suite_id,sq.suite_version)=(rq.suite_id,rq.suite_version) "
                    "WHERE rq.run_id=:run_id"
                ),
                {"run_id": run_id},
            )
            .mappings()
            .one_or_none()
        )
        if quota is None:
            raise RuntimeError("target has no durable holdout quota rows")
        data["run_quota_used"] = int(quota["run_used"])
        data["suite_quota_used"] = int(quota["suite_used"])
    return data


def _receipt(engine: Engine, run_id: UUID, reservation_id: UUID) -> dict[str, Any]:
    with engine.connect() as connection:
        value = connection.execute(
            text("SELECT lab.read_holdout_bit(:run_id,:reservation_id)"),
            {"run_id": run_id, "reservation_id": reservation_id},
        ).scalar_one()
    if not isinstance(value, dict):
        raise RuntimeError("Director returned a malformed holdout bit receipt")
    if value.get("run_id") not in (run_id, str(run_id)) or value.get("reservation_id") not in (
        reservation_id,
        str(reservation_id),
    ):
        raise RuntimeError("Director receipt identity differs from the reviewed target")
    return value


def _assert_systemd_drained(unit: str, cgroup: str, invocation: str) -> dict[str, Any]:
    properties = _systemctl_show(unit)
    if properties.get("LoadState") == "not-found":
        drained = _cgroup_is_absent_or_empty(cgroup)
    elif properties.get("LoadState") == "loaded":
        drained = (
            properties.get("ActiveState") == "inactive"
            and properties.get("InvocationID", "").lower() == invocation
            and properties.get("MainPID") == "0"
            and properties.get("ControlGroup") == cgroup
            and _cgroup_is_empty(cgroup)
        )
    else:
        drained = False
    if not drained:
        raise RuntimeError("exact worker systemd generation is not drained")
    return {"load_state": properties.get("LoadState"), "cgroup": cgroup, "drained": True}


def _reserve_receipt(path_value: str) -> tuple[Path, str, tuple[int, int]]:
    path = Path(path_value)
    receipt_root = PROJECT_ROOT / "data/runtime/holdout-lifecycle-receipts"
    if _contains_symlink(receipt_root) or not receipt_root.is_dir():
        raise RuntimeError("private receipt directory is missing or unsafe")
    root_info = receipt_root.stat()
    if root_info.st_uid != os.getuid() or stat.S_IMODE(root_info.st_mode) != 0o700:
        raise RuntimeError("private receipt directory must be owned by this user with mode 0700")
    if not path.is_absolute() or _contains_symlink(path):
        raise RuntimeError("receipt path must be absolute and contain no symlinks")
    if path.parent.resolve(strict=True) != receipt_root.resolve(strict=True):
        raise RuntimeError("receipt path must be directly inside the private receipt directory")
    token = os.urandom(32).hex()
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    info = os.fstat(descriptor)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(token)
        handle.flush()
        os.fsync(handle.fileno())
    return path, token, (info.st_dev, info.st_ino)


def _write_receipt(
    reservation: tuple[Path, str, tuple[int, int]], payload: dict[str, Any]
) -> Path:
    path, token, inode = reservation
    current = path.lstat()
    if path.is_symlink() or (current.st_dev, current.st_ino) != inode:
        raise RuntimeError("reserved receipt destination changed before publication")
    if path.read_text(encoding="ascii") != token:
        raise RuntimeError("reserved receipt destination ownership token changed")
    encoded = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".lifecycle-receipt-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        current = path.lstat()
        if path.is_symlink() or (current.st_dev, current.st_ino) != inode:
            raise RuntimeError("reserved receipt destination changed before atomic publish")
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def _remove_reserved_receipt(reservation: tuple[Path, str, tuple[int, int]]) -> bool:
    path, token, inode = reservation
    try:
        current = path.lstat()
        if (
            path.is_symlink()
            or (current.st_dev, current.st_ino) != inode
            or path.read_text(encoding="ascii") != token
        ):
            return False
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def _run(args: argparse.Namespace, state: dict[str, bool]) -> dict[str, Any]:
    if shutil.disk_usage(PROJECT_ROOT).free < 20 * 1024**3:
        raise RuntimeError("minimum 20 GiB free-disk reserve is not available")
    env_expected = {
        "source": os.environ.get("LAB_HOLDOUT_LIFECYCLE_EXPECTED_SOURCE_SHA256", ""),
        "image_lock": os.environ.get("LAB_HOLDOUT_LIFECYCLE_EXPECTED_IMAGE_LOCK_SHA256", ""),
        "alembic_head": os.environ.get("LAB_HOLDOUT_LIFECYCLE_EXPECTED_ALEMBIC_HEAD", ""),
        "token": os.environ.get("LAB_HOLDOUT_LIFECYCLE_API_TOKEN", ""),
        "owner_id": os.environ.get("LAB_HOLDOUT_LIFECYCLE_OWNER_ID", ""),
    }
    if not all(env_expected.values()):
        raise RuntimeError(
            "reviewed source, image lock, migration head, and API principal are required"
        )
    for key in ("source", "image_lock"):
        if re.fullmatch(r"[0-9a-f]{64}", env_expected[key]) is None:
            raise RuntimeError(f"expected {key} digest is malformed")
    if not 32 <= len(env_expected["token"]) <= 512:
        raise RuntimeError("API token length is outside the accepted range")
    fingerprint = _assert_expected_fingerprint(env_expected["source"], env_expected["image_lock"])
    if DEFAULT_SANDBOX_IMAGE != fingerprint["image"]:
        raise RuntimeError("imported sandbox image pin does not match the source lock")
    image_id = _run_readonly(
        ["docker", "image", "inspect", "--format", "{{.Id}}", fingerprint["image"]]
    )
    if image_id != fingerprint["image"]:
        raise RuntimeError("source-pinned 0.30 sandbox image is not available by its exact digest")

    run_id = UUID(args.run_id)
    reservation_id = UUID(args.reservation_id)
    dsn_dir = _secure_dsn_directory()
    scorer_alias_sha256 = _verify_snapshot_scorer_alias(dsn_dir)
    engines = _create_engines(dsn_dir)
    try:
        database = _verify_database_scope(engines, env_expected["alembic_head"])
        with engines["migrator"].connect() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT owner_id,origin,state,stop_requested "
                        "FROM lab.runs WHERE run_id=:run_id"
                    ),
                    {"run_id": run_id},
                )
                .mappings()
                .one_or_none()
            )
        if (
            rows is None
            or rows["owner_id"] != env_expected["owner_id"]
            or rows["origin"] != "local"
        ):
            raise RuntimeError("target run is not owned by the configured local principal")

        target = _read_holdout_recovery_target(engines["scorer"], reservation_id)
        if target["run_id"] != run_id or target["state"] != "running":
            raise RuntimeError("target must be the exact running reservation")
        if target["run_state"] != "running" or target["stop_requested"]:
            raise RuntimeError("target run is not in an active, stoppable state")
        identity = _worker_identity_from_target(target, reservation_id)
        if not _process_has_holdout_identity(identity):
            raise RuntimeError("recorded worker PID/start/boot/cgroup identity is not live")
        process_source = _verify_worker_source(identity, reservation_id)
        worker_unit = identity["worker_unit"]
        worker_cgroup = identity["worker_cgroup"]
        properties = _systemctl_show(worker_unit)
        if (
            properties.get("LoadState") != "loaded"
            or properties.get("ActiveState") != "active"
            or properties.get("InvocationID", "").lower() != identity["worker_invocation_id"]
            or properties.get("MainPID") != str(identity["worker_pid"])
            or properties.get("ControlGroup") != worker_cgroup
            or _expected_unit_cgroup(worker_unit) != worker_cgroup
        ):
            raise RuntimeError("worker systemd generation differs from the immutable SQL identity")
        unit_limits = _systemd_resource_limits(worker_unit)
        slice_limits = _systemd_resource_limits(SCORER_SLICE)
        if not _cgroup_is_empty(worker_cgroup):
            # The live main worker is expected in the cgroup; require the cgroup
            # populated event and recorded PID, while rejecting unexpected peers.
            cgroup_path = Path("/sys/fs/cgroup") / worker_cgroup.lstrip("/")
            procs = (cgroup_path / "cgroup.procs").read_text(encoding="ascii").split()
            events = dict(
                line.split()
                for line in (cgroup_path / "cgroup.events").read_text(encoding="ascii").splitlines()
            )
            if (
                str(identity["worker_pid"]) not in procs
                or len(procs) > 32
                or any(not item.isdecimal() for item in procs)
                or events.get("populated") != "1"
            ):
                raise RuntimeError("worker cgroup contains an unexpected process set")

        marker = _read_marker(identity, reservation_id, fingerprint["image"])
        container_before = _verify_live_container(marker, fingerprint["image"])
        before = _snapshot(engines["migrator"], run_id, reservation_id)
        if (
            before["run_state"] != "running"
            or before["reservation_state"] != "running"
            or before["stop_requested"] is not False
            or before["result_bit"] is not None
            or before["result_sha256"] is not None
            or before["owner_id"] != env_expected["owner_id"]
            or before["origin"] != "local"
        ):
            raise RuntimeError("target has already stopped or has a terminal holdout result")
        with engines["scorer"].connect() as connection:
            open_targets = connection.execute(
                text("SELECT lab.list_holdout_recovery_targets(:run_id)"), {"run_id": run_id}
            ).scalar_one()
        if (
            not isinstance(open_targets, list)
            or len(open_targets) != 1
            or open_targets[0].get("reservation_id") not in (reservation_id, str(reservation_id))
            or open_targets[0].get("state") != "running"
        ):
            raise RuntimeError("run has other or ambiguous open holdout reservations")

        principal = ApiPrincipal(
            token=env_expected["token"], origin="local", owner_id=env_expected["owner_id"]
        )
        app = create_app(director_engine=engines["director"], principals=(principal,))
        recovery_calls: list[dict[str, Any]] = []
        from lab.scorer import holdout_supervisor as supervisor_module

        original_run_recovery = supervisor_module.run_holdout_run_recovery_process

        def record_run_recovery(target_run_id: UUID, *, remaining_seconds: int = 30) -> Any:
            try:
                result = original_run_recovery(target_run_id, remaining_seconds=remaining_seconds)
                recovery_calls.append(
                    {
                        "run_id": str(target_run_id),
                        "unit": result.unit,
                        "invocation_id": result.invocation_id,
                        "exit_code": result.exit_code,
                        "state": result.state,
                    }
                )
                return result
            except Exception as exc:
                recovery_calls.append({"error_type": type(exc).__name__})
                raise

        supervisor_module.run_holdout_run_recovery_process = record_run_recovery
        # Preserve the production API callback and instrument only the exact
        # helper result it receives from the bounded Scorer recovery service.
        try:
            with TestClient(app, raise_server_exceptions=True) as client:
                state["stop_attempted"] = True
                response = client.post(
                    f"/v1/runs/{run_id}/stop",
                    headers={"Authorization": f"Bearer {env_expected['token']}"},
                )
        finally:
            supervisor_module.run_holdout_run_recovery_process = original_run_recovery
        if response.status_code != 200 or response.json().get("state") != "stop_requested":
            raise RuntimeError("authenticated API stop did not return stop_requested")
        if (
            len(recovery_calls) != 1
            or recovery_calls[0].get("state") != "drained"
            or recovery_calls[0].get("exit_code") != 0
        ):
            raise RuntimeError("API background recovery did not return one drained Scorer receipt")
        recovery_unit = recovery_calls[0].get("unit")
        recovery_invocation = recovery_calls[0].get("invocation_id")
        if (
            not isinstance(recovery_unit, str)
            or recovery_unit == worker_unit
            or re.fullmatch(r"swapp-ai-scientist-scorer-[0-9a-f]{32}\.service", recovery_unit)
            is None
            or not isinstance(recovery_invocation, str)
            or re.fullmatch(r"[0-9a-f]{32}", recovery_invocation) is None
        ):
            raise RuntimeError("background recovery service identity is missing or reused")

        worker_drain = _assert_systemd_drained(
            worker_unit, worker_cgroup, str(identity["worker_invocation_id"])
        )
        if _process_has_holdout_identity(identity):
            raise RuntimeError("recorded worker PID/start/boot/cgroup identity remains live")
        marker_path = Path(marker["marker_path"])
        if marker_path.exists() or marker_path.is_symlink():
            raise RuntimeError("reservation sandbox ownership marker remains after recovery")
        containers_left = _run_readonly(
            [
                "docker",
                "container",
                "ls",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                f"label={OWNED_CONTAINER_LABEL}={marker['owner_label_value']}",
            ]
        )
        if containers_left:
            raise RuntimeError("exact reservation-owned container remains after recovery")

        after = _snapshot(engines["migrator"], run_id, reservation_id)
        final_target = _read_holdout_recovery_target(engines["scorer"], reservation_id)
        director_receipt = _receipt(engines["director"], run_id, reservation_id)
        if (
            final_target["state"] != "failed"
            or final_target["run_id"] != run_id
            or after["reservation_state"] != "failed"
            or after["result_bit"] is not None
            or after["result_sha256"] is not None
            or director_receipt.get("state") != "failed"
            or director_receipt.get("bit") is not None
        ):
            raise RuntimeError("recovery did not produce a bitless terminal failure")
        for key in (
            "experiments",
            "score_jobs",
            "task_scores",
            "task_completions",
            "reservations",
            "holdout_results",
            "approvals",
            "run_quota_used",
            "suite_quota_used",
        ):
            if after[key] != before[key]:
                raise RuntimeError(f"recovery changed protected ledger count/quota: {key}")
        return {
            "schema": "holdout-030-lifecycle-review.v1",
            "created_at": datetime.now(UTC).isoformat(),
            "database": database,
            "run_id": str(run_id),
            "reservation_id": str(reservation_id),
            "source": fingerprint,
            "database_endpoint": {
                "database": database,
                "roles_verified": sorted(EXPECTED_ROLE_NAMES),
                "server_identity_equal_across_roles": True,
                "snapshot_scorer_alias_sha256": scorer_alias_sha256,
            },
            "authenticated_api_stop": {
                "status_code": response.status_code,
                "response_state": response.json().get("state"),
                "principal_origin": "local",
                "principal_owner_id": env_expected["owner_id"],
            },
            "worker_generation": {
                "pid": identity["worker_pid"],
                "start_ticks": identity["worker_start_ticks"],
                "boot_id": identity["worker_boot_id"],
                "unit": worker_unit,
                "invocation_id": identity["worker_invocation_id"],
                "cgroup": worker_cgroup,
                "source_binding": process_source,
                "unit_limits": unit_limits,
                "aggregate_slice_limits": slice_limits,
                "container": container_before,
                "drain": worker_drain,
                "container_gone_after_stop": True,
            },
            "recovery_service": recovery_calls[0],
            "before": before,
            "after": after,
            "director_receipt": director_receipt,
            "acceptance_scope": {
                "real_worker_process_and_container_stopped": True,
                "terminal_bitless_failure": True,
                "quota_and_score_counts_unchanged": True,
                "drain_before_terminal_sql_cas": (
                    "not independently proven by final state; requires separate "
                    "journal/timestamp instrumentation"
                ),
                "does_not_claim_real_model_quality_or_successful_holdout_score": True,
            },
        }
    finally:
        for engine in engines.values():
            engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id")
    parser.add_argument("--reservation-id")
    parser.add_argument("--receipt")
    parser.add_argument("--expected-source-sha256")
    parser.add_argument("--expected-image-lock-sha256")
    parser.add_argument("--expected-alembic-head")
    parser.add_argument(
        "--print-source-fingerprint",
        action="store_true",
        help="print read-only source/image fingerprints without touching runtime state",
    )
    parser.add_argument(
        "--execute-reviewed-stop",
        action="store_true",
        help="explicitly authorize stopping the named active run and reservation",
    )
    args = parser.parse_args(argv)
    if args.print_source_fingerprint:
        try:
            version, lock_hash, details = _source_fingerprint()
            lock_path = next(
                path
                for path in (
                    PROJECT_ROOT / "lab/sandbox/image.lock",
                    PROJECT_ROOT / "ops/sandbox-image.lock",
                )
                if path.is_file()
            )
            print(
                json.dumps(
                    {
                        "harness_version": version,
                        "source_sha256": details["__bundle_sha256__"],
                        "image_lock_sha256": lock_hash,
                        "image": lock_path.read_text(encoding="ascii").strip(),
                    },
                    sort_keys=True,
                )
            )
            return 0
        except Exception as exc:
            print(f"source-fingerprint-failed: {type(exc).__name__}", file=sys.stderr)
            return 1
    required = (
        args.run_id,
        args.reservation_id,
        args.receipt,
        args.expected_source_sha256,
        args.expected_image_lock_sha256,
        args.expected_alembic_head,
    )
    if not all(required):
        parser.error(
            "all run, reservation, receipt, source, image-lock, and migration IDs are required"
        )
    if not args.execute_reviewed_stop:
        parser.error("refusing to act without --execute-reviewed-stop")
    try:
        receipt_reservation = _reserve_receipt(args.receipt)
    except Exception as exc:
        print(f"lifecycle-review-preflight-failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    state = {"stop_attempted": False}
    os.environ["LAB_HOLDOUT_LIFECYCLE_EXPECTED_SOURCE_SHA256"] = args.expected_source_sha256
    os.environ["LAB_HOLDOUT_LIFECYCLE_EXPECTED_IMAGE_LOCK_SHA256"] = args.expected_image_lock_sha256
    os.environ["LAB_HOLDOUT_LIFECYCLE_EXPECTED_ALEMBIC_HEAD"] = args.expected_alembic_head
    try:
        payload = _run(args, state)
        receipt = _write_receipt(receipt_reservation, payload)
    except Exception as exc:
        if state["stop_attempted"]:
            failure_payload = {
                "schema": "holdout-030-lifecycle-review-failure.v1",
                "created_at": datetime.now(UTC).isoformat(),
                "run_id": args.run_id,
                "reservation_id": args.reservation_id,
                "stop_attempted": True,
                "failure_type": type(exc).__name__,
                "drain_before_terminal_sql_cas": "not independently proven",
            }
            try:
                _write_receipt(receipt_reservation, failure_payload)
            except Exception as receipt_exc:
                print(
                    f"lifecycle-review-failed: {type(exc).__name__}; "
                    f"failure-artifact-publication-failed: {type(receipt_exc).__name__}",
                    file=sys.stderr,
                )
                return 1
        else:
            _remove_reserved_receipt(receipt_reservation)
        print(f"lifecycle-review-failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": "passed",
                "receipt": str(receipt),
                "run_id": payload["run_id"],
                "reservation_id": payload["reservation_id"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
