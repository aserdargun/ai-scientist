"""One seed of the trusted Docker → Planner → isolated Scorer path."""

from __future__ import annotations

import hashlib
import math
import re
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Engine, text

from harness.fingerprint import compute_harness_hash
from lab.director.budget import MAX_SEED_WALL_SECONDS
from lab.director.journal import DirectorRunLease
from lab.director.suite import SuiteTask
from lab.director.task_plan import cancel_queued_score_job
from lab.sandbox.docker_runner import DEFAULT_SANDBOX_IMAGE, PROJECT_ROOT, LocalDockerRunner
from lab.sandbox.evaluation import CandidateGuardReject, run_guarded_seed_evaluation
from lab.scorer.jobs import DEFAULT_ARTIFACT_ROOT, enqueue_score_job
from lab.scorer.supervisor import ScorerProcessResult, run_scorer_process


class CandidateExecutionRejected(RuntimeError):
    """Typed no-score result from a candidate-controlled guard or failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def verify_execution_identity(
    runner: LocalDockerRunner, *, harness_sha256: str, image_sha256: str
) -> None:
    """Reject a changed trusted harness/image before cache or sandbox use."""
    current_harness = compute_harness_hash(PROJECT_ROOT).sha256
    runner_digest = DEFAULT_SANDBOX_IMAGE.rsplit("@sha256:", 1)[-1].removeprefix("sha256:")
    if (
        current_harness != harness_sha256
        or runner.image != DEFAULT_SANDBOX_IMAGE
        or runner_digest != image_sha256
    ):
        raise CandidateExecutionRejected("harness_hash_mismatch")


def _scorer_process_provenance(
    engine: Engine,
    result: ScorerProcessResult,
    *,
    job_id: UUID,
    run_id: UUID,
    experiment_id: str,
    evaluation_kind: str,
    task_id: str,
    seed: int,
) -> dict[str, Any]:
    """Bind this actual waiter result to immutable completed job metadata only.

    The RPC exposes only committed identity, never exit status. Missing actual
    waiter metadata remains unknown; RPC identity cannot fill it retroactively.
    """
    unit = f"swapp-ai-scientist-scorer-{job_id.hex}.service"
    invocation = result.invocation_id
    attempt = getattr(result, "attempt", None)
    if (
        result.job_id != job_id
        or result.unit != unit
        or result.exit_code != 0
        or (invocation is not None and re.fullmatch(r"[0-9a-f]{32}", invocation) is None)
        or (attempt is not None and (type(attempt) is not int or attempt < 1))
    ):
        raise RuntimeError("Scorer process receipt identity differs from requested job")
    if invocation is None or attempt is None:
        return {"scorer_process_provenance": "unknown", "scorer_process_attempt": None}
    with engine.connect() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        row = connection.execute(
            text("SELECT lab.completed_score_job_process_receipt(:job)"),
            {"job": job_id},
        ).scalar_one()

    if (
        not isinstance(row, dict)
        or row.get("schema") != "completed-score-job-process-receipt.v1"
        or (
            row["job_id"],
            row["run_id"],
            row["experiment_id"],
            row["evaluation_kind"],
            row["task_id"],
            row["seed"],
        )
        != (str(job_id), str(run_id), experiment_id, evaluation_kind, task_id, seed)
        or type(row["attempt"]) is not int
        or row["attempt"] < 1
        or (attempt is not None and row["attempt"] != attempt)
    ):
        raise RuntimeError("Scorer process receipt differs from completed job identity")
    if (
        row["worker_unit"] != unit
        or not isinstance(row["worker_invocation_id"], str)
        or re.fullmatch(r"[0-9a-f]{32}", row["worker_invocation_id"]) is None
        or (invocation is not None and row["worker_invocation_id"] != invocation)
    ):
        raise RuntimeError("Scorer invocation differs from immutable completed job")
    return {"scorer_process_provenance": "actual_process", "scorer_process_attempt": attempt}


def evaluate_and_score_seed(
    director_engine: Engine,
    planner_engine: Engine,
    runner: LocalDockerRunner,
    *,
    run_id: UUID,
    experiment_id: str,
    evaluation_kind: Literal["baseline", "primary", "confirmation"],
    task: SuiteTask,
    candidate_source: bytes,
    candidate_sha256: str,
    seed: int,
    harness_sha256: str,
    image_sha256: str,
    remaining_seconds: int,
    lease: DirectorRunLease,
    trusted_baseline_name: str | None = None,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
) -> dict[str, Any]:
    """Run guards in real fresh Docker phases then independently score output.

    Candidate exceptions are surfaced as typed no-score rejections. Infrastructure
    exceptions are not converted to candidate failures and remain retryable by the
    Director's bounded infrastructure retry policy.
    """
    if not 1 <= remaining_seconds <= MAX_SEED_WALL_SECONDS:
        raise ValueError("seed execution deadline exceeds the 600-second cap")
    if hashlib.sha256(candidate_source).hexdigest() != candidate_sha256:
        raise ValueError("candidate source bytes differ from their preregistered digest")
    verify_execution_identity(runner, harness_sha256=harness_sha256, image_sha256=image_sha256)
    from lab.director.evaluation_recovery import recover_evaluation_measurement

    replayed = recover_evaluation_measurement(
        director_engine,
        lease,
        run_id=run_id,
        experiment_id=experiment_id,
        kind=evaluation_kind,
        task_id=task.task_id,
        seed=seed,
        artifact_root=artifact_root,
        expected={
            "candidate_sha256": candidate_sha256,
            "dataset_id": task.dataset_id,
            "split_id": task.split_id,
            "session_id": task.session_id,
            "profile_sha256": task.profile_sha256,
            "harness_sha256": harness_sha256,
            "task_family": task.family,
        },
    )
    if replayed is not None:
        return replayed
    train, evaluation = task.frames()
    context = replace(task.context, seed=seed, time_budget_s=float(remaining_seconds))
    deadline = time.monotonic() + remaining_seconds
    lease.require_run_active()
    try:
        guarded = run_guarded_seed_evaluation(
            runner,
            candidate_source=candidate_source,
            train=train,
            evaluation=evaluation,
            context=context,
            task_ids=task.task_ids_for_guard,
            evaluation_instants=frozenset(task.evaluation_instants),
            remaining_seconds=remaining_seconds,
            trusted_baseline_name=trusted_baseline_name,
            admission_check=lease.require_run_active,
        )
    except CandidateGuardReject as exc:
        raise CandidateExecutionRejected(exc.code) from exc
    score_artifact = guarded.evaluation.score_document
    scorer_remaining = int(deadline - time.monotonic())
    if scorer_remaining < 1:
        raise CandidateExecutionRejected("timeout")
    lease.require_run_active()
    from lab.director.evaluation_recovery import persist_evaluation_evidence

    persist_evaluation_evidence(
        director_engine,
        lease,
        artifact_root=artifact_root,
        score_artifact=score_artifact,
        payload={
            "run_id": str(run_id),
            "experiment_id": experiment_id,
            "evaluation_kind": evaluation_kind,
            "task_id": task.task_id,
            "seed": seed,
            "candidate_sha256": candidate_sha256,
            "dataset_id": task.dataset_id,
            "split_id": task.split_id,
            "session_id": task.session_id,
            "profile_sha256": task.profile_sha256,
            "harness_sha256": harness_sha256,
            "task_family": task.family,
            "sample_count": len(guarded.evaluation.scores),
            "candidate_output_sha256": hashlib.sha256(score_artifact).hexdigest(),
            "fit_seconds": guarded.evaluation.fit_seconds,
            "score_seconds": guarded.evaluation.score_seconds,
            "guards": {
                "hardcoding": guarded.hardcoding.code,
                "determinism": guarded.determinism.code,
                "causality": guarded.causality.code,
            },
        },
    )
    job_id = enqueue_score_job(
        planner_engine,
        run_id=run_id,
        experiment_id=experiment_id,
        evaluation_kind=evaluation_kind,
        task_id=task.task_id,
        seed=seed,
        candidate_sha256=candidate_sha256,
        candidate_output=score_artifact,
        artifact_root=DEFAULT_ARTIFACT_ROOT,
    )
    try:
        lease.require_run_active()
    except RuntimeError as exc:
        if str(exc) == "director_run_is_not_active":
            cancel_queued_score_job(planner_engine, job_id=job_id)
        raise
    result = run_scorer_process(job_id, remaining_seconds=scorer_remaining)
    lease.require_run_active()
    if result.exit_code != 0 or result.result is None:
        raise RuntimeError(f"separate Scorer failed for job {job_id}")
    if result.result.get("state") != "completed":
        raise RuntimeError(f"separate Scorer did not complete job {job_id}")
    process_provenance = _scorer_process_provenance(
        director_engine,
        result,
        job_id=job_id,
        run_id=run_id,
        experiment_id=experiment_id,
        evaluation_kind=evaluation_kind,
        task_id=task.task_id,
        seed=seed,
    )
    with director_engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    """
                SELECT task_id, experiment_id, evaluation_kind, seed,
                       candidate_sha256, dataset_id, split_id, session_id,
                       task_family, task_score, vus_pr, vus_roc,
                       fa_per_day, duty_fraction, sampling_s, position_bias,
                       sample_count, profile_sha256,
                       harness_sha256, candidate_output_sha256
                  FROM lab.dev_task_results
                 WHERE run_id=:run_id AND experiment_id=:experiment_id
                   AND evaluation_kind=:evaluation_kind AND task_id=:task_id AND seed=:seed
                """
                ),
                {
                    "run_id": run_id,
                    "experiment_id": experiment_id,
                    "evaluation_kind": evaluation_kind,
                    "task_id": task.task_id,
                    "seed": seed,
                },
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise RuntimeError("Scorer completed without an authorized dev result")
    expected_output_digest = hashlib.sha256(score_artifact).hexdigest()
    expected = {
        "candidate_sha256": candidate_sha256,
        "dataset_id": task.dataset_id,
        "split_id": task.split_id,
        "session_id": task.session_id,
        "profile_sha256": task.profile_sha256,
        "harness_sha256": harness_sha256,
        "sample_count": len(guarded.evaluation.scores),
        "candidate_output_sha256": expected_output_digest,
        "task_family": task.family,
    }
    sample_count = int(row["sample_count"])
    task_score = float(row["task_score"])
    if not math.isfinite(task_score):
        raise RuntimeError("Scorer returned a non-finite family primary score")
    optional_metrics = {
        key: (float(row[key]) if row[key] is not None else None)
        for key in ("vus_pr", "vus_roc", "fa_per_day", "duty_fraction", "position_bias")
    }
    if any(value is not None and not math.isfinite(value) for value in optional_metrics.values()):
        raise RuntimeError("Scorer returned a non-finite optional metric")
    if task.family == "EVT" and (
        optional_metrics["vus_pr"] is None or optional_metrics["vus_roc"] is None
    ):
        raise RuntimeError("Scorer omitted required EVT VUS metrics")
    if task.family != "EVT" and (
        optional_metrics["vus_pr"] is not None or optional_metrics["vus_roc"] is not None
    ):
        raise RuntimeError("Scorer reported VUS metrics for a non-EVT task")
    if (task.family == "NRM") != (optional_metrics["position_bias"] is not None):
        raise RuntimeError("Scorer position-bias measurement differs from trusted task family")
    if any(
        (sample_count if key == "sample_count" else row[key]) != value
        for key, value in expected.items()
    ):
        raise RuntimeError("Scorer dev result differs from trusted task and artifact identities")
    return {
        "task_id": task.task_id,
        "experiment_id": experiment_id,
        "evaluation_kind": evaluation_kind,
        "seed": seed,
        "dataset_id": task.dataset_id,
        "split_id": task.split_id,
        "session_id": task.session_id,
        "profile_sha256": task.profile_sha256,
        "candidate_sha256": candidate_sha256,
        "harness_sha256": harness_sha256,
        "candidate_output_sha256": expected_output_digest,
        "task_family": task.family,
        "task_score": task_score,
        **optional_metrics,
        "fit_seconds": guarded.evaluation.fit_seconds,
        "score_seconds": guarded.evaluation.score_seconds,
        "guards": {
            "hardcoding": guarded.hardcoding.code,
            "determinism": guarded.determinism.code,
            "causality": guarded.causality.code,
        },
        "systemd_unit": result.unit,
        "scorer_process_exit_code": result.exit_code,
        "scorer_process_job_id": str(result.job_id),
        "scorer_process_invocation_id": result.invocation_id,
        **process_provenance,
    }
