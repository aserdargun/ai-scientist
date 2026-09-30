"""Mask-aware alarm and event-window scoring on the original time grid."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from numpy.typing import ArrayLike, NDArray
from scipy.stats import rankdata

from harness.contracts import AlarmPolicy


class InvalidMetricError(ValueError):
    """Scorer cannot calculate a meaningful metric for the supplied task."""


def position_bias(scores: ArrayLike) -> float:
    """Return absolute Spearman correlation between score and source order."""
    values = np.asarray(scores, dtype=np.float64)
    if values.ndim != 1 or values.size < 2 or not np.isfinite(values).all():
        raise InvalidMetricError("position-bias scores must be a finite ordered vector")
    ranks = rankdata(values, method="average")
    if np.std(ranks) == 0.0:
        return 0.0
    bias = float(abs(np.corrcoef(ranks, np.arange(values.size, dtype=np.float64))[0, 1]))
    if not np.isfinite(bias):
        raise InvalidMetricError("position bias is not finite")
    return bias


def _binary_vector(name: str, values: ArrayLike) -> NDArray[np.bool_]:
    """Validate a non-empty one-dimensional binary mask."""
    array = np.asarray(values)
    if array.ndim != 1 or array.size == 0:
        raise InvalidMetricError(f"{name} must be a non-empty one-dimensional mask")
    if not np.isin(array, (0, 1, False, True)).all():
        raise InvalidMetricError(f"{name} must be binary")
    return array.astype(np.bool_, copy=False)


def _validate_sampling(sampling_s: int) -> None:
    """Require a positive integer sampling interval."""
    if isinstance(sampling_s, bool) or not isinstance(sampling_s, int) or sampling_s <= 0:
        raise InvalidMetricError("sampling_s must be a positive integer")


def apply_alarm_policy(scores: ArrayLike, policy: AlarmPolicy) -> NDArray[np.bool_]:
    """Apply dwell/hysteresis; NaN mask points preserve alarm state and reset dwell."""
    values = np.asarray(scores, dtype=np.float64)
    if values.ndim != 1 or values.size == 0:
        raise InvalidMetricError("scores must be a non-empty vector")
    if np.isinf(values).any():
        raise InvalidMetricError("infinite score is invalid; NaN alone denotes a mask")
    result = np.zeros(values.size, dtype=np.bool_)
    active = False
    run = 0
    for index, value in enumerate(values):
        if np.isnan(value):
            run = 0
        elif active:
            if value < policy.release:
                active = False
                run = 0
        else:
            run = run + 1 if value >= policy.threshold else 0
            active = run >= policy.dwell
        result[index] = active
    return result


def _onsets(on: ArrayLike) -> NDArray[np.bool_]:
    """Mark rising edges without changing the input time axis."""
    active = _binary_vector("on", on)
    onset = np.zeros_like(active)
    onset[0] = active[0]
    onset[1:] = active[1:] & ~active[:-1]
    return onset


def alarm_stats(
    on: ArrayLike,
    positive: ArrayLike,
    masked: ArrayLike,
    sampling_s: int,
) -> tuple[float, float]:
    """Return false alarms/day and healthy-period alarm duty fraction.

    An onset at a masked index is not a false alarm. Alarm time is counted only where a
    healthy sample is observed; masked samples remain at their original temporal positions.
    """
    _validate_sampling(sampling_s)
    active = _binary_vector("on", on)
    anomalous = _binary_vector("positive", positive)
    invalid = _binary_vector("masked", masked)
    if active.size != anomalous.size or active.size != invalid.size:
        raise InvalidMetricError("alarm, positive and mask arrays must have equal lengths")
    healthy = ~anomalous & ~invalid
    healthy_count = int(healthy.sum())
    if healthy_count == 0:
        raise InvalidMetricError("task has zero healthy exposure")
    healthy_days = healthy_count * sampling_s / 86_400.0
    false_alarm_count = int((_onsets(active) & healthy).sum())
    false_alarms_per_day = false_alarm_count / healthy_days
    duty = float((active & healthy).sum()) / healthy_count
    return float(false_alarms_per_day), duty


def _validated_failures(
    failures: Sequence[tuple[int, int]], length: int
) -> tuple[tuple[int, int], ...]:
    """Check inclusive, ordered, non-overlapping event windows."""
    checked: list[tuple[int, int]] = []
    previous_end = -1
    for start, end in failures:
        if isinstance(start, bool) or isinstance(end, bool):
            raise InvalidMetricError("failure indices must be integers")
        if not isinstance(start, int) or not isinstance(end, int):
            raise InvalidMetricError("failure indices must be integers")
        if start < 0 or end <= start or end >= length or start <= previous_end:
            raise InvalidMetricError(
                "failure windows must be ordered non-overlapping valid indices"
            )
        checked.append((start, end))
        previous_end = end
    if not checked:
        raise InvalidMetricError("PDM task requires at least one failure window")
    return tuple(checked)


def pdm_earliness(
    on: ArrayLike,
    failures: Sequence[tuple[int, int]],
    masked: ArrayLike,
) -> float:
    """Credit only an unmasked alarm onset inside each inclusive pre-failure window."""
    active = _binary_vector("on", on)
    invalid = _binary_vector("masked", masked)
    if active.size != invalid.size:
        raise InvalidMetricError("alarm and mask arrays must have equal lengths")
    events = _validated_failures(failures, active.size)
    valid_onsets = _onsets(active) & ~invalid
    credit_values: list[float] = []
    for start, failure in events:
        hit = np.flatnonzero(valid_onsets[start : failure + 1])
        if hit.size == 0:
            credit_values.append(0.0)
        else:
            onset = start + int(hit[0])
            credit_values.append((failure - onset) / (failure - start))
    return float(np.mean(credit_values))


# The PDM surface follows the spec's positional task arrays and separate policy limits.
# pylint: disable=too-many-arguments
def pdm_task_score(
    on: ArrayLike,
    failures: Sequence[tuple[int, int]],
    masked: ArrayLike,
    sampling_s: int,
    *,
    max_fa: float,
    max_duty: float = 0.05,
) -> tuple[float, float, float]:
    """Score early warning or return zero when false-alarm/duty limits are exceeded."""
    active = _binary_vector("on", on)
    invalid = _binary_vector("masked", masked)
    events = _validated_failures(failures, active.size)
    if invalid.size != active.size:
        raise InvalidMetricError("alarm and mask arrays must have equal lengths")
    if not np.isfinite((max_fa, max_duty)).all() or max_fa < 0 or not 0 <= max_duty <= 1:
        raise InvalidMetricError("invalid PDM limits")
    positive = np.zeros(active.size, dtype=np.bool_)
    for start, failure in events:
        positive[start : failure + 1] = True
    false_alarms_per_day, duty = alarm_stats(active, positive, invalid, sampling_s)
    score = pdm_earliness(active, events, invalid)
    if false_alarms_per_day > max_fa or duty > max_duty:
        score = 0.0
    return score, false_alarms_per_day, duty


def nrm_task_score(
    on: ArrayLike,
    masked: ArrayLike,
    sampling_s: int,
    *,
    max_duty: float = 0.05,
) -> tuple[float, float, float]:
    """Score normal-series silence with an explicit healthy exposure denominator."""
    active = _binary_vector("on", on)
    invalid = _binary_vector("masked", masked)
    if active.size != invalid.size:
        raise InvalidMetricError("alarm and mask arrays must have equal lengths")
    if not np.isfinite(max_duty) or not 0 <= max_duty <= 1:
        raise InvalidMetricError("invalid NRM duty limit")
    false_alarms_per_day, duty = alarm_stats(
        active, np.zeros(active.size, dtype=np.bool_), invalid, sampling_s
    )
    score = float(np.exp(-false_alarms_per_day)) if duty <= max_duty else 0.0
    return score, false_alarms_per_day, duty
