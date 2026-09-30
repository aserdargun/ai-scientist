"""Negative and replay regressions for the Referee's trust boundary."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from harness.contracts import AlarmPolicy
from harness.guards import causality_violation
from harness.referee import decide, replay_decision
from lab.llm.router import critic_gate, route_system


def test_reject_serializes_without_nan_and_replays() -> None:
    manifest: dict[str, object] = {
        "parent": [0.1, 0.2],
        "child": [0.3, 0.4],
        "weights": [1.0, 1.0],
        "eps": 0.01,
        "noise_sd": 0.001,
        "simpler": False,
        "guards_ok": False,
        "seed": 3,
        "n_boot": 100,
    }
    rejected = replay_decision(manifest)
    assert rejected.verdict == "REJECT"
    assert rejected.delta is None and rejected.ci_low is None
    json.dumps(rejected.__dict__ if hasattr(rejected, "__dict__") else {
        "verdict": rejected.verdict, "delta": rejected.delta,
        "ci_low": rejected.ci_low, "reason": rejected.reason,
    }, allow_nan=False)
    accepted = dict(manifest, guards_ok=True)
    first = replay_decision(accepted)
    assert first == replay_decision(accepted)
    assert first.verdict == "KEEP"


@pytest.mark.parametrize(
    "invalid_manifest",
    [
        {"guards_ok": "false"},
        {"n_boot": 100.5},
        {"unexpected": "field"},
    ],
)
def test_replay_manifest_rejects_coercion_and_unknown_fields(
    invalid_manifest: dict[str, object]
) -> None:
    manifest: dict[str, object] = {
        "parent": [0.1, 0.2],
        "child": [0.3, 0.4],
        "weights": [1.0, 1.0],
        "eps": 0.01,
        "noise_sd": 0.001,
        "simpler": False,
        "guards_ok": False,
    }
    manifest.update(invalid_manifest)
    with pytest.raises(ValueError):
        replay_decision(manifest)


@pytest.mark.parametrize(
    ("parent", "child", "weights"),
    [([1.0], [1.0, 2.0], [1.0]), ([float("nan")], [1.0], [1.0]), ([1.0], [2.0], [0.0])],
)
def test_invalid_score_inputs_fail_closed(
    parent: list[float], child: list[float], weights: list[float]
) -> None:
    result = decide(parent, child, weights, eps=0.01, noise_sd=0.0,
                    simpler=False, guards_ok=True)
    assert result.verdict == "REJECT"
    assert result.delta is None and result.ci_low is None


@pytest.mark.parametrize(
    ("threshold", "release", "dwell"),
    [
        (float("inf"), 0.0, 1),
        (1.0, float("nan"), 1),
        (1.0, 1.0, 0),
        (1.0, 1.0, 1.5),
        (1.0, 1.0, True),
    ],
)
def test_alarm_policy_rejects_nonfinite_or_invalid_parameters(
    threshold: float, release: float, dwell: int
) -> None:
    with pytest.raises(ValueError):
        AlarmPolicy(threshold=threshold, release=release, dwell=dwell)


def test_stateful_causality_fixture_requires_fresh_score_instances() -> None:
    """A shared mutable scorer violates the guard; independent instances pass."""
    class Stateful:
        def __init__(self) -> None:
            self.calls = 0

        def score(self, values: pd.DataFrame) -> np.ndarray:
            self.calls += 1
            return values.to_numpy().sum(axis=1) + self.calls

    data = pd.DataFrame({"x": np.arange(100, dtype=float)})
    shared = Stateful()
    assert causality_violation(lambda: shared, data) > 0.0
    assert causality_violation(Stateful, data) == 0.0


def test_router_and_critic_are_deterministic_and_bounded() -> None:
    assert route_system("experiment", "detector", explore=False, discard_streak=0) == "S2"
    assert route_system("experiment", "hparam", explore=False, discard_streak=0) == "S1"
    assert critic_gate(0.01, tau=0.05, eps=0.1, draw=0.05) == "run"
