"""Focused tests for loop-level durable Thompson intent handling."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

from lab.director.budget import BudgetSnapshot, RunBudget
from lab.director.contracts import CandidateProposal, ExperimentDecision, ExperimentDocument
from lab.director.fake_llm import AgentContext, FakeLLM, ProposalTurn
from lab.director.loop import (
    DirectorLoop,
    DirectorLoopState,
    HoldoutApprovedSnapshot,
    _budget_to_json,
    _strategy_checkpoint_fields,
    _strategy_seed_for_run,
)
from lab.director.runner import RegisteredProposal, register_proposal_before_execution
from lab.director.strategy import (
    initial_strategy_state,
    resolve_strategy_selection,
    select_strategy_move,
    strategy_state_json,
)
from lab.reporting import _holdout_report_status


class MemoryLease:
    def __init__(self) -> None:
        self.checkpoints: dict[str, dict[str, object]] = {}
        self.append_count = 0
        self.active_checks = 0

    def read_checkpoint(self, *, key: str, artifact_root: object) -> dict[str, object] | None:
        del artifact_root
        return self.checkpoints.get(key)

    def append_checkpoint(
        self,
        *,
        sequence: int,
        key: str,
        phase: str,
        payload: dict[str, object],
        artifact_root: object,
    ) -> dict[str, object]:
        del sequence, artifact_root
        if key in self.checkpoints:
            raise AssertionError(f"duplicate checkpoint append: {key}")
        self.append_count += 1
        receipt: dict[str, object] = {
            "key": key,
            "phase": phase,
            "payload_sha256": "a" * 64,
        }
        self.checkpoints[key] = {"payload": payload, "receipt": receipt}
        return receipt

    def require_run_active(self) -> None:
        self.active_checks += 1


class MemoryBudgetEvents:
    def __init__(self, events: list[dict[str, object]]) -> None:
        self.events = events

    def mappings(self) -> MemoryBudgetEvents:
        return self

    def all(self) -> list[dict[str, object]]:
        return self.events


class MemoryBudgetConnection:
    def __init__(self, events: list[dict[str, object]]) -> None:
        self.events = events

    def __enter__(self) -> MemoryBudgetConnection:
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def execute(self, *args: object) -> MemoryBudgetEvents:
        del args
        return MemoryBudgetEvents(self.events)


class MemoryBudgetEngine:
    def __init__(self, events: list[dict[str, object]]) -> None:
        self.events = events

    def connect(self) -> MemoryBudgetConnection:
        return MemoryBudgetConnection(self.events)


class MemoryReceiptConnection:
    def __init__(self, receipt: dict[str, object]) -> None:
        self.receipt = receipt

    def __enter__(self) -> MemoryReceiptConnection:
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def execute(self, *args: object) -> MemoryReceiptConnection:
        del args
        return self

    def scalar_one_or_none(self) -> dict[str, object]:
        return self.receipt


class MemoryReportEngine:
    def __init__(self, receipt: dict[str, object]) -> None:
        self.receipt = receipt

    def connect(self) -> MemoryReceiptConnection:
        return MemoryReceiptConnection(self.receipt)


class MemoryTerminalResult:
    def __init__(self, value: dict[str, object] | None) -> None:
        self.value = value

    def mappings(self) -> MemoryTerminalResult:
        return self

    def one_or_none(self) -> dict[str, object] | None:
        return self.value


class MemoryTerminalConnection:
    def __init__(self, value: dict[str, object] | None) -> None:
        self.value = value

    def __enter__(self) -> MemoryTerminalConnection:
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def execute(self, *args: object, **kwargs: object) -> MemoryTerminalResult:
        del args, kwargs
        return MemoryTerminalResult(self.value)


class MemoryTerminalEngine:
    def __init__(self, value: dict[str, object] | None) -> None:
        self.value = value

    def connect(self) -> MemoryTerminalConnection:
        return MemoryTerminalConnection(self.value)


def _state() -> DirectorLoopState:
    run_id = UUID("00112233-4455-6677-8899-aabbccddeeff")
    baseline_id = f"exp_{'0' * 32}"
    strategy = initial_strategy_state(_strategy_seed_for_run(run_id))
    return DirectorLoopState.model_validate(
        {
            **_strategy_checkpoint_fields(strategy),
            "run_id": run_id,
            "suite_manifest_sha256": "a" * 64,
            "suite_id": "synthetic.v1",
            "suite_version": 1,
            "calibration_sha256": "b" * 64,
            "harness_sha256": "c" * 64,
            "image_sha256": "d" * 64,
            "proposal_limit": 35,
            "completed_proposals": 0,
            "next_ordinal": 1,
            "champion_experiment_id": baseline_id,
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
            "discard_streak": 0,
            "previous_move_type": None,
            "recent_feedback": (),
            "budget": {},
            "holdout_approved_snapshot": {
                "experiment_id": baseline_id,
                "source_sha256": "e" * 64,
                "tree_sha256": "f" * 40,
                "source_blob_sha256": "1" * 64,
                "seed0_by_task": {"task": 0.1},
                "seed1_by_task": {"task": 0.1},
                "suite_seed_scores": (0.1, 0.1, 0.1),
                "noise_sd": 0.01,
                "best_suite": 0.1,
                "periodic_keep_watermark": 0,
            },
        },
        strict=True,
    ).verify_consistency()


def _loop(
    state: DirectorLoopState, lease: MemoryLease, *, provider_id: str = "fake-json"
) -> DirectorLoop:
    loop = cast(Any, object.__new__(DirectorLoop))
    loop.run_id = state.run_id
    loop.lease = lease
    loop.artifact_root = object()
    loop.provider = SimpleNamespace(provider_id=provider_id)
    loop._next_checkpoint_sequence = lambda: lease.append_count + 1
    return cast(DirectorLoop, loop)


@pytest.mark.parametrize("provider_id", ("fake-json", "local-qwen.v1"))
def test_intent_is_durable_before_provider_and_retry_does_not_resample(
    provider_id: str,
) -> None:
    state = _state()
    lease = MemoryLease()
    loop = _loop(state, lease, provider_id=provider_id)

    pending, receipt, selection, system = loop._prepare_strategy_intent(
        state, ordinal=1, is_explore=False
    )
    state_json = strategy_state_json(pending.load_strategy_state())
    assert selection.move_type == "hparam"
    assert selection.reason == "initial_coverage"
    assert system == "S1"
    assert receipt["phase"] == "director_loop_state"
    intent_checkpoint = lease.checkpoints["proposal-intent:1"]
    intent_payload = cast(dict[str, object], intent_checkpoint["payload"])
    assert intent_payload["move_type"] == "hparam"
    assert lease.active_checks == 2

    repeated, _, retry_selection, retry_system = loop._prepare_strategy_intent(
        pending, ordinal=1, is_explore=False
    )
    assert strategy_state_json(repeated.load_strategy_state()) == state_json
    assert retry_selection == selection
    assert retry_system == system
    assert lease.append_count == 2


def test_first_nine_move_routing_and_fifth_discard_escalation() -> None:
    state = _state()
    lease = MemoryLease()
    loop = _loop(state, lease)
    expected_moves = (
        "hparam",
        "preprocess",
        "features",
        "regime",
        "detector",
        "fusion",
        "alarm_policy",
        "simplify",
        "skill_reuse",
    )
    expected_systems = ("S1", "S1", "S2", "S2", "S2", "S2", "S1", "S1", "S1")
    current = state
    for ordinal, (move, routed) in enumerate(
        zip(expected_moves, expected_systems, strict=True), start=1
    ):
        current, _, selection, system = loop._prepare_strategy_intent(
            current, ordinal=ordinal, is_explore=False
        )
        assert selection.move_type == move
        assert system == routed
        completed = resolve_strategy_selection(current.load_strategy_state(), ordinal, "KEEP")
        current = loop._with_strategy_state(
            current,
            completed,
            completed_proposals=ordinal,
            next_ordinal=ordinal + 1,
            discard_streak=0,
        )
    current = current.model_copy(update={"discard_streak": 5}).verify_consistency()
    forced_state, _, forced_selection, forced_system = loop._prepare_strategy_intent(
        current, ordinal=10, is_explore=False
    )
    assert forced_selection.ordinal == 10
    assert forced_system == "S2"
    assert forced_state.load_strategy_state().pending == forced_selection


def test_holdout_rollback_preserves_strategy_checkpoint_and_pending_intent() -> None:
    state = _state()
    strategy = state.load_strategy_state()

    pending, _ = select_strategy_move(strategy, 1)
    state = DirectorLoop._with_strategy_state(object.__new__(DirectorLoop), state, pending)
    snapshot = HoldoutApprovedSnapshot.from_state(state, keep_count=0)
    restored = snapshot.restore(state, keep_count=0)
    assert restored.strategy_state_json == state.strategy_state_json
    assert restored.strategy_state_sha256 == state.strategy_state_sha256
    assert restored.load_strategy_state().pending == pending.pending


def test_run_seed_is_stable_and_scoped_to_run_identity() -> None:
    run_id = UUID("00112233-4455-6677-8899-aabbccddeeff")
    other_id = UUID("10112233-4455-6677-8899-aabbccddeeff")
    assert _strategy_seed_for_run(run_id) == _strategy_seed_for_run(run_id)
    assert _strategy_seed_for_run(run_id) != _strategy_seed_for_run(other_id)


def test_terminal_receipt_recovery_applies_loop_verdict_once() -> None:
    state = _state()
    lease = MemoryLease()
    loop = _loop(state, lease)
    loop.budget = RunBudget(
        proposal_limit=35, wall_limit=100, token_limit=1000, monotonic=lambda: 0.0
    )
    pending, _, selection, system = loop._prepare_strategy_intent(
        state, ordinal=1, is_explore=False
    )
    proposal_contract = CandidateProposal(
        hypothesis="bounded test candidate",
        move_type=selection.move_type,
        candidate_source="class Candidate: pass\n",
        predicted_delta=0.0,
    )
    candidate_digest = hashlib.sha256(proposal_contract.candidate_source.encode()).hexdigest()
    proposal = RegisteredProposal(
        experiment_id=f"exp_{'2' * 32}",
        experiment_number=1,
        proposal=proposal_contract,
        candidate_sha256=candidate_digest,
        candidate_blob_sha256=candidate_digest,
        inputs_sha256="3" * 64,
        messages_blob_sha256="4" * 64,
        calibration_sha256=state.calibration_sha256,
        parent_experiment_id=state.champion_experiment_id,
        parent_tree_sha256=state.champion_tree_sha256,
        harness_sha256=state.harness_sha256,
        image_sha256=state.image_sha256,
        suite_id=state.suite_id,
        suite_version=state.suite_version,
        system=system,
        input_tokens=0,
        output_tokens=0,
    )
    terminal_receipt = ExperimentDocument(
        schema="experiment.v1",
        experiment_id=proposal.experiment_id,
        run_id=state.run_id,
        ordinal=1,
        kind="proposal",
        experiment_number=1,
        baseline_name=None,
        calibration_sha256=state.calibration_sha256,
        agent_version="test",
        parent_experiment_id=state.champion_experiment_id,
        candidate_sha256=proposal.candidate_sha256,
        candidate_blob_sha256=proposal.candidate_blob_sha256,
        move_type=selection.move_type,
        system=system,
        hypothesis=proposal_contract.hypothesis,
        predicted_delta=0.0,
        inputs_sha256=proposal.inputs_sha256,
        parent_tree=state.champion_tree_sha256,
        child_tree=None,
        harness_sha256=state.harness_sha256,
        image_sha256=state.image_sha256,
        suite_id=state.suite_id,
        suite_version=state.suite_version,
        per_task=(),
        suite_score=None,
        guards={},
        decision=ExperimentDecision(
            verdict="DISCARD", delta=0.0, ci_low=0.0, noise_sd=0.0, reason="no_improvement"
        ),
        status="rejected",
        fit_seconds=None,
        score_seconds=None,
        llm_input_tokens=0,
        llm_output_tokens=0,
        wall_seconds=0.0,
    )

    uninterrupted = loop._advance_state(pending, proposal, terminal_receipt, cast(Any, None), False)
    lease.checkpoints["proposal:1"] = {
        "payload": {
            "run_id": str(state.run_id),
            "experiment_number": 1,
            "experiment_id": proposal.experiment_id,
            "candidate_sha256": candidate_digest,
            "candidate_blob_sha256": candidate_digest,
            "proposal": proposal_contract.model_dump(mode="json"),
            "inputs_sha256": proposal.inputs_sha256,
            "messages_blob_sha256": proposal.messages_blob_sha256,
            "calibration_sha256": proposal.calibration_sha256,
            "parent_experiment_id": proposal.parent_experiment_id,
            "parent_tree_sha256": proposal.parent_tree_sha256,
            "harness_sha256": proposal.harness_sha256,
            "image_sha256": proposal.image_sha256,
            "suite_id": proposal.suite_id,
            "suite_version": proposal.suite_version,
            "system": proposal.system,
            "input_tokens": proposal.input_tokens,
            "output_tokens": proposal.output_tokens,
            "sequence": 1,
        }
    }
    cast(Any, loop).director_engine = MemoryTerminalEngine(
        {"experiment_id": proposal.experiment_id, "status": "rejected"}
    )
    cast(Any, loop)._read_terminal_document = lambda _experiment_id: terminal_receipt
    # Simulate process loss after the terminal record exists but before state application.
    reloaded = DirectorLoopState.model_validate_json(
        json.dumps(lease.checkpoints["director-state:0:strategy-intent-1"]["payload"]),
        strict=True,
    ).verify_consistency()
    recovered_result = loop._recover_committed_terminal(
        reloaded, {"payload_sha256": "a" * 64}, cast(Any, None)
    )
    assert recovered_result is not None
    recovered, recovered_receipt, promoted = recovered_result
    assert not promoted
    recovered_payload = recovered.model_dump(mode="json", by_alias=True)
    uninterrupted_payload = uninterrupted.model_dump(mode="json", by_alias=True)
    recovered_payload.pop("budget")
    uninterrupted_payload.pop("budget")
    assert recovered_payload == uninterrupted_payload
    assert recovered.completed_proposals == 1
    assert recovered.load_strategy_state().completed_experiments == 1
    assert recovered.load_strategy_state().pending is None
    assert recovered.load_strategy_state().posterior[selection.move_type].beta > 1.0

    # A second process reloads the applied checkpoint and must not apply the verdict again.
    applied_checkpoint = lease.checkpoints[str(recovered_receipt["key"])]
    applied_state = DirectorLoopState.model_validate_json(
        json.dumps(applied_checkpoint["payload"]), strict=True
    ).verify_consistency()
    cast(Any, loop).director_engine = MemoryTerminalEngine(None)
    append_count = lease.append_count
    applied_payload = applied_state.model_dump(mode="json", by_alias=True)
    budget_before_replay = loop.budget.snapshot()
    assert (
        loop._recover_committed_terminal(applied_state, recovered_receipt, cast(Any, None)) is None
    )
    assert lease.append_count == append_count
    assert applied_state.model_dump(mode="json", by_alias=True) == applied_payload
    assert loop.budget.snapshot() == budget_before_replay

    with pytest.raises(RuntimeError, match="terminal proposal ordinal differs"):
        loop._advance_state(recovered, proposal, terminal_receipt, cast(Any, None), False)


def test_provider_failure_checkpoint_keeps_pending_strategy_and_spent_budget() -> None:
    state = _state()
    lease = MemoryLease()
    loop = _loop(state, lease)
    loop.budget = RunBudget(
        proposal_limit=35,
        wall_limit=100,
        token_limit=1000,
        snapshot=BudgetSnapshot(wall_seconds=7.0, model_tokens=321),
    )
    pending, _, selection, _ = loop._prepare_strategy_intent(state, ordinal=1, is_explore=False)
    before = strategy_state_json(pending.load_strategy_state())
    failed, _ = loop._preserve_pending_strategy_after_failure(pending, ordinal=1)
    persisted = lease.checkpoints["director-state:0:provider-failure-1"]
    reloaded = DirectorLoopState.model_validate_json(
        json.dumps(persisted["payload"]), strict=True
    ).verify_consistency()
    assert strategy_state_json(failed.load_strategy_state()) == before
    assert strategy_state_json(reloaded.load_strategy_state()) == before
    assert reloaded.next_ordinal == 1
    assert reloaded.budget["wall_seconds"] == 7.0
    assert reloaded.budget["model_tokens"] == 321
    restored_strategy = reloaded.load_strategy_state()
    assert restored_strategy.pending is not None
    assert restored_strategy.pending.move_type == selection.move_type
    first_append_count = lease.append_count
    loop.budget = RunBudget(
        proposal_limit=35,
        wall_limit=100,
        token_limit=1000,
        snapshot=BudgetSnapshot(wall_seconds=11.0, model_tokens=500),
    )
    replayed, _ = loop._preserve_pending_strategy_after_failure(failed, ordinal=1)
    assert replayed == failed
    assert lease.append_count == first_append_count


@pytest.mark.parametrize(
    ("events", "expected_wall", "expected_tokens"),
    [
        pytest.param([{"key": "older-budget", "sequence": 9}], 5.0, 10, id="older-only"),
        pytest.param([{"key": "older-budget", "sequence": 10}], 5.0, 10, id="equal-sequence"),
        pytest.param(
            [
                {"key": "newer-budget", "sequence": 12},
                {"key": "older-budget", "sequence": 9},
            ],
            8.0,
            40,
            id="newer-supersedes",
        ),
    ],
)
def test_budget_restore_ignores_receipts_older_than_failure_loop_state(
    events: list[dict[str, object]], expected_wall: float, expected_tokens: int
) -> None:
    state = _state()
    lease = MemoryLease()
    loop = _loop(state, lease)
    base = RunBudget(
        proposal_limit=35,
        wall_limit=100,
        token_limit=1000,
        snapshot=BudgetSnapshot(wall_seconds=5.0, model_tokens=10),
    )
    older_budget = RunBudget(
        proposal_limit=35,
        wall_limit=100,
        token_limit=1000,
        snapshot=BudgetSnapshot(wall_seconds=2.0, model_tokens=5),
    )
    newer_budget = RunBudget(
        proposal_limit=35,
        wall_limit=100,
        token_limit=1000,
        snapshot=BudgetSnapshot(wall_seconds=8.0, model_tokens=40),
    )
    lease.checkpoints["older-budget"] = {
        "payload": {"budget": _budget_to_json(older_budget.snapshot())},
        "receipt": {"phase": "proposal_budget_reconciled"},
    }
    lease.checkpoints["newer-budget"] = {
        "payload": {"budget": _budget_to_json(newer_budget.snapshot())},
        "receipt": {"phase": "proposal_budget_reconciled"},
    }
    cast(Any, loop).director_engine = MemoryBudgetEngine(events)
    # This test isolates durable receipt ordering; elapsed SQL anchoring is covered
    # by the real-role holdout closure chain and the budget elapsed-floor test.
    cast(Any, loop)._observe_execution_elapsed = lambda: None
    loop.budget = base
    loop._restore_budget(_budget_to_json(base.snapshot()), after_sequence=10)
    restored = loop.budget.snapshot()
    assert restored.wall_seconds == expected_wall
    assert restored.model_tokens == expected_tokens


def test_runner_rejects_provider_move_change_before_any_proposal_checkpoint() -> None:
    run_id = UUID("00112233-4455-6677-8899-aabbccddeeff")
    provider_turn = ProposalTurn(
        proposal=CandidateProposal(
            hypothesis="changed move",
            move_type="features",
            candidate_source="class Candidate: pass\n",
            predicted_delta=0.0,
        ),
        messages=("proposal",),
        input_tokens=1,
        output_tokens=1,
    )
    provider = FakeLLM((provider_turn,))
    lease = MemoryLease()
    context = AgentContext(
        phase="proposal",
        experiment_number=1,
        system="S1",
        move_type="hparam",
        task_cards=("metadata only",),
        champion_source="class Champion: pass\n",
        recent_feedback=(),
    )
    with pytest.raises(ValueError, match="changed the preselected move intent"):
        register_proposal_before_execution(
            cast(Any, None),
            run_id=run_id,
            ordinal=1,
            parent_experiment_id=f"exp_{'0' * 32}",
            parent_tree_sha256="f" * 40,
            suite_id="synthetic.v1",
            suite_version=1,
            calibration_sha256="b" * 64,
            harness_sha256="c" * 64,
            image_sha256="d" * 64,
            system="S1",
            context=context,
            provider=provider,
            lease=cast(Any, lease),
            artifact_root=cast(Any, None),
        )
    assert "proposal:1" not in lease.checkpoints


@pytest.mark.parametrize("schema_version", ("director-loop-state.v1", "director-loop-state.v2"))
def test_report_reads_historical_and_thompson_loop_state(
    schema_version: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_payload = _state().model_dump(mode="json", by_alias=True)
    if schema_version == "director-loop-state.v1":
        state_payload["schema"] = schema_version
        for key in (
            "strategy_seed",
            "strategy_policy_version",
            "strategy_state_json",
            "strategy_state_sha256",
        ):
            state_payload.pop(key)
    payload = json.dumps(
        state_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    digest = hashlib.sha256(payload).hexdigest()
    engine = MemoryReportEngine({"phase": "director_loop_state", "payload_sha256": digest})
    monkeypatch.setattr("lab.reporting.read_director_artifact", lambda *_a, **_k: payload)
    status = _holdout_report_status(
        cast(Any, engine), UUID(state_payload["run_id"]), artifact_root=Path("/private/test")
    )
    assert status is not None
    assert status["development_champion_id"] == state_payload["champion_experiment_id"]
    assert status["approved_champion_id"] == state_payload["champion_experiment_id"]
