"""Trusted host-side fit/score orchestration and candidate guard decisions."""

from __future__ import annotations

import ast
import hashlib
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.ipc as ipc
from scipy.stats import spearmanr

from harness.contracts import AlarmPolicy, FitContext
from lab.sandbox.docker_runner import LocalDockerRunner, Phase, SandboxResult, SandboxRunError
from lab.scorer.service import CandidateScoreArtifact, parse_candidate_score

FORBIDDEN_COLUMNS = frozenset(
    {"label", "labels", "anomaly", "is_anomaly", "changepoint", "timestamp", "datetime"}
)
DETERMINISM_REL_TOLERANCE = 1e-7
CAUSALITY_REL_TOLERANCE = 1e-7


class CandidateGuardReject(ValueError):
    """Typed guard rejection safe to persist without candidate source text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class CandidateEvaluation:
    """Opaque fit state and strict numeric output from fresh Docker phases."""

    fit_artifact: bytes
    fit_artifact_sha256: str
    scores: tuple[float, ...]
    policy: AlarmPolicy
    fit_seconds: float
    score_seconds: float
    score_document: bytes
    fit_container_name: str
    score_container_name: str


@dataclass(frozen=True, slots=True)
class GuardResult:
    """Stable guard outcome with a machine-readable code and bounded detail."""

    passed: bool
    code: str
    value: float | None = None


@dataclass(frozen=True, slots=True)
class GuardedCandidateEvaluation:
    """Candidate output after per-seed guards, pending suite-wide NRM gating."""

    evaluation: CandidateEvaluation
    determinism: GuardResult
    causality: GuardResult
    hardcoding: GuardResult


def _arrow_bytes(frame: pd.DataFrame, *, allowed_sensor_columns: tuple[str, ...]) -> bytes:
    if not isinstance(frame, pd.DataFrame) or frame.empty or not frame.columns.is_unique:
        raise CandidateGuardReject("invalid_input_frame")
    if tuple(str(name) for name in frame.columns) != allowed_sensor_columns:
        raise CandidateGuardReject("unapproved_sensor_columns")
    if any(name.lower() in FORBIDDEN_COLUMNS for name in allowed_sensor_columns):
        raise CandidateGuardReject("forbidden_input_column")
    try:
        values = frame.to_numpy(dtype=np.float64, copy=True)
    except (TypeError, ValueError) as exc:
        raise CandidateGuardReject("non_numeric_input") from exc
    if values.ndim != 2 or not np.isfinite(values).all():
        raise CandidateGuardReject("non_finite_input")
    # Rebuild from validated numeric values so pandas categoricals, unused levels,
    # extension metadata and frame attrs cannot travel through Arrow IPC.
    clean_frame = pd.DataFrame(values, columns=allowed_sensor_columns)
    table = pa.Table.from_pandas(clean_frame, preserve_index=False)
    table = table.replace_schema_metadata(None)
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    payload = cast(bytes, sink.getvalue().to_pybytes())
    if len(payload) > 128 * 1024**2:
        raise CandidateGuardReject("input_too_large")
    return payload


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate policy key")
        result[key] = value
    return result


def _strict_policy(payload: bytes) -> AlarmPolicy:
    try:
        value = json.loads(
            payload.decode("ascii"),
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite")),
        )
        if not isinstance(value, dict) or set(value) != {"threshold", "release", "dwell"}:
            raise ValueError
        threshold = value["threshold"]
        release = value["release"]
        dwell = value["dwell"]
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or isinstance(release, bool)
            or not isinstance(release, (int, float))
            or isinstance(dwell, bool)
            or not isinstance(dwell, int)
        ):
            raise ValueError
        threshold_value = float(threshold)
        release_value = float(release)
        if dwell > 1_000_000:
            raise ValueError
        return AlarmPolicy(threshold_value, release_value, dwell)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        OverflowError,
    ) as exc:
        raise CandidateGuardReject("invalid_alarm_policy") from exc


def _score_artifact(
    result: bytes,
    expected_rows: int,
    *,
    allow_constant_scores: bool = False,
    expected_policy: AlarmPolicy | None = None,
) -> CandidateScoreArtifact:
    try:
        artifact = parse_candidate_score(result)
    except ValueError as exc:
        raise CandidateGuardReject("invalid_score_artifact") from exc
    if len(artifact.scores) != expected_rows or artifact.sample_indices != list(
        range(expected_rows)
    ):
        raise CandidateGuardReject("score_length_or_index_mismatch")
    scores = np.asarray(artifact.scores, dtype=np.float64)
    if not np.isfinite(scores).all():
        raise CandidateGuardReject("non_finite_scores")
    if np.all(scores == scores[0]) and not allow_constant_scores:
        raise CandidateGuardReject("degenerate_constant_scores")
    if expected_policy is not None:
        observed = artifact.alarm_policy
        if observed is None or (
            observed.threshold != expected_policy.threshold
            or observed.release != expected_policy.release
            or observed.dwell != expected_policy.dwell
        ):
            raise CandidateGuardReject("score_policy_differs_from_fitted_policy")
    return artifact


def _run_candidate_phase(
    runner: LocalDockerRunner,
    *,
    phase: Phase,
    candidate_source: bytes,
    arrow_input: bytes,
    fit_artifact: bytes | None,
    context: FitContext,
    timeout_seconds: int,
    trusted_baseline_name: str | None = None,
    admission_check: Callable[[], None] | None = None,
) -> SandboxResult:
    try:
        if admission_check is not None:
            admission_check()
        return runner.run_phase(
            phase=phase,
            candidate_source=candidate_source,
            arrow_input=arrow_input,
            fit_artifact=fit_artifact,
            contract_context=context,
            trusted_baseline_name=trusted_baseline_name,
            timeout_seconds=timeout_seconds,
        )
    except SandboxRunError as exc:
        if "timed out" in exc.reason:
            raise CandidateGuardReject("timeout") from exc
        typed_rejections = {
            65: "score_length_or_index_mismatch",
            66: "non_finite_scores",
            67: "degenerate_constant_scores",
            68: "forbidden_access",
        }
        if exc.exit_code in typed_rejections:
            raise CandidateGuardReject(typed_rejections[exc.exit_code]) from exc
        if exc.exit_code is not None:
            raise CandidateGuardReject("candidate_crash") from exc
        raise


def run_candidate_fit_score(
    runner: LocalDockerRunner,
    *,
    candidate_source: bytes,
    train: pd.DataFrame,
    evaluation: pd.DataFrame,
    context: FitContext,
    remaining_seconds: int = 90,
    fit_timeout_seconds: int = 60,
    score_timeout_seconds: int = 30,
    trusted_baseline_name: str | None = None,
    admission_check: Callable[[], None] | None = None,
) -> CandidateEvaluation:
    """Run typed fit and score in separate fresh containers and validate output."""
    if not isinstance(context, FitContext):
        raise CandidateGuardReject("invalid_fit_context")
    if (
        isinstance(remaining_seconds, bool)
        or not isinstance(remaining_seconds, int)
        or not 1 <= remaining_seconds <= 600
    ):
        raise ValueError("remaining evaluation deadline must be within 1..600 seconds")
    deadline = time.monotonic() + remaining_seconds
    allowed_columns = tuple(context.signals)
    if (
        not allowed_columns
        or len(allowed_columns) != len(set(allowed_columns))
        or any(signal not in allowed_columns for signal in context.regime_signals)
    ):
        raise CandidateGuardReject("invalid_fit_context")
    train_bytes = _arrow_bytes(train, allowed_sensor_columns=allowed_columns)
    eval_bytes = _arrow_bytes(evaluation, allowed_sensor_columns=allowed_columns)
    fit = _run_candidate_phase(
        runner,
        phase="fit",
        candidate_source=candidate_source,
        arrow_input=train_bytes,
        fit_artifact=None,
        context=context,
        timeout_seconds=min(fit_timeout_seconds, max(1, int(deadline - time.monotonic()))),
        trusted_baseline_name=trusted_baseline_name,
        admission_check=admission_check,
    )
    by_name = {artifact.name: artifact for artifact in fit.artifacts}
    if set(by_name) != {"model.bin", "policy.json"}:
        raise CandidateGuardReject("fit_artifact_set_mismatch")
    fit_artifact = by_name["model.bin"].content
    if not fit_artifact:
        raise CandidateGuardReject("empty_fit_artifact")
    policy = _strict_policy(by_name["policy.json"].content)
    remaining = int(deadline - time.monotonic())
    if remaining < 1:
        raise CandidateGuardReject("timeout")
    score = _run_candidate_phase(
        runner,
        phase="score",
        candidate_source=candidate_source,
        arrow_input=eval_bytes,
        fit_artifact=fit_artifact,
        context=context,
        timeout_seconds=min(score_timeout_seconds, remaining),
        trusted_baseline_name=trusted_baseline_name,
        admission_check=admission_check,
    )
    if len(score.artifacts) != 1 or score.artifacts[0].name != "scores.json":
        raise CandidateGuardReject("score_artifact_set_mismatch")
    score_document = score.artifacts[0].content
    artifact = _score_artifact(
        score_document,
        len(evaluation),
        allow_constant_scores=trusted_baseline_name is not None,
        expected_policy=policy,
    )
    return CandidateEvaluation(
        fit_artifact=fit_artifact,
        fit_artifact_sha256=hashlib.sha256(fit_artifact).hexdigest(),
        scores=tuple(artifact.scores),
        policy=policy,
        fit_seconds=fit.elapsed_seconds,
        score_seconds=score.elapsed_seconds,
        score_document=score_document,
        fit_container_name=fit.container_name,
        score_container_name=score.container_name,
    )


def _relative_difference(left: np.ndarray, right: np.ndarray) -> float:
    """Use relative maximum error; exact zero references require exact equality."""
    if left.shape != right.shape or not left.size:
        raise CandidateGuardReject("guard_vector_mismatch")
    if not np.isfinite(left).all() or not np.isfinite(right).all():
        raise CandidateGuardReject("non_finite_guard_vector")
    scale = float(max(np.max(np.abs(left)), np.max(np.abs(right))))
    if scale == 0.0:
        return 0.0
    reference_scale = float(np.max(np.abs(left)))
    if reference_scale == 0.0:
        return math.inf
    relative_reference = reference_scale / scale
    if relative_reference == 0.0:
        return math.inf
    return float(np.max(np.abs(left / scale - right / scale))) / relative_reference


def check_determinism(
    runner: LocalDockerRunner,
    *,
    candidate_source: bytes,
    train: pd.DataFrame,
    evaluation: pd.DataFrame,
    context: FitContext,
    remaining_seconds: int = 180,
    trusted_baseline_name: str | None = None,
    admission_check: Callable[[], None] | None = None,
) -> GuardResult:
    """Repeat full fit+score in new containers with the same seed and context."""
    if (
        isinstance(remaining_seconds, bool)
        or not isinstance(remaining_seconds, int)
        or not 1 <= remaining_seconds <= 600
    ):
        raise ValueError("remaining guard deadline must be within 1..600 seconds")
    deadline = time.monotonic() + remaining_seconds
    first = run_candidate_fit_score(
        runner,
        candidate_source=candidate_source,
        train=train,
        evaluation=evaluation,
        context=context,
        remaining_seconds=max(1, int(deadline - time.monotonic())),
        trusted_baseline_name=trusted_baseline_name,
        admission_check=admission_check,
    )
    remaining = int(deadline - time.monotonic())
    if remaining < 1:
        raise CandidateGuardReject("timeout")
    second = run_candidate_fit_score(
        runner,
        candidate_source=candidate_source,
        train=train,
        evaluation=evaluation,
        context=context,
        remaining_seconds=remaining,
        trusted_baseline_name=trusted_baseline_name,
        admission_check=admission_check,
    )
    delta = _relative_difference(np.asarray(first.scores), np.asarray(second.scores))
    same_policy = first.policy == second.policy
    passed = delta <= DETERMINISM_REL_TOLERANCE and same_policy
    return GuardResult(
        passed=passed,
        code="determinism_pass" if passed else "determinism",
        value=delta,
    )


def _perturb_future(frame: pd.DataFrame, cut: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    values = frame.to_numpy(dtype=np.float64, copy=True)
    tail = values[cut:]
    if not len(tail):
        raise CandidateGuardReject("invalid_causality_cut")
    changed = tail[rng.permutation(len(tail))]
    changed = changed * rng.uniform(0.5, 2.0) + rng.normal(0.0, 1.0, changed.shape)
    values[cut:] = changed
    return pd.DataFrame(values, columns=frame.columns)


def check_causality(
    runner: LocalDockerRunner,
    *,
    candidate_source: bytes,
    train: pd.DataFrame,
    evaluation: pd.DataFrame,
    context: FitContext,
    cuts: tuple[float, ...] = (0.50, 0.70, 0.85),
    remaining_seconds: int = 180,
    trusted_baseline_name: str | None = None,
    admission_check: Callable[[], None] | None = None,
) -> GuardResult:
    """Test fresh score containers restored from one unchanged opaque fit artifact."""
    if (
        isinstance(remaining_seconds, bool)
        or not isinstance(remaining_seconds, int)
        or not 1 <= remaining_seconds <= 600
    ):
        raise ValueError("remaining guard deadline must be within 1..600 seconds")
    deadline = time.monotonic() + remaining_seconds
    fit = _run_candidate_phase(
        runner,
        phase="fit",
        candidate_source=candidate_source,
        arrow_input=_arrow_bytes(train, allowed_sensor_columns=tuple(context.signals)),
        fit_artifact=None,
        context=context,
        timeout_seconds=min(60, max(1, int(deadline - time.monotonic()))),
        trusted_baseline_name=trusted_baseline_name,
        admission_check=admission_check,
    )
    artifacts = {item.name: item.content for item in fit.artifacts}
    if set(artifacts) != {"model.bin", "policy.json"}:
        raise CandidateGuardReject("fit_artifact_set_mismatch")
    eval_bytes = _arrow_bytes(evaluation, allowed_sensor_columns=tuple(context.signals))
    expected_policy = _strict_policy(artifacts["policy.json"])

    def score(data: bytes) -> np.ndarray:
        remaining = int(deadline - time.monotonic())
        if remaining < 1:
            raise CandidateGuardReject("timeout")
        result = _run_candidate_phase(
            runner,
            phase="score",
            candidate_source=candidate_source,
            arrow_input=data,
            fit_artifact=artifacts["model.bin"],
            context=context,
            timeout_seconds=min(30, remaining),
            trusted_baseline_name=trusted_baseline_name,
            admission_check=admission_check,
        )
        docs = {item.name: item.content for item in result.artifacts}
        if set(docs) != {"scores.json"}:
            raise CandidateGuardReject("score_artifact_set_mismatch")
        return np.asarray(
            _score_artifact(
                docs["scores.json"],
                len(evaluation),
                allow_constant_scores=trusted_baseline_name is not None,
                expected_policy=expected_policy,
            ).scores
        )

    reference = score(eval_bytes)
    max_delta = 0.0
    for index, fraction in enumerate(cuts):
        if not 0.0 < fraction < 1.0:
            raise ValueError("causality cuts must be strictly within (0, 1)")
        cut = int(len(evaluation) * fraction)
        if cut < 1 or cut >= len(evaluation):
            raise CandidateGuardReject("invalid_causality_cut")
        changed = _perturb_future(evaluation, cut, context.seed + index)
        perturbed = score(_arrow_bytes(changed, allowed_sensor_columns=tuple(context.signals)))
        delta = _relative_difference(reference[:cut], perturbed[:cut])
        max_delta = max(max_delta, delta)
    return GuardResult(
        passed=max_delta <= CAUSALITY_REL_TOLERANCE,
        code="causality_pass" if max_delta <= CAUSALITY_REL_TOLERANCE else "causality",
        value=max_delta,
    )


def check_position_bias(
    task_scores: tuple[tuple[bool, tuple[float, ...]], ...], *, threshold: float = 0.8
) -> GuardResult:
    """Reject suite-wide temporal ranking only when enough eligible tasks are NRM."""
    eligible = [scores for is_nrm, scores in task_scores if is_nrm]
    if not eligible:
        return GuardResult(True, "position_bias_not_applicable")
    biased = 0
    correlations: list[float] = []
    for scores in eligible:
        values = np.asarray(scores, dtype=np.float64)
        if values.ndim != 1 or values.size < 3 or not np.isfinite(values).all():
            raise CandidateGuardReject("invalid_position_bias_vector")
        if np.all(values == values[0]):
            correlation = 0.0
        else:
            result = spearmanr(np.arange(values.size, dtype=np.float64), values)
            correlation = abs(float(result.statistic)) if np.isfinite(result.statistic) else 0.0
        correlations.append(correlation)
        biased += correlation > threshold
    fraction = biased / len(eligible)
    passed = fraction < 0.5
    return GuardResult(
        passed,
        "position_bias_pass" if passed else "position_bias",
        max(correlations, default=0.0),
    )


def check_complete_suite_position_bias(
    task_scores: tuple[tuple[bool, tuple[float, ...]], ...], *, expected_nrm_tasks: int
) -> GuardResult:
    """Apply the NRM guard only after the trusted plan's NRM outputs are complete."""
    if (
        isinstance(expected_nrm_tasks, bool)
        or not isinstance(expected_nrm_tasks, int)
        or expected_nrm_tasks < 0
    ):
        raise ValueError("expected NRM task count must be a non-negative integer")
    observed = sum(1 for is_nrm, _ in task_scores if is_nrm)
    if observed != expected_nrm_tasks:
        return GuardResult(False, "position_bias_incomplete_suite", float(observed))
    return check_position_bias(task_scores)


