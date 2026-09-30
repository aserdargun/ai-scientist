"""Strict, idempotent task-plan writes using the isolated Planner role."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert

from lab.db.schema import (
    dataset_profiles,
    run_events,
    run_tasks,
    runs,
    score_jobs,
    task_terminal_outcomes,
)
from lab.db.task_plan import TASK_PLAN_FIELDS, canonical_task_plan_digest, run_plan_lock_key
from lab.director.ownership import (
    assert_execution_owner_closure_transaction,
    assert_execution_owner_transaction,
)


@dataclass(frozen=True, slots=True)
class TaskPlanSeal:
    """Immutable digest and size of every trusted task planned for a run."""

    sha256: str
    task_count: int


class RunTaskAssignment(BaseModel):
    """Trusted identity for one experiment/evaluation/task/seed assignment."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    experiment_id: str = Field(min_length=1, max_length=128)
    evaluation_kind: Literal["baseline", "primary", "confirmation"]
    task_id: str = Field(min_length=1, max_length=128)
    seed: int = Field(ge=0)
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_id: str = Field(min_length=1, max_length=128)
    split_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=256)


def plan_run_tasks(
    engine: Engine, *, run_id: UUID, assignments: tuple[RunTaskAssignment, ...]
) -> None:
    """Append trusted task assignments to an active, still-open run plan."""
    if not assignments or len(assignments) > 10_000:
        raise ValueError("task plan must contain 1..10000 assignments")
    identities = [
        (
            task.experiment_id,
            task.evaluation_kind,
            task.task_id,
            task.seed,
        )
        for task in assignments
    ]
    if len(identities) != len(set(identities)):
        raise ValueError("task plan contains duplicate task identities")
    with engine.begin() as connection:
        connection.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": run_plan_lock_key(run_id)},
        )
        assert_execution_owner_transaction(connection, run_id=run_id)
        run = connection.execute(
            select(runs.c.state, runs.c.task_plan_sha256)
            .where(runs.c.run_id == run_id)
            .with_for_update()
        ).one_or_none()
        if run is None or run.state != "running":
            raise ValueError("task plans can only be written for a running Lab run")
        if run.task_plan_sha256 is not None:
            raise ValueError("run task plan is sealed")
        for task in assignments:
            profile = connection.execute(
                select(dataset_profiles.c.dataset_id).where(
                    dataset_profiles.c.dataset_id == task.dataset_id,
                    dataset_profiles.c.split_id == task.split_id,
                    dataset_profiles.c.session_id == task.session_id,
                )
            ).scalar_one_or_none()
            if profile is None:
                raise ValueError("task plan references an unknown trusted dataset profile")
            values = {
                "run_id": run_id,
                "experiment_id": task.experiment_id,
                "evaluation_kind": task.evaluation_kind,
                "task_id": task.task_id,
                "seed": task.seed,
                "candidate_sha256": task.candidate_sha256,
                "dataset_id": task.dataset_id,
                "split_id": task.split_id,
                "session_id": task.session_id,
            }
            connection.execute(
                postgres_insert(run_tasks)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=[
                        run_tasks.c.run_id,
                        run_tasks.c.experiment_id,
                        run_tasks.c.evaluation_kind,
                        run_tasks.c.task_id,
                        run_tasks.c.seed,
                    ]
                )
            )
            existing = (
                connection.execute(
                    select(run_tasks).where(
                        run_tasks.c.run_id == run_id,
                        run_tasks.c.experiment_id == task.experiment_id,
                        run_tasks.c.evaluation_kind == task.evaluation_kind,
                        run_tasks.c.task_id == task.task_id,
                        run_tasks.c.seed == task.seed,
                    )
                )
                .mappings()
                .one()
            )
            if any(existing[key] != value for key, value in values.items()):
                raise ValueError("task identity is already bound to a different trusted plan")


