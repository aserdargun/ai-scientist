"""Keep the best confirmed suite score fixed across repeated simplifications."""

from __future__ import annotations

import math

import pytest

from harness.referee import decide


def test_small_simplification_losses_cannot_accumulate_past_the_best_suite_floor() -> None:
    """Each local loss is tolerable, but the third exceeds the run-wide loss cap."""
    best_confirmed_suite = 1.0
    parent = [1.0] * 4
    observed = []
    for score in (0.996, 0.992, 0.988):
        child = [score] * 4
        result = decide(
            parent,
            child,
            [0.25] * 4,
            eps=0.01,
            noise_sd=0.0,
            simpler=True,
            guards_ok=True,
            best_suite=best_confirmed_suite,
        )
        observed.append(result.verdict)
        assert result.delta == pytest.approx(-0.004)
        assert result.ci_low == pytest.approx(-0.004)
        if result.verdict == "KEEP_SIMPLER":
            parent = child
    assert observed == ["KEEP_SIMPLER", "KEEP_SIMPLER", "DISCARD"]
    assert parent == [0.992] * 4
    assert best_confirmed_suite == 1.0


@pytest.mark.parametrize(
    ("child_score", "expected_verdict"),
    [(0.99, "KEEP_SIMPLER"), (math.nextafter(0.99, -math.inf), "DISCARD")],
)
def test_simplification_floor_includes_equality_but_not_the_next_lower_float(
    child_score: float, expected_verdict: str
) -> None:
    """Only the cumulative floor changes at this boundary; local loss remains safe."""
    result = decide(
        [0.992] * 4,
        [child_score] * 4,
        [0.25] * 4,
        eps=0.01,
        noise_sd=0.0,
        simpler=True,
        guards_ok=True,
        best_suite=1.0,
    )
    assert result.delta is not None and result.delta > -0.005
    assert result.ci_low is not None and result.ci_low > -0.01
    assert result.verdict == expected_verdict
