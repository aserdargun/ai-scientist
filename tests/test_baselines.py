"""Causal, versioned baselines and frozen per-task score calibration."""

from __future__ import annotations

import hashlib
import math
from typing import cast
from uuid import uuid4

import numpy as np
import pandas as pd
import pytest

from harness.alarm import apply_alarm_policy
from harness.baselines import (
    BASELINE_SEEDS,
    CHAMPION_NOISE_VERSION,
    ECODTrainFrozenBaseline,
    IsolationForestBaseline,
    RobustZBaseline,
    build_baseline,
    champion_noise_sd,
    freeze_task_references,
    normalize_task_score,
)
from harness.contracts import FitContext
from lab.director.baselines import (
    BASELINE_NAMES,
    baseline_candidate_sha256,
    baseline_candidate_source,
    build_task_calibration,
    calibration_sha256,
    canonical_calibration_bytes,
    freeze_calibration_document,
)


def _context(seed: int = 7, signals: tuple[str, ...] = ("a", "b")) -> FitContext:
    return FitContext(
        seed=seed,
        signals=signals,
        regime_signals=(),
        sampling_s=60,
        time_budget_s=60.0,
    )


@pytest.mark.parametrize(
    ("name", "expected_type"),
    [
        ("robust_z", RobustZBaseline),
        ("iforest", IsolationForestBaseline),
        ("ecod_train_frozen", ECODTrainFrozenBaseline),
    ],
)
def test_allowlisted_baselines_fit_only_numeric_trusted_columns(
    name: str, expected_type: type[object]
) -> None:
    train = pd.DataFrame({"a": [-2.0, -1.0, 0.0, 1.0, 2.0], "b": [1, 2, 3, 4, 5]})
    evaluation = pd.DataFrame({"a": [-4.0, 0.0, 4.0], "b": [0, 3, 6]})
    first = build_baseline(name)
    second = build_baseline(name)
    assert isinstance(first, expected_type)
    first.fit(train, _context())
    second.fit(train, _context())
    left = first.score(evaluation)
    right = second.score(evaluation)
    assert left.shape == (3,)
    assert np.isfinite(left).all()
    np.testing.assert_array_equal(left, right)
    assert first.algorithm_version


def test_baselines_reject_untrusted_columns_and_nonfinite_train_values() -> None:
    detector = RobustZBaseline()
    with pytest.raises(ValueError, match="signals"):
        detector.fit(pd.DataFrame({"label": [0.0, 1.0]}), _context(signals=("a", "b")))
    with pytest.raises(ValueError, match="finite"):
        detector.fit(pd.DataFrame({"a": [0.0, float("nan")], "b": [1.0, 2.0]}), _context())
    with pytest.raises(ValueError, match="unknown baseline"):
        build_baseline("pyod_ecod")
    with pytest.raises(ValueError, match="unknown baseline"):
        baseline_candidate_source("pyod_ecod")


def test_ecod_freezes_inclusive_ties_tail_floor_and_future_prefix() -> None:
    train = pd.DataFrame({"a": [0.0, 0.0, 0.0, 1.0, 10.0]})
    detector = ECODTrainFrozenBaseline()
    detector.fit(train, _context(signals=("a",)))
    assert detector.algorithm_version == "ecod-train-frozen.v1"
    # Positive train skew selects U_r for U_skew, but upstream's final maximum
    # also includes both raw tails; unseen low-tail values therefore score ln(6).
    low_tail = detector.score(pd.DataFrame({"a": [-10.0]}))[0]
    assert low_tail == pytest.approx(math.log(6.0))

    symmetric = ECODTrainFrozenBaseline()
    symmetric.fit(pd.DataFrame({"a": [-2.0, -1.0, 0.0, 1.0, 2.0]}), _context(signals=("a",)))
    # Inclusive ties make three of five train values <= 0 and >= 0.
    tie_score = symmetric.score(pd.DataFrame({"a": [0.0]}))[0]
    assert tie_score == pytest.approx(-2.0 * math.log(3.0 / 5.0))

    prefix = pd.DataFrame({"a": [-3.0, -1.0, 0.0]})
    extended = pd.DataFrame({"a": [-3.0, -1.0, 0.0, 100.0, -100.0]})
    np.testing.assert_array_equal(symmetric.score(prefix), symmetric.score(extended)[: len(prefix)])


