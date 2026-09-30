"""Fault injection for the real primary orchestration's retry and time accounting."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from lab.director import evaluation_recovery
from lab.director import runner as module
from lab.director.budget import RunBudget
from lab.director.contracts import CandidateProposal
from lab.director.executor import CandidateExecutionRejected


def _arguments(monkeypatch: pytest.MonkeyPatch, *, task_count: int = 1) -> dict[str, Any]:
    """Isolate control flow; Docker/SQL behavior is covered by separate live probes."""
    # This control-flow fixture has no persisted restart/orphan evidence.
    monkeypatch.setattr(
        evaluation_recovery, "recover_abandoned_experiment", lambda *args, **kwargs: False
    )
    retry_reserved: set[str] = set()

    def reserve_retry(*args: Any, experiment_id: str, **kwargs: Any) -> bool:
        if experiment_id in retry_reserved:
            return False
        retry_reserved.add(experiment_id)
        return True

    monkeypatch.setattr(evaluation_recovery, "reserve_infrastructure_retry", reserve_retry)
    monkeypatch.setattr(module, "verify_execution_identity", lambda *args, **kwargs: None)
    for name in ("plan_run_tasks", "transition_experiment", "_append_next_checkpoint",
                 "record_planner_terminal_outcome"):
        monkeypatch.setattr(module, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_read_measurement", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_task_has_terminal_outcome", lambda *args, **kwargs: False)
    proposal = module.RegisteredProposal(
        experiment_id="exp_" + "a" * 32,
        experiment_number=1,
        proposal=CandidateProposal(hypothesis="A control-flow fixture", move_type="features",
                                   candidate_source="def build_candidate(): pass\n",
                                   predicted_delta=0.1),
        candidate_sha256="b" * 64, candidate_blob_sha256="b" * 64,
        inputs_sha256="c" * 64, messages_blob_sha256="d" * 64, calibration_sha256="e" * 64,
        parent_experiment_id="exp_" + "2" * 32, parent_tree_sha256="3" * 40,
        harness_sha256="f" * 64, image_sha256="1" * 64, suite_id="synthetic.test.v1",
        suite_version=1, system="S2", input_tokens=10, output_tokens=20,
    )
    tasks = tuple(SimpleNamespace(task_id=f"task-{index}", dataset_id=f"data-{index}",
                                  split_id="dev", session_id="session")
                  for index in range(task_count))
    return {
        "director_engine": object(), "planner_engine": object(), "runner": object(),
        "lease": SimpleNamespace(
            heartbeat=lambda: None,
            require_run_active=lambda: None,
            read_checkpoint=lambda **kwargs: None,
        ),
        "run_id": uuid4(), "proposal": proposal, "tasks": tasks,
        "harness_sha256": "f" * 64, "image_sha256": "1" * 64,
        "budget": RunBudget(wall_limit=1000, token_limit=0),
    }


def test_primary_successful_retry_does_not_raise_the_previous_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _arguments(monkeypatch)
    calls = []

    def evaluation(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("temporary infrastructure failure")
        return {"task_id": "task-0", "vus_pr": 0.5, "vus_roc": 0.6}

    monkeypatch.setattr(module, "evaluate_and_score_seed", evaluation)
    result = module.execute_primary_seed(**arguments)
    assert result.status == "primary_measured"
    assert len(result.measurements) == 1
    assert len(calls) == 2


def test_measurement_checkpoint_error_does_not_repeat_completed_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _arguments(monkeypatch)
    calls = []

    def evaluation(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"task_id": "task-0", "vus_pr": 0.5, "vus_roc": 0.6}

    def checkpoint(*args: Any, **kwargs: Any) -> None:
        if kwargs["phase"] == "primary_measured":
            raise RuntimeError("result checkpoint unavailable")

    monkeypatch.setattr(module, "evaluate_and_score_seed", evaluation)
    monkeypatch.setattr(module, "_append_next_checkpoint", checkpoint)
    with pytest.raises(RuntimeError, match="result checkpoint unavailable"):
        module.execute_primary_seed(**arguments)
    assert len(calls) == 1


def test_primary_tasks_share_one_seed_wall_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    arguments = _arguments(monkeypatch, task_count=2)
    now = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    arguments["budget"] = RunBudget(wall_limit=1000, token_limit=0, monotonic=lambda: now[0])
    deadlines = []

    def evaluation(*args: Any, **kwargs: Any) -> dict[str, Any]:
        deadlines.append(kwargs["remaining_seconds"])
        now[0] += 400.0 if len(deadlines) == 1 else 1.0
        return {"task_id": kwargs["task"].task_id, "vus_pr": 0.5, "vus_roc": 0.6}

    monkeypatch.setattr(module, "evaluate_and_score_seed", evaluation)
    result = module.execute_primary_seed(**arguments)
    assert result.status == "primary_measured"
    assert deadlines[0] <= 600
    assert deadlines[1] <= 200


def test_candidate_crash_returns_for_atomic_terminal_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = _arguments(monkeypatch)
    transitions = []

    def evaluation(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise CandidateExecutionRejected("candidate_crash")

    def transition(*args: Any, **kwargs: Any) -> None:
        transitions.append(kwargs["status"])
        assert kwargs["status"] in {
            "primary_running", "awaiting_confirmation", "confirmation_running"
        }

    monkeypatch.setattr(module, "evaluate_and_score_seed", evaluation)
    monkeypatch.setattr(module, "transition_experiment", transition)
    result = module.execute_primary_seed(**arguments)
    assert result.status == "crashed"
    assert result.terminal_code == "candidate_crash"
    assert transitions == ["primary_running"]
