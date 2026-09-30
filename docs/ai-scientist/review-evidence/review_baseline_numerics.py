"""Independent arithmetic review of trusted baseline code, not sandbox acceptance."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from harness.alarm import apply_alarm_policy
from harness.baselines import build_baseline, normalize_task_score
from harness.contracts import FitContext

ROOT = Path(__file__).resolve().parents[3]


def expected_ecod(train: list[float], queries: list[float], skew_sign: int) -> list[float]:
    """Count each inclusive tail directly; signs are known for these fixed inputs."""
    result = []
    for value in queries:
        left = -math.log(max(sum(item <= value for item in train) / len(train), 1 / (len(train) + 1)))
        right = -math.log(max(sum(item >= value for item in train) / len(train), 1 / (len(train) + 1)))
        skew_tail = left if skew_sign < 0 else right if skew_sign > 0 else left + right
        result.append(max(left, right, skew_tail))
    return result


def main() -> None:
    paths = ("harness/baselines.py", "harness/alarm.py", "harness/contracts.py")
    hashes = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in paths}
    checks: dict[str, bool] = {}
    cases = []
    context = FitContext(0, ("x",), (), 1, 30.0)
    examples = (
        ("positive_skew", [0., 0., 0., 1., 10.], [-10., 0., .5, 1., 10., 20.], 1),
        ("negative_skew", [-10., -1., 0., 0., 0.], [-20., -10., -1., -.5, 0., 10.], -1),
        ("symmetric", [-2., -1., 0., 1., 2.], [-3., -2., -1., 0., 1., 2., 3.], 0),
        ("symmetric_ties", [-2., -2., 0., 2., 2.], [-3., -2., -1., 0., 1., 2., 3.], 0),
        ("constant_feature", [4., 4., 4., 4., 4.], [3., 4., 5.], 0),
    )
    for name, train_values, queries, sign in examples:
        model = build_baseline("ecod_train_frozen")
        train = pd.DataFrame({"x": train_values})
        evaluation = pd.DataFrame({"x": queries})
        model.fit(train, context)
        before = model.score(train)
        actual = model.score(evaluation)
        expected = expected_ecod(train_values, queries, sign)
        checks[f"{name}_inclusive_tail_oracle"] = bool(np.allclose(actual, expected, rtol=1e-12, atol=1e-12))
        checks[f"{name}_single_vs_batch"] = all(
            model.score(evaluation.iloc[index:index + 1])[0] == value
            for index, value in enumerate(actual)
        )
        checks[f"{name}_fit_state_unchanged"] = bool(np.array_equal(before, model.score(train)))
        cases.append({"name": name, "train": train_values, "eval": queries,
                      "skew_sign": sign, "expected": expected, "actual": actual.tolist()})

    # This two-column case requires addition of independently verified feature contributions.
    train = pd.DataFrame({"x": [-2., -1., 0., 1., 2.], "y": [0., 0., 0., 1., 10.]})
    evaluation = pd.DataFrame({"x": [-3., 0., 3.], "y": [-10., 0., 20.]})
    model = build_baseline("ecod_train_frozen")
    model.fit(train, FitContext(0, ("x", "y"), (), 1, 30.0))
    expected = np.asarray(expected_ecod(train.x.tolist(), evaluation.x.tolist(), 0))
    expected += np.asarray(expected_ecod(train.y.tolist(), evaluation.y.tolist(), 1))
    checks["ecod_feature_sum"] = bool(np.allclose(model.score(evaluation), expected, rtol=1e-12, atol=1e-12))

    # All detectors must score a fixed prefix independently of future rows and batch length.
    rng = np.random.default_rng(81)
    random_train = pd.DataFrame({"x": rng.normal(size=80)})
    prefix = pd.DataFrame({"x": [-1., .3, 2.]})
    future = pd.concat([prefix, pd.DataFrame({"x": [-100., 80., 13.]})], ignore_index=True)
    flat_alarms = {}
    for name in ("robust_z", "iforest", "ecod_train_frozen"):
        detector = build_baseline(name)
        detector.fit(random_train, context)
        checks[f"{name}_causal_prefix"] = bool(np.array_equal(detector.score(prefix), detector.score(future)[:3]))
        repeated = build_baseline(name)
        repeated.fit(random_train, context)
        checks[f"{name}_seed_zero_repeat"] = bool(np.array_equal(detector.score(future), repeated.score(future)))
        policy = detector.alarm_policy(np.zeros(8, dtype=np.float64))
        actual_alarm = apply_alarm_policy(np.array([0., 1., 0., 0.]), policy)
        checks[f"{name}_flat_train_alarm_recovers"] = bool(np.array_equal(actual_alarm, [False, True, False, False]))
        flat_alarms[name] = {"threshold": policy.threshold, "release": policy.release,
                             "dwell": policy.dwell, "actual": actual_alarm.tolist()}

    robust = build_baseline("robust_z")
    robust.fit(pd.DataFrame({"x": [-2., -1., 0., 1., 2.]}), context)
    checks["robust_z_median_mad_oracle"] = bool(np.allclose(
        robust.score(pd.DataFrame({"x": [-3., 0., 3.]})), [3 / 1.4826, 0., 3 / 1.4826],
        rtol=1e-12, atol=1e-12,
    ))
    normalization = {}
    for task_type, expected_value in (("EVT", .5), ("PDM", .1), ("NRM", .1), ("C-EXT", .2), ("C-UTIL", .5)):
        actual = normalize_task_score(.11, .1, .1001, family=task_type)
        normalization[task_type] = actual
        checks[f"{task_type}_normalization_floor"] = math.isclose(actual, expected_value, rel_tol=1e-12, abs_tol=1e-12)
    checks["normalization_upper_clip"] = normalize_task_score(1., .1, .1001, family="EVT") == 3.
    checks["normalization_lower_clip"] = normalize_task_score(0., .1, .1001, family="EVT") == -1.
    checks["normalization_baseline_zero"] = normalize_task_score(.1, .1, .5, family="EVT") == 0.
    checks["normalization_reference_one"] = normalize_task_score(.5, .1, .5, family="EVT") == 1.
    evidence = {
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": "Trusted baseline code on local CPU synthetic arrays. Literal inclusive tail counts and hand-derived MAD/normalization values are independent arithmetic oracles. Does not establish Docker/image parity, independent Scorer/PG calibration, physical blobs, Director20, public-data, model, GPU or AOS acceptance.",
        "source_sha256": hashes,
        "checks": checks,
        "ecod_cases": cases,
        "flat_alarm_cases": flat_alarms,
        "normalization_floor_values": normalization,
        "source_unchanged": all(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest for name, digest in hashes.items()),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "command": "OPENBLAS_NUM_THREADS=2 OMP_NUM_THREADS=2 .venv/bin/python docs/ai-scientist/review-evidence/review_baseline_numerics.py",
    }
    evidence["all_passed"] = all(checks.values()) and evidence["source_unchanged"]
    Path(__file__).with_name("baseline-numerics-review.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps({"checks": checks, "source_unchanged": evidence["source_unchanged"], "all_passed": evidence["all_passed"]}, indent=2))
    if not evidence["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
