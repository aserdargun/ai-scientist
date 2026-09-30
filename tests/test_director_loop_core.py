"""Focused tests for proposal-only generation, budget reservations, and receipts."""

from __future__ import annotations

import pytest

from lab.director.budget import RunBudget
from lab.director.contracts import CandidateProposal
from lab.director.fake_llm import AgentContext, FakeLLM, ProposalTurn, prompt_context_sha256
from lab.director.journal import canonical_bytes


def _turn(hypothesis: str) -> ProposalTurn:
    return ProposalTurn(
        proposal=CandidateProposal(
            hypothesis=hypothesis,
            move_type="features",
            candidate_source=(
                "import numpy as np\n"
                "class Candidate:\n"
                " def fit(self, train, ctx): self.center=np.median(train.to_numpy(),axis=0)\n"
                " def score(self, data): return np.mean("
                "np.abs(data.to_numpy()-self.center),axis=1)\n"
                " def alarm_policy(self, train_scores):\n"
                "  from harness.contracts import AlarmPolicy\n"
                "  return AlarmPolicy(float(np.quantile(train_scores,.99)),"
                "float(np.quantile(train_scores,.98)),1)\n"
            ),
            predicted_delta=0.1,
        ),
        messages=(hypothesis,),
        input_tokens=100,
        output_tokens=30,
    )


def _context(ordinal: int) -> AgentContext:
    return AgentContext(
        phase="LOOP",
        experiment_number=ordinal,
        task_cards=("EVT task: learn robust deviations; labels withheld",),
        champion_source="class Champion: pass",
        recent_feedback=(),
    )


def test_fake_provider_resumes_by_durable_ordinal() -> None:
    provider = FakeLLM((_turn("proposal one"), _turn("proposal two")))
    assert provider.propose(_context(2)).proposal.hypothesis == "proposal two"
    resumed = FakeLLM((_turn("proposal one"), _turn("proposal two")))
    assert resumed.propose(_context(2)).proposal.hypothesis == "proposal two"
    with pytest.raises(RuntimeError, match="sequence_exhausted"):
        provider.propose(_context(3))


def test_fake_provider_rejects_measurement_or_verdict_fields() -> None:
    raw = {
        "proposal": _turn("only a proposal").proposal.model_dump(mode="json"),
        "messages": ["source idea"],
        "input_tokens": 10,
        "output_tokens": 10,
        "score": 0.99,
        "verdict": "KEEP",
    }
    with pytest.raises(ValueError):
        ProposalTurn.model_validate(raw, strict=True)


def test_context_hash_is_canonical_and_metadata_bound() -> None:
    first = _context(1)
    same = AgentContext.model_validate(first.model_dump(), strict=True)
    other = _context(2)
    assert prompt_context_sha256(first) == prompt_context_sha256(same)
    assert prompt_context_sha256(first) != prompt_context_sha256(other)
    assert canonical_bytes({"b": 2, "a": 1}) == b'{"a":1,"b":2}'


def test_budget_reserves_before_proposal_and_releases_unused_capacity() -> None:
    now = [1.0]
    budget = RunBudget(
        proposal_limit=35,
        wall_limit=900,
        token_limit=20_000,
        monotonic=lambda: now[0],
    )
    reservation = budget.reserve_proposal(wall_seconds=180, model_tokens=5_000)
    assert budget.remaining_wall_seconds == 720
    assert budget.remaining_model_tokens == 15_000
    now[0] += 20.0
    budget.reconcile_proposal(
        reservation, measured_wall_seconds=20.0, measured_model_tokens=250
    )
    assert budget.remaining_wall_seconds == 880
    assert budget.remaining_model_tokens == 19_750
    with pytest.raises(ValueError, match="already reconciled"):
        budget.reconcile_proposal(
            reservation, measured_wall_seconds=1, measured_model_tokens=1
        )


def test_budget_rejects_proposal_without_confirmation_headroom() -> None:
    budget = RunBudget(proposal_limit=1, wall_limit=100, token_limit=100)
    with pytest.raises(RuntimeError, match="run_wall_budget_exhausted"):
        budget.reserve_proposal(wall_seconds=101, model_tokens=10)
