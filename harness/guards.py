"""Deterministic harness guard helpers for causality regressions."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd

from harness.contracts import ADPipeline


def causality_violation(
    fitted_pipeline_factory: Callable[[], ADPipeline],
    data: pd.DataFrame,
    *,
    cut_frac: float = 0.7,
    lookahead: int = 0,
    seed: int = 0,
) -> float:
    """Compare independent fitted instances before/after a future perturbation.

    Each factory call must restore the frozen fit state in a separate candidate sandbox.
    The trusted process treats candidates as opaque and never deserializes their artifacts.
    """
    if not 0.0 < cut_frac < 1.0 or lookahead < 0 or len(data) < 2:
        raise ValueError("invalid causality test range")
    rng = np.random.default_rng(seed)
    cut = int(len(data) * cut_frac)
    tail = data.iloc[cut:].to_numpy(dtype=np.float64)
    perturbed_values = tail[rng.permutation(len(tail))] * rng.uniform(0.5, 2.0)
    perturbed_values += rng.normal(0.0, 1.0, tail.shape)
    perturbed = data.copy()
    perturbed.iloc[cut:, :] = perturbed_values
    keep = cut - lookahead
    if keep < 1:
        raise ValueError("causality prefix is empty")
    baseline_scores = np.asarray(fitted_pipeline_factory().score(data), dtype=np.float64)
    perturbed_scores = np.asarray(fitted_pipeline_factory().score(perturbed), dtype=np.float64)
    if baseline_scores.shape != (len(data),) or perturbed_scores.shape != (len(data),):
        raise ValueError("candidate score output length mismatch")
    if not np.isfinite(baseline_scores).all() or not np.isfinite(perturbed_scores).all():
        raise ValueError("candidate score output must be finite")
    return float(np.max(np.abs(baseline_scores[:keep] - perturbed_scores[:keep])))
