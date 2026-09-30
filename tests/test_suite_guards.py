"""Complete suite bias guard must not pass incomplete task evidence."""

from __future__ import annotations

import pytest

from lab.director.suite_guards import check_complete_suite_position_bias


def test_position_bias_waits_for_all_nrm_tasks() -> None:
    result = check_complete_suite_position_bias(
        {"n1": [0.0, 0.2, 0.1]}, {"n1": "NRM", "n2": "NRM", "e1": "EVT"}
    )
    assert result.state == "pending"
    assert not result.passed
    assert result.missing_task_ids == ("n2",)


def test_position_bias_fails_when_half_nrm_tasks_exceed_threshold() -> None:
    result = check_complete_suite_position_bias(
        {
            "n1": [0.0, 1.0, 2.0, 3.0],
            "n2": [0.0, 1.0, 0.0, 1.0],
        },
        {"n1": "NRM", "n2": "NRM", "p1": "PDM"},
    )
    assert result.state == "fail"
    assert result.violating_tasks == 1


def test_position_bias_uses_spec_threshold_and_strict_comparison() -> None:
    near_but_allowed = check_complete_suite_position_bias(
        {"n1": [0.0, 1.0, 3.0, 2.0]}, {"n1": "NRM"}
    )
    assert near_but_allowed.task_bias["n1"] == pytest.approx(0.8)
    assert near_but_allowed.state == "pass"
    above_threshold = check_complete_suite_position_bias(
        {"n1": [0.0, 1.0, 2.0, 4.0, 3.0]}, {"n1": "NRM"}
    )
    assert above_threshold.task_bias["n1"] > 0.8
    assert above_threshold.state == "fail"


def test_position_bias_passes_only_complete_and_nonbiased_nrm_suite() -> None:
    result = check_complete_suite_position_bias(
        {"n1": [0.0, 1.0, 0.1, 0.9], "n2": [1.0, 0.0, 1.0, 0.0]},
        {"n1": "NRM", "n2": "NRM", "e1": "EVT"},
    )
    assert result.state == "pass"
    assert result.passed


def test_position_bias_rejects_nonfinite_or_untrusted_inputs() -> None:
    with pytest.raises(ValueError, match="untrusted"):
        check_complete_suite_position_bias({"x": [0.0, 1.0]}, {"n1": "NRM"})
    with pytest.raises(ValueError, match="finite ordered"):
        check_complete_suite_position_bias({"n1": [0.0, float("nan")]}, {"n1": "NRM"})