def _normalize_timestamp(value: str | int | float) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            timestamp = float(value)
            if not math.isfinite(timestamp):
                return None
            return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    try:
        return parsed.astimezone(UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _timestamp_value(value: str | int | float) -> datetime | None:
    normalized = _normalize_timestamp(value)
    if normalized is None:
        return None
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def check_hardcoding(
    source: bytes,
    *,
    task_ids: frozenset[str],
    evaluation_instants: frozenset[str | int | float],
    evaluation_intervals: tuple[tuple[str | int | float, str | int | float], ...] = (),
) -> GuardResult:
    """Reject task/time literals and numeric literal sequences of length 50+."""
    try:
        tree = ast.parse(source.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise CandidateGuardReject("invalid_source") from exc
    normalized_instants = {
        stamp for value in evaluation_instants if (stamp := _timestamp_value(value)) is not None
    }
    normalized_intervals: list[tuple[datetime, datetime]] = []
    for start, end in evaluation_intervals:
        normalized_start = _timestamp_value(start)
        normalized_end = _timestamp_value(end)
        if normalized_start is None or normalized_end is None or normalized_start > normalized_end:
            raise ValueError("evaluation intervals need ordered, timezone-aware endpoints")
        normalized_intervals.append((normalized_start, normalized_end))

    def numeric_literal(node: ast.expr) -> bool:
        if isinstance(node, ast.Constant):
            return isinstance(node.value, (int, float)) and not isinstance(node.value, bool)
        return (
            isinstance(node, ast.UnaryOp)
            and isinstance(node.op, (ast.USub, ast.UAdd))
            and isinstance(node.operand, ast.Constant)
            and isinstance(node.operand.value, (int, float))
            and not isinstance(node.operand.value, bool)
        )

    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)) and len(node.elts) >= 50:
            if all(numeric_literal(item) for item in node.elts):
                return GuardResult(False, "hardcoding_numeric_sequence")
        if isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, str) and value in task_ids:
                return GuardResult(False, "hardcoding_task_id")
            if isinstance(value, (str, int, float)):
                stamp = _timestamp_value(value)
                if stamp is not None and (
                    stamp in normalized_instants
                    or any(start <= stamp <= end for start, end in normalized_intervals)
                ):
                    return GuardResult(False, "hardcoding_timestamp")
    return GuardResult(True, "hardcoding_pass")


