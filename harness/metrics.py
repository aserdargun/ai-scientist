"""Bounded wrapper around the vendored TSB-AD 1.5 VUS implementation."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import ArrayLike

from vendor.tsb_ad_eval.basic_metrics import generate_curve


@dataclass(frozen=True, slots=True)
class VUSResult:
    """Threshold-independent volume under the surface metrics."""

    vus_roc: float
    vus_pr: float


def vus_metrics(
    labels: ArrayLike,
    scores: ArrayLike,
    *,
    sliding_window: int,
    threshold_count: int = 250,
) -> VUSResult:
    """Calculate VUS-ROC/PR with validated arrays and explicit oracle version."""
    label_vector = np.asarray(labels)
    score_vector = np.asarray(scores, dtype=np.float64)
    if label_vector.ndim != 1 or score_vector.ndim != 1:
        raise ValueError("labels and scores must be one-dimensional")
    if label_vector.size != score_vector.size or label_vector.size == 0:
        raise ValueError("labels and scores must have equal non-zero lengths")
    if not np.isfinite(label_vector).all() or not np.isfinite(score_vector).all():
        raise ValueError("labels and scores must be finite")
    if not np.isin(label_vector, (0, 1)).all():
        raise ValueError("labels must be binary")
    if not isinstance(sliding_window, int) or isinstance(sliding_window, bool):
        raise ValueError("sliding_window must be an integer")
    if not isinstance(threshold_count, int) or isinstance(threshold_count, bool):
        raise ValueError("threshold_count must be an integer")
    if sliding_window < 1 or sliding_window > len(label_vector) or threshold_count < 2:
        raise ValueError("sliding_window or threshold_count is outside supported bounds")
    _, _, _, _, _, _, vus_roc, vus_pr = generate_curve(
        label_vector.astype(np.int8, copy=False),
        score_vector,
        sliding_window,
        "opt",
        threshold_count,
    )
    metrics = (float(vus_roc), float(vus_pr))
    if not np.isfinite(metrics).all():
        raise ValueError("vendored VUS output is non-finite")
    return VUSResult(vus_roc=metrics[0], vus_pr=metrics[1])
