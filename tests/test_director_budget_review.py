"""Independent budget invariants for elapsed time and one-time reservations."""

from __future__ import annotations

import math

import pytest

from lab.director.budget import BudgetSnapshot, RunBudget


def test_measured_episode_does_not_charge_its_elapsed_time_twice() -> None:
    """Twenty elapsed seconds consume twenty seconds of the run's wall budget."""
    now = [100.0]
    budget = RunBudget(wall_limit=100, token_limit=100, monotonic=lambda: now[0])
    reservation = budget.reserve_proposal(wall_seconds=30, model_tokens=50)
    now[0] += 20.0
    budget.reconcile_proposal(
        reservation, measured_wall_seconds=20.0, measured_model_tokens=10
    )
    assert budget.remaining_wall_seconds == 80.0
    assert budget.remaining_model_tokens == 90


def test_repeated_reconciliation_cannot_refund_a_reservation_twice() -> None:
    """An accidental retry cannot create extra budget or negative reservations."""
    budget = RunBudget(wall_limit=100, token_limit=100, monotonic=lambda: 0.0)
    reservation = budget.reserve_proposal(wall_seconds=30, model_tokens=50)
    budget.reconcile_proposal(
        reservation, measured_wall_seconds=0.0, measured_model_tokens=10
    )
    before = budget.remaining_wall_seconds, budget.remaining_model_tokens
    with pytest.raises((ValueError, RuntimeError)):
        budget.reconcile_proposal(
            reservation, measured_wall_seconds=0.0, measured_model_tokens=10
        )
    assert (budget.remaining_wall_seconds, budget.remaining_model_tokens) == before


def test_reservations_with_equal_amounts_are_not_interchangeable() -> None:
    """Two runs reserving the same amounts still own distinct reservations."""
    first = RunBudget(wall_limit=100, token_limit=100, monotonic=lambda: 0.0)
    second = RunBudget(wall_limit=100, token_limit=100, monotonic=lambda: 0.0)
    foreign = first.reserve_proposal(wall_seconds=30, model_tokens=50)
    second.reserve_proposal(wall_seconds=30, model_tokens=50)
    before = second.remaining_wall_seconds, second.remaining_model_tokens
    with pytest.raises((ValueError, RuntimeError)):
        second.reconcile_proposal(
            foreign, measured_wall_seconds=0.0, measured_model_tokens=10
        )
    assert (second.remaining_wall_seconds, second.remaining_model_tokens) == before


@pytest.mark.parametrize("wall_seconds", [math.nan, math.inf, -math.inf])
def test_resume_refuses_nonfinite_wall_counters(wall_seconds: float) -> None:
    """Malformed persisted accounting fails before new work is admitted."""
    with pytest.raises(ValueError):
        RunBudget(snapshot=BudgetSnapshot(wall_seconds=wall_seconds))


def test_repeated_restore_preserves_elapsed_and_does_not_double_charge() -> None:
    """Older frozen snapshots cannot refund in-process time or charge downtime twice."""
    from lab.director.budget import BudgetSnapshot, RunBudget

    now = [100.0]
    frozen = BudgetSnapshot(wall_seconds=12.0, elapsed_wall_seconds=20.0, model_tokens=9)
    budget = RunBudget(snapshot=frozen, monotonic=lambda: now[0])
    now[0] = 107.0
    budget.restore(frozen)
    assert budget.snapshot().elapsed_wall_seconds == 27.0
    budget.observe_elapsed(40.0)
    budget.restore(frozen)
    budget.observe_elapsed(40.0)
    assert budget.snapshot().elapsed_wall_seconds == 40.0
    assert budget.snapshot().wall_seconds == 12.0
    assert budget.snapshot().model_tokens == 9
    now[0] = 110.0
    budget.restore(frozen)
    assert budget.snapshot().elapsed_wall_seconds == 43.0
