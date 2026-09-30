"""Negative fixtures for masks, event onset and healthy exposure."""

from __future__ import annotations

import numpy as np
import pytest

from harness.alarm import (
    InvalidMetricError,
    alarm_stats,
    apply_alarm_policy,
    nrm_task_score,
    pdm_earliness,
    pdm_task_score,
)
from harness.contracts import AlarmPolicy


def test_alarm_onset_in_masked_samples_gets_no_early_warning_credit() -> None:
    active = np.zeros(200, dtype=bool)
    active[110:191] = True
    masked = np.zeros(200, dtype=bool)
    masked[110:160] = True
    assert pdm_earliness(active, [(100, 190)], masked) == 0.0


def test_masked_alarm_carries_through_without_counting_healthy_onset() -> None:
    active = np.array([False, True, True, True, False])
    positive = np.zeros(5, dtype=bool)
    masked = np.array([False, True, True, False, False])
    false_alarms, duty = alarm_stats(active, positive, masked, 60)
    assert false_alarms == 0.0
    assert duty == pytest.approx(1 / 3)


def test_pdm_expected_earliness_uses_uncompressed_original_time_axis() -> None:
    active = np.zeros(200, dtype=bool)
    active[122:191] = True
    masked = np.zeros(200, dtype=bool)
    masked[191:] = True
    score, false_alarms, duty = pdm_task_score(
        active, [(100, 190)], masked, 600, max_fa=1.0
    )
    assert score == pytest.approx(68 / 90)
    assert false_alarms == 0.0
    assert duty == 0.0


def test_always_on_alarm_scores_zero_on_pdm_and_nrm() -> None:
    active = np.ones(200, dtype=bool)
    masked = np.zeros(200, dtype=bool)
    pdm_score, _, pdm_duty = pdm_task_score(
        active, [(100, 190)], masked, 600, max_fa=1.0
    )
    nrm_score, _, nrm_duty = nrm_task_score(active, masked, 600)
    assert pdm_score == 0.0 and pdm_duty == 1.0
    assert nrm_score == 0.0 and nrm_duty == 1.0


def test_zero_healthy_exposure_is_an_explicit_metric_error() -> None:
    with pytest.raises(InvalidMetricError, match="zero healthy exposure"):
        alarm_stats(np.ones(10), np.ones(10), np.zeros(10), 60)


def test_nan_mask_preserves_active_state_and_resets_dwell() -> None:
    policy = AlarmPolicy(2.0, 1.0, 2)
    scores = [3.0, 3.0, float("nan"), 2.0, 0.0]
    assert apply_alarm_policy(scores, policy).tolist() == [False, True, True, True, False]
    assert apply_alarm_policy([3.0, float("nan"), 3.0, 0.0], policy).tolist() == [
        False,
        False,
        False,
        False,
    ]
