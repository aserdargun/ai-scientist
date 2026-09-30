"""CPU-only control-flow checks for captured holdout closure identity.

These tests exercise the Director wrapper's no-readmission and no-budget-reset
branches using typed receipts. The terminal-budget case models only its
checkpoint recovery seam; it is not a durable journal test. They do not replace
the 0026 real-role SQL tests, prove process/cgroup/sandbox drain, or constitute
acceptance evidence.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from lab.director.budget import BudgetSnapshot, EpisodeReservation, RunBudget
from lab.director.holdout import HoldoutReceipt, recover_existing_holdout_request
from lab.director.loop import DirectorLoop, DirectorLoopResult
from lab.director.ownership import ExecutionOwner, owned_execution


@pytest.mark.parametrize("state", ["reserved", "running"])
def test_existing_open_holdout_recovery_uses_same_uuid_without_readmission(
    monkeypatch, state: str
) -> None:
    """An existing reservation is recovered by UUID, never newly reserved."""
    import lab.director.holdout as holdout

    run_id = uuid4()
    reservation_id = uuid4()
    owner = ExecutionOwner(
        run_id=run_id,
        generation=4,
        invocation_id="a" * 32,
        execution_sha256="b" * 64,
    )
    admitted = HoldoutReceipt(
        reservation_id=reservation_id,
        state=state,
        bit=None,
        run_id=run_id,
        candidate_experiment_id="exp-final",
        trigger_kind="run_end",
        trigger_index=1,
        admitted_generation=owner.generation,
        execution_sha256=owner.execution_sha256,
    )
    reread = HoldoutReceipt(
        reservation_id=reservation_id,
        state=state,
        bit=None,
        run_id=run_id,
        candidate_experiment_id="exp-final",
        trigger_kind="run_end",
        trigger_index=1,
        admitted_generation=owner.generation,
        execution_sha256=owner.execution_sha256,
    )
    recovery_calls: list[tuple[object, int]] = []
    read_calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        holdout,
        "read_holdout_request",
        lambda _engine, **kwargs: admitted
        if kwargs["candidate_experiment_id"] == "exp-final"
        else None,
    )
    monkeypatch.setattr(
        holdout,
        "read_holdout_bit",
        lambda _engine, **kwargs: read_calls.append(("bit", kwargs["reservation_id"]))
        or reread,
    )
    monkeypatch.setattr(
        holdout,
        "reserve_holdout_check",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("existing request must never be readmitted")
        ),
    )
    monkeypatch.setattr(
        "lab.scorer.holdout_supervisor.run_holdout_recovery_process",
        lambda chosen_id, *, remaining_seconds: recovery_calls.append(
            (chosen_id, remaining_seconds)
        )
        or type("Recovery", (), {"state": "pending"})(),
    )

    with owned_execution(owner):
        result = recover_existing_holdout_request(
            object(),
            run_id=run_id,
            candidate_experiment_id="exp-final",
            trigger_kind="run_end",
            trigger_index=1,
            remaining_seconds=17,
        )

    assert result is reread
    assert recovery_calls == [(reservation_id, 17)]
    assert read_calls == [("bit", reservation_id)]


def test_terminal_failed_receipt_charges_unresolved_budget_before_apply(monkeypatch) -> None:
    """A durable failed bitless receipt charges its old reservation, without readmission."""
    import lab.director.holdout as holdout

    run_id = uuid4()
    receipt = HoldoutReceipt(
        reservation_id=uuid4(),
        state="failed",
        bit=None,
        run_id=run_id,
        candidate_experiment_id="exp-final",
        trigger_kind="run_end",
        trigger_index=1,
        admitted_generation=2,
        execution_sha256="c" * 64,
    )
    result = DirectorLoopResult(
        run_id=run_id,
        status="budget_exhausted",
        completed_proposals=2,
        next_ordinal=3,
        champion_experiment_id="exp-final",
        best_suite=0.25,
        checkpoint_sha256="d" * 64,
    )
    loop = object.__new__(DirectorLoop)
    loop.run_id = run_id
    loop.director_engine = object()
    loop.lease = SimpleNamespace(read_checkpoint=lambda **_kwargs: None)
    loop.artifact_root = None
    loop.restore_run_end_holdout = lambda _result: None
    reservation = EpisodeReservation(uuid4(), 9, 0)
    loop.budget = RunBudget(
        proposal_limit=3,
        wall_limit=120,
        token_limit=0,
        snapshot=BudgetSnapshot(
            wall_seconds=0.0,
            elapsed_wall_seconds=120.0,
            reserved_wall_seconds=9.0,
            reservations=(reservation,),
        ),
    )
    recovered: list[EpisodeReservation] = []

    def charge_unresolved_reservation() -> None:
        recovered.append(reservation)
        loop.budget.reconcile_proposal(
            reservation,
            measured_wall_seconds=float(reservation.wall_seconds),
            measured_model_tokens=0,
        )

    loop._recover_unresolved_holdout_budget = charge_unresolved_reservation
    applied: list[HoldoutReceipt] = []
    loop.apply_run_end_holdout = lambda _result, found: applied.append(found) or result
    monkeypatch.setattr(holdout, "read_run_end_unavailable", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        holdout, "read_run_end_admission_failure", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(holdout, "read_holdout_request", lambda *_args, **_kwargs: receipt)
    monkeypatch.setattr(
        holdout,
        "recover_existing_holdout_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("terminal failed receipt needs no worker recovery")
        ),
    )
    monkeypatch.setattr(
        holdout,
        "evaluate_holdout_check",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("terminal failed receipt must not reserve or query again")
        ),
    )
    monkeypatch.setattr(
        holdout,
        "check_at_run_end",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("terminal failed receipt must not start a new run-end check")
        ),
    )

    before = loop.budget.snapshot()
    assert loop.check_run_end_holdout(result) is result
    after = loop.budget.snapshot()

    assert applied == [receipt]
    assert recovered == [reservation]
    assert before.wall_seconds == 0.0
    assert before.reserved_wall_seconds == 9.0
    assert after.wall_seconds == 9.0
    assert after.reserved_wall_seconds == 0.0
    assert after.reservations == ()
    assert after.proposal_count == before.proposal_count
    assert after.model_tokens == before.model_tokens