def test_ecod_negative_skew_uses_opposite_frozen_tail_and_constants_are_finite() -> None:
    detector = ECODTrainFrozenBaseline()
    detector.fit(pd.DataFrame({"a": [-10.0, -1.0, 0.0, 0.0, 0.0]}), _context(signals=("a",)))
    assert detector.score(pd.DataFrame({"a": [10.0]}))[0] == pytest.approx(math.log(6.0))
    constant = ECODTrainFrozenBaseline()
    constant.fit(pd.DataFrame({"a": [2.0] * 5}), _context(signals=("a",)))
    np.testing.assert_array_equal(constant.score(pd.DataFrame({"a": [2.0, 2.0]})), [0.0, 0.0])


def test_constant_train_alarm_policy_does_not_latch_on_transient_score() -> None:
    detector = RobustZBaseline()
    train = pd.DataFrame({"a": [0.0] * 10})
    detector.fit(train, _context(signals=("a",)))
    policy = detector.alarm_policy(detector.score(train))
    assert policy.threshold == policy.release > 0.0
    np.testing.assert_array_equal(
        apply_alarm_policy([0.0, 1.0, 0.0, 0.0], policy), [False, True, False, False]
    )
    with pytest.raises(ValueError, match="finite alarm successor"):
        detector.alarm_policy(np.full(3, np.finfo(np.float64).max))


def test_baseline_reference_and_champion_noise_use_only_frozen_seed_sets() -> None:
    references = freeze_task_references(
        {
            "robust_z": {0: 0.1, 1: 0.2, 2: 0.3},
            "iforest": {0: 0.3, 1: 0.4, 2: 0.5},
            "ecod_train_frozen": {0: 0.2, 1: 0.3, 2: 0.4},
        },
        family="PDM",
    )
    assert references.base_score == pytest.approx(0.2)
    assert references.reference_score == pytest.approx(0.4)
    assert references.seed_scores["robust_z"] == (0.1, 0.2, 0.3)
    with pytest.raises(TypeError):
        cast(dict[str, tuple[float, float, float]], references.seed_scores)["robust_z"] = (
            9.0,
            9.0,
            9.0,
        )
    assert normalize_task_score(0.5, 0.2, 0.4, family="PDM") == pytest.approx(1.5)
    assert normalize_task_score(0.5, 0.2, 0.2, family="PDM") == pytest.approx(3.0)
    assert normalize_task_score(-2.0, 0.2, 0.4, family="PDM") == -1.0
    assert CHAMPION_NOISE_VERSION == "population-ddof0-seeds-0-2.v1"
    assert champion_noise_sd({0: 1.0, 1: 2.0, 2: 3.0}) == pytest.approx(np.sqrt(2.0 / 3.0))
    assert champion_noise_sd({0: 0.1, 1: 0.1, 2: 0.1}) == 0.0
    with pytest.raises(ValueError, match="seeds 0, 1, and 2"):
        freeze_task_references(
            {
                "robust_z": {1: 0.1, 2: 0.2},
                "iforest": {0: 0.3, 1: 0.4, 2: 0.5},
                "ecod_train_frozen": {0: 0.2, 1: 0.3, 2: 0.4},
            },
            family="PDM",
        )
    assert BASELINE_SEEDS == (0, 1, 2)


def test_canonical_calibration_binds_measured_seed_grid_and_source_hashes() -> None:
    source_hashes = {name: baseline_candidate_sha256(name) for name in BASELINE_NAMES}
    task = build_task_calibration(
        task_id="evt-small",
        dataset_id="fixture",
        split_id="dev",
        session_id="session-1",
        profile_sha256="a" * 64,
        family="EVT",
        baseline_seed_scores={name: {0: 0.1, 1: 0.2, 2: 0.3} for name in BASELINE_NAMES},
        scorer_artifact_sha256={
            name: {0: "b" * 64, 1: "c" * 64, 2: "d" * 64} for name in BASELINE_NAMES
        },
        baseline_candidate_sha256=source_hashes,
        task_weight=1.0,
    )
    document = freeze_calibration_document(
        run_id=uuid4(),
        suite_id="fixture-suite",
        suite_version=1,
        harness_sha256="e" * 64,
        image_sha256="f" * 64,
        expected_task_identities=(("fixture", "dev", "session-1", "evt-small"),),
        tasks=(task,),
        champion_experiment_id="exp_" + "0" * 32,
        champion_baseline_name="robust_z",
        champion_suite_scores={0: 0.0, 1: 0.0, 2: 0.0},
    )
    payload = canonical_calibration_bytes(document)
    assert calibration_sha256(document) == hashlib.sha256(payload).hexdigest()
    assert document.champion_noise_sd == 0.0
    assert [item.name for item in task.seed_scores] == list(BASELINE_NAMES)
    assert task.reference_score == pytest.approx(0.2)
