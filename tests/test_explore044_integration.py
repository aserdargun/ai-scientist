"""Inert EXPLORE glue tests: memory ledger and mocked admission, never runtime workers."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from lab.director.baselines import baseline_candidate_source
from lab.director.budget import BudgetSnapshot, RunBudget
from lab.director.contracts import CandidateProposal, ExperimentDecision, ExperimentDocument
from lab.director.explore import family_directive
from lab.director.fake_llm import AgentContext, FakeLLM, ProposalTurn, prompt_context_sha256
from lab.director.journal import canonical_bytes
from lab.director.local_llm import LocalQwenProposalProvider
from lab.director.loop import DirectorLoopState
from lab.director.parameter_grid import ParameterGridProvider, grid_document
from lab.director.runner import (
    ExploreFamilyMismatch,
    RegisteredProposal,
    register_proposal_before_execution,
)
from lab.director.strategy import (
    resolve_strategy_selection,
    select_strategy_move,
    strategy_state_json,
)
from lab.operating_modes import ModeConfig
from lab.scorer.explore_report import terminal_explore_provenance
from tests.test_director_loop_strategy import MemoryLease, _loop, _state


def _ready(tmp_path: Path):
    state = _state()
    lease = MemoryLease()
    loop = _loop(state, lease)
    loop.artifact_root = tmp_path
    loop.budget = RunBudget(
        proposal_limit=35, wall_limit=1000, token_limit=10000, monotonic=lambda: 0.0
    )
    for ordinal in range(1, 26):
        pending, _ = select_strategy_move(state.load_strategy_state(), ordinal)
        resolved = resolve_strategy_selection(pending, ordinal, "DISCARD")
        state = loop._with_strategy_state(
            state,
            resolved,
            completed_proposals=ordinal,
            next_ordinal=ordinal + 1,
            consecutive_non_keep=ordinal,
        )
    cast(Any, loop)._champion_source = lambda _: baseline_candidate_source("robust_z")
    return loop, lease, state


def _context(state, move="hparam"):
    intent = state.explore_episode.intent
    return AgentContext(
        phase="explore",
        experiment_number=state.next_ordinal,
        system="S2",
        move_type=move,
        task_cards=("metadata only",),
        champion_source=baseline_candidate_source("robust_z").decode(),
        recent_feedback=(),
        explore_intent=intent,
        explore_directive=family_directive(intent),
    )


def _abandon(loop, lease, state):
    pending, _, selection, system = loop._prepare_strategy_intent(
        state, ordinal=state.next_ordinal, is_explore=True
    )
    assert system == "S2"
    ordinal = state.next_ordinal
    lease.append_checkpoint(
        sequence=lease.append_count + 1,
        key=f"proposal-abandoned:{ordinal}",
        phase="proposal_abandoned",
        payload={
            "run_id": str(state.run_id),
            "ordinal": ordinal,
            "reason": "explore_family_mismatch",
        },
        artifact_root=loop.artifact_root,
    )
    updated = loop._advance_tool_parse_abandonment(
        pending,
        ordinal=ordinal,
        move_type=selection.move_type,
        is_explore=True,
        reason="explore_family_mismatch",
    )
    # A crash after the terminal commit but before the state write repeats exact bytes.
    restored = DirectorLoopState.model_validate_json(pending.model_dump_json(by_alias=True))
    replayed = loop._advance_tool_parse_abandonment(
        restored,
        ordinal=ordinal,
        move_type=selection.move_type,
        is_explore=True,
        reason="explore_family_mismatch",
    )
    assert updated.model_dump_json(by_alias=True) == replayed.model_dump_json(by_alias=True)
    return updated


def test_family_intent_precedes_thompson_and_survives_provider_failure(tmp_path):
    loop, lease, state = _ready(tmp_path)
    before = strategy_state_json(state.load_strategy_state())
    state, receipt = loop._prepare_explore_episode(state, {})
    assert list(lease.checkpoints) == ["director-state:25:explore-intent-26"]
    assert strategy_state_json(state.load_strategy_state()) == before
    pending, _, selection, system = loop._prepare_strategy_intent(
        state, ordinal=26, is_explore=True
    )
    assert system == "S2"
    assert (
        lease.checkpoints["proposal-intent:26"]["payload"]["explore_episode_id"]
        == state.explore_episode.intent.episode_id
    )
    loop.budget = RunBudget(
        proposal_limit=35,
        wall_limit=1000,
        token_limit=10000,
        snapshot=BudgetSnapshot(wall_seconds=7.0, model_tokens=123),
        monotonic=lambda: 0.0,
    )
    failed, failure_receipt = loop._preserve_pending_strategy_after_failure(pending, ordinal=26)
    count = lease.append_count
    same, same_receipt = loop._prepare_explore_episode(failed, failure_receipt)
    assert same_receipt == failure_receipt and receipt != failure_receipt
    assert same.explore_episode == state.explore_episode
    assert same.explore_episode.resolutions == ()
    assert same.load_strategy_state().pending == selection
    assert same.budget["wall_seconds"] == 7.0 and same.budget["model_tokens"] == 123
    assert lease.append_count == count


def test_ten_terminal_results_bind_exhaustion_without_extra_checkpoint(tmp_path):
    loop, lease, state = _ready(tmp_path)
    state, receipt = loop._prepare_explore_episode(state, {})
    for ordinal in range(26, 36):
        assert loop._is_explore(state)
        state = _abandon(loop, lease, state)
        receipt = loop._persist_state(state)
        assert state.explore_proposals == ordinal - 25
        assert state.termination_reason == ("explore_exhausted" if ordinal == 35 else None)
    count = lease.append_count
    result = loop._result(state, receipt, "proposal_limit_reached")
    assert result.termination_reason == "explore_exhausted"
    assert state.loop_phase == "HOLDOUT_CHECK" and result.completed_proposals == 35
    assert loop._persist_state(state) == receipt and lease.append_count == count
    assert not any(key.startswith("explore-exhausted:") for key in lease.checkpoints)
    changed = state.model_copy(update={"termination_reason": None})
    with pytest.raises(ValueError, match="exhaustion"):
        changed.verify_consistency()


def test_context_hash_and_prompt_bind_exact_s2_directive(tmp_path):
    loop, _, state = _ready(tmp_path)
    state, _ = loop._prepare_explore_episode(state, {})
    context = _context(state)
    variants = LocalQwenProposalProvider._message_variants(
        context, cast(Any, SimpleNamespace(max_context_tokens=16384))
    )
    for messages in variants:
        assert context.explore_intent.episode_id in messages[1]["content"]
        assert context.explore_directive in messages[1]["content"]
    ordinary = AgentContext(
        phase="proposal",
        experiment_number=26,
        system="S2",
        move_type="hparam",
        task_cards=context.task_cards,
        champion_source=context.champion_source,
        recent_feedback=(),
    )
    assert "explore_intent" not in ordinary.model_dump(mode="json")
    assert prompt_context_sha256(ordinary) != prompt_context_sha256(context)
    with pytest.raises(ValueError, match="S2 family intent"):
        AgentContext.model_validate_json(
            context.model_copy(update={"system": "S1"}).model_dump_json()
        )


class _ReadOnlySequence:
    def connect(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def execute(self, statement, *_):
        assert str(statement).lstrip().startswith("SELECT max(")
        return self

    def scalar_one(self):
        return None


@pytest.mark.parametrize(
    "source", [baseline_candidate_source("robust_z"), b"def build_candidate(): return object()"]
)
def test_wrong_family_and_restored_proposal_never_register(tmp_path, monkeypatch, source):
    import lab.director.runner as runner_module

    loop, lease, state = _ready(tmp_path)
    state, _ = loop._prepare_explore_episode(state, {})
    context = _context(state)
    turn = ProposalTurn(
        proposal=CandidateProposal(
            hypothesis="wrong target",
            move_type="hparam",
            candidate_source=source.decode(),
            predicted_delta=0.0,
        ),
        messages=("fixture",),
        input_tokens=7,
        output_tokens=11,
    )
    provider = FakeLLM((turn,) * 26)
    calls = []
    original_propose = provider.propose

    def counted(context):
        calls.append(context)
        return original_propose(context)

    monkeypatch.setattr(provider, "propose", counted)
    blobs = {}

    def store(payload, **_):
        digest = hashlib.sha256(payload).hexdigest()
        blobs[digest] = payload
        return digest

    monkeypatch.setattr(runner_module, "store_director_artifact", store)
    monkeypatch.setattr(runner_module, "read_director_artifact", lambda digest, **_: blobs[digest])

    def forbidden(*_, **__):
        pytest.fail("family mismatch reached experiment admission")

    monkeypatch.setattr(runner_module, "register_experiment", forbidden)
    args = dict(
        run_id=state.run_id,
        ordinal=26,
        parent_experiment_id=state.champion_experiment_id,
        parent_tree_sha256=state.champion_tree_sha256,
        suite_id=state.suite_id,
        suite_version=state.suite_version,
        calibration_sha256=state.calibration_sha256,
        harness_sha256=state.harness_sha256,
        image_sha256=state.image_sha256,
        system="S2",
        context=context,
        provider=provider,
        lease=lease,
        artifact_root=tmp_path,
    )
    for _ in range(2):
        with pytest.raises(ExploreFamilyMismatch):
            register_proposal_before_execution(cast(Any, _ReadOnlySequence()), **args)
    assert "proposal:26" in lease.checkpoints
    assert len(calls) == 1


def test_thirty_item_trusted_grid_preserves_order_without_explore(tmp_path):
    loop, _, state = _ready(tmp_path)
    configs = [ModeConfig(method="lsh", seed=i).model_dump(mode="json") for i in range(30)]
    document = grid_document("a" * 64, configs)
    provider = ParameterGridProvider(
        document,
        configuration_sha256=hashlib.sha256(canonical_bytes(document)).hexdigest(),
        registry_entry_sha256="b" * 64,
    )
    loop.provider = provider
    assert not loop._is_explore(state)
    for ordinal in range(1, 31):
        context = AgentContext(
            phase="proposal",
            experiment_number=ordinal,
            system="S1",
            move_type="hparam",
            task_cards=("metadata",),
            champion_source="source",
            recent_feedback=(),
        )
        turn = provider.propose(context)
        assert turn.proposal.candidate_source == provider.sources[ordinal - 1]
        assert turn.input_tokens == turn.output_tokens == 0
    loop.provider = cast(Any, SimpleNamespace(provider_id=provider.provider_id))
    assert loop._is_explore(state)  # An untrusted label cannot select the exemption.


def test_unknown_champion_is_typed_and_creates_no_intent(tmp_path):
    loop, lease, state = _ready(tmp_path)
    cast(Any, loop)._champion_source = lambda _: b"unknown_factory()"
    with pytest.raises(ValueError, match="explore_family_unverified"):
        loop._prepare_explore_episode(state, {})
    assert not lease.checkpoints


@pytest.mark.parametrize("verdict", ["KEEP", "KEEP_SIMPLER"])
def test_measured_keep_clears_episode_and_rollback_restores_source_family(
    tmp_path, monkeypatch, verdict
):
    import lab.director.artifacts as artifact_module
    import lab.director.loop as loop_module

    loop, _, state = _ready(tmp_path)
    state, _ = loop._prepare_explore_episode(state, {})
    pending, _, selection, system = loop._prepare_strategy_intent(
        state, ordinal=26, is_explore=True
    )
    source = baseline_candidate_source("iforest")
    digest = hashlib.sha256(source).hexdigest()
    proposal = RegisteredProposal(
        experiment_id=f"exp_{'2' * 32}",
        experiment_number=26,
        proposal=CandidateProposal(
            hypothesis="target",
            move_type=selection.move_type,
            candidate_source=source.decode(),
            predicted_delta=0.0,
        ),
        candidate_sha256=digest,
        candidate_blob_sha256=digest,
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
    doc = ExperimentDocument(
        schema="experiment.v1",
        experiment_id=proposal.experiment_id,
        run_id=state.run_id,
        ordinal=26,
        kind="proposal",
        experiment_number=26,
        baseline_name=None,
        calibration_sha256=state.calibration_sha256,
        agent_version="mocked-measurements",
        parent_experiment_id=state.champion_experiment_id,
        candidate_sha256=digest,
        candidate_blob_sha256=digest,
        move_type=selection.move_type,
        system=system,
        hypothesis="target",
        predicted_delta=0.0,
        inputs_sha256="3" * 64,
        parent_tree=state.champion_tree_sha256,
        child_tree="5" * 40,
        harness_sha256=state.harness_sha256,
        image_sha256=state.image_sha256,
        suite_id=state.suite_id,
        suite_version=state.suite_version,
        per_task=(),
        suite_score=0.2,
        guards={},
        decision=ExperimentDecision(
            verdict=verdict, delta=0.1, ci_low=0.1, noise_sd=0.0, reason="fixture"
        ),
        status="scored",
        fit_seconds=0.0,
        score_seconds=0.0,
        llm_input_tokens=0,
        llm_output_tokens=0,
        wall_seconds=0.0,
    )
    loop.tasks = ()
    cast(Any, loop)._seed_measurements = lambda *_: SimpleNamespace(
        measurements=({"task_id": "task", "task_score": 0.2},)
    )
    monkeypatch.setattr(loop_module, "_weighted_suite_seed_scores", lambda *_, **__: 0.2)
    monkeypatch.setattr(loop_module, "_candidate_git_tree", lambda _: "5" * 40)
    monkeypatch.setattr(
        artifact_module,
        "store_director_artifact",
        lambda payload, **_: hashlib.sha256(payload).hexdigest(),
    )
    updated = loop._advance_state(pending, proposal, doc, cast(Any, None), True)
    assert updated.explore_episode is None and updated.explore_proposals == 0
    assert updated.consecutive_non_keep == 0 and updated.termination_reason is None
    assert updated.champion_source_sha256 == digest
    restored = state.holdout_approved_snapshot.restore(updated, keep_count=1)
    assert restored.champion_source_sha256 == state.champion_source_sha256
    assert restored.explore_episode is None


def test_report_reason_follows_sealed_marker_and_exact_terminal_state(tmp_path, monkeypatch):
    import lab.scorer.explore_report as report_module

    loop, lease, state = _ready(tmp_path)
    state, _ = loop._prepare_explore_episode(state, {})
    for _ in range(10):
        state = _abandon(loop, lease, state)
    blobs = {}

    def store(value):
        payload = canonical_bytes(value)
        digest = hashlib.sha256(payload).hexdigest()
        blobs[digest] = payload
        return digest

    state_sha = store(state.model_dump(mode="json", by_alias=True))
    marker_sha = store(
        {
            "schema": "director-holdout-state-application.v1",
            "run_id": str(state.run_id),
            "state_sha256": state_sha,
            "application_sha256": "a" * 64,
        }
    )
    seal = {
        "generation": 1,
        "execution_sha256": "b" * 64,
        "source_kind": "holdout",
        "source_sha256": marker_sha,
    }

    def execute(statement, parameters):
        if "verify_terminal_finalization" in str(statement):
            if parameters["generation"] != 1:
                raise ValueError("terminal EXPLORE seal belongs to another execution")
            return SimpleNamespace(scalar_one=lambda: {"run_id": str(state.run_id), **seal})
        return SimpleNamespace(mappings=lambda: SimpleNamespace(one_or_none=lambda: seal))

    connection = SimpleNamespace(execute=execute)

    def read(digest, **_):
        payload = blobs[digest]
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError("artifact hash mismatch")
        return payload

    monkeypatch.setattr(report_module, "read_artifact_bytes", read)
    args = dict(
        run_id=state.run_id, generation=1, execution_sha256="b" * 64, artifact_root=tmp_path
    )
    first = terminal_explore_provenance(cast(Any, connection), **args)
    assert first == terminal_explore_provenance(cast(Any, connection), **args)
    assert first["termination_reason"] == "explore_exhausted"
    assert first["termination_provenance"]["terminal_state_sha256"] == state_sha
    with pytest.raises(ValueError, match="another execution"):
        terminal_explore_provenance(cast(Any, connection), **{**args, "generation": 2})
    blobs[state_sha] = b"{}"
    with pytest.raises(ValueError, match="hash mismatch"):
        terminal_explore_provenance(cast(Any, connection), **args)
