"""Prepare causal synthetic proposals; final acceptance needs the real Director."""

from __future__ import annotations

import ast
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import textwrap

import numpy as np
import pandas as pd

from harness.baselines import build_baseline, freeze_task_references, normalize_task_score
from harness.contracts import FitContext
from harness.metrics import vus_metrics
from harness.referee import decide

ROOT = Path(__file__).resolve().parents[3]

RESIDUAL_VERBOSE = textwrap.dedent('''\
    import numpy as np
    from harness.contracts import AlarmPolicy

    class Candidate:
        def fit(self, train, ctx):
            values = train.to_numpy(dtype=float)
            first_sensor = values[:, 0]
            second_sensor = values[:, 1]
            constant_column = np.ones(len(values))
            design = np.column_stack((first_sensor, constant_column))
            coefficients, _, _, _ = np.linalg.lstsq(design, second_sensor, rcond=None)
            self.slope = coefficients[0]
            self.intercept = coefficients[1]

        def score(self, data):
            values = data.to_numpy(dtype=float)
            first_sensor = values[:, 0]
            second_sensor = values[:, 1]
            scaled = self.slope * first_sensor
            predicted = scaled + self.intercept
            residual = second_sensor - predicted
            magnitude = np.abs(residual)
            return magnitude

        def alarm_policy(self, train_scores):
            high = float(np.quantile(train_scores, 0.99))
            low = float(np.quantile(train_scores, 0.95))
            policy = AlarmPolicy(high, low, 1)
            return policy

    def build_candidate():
        return Candidate()
''')

RESIDUAL_SIMPLE = textwrap.dedent('''\
    import numpy as np
    from harness.contracts import AlarmPolicy

    class Candidate:
        def fit(self, train, ctx):
            values = train.to_numpy(dtype=float)
            design = np.column_stack((values[:, 0], np.ones(len(values))))
            self.slope, self.intercept = np.linalg.lstsq(design, values[:, 1], rcond=None)[0]

        def score(self, data):
            values = data.to_numpy(dtype=float)
            return np.abs(values[:, 1] - (self.slope * values[:, 0] + self.intercept))

        def alarm_policy(self, train_scores):
            return AlarmPolicy(float(np.quantile(train_scores, 0.99)),
                               float(np.quantile(train_scores, 0.95)), 1)

    def build_candidate():
        return Candidate()
''')

RESIDUAL_INVERSE = RESIDUAL_SIMPLE.replace(
    "return np.abs(values[:, 1]", "return -np.abs(values[:, 1]"
)
CONSTANT = RESIDUAL_SIMPLE.replace(
    "return np.abs(values[:, 1] - (self.slope * values[:, 0] + self.intercept))",
    "return np.zeros(len(values))",
)
CRASH = RESIDUAL_SIMPLE.replace(
    "values = train.to_numpy(dtype=float)", 'raise RuntimeError("synthetic candidate failure")'
)
LONG_FIT = RESIDUAL_SIMPLE.replace(
    "values = train.to_numpy(dtype=float)", "import time\n        time.sleep(120)\n        values = train.to_numpy(dtype=float)"
)


def fixture_tasks() -> list[tuple[pd.DataFrame, pd.DataFrame, np.ndarray]]:
    """Trusted loader output; labels must only be installed in Scorer storage."""
    result = []
    for index in range(4):
        rng = np.random.default_rng(240_924 + index)
        slope = 1.5 + 0.5 * index
        train_first = rng.normal(size=128)
        train_second = slope * train_first + rng.normal(scale=0.03, size=128)
        eval_first = rng.normal(size=128)
        eval_second = slope * eval_first + rng.normal(scale=0.03, size=128)
        labels = np.zeros(128, dtype=np.int8)
        labels[25 + index:37 + index] = 1
        labels[84 - index:99 - index] = 1
        eval_second[labels.astype(bool)] += 0.7 + 0.1 * index
        columns = ("sensor_a", "sensor_b")
        train = pd.DataFrame(np.column_stack((train_first, train_second)), columns=columns)
        evaluation = pd.DataFrame(np.column_stack((eval_first, eval_second)), columns=columns)
        result.append((train, evaluation, labels))
    return result