def run_guarded_seed_evaluation(
    runner: LocalDockerRunner,
    *,
    candidate_source: bytes,
    train: pd.DataFrame,
    evaluation: pd.DataFrame,
    context: FitContext,
    task_ids: frozenset[str],
    evaluation_instants: frozenset[str | int | float],
    evaluation_intervals: tuple[tuple[str | int | float, str | int | float], ...] = (),
    remaining_seconds: int = 600,
    trusted_baseline_name: str | None = None,
    admission_check: Callable[[], None] | None = None,
) -> GuardedCandidateEvaluation:
    """Run all per-seed guards before returning output eligible for Scorer enqueue."""
    if (
        isinstance(remaining_seconds, bool)
        or not isinstance(remaining_seconds, int)
        or not 1 <= remaining_seconds <= 600
    ):
        raise ValueError("guarded seed deadline must be within 1..600 seconds")
    deadline = time.monotonic() + remaining_seconds
    hardcoding = check_hardcoding(
        candidate_source,
        task_ids=task_ids,
        evaluation_instants=evaluation_instants,
        evaluation_intervals=evaluation_intervals,
    )
    if not hardcoding.passed:
        raise CandidateGuardReject(hardcoding.code)

    def remaining() -> int:
        seconds = int(deadline - time.monotonic())
        if seconds < 1:
            raise CandidateGuardReject("timeout")
        return seconds

    candidate = run_candidate_fit_score(
        runner,
        candidate_source=candidate_source,
        train=train,
        evaluation=evaluation,
        context=context,
        remaining_seconds=remaining(),
        trusted_baseline_name=trusted_baseline_name,
        admission_check=admission_check,
    )
    determinism = check_determinism(
        runner,
        candidate_source=candidate_source,
        train=train,
        evaluation=evaluation,
        context=context,
        remaining_seconds=remaining(),
        trusted_baseline_name=trusted_baseline_name,
        admission_check=admission_check,
    )
    if not determinism.passed:
        raise CandidateGuardReject(determinism.code)
    causality = check_causality(
        runner,
        candidate_source=candidate_source,
        train=train,
        evaluation=evaluation,
        context=context,
        remaining_seconds=remaining(),
        trusted_baseline_name=trusted_baseline_name,
        admission_check=admission_check,
    )
    if not causality.passed:
        raise CandidateGuardReject(causality.code)
    remaining()
    return GuardedCandidateEvaluation(
        evaluation=candidate,
        determinism=determinism,
        causality=causality,
        hardcoding=hardcoding,
    )
