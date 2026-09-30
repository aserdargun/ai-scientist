from __future__ import annotations

import pytest

from lab.director.budget import RunBudget


def test_provider_overrun_is_charged_as_actual_and_blocks_episode_retry() -> None:
    current = [0.0]
    budget = RunBudget(
        wall_limit=200, token_limit=1_000, monotonic=lambda: current[0]
    )
    reservation = budget.reserve_proposal(wall_seconds=180, model_tokens=500)

    budget.reconcile_proposal_overrun(
        reservation, measured_wall_seconds=190.25, measured_model_tokens=500
    )

    snapshot = budget.snapshot()
    assert snapshot.wall_seconds == pytest.approx(190.25)
    assert snapshot.model_tokens == 500
    assert snapshot.reserved_wall_seconds == 0
    assert snapshot.reserved_model_tokens == 0
    assert budget.remaining_wall_seconds == pytest.approx(9.75)
    with pytest.raises(RuntimeError, match="run_wall_budget_exhausted"):
        budget.reserve_proposal(wall_seconds=180, model_tokens=1)


def test_restored_wall_overrun_blocks_new_work_even_if_clock_is_behind() -> None:
    budget = RunBudget(wall_limit=50, token_limit=0, monotonic=lambda: 0.0)
    reservation = budget.reserve_work(wall_seconds=40, model_tokens=0)
    budget.reconcile_proposal_overrun(
        reservation, measured_wall_seconds=70, measured_model_tokens=0
    )
    assert budget.remaining_wall_seconds == 0
    with pytest.raises(RuntimeError, match="run_wall_budget_exhausted"):
        budget.reserve_work(wall_seconds=1, model_tokens=0)
