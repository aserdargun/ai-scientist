"""Deterministic local model role routing."""

from __future__ import annotations

import math
from typing import Literal

SystemName = Literal["S1", "S2"]

S2_MOVES = frozenset({"detector", "fusion", "regime", "features"})
S2_JOBS = frozenset({"scout", "consolidate", "skill_author", "program_proposal", "holdout_revert"})


def route_system(
    job: str,
    move_type: str | None,
    *,
    explore: bool,
    discard_streak: int,
    escalate_after: int = 5,
) -> SystemName:
    """Route designated novel work to S2; all routine work remains S1."""
    if discard_streak < 0 or escalate_after < 1:
        raise ValueError("discard counters must be non-negative and escalation threshold positive")
    if job in S2_JOBS or explore or discard_streak >= escalate_after:
        return "S2"
    if job == "experiment" and move_type in S2_MOVES:
        return "S2"
    return "S1"


def critic_gate(
    p_keep: float, *, tau: float, eps: float, draw: float
) -> Literal["run", "escalate"]:
    """Apply the recorded critic threshold and exploration draw."""
    if not all(
        math.isfinite(value) and 0.0 <= value <= 1.0
        for value in (p_keep, tau, eps, draw)
    ):
        raise ValueError("critic probabilities must be within [0, 1]")
    return "run" if p_keep >= tau or draw < eps else "escalate"
