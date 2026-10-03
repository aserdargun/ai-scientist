"""A completed seed must survive restart without new dispatch or budget charges."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from lab.director import evaluation_recovery
from lab.director import runner as module
from lab.director.budget import RunBudget
from lab.director.contracts import CandidateProposal
from lab.director.executor import CandidateExecutionRejected


class MemoryCheckpoints:
    """Model immutable checkpoint semantics; this is not a real SQL restart."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}

    def heartbeat(self) -> None:
        pass

    def require_run_active(self) -> None:
        """This resume fixture represents an active owned run throughout."""

    def read_checkpoint(self, *, key: str, **_: Any) -> dict[str, Any] | None:
        return copy.deepcopy(self.records.get(key))

    def append(self, *_: Any, key: str, phase: str, payload: dict[str, Any], **__: Any) -> None:
        record = {"payload": copy.deepcopy(payload), "receipt": {"key": key, "phase": phase}}
        if key in self.records and self.records[key] != record:
            raise RuntimeError("immutable checkpoint payload conflict")
        self.records[key] = record


@pytest.mark.parametrize(
    ("candidate_outcome", "later_confirmation"),
    [
        ("measured", False),
        ("candidate_crash", False),
        ("timeout", False),
        ("degenerate_constant_scores", False),
        ("guard_input_provenance_changed", False),
        ("guard_fit_provenance_changed", False),
        ("guard_score_provenance_changed", False),
        ("measured", True),
    ],
)
def test_completed_seed_resumes_without_dispatch_or_second_budget_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    candidate_outcome: str,
    later_confirmation: bool,
) -> None:
    journal = MemoryCheckpoints()
    source = "def build_candidate():\n    return None\n"
    source_hash = hashlib.sha256(source.encode()).hexdigest()
    proposal = module.RegisteredProposal(
        experiment_id="exp_" + "1" * 32,
        experiment_number=1,
        proposal=CandidateProposal(
            hypothesis="Durable seed boundary fixture",
            move_type="features",
            candidate_source=source,
            predicted_delta=0.1,
        ),
        candidate_sha256=source_hash,
        candidate_blob_sha256=source_hash,
        inputs_sha256="a" * 64,
        messages_blob_sha256="b" * 64,
        calibration_sha256="c" * 64,
        parent_experiment_id="exp_" + "2" * 32,
        parent_tree_sha256=module._candidate_git_tree(source.encode()),
        harness_sha256="d" * 64,
        image_sha256="e" * 64,
        suite_id="seed-resume-fixture",
        suite_version=1,
        system="S1",
        input_tokens=1,
        output_tokens=1,
        ledger_sequence=3,
    )
    task = SimpleNamespace(
        task_id="evt-fixture",
        family="EVT",
        dataset_id="fixture",
        split_id="dev",
        session_id="session",
        profile_sha256="f" * 64,
    )
    dispatched: list[dict[str, Any]] = []
    mutations: list[str] = []
    terminal_outcomes: list[str] = []

    def execute(*_: Any, **kwargs: Any) -> dict[str, Any]:
        dispatched.append(kwargs)
        if candidate_outcome != "measured":
            raise CandidateExecutionRejected(candidate_outcome)
        return {
            "task_id": task.task_id,
            "experiment_id": proposal.experiment_id,
            "evaluation_kind": kwargs["evaluation_kind"],
            "seed": kwargs["seed"],
            "dataset_id": task.dataset_id,
            "split_id": task.split_id,
            "session_id": task.session_id,
            "profile_sha256": task.profile_sha256,
            "candidate_sha256": source_hash,
            "harness_sha256": proposal.harness_sha256,
            "candidate_output_sha256": "0" * 64,
            "vus_pr": 0.5,
            "vus_roc": 0.6,
            "fit_seconds": 0.01,
            "score_seconds": 0.01,
            "guards": {name: f"{name}_pass" for name in ("hardcoding", "determinism", "causality")},
        }

    def plan(*_: Any, **__: Any) -> None:
        mutations.append("task_plan")

    def transition(*_: Any, **__: Any) -> None:
        mutations.append("experiment_status")

    def terminal(*_: Any, outcome_code: str, **__: Any) -> None:
        terminal_outcomes.append(outcome_code)

    # This control-flow fixture has no persisted restart/orphan evidence.
    monkeypatch.setattr(
        evaluation_recovery, "recover_abandoned_experiment", lambda *args, **kwargs: False
    )
    monkeypatch.setattr(module, "plan_run_tasks", plan)
    monkeypatch.setattr(module, "transition_experiment", transition)
    monkeypatch.setattr(module, "record_planner_terminal_outcome", terminal)
    monkeypatch.setattr(
        module, "_task_has_terminal_outcome", lambda *args, **kwargs: bool(terminal_outcomes)
    )
    monkeypatch.setattr(module, "_append_next_checkpoint", journal.append)
    monkeypatch.setattr(module, "evaluate_and_score_seed", execute)
    monkeypatch.setattr(module, "verify_execution_identity", lambda *args, **kwargs: None)
    budget = RunBudget(wall_limit=3600, token_limit=0)
    arguments = {
        "director_engine": object(),
        "planner_engine": object(),
        "lease": journal,
        "runner": object(),
        "run_id": uuid4(),
        "proposal": proposal,
        "tasks": (task,),
        "harness_sha256": proposal.harness_sha256,
        "image_sha256": proposal.image_sha256,
        "artifact_root": tmp_path,
    }
    first = module.execute_candidate_seed(**arguments, budget=budget)
    if later_confirmation:
        module.execute_candidate_seed(
            **arguments, budget=budget, evaluation_kind="confirmation", seed=1
        )
    # Crash after completion or a later confirmation, before caller's phase checkpoint.
    snapshot = budget.snapshot()
    previous_records = copy.deepcopy(journal.records)
    previous_mutations = list(mutations)
    resumed_budget = RunBudget(wall_limit=3600, token_limit=0, snapshot=snapshot)
    recovered = module.execute_candidate_seed(**arguments, budget=resumed_budget)
    assert recovered == first
    assert len(dispatched) == (2 if later_confirmation else 1)
    assert mutations == previous_mutations
    assert journal.records == previous_records
    assert resumed_budget.wall_seconds == snapshot.wall_seconds
    assert resumed_budget.model_tokens == snapshot.model_tokens
    assert resumed_budget.reserved_wall_seconds == 0
    assert resumed_budget.snapshot().reservations == ()