def read_terminal_holdout_checkpoints(
    engine: Engine, *, run_id: UUID, lease: Any, artifact_root: Path
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Read exact immutable application/state bytes named by the terminal marker."""
    marker = lease.read_checkpoint(
        key="holdout-state-application:run_end:1", artifact_root=artifact_root
    )
    if not isinstance(marker, dict) or not isinstance(marker.get("payload"), dict):
        raise ValueError("terminal holdout marker is missing")
    payload = marker["payload"]
    found = []
    for field in ("application_sha256", "state_sha256"):
        with engine.connect() as connection:
            events = (
                connection.execute(
                    select(run_events.c.event_json).where(
                        run_events.c.run_id == run_id,
                        run_events.c.event_type == "director.checkpoint",
                    )
                )
                .scalars()
                .all()
            )
        matches = [event for event in events if event.get("payload_sha256") == payload.get(field)]
        if len(matches) != 1:
            raise ValueError("terminal marker does not name one durable checkpoint")
        checkpoint = lease.read_checkpoint(key=matches[0]["key"], artifact_root=artifact_root)
        if not isinstance(checkpoint, dict) or not isinstance(checkpoint.get("payload"), dict):
            raise ValueError("terminal checkpoint payload is unavailable")
        found.append(checkpoint["payload"])
    return found[0], found[1], payload


def terminal_finalization_seconds(engine: Engine, *, run_id: UUID) -> int:
    """Read the fixed cleanup window; retries never extend it or the research deadline."""
    with engine.connect() as connection:
        remaining = connection.execute(
            text(
                "SELECT floor(extract(epoch FROM deadline_at-clock_timestamp())) "
                "FROM lab.terminal_finalizations WHERE run_id=:run_id"
            ),
            {"run_id": run_id},
        ).scalar_one()
    return max(0, min(60, int(remaining)))


def seal_run_task_plan(
    engine: Engine,
    *,
    run_id: UUID,
    terminal_checkpoints: tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None = None,
) -> TaskPlanSeal:
    """Close the append-only plan under the same lock used by plan writers."""
    with engine.begin() as connection:
        connection.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": run_plan_lock_key(run_id)},
        )
        observed_state = connection.execute(
            select(runs.c.state).where(runs.c.run_id == run_id)
        ).scalar_one_or_none()
        if observed_state == "running" and terminal_checkpoints is None:
            assert_execution_owner_transaction(connection, run_id=run_id)
        elif observed_state == "running":
            assert_execution_owner_closure_transaction(connection, run_id=run_id)
        elif observed_state == "stop_requested":
            assert_execution_owner_closure_transaction(connection, run_id=run_id)
        run = connection.execute(
            select(runs.c.state, runs.c.task_plan_sha256, runs.c.task_plan_count)
            .where(runs.c.run_id == run_id)
            .with_for_update()
        ).one_or_none()
        if run is None or run.state not in {"running", "stop_requested"}:
            raise ValueError("only a running or stopped run can seal its task plan")
        task_rows = (
            connection.execute(
                select(run_tasks)
                .where(run_tasks.c.run_id == run_id)
                .order_by(
                    run_tasks.c.experiment_id,
                    run_tasks.c.evaluation_kind,
                    run_tasks.c.task_id,
                    run_tasks.c.seed,
                )
            )
            .mappings()
            .all()
        )
        if not task_rows and run.state != "stop_requested":
            raise ValueError("an active completed run cannot seal an empty task plan")
        digest = canonical_task_plan_digest(task_rows)
        count = len(task_rows)
        if terminal_checkpoints is not None:
            from lab.director.journal import canonical_bytes
            from lab.director.ownership import active_execution_owner

            owner = active_execution_owner()
            if owner is None:
                raise RuntimeError("terminal seal requires its captured owner")
            application, state, marker = terminal_checkpoints
            plan = [
                {field: row[field] for field in TASK_PLAN_FIELDS}
                for row in sorted(
                    task_rows,
                    key=lambda row: (
                        row["experiment_id"],
                        row["evaluation_kind"],
                        row["task_id"],
                        row["seed"],
                    ),
                )
            ]
            connection.execute(
                text(
                    "SELECT lab.prepare_terminal_finalization("
                    ":run_id,:generation,:invocation,:execution,:plan,:application,:state,:marker)"
                ),
                {
                    "run_id": run_id,
                    "generation": owner.generation,
                    "invocation": owner.invocation_id,
                    "execution": owner.execution_sha256,
                    "plan": json.dumps(
                        plan,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        allow_nan=False,
                    ),
                    "application": canonical_bytes(application).decode("utf-8"),
                    "state": canonical_bytes(state).decode("utf-8"),
                    "marker": canonical_bytes(marker).decode("utf-8"),
                },
            )
        if run.task_plan_sha256 is not None:
            if run.task_plan_sha256 != digest or run.task_plan_count != count:
                raise ValueError("sealed task plan no longer matches its trusted rows")
            return TaskPlanSeal(digest, count)
        changed = connection.execute(
            update(runs)
            .where(
                runs.c.run_id == run_id,
                runs.c.state == run.state,
                runs.c.task_plan_sha256.is_(None),
            )
            .values(task_plan_sha256=digest, task_plan_count=count)
        ).rowcount
        if changed != 1:
            raise ValueError("run state changed while sealing the task plan")
        # The DB trigger serializes direct INSERTs against the seal update. Rehash
        # after taking the row lock so a writer that raced before the seal is seen.
        current_rows = (
            connection.execute(
                select(run_tasks)
                .where(run_tasks.c.run_id == run_id)
                .order_by(
                    run_tasks.c.experiment_id,
                    run_tasks.c.evaluation_kind,
                    run_tasks.c.task_id,
                    run_tasks.c.seed,
                )
            )
            .mappings()
            .all()
        )
        if len(current_rows) != count or canonical_task_plan_digest(current_rows) != digest:
            raise ValueError("task rows changed while sealing the run plan")
    return TaskPlanSeal(digest, count)


def cancel_queued_score_job(engine: Engine, *, job_id: UUID) -> None:
    """Record a no-score stop outcome for one queued job in a stopped run."""
    with engine.begin() as connection:
        job = (
            connection.execute(select(score_jobs).where(score_jobs.c.job_id == job_id))
            .mappings()
            .one_or_none()
        )
        if job is None:
            raise ValueError("score job does not exist")
        if job["state"] == "cancelled":
            existing = connection.execute(
                select(
                    task_terminal_outcomes.c.candidate_sha256,
                    task_terminal_outcomes.c.outcome_code,
                    task_terminal_outcomes.c.score_job_id,
                    task_terminal_outcomes.c.worker_invocation_id,
                    task_terminal_outcomes.c.admitted_generation,
                    task_terminal_outcomes.c.execution_sha256,
                ).where(
                    task_terminal_outcomes.c.run_id == job["run_id"],
                    task_terminal_outcomes.c.experiment_id == job["experiment_id"],
                    task_terminal_outcomes.c.evaluation_kind == job["evaluation_kind"],
                    task_terminal_outcomes.c.task_id == job["task_id"],
                    task_terminal_outcomes.c.seed == job["seed"],
                )
            ).one_or_none()
            if (
                existing is not None
                and existing.outcome_code == "cancelled"
                and existing.candidate_sha256 == job["candidate_sha256"]
                and existing.score_job_id == job_id
                and existing.worker_invocation_id is None
                and existing.admitted_generation == job["admitted_generation"]
                and existing.execution_sha256 == job["execution_sha256"]
            ):
                return
            raise ValueError("cancelled score job has no matching immutable task outcome")
        receipt = connection.execute(
            text("SELECT lab.assert_planner_job_stop_execution(:job_id)"),
            {"job_id": job_id},
        ).scalar_one()
        if isinstance(receipt, str):
            receipt = json.loads(receipt)
        if not isinstance(receipt, dict):
            raise ValueError("Planner stop execution RPC returned an invalid receipt")
        receipt_generation = receipt.get("admitted_generation")
        receipt_hash = receipt.get("execution_sha256")
        if (
            receipt.get("run_id") != str(job["run_id"])
            or isinstance(receipt_generation, bool)
            or not isinstance(receipt_generation, int)
            or not isinstance(receipt_hash, str)
            or job["admitted_generation"] != receipt_generation
            or job["execution_sha256"] != receipt_hash
        ):
            raise ValueError("queued score job differs from its authorized stop generation")
        run = connection.execute(
            select(runs.c.state).where(runs.c.run_id == job["run_id"])
        ).scalar_one_or_none()
        locked = (
            connection.execute(
                select(score_jobs).where(score_jobs.c.job_id == job_id).with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if locked is None or locked["run_id"] != job["run_id"]:
            raise ValueError("score job disappeared or changed its run while being cancelled")
        if (
            locked["admitted_generation"] != receipt_generation
            or locked["execution_sha256"] != receipt_hash
        ):
            raise ValueError("queued score job changed its admitted generation")
        existing = connection.execute(
            select(
                task_terminal_outcomes.c.candidate_sha256,
                task_terminal_outcomes.c.outcome_code,
                task_terminal_outcomes.c.score_job_id,
                task_terminal_outcomes.c.worker_invocation_id,
            ).where(
                task_terminal_outcomes.c.run_id == locked["run_id"],
                task_terminal_outcomes.c.experiment_id == locked["experiment_id"],
                task_terminal_outcomes.c.evaluation_kind == locked["evaluation_kind"],
                task_terminal_outcomes.c.task_id == locked["task_id"],
                task_terminal_outcomes.c.seed == locked["seed"],
            )
        ).one_or_none()
        if locked["state"] == "cancelled":
            if (
                existing is not None
                and existing.outcome_code == "cancelled"
                and existing.candidate_sha256 == locked["candidate_sha256"]
                and existing.score_job_id == job_id
                and existing.worker_invocation_id is None
            ):
                return
            raise ValueError("cancelled score job has no matching immutable task outcome")
        if run != "stop_requested":
            raise ValueError("only a stop-requested run can cancel a queued score job")
        if locked["state"] != "queued":
            raise ValueError("only a queued score job can be cancelled by the Planner")
        assignment = connection.execute(
            select(run_tasks.c.candidate_sha256).where(
                run_tasks.c.run_id == locked["run_id"],
                run_tasks.c.experiment_id == locked["experiment_id"],
                run_tasks.c.evaluation_kind == locked["evaluation_kind"],
                run_tasks.c.task_id == locked["task_id"],
                run_tasks.c.seed == locked["seed"],
            )
        ).scalar_one_or_none()
        if assignment != locked["candidate_sha256"]:
            raise ValueError("queued score job no longer matches the trusted task plan")
        if existing is not None:
            raise ValueError("task already has a different terminal result")
        connection.execute(
            task_terminal_outcomes.insert().values(
                run_id=locked["run_id"],
                experiment_id=locked["experiment_id"],
                evaluation_kind=locked["evaluation_kind"],
                task_id=locked["task_id"],
                seed=locked["seed"],
                candidate_sha256=locked["candidate_sha256"],
                outcome_code="cancelled",
                producer_role="untrusted",
                score_job_id=job_id,
                claim_token=None,
                worker_invocation_id=None,
                recovery_invocation_id=None,
                admitted_generation=receipt_generation,
                execution_sha256=receipt_hash,
            )
        )


def record_baseline_cancelled_task(
    engine: Engine,
    *,
    run_id: UUID,
    experiment_id: str,
    task_id: str,
    seed: int,
    candidate_sha256: str,
) -> None:
    """Resolve one unclaimed baseline task under its captured stop generation."""
    if len(candidate_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in candidate_sha256):
        raise ValueError("candidate digest must be lowercase SHA-256")
    with engine.begin() as connection:
        owner = assert_execution_owner_closure_transaction(connection, run_id=run_id)
        run = connection.execute(
            select(runs.c.state, runs.c.request_json).where(runs.c.run_id == run_id)
        ).one_or_none()
        if (
            run is None
            or run.state != "stop_requested"
            or not isinstance(run.request_json, dict)
            or run.request_json.get("purpose") != "baseline"
        ):
            raise ValueError("baseline cancellation requires a stopped owned baseline run")
        assignment = connection.execute(
            select(run_tasks.c.candidate_sha256).where(
                run_tasks.c.run_id == run_id,
                run_tasks.c.experiment_id == experiment_id,
                run_tasks.c.evaluation_kind == "baseline",
                run_tasks.c.task_id == task_id,
                run_tasks.c.seed == seed,
            )
        ).scalar_one_or_none()
        if assignment != candidate_sha256:
            raise ValueError("cancelled baseline cell differs from its admitted assignment")
        existing = connection.execute(
            select(task_terminal_outcomes.c.outcome_code).where(
                task_terminal_outcomes.c.run_id == run_id,
                task_terminal_outcomes.c.experiment_id == experiment_id,
                task_terminal_outcomes.c.evaluation_kind == "baseline",
                task_terminal_outcomes.c.task_id == task_id,
                task_terminal_outcomes.c.seed == seed,
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing == "cancelled":
                return
            raise ValueError("baseline cell already has another terminal outcome")
        job_id = connection.execute(
            select(score_jobs.c.job_id).where(
                score_jobs.c.run_id == run_id,
                score_jobs.c.experiment_id == experiment_id,
                score_jobs.c.evaluation_kind == "baseline",
                score_jobs.c.task_id == task_id,
                score_jobs.c.seed == seed,
            )
        ).scalar_one_or_none()
        if job_id is not None:
            raise ValueError("baseline cancellation cannot bypass an existing score job")
        connection.execute(
            task_terminal_outcomes.insert().values(
                run_id=run_id,
                experiment_id=experiment_id,
                evaluation_kind="baseline",
                task_id=task_id,
                seed=seed,
                candidate_sha256=candidate_sha256,
                outcome_code="cancelled",
                producer_role="untrusted",
                score_job_id=None,
                claim_token=None,
                worker_invocation_id=None,
                recovery_invocation_id=None,
                admitted_generation=owner.generation,
                execution_sha256=owner.execution_sha256,
            )
        )


def record_planner_terminal_outcome(
    engine: Engine,
    *,
    run_id: UUID,
    experiment_id: str,
    evaluation_kind: Literal["baseline", "primary", "confirmation"],
    task_id: str,
    seed: int,
    candidate_sha256: str,
    outcome_code: Literal[
        "guard_rejected", "candidate_rejected", "candidate_crash", "candidate_timeout"
    ],
) -> None:
    """Record a pre-score outcome only when no score job has been enqueued."""
    if len(candidate_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in candidate_sha256):
        raise ValueError("candidate digest must be lowercase SHA-256")
    with engine.begin() as connection:
        connection.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": run_plan_lock_key(run_id)},
        )
        owner = assert_execution_owner_transaction(connection, run_id=run_id)
        run_state = connection.execute(
            select(runs.c.state).where(runs.c.run_id == run_id).with_for_update()
        ).scalar_one_or_none()
        if run_state != "running":
            raise ValueError("Planner terminal outcome requires an active run")
        assignment = connection.execute(
            select(run_tasks.c.candidate_sha256).where(
                run_tasks.c.run_id == run_id,
                run_tasks.c.experiment_id == experiment_id,
                run_tasks.c.evaluation_kind == evaluation_kind,
                run_tasks.c.task_id == task_id,
                run_tasks.c.seed == seed,
            )
        ).scalar_one_or_none()
        if assignment != candidate_sha256:
            raise ValueError("terminal outcome differs from the trusted task identity")
        existing = connection.execute(
            select(
                task_terminal_outcomes.c.candidate_sha256,
                task_terminal_outcomes.c.outcome_code,
                task_terminal_outcomes.c.producer_role,
                task_terminal_outcomes.c.score_job_id,
                task_terminal_outcomes.c.claim_token,
                task_terminal_outcomes.c.worker_invocation_id,
                task_terminal_outcomes.c.recovery_invocation_id,
            ).where(
                task_terminal_outcomes.c.run_id == run_id,
                task_terminal_outcomes.c.experiment_id == experiment_id,
                task_terminal_outcomes.c.evaluation_kind == evaluation_kind,
                task_terminal_outcomes.c.task_id == task_id,
                task_terminal_outcomes.c.seed == seed,
            )
        ).one_or_none()
        if existing is not None:
            if (
                existing.candidate_sha256 == candidate_sha256
                and existing.outcome_code == outcome_code
                and existing.producer_role == "swapp_lab_planner"
                and existing.score_job_id is None
                and existing.claim_token is None
                and existing.worker_invocation_id is None
                and existing.recovery_invocation_id is None
            ):
                return
            raise ValueError("task already has a different durable terminal outcome")
        job = connection.execute(
            select(score_jobs.c.job_id).where(
                score_jobs.c.run_id == run_id,
                score_jobs.c.experiment_id == experiment_id,
                score_jobs.c.evaluation_kind == evaluation_kind,
                score_jobs.c.task_id == task_id,
                score_jobs.c.seed == seed,
            )
        ).scalar_one_or_none()
        if job is not None:
            raise ValueError("Planner cannot resolve a task after a score job exists")
        connection.execute(
            task_terminal_outcomes.insert().values(
                run_id=run_id,
                experiment_id=experiment_id,
                evaluation_kind=evaluation_kind,
                task_id=task_id,
                seed=seed,
                candidate_sha256=candidate_sha256,
                outcome_code=outcome_code,
                producer_role="untrusted",
                score_job_id=None,
                claim_token=None,
                worker_invocation_id=None,
                recovery_invocation_id=None,
                admitted_generation=owner.generation,
                execution_sha256=owner.execution_sha256,
            )
        )
