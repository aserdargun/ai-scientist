"""Separate-process durable Scorer queue consumer."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import stat
import subprocess  # nosec B404 -- fixed systemctl vector for exact unit inspection
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from lab.scorer.jobs import ClaimedScoreJob

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DSN_FILE = PROJECT_ROOT / "data/runtime/postgres/scorer.dsn"
DEFAULT_ARTIFACT_ROOT = PROJECT_ROOT / "data/runtime/candidate-blobs"
SCORER_ADMISSION_KEY = int.from_bytes(b"SWAPPSCO", "big")
SCORER_RUNTIME_DIRECTORY = "swapp-ai-scientist"
SCORER_SLICE = "swapp-ai-scientist-scorer.slice"
SYSTEMCTL = "/usr/bin/systemctl"


@dataclass(frozen=True, slots=True)
class ScorerInvocation:
    """Host-verified identity of the transient unit running this process."""

    unit: str
    invocation_id: str
    control_group: str


def verify_systemd_invocation(expected_unit: str) -> ScorerInvocation:
    """Bind the caller PID to the active exact unit generation in systemd."""
    invocation_id = os.environ.get("INVOCATION_ID", "")
    if re.fullmatch(r"[0-9a-f]{32}", invocation_id) is None:
        raise RuntimeError("Scorer requires a systemd invocation identity")
    if (
        re.fullmatch(
            r"swapp-ai-scientist-(?:scorer|finalize|mode-install)-[0-9a-f]{32}\.service",
            expected_unit,
        )
        is None
    ):
        raise RuntimeError("Scorer unit name is invalid")
    result = subprocess.run(  # nosec B603 -- fixed absolute binary and validated unit ID
        [
            SYSTEMCTL,
            "--user",
            "show",
            "--no-pager",
            "--property=LoadState",
            "--property=ActiveState",
            "--property=InvocationID",
            "--property=ControlGroup",
            "--property=MainPID",
            expected_unit,
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("Scorer systemd unit inspection failed")
    properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    control_group = properties.get("ControlGroup", "")
    if (
        properties.get("LoadState") != "loaded"
        or properties.get("ActiveState") != "active"
        or properties.get("InvocationID") != invocation_id
        or properties.get("MainPID") != str(os.getpid())
        or not control_group.startswith("/")
        or ".." in Path(control_group).parts
    ):
        raise RuntimeError("Scorer process is not the main process of its exact active unit")
    slice_result = subprocess.run(  # nosec B603 -- fixed absolute binary and constant slice name
        [
            SYSTEMCTL,
            "--user",
            "show",
            "--no-pager",
            "--property=LoadState",
            "--property=ControlGroup",
            SCORER_SLICE,
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if slice_result.returncode != 0:
        raise RuntimeError("aggregate Scorer slice inspection failed")
    slice_properties = dict(
        line.split("=", 1) for line in slice_result.stdout.splitlines() if "=" in line
    )
    slice_cgroup = slice_properties.get("ControlGroup", "")
    if (
        slice_properties.get("LoadState") != "loaded"
        or not slice_cgroup.startswith("/")
        or ".." in Path(slice_cgroup).parts
        or control_group != f"{slice_cgroup.rstrip('/')}/{expected_unit}"
    ):
        raise RuntimeError("Scorer process escaped the aggregate Scorer slice")
    try:
        own_cgroup = next(
            parts[2]
            for line in Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines()
            if len(parts := line.split(":")) == 3 and parts[0] == "0" and parts[1] == ""
        )
        pids_path = Path("/sys/fs/cgroup") / control_group.lstrip("/") / "cgroup.procs"
        pids = pids_path.read_text(encoding="ascii").split()
    except (OSError, StopIteration) as exc:
        raise RuntimeError("Scorer cgroup membership is unavailable") from exc
    if own_cgroup != control_group or str(os.getpid()) not in pids:
        raise RuntimeError("Scorer PID is not in its exact systemd unit cgroup")
    return ScorerInvocation(expected_unit, invocation_id, control_group)


def _try_process_admission_lock() -> int | None:
    """Hold the host-wide Scorer slot for this process, independent of DB health."""
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    if runtime.is_symlink() or not runtime.is_dir():
        raise RuntimeError("user runtime directory is unavailable")
    runtime_info = runtime.stat()
    if runtime_info.st_uid != os.getuid() or stat.S_IMODE(runtime_info.st_mode) & 0o077:
        raise RuntimeError("user runtime directory ownership or mode is unsafe")
    directory = runtime / SCORER_RUNTIME_DIRECTORY
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise RuntimeError("Scorer runtime directory ownership or mode is unsafe")
    lock_path = directory / "scorer-p1.lock"
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    lock_info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(lock_info.st_mode)
        or lock_info.st_uid != os.getuid()
        or stat.S_IMODE(lock_info.st_mode) & 0o077
    ):
        os.close(descriptor)
        raise RuntimeError("Scorer admission lock file is unsafe")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    return descriptor


def _secret(path: Path) -> str:
    """Read a private service credential without including it in errors."""
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("scorer credential file is unavailable")
    if path.stat().st_mode & 0o077:
        raise RuntimeError("scorer credential file permissions must be 0600")
    value = path.read_text(encoding="utf-8").strip()
    if not value.startswith("postgresql+psycopg://"):
        raise RuntimeError("scorer credential must contain a psycopg PostgreSQL DSN")
    return value


def _retry_job(engine: Engine, job: ClaimedScoreJob) -> bool:
    """Requeue only a still-current Scorer claim after retryable infrastructure failure."""
    from sqlalchemy import update

    from lab.db.schema import score_jobs
    from lab.scorer.jobs import _assert_scorer_job_execution

    with engine.begin() as connection:
        receipt_run_id, generation, execution_sha256 = _assert_scorer_job_execution(
            connection,
            job_id=job.job_id,
            claim_token=job.claim_token,
            invocation_id=job.claim_invocation_id,
        )
        if (
            receipt_run_id != job.run_id
            or generation != job.admitted_generation
            or execution_sha256 != job.execution_sha256
        ):
            raise ValueError("Scorer retry no longer matches its admitted generation")
        changed = connection.execute(
            update(score_jobs)
            .where(
                score_jobs.c.job_id == job.job_id,
                score_jobs.c.state == "running",
                score_jobs.c.claimed_by == job.claim_token,
                score_jobs.c.claim_invocation_id == job.claim_invocation_id,
                score_jobs.c.lease_until > datetime.now(UTC),
                score_jobs.c.admitted_generation == job.admitted_generation,
                score_jobs.c.execution_sha256 == job.execution_sha256,
            )
            .values(
                state="queued",
                claimed_by=None,
                lease_until=None,
                claim_unit=None,
                claim_invocation_id=None,
                error_code="retryable_infrastructure",
            )
        ).rowcount
    return changed == 1


def process_one(
    engine: Engine,
    *,
    job_id: UUID,
    invocation: ScorerInvocation,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
) -> dict[str, str] | None:
    """Use one database advisory slot, then claim and score a durable job."""
    from sqlalchemy import text

    from harness.fingerprint import compute_harness_hash
    from lab.scorer.jobs import MAX_SCORE_JOB_ATTEMPTS, claim_score_job, read_candidate_artifact
    from lab.scorer.service import CandidateOutputError, IndependentScorer

    with engine.connect() as slot_connection:
        owns_slot = slot_connection.execute(
            text("SELECT pg_try_advisory_lock(:lock_key)"),
            {"lock_key": SCORER_ADMISSION_KEY},
        ).scalar_one()
        if not owns_slot:
            return {"state": "capacity_busy"}
        try:
            expected_unit = f"swapp-ai-scientist-scorer-{job_id.hex}.service"
            if invocation.unit != expected_unit:
                raise RuntimeError("verified systemd invocation does not own this score job")
            job = claim_score_job(
                engine,
                job_id=job_id,
                claim_unit=invocation.unit,
                claim_invocation_id=invocation.invocation_id,
            )
            if job is None:
                return None
            try:
                harness_sha256 = compute_harness_hash(PROJECT_ROOT).sha256
                scorer = IndependentScorer(engine, harness_sha256=harness_sha256)
                payload = read_candidate_artifact(job.artifact_sha256, artifact_root=artifact_root)
                scorer.score_task(
                    run_id=job.run_id,
                    experiment_id=job.experiment_id,
                    evaluation_kind=job.evaluation_kind,
                    task_id=job.task_id,
                    seed=job.seed,
                    candidate_sha256=job.candidate_sha256,
                    candidate_output=payload,
                    score_job_id=job.job_id,
                    claim_token=job.claim_token,
                    worker_invocation_id=invocation.invocation_id,
                    admitted_generation=job.admitted_generation,
                    execution_sha256=job.execution_sha256,
                )
                report = scorer.finalize_if_ready(
                    run_id=job.run_id,
                    admitted_generation=job.admitted_generation,
                    execution_sha256=job.execution_sha256,
                )
                return {
                    "job_id": str(job.job_id),
                    "state": "completed",
                    "worker_pid": str(os.getpid()),
                    "worker_unit": invocation.unit,
                    "worker_invocation_id": invocation.invocation_id,
                    "worker_claim_attempt": str(job.attempt),
                    "run_finalized": str(report is not None).lower(),
                    "research_status": report[0]["status"] if report is not None else "pending",
                }
            except CandidateOutputError:
                scorer.record_terminal_outcome(
                    run_id=job.run_id,
                    experiment_id=job.experiment_id,
                    evaluation_kind=job.evaluation_kind,
                    task_id=job.task_id,
                    seed=job.seed,
                    candidate_sha256=job.candidate_sha256,
                    outcome_code="candidate_rejected",
                    score_job_id=job.job_id,
                    claim_token=job.claim_token,
                    worker_invocation_id=invocation.invocation_id,
                    admitted_generation=job.admitted_generation,
                    execution_sha256=job.execution_sha256,
                )
                return {
                    "job_id": str(job.job_id),
                    "state": "terminal",
                    "outcome": "candidate_rejected",
                }
            except (OSError, ValueError):
                if job.attempt < MAX_SCORE_JOB_ATTEMPTS:
                    if not _retry_job(engine, job):
                        return {"job_id": str(job.job_id), "state": "claim_lost"}
                    return {
                        "job_id": str(job.job_id),
                        "state": "retrying",
                        "attempt": str(job.attempt),
                    }
                scorer.record_terminal_outcome(
                    run_id=job.run_id,
                    experiment_id=job.experiment_id,
                    evaluation_kind=job.evaluation_kind,
                    task_id=job.task_id,
                    seed=job.seed,
                    candidate_sha256=job.candidate_sha256,
                    outcome_code="scorer_error",
                    score_job_id=job.job_id,
                    claim_token=job.claim_token,
                    worker_invocation_id=invocation.invocation_id,
                    admitted_generation=job.admitted_generation,
                    execution_sha256=job.execution_sha256,
                )
                return {
                    "job_id": str(job.job_id),
                    "state": "terminal",
                    "outcome": "scorer_error",
                }
        finally:
            slot_connection.execute(
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": SCORER_ADMISSION_KEY},
            )


def process_finalize(
    engine: Engine,
    *,
    run_id: UUID,
    invocation: ScorerInvocation,
    admitted_generation: int | None,
    execution_sha256: str | None,
    empty_baseline_stop: bool = False,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
) -> dict[str, str]:
    """Run explicit post-seal finalization under both Scorer admission slots."""
    from sqlalchemy import text

    from harness.fingerprint import compute_harness_hash
    from lab.scorer.service import IndependentScorer

    has_pair = admitted_generation is not None and execution_sha256 is not None
    if (admitted_generation is None) != (execution_sha256 is None):
        raise ValueError("finalizer generation and execution hash must be supplied together")
    if has_pair == empty_baseline_stop:
        raise ValueError("finalizer requires either a captured pair or explicit empty-stop mode")

    with engine.connect() as slot_connection:
        if invocation.unit != f"swapp-ai-scientist-finalize-{run_id.hex}.service":
            raise RuntimeError("verified systemd invocation does not own this finalizer")
        owns_slot = slot_connection.execute(
            text("SELECT pg_try_advisory_lock(:lock_key)"),
            {"lock_key": SCORER_ADMISSION_KEY},
        ).scalar_one()
        if not owns_slot:
            return {"state": "capacity_busy"}
        try:
            scorer = IndependentScorer(
                engine,
                harness_sha256=compute_harness_hash(PROJECT_ROOT).sha256,
                artifact_root=artifact_root,
            )
            report = scorer.finalize_if_ready(
                run_id=run_id,
                admitted_generation=admitted_generation,
                execution_sha256=execution_sha256,
                empty_baseline_stop=empty_baseline_stop,
            )
            return {
                "state": "finalized" if report is not None else "not_ready",
                "run_id": str(run_id),
                "worker_pid": str(os.getpid()),
                "research_status": report[0]["status"] if report is not None else "pending",
                "report_sha256": report[1] if report is not None else "",
            }
        finally:
            slot_connection.execute(
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": SCORER_ADMISSION_KEY},
            )


def process_recover_stopped_job(
    engine: Engine,
    *,
    job_id: UUID,
    expected_claim_invocation_id: str,
    invocation: ScorerInvocation,
) -> dict[str, str]:
    """Cancel one stop-requested job after its prior systemd generation drained."""
    from sqlalchemy import select, text

    from harness.fingerprint import compute_harness_hash
    from lab.db.schema import reports, runs, score_jobs, task_terminal_outcomes
    from lab.scorer.service import IndependentScorer

    expected_unit = f"swapp-ai-scientist-scorer-{job_id.hex}.service"
    if (
        invocation.unit != expected_unit
        or invocation.invocation_id == expected_claim_invocation_id
        or re.fullmatch(r"[0-9a-f]{32}", expected_claim_invocation_id) is None
    ):
        raise RuntimeError("recovery requires a fresh verified invocation for the exact job unit")
    with engine.connect() as slot_connection:
        owns_slot = slot_connection.execute(
            text("SELECT pg_try_advisory_lock(:lock_key)"),
            {"lock_key": SCORER_ADMISSION_KEY},
        ).scalar_one()
        if not owns_slot:
            return {"state": "capacity_busy"}
        try:
            with engine.connect() as connection:
                row = (
                    connection.execute(
                        select(score_jobs, runs.c.state.label("run_state"))
                        .join(runs, runs.c.run_id == score_jobs.c.run_id)
                        .where(score_jobs.c.job_id == job_id)
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None:
                    return {"job_id": str(job_id), "state": "job_missing"}
                outcome = connection.execute(
                    select(
                        task_terminal_outcomes.c.candidate_sha256,
                        task_terminal_outcomes.c.outcome_code,
                        task_terminal_outcomes.c.score_job_id,
                        task_terminal_outcomes.c.worker_invocation_id,
                        task_terminal_outcomes.c.admitted_generation,
                        task_terminal_outcomes.c.execution_sha256,
                    ).where(
                        task_terminal_outcomes.c.run_id == row["run_id"],
                        task_terminal_outcomes.c.experiment_id == row["experiment_id"],
                        task_terminal_outcomes.c.evaluation_kind == row["evaluation_kind"],
                        task_terminal_outcomes.c.task_id == row["task_id"],
                        task_terminal_outcomes.c.seed == row["seed"],
                    )
                ).one_or_none()
            scorer = IndependentScorer(
                engine, harness_sha256=compute_harness_hash(PROJECT_ROOT).sha256
            )
            if row["state"] == "cancelled":
                if (
                    outcome is None
                    or outcome.outcome_code != "cancelled"
                    or outcome.candidate_sha256 != row["candidate_sha256"]
                    or outcome.score_job_id != job_id
                    or outcome.worker_invocation_id != expected_claim_invocation_id
                    or outcome.admitted_generation != row["admitted_generation"]
                    or outcome.execution_sha256 != row["execution_sha256"]
                ):
                    return {"job_id": str(job_id), "state": "claim_changed"}
                report = scorer.finalize_if_ready(
                    run_id=row["run_id"],
                    admitted_generation=row["admitted_generation"],
                    execution_sha256=row["execution_sha256"],
                )
                report_status = report[0]["status"] if report is not None else None
                if report_status is None:
                    with engine.connect() as connection:
                        stored_report = connection.execute(
                            select(reports.c.report_json).where(reports.c.run_id == row["run_id"])
                        ).scalar_one_or_none()
                    if isinstance(stored_report, dict):
                        report_status = stored_report.get("status")
                return {
                    "job_id": str(job_id),
                    "state": "already_cancelled",
                    "report_state": report_status or "pending",
                }
            if (
                row["state"] != "running"
                or row["run_state"] != "stop_requested"
                or row["claim_unit"] != expected_unit
                or row["claim_invocation_id"] != expected_claim_invocation_id
                or row["claimed_by"] is None
            ):
                return {"job_id": str(job_id), "state": "claim_changed"}
            scorer.record_terminal_outcome(
                run_id=row["run_id"],
                experiment_id=row["experiment_id"],
                evaluation_kind=row["evaluation_kind"],
                task_id=row["task_id"],
                seed=row["seed"],
                candidate_sha256=row["candidate_sha256"],
                outcome_code="cancelled",
                score_job_id=job_id,
                claim_token=row["claimed_by"],
                worker_invocation_id=expected_claim_invocation_id,
                recovery_invocation_id=invocation.invocation_id,
                admitted_generation=row["admitted_generation"],
                execution_sha256=row["execution_sha256"],
            )
            report = scorer.finalize_if_ready(
                run_id=row["run_id"],
                admitted_generation=row["admitted_generation"],
                execution_sha256=row["execution_sha256"],
            )
            return {
                "job_id": str(job_id),
                "state": "cancelled_after_drain",
                "report_state": report[0]["status"] if report else "pending",
            }
        finally:
            slot_connection.execute(
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": SCORER_ADMISSION_KEY},
            )


def main(argv: list[str] | None = None) -> int:
    """Consume at most one job; the owning Director supervises this process."""
    parser = argparse.ArgumentParser(prog="lab-scorer-worker")
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--job-id", type=UUID)
    operation.add_argument("--recover-job-id", type=UUID)
    operation.add_argument("--finalize-run-id", type=UUID)
    parser.add_argument("--finalize-admitted-generation", type=int)
    parser.add_argument("--finalize-execution-sha256")
    parser.add_argument("--finalize-empty-baseline-stop", action="store_true")
    parser.add_argument("--expected-claim-invocation-id")
    args = parser.parse_args(argv)
    if (args.recover_job_id is None) != (args.expected_claim_invocation_id is None):
        parser.error("--recover-job-id requires --expected-claim-invocation-id")
    has_generation = args.finalize_admitted_generation is not None
    has_execution_hash = args.finalize_execution_sha256 is not None
    if has_generation != has_execution_hash:
        parser.error("finalizer generation and execution hash must be supplied together")
    if args.finalize_run_id is not None:
        if args.finalize_empty_baseline_stop == has_generation:
            parser.error("finalizer requires a pair or explicit empty-baseline-stop mode")
        if has_generation and (
            args.finalize_admitted_generation < 1
            or len(args.finalize_execution_sha256) != 64
            or any(c not in "0123456789abcdef" for c in args.finalize_execution_sha256)
        ):
            parser.error("invalid finalizer execution pair")
    elif args.finalize_empty_baseline_stop or has_generation:
        parser.error("finalizer identity arguments require --finalize-run-id")
    expected_unit = (
        f"swapp-ai-scientist-finalize-{args.finalize_run_id.hex}.service"
        if args.finalize_run_id is not None
        else f"swapp-ai-scientist-scorer-{(args.job_id or args.recover_job_id).hex}.service"
    )
    dsn_file = Path(os.environ.get("LAB_SCORER_DSN_FILE", DEFAULT_DSN_FILE))
    artifact_root = Path(os.environ.get("LAB_ARTIFACT_ROOT", DEFAULT_ARTIFACT_ROOT))
    try:
        invocation = verify_systemd_invocation(expected_unit)
        admission_descriptor = _try_process_admission_lock()
    except Exception as exc:
        print(f"scorer admission failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    if admission_descriptor is None:
        print(json.dumps({"state": "capacity_busy"}, sort_keys=True))
        return 0
    engine: Engine | None = None
    try:
        # Heavy data/scoring libraries load only after the host-wide P=1 lock.
        from sqlalchemy import create_engine

        engine = create_engine(_secret(dsn_file), pool_size=2, max_overflow=0, pool_pre_ping=True)
        result: dict[str, str] | None
        if args.finalize_run_id is not None:
            result = process_finalize(
                engine,
                run_id=args.finalize_run_id,
                invocation=invocation,
                admitted_generation=args.finalize_admitted_generation,
                execution_sha256=args.finalize_execution_sha256,
                empty_baseline_stop=args.finalize_empty_baseline_stop,
                artifact_root=artifact_root,
            )
        elif args.recover_job_id is not None:
            result = process_recover_stopped_job(
                engine,
                job_id=args.recover_job_id,
                expected_claim_invocation_id=args.expected_claim_invocation_id,
                invocation=invocation,
            )
        else:
            result = process_one(
                engine,
                job_id=args.job_id,
                invocation=invocation,
                artifact_root=artifact_root,
            )
        print(json.dumps(result or {"state": "idle"}, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"scorer worker failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()
        os.close(admission_descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
