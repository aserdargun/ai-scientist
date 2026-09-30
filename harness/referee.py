"""Deterministic paired bootstrap Referee; KEEP means dev selection only."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

import numpy as np
from numpy.typing import ArrayLike, NDArray
from pydantic import ValidationError

from harness.contracts import Decision, ReplayManifest


def _finite_vector(name: str, values: ArrayLike) -> NDArray[np.float64]:
    """Convert a one-dimensional finite input vector or fail closed."""
    vector = np.asarray(values, dtype=np.float64)
    if vector.ndim != 1 or vector.size == 0:
        raise ValueError(f"{name} tek boyutlu ve boş olmayan dizi olmalı")
    if not np.isfinite(vector).all():
        raise ValueError(f"{name} yalnız sonlu değer içermeli")
    return vector


# The Appendix C contract intentionally keeps a flat keyword-only API for exact replay.
# pylint: disable=too-many-arguments,too-many-locals,too-many-return-statements
def decide(
    parent: ArrayLike,
    child: ArrayLike,
    weights: ArrayLike,
    *,
    eps: float,
    noise_sd: float,
    simpler: bool,
    guards_ok: bool,
    max_task_drop: float = 0.5,
    best_suite: float | None = None,
    n_boot: int = 4000,
    seed: int = 0,
) -> Decision:
    """Return a replayable decision; invalid inputs become a strict-JSON REJECT."""
    if not isinstance(simpler, bool) or not isinstance(guards_ok, bool):
        return Decision("REJECT", None, None, "invalid_boolean_parameter")
    if any(isinstance(value, bool) for value in (eps, noise_sd, max_task_drop)):
        return Decision("REJECT", None, None, "invalid_parameters")
    if not guards_ok:
        return Decision("REJECT", None, None, "guardrail")
    try:
        p = _finite_vector("parent", parent)
        c = _finite_vector("child", child)
        w = _finite_vector("weights", weights)
    except (TypeError, ValueError) as exc:
        return Decision("REJECT", None, None, f"invalid_input:{exc}")
    if p.size != c.size or p.size != w.size:
        return Decision("REJECT", None, None, "length_mismatch")
    if np.any(w <= 0.0):
        return Decision("REJECT", None, None, "invalid_weights")
    if (
        not np.isfinite((eps, noise_sd, max_task_drop)).all()
        or eps < 0
        or noise_sd < 0
        or max_task_drop < 0
    ):
        return Decision("REJECT", None, None, "invalid_parameters")
    if best_suite is not None and not np.isfinite(best_suite):
        return Decision("REJECT", None, None, "invalid_best_suite")
    if isinstance(n_boot, bool) or not isinstance(n_boot, int) or n_boot < 1:
        return Decision("REJECT", None, None, "invalid_bootstrap_count")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        return Decision("REJECT", None, None, "invalid_seed")
    if np.any((p < -1.0) | (p > 3.0)) or np.any((c < -1.0) | (c > 3.0)):
        return Decision("REJECT", None, None, "score_out_of_domain")

    delta_by_task = c - p
    stable_weights = w / float(w.max())
    if not np.isfinite(stable_weights).all() or float(stable_weights.sum()) <= 0.0:
        return Decision("REJECT", None, None, "invalid_weights")
    with np.errstate(over="raise", invalid="raise", divide="raise"):
        try:
            total_weight = float(stable_weights.sum())
            delta = float(np.dot(stable_weights, delta_by_task) / total_weight)
            rng = np.random.default_rng(seed)
            indices = rng.integers(0, delta_by_task.size, size=(n_boot, delta_by_task.size))
            sampled_weights = stable_weights[indices]
            bootstrap = (
                (delta_by_task[indices] * sampled_weights).sum(axis=1)
                / sampled_weights.sum(axis=1)
            )
            ci_low = float(np.quantile(bootstrap, 0.10))
        except FloatingPointError:
            return Decision("REJECT", None, None, "numeric_overflow")
    if float(delta_by_task.min()) < -max_task_drop:
        return Decision("DISCARD", delta, ci_low, "tek görevde büyük gerileme")
    if delta >= max(eps, 2.0 * noise_sd) and ci_low > 0.0:
        return Decision("KEEP", delta, ci_low, "anlamlı iyileşme; yalnız dev seçimi")
    child_suite = float(np.dot(stable_weights, c) / total_weight)
    floor_ok = best_suite is None or child_suite >= best_suite - eps
    if simpler and floor_ok and delta >= -eps / 2 and ci_low >= -eps:
        return Decision("KEEP_SIMPLER", delta, ci_low, "sadeleşme, anlamlı kayıp yok")
    return Decision("DISCARD", delta, ci_low, "anlamlı iyileşme yok")


def replay_decision(inputs: Mapping[str, object]) -> Decision:
    """Recompute a decision from its explicit replay manifest."""
    try:
        manifest = ReplayManifest.model_validate(inputs, strict=True)
    except ValidationError as exc:
        raise ValueError(f"invalid replay manifest: {exc}") from exc
    return decide(
        cast(ArrayLike, manifest.parent),
        cast(ArrayLike, manifest.child),
        cast(ArrayLike, manifest.weights),
        eps=manifest.eps,
        noise_sd=manifest.noise_sd,
        simpler=manifest.simpler,
        guards_ok=manifest.guards_ok,
        max_task_drop=manifest.max_task_drop,
        best_suite=manifest.best_suite,
        n_boot=manifest.n_boot,
        seed=manifest.seed,
    )


def score_vector(values: Sequence[float]) -> tuple[float, ...]:
    """Validate a candidate score vector at the untrusted boundary."""
    return tuple(float(item) for item in _finite_vector("scores", values))
