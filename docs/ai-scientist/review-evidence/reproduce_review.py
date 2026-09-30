"""Reproduce review findings against the exact Appendix C, not proposed fixes.

Run: OPENBLAS_NUM_THREADS=2 uv run --python 3.12 --with numpy==2.2.6
     --with pandas==2.2.3 docs/ai-scientist/review-evidence/reproduce_review.py
"""
from __future__ import annotations

import contextlib
import io
import json
import platform
import re
import sys
import types
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
spec = (HERE.parent / "01-ai-scientist-spec.md").read_text()
appendix = spec.split("## Ek C — Referans kod (test edilmiş)", 1)[1]
blocks = re.findall(r"```python\n(.*?)\n```", appendix, re.S)
module = types.ModuleType("contracts")
sys.modules["contracts"] = module
exec(compile(blocks[0], "spec-appendix-c", "exec"), module.__dict__)
log = io.StringIO()
with contextlib.redirect_stdout(log):
    exec(compile(blocks[1], "spec-appendix-c-tests", "exec"), {})
(HERE / "appendix-c-original-test-output.txt").write_text(log.getvalue())


class Stateful:
    """Causal output plus call counter exposes missing object isolation."""

    def __init__(self) -> None:
        self.calls = 0

    def score(self, data: pd.DataFrame) -> np.ndarray:
        self.calls += 1
        return data.iloc[:, 0].to_numpy() + self.calls


edge: dict[str, object] = {}
data = pd.DataFrame({"x": np.arange(30.0)})
edge["stateful_causal_violation"] = module.causality_violation(Stateful(), data)
on = np.zeros(100, dtype=bool)
on[60:65] = True
masked = np.zeros(100, dtype=bool)
masked[60:65] = True
edge["masked_alarm_pdm"] = module.pdm_task_score(on, [(50, 99)], masked, 600, max_fa=1.0)
on_all = np.ones(100, dtype=bool)
edge["no_healthy_time_pdm"] = module.pdm_task_score(
    on_all, [(0, 99)], np.zeros(100, dtype=bool), 600, max_fa=1.0
)
pol = module.AlarmPolicy(float("inf"), float("inf"), 1)
edge["infinite_alarm_threshold_accepted"] = bool(np.isinf(pol.threshold))
try:
    json.dumps(module.decide(np.ones(4), np.ones(4), np.ones(4), eps=.01,
                             noise_sd=0, simpler=False, guards_ok=False).__dict__, allow_nan=False)
except ValueError as error:
    edge["reject_not_strict_json"] = str(error)


def wilson(successes: int, total: int) -> list[float]:
    p = successes / total
    z = 1.959963984540054
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    half = z * np.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return [float(center - half), float(center + half)]


def monte_carlo(n: int, sigma: float, seed_corr: float, family_corr: float,
                noise_sd: float, trials: int = 10000) -> dict[str, object]:
    """Zero population-mean differences; four equal families, equal task weights.

    Bootstrap indices match decide(seed=0, n_boot=4000). Reuse those indices
    exactly as the reference does. A positive suite difference on the dev sample
    may still be a false generalization claim about new tasks.
    """
    rng = np.random.default_rng(20260924)
    family = np.arange(n) % 4

    def draw() -> np.ndarray:
        common = rng.normal(size=(trials, 4))[:, family]
        individual = rng.normal(size=(trials, n))
        return sigma * (np.sqrt(family_corr) * common + np.sqrt(1 - family_corr) * individual)

    d0 = draw()
    d1 = seed_corr * d0 + np.sqrt(1 - seed_corr * seed_corr) * draw()
    idx = np.random.default_rng(0).integers(0, n, size=(4000, n))
    counts = np.stack([(idx == i).sum(axis=1) for i in range(n)], axis=1) / n

    def keep(differences: np.ndarray) -> np.ndarray:
        result = []
        for begin in range(0, len(differences), 200):
            chunk = differences[begin:begin + 200]
            low = np.quantile(chunk @ counts.T, .10, axis=1)
            result.extend((chunk.mean(axis=1) >= max(.01, 2 * noise_sd))
                          & (low > 0) & (chunk.min(axis=1) >= -.5))
        return np.asarray(result, dtype=bool)

    first = keep(d0)
    confirmed = first & keep((d0 + d1) / 2)
    # Check equivalence to actual Appendix C on a subset, including confirmations.
    for differences in (d0[:50], ((d0 + d1) / 2)[:50]):
        expected = [module.decide(np.zeros(n), d, np.ones(n), eps=.01,
                                 noise_sd=noise_sd, simpler=False, guards_ok=True).verdict == "KEEP"
                    for d in differences]
        assert np.array_equal(keep(differences), expected)
    count = int(confirmed.sum())
    return {"n_tasks": n, "sigma": sigma, "seed_correlation": seed_corr,
            "within_family_correlation": family_corr, "noise_sd": noise_sd,
            "trials": trials, "first_stage_rate": float(first.mean()),
            "confirmed_keep_count": count, "confirmed_rate": count / trials,
            "wilson_95_ci": wilson(count, trials)}


rows = []
for n in (12, 16):
    for sigma, seed_corr, family_corr, noise_sd in (
        (.03, 0., 0., 0.), (.08, 0., 0., 0.), (.08, 1., 0., 0.),
        (.08, .8, .6, 0.), (.08, 0., 0., .02),
    ):
        row = monte_carlo(n, sigma, seed_corr, family_corr, noise_sd)
        rows.append(row)
        print(json.dumps(row), flush=True)


def clean(value: object) -> object:
    if isinstance(value, float) and not np.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value]
    return value


output = {"python": platform.python_version(), "numpy": np.__version__, "pandas": pd.__version__,
          "appendix_tests": "passed", "edge_cases": clean(edge), "null_simulation": rows,
          "limitations": "Scenario-specific population-null simulation, not a measured production error rate; fixed known noise_sd; equal weights; four families; no adaptive candidate selection modeled."}
(HERE / "review-results.json").write_text(json.dumps(output, indent=2, allow_nan=False) + "\n")
print(json.dumps(clean(edge), indent=2))
