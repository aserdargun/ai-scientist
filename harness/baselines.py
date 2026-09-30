"""Versioned causal baseline detectors and per-task score normalization."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import ClassVar

import numpy as np
import pandas as pd
from numpy.typing import NDArray
from sklearn.ensemble import IsolationForest

from harness.contracts import ADPipeline, AlarmPolicy, FitContext

BASELINE_VERSIONS: dict[str, str] = {
    "robust_z": "robust-z.v1",
    "iforest": "isolation-forest-sklearn.v1",
    "ecod_train_frozen": "ecod-train-frozen.v1",
}

NORMALIZATION_FLOORS: dict[str, float] = {
    "EVT": 0.02,
    "PDM": 0.10,
    "NRM": 0.10,
    "C-EXT": 0.05,
    "C-UTIL": 0.02,
}

BASELINE_SEEDS = (0, 1, 2)
REFERENCE_SUMMARY_VERSION = "mean-seeds-0-2.v1"
CHAMPION_NOISE_VERSION = "population-ddof0-seeds-0-2.v1"


@dataclass(frozen=True, slots=True)
class FrozenTaskReferences:
    """Per-task baseline measurements frozen before candidate evaluation."""

    family: str
    seed_scores: Mapping[str, tuple[float, float, float]]
    seed_means: Mapping[str, float]
    base_score: float
    reference_score: float
    summary_version: str = REFERENCE_SUMMARY_VERSION


def freeze_task_references(
    baseline_seed_scores: Mapping[str, Mapping[int, float]], *, family: str
) -> FrozenTaskReferences:
    """Freeze base/ref from exactly baseline seeds 0, 1, 2 using arithmetic means."""
    if family not in NORMALIZATION_FLOORS:
        raise ValueError("unknown task family")
    allowed = {"robust_z", "iforest", "ecod_train_frozen"}
    if set(baseline_seed_scores) != allowed:
        raise ValueError("all three allowlisted baseline algorithms are required")
    normalized: dict[str, tuple[float, float, float]] = {}
    means: dict[str, float] = {}
    for name in sorted(allowed):
        per_seed = baseline_seed_scores[name]
        if set(per_seed) != set(BASELINE_SEEDS):
            raise ValueError("baseline measurements require seeds 0, 1, and 2 exactly")
        scores = (
            float(per_seed[0]),
            float(per_seed[1]),
            float(per_seed[2]),
        )
        if not np.isfinite(scores).all():
            raise ValueError("baseline seed scores must be finite")
        normalized[name] = scores
        means[name] = float(np.mean(scores))
    base = means["robust_z"]
    reference = max(base, *(means[name] for name in allowed))
    return FrozenTaskReferences(
        family=family,
        seed_scores=MappingProxyType(normalized),
        seed_means=MappingProxyType(means),
        base_score=base,
        reference_score=reference,
    )


def champion_noise_sd(champion_suite_scores: Mapping[int, float]) -> float:
    """Measure champion noise from its separate suite-level seeds (population SD)."""
    if set(champion_suite_scores) != set(BASELINE_SEEDS):
        raise ValueError("champion noise requires suite scores for seeds 0, 1, and 2 exactly")
    scores = np.asarray([champion_suite_scores[seed] for seed in BASELINE_SEEDS], dtype=np.float64)
    if not np.isfinite(scores).all():
        raise ValueError("champion suite seed scores must be finite")
    if scores[0] == scores[1] == scores[2]:
        return 0.0
    return float(np.std(scores, ddof=0))


def _matrix(frame: pd.DataFrame, columns: tuple[str, ...]) -> NDArray[np.float64]:
    if not isinstance(frame, pd.DataFrame) or not columns or len(columns) != len(set(columns)):
        raise ValueError("baseline input requires named, unique sensor columns")
    if tuple(frame.columns) != columns or len(frame) == 0:
        raise ValueError("baseline input columns or row count differ from the fitted contract")
    values = frame.to_numpy(dtype=np.float64, copy=True)
    if values.ndim != 2 or not np.isfinite(values).all():
        raise ValueError("baseline input must be finite numeric sensor data")
    return values


def _alarm_policy(train_scores: NDArray[np.float64]) -> AlarmPolicy:
    scores = np.asarray(train_scores, dtype=np.float64)
    if scores.ndim != 1 or scores.size == 0 or not np.isfinite(scores).all():
        raise ValueError("baseline alarm policy requires finite train scores")
    high = float(np.quantile(scores, 0.995, method="linear"))
    if float(np.min(scores)) == float(np.max(scores)):
        maximum = float(np.max(scores))
        if maximum == np.finfo(np.float64).max:
            raise ValueError("constant train scores have no finite alarm successor")
        high = float(np.nextafter(maximum, np.inf))
        # Keep the release threshold above the flat train value too. If release
        # were below it, the policy would latch after one onset on a flat series.
        return AlarmPolicy(threshold=high, release=high, dwell=1)
    spread = max(abs(high) * 0.1, np.finfo(np.float64).eps)
    low = high - spread
    if not np.isfinite(low) or low > high:
        raise ValueError("baseline alarm policy overflow")
    return AlarmPolicy(threshold=high, release=low, dwell=1)


class _Baseline:
    algorithm_version: ClassVar[str]

    def __init__(self) -> None:
        self._columns: tuple[str, ...] | None = None
        self._context: FitContext | None = None

    def fit(self, train: pd.DataFrame, ctx: FitContext) -> None:
        if not isinstance(ctx, FitContext):
            raise ValueError("baseline fit requires a typed FitContext")
        if tuple(train.columns) != ctx.signals:
            raise ValueError("baseline signals must match the trusted fit context")
        values = _matrix(train, ctx.signals)
        self._columns = ctx.signals
        self._context = ctx
        self._fit_values(values)

    def _fit_values(self, values: NDArray[np.float64]) -> None:
        raise NotImplementedError

    def score(self, data: pd.DataFrame) -> NDArray[np.float64]:
        if self._columns is None:
            raise ValueError("baseline must be fit before scoring")
        values = _matrix(data, self._columns)
        scores = np.asarray(self._score_values(values), dtype=np.float64)
        if scores.shape != (len(data),) or not np.isfinite(scores).all():
            raise ValueError("baseline produced invalid score values")
        return scores

    def _score_values(self, values: NDArray[np.float64]) -> NDArray[np.float64]:
        raise NotImplementedError

    def alarm_policy(self, train_scores: NDArray[np.float64]) -> AlarmPolicy:
        return _alarm_policy(train_scores)


class RobustZBaseline(_Baseline):
    """Train-median and MAD robust z-score, aggregated by maximum sensor score."""

    algorithm_version = BASELINE_VERSIONS["robust_z"]

    def _fit_values(self, values: NDArray[np.float64]) -> None:
        self.center_ = np.median(values, axis=0)
        deviations = np.abs(values - self.center_)
        scale = 1.4826 * np.median(deviations, axis=0)
        fallback = np.subtract(*np.percentile(values, [75, 25], axis=0)) / 1.349
        standard = np.std(values, axis=0)
        scale = np.where(scale > 0.0, scale, np.where(fallback > 0.0, fallback, standard))
        scale = np.where(scale > 0.0, scale, 1.0)
        if not np.isfinite(scale).all():
            raise ValueError("robust-z training scale is non-finite")
        self.scale_ = scale

    def _score_values(self, values: NDArray[np.float64]) -> NDArray[np.float64]:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            try:
                return np.max(np.abs((values - self.center_) / self.scale_), axis=1)
            except FloatingPointError as exc:
                raise ValueError("robust-z score overflow") from exc


class IsolationForestBaseline(_Baseline):
    """Single-threaded seeded sklearn Isolation Forest baseline."""

    algorithm_version = BASELINE_VERSIONS["iforest"]

    def _fit_values(self, values: NDArray[np.float64]) -> None:
        if len(values) < 2:
            raise ValueError("Isolation Forest requires at least two train rows")
        if self._context is None:
            raise ValueError("baseline fit context is unavailable")
        self.estimator_ = IsolationForest(
            n_estimators=100,
            max_samples=min(256, len(values)),
            contamination="auto",
            random_state=self._context.seed,
            n_jobs=1,
        ).fit(values)

    def _score_values(self, values: NDArray[np.float64]) -> NDArray[np.float64]:
        return -np.asarray(self.estimator_.decision_function(values), dtype=np.float64)


class ECODTrainFrozenBaseline(_Baseline):
    """Inductive ECOD adaptation with train-frozen inclusive empirical tails.

    This is an explicitly versioned causal adaptation, not PyOD's transductive
    ``decision_function``. Train skew signs and sorted empirical values are frozen
    by ``fit``. Tail probabilities use inclusive counts and floor ``1/(n+1)``.
    A zero train skew uses both tails, matching the cited ECOD skew formula.
    """

    algorithm_version = BASELINE_VERSIONS["ecod_train_frozen"]
    tail_floor_rule: ClassVar[str] = "1/(train_rows+1)"
    tie_rule: ClassVar[str] = "inclusive_le_and_ge"
    skew_rule: ClassVar[str] = "train_population_moment_sign_upstream_formula"

    def _fit_values(self, values: NDArray[np.float64]) -> None:
        self.sorted_train_ = tuple(np.sort(values[:, column]) for column in range(values.shape[1]))
        signs: list[int] = []
        for column in range(values.shape[1]):
            feature = values[:, column]
            centered = feature - float(np.mean(feature))
            variance = float(np.mean(centered * centered))
            if variance == 0.0:
                signs.append(0)
                continue
            skew = float(np.mean(centered * centered * centered) / variance**1.5)
            if not np.isfinite(skew):
                raise ValueError("ECOD train skew is non-finite")
            signs.append(int(np.sign(skew)))
        self.skew_signs_ = tuple(signs)
        self.floor_ = 1.0 / (len(values) + 1.0)

    def _score_values(self, values: NDArray[np.float64]) -> NDArray[np.float64]:
        output = np.zeros(len(values), dtype=np.float64)
        for column, train_sorted in enumerate(self.sorted_train_):
            feature = values[:, column]
            left_count = np.searchsorted(train_sorted, feature, side="right")
            right_count = len(train_sorted) - np.searchsorted(train_sorted, feature, side="left")
            left_probability = np.maximum(left_count / len(train_sorted), self.floor_)
            right_probability = np.maximum(right_count / len(train_sorted), self.floor_)
            left_tail = -np.log(left_probability)
            right_tail = -np.log(right_probability)
            skew_sign = self.skew_signs_[column]
            if skew_sign < 0:
                skew_score = left_tail
            elif skew_sign > 0:
                skew_score = right_tail
            else:
                skew_score = left_tail + right_tail
            output += np.maximum(skew_score, np.maximum(left_tail, right_tail))
        return output


def build_baseline(name: str) -> ADPipeline:
    """Return one allowlisted, versioned M0 baseline implementation."""
    factories: dict[str, type[_Baseline]] = {
        "robust_z": RobustZBaseline,
        "iforest": IsolationForestBaseline,
        "ecod_train_frozen": ECODTrainFrozenBaseline,
    }
    try:
        return factories[name]()
    except KeyError as exc:
        raise ValueError("unknown baseline name") from exc


def normalize_task_score(
    raw_score: float,
    base_score: float,
    reference_score: float,
    *,
    family: str,
) -> float:
    """Apply spec §3.2.2's frozen task-wise normalization and family floor."""
    values = (raw_score, base_score, reference_score)
    if not all(np.isfinite(value) for value in values):
        raise ValueError("normalization inputs must be finite")
    if reference_score < base_score:
        raise ValueError("reference score must be at least the robust-z base")
    try:
        floor = NORMALIZATION_FLOORS[family]
    except KeyError as exc:
        raise ValueError("unknown task family") from exc
    denominator = max(reference_score - base_score, floor)
    normalized = (raw_score - base_score) / denominator
    if not np.isfinite(normalized):
        raise ValueError("normalized task score overflow")
    return float(np.clip(normalized, -1.0, 3.0))