def main() -> None:
    """Measure fixture viability without treating it as sandbox/Director evidence."""
    source_paths = [
        Path(__file__), ROOT / "harness/baselines.py", ROOT / "harness/referee.py",
        ROOT / "harness/metrics.py",
    ]
    hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}
    record = {
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": "Fixture design: four synthetic correlated-sensor EVT tasks, trusted in-process baseline/metric arithmetic. No candidate source execution, Docker, PG, Director, public data, local LLM or AOS acceptance.",
        "source_sha256": hashes, "tasks": [], "candidate_sources": {}, "checks": {},
    }
    better_scores, worse_scores = [], []
    for index, (train, evaluation, labels) in enumerate(fixture_tasks()):
        context = FitContext(0, tuple(train.columns), (), 60, 30.0)
        baseline_scores = {}
        for name in ("robust_z", "iforest", "ecod_train_frozen"):
            baseline_scores[name] = {}
            for seed in range(3):
                model = build_baseline(name)
                model.fit(train, FitContext(seed, context.signals, (), 60, 30.0))
                baseline_scores[name][seed] = vus_metrics(labels, model.score(evaluation), sliding_window=4).vus_pr
        references = freeze_task_references(baseline_scores, family="EVT")
        # Trusted independent arithmetic; untrusted source strings are never executed here.
        values = train.to_numpy(dtype=float)
        slope, intercept = np.linalg.lstsq(
            np.column_stack((values[:, 0], np.ones(len(values)))), values[:, 1], rcond=None
        )[0]
        observed = evaluation.to_numpy(dtype=float)
        residual = np.abs(observed[:, 1] - (slope * observed[:, 0] + intercept))
        good_vus = vus_metrics(labels, residual, sliding_window=4).vus_pr
        inverse_vus = vus_metrics(labels, -residual, sliding_window=4).vus_pr
        better = normalize_task_score(good_vus, references.base_score, references.reference_score, family="EVT")
        worse = normalize_task_score(inverse_vus, references.base_score, references.reference_score, family="EVT")
        better_scores.append(better)
        worse_scores.append(worse)
        task_bytes = train.to_numpy().tobytes() + observed.tobytes() + labels.tobytes()
        record["tasks"].append({
            "task": f"synthetic-correlated-{index}", "train_rows": 128, "eval_rows": 128,
            "fixture_sha256": hashlib.sha256(task_bytes).hexdigest(),
            "baseline_seed_scores": baseline_scores, "base": references.base_score,
            "ref": references.reference_score, "residual_vus": good_vus,
            "inverse_vus": inverse_vus, "residual_normalized": better, "inverse_normalized": worse,
        })
    for name, source in {
        "residual_verbose": RESIDUAL_VERBOSE, "residual_simple": RESIDUAL_SIMPLE,
        "inverse": RESIDUAL_INVERSE, "constant": CONSTANT, "crash": CRASH, "long_fit": LONG_FIT,
    }.items():
        tree = ast.parse(source)
        record["candidate_sources"][name] = {
            "source": source, "sha256": hashlib.sha256(source.encode()).hexdigest(),
            "ast_lines": len({node.lineno for node in ast.walk(tree) if hasattr(node, "lineno")}),
        }
    weights = [0.25] * 4
    decisions = [
        decide([0.0] * 4, better_scores, weights, eps=0.01, noise_sd=0.0, simpler=False, guards_ok=True, best_suite=0.0),
        decide(better_scores, better_scores, weights, eps=0.01, noise_sd=0.0, simpler=True, guards_ok=True, best_suite=float(np.mean(better_scores))),
        decide(better_scores, worse_scores, weights, eps=0.01, noise_sd=0.0, simpler=False, guards_ok=True, best_suite=float(np.mean(better_scores))),
    ]
    record["arithmetic_verdicts"] = [decision.verdict for decision in decisions]
    record["checks"] = {
        "residual_improves_every_task": all(value >= 0.01 for value in better_scores),
        "arithmetic_decision_diversity": record["arithmetic_verdicts"] == ["KEEP", "KEEP_SIMPLER", "DISCARD"],
        "source_ast_reduction_at_least_5_percent": record["candidate_sources"]["residual_simple"]["ast_lines"] <= 0.95 * record["candidate_sources"]["residual_verbose"]["ast_lines"],
    }
    record["source_unchanged"] = all(hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest for path, digest in hashes.items())
    record["all_passed"] = all(record["checks"].values()) and record["source_unchanged"]
    record["command"] = ".venv/bin/python docs/ai-scientist/review-evidence/review_director_synthetic_scenario.py"
    Path(__file__).with_name("director-synthetic-scenario-review.json").write_text(json.dumps(record, indent=2, allow_nan=False) + "\n")
    print(json.dumps({key: record[key] for key in ("scope", "tasks", "arithmetic_verdicts", "checks", "all_passed")}, indent=2))
    if not record["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
