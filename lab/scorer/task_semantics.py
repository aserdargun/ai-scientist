"""Shared, fail-closed handling for Scorer-only temporal semantics."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime, timedelta

import numpy as np

from harness.metrics import VUSResult, vus_metrics


def parse_canonical_time_axis(values: Sequence[str]) -> list[datetime]:
    """Preserve naive source axes or homogeneous explicit UTC without rewriting bytes."""
    parsed = [datetime.fromisoformat(value) for value in values]
    if any(value.tzinfo is not None for value in parsed) and not all(
        value.tzinfo is not None
        and value.utcoffset() == timedelta(0)
        and raw.endswith("+00:00")
        and value.isoformat() == raw
        for raw, value in zip(values, parsed, strict=True)
    ):
        raise ValueError("evaluation times must be homogeneous naive or canonical UTC timestamps")
    return parsed


def validate_evt_semantics(
    *,
    sample_count: int,
    sampling_s: object,
    evaluation_times: object,
    masked_samples: object,
    failure_windows: object,
    semantics_sha256: object,
) -> list[bool]:
    """Validate the Scorer's EVT axis/mask record and return its strict mask."""
    if (
        isinstance(sampling_s, bool)
        or (sampling_s is not None and (not isinstance(sampling_s, int) or sampling_s <= 0))
        or not isinstance(evaluation_times, list)
        or len(evaluation_times) != sample_count
        or not isinstance(masked_samples, list)
        or len(masked_samples) != sample_count
        or any(type(masked) is not bool for masked in masked_samples)
        or not isinstance(failure_windows, list)
        or failure_windows
        or not isinstance(semantics_sha256, str)
        or len(semantics_sha256) != 64
    ):
        raise ValueError("EVT semantics are malformed or misaligned")
    if all(isinstance(value, str) and value for value in evaluation_times):
        try:
            parsed = parse_canonical_time_axis(evaluation_times)
        except ValueError as exc:
            raise ValueError("EVT evaluation time axis is malformed") from exc
        deltas = [
            (right - left).total_seconds() for left, right in zip(parsed, parsed[1:], strict=False)
        ]
    elif all(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and np.isfinite(float(value))
        for value in evaluation_times
    ):
        deltas = [
            float(right) - float(left)
            for left, right in zip(evaluation_times, evaluation_times[1:], strict=False)
        ]
    else:
        raise ValueError("EVT evaluation time axis has unsupported values")
    for index, delta in enumerate(deltas):
        # A masked sample may be the source-declared clock reset (for example,
        # GECCO's reset row). Validate each unmasked contiguous segment while
        # retaining the original sample-index alignment for the mask itself.
        if masked_samples[index] or masked_samples[index + 1]:
            continue
        if delta <= 0 or (sampling_s is not None and delta != sampling_s):
            raise ValueError("EVT evaluation axis violates its registered ordering/cadence")
    payload = {
        "schema": "public-task-semantics.v1",
        "task_family": "EVT",
        "sampling_s": sampling_s,
        "evaluation_times": evaluation_times,
        "masked_samples": masked_samples,
        "failure_windows": failure_windows,
    }
    actual = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    ).hexdigest()
    if actual != semantics_sha256:
        raise ValueError("EVT semantics digest differs from its registered content")
    return masked_samples


def score_masked_vus(
    labels: Sequence[bool] | np.ndarray,
    scores: Sequence[float] | np.ndarray,
    *,
    masks: Sequence[bool] | None,
    sliding_window: int,
    threshold_count: int = 250,
) -> tuple[VUSResult, int]:
    """Score aligned unmasked samples and reject unusable masked grids."""
    label_values = np.asarray(labels)
    score_values = np.asarray(scores, dtype=np.float64)
    if label_values.ndim != 1 or score_values.ndim != 1:
        raise ValueError("EVT labels and scores must be one-dimensional")
    if label_values.size == 0 or label_values.size != score_values.size:
        raise ValueError("EVT labels and scores must have equal non-zero length")
    if masks is None:
        mask_values = np.zeros(label_values.size, dtype=np.bool_)
    else:
        if len(masks) != label_values.size or any(type(value) is not bool for value in masks):
            raise ValueError("EVT mask must contain one strict boolean per sample")
        mask_values = np.asarray(masks, dtype=np.bool_)
    if not np.isin(label_values, (False, True, 0, 1)).all():
        raise ValueError("EVT labels must be binary")
    if not np.isfinite(score_values).all():
        raise ValueError("EVT scores must be finite")
    keep = ~mask_values
    kept_count = int(np.count_nonzero(keep))
    if (
        isinstance(sliding_window, bool)
        or not isinstance(sliding_window, int)
        or sliding_window < 1
        or kept_count < sliding_window
    ):
        raise ValueError("unmasked EVT grid is shorter than its fixed training window")
    result = vus_metrics(
        label_values[keep],
        score_values[keep],
        sliding_window=sliding_window,
        threshold_count=threshold_count,
    )
    return result, int(label_values.size - kept_count)
