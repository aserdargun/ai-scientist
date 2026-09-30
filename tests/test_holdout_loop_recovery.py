"""Focused restart and boundary regressions for durable holdout work."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from lab.director.budget import BudgetSnapshot, EpisodeReservation, RunBudget
from lab.director.loop import (
    DirectorLoop,
    DirectorLoopResult,
    _budget_to_json,
)
from lab.director.ownership import ExecutionOwner, owned_execution


class _Result:
    def __init__(self, *, rows: list[dict[str, str]] | None = None, value: object = None) -> None:
        self.rows = rows or []
        self.value = value

    def scalars(self) -> _Result:
        return self

    def mappings(self) -> _Result:
        return self

    def all(self) -> list[dict[str, str]]:
        return self.rows

    def scalar_one(self) -> object:
        return self.value


class _Connection:
    def __init__(self, events: list[dict[str, str]]) -> None:
        self.events = events

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: object, _params: object = None) -> _Result:
        if "extract(epoch FROM clock_timestamp()-started_at)" in str(statement):
            return _Result(value=80.0)
        if "SELECT event_json->>'key' AS key" in str(statement):
            return _Result(rows=self.events)
        return _Result(value=None)


class _Engine:
    def __init__(self, events: list[dict[str, str]]) -> None:
        self.events = events

    def connect(self) -> _Connection:
        return _Connection(self.events)


class _Lease:
    def __init__(self, checkpoints: dict[str, dict[str, object]]) -> None:
        self.checkpoints = checkpoints
        self.appended: list[dict[str, object]] = []

    def read_checkpoint(self, *, key: str, artifact_root: Path) -> dict[str, object] | None:
        del artifact_root
        return self.checkpoints.get(key)

    def append_checkpoint(self, **kwargs: object) -> dict[str, object]:
        record = dict(kwargs)
        self.appended.append(record)
        return record

    def append_holdout_closure_checkpoint(self, **kwargs: object) -> dict[str, object]:
        record = dict(kwargs)
        record["holdout_closure"] = True
        self.appended.append(record)
        return record


def _loop_for_budget(
    *, events: list[dict[str, str]], checkpoints: dict[str, dict[str, object]]
) -> DirectorLoop:
    loop = object.__new__(DirectorLoop)
    loop.director_engine = _Engine(events)
    loop.lease = _Lease(checkpoints)
    loop.run_id = uuid4()
    loop.artifact_root = Path("/private/test-artifacts")
    return loop


def test_reconciled_holdout_budget_wins_over_stale_state_on_restart() -> None:
    budget_json = _budget_to_json(
        BudgetSnapshot(wall_seconds=47.5, model_tokens=0, elapsed_wall_seconds=20.0)
    )
    key = "holdout-budget-reconciled:receipt"
    loop = _loop_for_budget(
        events=[{"key": key}, {"key": "director-state:10"}],
        checkpoints={
            key: {
                "payload": {"budget": budget_json},
                "receipt": {"phase": "holdout_budget_reconciled"},
            }
        },
    )
    loop.lease.checkpoints[key]["payload"]["run_id"] = str(loop.run_id)
    restored = loop._latest_budget_checkpoint(BudgetSnapshot())
    resumed = RunBudget(proposal_limit=10, wall_limit=200, token_limit=0, snapshot=restored)

    assert restored.wall_seconds == 47.5
    assert resumed.remaining_wall_seconds == 152.5
    assert resumed.snapshot().reserved_wall_seconds == 0


def test_unresolved_holdout_reservation_is_charged_fully_after_restart() -> None:
    run_id = uuid4()
    reservation_id = uuid4()
    reservation_key = f"holdout-budget-reserved:{reservation_id}"
    reservation = EpisodeReservation(reservation_id, 60, 0)
    loop = _loop_for_budget(
        events=[{"key": reservation_key}],
        checkpoints={
            reservation_key: {
                "payload": {
                    "run_id": str(run_id),
                    "trigger_kind": "keep_interval",
                    "trigger_index": 10,
                    "candidate_experiment_id": "exp-10",
                    "reservation_id": str(reservation_id),
                    "wall_seconds": 60,
                    "model_tokens": 0,
                    "budget": _budget_to_json(
                        BudgetSnapshot(
                            reserved_wall_seconds=60,
                            reservations=(reservation,),
                        )
                    ),
                },
                "receipt": {"phase": "holdout_budget_reserved"},
            }
        },
    )
    loop.run_id = run_id
    loop.budget = RunBudget(proposal_limit=10, wall_limit=100, token_limit=0)

    loop._recover_unresolved_holdout_budget()

    snapshot = loop.budget.snapshot()
    assert snapshot.wall_seconds == pytest.approx(60, abs=0.01)
    assert snapshot.reserved_wall_seconds == 0
    assert snapshot.reservations == ()
    assert loop.budget.remaining_wall_seconds == pytest.approx(40, abs=0.01)
    assert loop.lease.appended[-1]["phase"] == "holdout_budget_reconciled"


def test_unresolved_holdout_charges_original_reservation_and_preserves_elapsed() -> None:
    run_id = uuid4()
    reservation_id = uuid4()
    reservation_key = f"holdout-budget-reserved:{reservation_id}"
    reservation = EpisodeReservation(reservation_id, 60, 0)
    loop = _loop_for_budget(
        events=[{"key": reservation_key}],
        checkpoints={
            reservation_key: {
                "payload": {
                    "run_id": str(run_id),
                    "trigger_kind": "run_end",
                    "trigger_index": 1,
                    "candidate_experiment_id": "exp-10",
                    "reservation_id": str(reservation_id),
                    "wall_seconds": 60,
                    "model_tokens": 0,
                    "budget": _budget_to_json(
                        BudgetSnapshot(
                            wall_seconds=20,
                            elapsed_wall_seconds=80,
                            reserved_wall_seconds=60,
                            reservations=(reservation,),
                        )
                    ),
                },
                "receipt": {"phase": "holdout_budget_reserved"},
            }
        },
    )
    loop.run_id = run_id
    loop.budget = RunBudget(
        proposal_limit=10,
        wall_limit=300,
        token_limit=0,
        snapshot=BudgetSnapshot(
            wall_seconds=20,
            elapsed_wall_seconds=80,
            reserved_wall_seconds=60,
            reservations=(reservation,),
        ),
    )

    with owned_execution(ExecutionOwner(run_id, 1, "2" * 32, "f" * 64)):
        loop._recover_unresolved_holdout_budget()

    snapshot = loop.budget.snapshot()
    assert snapshot.wall_seconds == pytest.approx(80, abs=0.01)
    assert snapshot.reserved_wall_seconds == 0
    assert snapshot.reservations == ()
    assert loop.budget.remaining_wall_seconds == pytest.approx(220, abs=0.01)


def test_periodic_reconciliation_runs_before_exact_proposal_limit_return(monkeypatch) -> None:
    from lab.director import loop as loop_module

    monkeypatch.setattr(
        loop_module, "read_registered_calibration", lambda *_args, **_kwargs: object()
    )
    loop = object.__new__(DirectorLoop)
    state = SimpleNamespace(completed_proposals=10)
    rolled_back_state = SimpleNamespace(completed_proposals=10, champion="approved")
    checkpoint = {"marker": "before-application"}
    applied_checkpoint = {"marker": "applied-rollback"}
    calls: list[str] = []
    loop.run_id = uuid4()
    loop.director_engine = object()
    loop.lease = SimpleNamespace(heartbeat=lambda: None)
    loop.artifact_root = Path("/private/test-artifacts")
    loop.proposal_limit = 10
    loop._verify_execution_identity = lambda _calibration: None
    loop._load_or_initialize = lambda _calibration: (state, checkpoint)
    loop._recover_unresolved_holdout_budget = lambda: None

    def reconcile(current: object, receipt: object) -> tuple[object, object]:
        assert current is state
        assert receipt is checkpoint
        calls.append("applied durable periodic result")
        return rolled_back_state, applied_checkpoint

    loop._reconcile_periodic_holdout = reconcile
    loop._result = lambda final_state, final_receipt, status: (
        final_state,
        final_receipt,
        status,
    )

    final_state, final_receipt, status = loop.run()

    assert calls == ["applied durable periodic result"]
    assert final_state is rolled_back_state
    assert final_receipt is applied_checkpoint
    assert status == "proposal_limit_reached"


def test_run_end_wrapper_parses_uuid_json_checkpoint_strictly(monkeypatch) -> None:
    from lab.director import holdout as holdout_module
    from lab.director.holdout import HoldoutReceipt

    run_id = uuid4()
    digest = "a" * 64
    state = {
        "schema": "director-loop-state.v1",
        "run_id": str(run_id),
        "suite_manifest_sha256": "b" * 64,
        "suite_id": "fixture-suite",
        "suite_version": 1,
        "calibration_sha256": "c" * 64,
        "harness_sha256": "d" * 64,
        "image_sha256": "e" * 64,
        "proposal_limit": 1,
        "completed_proposals": 0,
        "next_ordinal": 1,
        "champion_experiment_id": "baseline",
        "champion_source_sha256": "f" * 64,
        "champion_tree_sha256": "1" * 40,
        "champion_source_blob_sha256": "2" * 64,
        "champion_seed0_by_task": {"task": 0.1},
        "champion_seed1_by_task": {"task": 0.1},
        "champion_suite_seed_scores": [0.1, 0.1, 0.1],
        "champion_noise_sd": 0.01,
        "best_suite": 0.1,
        "consecutive_non_keep": 0,
        "explore_proposals": 0,
        "explore_family": None,
        "consecutive_candidate_crashes": 0,
        "discard_streak": 0,
        "previous_move_type": None,
        "recent_feedback": [],
        "budget": _budget_to_json(BudgetSnapshot()),
    }
    state_key = "director-state:0"
    state_checkpoint = {
        "payload": state,
        "receipt": {
            "phase": "director_loop_state",
            "key": state_key,
            "payload_sha256": digest,
        },
    }

    class _RunEndEngine(_Engine):
        def connect(self) -> _Connection:
            class _NoFenceConnection(_Connection):
                def execute(self, statement: object, _params: object = None) -> _Result:
                    del statement
                    return _Result(value=None)

            return _NoFenceConnection([])

    class _RunEndLease(_Lease):
        def read_checkpoint(self, *, key: str, artifact_root: Path) -> dict[str, object] | None:
            del artifact_root
            if key == "director-holdout-run-end-intent":
                return None
            return state_checkpoint if key == state_key else None

        def append_checkpoint(self, **kwargs: object) -> dict[str, object]:
            record = dict(kwargs)
            self.appended.append(record)
            return {
                "key": kwargs["key"],
                "phase": kwargs["phase"],
                "sequence": kwargs["sequence"],
                "payload_sha256": digest,
            }

    result = DirectorLoopResult(
        run_id=run_id,
        status="proposal_limit_reached",
        completed_proposals=0,
        next_ordinal=1,
        champion_experiment_id="baseline",
        best_suite=0.1,
        checkpoint_sha256=digest,
    )
    loop = object.__new__(DirectorLoop)
    loop.run_id = run_id
    loop.director_engine = _RunEndEngine([])
    loop.lease = _RunEndLease({})
    loop.artifact_root = Path("/private/test-artifacts")
    loop.budget = RunBudget(proposal_limit=1, wall_limit=60, token_limit=0)
    loop.restore_run_end_holdout = lambda _result: None
    loop._state_key_for_digest = lambda _digest: state_key
    loop._next_checkpoint_sequence = lambda: len(loop.lease.appended) + 1
    loop._persist_state = lambda *_args, **_kwargs: {
        "payload_sha256": digest,
        "key": state_key,
    }
    loop.apply_run_end_holdout = lambda _result, _receipt: result
    monkeypatch.setattr(holdout_module, "read_holdout_request", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        holdout_module,
        "read_run_end_admission_registration",
        lambda *_a, **_kw: (True, False),
    )
    monkeypatch.setattr(
        holdout_module,
        "check_at_run_end",
        lambda *_a, **_kw: HoldoutReceipt(
            reservation_id=uuid4(),
            state="exhausted",
            bit=None,
            run_id=run_id,
            candidate_experiment_id="baseline",
            trigger_kind="run_end",
            trigger_index=1,
        ),
    )

    with owned_execution(ExecutionOwner(run_id, 1, "2" * 32, "f" * 64)):
        assert loop.check_run_end_holdout(result) is result


def test_registered_run_end_intent_without_reservation_is_fenced_without_new_query(
    monkeypatch,
) -> None:
    from lab.director import holdout as holdout_module
    from lab.director.holdout import RunEndAdmissionFailure

    run_id = uuid4()
    intent_sha = "a" * 64
    budget_sha = "b" * 64
    budget_key = "holdout-budget-reconciled:reservation"
    intent = {
        "payload": {
            "run_id": str(run_id),
            "candidate_experiment_id": "approved-candidate",
            "admitted_generation": 1,
            "execution_sha256": "f" * 64,
        },
        "receipt": {
            "phase": "holdout_run_end_intent",
            "sequence": 21,
            "payload_sha256": intent_sha,
        },
    }
    budget_checkpoint = {
        "payload": {
            "run_id": str(run_id),
            "trigger_kind": "run_end",
            "trigger_index": 1,
            "budget": _budget_to_json(BudgetSnapshot(wall_seconds=40)),
        },
        "receipt": {
            "key": budget_key,
            "phase": "holdout_budget_reconciled",
            "payload_sha256": budget_sha,
        },
    }
    failure = RunEndAdmissionFailure(
        run_id=run_id,
        candidate_experiment_id="approved-candidate",
        intent_checkpoint_sha256=intent_sha,
        intent_checkpoint_sequence=21,
        budget_checkpoint_key=budget_key,
        budget_checkpoint_sha256=budget_sha,
        failure_kind="missing_reservation_unverifiable_budget",
        admitted_generation=1,
        execution_sha256="f" * 64,
    )
    result = DirectorLoopResult(
        run_id=run_id,
        status="proposal_limit_reached",
        completed_proposals=10,
        next_ordinal=11,
        champion_experiment_id="approved-candidate",
        best_suite=0.1,
        checkpoint_sha256="c" * 64,
    )
    loop = object.__new__(DirectorLoop)
    loop.run_id = run_id
    loop.director_engine = _Engine([])
    loop.lease = _Lease({"director-holdout-run-end-intent": intent})
    loop.artifact_root = Path("/private/test-artifacts")
    loop.restore_run_end_holdout = lambda _result: None
    loop._recover_unresolved_holdout_budget = lambda: None
    loop._latest_reconciled_run_end_budget = lambda: budget_checkpoint
    loop.apply_run_end_admission_failure = lambda _result, applied: (
        result if applied == failure else None
    )
    calls: list[str] = []

    def denied_new_reservation(*_args, **_kwargs):
        calls.append("new query")
        raise AssertionError("existing run-end intent must never admit a new query")

    monkeypatch.setattr(holdout_module, "read_run_end_admission_failure", lambda *_a, **_kw: None)
    monkeypatch.setattr(holdout_module, "read_holdout_request", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        holdout_module,
        "read_run_end_admission_registration",
        lambda *_a, **_kw: (True, False),
    )
    monkeypatch.setattr(
        holdout_module, "fence_missing_run_end_admission", lambda *_a, **_kw: failure
    )
    monkeypatch.setattr(holdout_module, "check_at_run_end", denied_new_reservation)
    loop._state_key_for_digest = lambda _digest: "director-state:10"
    loop.budget = SimpleNamespace(reserve_work=denied_new_reservation)

    assert loop.check_run_end_holdout(result) is result
    assert calls == []


def test_no_row_failure_rpc_receipt_is_bitless_identity_only() -> None:
    from lab.director.holdout import _parse_run_end_admission_failure

    run_id = uuid4()
    payload = {
        "run_id": str(run_id),
        "candidate_experiment_id": "approved-candidate",
        "intent_checkpoint_sha256": "a" * 64,
        "intent_checkpoint_sequence": 12,
        "budget_checkpoint_key": "holdout-budget-reconciled:reservation",
        "budget_checkpoint_sha256": "b" * 64,
        "state": "failed",
        "bit": None,
        "failure_kind": "missing_reservation_unverifiable_budget",
        "admitted_generation": 1,
        "execution_sha256": "f" * 64,
    }

    receipt = _parse_run_end_admission_failure(payload)

    assert receipt.run_id == run_id
    assert receipt.state == "failed"
    assert receipt.bit is None
    with pytest.raises(ValueError, match="invalid run-end"):
        _parse_run_end_admission_failure({**payload, "metrics": {"secret": 1}})


def test_zero_wall_budget_routes_to_durable_unavailable_outcome(monkeypatch) -> None:
    from lab.director import holdout as holdout_module

    run_id = uuid4()
    result = DirectorLoopResult(
        run_id=run_id,
        status="budget_exhausted",
        completed_proposals=0,
        next_ordinal=1,
        champion_experiment_id="baseline",
        best_suite=0.1,
        checkpoint_sha256="c" * 64,
    )
    loop = object.__new__(DirectorLoop)
    loop.run_id = run_id
    loop.director_engine = _Engine([])
    loop.lease = _Lease({})
    loop.artifact_root = Path("/private/test-artifacts")
    loop.restore_run_end_holdout = lambda _result: None
    loop.budget = RunBudget(
        proposal_limit=1,
        wall_limit=60,
        token_limit=0,
        snapshot=BudgetSnapshot(wall_seconds=60),
    )
    sentinel = object()
    calls: list[object] = []
    monkeypatch.setattr(holdout_module, "read_run_end_unavailable", lambda *_a, **_kw: None)
    monkeypatch.setattr(holdout_module, "read_run_end_admission_failure", lambda *_a, **_kw: None)
    monkeypatch.setattr(holdout_module, "read_holdout_request", lambda *_a, **_kw: None)
    loop._fence_run_end_unavailable = lambda _result, **kwargs: calls.append(kwargs) or sentinel
    loop.apply_run_end_unavailable = lambda _result, failure: failure

    assert loop.check_run_end_holdout(result) is sentinel
    assert len(calls) == 1
    assert calls[0]["reason"] == "fresh_wall_budget_unavailable"
    assert calls[0]["remaining_wall_seconds"] == 0
    assert loop.budget.snapshot().reserved_wall_seconds == 0


def test_run_startup_stops_on_terminal_intent_before_budget_recovery_or_provider(
    monkeypatch,
) -> None:
    from lab.director import holdout as holdout_module
    from lab.director import loop as loop_module

    run_id = uuid4()
    state = _minimal_loop_state(run_id, "candidate-10")
    intent = {
        "payload": {
            "run_id": str(run_id),
            "candidate_experiment_id": "candidate-10",
            "terminal_status": "budget_exhausted",
        },
        "receipt": {"phase": "holdout_run_end_intent"},
    }
    lease = _Lease({"director-holdout-run-end-intent": intent})
    lease.heartbeat = lambda: None
    loop = object.__new__(DirectorLoop)
    loop.run_id = run_id
    loop.director_engine = _Engine([])
    loop.planner_engine = object()
    loop.lease = lease
    loop.runner = object()
    loop.tasks = ()
    loop.suite_manifest_sha256 = "a" * 64
    loop.provider = object()
    loop.budget = RunBudget(proposal_limit=1, wall_limit=60, token_limit=0)
    loop.artifact_root = Path("/private/test-artifacts")
    loop.proposal_limit = 1
    loop.holdout_enabled = True
    state_receipt = {
        "key": "director-state:0",
        "phase": "director_loop_state",
        "payload_sha256": "d" * 64,
    }
    loop._verify_execution_identity = lambda _calibration: None
    loop._load_or_initialize = lambda _calibration: (state, state_receipt)
    loop._recover_unresolved_holdout_budget = lambda: (_ for _ in ()).throw(
        AssertionError("terminal replay must return before budget recovery")
    )
    check_calls: list[DirectorLoopResult] = []
    loop.check_run_end_holdout = lambda terminal: check_calls.append(terminal) or terminal
    monkeypatch.setattr(loop_module, "read_registered_calibration", lambda *_a, **_k: object())
    monkeypatch.setattr(holdout_module, "read_run_end_unavailable", lambda *_a, **_k: None)
    monkeypatch.setattr(
        holdout_module,
        "read_run_end_admission_registration",
        lambda *_a, **_kw: (False, False),
    )

    result = loop.run()

    assert result.status == "budget_exhausted"
    assert result.champion_experiment_id == "candidate-10"
    assert result.checkpoint_sha256 == "d" * 64
    assert len(check_calls) == 1


def test_latest_budget_checkpoint_uses_unavailable_intent_snapshot() -> None:
    run_id = uuid4()
    frozen = _budget_to_json(BudgetSnapshot(wall_seconds=58.0, elapsed_wall_seconds=60.0))
    key = "director-holdout-run-end-unavailable-intent:1"
    loop = _loop_for_budget(
        events=[{"key": key}],
        checkpoints={
            key: {
                "payload": {
                    "run_id": str(run_id),
                    "budget_snapshot": frozen,
                },
                "receipt": {"phase": "holdout_run_end_unavailable_intent"},
            }
        },
    )
    loop.run_id = run_id

    snapshot = loop._latest_budget_checkpoint(BudgetSnapshot())

    assert snapshot.wall_seconds == 58.0
    assert snapshot.elapsed_wall_seconds == 60.0


@pytest.mark.parametrize("phase", ["proposal_budget_reconciled", "seed_complete"])
def test_legacy_budget_checkpoint_without_payload_run_id_is_restored(phase: str) -> None:
    run_id = uuid4()
    budget_json = _budget_to_json(BudgetSnapshot(wall_seconds=31.0, elapsed_wall_seconds=17.0))
    key = f"legacy-{phase}"
    loop = _loop_for_budget(
        events=[{"key": key}],
        checkpoints={
            key: {
                "payload": {"budget": budget_json},
                "receipt": {"phase": phase},
            }
        },
    )
    loop.run_id = run_id

    restored = loop._latest_budget_checkpoint(BudgetSnapshot())

    assert restored.wall_seconds == 31.0
    assert restored.elapsed_wall_seconds == 17.0


def test_unavailable_intent_freeze_retains_original_terminal_status_on_retry(monkeypatch) -> None:
    from lab.director import holdout as holdout_module

    run_id = uuid4()
    state = _minimal_loop_state(run_id, "candidate-10")
    state = state.model_copy(update={"completed_proposals": 4, "next_ordinal": 5})
    state_receipt = {
        "key": "director-state:4",
        "phase": "director_loop_state",
        "payload_sha256": "d" * 64,
        "sequence": 4,
    }
    budget_snapshot = _budget_to_json(BudgetSnapshot(wall_seconds=55.0, elapsed_wall_seconds=25.0))
    frozen_payload = {
        "schema": "director-run-end-unavailable-intent.v1",
        "run_id": str(run_id),
        "candidate_experiment_id": "candidate-10",
        "reason": "fresh_wall_budget_unavailable",
        "terminal_status": "budget_exhausted",
        "prior_state_key": "director-state:4",
        "prior_state_sha256": "d" * 64,
        "prior_state_sequence": 4,
        "budget_snapshot": budget_snapshot,
        "budget_snapshot_sha256": DirectorLoop._budget_snapshot_sha256(budget_snapshot),
        "remaining_wall_seconds": 0.25,
        "original_intent_checkpoint_sha256": None,
        "original_intent_checkpoint_sequence": None,
        "admitted_generation": 1,
        "execution_sha256": "f" * 64,
    }
    frozen_key = "director-holdout-run-end-unavailable-intent:1"
    loop = _loop_for_budget(
        events=[{"key": frozen_key}],
        checkpoints={
            frozen_key: {
                "payload": frozen_payload,
                "receipt": {"phase": "holdout_run_end_unavailable_intent"},
            }
        },
    )
    loop.run_id = run_id
    loop.lease.checkpoints["director-state:4"] = {
        "payload": state.model_dump(mode="json"),
        "receipt": state_receipt,
    }
    loop._next_checkpoint_sequence = lambda: 5
    loop._budget_snapshot_sha256 = DirectorLoop._budget_snapshot_sha256
    loop.check_run_end_holdout = lambda terminal: terminal
    loop.holdout_enabled = True
    loop.budget = RunBudget(
        proposal_limit=35,
        wall_limit=100,
        token_limit=0,
        snapshot=BudgetSnapshot(wall_seconds=99.0, elapsed_wall_seconds=1.0),
    )
    monkeypatch.setattr(holdout_module, "read_run_end_unavailable", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        holdout_module,
        "read_run_end_admission_registration",
        lambda *_a, **_kw: (False, False),
    )

    result = loop._pending_run_end_terminal_result(state, state_receipt)

    assert result is not None
    assert result.status == "budget_exhausted"
    resumed_snapshot = loop.budget.snapshot()
    assert resumed_snapshot.wall_seconds == 55.0
    assert resumed_snapshot.elapsed_wall_seconds == pytest.approx(25.0, abs=0.01)


def test_loop_startup_observes_frozen_zero_budget_before_any_new_proposal(monkeypatch) -> None:
    from lab.director import holdout as holdout_module
    from lab.director import loop as loop_module

    run_id = uuid4()
    frozen_budget = _budget_to_json(BudgetSnapshot(wall_seconds=0.0, elapsed_wall_seconds=120.0))
    frozen_key = "director-holdout-run-end-unavailable-intent:1"
    frozen_payload = {
        "schema": "director-run-end-unavailable-intent.v1",
        "run_id": str(run_id),
        "candidate_experiment_id": "candidate-4",
        "reason": "fresh_wall_budget_unavailable",
        "terminal_status": "budget_exhausted",
        "prior_state_key": "director-state:4",
        "prior_state_sha256": "d" * 64,
        "prior_state_sequence": 4,
        "budget_snapshot": frozen_budget,
        "budget_snapshot_sha256": DirectorLoop._budget_snapshot_sha256(frozen_budget),
        "remaining_wall_seconds": 0.25,
        "original_intent_checkpoint_sha256": None,
        "original_intent_checkpoint_sequence": None,
    }
    lease = _Lease(
        {
            frozen_key: {
                "payload": frozen_payload,
                "receipt": {"phase": "holdout_run_end_unavailable_intent"},
            }
        }
    )
    lease.heartbeat = lambda: None
    loop = object.__new__(DirectorLoop)
    loop.run_id = run_id
    loop.director_engine = _Engine([])
    loop.planner_engine = object()
    loop.lease = lease
    loop.runner = object()
    loop.tasks = ()
    loop.suite_manifest_sha256 = "a" * 64
    loop.provider = object()
    loop.budget = RunBudget(
        proposal_limit=35,
        wall_limit=120,
        token_limit=0,
        snapshot=BudgetSnapshot(wall_seconds=119.0, elapsed_wall_seconds=1.0),
    )
    loop.artifact_root = Path("/private/test-artifacts")
    loop.proposal_limit = 35
    loop.holdout_enabled = True
    state = _minimal_loop_state(run_id, "candidate-4").model_copy(
        update={
            "completed_proposals": 4,
            "next_ordinal": 5,
            "proposal_limit": 35,
            "budget": _budget_to_json(BudgetSnapshot(wall_seconds=119.0, elapsed_wall_seconds=1.0)),
        }
    )
    state_receipt = {
        "key": "director-state:4",
        "phase": "director_loop_state",
        "payload_sha256": "d" * 64,
        "sequence": 4,
    }
    loop._verify_execution_identity = lambda _calibration: None
    loop._load_or_initialize = lambda _calibration: (state, state_receipt)
    loop._recover_unresolved_holdout_budget = lambda: (_ for _ in ()).throw(
        AssertionError("frozen zero-budget terminal must precede fresh budget admission")
    )
    checked: list[DirectorLoopResult] = []
    loop.check_run_end_holdout = lambda terminal: checked.append(terminal) or terminal
    monkeypatch.setattr(loop_module, "read_registered_calibration", lambda *_a, **_kw: object())
    monkeypatch.setattr(holdout_module, "read_run_end_unavailable", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        holdout_module,
        "read_run_end_admission_registration",
        lambda *_a, **_kw: (False, False),
    )

    result = loop.run()

    assert result.status == "budget_exhausted"
    resumed_snapshot = loop.budget.snapshot()
    assert resumed_snapshot.wall_seconds == 0.0
    assert resumed_snapshot.elapsed_wall_seconds == pytest.approx(120.0, abs=0.01)
    assert checked == [result]


def test_unregistered_intent_uses_checkpoint_only_fence_without_query(monkeypatch) -> None:
    from lab.director import holdout as holdout_module

    run_id = uuid4()
    intent = {
        "payload": {
            "run_id": str(run_id),
            "candidate_experiment_id": "candidate-10",
        },
        "receipt": {
            "phase": "holdout_run_end_intent",
            "sequence": 11,
            "payload_sha256": "a" * 64,
        },
    }
    result = DirectorLoopResult(
        run_id=run_id,
        status="budget_exhausted",
        completed_proposals=10,
        next_ordinal=11,
        champion_experiment_id="candidate-10",
        best_suite=0.1,
        checkpoint_sha256="c" * 64,
    )
    loop = object.__new__(DirectorLoop)
    loop.run_id = run_id
    loop.director_engine = _Engine([])
    loop.lease = _Lease({"director-holdout-run-end-intent": intent})
    loop.artifact_root = Path("/private/test-artifacts")
    loop.restore_run_end_holdout = lambda _result: None
    loop._recover_unresolved_holdout_budget = lambda: None
    loop.budget = SimpleNamespace(remaining_wall_seconds=0.25)
    sentinel = object()
    captured: list[object] = []
    monkeypatch.setattr(holdout_module, "read_run_end_unavailable", lambda *_a, **_kw: None)
    monkeypatch.setattr(holdout_module, "read_run_end_admission_failure", lambda *_a, **_kw: None)
    monkeypatch.setattr(holdout_module, "read_holdout_request", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        holdout_module,
        "read_run_end_admission_registration",
        lambda *_a, **_kw: (False, False),
    )
    loop._fence_run_end_unavailable = lambda _result, **kwargs: captured.append(kwargs) or sentinel
    loop.apply_run_end_unavailable = lambda _result, failure: failure

    assert loop.check_run_end_holdout(result) is sentinel
    assert len(captured) == 1
    assert captured[0]["reason"] == "intent_checkpoint_without_sql_registration"
    assert captured[0]["original_intent"]["receipt"]["payload_sha256"] == "a" * 64


def test_unavailable_receipt_is_typed_and_bitless() -> None:
    from lab.director.holdout import _parse_run_end_unavailable

    run_id = uuid4()
    payload = {
        "run_id": str(run_id),
        "candidate_experiment_id": "baseline",
        "reason": "fresh_wall_budget_unavailable",
        "prior_state_key": "director-state:0",
        "prior_state_sha256": "a" * 64,
        "prior_state_sequence": 3,
        "budget_snapshot_sha256": "b" * 64,
        "unavailable_checkpoint_sha256": "c" * 64,
        "unavailable_checkpoint_sequence": 4,
        "original_intent_checkpoint_sha256": None,
        "original_intent_checkpoint_sequence": None,
        "reservation_checkpoint_key": None,
        "reservation_checkpoint_sha256": None,
        "reservation_checkpoint_sequence": None,
        "state": "failed",
        "bit": None,
        "admitted_generation": 1,
        "execution_sha256": "f" * 64,
    }

    receipt = _parse_run_end_unavailable(payload)

    assert receipt.run_id == run_id
    assert receipt.reason == "fresh_wall_budget_unavailable"
    assert receipt.state == "failed" and receipt.bit is None
    with pytest.raises(ValueError, match="invalid run-end unavailable"):
        _parse_run_end_unavailable({**payload, "metrics": {"private": True}})


def test_run_end_failure_restore_accepts_rolled_back_champion_but_binds_prior_candidate() -> None:
    from lab.director.ownership import ExecutionOwner, owned_execution

    run_id = uuid4()
    prior = _minimal_loop_state(run_id, "attempted-candidate")
    current = _minimal_loop_state(run_id, "approved-champion")
    prior_sha = "a" * 64
    current_sha = "d" * 64
    application_sha = "e" * 64
    app_payload = {
        "run_id": str(run_id),
        "candidate_experiment_id": "attempted-candidate",
        "intent_checkpoint_sha256": "b" * 64,
        "intent_checkpoint_sequence": 11,
        "budget_checkpoint_key": "holdout-budget-reconciled:test",
        "budget_checkpoint_sha256": "c" * 64,
        "failure_kind": "missing_reservation_unverifiable_budget",
        "admitted_generation": 1,
        "execution_sha256": "f" * 64,
        "prior_state_sha256": prior_sha,
    }
    marker = {
        "schema": "director-holdout-state-application.v1",
        "run_id": str(run_id),
        "application_sha256": application_sha,
        "state_sha256": current_sha,
    }
    checkpoints = {
        "holdout-run-end-admission-failure:1": {
            "payload": app_payload,
            "receipt": {
                "phase": "holdout_run_end_admission_failure",
                "payload_sha256": application_sha,
            },
        },
        "director-holdout-run-end-intent": {
            "payload": {
                "admitted_generation": 1,
                "execution_sha256": "f" * 64,
            },
            "receipt": {
                "payload_sha256": "b" * 64,
                "sequence": 11,
            },
        },
        "prior-state": {
            "payload": prior.model_dump(mode="json", by_alias=True),
            "receipt": {"payload_sha256": prior_sha},
        },
        "current-state": {
            "payload": current.model_dump(mode="json", by_alias=True),
            "receipt": {"payload_sha256": current_sha},
        },
        "holdout-state-application:run_end:1": {"payload": marker},
    }
    loop = object.__new__(DirectorLoop)
    loop.run_id = run_id
    loop.lease = _Lease(checkpoints)
    loop.artifact_root = Path("/private/test-artifacts")
    loop._state_key_for_digest = lambda _digest: "prior-state"
    loop._latest_state_key = lambda: "current-state"
    result = DirectorLoopResult(
        run_id=run_id,
        status="budget_exhausted",
        completed_proposals=0,
        next_ordinal=1,
        champion_experiment_id="approved-champion",
        best_suite=0.1,
        checkpoint_sha256=current_sha,
    )

    with owned_execution(ExecutionOwner(run_id, 1, "2" * 32, "f" * 64)):
        restored = loop._restore_run_end_admission_failure(result)

    assert restored is not None
    assert restored.champion_experiment_id == "approved-champion"


def test_run_end_failure_restore_rejects_candidate_not_bound_to_prior_state() -> None:
    from lab.director.ownership import ExecutionOwner, owned_execution

    run_id = uuid4()
    prior = _minimal_loop_state(run_id, "different-candidate")
    prior_sha = "a" * 64
    app_payload = {
        "run_id": str(run_id),
        "candidate_experiment_id": "attempted-candidate",
        "intent_checkpoint_sha256": "b" * 64,
        "intent_checkpoint_sequence": 11,
        "budget_checkpoint_key": "holdout-budget-reconciled:test",
        "budget_checkpoint_sha256": "c" * 64,
        "failure_kind": "missing_reservation_unverifiable_budget",
        "admitted_generation": 1,
        "execution_sha256": "f" * 64,
        "prior_state_sha256": prior_sha,
    }
    loop = object.__new__(DirectorLoop)
    loop.run_id = run_id
    loop.lease = _Lease(
        {
            "holdout-run-end-admission-failure:1": {
                "payload": app_payload,
                "receipt": {"phase": "holdout_run_end_admission_failure"},
            },
            "director-holdout-run-end-intent": {
                "payload": {
                    "admitted_generation": 1,
                    "execution_sha256": "f" * 64,
                },
                "receipt": {
                    "payload_sha256": "b" * 64,
                    "sequence": 11,
                },
            },
            "prior-state": {
                "payload": prior.model_dump(mode="json", by_alias=True),
                "receipt": {"payload_sha256": prior_sha},
            },
        }
    )
    loop.artifact_root = Path("/private/test-artifacts")
    loop._state_key_for_digest = lambda _digest: "prior-state"
    result = DirectorLoopResult(
        run_id=run_id,
        status="budget_exhausted",
        completed_proposals=0,
        next_ordinal=1,
        champion_experiment_id="approved-champion",
        best_suite=0.1,
        checkpoint_sha256="d" * 64,
    )

    with owned_execution(ExecutionOwner(run_id, 1, "2" * 32, "f" * 64)):
        with pytest.raises(RuntimeError, match="candidate differs from its hash-bound prior state"):
            loop._restore_run_end_admission_failure(result)


def test_run_end_failure_restore_rejects_different_captured_generation() -> None:
    from lab.director.ownership import ExecutionOwner, owned_execution

    run_id = uuid4()
    loop = object.__new__(DirectorLoop)
    loop.run_id = run_id
    loop.artifact_root = Path("/private/test-artifacts")
    loop.lease = _Lease(
        {
            "holdout-run-end-admission-failure:1": {
                "payload": {
                    "run_id": str(run_id),
                    "candidate_experiment_id": "candidate",
                    "intent_checkpoint_sha256": "a" * 64,
                    "intent_checkpoint_sequence": 3,
                    "budget_checkpoint_key": "budget",
                    "budget_checkpoint_sha256": "b" * 64,
                    "failure_kind": "missing_reservation_unverifiable_budget",
                    "admitted_generation": 2,
                    "execution_sha256": "e" * 64,
                },
                "receipt": {"phase": "holdout_run_end_admission_failure"},
            }
        }
    )
    result = DirectorLoopResult(
        run_id=run_id,
        status="budget_exhausted",
        completed_proposals=0,
        next_ordinal=1,
        champion_experiment_id="candidate",
        best_suite=0.1,
        checkpoint_sha256="c" * 64,
    )

    with owned_execution(ExecutionOwner(run_id, 1, "2" * 32, "f" * 64)):
        with pytest.raises(RuntimeError, match="differs from captured execution owner"):
            loop._restore_run_end_admission_failure(result)


def test_holdout_closure_checkpoint_api_rejects_arbitrary_or_stale_payload() -> None:
    from lab.director.journal import DirectorRunLease
    from lab.director.ownership import ExecutionOwner, owned_execution

    run_id = uuid4()
    lease = object.__new__(DirectorRunLease)
    lease.run_id = run_id
    owner = ExecutionOwner(run_id, 1, "2" * 32, "f" * 64)
    base = {
        "schema": "director-run-end-unavailable-intent.v1",
        "run_id": str(run_id),
        "admitted_generation": owner.generation,
        "execution_sha256": owner.execution_sha256,
        "reason": "fresh_wall_budget_unavailable",
    }

    with owned_execution(owner):
        with pytest.raises(ValueError, match="allowed run-end"):
            lease.append_holdout_closure_checkpoint(
                sequence=1,
                key="director-proposal-intent:1",
                phase="proposal_intent",
                payload=base,
            )
        with pytest.raises(ValueError, match="allowed run-end"):
            lease.append_holdout_closure_checkpoint(
                sequence=1,
                key="director-holdout-run-end-unavailable-intent:1",
                phase="holdout_run_end_unavailable_intent",
                payload={**base, "execution_sha256": "e" * 64},
            )


def test_holdout_closure_checkpoint_api_accepts_consistent_v2_state() -> None:
    from lab.director.journal import DirectorRunLease
    from lab.director.loop import (
        DirectorLoopState,
        HoldoutApprovedSnapshot,
        _strategy_checkpoint_fields,
        _strategy_seed_for_run,
    )
    from lab.director.ownership import ExecutionOwner, owned_execution
    from lab.director.strategy import initial_strategy_state

    run_id = uuid4()
    owner = ExecutionOwner(run_id, 4, "2" * 32, "f" * 64)
    state = _minimal_loop_state(run_id, "baseline")
    snapshot = HoldoutApprovedSnapshot.from_state(state, keep_count=0)
    strategy = initial_strategy_state(_strategy_seed_for_run(run_id))
    state_payload = {
        **state.model_dump(mode="json", by_alias=True),
        **_strategy_checkpoint_fields(strategy),
        "holdout_last_status": "failed",
        "holdout_approved_snapshot": snapshot.model_dump(mode="json", by_alias=True),
    }
    state = DirectorLoopState.model_validate_json(
        json.dumps(state_payload, allow_nan=False)
    ).verify_consistency()

    lease = object.__new__(DirectorRunLease)
    lease.run_id = run_id
    captured: dict[str, object] = {}

    def append(**kwargs):
        captured.update(kwargs)
        return {"stored": True}

    lease._append_checkpoint = append
    with owned_execution(owner):
        malformed_payload = state.model_dump(mode="json", by_alias=True)
        malformed_payload.pop("strategy_state_sha256")
        with pytest.raises(ValueError, match="allowed run-end"):
            lease.append_holdout_closure_checkpoint(
                sequence=7,
                key="director-state:7:run_end",
                phase="director_loop_state",
                payload=malformed_payload,
            )
        result = lease.append_holdout_closure_checkpoint(
            sequence=7,
            key="director-state:7:run_end",
            phase="director_loop_state",
            payload=state.model_dump(mode="json", by_alias=True),
        )

    assert result == {"stored": True}
    assert captured["holdout_closure"] is True
    assert captured["payload"]["schema"] == "director-loop-state.v2"


def test_holdout_closure_checkpoint_allows_only_monotone_run_end_reconciliation() -> None:
    from lab.director.budget import BudgetSnapshot, EpisodeReservation
    from lab.director.journal import DirectorRunLease
    from lab.director.loop import _budget_to_json
    from lab.director.ownership import ExecutionOwner, owned_execution

    run_id = uuid4()
    reservation_id = uuid4()
    reservation = EpisodeReservation(reservation_id, 60, 0)
    owner = ExecutionOwner(run_id, 3, "2" * 32, "f" * 64)
    reservation_key = f"holdout-budget-reserved:{reservation_id}"
    reconciliation_key = f"holdout-budget-reconciled:{reservation_id}"
    prior_payload = {
        "run_id": str(run_id),
        "trigger_kind": "run_end",
        "trigger_index": 1,
        "candidate_experiment_id": "candidate",
        "admitted_generation": owner.generation,
        "execution_sha256": owner.execution_sha256,
        "reservation_id": str(reservation_id),
        "wall_seconds": 60,
        "model_tokens": 0,
        "budget": _budget_to_json(
            BudgetSnapshot(
                proposal_count=4,
                wall_seconds=20.0,
                model_tokens=11,
                reserved_wall_seconds=60.0,
                elapsed_wall_seconds=25.0,
                reservations=(reservation,),
            )
        ),
    }
    intent_payload = {
        "run_id": str(run_id),
        "admitted_generation": owner.generation,
        "execution_sha256": owner.execution_sha256,
    }
    payload = {
        "run_id": str(run_id),
        "trigger_kind": "run_end",
        "trigger_index": 1,
        "candidate_experiment_id": "candidate",
        "admitted_generation": owner.generation,
        "execution_sha256": owner.execution_sha256,
        "reservation_id": str(reservation_id),
        "reserved_wall_seconds": 60,
        "measured_wall_seconds": 60.0,
        "budget": _budget_to_json(
            BudgetSnapshot(
                proposal_count=4,
                wall_seconds=80.0,
                model_tokens=11,
                elapsed_wall_seconds=90.0,
            )
        ),
    }
    checkpoints = {
        reservation_key: {
            "payload": prior_payload,
            "receipt": {"phase": "holdout_budget_reserved"},
        },
        "director-holdout-run-end-intent": {
            "payload": intent_payload,
            "receipt": {"phase": "holdout_run_end_intent"},
        },
    }
    lease = object.__new__(DirectorRunLease)
    lease.run_id = run_id
    lease.read_checkpoint = lambda *, key, artifact_root: checkpoints.get(key)
    captured: dict[str, object] = {}
    lease._append_checkpoint = lambda **kwargs: captured.update(kwargs) or {"stored": True}

    with owned_execution(owner):
        invalid_payload = {
            **payload,
            "budget": _budget_to_json(
                BudgetSnapshot(proposal_count=4, wall_seconds=79.0, model_tokens=11)
            ),
        }
        with pytest.raises(ValueError, match="allowed run-end"):
            lease.append_holdout_closure_checkpoint(
                sequence=5,
                key=reconciliation_key,
                phase="holdout_budget_reconciled",
                payload=invalid_payload,
            )
        result = lease.append_holdout_closure_checkpoint(
            sequence=5,
            key=reconciliation_key,
            phase="holdout_budget_reconciled",
            payload=payload,
        )

    assert result == {"stored": True}
    assert captured["holdout_closure"] is True


def _minimal_loop_state(run_id, candidate_id):
    from lab.director.loop import DirectorLoopState

    return DirectorLoopState.model_validate(
        {
            "schema": "director-loop-state.v1",
            "run_id": run_id,
            "suite_manifest_sha256": "a" * 64,
            "suite_id": "synthetic.v1",
            "suite_version": 1,
            "calibration_sha256": "b" * 64,
            "harness_sha256": "c" * 64,
            "image_sha256": "d" * 64,
            "proposal_limit": 1,
            "completed_proposals": 0,
            "next_ordinal": 1,
            "champion_experiment_id": candidate_id,
            "champion_source_sha256": "e" * 64,
            "champion_tree_sha256": "f" * 40,
            "champion_source_blob_sha256": "1" * 64,
            "champion_seed0_by_task": {"task": 0.1},
            "champion_seed1_by_task": {"task": 0.1},
            "champion_suite_seed_scores": (0.1, 0.1, 0.1),
            "champion_noise_sd": 0.01,
            "best_suite": 0.1,
            "consecutive_non_keep": 0,
            "explore_proposals": 0,
            "explore_family": None,
            "consecutive_candidate_crashes": 0,
            "previous_move_type": None,
            "recent_feedback": (),
            "budget": {},
        },
        strict=True,
    ).verify_consistency()


def test_run_end_wrapper_recovers_running_reservation_and_rereads_bit(monkeypatch) -> None:
    from lab.director import holdout as holdout_module
    from lab.director.holdout import HoldoutReceipt
    from lab.scorer import holdout_supervisor

    run_id = uuid4()
    reservation_id = uuid4()
    candidate_id = "candidate-10"
    running = HoldoutReceipt(
        reservation_id=reservation_id,
        state="running",
        bit=None,
        run_id=run_id,
        candidate_experiment_id=candidate_id,
        trigger_kind="run_end",
        trigger_index=1,
        admitted_generation=1,
        execution_sha256="f" * 64,
    )
    passed = HoldoutReceipt(
        reservation_id=reservation_id,
        state="passed",
        bit=True,
        run_id=run_id,
        candidate_experiment_id=candidate_id,
        trigger_kind="run_end",
        trigger_index=1,
        admitted_generation=1,
        execution_sha256="f" * 64,
    )
    result = DirectorLoopResult(
        run_id=run_id,
        status="proposal_limit_reached",
        completed_proposals=10,
        next_ordinal=11,
        champion_experiment_id=candidate_id,
        best_suite=0.1,
        checkpoint_sha256="c" * 64,
    )
    loop = object.__new__(DirectorLoop)
    loop.run_id = run_id
    loop.director_engine = _Engine([])
    loop.lease = _Lease({})
    loop.artifact_root = Path("/private/test-artifacts")
    loop.restore_run_end_holdout = lambda _result: None
    loop.apply_run_end_holdout = lambda _result, receipt: result if receipt == passed else None
    reserve_calls: list[object] = []

    def return_existing(_engine, **kwargs):
        reserve_calls.append(kwargs["reservation_id"])
        return running

    monkeypatch.setattr(holdout_module, "read_run_end_admission_failure", lambda *_a, **_k: None)
    monkeypatch.setattr(holdout_module, "read_holdout_request", lambda *_a, **_k: running)
    monkeypatch.setattr(holdout_module, "reserve_holdout_check", return_existing)
    monkeypatch.setattr(holdout_module, "read_holdout_bit", lambda *_a, **_k: passed)
    monkeypatch.setattr(
        holdout_supervisor,
        "run_holdout_recovery_process",
        lambda received_id, **_kw: (
            type("RecoveryResult", (), {"state": "passed"})()
            if received_id == reservation_id
            else (_ for _ in ()).throw(AssertionError("wrong recovery reservation"))
        ),
    )
    monkeypatch.setattr(
        holdout_module,
        "check_at_run_end",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError("recovery must not create a new run-end query")
        ),
    )

    with owned_execution(ExecutionOwner(run_id, 1, "2" * 32, "f" * 64)):
        assert loop.check_run_end_holdout(result) is result
    assert reserve_calls == []
