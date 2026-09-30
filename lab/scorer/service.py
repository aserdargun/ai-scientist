"""Parse untrusted numeric candidate output and score it against private labels."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, model_validator
from sqlalchemy import Engine, and_, func, insert, select, text, update

from harness.alarm import apply_alarm_policy, nrm_task_score, pdm_task_score, position_bias
from harness.contracts import AlarmPolicy
from lab.db.schema import (
    dataset_labels,
    dataset_profiles,
    dataset_task_semantics,
    experiment_records,
    experiments,
    reports,
    run_tasks,
    runs,
    score_jobs,
    task_scores,
    task_terminal_outcomes,
    trajectory_records,
)
from lab.db.task_plan import canonical_task_plan_digest
from lab.scorer.task_semantics import (
    parse_canonical_time_axis,
    score_masked_vus,
    validate_evt_semantics,
)

MAX_CANDIDATE_SCORE_BYTES = 32 * 1024 * 1024
MAX_SCORE_ROWS = 1_000_000


def _assert_scorer_run_execution(
    connection: Any,
    *,
    run_id: UUID,
    admitted_generation: int,
    execution_sha256: str,
    stop_requested: bool = False,
) -> None:
    """Fence a report transaction using its immutable durable admission pair."""
    function = (
        "lab.assert_scorer_run_stop_execution"
        if stop_requested
        else "lab.assert_scorer_report_execution"
    )
    receipt = connection.execute(
        text(f"SELECT {function}(:run_id,:admitted_generation,:execution_sha256)"),
        {
            "run_id": run_id,
            "admitted_generation": admitted_generation,
            "execution_sha256": execution_sha256,
        },
    ).scalar_one()
    if not isinstance(receipt, dict):
        raise RuntimeError("Scorer run-generation assertion returned no receipt")
    if (
        str(receipt.get("run_id")) != str(run_id)
        or receipt.get("admitted_generation") != admitted_generation
        or receipt.get("execution_sha256") != execution_sha256
    ):
        raise RuntimeError("Scorer run-generation assertion returned a different identity")


class CandidateOutputError(ValueError):
    """The candidate supplied malformed or incomplete numeric output."""


class CandidateAlarmPolicy(BaseModel):
    """Strict finite policy frozen during fit and echoed by the score phase."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    threshold: Annotated[StrictFloat, Field(allow_inf_nan=False)]
    release: Annotated[StrictFloat, Field(allow_inf_nan=False)]
    dwell: Annotated[StrictInt, Field(ge=1, le=1_000_000)]

    @model_validator(mode="after")
    def validate_release(self) -> CandidateAlarmPolicy:
        if self.release > self.threshold:
            raise ValueError("alarm release must not exceed threshold")
        return self


