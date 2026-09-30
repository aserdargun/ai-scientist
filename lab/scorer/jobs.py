"""Durable identity-only Scorer queue and content-addressed untrusted artifacts."""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import secrets
import shutil
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import Engine, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert

from lab.db.schema import run_tasks, runs, score_jobs
from lab.db.task_plan import run_plan_lock_key
from lab.director.ownership import assert_execution_owner_transaction
from lab.scorer.service import MAX_CANDIDATE_SCORE_BYTES, parse_candidate_score

DEFAULT_ARTIFACT_ROOT = Path("data/runtime/candidate-blobs")
SCORE_JOB_LEASE = timedelta(minutes=15)
MAX_SCORE_JOB_ATTEMPTS = 3
MIN_FREE_DISK_BYTES = 20 * 1024**3
MAX_ARTIFACT_STORE_BYTES = 8 * 1024**3
MAX_ORPHAN_TEMP_FILES = 256
MAX_BLOB_SCAN_ENTRIES = 100_000
_ORPHAN_TEMP_NAME = re.compile(r"\.([0-9a-f]{64})\.([0-9a-f]{16})\.tmp\Z")


def _acquire_blob_quota_lock(root: Path) -> tuple[int, Path]:
    if root.is_symlink():
        raise ValueError("artifact root cannot be a symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = root.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("artifact root must be private to the Lab service user")
    lock_path = root / ".quota.lock"
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    lock_info = os.fstat(descriptor)
    if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid():
        os.close(descriptor)
        raise ValueError("artifact quota lock file is unsafe")
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    return descriptor, lock_path


def _blob_store_usage(root: Path, lock_path: Path) -> int:
    total = 0
    for shard in root.iterdir():
        if shard == lock_path:
            continue
        if shard.is_symlink() or not shard.is_dir() or len(shard.name) != 2:
            raise ValueError("unexpected entry in content-addressed artifact store")
        for item in shard.iterdir():
            if item.is_symlink() or not item.is_file() or not item.name.endswith(".json"):
                raise ValueError("unexpected artifact entry in content-addressed store")
            info = item.stat()
            if (
                info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise ValueError("artifact blob ownership or mode is unsafe")
            total += info.st_size
    return total


def _recover_orphan_blob_temps(root: Path) -> int:
    """Remove only strict, private temp files left by a crashed locked writer."""
    recovered = 0
    recovered_bytes = 0
    scanned = 0
    uid = os.getuid()
    for shard in root.iterdir():
        scanned += 1
        if scanned > MAX_BLOB_SCAN_ENTRIES:
            raise ValueError("artifact recovery scan exceeded its bounded entry count")
        if shard.name == ".quota.lock":
            continue
        info = shard.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077:
            raise ValueError("artifact shard is not a private owned directory")
        if len(shard.name) != 2 or any(ch not in "0123456789abcdef" for ch in shard.name):
            raise ValueError("unexpected entry in content-addressed artifact store")
        for item in shard.iterdir():
            scanned += 1
            if scanned > MAX_BLOB_SCAN_ENTRIES:
                raise ValueError("artifact recovery scan exceeded its bounded entry count")
            match = _ORPHAN_TEMP_NAME.fullmatch(item.name)
            if match is None:
                continue
            digest = match.group(1)
            if digest[:2] != shard.name:
                raise ValueError("orphan artifact temp is in the wrong shard")
            info = item.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != uid
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > MAX_CANDIDATE_SCORE_BYTES
            ):
                raise ValueError("orphan artifact temp has unsafe ownership or type")
            recovered += 1
            recovered_bytes += info.st_size
            if recovered > MAX_ORPHAN_TEMP_FILES or recovered_bytes > MAX_ARTIFACT_STORE_BYTES:
                raise ValueError("orphan artifact temp recovery exceeds its bounded quota")
            item.unlink()
            directory_fd = os.open(shard, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    return recovered


@dataclass(frozen=True, slots=True)
class ClaimedScoreJob:
    """One DB-fenced job claim owned by one Scorer process."""

    job_id: UUID
    run_id: UUID
    experiment_id: str
    evaluation_kind: Literal["baseline", "primary", "confirmation"]
    task_id: str
    seed: int
    candidate_sha256: str
    artifact_sha256: str
    admitted_generation: int
    execution_sha256: str
    claim_token: str
    claim_unit: str
    claim_invocation_id: str
    attempt: int


def _blob_path(root: Path, digest: str, *, create: bool = True) -> Path:
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("artifact digest must be lowercase SHA-256")
    if root.is_symlink():
        raise ValueError("artifact root cannot be a symlink")
    if create:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    base = root.resolve(strict=True)
    shard = base / digest[:2]
    if create:
        shard.mkdir(mode=0o700, exist_ok=True)
    shard_info = shard.lstat()
    if (
        not stat.S_ISDIR(shard_info.st_mode)
        or shard_info.st_uid != os.getuid()
        or stat.S_IMODE(shard_info.st_mode) != 0o700
        or not shard.resolve(strict=True).is_relative_to(base)
    ):
        raise ValueError("artifact shard escaped the trusted blob root")
    return shard / f"{digest}.json"


def _assert_scorer_job_execution(
    connection: Any,
    *,
    job_id: UUID,
    claim_token: str | None = None,
    invocation_id: str | None = None,
    stop_closure: bool = False,
) -> tuple[UUID, int, str]:
    """Ask PostgreSQL to assert the exact admitted job tuple before child locks."""
    if (claim_token is None) != (invocation_id is None):
        raise ValueError("Scorer job assertion requires both claim fields or neither")
    function = (
        "lab.assert_scorer_job_stop_execution"
        if stop_closure
        else "lab.assert_scorer_job_execution"
    )
    receipt = connection.execute(
        text(
            f"SELECT {function}(:job_id,:claim_token,:invocation_id)"
        ),
        {
            "job_id": job_id,
            "claim_token": claim_token,
            "invocation_id": invocation_id,
        },
    ).scalar_one()
    if not isinstance(receipt, dict):
        raise RuntimeError("Scorer generation assertion returned no receipt")
    try:
        receipt_run_id = UUID(str(receipt["run_id"]))
        generation = receipt["admitted_generation"]
        execution_sha256 = receipt["execution_sha256"]
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Scorer generation assertion receipt is malformed") from exc
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
        or not isinstance(execution_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", execution_sha256) is None
    ):
        raise RuntimeError("Scorer generation assertion receipt is malformed")
    return receipt_run_id, generation, execution_sha256


def store_candidate_artifact(payload: bytes, *, artifact_root: Path = DEFAULT_ARTIFACT_ROOT) -> str:
    """Validate and atomically write an opaque candidate score document by digest."""
    if len(payload) > MAX_CANDIDATE_SCORE_BYTES:
        raise ValueError("candidate artifact exceeds the 32 MiB transport bound")
    parse_candidate_score(payload)
    return store_artifact_bytes(payload, artifact_root=artifact_root)


def store_artifact_bytes(
    payload: bytes,
    *,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
    max_bytes: int = MAX_CANDIDATE_SCORE_BYTES,
) -> str:
    """Atomically store bounded opaque bytes without changing the score parser."""
    if not payload or len(payload) > max_bytes or max_bytes > MAX_CANDIDATE_SCORE_BYTES:
        raise ValueError("artifact bytes exceed the shared 32 MiB bound")
    digest = hashlib.sha256(payload).hexdigest()
    lock_descriptor, lock_path = _acquire_blob_quota_lock(artifact_root)
    try:
        _recover_orphan_blob_temps(artifact_root)
        path = _blob_path(artifact_root, digest)
        if path.exists() or path.is_symlink():
            existing = read_artifact_bytes(digest, artifact_root=artifact_root, max_bytes=max_bytes)
            if existing != payload:
                raise ValueError("content-addressed artifact path contains different bytes")
            return digest
        free_bytes = shutil.disk_usage(artifact_root).free
        if free_bytes < MIN_FREE_DISK_BYTES + len(payload):
            raise RuntimeError("artifact write would breach the 20 GiB disk reserve")
        if _blob_store_usage(artifact_root, lock_path) + len(payload) > MAX_ARTIFACT_STORE_BYTES:
            raise RuntimeError("shared artifact store reached its 8 GiB quota")
        temporary = path.with_name(f".{digest}.{secrets.token_hex(8)}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
    finally:
        os.close(lock_descriptor)
    return digest


def read_candidate_artifact(digest: str, *, artifact_root: Path = DEFAULT_ARTIFACT_ROOT) -> bytes:
    """Open one verified regular blob without following its final symlink."""
    return read_artifact_bytes(
        digest,
        artifact_root=artifact_root,
        max_bytes=MAX_CANDIDATE_SCORE_BYTES,
    )


def read_artifact_bytes(
    digest: str,
    *,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
    max_bytes: int = MAX_CANDIDATE_SCORE_BYTES,
) -> bytes:
    """Read generic bounded bytes by digest; caller validates its own document type."""
    if not 0 < max_bytes <= MAX_CANDIDATE_SCORE_BYTES:
        raise ValueError("artifact read bound must be within the shared 32 MiB limit")
    path = _blob_path(artifact_root, digest, create=False)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or metadata.st_size > max_bytes
        ):
            raise ValueError("artifact is not a bounded regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read(max_bytes + 1)
    finally:
        os.close(descriptor)
    if len(payload) > max_bytes or hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("artifact content digest mismatch")
    return payload


def enqueue_score_job(
    engine: Engine,
    *,
    run_id: UUID,
    experiment_id: str,
    evaluation_kind: Literal["baseline", "primary", "confirmation"],
    task_id: str,
    seed: int,
    candidate_sha256: str,
    candidate_output: bytes,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
) -> UUID:
    """Persist bytes, verify trusted task identity, then enqueue an idempotent job."""
    if len(candidate_output) > MAX_CANDIDATE_SCORE_BYTES:
        raise ValueError("candidate artifact exceeds the 32 MiB transport bound")
    parse_candidate_score(candidate_output)
    artifact_sha256 = hashlib.sha256(candidate_output).hexdigest()
    job_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": run_plan_lock_key(run_id)},
        )
        owner = assert_execution_owner_transaction(connection, run_id=run_id)
        run = connection.execute(
            select(runs.c.state).where(runs.c.run_id == run_id).with_for_update()
        ).scalar_one_or_none()
        if run != "running":
            raise ValueError("only a running Lab job can accept a score submission")
        assignment = connection.execute(
            select(run_tasks.c.candidate_sha256).where(
                run_tasks.c.run_id == run_id,
                run_tasks.c.experiment_id == experiment_id,
                run_tasks.c.evaluation_kind == evaluation_kind,
                run_tasks.c.task_id == task_id,
                run_tasks.c.seed == seed,
            )
        ).scalar_one_or_none()
        if assignment is None or assignment != candidate_sha256:
            raise ValueError("submission does not match the trusted Director task plan")
        existing = (
            connection.execute(
                select(score_jobs).where(
                    score_jobs.c.run_id == run_id,
                    score_jobs.c.experiment_id == experiment_id,
                    score_jobs.c.evaluation_kind == evaluation_kind,
                    score_jobs.c.task_id == task_id,
                    score_jobs.c.seed == seed,
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            if (
                existing["candidate_sha256"] != candidate_sha256
                or existing["artifact_sha256"] != artifact_sha256
                or existing["admitted_generation"] != owner.generation
                or existing["execution_sha256"] != owner.execution_sha256
            ):
                raise ValueError("score task is bound to a different artifact or owner generation")
            return cast(UUID, existing["job_id"])
        stored_digest = store_candidate_artifact(candidate_output, artifact_root=artifact_root)
        if stored_digest != artifact_sha256:
            raise RuntimeError("candidate artifact digest changed before durable write")
        inserted = connection.execute(
            postgres_insert(score_jobs)
            .values(
                job_id=job_id,
                run_id=run_id,
                experiment_id=experiment_id,
                evaluation_kind=evaluation_kind,
                task_id=task_id,
                seed=seed,
                candidate_sha256=candidate_sha256,
                artifact_sha256=artifact_sha256,
                admitted_generation=owner.generation,
                execution_sha256=owner.execution_sha256,
                state="queued",
                attempt=0,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(
                index_elements=[
                    score_jobs.c.run_id,
                    score_jobs.c.experiment_id,
                    score_jobs.c.evaluation_kind,
                    score_jobs.c.task_id,
                    score_jobs.c.seed,
                ]
            )
            .returning(score_jobs.c.job_id)
        ).scalar_one_or_none()
        if inserted is not None:
            return cast(UUID, inserted)
        winner = connection.execute(
            select(
                score_jobs.c.job_id,
                score_jobs.c.candidate_sha256,
                score_jobs.c.artifact_sha256,
                score_jobs.c.admitted_generation,
                score_jobs.c.execution_sha256,
            ).where(
                score_jobs.c.run_id == run_id,
                score_jobs.c.experiment_id == experiment_id,
                score_jobs.c.evaluation_kind == evaluation_kind,
                score_jobs.c.task_id == task_id,
                score_jobs.c.seed == seed,
            )
        ).one_or_none()
        if winner is None:
            raise RuntimeError("score job insert conflicted without a matching task row")
        if winner.candidate_sha256 != candidate_sha256 or winner.artifact_sha256 != artifact_sha256:
            raise ValueError("score task already has a different durable artifact")
        if (
            winner.admitted_generation != owner.generation
            or winner.execution_sha256 != owner.execution_sha256
        ):
            raise ValueError("score task is bound to a different owner generation")
        return cast(UUID, winner.job_id)


def claim_score_job(
    engine: Engine,
    *,
    job_id: UUID,
    claim_unit: str,
    claim_invocation_id: str,
) -> ClaimedScoreJob | None:
    """Claim a ready job, bound to its verified transient systemd invocation."""
    expected_unit = f"swapp-ai-scientist-scorer-{job_id.hex}.service"
    if claim_unit != expected_unit or re.fullmatch(r"[0-9a-f]{32}", claim_invocation_id) is None:
        raise ValueError("score claim requires the exact owned systemd unit invocation")
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    ready = or_(
        score_jobs.c.state == "queued",
        (score_jobs.c.state == "running") & (score_jobs.c.lease_until < now),
    )
    with engine.begin() as connection:
        query = (
            select(score_jobs.c.job_id, score_jobs.c.run_id)
            .join(runs, runs.c.run_id == score_jobs.c.run_id)
            .where(ready, runs.c.state == "running")
        )
        query = query.where(score_jobs.c.job_id == job_id)
        candidates = connection.execute(query.order_by(score_jobs.c.created_at).limit(32)).all()
        row = None
        for candidate in candidates:
            receipt_run_id, admitted_generation, execution_sha256 = (
                _assert_scorer_job_execution(connection, job_id=candidate.job_id)
            )
            if receipt_run_id != candidate.run_id:
                raise RuntimeError("Scorer assertion receipt changed the job run identity")
            run_state = connection.execute(
                select(runs.c.state).where(runs.c.run_id == candidate.run_id).with_for_update()
            ).scalar_one_or_none()
            if run_state != "running":
                continue
            row = (
                connection.execute(
                    select(score_jobs)
                    .where(score_jobs.c.job_id == candidate.job_id, ready)
                    .with_for_update(skip_locked=True)
                )
                    .mappings()
                    .one_or_none()
            )
            if row is not None and (
                row["admitted_generation"] != admitted_generation
                or row["execution_sha256"] != execution_sha256
            ):
                raise RuntimeError("score job owner changed after its Scorer assertion")
            if row is not None:
                break
        if row is None:
            return None
        changed = connection.execute(
            update(score_jobs)
            .where(score_jobs.c.job_id == row["job_id"])
            .values(
                state="running",
                attempt=score_jobs.c.attempt + 1,
                claimed_by=token,
                lease_until=now + SCORE_JOB_LEASE,
                claim_unit=claim_unit,
                claim_invocation_id=claim_invocation_id,
                error_code=None,
                updated_at=now,
            )
        ).rowcount
        if changed != 1:
            raise RuntimeError("score job claim changed while locked")
        return ClaimedScoreJob(
            job_id=row["job_id"],
            run_id=row["run_id"],
            experiment_id=row["experiment_id"],
            evaluation_kind=row["evaluation_kind"],
            task_id=row["task_id"],
            seed=row["seed"],
            candidate_sha256=row["candidate_sha256"],
            artifact_sha256=row["artifact_sha256"],
            admitted_generation=row["admitted_generation"],
            execution_sha256=row["execution_sha256"],
            claim_token=token,
            claim_unit=claim_unit,
            claim_invocation_id=claim_invocation_id,
            attempt=row["attempt"] + 1,
        )