class CandidateScoreArtifact(BaseModel):
    """Strict JSON output accepted from an untrusted sandbox candidate."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal["candidate-scores.v1"] = Field(alias="schema")
    sample_indices: list[StrictInt] = Field(min_length=1, max_length=MAX_SCORE_ROWS)
    scores: list[Annotated[StrictFloat, Field(allow_inf_nan=False)]] = Field(
        min_length=1, max_length=MAX_SCORE_ROWS
    )
    alarm_policy: CandidateAlarmPolicy | None = None
    mode_diagnostics: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_score_alignment(self) -> CandidateScoreArtifact:
        if len(self.sample_indices) != len(self.scores):
            raise ValueError("sample index and score counts differ")
        if any(
            left >= right
            for left, right in zip(self.sample_indices[:-1], self.sample_indices[1:], strict=True)
        ):
            raise ValueError("sample indices must be strictly increasing")
        if self.mode_diagnostics is not None:
            from lab.scorer.mode_diagnostics import validate_mode_diagnostics

            validate_mode_diagnostics(self.mode_diagnostics, self.scores)
        return self


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant rejected: {value}")


def _strict_ordered_times(values: object, sampling_s: int) -> bool:
    if not isinstance(values, list) or not values:
        return False
    if all(isinstance(value, str) and value for value in values):
        try:
            parsed = parse_canonical_time_axis(values)
        except ValueError:
            return False
        return all(
            (right - left).total_seconds() == sampling_s
            for left, right in zip(parsed, parsed[1:], strict=False)
        )
    if all(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        for value in values
    ):
        return all(
            float(right) - float(left) == sampling_s
            for left, right in zip(values, values[1:], strict=False)
        )
    return False


def _strict_failure_windows(values: object, length: int) -> tuple[tuple[int, int], ...]:
    if not isinstance(values, list):
        raise ValueError("failure windows must be a JSON list")
    result: list[tuple[int, int]] = []
    previous_end = -1
    for window in values:
        if (
            not isinstance(window, list)
            or len(window) != 2
            or any(isinstance(point, bool) or not isinstance(point, int) for point in window)
        ):
            raise ValueError("failure window must contain exactly two integer indices")
        start, end = window
        if start < 0 or end <= start or end >= length or start <= previous_end:
            raise ValueError("failure windows must be ordered, disjoint, and in range")
        result.append((start, end))
        previous_end = end
    return tuple(result)


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def parse_candidate_score(payload: bytes) -> CandidateScoreArtifact:
    """Validate a byte-bounded artifact before score computation."""
    if not payload or len(payload) > MAX_CANDIDATE_SCORE_BYTES:
        raise CandidateOutputError("candidate score payload is empty or exceeds 32 MiB")
    if b'"mode_diagnostics"' in payload and len(payload) > 2 * 1024 * 1024:
        raise CandidateOutputError("mode diagnostics exceed the 2 MiB transport bound")
    try:
        parsed = json.loads(
            payload.decode("utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_no_duplicate_keys,
        )
        return CandidateScoreArtifact.model_validate(parsed)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise CandidateOutputError("candidate score payload is invalid") from exc


class IndependentScorer:
    """Score only with the dedicated scorer DB connection and trusted metadata."""

    def __init__(
        self,
        engine: Engine,
        *,
        harness_sha256: str,
        artifact_root: Path | None = None,
    ) -> None:
        if len(harness_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in harness_sha256
        ):
            raise ValueError("harness digest must be lowercase SHA-256")
        self.engine = engine
        self.harness_sha256 = harness_sha256
        from lab.scorer.jobs import DEFAULT_ARTIFACT_ROOT

        self.artifact_root = DEFAULT_ARTIFACT_ROOT if artifact_root is None else artifact_root

    @staticmethod
    def _experiment_records_complete(connection: Any, *, run_id: UUID) -> bool:
        """Check terminal experiment/document coverage without exposing ledger rows."""
        if connection.dialect.name == "postgresql":
            return bool(
                connection.execute(
                    text("SELECT lab.experiment_records_complete(:run_id)"),
                    {"run_id": run_id},
                ).scalar_one()
            )
        rows = connection.execute(
            select(experiments.c.experiment_id, experiments.c.status).where(
                experiments.c.run_id == run_id
            )
        ).all()
        terminal_statuses = {"scored", "crashed", "abandoned", "rejected"}
        for experiment_id, status in rows:
            if status not in terminal_statuses:
                return False
            record = connection.execute(
                select(experiment_records.c.experiment_id).where(
                    experiment_records.c.experiment_id == experiment_id
                )
            ).first()
            trajectory = connection.execute(
                select(trajectory_records.c.experiment_id).where(
                    trajectory_records.c.experiment_id == experiment_id
                )
            ).first()
            if record is None or trajectory is None:
                return False
        return True

    def score_task(
        self,
        *,
        run_id: UUID,
        experiment_id: str,
        evaluation_kind: Literal["baseline", "primary", "confirmation"],
        task_id: str,
        seed: int,
        candidate_sha256: str,
        candidate_output: bytes,
        score_job_id: UUID | None = None,
        claim_token: str | None = None,
        worker_invocation_id: str | None = None,
        admitted_generation: int | None = None,
        execution_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Recompute VUS from candidate scores and scorer-only labels."""
        if not task_id or len(task_id) > 128 or not experiment_id or len(experiment_id) > 128:
            raise ValueError("task or experiment id is empty or too long")
        if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
            raise ValueError("seed must be a non-negative integer")
        if len(candidate_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in candidate_sha256
        ):
            raise ValueError("candidate digest must be lowercase SHA-256")
        artifact = parse_candidate_score(candidate_output)
        if artifact.mode_diagnostics is not None:
            from lab.scorer.mode_diagnostics import validate_mode_diagnostics

            validate_mode_diagnostics(
                artifact.mode_diagnostics,
                artifact.scores,
                candidate_sha256=candidate_sha256,
                seed=seed,
            )
        if score_job_id is None:
            if (
                claim_token is not None
                or worker_invocation_id is not None
                or admitted_generation is not None
                or execution_sha256 is not None
            ):
                raise ValueError("claim fields require a score-job id")
            if self.engine.dialect.name == "postgresql":
                raise ValueError("managed PostgreSQL scoring requires an admitted score-job")
        elif (
            claim_token is None
            or worker_invocation_id is None
            or len(worker_invocation_id) != 32
            or any(character not in "0123456789abcdef" for character in worker_invocation_id)
        ):
            raise ValueError("score-job fencing requires a token and systemd invocation")
        if score_job_id is not None and self.engine.dialect.name == "postgresql":
            if (
                isinstance(admitted_generation, bool)
                or not isinstance(admitted_generation, int)
                or admitted_generation < 1
                or not isinstance(execution_sha256, str)
                or len(execution_sha256) != 64
                or any(character not in "0123456789abcdef" for character in execution_sha256)
            ):
                raise ValueError("managed score requires its captured execution generation")
        with self.engine.connect() as connection:
            run = connection.execute(
                select(runs.c.run_id, runs.c.state).where(runs.c.run_id == run_id)
            ).first()
            if run is None or run.state != "running":
                raise ValueError("run is not active for independent scoring")
            assignment = (
                connection.execute(
                    select(run_tasks).where(
                        run_tasks.c.run_id == run_id,
                        run_tasks.c.experiment_id == experiment_id,
                        run_tasks.c.evaluation_kind == evaluation_kind,
                        run_tasks.c.task_id == task_id,
                        run_tasks.c.seed == seed,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if assignment is None:
                raise ValueError("trusted run-to-task assignment not found")
            if assignment["candidate_sha256"] != candidate_sha256:
                raise ValueError("candidate digest does not match the trusted task plan")
            profile = (
                connection.execute(
                    select(dataset_profiles).where(
                        dataset_profiles.c.dataset_id == assignment["dataset_id"],
                        dataset_profiles.c.split_id == assignment["split_id"],
                        dataset_profiles.c.session_id == assignment["session_id"],
                    )
                )
                .mappings()
                .one_or_none()
            )
            if profile is None:
                raise ValueError("trusted scorer dataset profile not found")
            semantics = (
                connection.execute(
                    select(dataset_task_semantics).where(
                        dataset_task_semantics.c.dataset_id == assignment["dataset_id"],
                        dataset_task_semantics.c.split_id == assignment["split_id"],
                        dataset_task_semantics.c.session_id == assignment["session_id"],
                    )
                )
                .mappings()
                .one_or_none()
            )
            label_rows = connection.execute(
                select(dataset_labels.c.sample_index, dataset_labels.c.is_anomaly)
                .where(
                    dataset_labels.c.dataset_id == assignment["dataset_id"],
                    dataset_labels.c.split_id == assignment["split_id"],
                    dataset_labels.c.session_id == assignment["session_id"],
                )
                .order_by(dataset_labels.c.sample_index)
            ).all()
            if len(label_rows) != profile["sample_count"]:
                raise ValueError("trusted label count does not match dataset profile")
            expected_indices = list(range(profile["sample_count"]))
            labels = [bool(row.is_anomaly) for row in label_rows]
            stored_indices = [int(row.sample_index) for row in label_rows]
            if stored_indices != expected_indices:
                raise ValueError("trusted labels have an index gap or duplicate")
            if artifact.sample_indices != expected_indices:
                raise CandidateOutputError(
                    "candidate scores do not cover the complete ordered eval split"
                )
            profile_snapshot = dict(profile)
            assignment_snapshot = dict(assignment)
            semantics_snapshot = dict(semantics) if semantics is not None else None
        task_family = profile_snapshot["task_family"]
        if semantics_snapshot is not None and semantics_snapshot["task_family"] != task_family:
            raise ValueError("Scorer task family differs from the public trusted profile")
        score_values = np.asarray(artifact.scores, dtype=np.float64)
        base_metrics: dict[str, Any] = {
            "schema": "task-score.v1",
            "task_family": task_family,
            "task_id": task_id,
            "experiment_id": experiment_id,
            "evaluation_kind": evaluation_kind,
            "seed": seed,
            "dataset_id": assignment_snapshot["dataset_id"],
            "split_id": assignment_snapshot["split_id"],
            "session_id": assignment_snapshot["session_id"],
            "sample_count": profile_snapshot["sample_count"],
            "profile_sha256": profile_snapshot["profile_sha256"],
            "candidate_output_sha256": hashlib.sha256(candidate_output).hexdigest(),
            "candidate_sha256": candidate_sha256,
            "harness_sha256": self.harness_sha256,
        }
        if artifact.mode_diagnostics is not None:
            base_metrics["mode_diagnostics"] = {
                "schema": "candidate-mode-diagnostics-reference.v1",
                "candidate_derived": True,
                "artifact_sha256": hashlib.sha256(candidate_output).hexdigest(),
                "model_sha256": artifact.mode_diagnostics["model"]["model_sha256"],
                "configuration_sha256": artifact.mode_diagnostics["configuration_sha256"],
            }
        if task_family == "EVT":
            masks = None
            if semantics_snapshot is not None:
                masks = validate_evt_semantics(
                    sample_count=profile_snapshot["sample_count"],
                    sampling_s=semantics_snapshot["sampling_s"],
                    evaluation_times=semantics_snapshot["evaluation_times_json"],
                    masked_samples=semantics_snapshot["masked_samples_json"],
                    failure_windows=semantics_snapshot["failure_windows_json"],
                    semantics_sha256=semantics_snapshot["semantics_sha256"],
                )
                if any(labels[index] and masks[index] for index in range(len(labels))):
                    raise ValueError("masked public task points cannot carry positive labels")
            result, masked_count = score_masked_vus(
                labels,
                artifact.scores,
                masks=masks,
                sliding_window=profile_snapshot["sliding_window"],
            )
            metrics = {
                **base_metrics,
                "task_score": result.vus_pr,
                "vus_roc": result.vus_roc,
                "vus_pr": result.vus_pr,
                "sliding_window": profile_snapshot["sliding_window"],
                "scored_sample_count": profile_snapshot["sample_count"] - masked_count,
                "masked_sample_count": masked_count,
            }
        elif task_family in {"PDM", "NRM"}:
            if semantics_snapshot is None or artifact.alarm_policy is None:
                raise CandidateOutputError(
                    "PDM/NRM scoring requires trusted semantics and fit policy"
                )
            masks_raw = semantics_snapshot["masked_samples_json"]
            times_raw = semantics_snapshot["evaluation_times_json"]
            windows_raw = semantics_snapshot["failure_windows_json"]
            sample_count = int(profile_snapshot["sample_count"])
            if (
                not isinstance(masks_raw, list)
                or len(masks_raw) != sample_count
                or any(not isinstance(item, bool) for item in masks_raw)
                or not isinstance(times_raw, list)
                or len(times_raw) != sample_count
                or not isinstance(windows_raw, list)
                or (task_family == "PDM" and not windows_raw)
                or (task_family == "NRM" and windows_raw)
            ):
                raise ValueError("trusted task semantics are malformed or misaligned")
            canonical_semantics = json.dumps(
                {
                    "schema": "public-task-semantics.v1",
                    "task_family": task_family,
                    "sampling_s": semantics_snapshot["sampling_s"],
                    "evaluation_times": times_raw,
                    "masked_samples": masks_raw,
                    "failure_windows": windows_raw,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("ascii")
            if (
                hashlib.sha256(canonical_semantics).hexdigest()
                != semantics_snapshot["semantics_sha256"]
            ):
                raise ValueError("trusted task semantics digest does not match its content")
            if any(labels[index] and masks_raw[index] for index in range(sample_count)):
                raise ValueError("masked public task points cannot carry positive labels")
            sampling_s = int(semantics_snapshot["sampling_s"])
            if not _strict_ordered_times(times_raw, sampling_s):
                raise ValueError("trusted evaluation times differ from the ordered source cadence")
            failures = _strict_failure_windows(windows_raw, sample_count)
            if task_family == "PDM":
                positive_indices = {index for index, value in enumerate(labels) if value}
                expected_positive = {
                    index
                    for start, stop in failures
                    for index in range(start, stop + 1)
                    if not masks_raw[index]
                }
                if positive_indices != expected_positive:
                    raise ValueError("PDM positive labels differ from the trusted failure window")
            elif any(labels):
                raise ValueError("NRM tasks must have no positive event labels")
            policy = AlarmPolicy(
                artifact.alarm_policy.threshold,
                artifact.alarm_policy.release,
                artifact.alarm_policy.dwell,
            )
            masked = np.asarray(masks_raw, dtype=np.bool_)
            score_values[masked] = np.nan
            active = apply_alarm_policy(score_values, policy)
            if task_family == "PDM":
                task_score, fa_per_day, duty_fraction = pdm_task_score(
                    active, failures, masked, sampling_s, max_fa=1.0, max_duty=0.05
                )
            else:
                task_score, fa_per_day, duty_fraction = nrm_task_score(
                    active, masked, sampling_s, max_duty=0.05
                )
            metrics = {
                **base_metrics,
                "task_score": task_score,
                "fa_per_day": fa_per_day,
                "duty_fraction": duty_fraction,
                "sampling_s": sampling_s,
                "failure_windows": [list(window) for window in failures],
                "position_bias": position_bias(artifact.scores) if task_family == "NRM" else None,
                "alarm_policy": {
                    "threshold": policy.threshold,
                    "release": policy.release,
                    "dwell": policy.dwell,
                },
            }
        else:
            raise ValueError("trusted task family is unsupported")
        with self.engine.begin() as connection:
            if score_job_id is not None and self.engine.dialect.name == "postgresql":
                from lab.scorer.jobs import _assert_scorer_job_execution

                receipt_run_id, receipt_generation, receipt_execution = (
                    _assert_scorer_job_execution(
                        connection,
                        job_id=score_job_id,
                        claim_token=claim_token,
                        invocation_id=worker_invocation_id,
                    )
                )
                if (
                    receipt_run_id != run_id
                    or receipt_generation != admitted_generation
                    or receipt_execution != execution_sha256
                ):
                    raise ValueError(
                        "score job no longer matches its captured execution generation"
                    )
            # VUS can be expensive; reacquire and fence only around the score commit.
            run = connection.execute(
                select(runs.c.state).where(runs.c.run_id == run_id).with_for_update()
            ).first()
            if run is None or run.state != "running":
                raise ValueError("run is no longer active for score commit")
            if score_job_id is not None and claim_token is not None:
                job = (
                    connection.execute(
                        select(score_jobs)
                        .where(score_jobs.c.job_id == score_job_id)
                        .with_for_update()
                    )
                    .mappings()
                    .one_or_none()
                )
                if (
                    job is None
                    or job["state"] != "running"
                    or job["claimed_by"] != claim_token
                    or job["claim_invocation_id"] != worker_invocation_id
                    or job["claim_unit"] != f"swapp-ai-scientist-scorer-{score_job_id.hex}.service"
                    or job["lease_until"] is None
                    or job["lease_until"] <= datetime.now(UTC)
                    or job["artifact_sha256"] != hashlib.sha256(candidate_output).hexdigest()
                    or job["admitted_generation"] != admitted_generation
                    or job["execution_sha256"] != execution_sha256
                    or (
                        job["run_id"],
                        job["experiment_id"],
                        job["evaluation_kind"],
                        job["task_id"],
                        job["seed"],
                        job["candidate_sha256"],
                    )
                    != (
                        run_id,
                        experiment_id,
                        evaluation_kind,
                        task_id,
                        seed,
                        candidate_sha256,
                    )
                ):
                    raise ValueError("score-job claim expired or no longer owns this task")
            current_assignment = (
                connection.execute(
                    select(run_tasks).where(
                        run_tasks.c.run_id == run_id,
                        run_tasks.c.experiment_id == experiment_id,
                        run_tasks.c.evaluation_kind == evaluation_kind,
                        run_tasks.c.task_id == task_id,
                        run_tasks.c.seed == seed,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if current_assignment is None or dict(current_assignment) != assignment_snapshot:
                raise ValueError("trusted task plan changed before score commit")
            current_profile = (
                connection.execute(
                    select(dataset_profiles).where(
                        dataset_profiles.c.dataset_id == assignment_snapshot["dataset_id"],
                        dataset_profiles.c.split_id == assignment_snapshot["split_id"],
                        dataset_profiles.c.session_id == assignment_snapshot["session_id"],
                    )
                )
                .mappings()
                .one_or_none()
            )
            if current_profile is None or any(
                current_profile[key] != profile_snapshot[key]
                for key in ("sample_count", "sliding_window", "profile_sha256", "task_family")
            ):
                raise ValueError("trusted dataset profile changed before score commit")
            current_semantics = (
                connection.execute(
                    select(dataset_task_semantics).where(
                        dataset_task_semantics.c.dataset_id == assignment_snapshot["dataset_id"],
                        dataset_task_semantics.c.split_id == assignment_snapshot["split_id"],
                        dataset_task_semantics.c.session_id == assignment_snapshot["session_id"],
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                dict(current_semantics) if current_semantics is not None else None
            ) != semantics_snapshot:
                raise ValueError("trusted task semantics changed before score commit")
            existing = connection.execute(
                select(task_scores.c.score).where(
                    task_scores.c.run_id == run_id,
                    task_scores.c.experiment_id == experiment_id,
                    task_scores.c.evaluation_kind == evaluation_kind,
                    task_scores.c.task_id == task_id,
                    task_scores.c.seed == seed,
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing != metrics:
                    raise ValueError("task score already exists with different verified output")
                return metrics
            connection.execute(
                insert(task_scores).values(
                    run_id=run_id,
                    experiment_id=experiment_id,
                    evaluation_kind=evaluation_kind,
                    task_id=task_id,
                    seed=seed,
                    score=metrics,
                    guard_results={"score_domain": "finite", "full_index_coverage": True},
                    score_job_id=score_job_id,
                    claim_token=claim_token,
                    worker_invocation_id=worker_invocation_id,
                    created_at=datetime.now(UTC),
                )
            )
        return metrics

    def record_terminal_outcome(
        self,
        *,
        run_id: UUID,
        experiment_id: str,
        evaluation_kind: Literal["baseline", "primary", "confirmation"],
        task_id: str,
        seed: int,
        candidate_sha256: str,
        outcome_code: Literal[
            "guard_rejected",
            "candidate_rejected",
            "candidate_crash",
            "candidate_timeout",
            "scorer_error",
            "cancelled",
        ],
        score_job_id: UUID | None = None,
        claim_token: str | None = None,
        worker_invocation_id: str | None = None,
        recovery_invocation_id: str | None = None,
        admitted_generation: int | None = None,
        execution_sha256: str | None = None,
    ) -> None:
        """Persist one typed non-score result; PostgreSQL derives its producer role."""
        if score_job_id is None:
            if (
                claim_token is not None
                or worker_invocation_id is not None
                or recovery_invocation_id is not None
                or admitted_generation is not None
                or execution_sha256 is not None
            ):
                raise ValueError("terminal claim fields require a score-job id")
            if self.engine.dialect.name == "postgresql":
                raise ValueError("managed PostgreSQL outcomes require an admitted score-job")
        elif (
            claim_token is None
            or worker_invocation_id is None
            or len(worker_invocation_id) != 32
            or any(character not in "0123456789abcdef" for character in worker_invocation_id)
        ):
            raise ValueError("terminal Scorer claim requires token and systemd invocation")
        if score_job_id is not None and self.engine.dialect.name == "postgresql":
            if (
                isinstance(admitted_generation, bool)
                or not isinstance(admitted_generation, int)
                or admitted_generation < 1
                or not isinstance(execution_sha256, str)
                or len(execution_sha256) != 64
                or any(character not in "0123456789abcdef" for character in execution_sha256)
            ):
                raise ValueError("managed terminal outcome requires its captured execution pair")
        if recovery_invocation_id is not None and (
            outcome_code != "cancelled"
            or worker_invocation_id is None
            or recovery_invocation_id == worker_invocation_id
            or len(recovery_invocation_id) != 32
            or any(character not in "0123456789abcdef" for character in recovery_invocation_id)
        ):
            raise ValueError("recovery requires a distinct verified cancellation invocation")
        if outcome_code in {"scorer_error", "candidate_rejected"} and score_job_id is None:
            raise ValueError("Scorer terminal outcomes require an active score-job claim")
        if len(candidate_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in candidate_sha256
        ):
            raise ValueError("candidate digest must be lowercase SHA-256")
        identity = {
            "run_id": run_id,
            "experiment_id": experiment_id,
            "evaluation_kind": evaluation_kind,
            "task_id": task_id,
            "seed": seed,
        }
        with self.engine.begin() as connection:
            if score_job_id is not None and self.engine.dialect.name == "postgresql":
                from lab.scorer.jobs import _assert_scorer_job_execution

                receipt_run_id, receipt_generation, receipt_execution = (
                    _assert_scorer_job_execution(
                        connection,
                        job_id=score_job_id,
                        claim_token=claim_token,
                        invocation_id=worker_invocation_id,
                        stop_closure=(outcome_code == "cancelled"),
                    )
                )
                if (
                    receipt_run_id != run_id
                    or receipt_generation != admitted_generation
                    or receipt_execution != execution_sha256
                ):
                    raise ValueError("terminal outcome differs from the admitted job generation")
            run = connection.execute(
                select(runs.c.state).where(runs.c.run_id == run_id).with_for_update()
            ).scalar_one_or_none()
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
                raise ValueError("terminal outcome does not match the trusted task assignment")
            existing = connection.execute(
                select(
                    task_terminal_outcomes.c.candidate_sha256,
                    task_terminal_outcomes.c.outcome_code,
                    task_terminal_outcomes.c.score_job_id,
                    task_terminal_outcomes.c.worker_invocation_id,
                    task_terminal_outcomes.c.admitted_generation,
                    task_terminal_outcomes.c.execution_sha256,
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
                    and existing.score_job_id == score_job_id
                    and existing.worker_invocation_id == worker_invocation_id
                    and existing.admitted_generation == admitted_generation
                    and existing.execution_sha256 == execution_sha256
                ):
                    return
                raise ValueError("task already has a different immutable terminal outcome")
            expected_state = "stop_requested" if outcome_code == "cancelled" else "running"
            if run != expected_state:
                raise ValueError("run is not active for terminal task resolution")
            connection.execute(
                insert(task_terminal_outcomes).values(
                    **identity,
                    candidate_sha256=candidate_sha256,
                    outcome_code=outcome_code,
                    # PostgreSQL's BEFORE INSERT trigger replaces this untrusted placeholder
                    # with session_user; SQLite fixtures have no database role boundary.
                    producer_role="untrusted",
                    score_job_id=score_job_id,
                    claim_token=claim_token,
                    worker_invocation_id=worker_invocation_id,
                    recovery_invocation_id=recovery_invocation_id,
                    admitted_generation=admitted_generation,
                    execution_sha256=execution_sha256,
                    created_at=datetime.now(UTC),
                )
            )

    def complete_run(
        self,
        *,
        run_id: UUID,
        admitted_generation: int | None = None,
        execution_sha256: str | None = None,
        empty_baseline_stop: bool = False,
    ) -> tuple[dict[str, Any], str]:
        """Freeze independently computed task scores into a content-hashed report."""
        if self.engine.dialect.name == "postgresql":
            # Commit the baseline cleanup window before report construction. A failed
            # publication retry must never start a fresh 60-second authorization.
            with self.engine.begin() as preparation:
                identity = preparation.execute(
                    select(runs.c.state, runs.c.request_json).where(runs.c.run_id == run_id)
                ).first()
                if (
                    identity is not None
                    and identity.state == "running"
                    and isinstance(identity.request_json, dict)
                    and identity.request_json.get("purpose") == "baseline"
                ):
                    preparation.execute(
                        text(
                            "SELECT lab.prepare_scorer_baseline_finalization(:run,:generation,:sha)"
                        ),
                        {"run": run_id, "generation": admitted_generation, "sha": execution_sha256},
                    )
        with self.engine.begin() as connection:
            report_generation: int | None = None
            report_execution_sha256: str | None = None
            receipt_execution_pairs: list[dict[str, Any]] | None = None
            ownerless_baseline_stop = False
            existing_report = connection.execute(
                select(reports.c.report_json, reports.c.report_sha256).where(
                    reports.c.run_id == run_id
                )
            ).first()
            if existing_report is not None:
                if self.engine.dialect.name == "postgresql":
                    report_pair = (
                        existing_report.report_json.get("admitted_generation"),
                        existing_report.report_json.get("execution_sha256"),
                    )
                    if empty_baseline_stop:
                        if report_pair != (None, None):
                            raise ValueError(
                                "empty-stop finalizer found a report for another generation"
                            )
                    elif report_pair != (admitted_generation, execution_sha256):
                        raise ValueError("finalizer pair differs from the already published report")
                return existing_report.report_json, existing_report.report_sha256
            if self.engine.dialect.name == "postgresql":
                snapshot = connection.execute(
                    select(
                        runs.c.state,
                        runs.c.request_json,
                        runs.c.task_plan_sha256,
                        runs.c.task_plan_count,
                    ).where(runs.c.run_id == run_id)
                ).first()
                if snapshot is None:
                    raise ValueError("run not found")
                receipts = connection.execute(
                    select(
                        score_jobs.c.admitted_generation,
                        score_jobs.c.execution_sha256,
                    )
                    .where(score_jobs.c.run_id == run_id)
                    .union_all(
                        select(
                            task_terminal_outcomes.c.admitted_generation,
                            task_terminal_outcomes.c.execution_sha256,
                        ).where(task_terminal_outcomes.c.run_id == run_id)
                    )
                ).all()
                pairs = {(row.admitted_generation, row.execution_sha256) for row in receipts}
                zero_task_baseline_stop = (
                    snapshot.state == "stop_requested"
                    and isinstance(snapshot.request_json, dict)
                    and snapshot.request_json.get("purpose") == "baseline"
                    and snapshot.task_plan_count == 0
                    and snapshot.task_plan_sha256 == canonical_task_plan_digest([])
                    and not receipts
                )
                captured_pair = (admitted_generation, execution_sha256)
                if empty_baseline_stop and captured_pair != (None, None):
                    raise ValueError("empty baseline stop cannot carry an execution pair")
                ownerless_baseline_stop = zero_task_baseline_stop and captured_pair == (None, None)
                if not ownerless_baseline_stop:
                    if admitted_generation is None or execution_sha256 is None:
                        raise ValueError("Scorer finalization requires its captured execution pair")
                    if receipts:
                        authority = connection.execute(
                            text(
                                "SELECT lab.assert_scorer_report_receipt_ancestry("
                                ":run,:generation,:sha)"
                            ),
                            {
                                "run": run_id,
                                "generation": admitted_generation,
                                "sha": execution_sha256,
                            },
                        ).scalar_one()
                        if (
                            not isinstance(authority, dict)
                            or authority.get("run_id") != str(run_id)
                            or authority.get("admitted_generation") != admitted_generation
                            or authority.get("execution_sha256") != execution_sha256
                        ):
                            raise ValueError(
                                "Scorer report ancestry returned another writer identity"
                            )
                        observed = authority.get("receipt_execution_pairs")
                        expected_pairs = [
                            {"admitted_generation": generation, "execution_sha256": digest}
                            for generation, digest in sorted(pairs)
                        ]
                        if observed != expected_pairs:
                            raise ValueError(
                                "Scorer report ancestry differs from durable receipt inventory"
                            )
                        if any(generation != admitted_generation for generation, _ in pairs):
                            receipt_execution_pairs = expected_pairs
                        receipt_generation = admitted_generation
                        receipt_execution = execution_sha256
                    elif zero_task_baseline_stop:
                        # A claimed baseline may be stopped before its first job exists.
                        # Its caller-supplied pair is checked against durable ownership
                        # below; absence of task receipts does not make it ownerless.
                        receipt_generation = admitted_generation
                        receipt_execution = execution_sha256
                    else:
                        raise ValueError(
                            "Scorer report has no durable receipt for its execution pair"
                        )
                    report_generation = receipt_generation
                    report_execution_sha256 = receipt_execution
                    _assert_scorer_run_execution(
                        connection,
                        run_id=run_id,
                        admitted_generation=receipt_generation,
                        execution_sha256=receipt_execution,
                        stop_requested=snapshot.state == "stop_requested",
                    )
                else:
                    if not empty_baseline_stop:
                        raise ValueError(
                            "ownerless baseline stop requires its explicit finalizer mode"
                        )
                    connection.execute(
                        text("SELECT lab.assert_scorer_empty_baseline_stop(:run_id)"),
                        {"run_id": run_id},
                    )
            run = connection.execute(
                select(
                    runs.c.state,
                    runs.c.report_sha256,
                    runs.c.task_plan_sha256,
                    runs.c.task_plan_count,
                    runs.c.request_json,
                )
                .where(runs.c.run_id == run_id)
                .with_for_update()
            ).first()
            if run is None:
                raise ValueError("run not found")
            if not self._experiment_records_complete(connection, run_id=run_id):
                raise ValueError(
                    "cannot publish until registered experiments have terminal records"
                )
            if run.state not in {"running", "stop_requested"}:
                raise ValueError("run cannot be finalized from its current state")
            baseline_intent = (
                isinstance(run.request_json, dict) and run.request_json.get("purpose") == "baseline"
            )
            if run.task_plan_sha256 is None or run.task_plan_count is None:
                raise ValueError("cannot publish a report until the Director seals the task plan")
            trusted_tasks = (
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
            if (
                len(trusted_tasks) != run.task_plan_count
                or canonical_task_plan_digest(trusted_tasks) != run.task_plan_sha256
            ):
                raise ValueError(
                    "sealed task plan digest or task count does not match trusted rows"
                )
            task_rows = connection.execute(
                select(
                    task_scores.c.experiment_id,
                    task_scores.c.evaluation_kind,
                    task_scores.c.task_id,
                    task_scores.c.seed,
                    task_scores.c.score,
                    dataset_profiles.c.visibility,
                )
                .where(task_scores.c.run_id == run_id)
                .join(
                    run_tasks,
                    and_(
                        run_tasks.c.run_id == task_scores.c.run_id,
                        run_tasks.c.experiment_id == task_scores.c.experiment_id,
                        run_tasks.c.evaluation_kind == task_scores.c.evaluation_kind,
                        run_tasks.c.task_id == task_scores.c.task_id,
                        run_tasks.c.seed == task_scores.c.seed,
                    ),
                )
                .join(
                    dataset_profiles,
                    and_(
                        dataset_profiles.c.dataset_id == run_tasks.c.dataset_id,
                        dataset_profiles.c.split_id == run_tasks.c.split_id,
                        dataset_profiles.c.session_id == run_tasks.c.session_id,
                    ),
                )
                .order_by(
                    task_scores.c.experiment_id,
                    task_scores.c.evaluation_kind,
                    task_scores.c.task_id,
                    task_scores.c.seed,
                )
            ).all()
            visible_task_rows = [row for row in task_rows if row.visibility == "dev"]
            expected_task_ids = {
                (
                    row["experiment_id"],
                    row["evaluation_kind"],
                    row["task_id"],
                    row["seed"],
                )
                for row in trusted_tasks
            }
            scored_task_ids = {
                (row.experiment_id, row.evaluation_kind, row.task_id, row.seed) for row in task_rows
            }
            outcome_rows = connection.execute(
                select(
                    task_terminal_outcomes.c.experiment_id,
                    task_terminal_outcomes.c.evaluation_kind,
                    task_terminal_outcomes.c.task_id,
                    task_terminal_outcomes.c.seed,
                    task_terminal_outcomes.c.candidate_sha256,
                    task_terminal_outcomes.c.outcome_code,
                    task_terminal_outcomes.c.producer_role,
                    task_terminal_outcomes.c.score_job_id,
                    dataset_profiles.c.visibility,
                )
                .where(task_terminal_outcomes.c.run_id == run_id)
                .join(
                    run_tasks,
                    and_(
                        run_tasks.c.run_id == task_terminal_outcomes.c.run_id,
                        run_tasks.c.experiment_id == task_terminal_outcomes.c.experiment_id,
                        run_tasks.c.evaluation_kind == task_terminal_outcomes.c.evaluation_kind,
                        run_tasks.c.task_id == task_terminal_outcomes.c.task_id,
                        run_tasks.c.seed == task_terminal_outcomes.c.seed,
                    ),
                )
                .join(
                    dataset_profiles,
                    and_(
                        dataset_profiles.c.dataset_id == run_tasks.c.dataset_id,
                        dataset_profiles.c.split_id == run_tasks.c.split_id,
                        dataset_profiles.c.session_id == run_tasks.c.session_id,
                    ),
                )
                .order_by(
                    task_terminal_outcomes.c.experiment_id,
                    task_terminal_outcomes.c.evaluation_kind,
                    task_terminal_outcomes.c.task_id,
                    task_terminal_outcomes.c.seed,
                )
            ).all()
            visible_outcome_rows = [row for row in outcome_rows if row.visibility == "dev"]
            outcome_task_ids = {
                (row.experiment_id, row.evaluation_kind, row.task_id, row.seed)
                for row in outcome_rows
            }
            if (
                scored_task_ids & outcome_task_ids
                or scored_task_ids | outcome_task_ids != expected_task_ids
            ):
                raise ValueError("cannot publish until every trusted task has one resolution")
            if run.state == "running" and not expected_task_ids:
                raise ValueError("an active completed run requires a nonempty task plan")
            pending_jobs = connection.execute(
                select(func.count())
                .select_from(score_jobs)
                .where(
                    score_jobs.c.run_id == run_id,
                    score_jobs.c.state.in_(("queued", "running")),
                )
            ).scalar_one()
            if pending_jobs:
                raise ValueError("cannot publish while queued or claimed score jobs remain")
            outcome_state = (
                "stopped"
                if run.state == "stop_requested"
                else (
                    "failed"
                    if any(row.outcome_code == "scorer_error" for row in outcome_rows)
                    else "completed"
                )
            )
            task_score_values = [
                {
                    "experiment_id": row.experiment_id,
                    "evaluation_kind": row.evaluation_kind,
                    "task_id": row.task_id,
                    "seed": row.seed,
                    **row.score,
                }
                for row in visible_task_rows
            ]
            task_outcome_values = [
                {
                    "experiment_id": row.experiment_id,
                    "evaluation_kind": row.evaluation_kind,
                    "task_id": row.task_id,
                    "seed": row.seed,
                    "candidate_sha256": row.candidate_sha256,
                    "outcome": row.outcome_code,
                    "producer_role": row.producer_role,
                    "score_job_id": str(row.score_job_id) if row.score_job_id else None,
                }
                for row in visible_outcome_rows
            ]
            baseline_document_receipts: list[dict[str, Any]] = []
            if baseline_intent:
                receipt_value = connection.execute(
                    text("SELECT lab.baseline_terminal_document_receipts(:run_id)"),
                    {"run_id": run_id},
                ).scalar_one()
                if not isinstance(receipt_value, list) or any(
                    not isinstance(item, dict) for item in receipt_value
                ):
                    raise ValueError("Scorer did not receive baseline terminal pair receipts")
                baseline_document_receipts.extend(receipt_value)
            if baseline_intent:
                from lab.scorer.baseline_report import validate_baseline_report

                if outcome_state == "completed":
                    verified = connection.execute(
                        text("SELECT lab.verify_baseline_operation(:run_id)"),
                        {"run_id": run_id},
                    ).scalar_one()
                    if not isinstance(verified, dict):
                        raise ValueError("Scorer did not receive a verified baseline receipt")
                    budget_receipt = verified.get("budget")
                    if (
                        not isinstance(budget_receipt, dict)
                        or budget_receipt.get("proposal_count") != 0
                        or budget_receipt.get("model_tokens") != 0
                        or budget_receipt.get("reserved_model_tokens") != 0
                    ):
                        raise ValueError("verified baseline budget receipt is invalid")
                    calibration_complete = True
                    budget_verified = True
                else:
                    verified = {
                        "suite_manifest_sha256": run.request_json.get("suite_manifest_sha256"),
                        "harness_sha256": run.request_json.get("harness_sha256"),
                        "image_sha256": run.request_json.get("image_sha256"),
                        "task_plan_sha256": run.task_plan_sha256,
                        "task_plan_count": run.task_plan_count,
                        "task_count": len({row["task_id"] for row in trusted_tasks}),
                        "score_count": len(task_score_values),
                    }
                    budget_receipt = {
                        "status": "unverified_incomplete",
                        "requested": run.request_json.get("budget"),
                    }
                    calibration_complete = False
                    budget_verified = False
                report = {
                    "schema": "lab.baseline-report.v1",
                    "run_id": str(run_id),
                    "purpose": "baseline",
                    "status": outcome_state,
                    "calibration_complete": calibration_complete,
                    "budget_verified": budget_verified,
                    "request_sha256": hashlib.sha256(
                        json.dumps(
                            {
                                key: value
                                for key, value in run.request_json.items()
                                if key != "idempotency_key"
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ).encode("utf-8")
                    ).hexdigest(),
                    "suite_id": verified.get("suite_id", run.request_json.get("suite")),
                    "suite_version": verified.get("suite_version"),
                    "suite_manifest_sha256": verified.get("suite_manifest_sha256"),
                    "harness_sha256": verified.get("harness_sha256"),
                    "image_sha256": verified.get("image_sha256"),
                    "task_plan_sha256": verified.get("task_plan_sha256"),
                    "task_plan_count": verified.get("task_plan_count"),
                    "admitted_generation": report_generation,
                    "execution_sha256": report_execution_sha256,
                    "calibration_sha256": verified.get("calibration_sha256"),
                    "algorithms": ["robust_z", "iforest", "ecod_train_frozen"],
                    "seeds": [0, 1, 2],
                    "task_count": verified.get("task_count"),
                    "score_count": verified.get("score_count"),
                    "budget_receipt": budget_receipt,
                    "model_usage": {"input_tokens": 0, "output_tokens": 0, "calls": 0},
                    "baseline_records": [
                        {
                            "experiment_id": row.experiment_id,
                            "evaluation_kind": row.evaluation_kind,
                            "task_id": row.task_id,
                            "seed": row.seed,
                            **row.score,
                        }
                        for row in visible_task_rows
                    ],
                    "experiment_records": baseline_document_receipts,
                    "task_terminal_outcomes": task_outcome_values,
                }
                if ownerless_baseline_stop:
                    report.pop("admitted_generation")
                    report.pop("execution_sha256")
                if receipt_execution_pairs is not None:
                    report["receipt_execution_pairs"] = receipt_execution_pairs
                report = validate_baseline_report(report)
            else:
                report = {
                    "schema": "lab.report.v1",
                    **(
                        {
                            "study_kind": "single_snapshot_study",
                            "provider": "mode-grid",
                            "benchmark_acceptance": False,
                        }
                        if run.request_json.get("provider") == "mode-grid"
                        else {}
                    ),
                    "run_id": str(run_id),
                    "status": outcome_state,
                    "admitted_generation": report_generation,
                    "execution_sha256": report_execution_sha256,
                    "task_scores": task_score_values,
                    "task_terminal_outcomes": task_outcome_values,
                    **(
                        {"receipt_execution_pairs": receipt_execution_pairs}
                        if receipt_execution_pairs is not None
                        else {}
                    ),
                }
            if (
                connection.dialect.name == "postgresql"
                and run.state == "running"
                and run.request_json.get("purpose") != "baseline"
            ):
                from lab.scorer.explore_report import terminal_explore_provenance

                if report_generation is None or report_execution_sha256 is None:
                    raise ValueError("terminal research report requires an execution pair")
                report.update(
                    terminal_explore_provenance(
                        connection,
                        run_id=run_id,
                        generation=report_generation,
                        execution_sha256=report_execution_sha256,
                        artifact_root=self.artifact_root,
                    )
                )
            encoded = json.dumps(
                report,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            report_sha256 = hashlib.sha256(encoded).hexdigest()
            now = datetime.now(UTC)
            connection.execute(
                insert(reports).values(
                    run_id=run_id,
                    report_sha256=report_sha256,
                    report_json=report,
                    verified_at=now,
                )
            )
            changed = connection.execute(
                update(runs)
                .where(runs.c.run_id == run_id, runs.c.state == run.state)
                .values(
                    state=outcome_state,
                    report_sha256=report_sha256,
                    stop_requested=False,
                    updated_at=now,
                )
            ).rowcount
            if changed != 1:
                raise ValueError("run state changed while report was being finalized")
        return report, report_sha256

    def finalize_if_ready(
        self,
        *,
        run_id: UUID,
        admitted_generation: int | None = None,
        execution_sha256: str | None = None,
        empty_baseline_stop: bool = False,
    ) -> tuple[dict[str, Any], str] | None:
        """Publish after every sealed task has one score or typed terminal outcome."""
        with self.engine.connect() as connection:
            candidate = connection.execute(
                select(
                    runs.c.state,
                    runs.c.request_json,
                    runs.c.payload_sha256,
                    runs.c.task_plan_sha256,
                    runs.c.task_plan_count,
                ).where(runs.c.run_id == run_id)
            ).one_or_none()
        if candidate is not None and candidate.state in {"completed", "failed", "stopped"}:
            return self.complete_run(
                run_id=run_id,
                admitted_generation=admitted_generation,
                execution_sha256=execution_sha256,
                empty_baseline_stop=empty_baseline_stop,
            )
        if (
            candidate is not None
            and candidate.state == "stop_requested"
            and (
                candidate.task_plan_sha256 is None
                or (
                    candidate.task_plan_sha256 == canonical_task_plan_digest([])
                    and candidate.task_plan_count == 0
                )
            )
            and isinstance(candidate.request_json, dict)
            and candidate.request_json.get("purpose") == "baseline"
            and empty_baseline_stop
        ):
            with self.engine.begin() as connection:
                prepared = connection.execute(
                    text("SELECT lab.prepare_unstarted_baseline_stop(:run_id,:payload_sha256)"),
                    {"run_id": run_id, "payload_sha256": candidate.payload_sha256},
                ).scalar_one()
            if (
                not isinstance(prepared, dict)
                or prepared.get("run_id") != str(run_id)
                or prepared.get("task_plan_sha256") != canonical_task_plan_digest([])
                or prepared.get("task_plan_count") != 0
                or prepared.get("state") != "stop_requested"
            ):
                raise ValueError("Scorer did not seal the verified empty baseline stop plan")
        with self.engine.connect() as connection:
            run = connection.execute(
                select(runs.c.state, runs.c.task_plan_sha256, runs.c.task_plan_count).where(
                    runs.c.run_id == run_id
                )
            ).one_or_none()
            if (
                run is None
                or run.state not in {"running", "stop_requested"}
                or run.task_plan_sha256 is None
                or run.task_plan_count is None
            ):
                return None
            task_rows = (
                connection.execute(select(run_tasks).where(run_tasks.c.run_id == run_id))
                .mappings()
                .all()
            )
            if (
                len(task_rows) != run.task_plan_count
                or canonical_task_plan_digest(task_rows) != run.task_plan_sha256
            ):
                raise ValueError(
                    "sealed task plan digest or task count does not match trusted rows"
                )
            scored = connection.execute(
                select(func.count()).select_from(task_scores).where(task_scores.c.run_id == run_id)
            ).scalar_one()
            terminal = connection.execute(
                select(func.count())
                .select_from(task_terminal_outcomes)
                .where(task_terminal_outcomes.c.run_id == run_id)
            ).scalar_one()
            pending = connection.execute(
                select(func.count())
                .select_from(score_jobs)
                .where(
                    score_jobs.c.run_id == run_id,
                    score_jobs.c.state.in_(("queued", "running")),
                )
            ).scalar_one()
            experiments_complete = self._experiment_records_complete(connection, run_id=run_id)
        if (
            (run.state == "running" and run.task_plan_count == 0)
            or scored + terminal != run.task_plan_count
            or pending
            or not experiments_complete
        ):
            return None
        return self.complete_run(
            run_id=run_id,
            admitted_generation=admitted_generation,
            execution_sha256=execution_sha256,
            empty_baseline_stop=empty_baseline_stop,
        )
