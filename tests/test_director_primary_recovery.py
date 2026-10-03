"""Fault injection for the real primary orchestration's retry and time accounting."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
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


def _deadline_arguments(monkeypatch: pytest.MonkeyPatch, *, task_count: int) -> SimpleNamespace:
    """Keep real seed orchestration/accounting; model immutable SQL receipts and clock."""
    arguments = _arguments(monkeypatch, task_count=task_count)
    now = [0.0]
    epoch = datetime(2026, 10, 2, tzinfo=UTC)

    class ClockDateTime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return (epoch + timedelta(seconds=now[0])).astimezone(tz)

    monkeypatch.setattr(module, "datetime", ClockDateTime)
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    arguments["budget"] = RunBudget(wall_limit=1000, token_limit=0, monotonic=lambda: now[0])
    records: dict[str, dict[str, Any]] = {}
    outcomes: dict[str, str] = {}
    reserved = []

    def checkpoint(*args: Any, key: str, phase: str, payload: dict[str, Any], **_: Any) -> None:
        value = {"payload": copy.deepcopy(payload), "receipt": {"key": key, "phase": phase}}
        if key in records:
            assert records[key] == value, "immutable checkpoint changed"
        records[key] = value
        if phase == "seed_reserved":
            reserved.append(arguments["budget"].snapshot())

    def measurement(*args: Any, task: Any, evaluation_kind: str, seed: int, **_: Any) -> Any:
        key = f"measurement:1:{task.task_id}:{evaluation_kind}:{seed}"
        return copy.deepcopy(records.get(key, {}).get("payload", {}).get("measurement"))

    def terminal(*args: Any, task_id: str, outcome_code: str, **_: Any) -> None:
        if task_id in outcomes:
            assert outcomes[task_id] == outcome_code, "terminal outcome changed"
        outcomes[task_id] = outcome_code

    arguments["lease"].read_checkpoint = lambda key, **_: copy.deepcopy(records.get(key))
    monkeypatch.setattr(module, "_append_next_checkpoint", checkpoint)
    monkeypatch.setattr(module, "_read_measurement", measurement)
    monkeypatch.setattr(module, "record_planner_terminal_outcome", terminal)
    monkeypatch.setattr(
        module, "_task_has_terminal_outcome", lambda *args, task, **kwargs: task.task_id in outcomes
    )
    return SimpleNamespace(
        arguments=arguments, now=now, records=records, outcomes=outcomes,
        reserved=reserved, terminal=terminal,
    )


@pytest.mark.parametrize(
    ("kind", "seed"), [("primary", 0), ("confirmation", 1), ("confirmation", 2)]
)
@pytest.mark.parametrize("elapsed", [599.5, 600.25])
def test_seed_deadline_between_tasks_finishes_timeout_and_resumes_without_new_work(
    monkeypatch: pytest.MonkeyPatch, kind: str, seed: int, elapsed: float,
) -> None:
    fixture = _deadline_arguments(monkeypatch, task_count=3)
    fixture.arguments.update(evaluation_kind=kind, seed=seed)
    calls = []

    def evaluate(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        fixture.now[0] = elapsed
        return {"task_id": kwargs["task"].task_id, "vus_pr": 0.5, "vus_roc": 0.6}

    monkeypatch.setattr(module, "evaluate_and_score_seed", evaluate)
    result = module.execute_candidate_seed(**fixture.arguments)
    assert result.status == "rejected" and result.terminal_code == "timeout"
    assert result.wall_seconds == elapsed
    assert len(result.measurements) == 1
    assert len(calls) == 1 and calls[0]["remaining_seconds"] == 600
    assert fixture.outcomes == {"task-1": "candidate_timeout", "task-2": "guard_rejected"}
    proposal = fixture.arguments["proposal"]
    key = f"seed-complete:{proposal.experiment_id}:{kind}:{seed}"
    receipt = fixture.records[key]["payload"]
    assert receipt["terminal_code"] == "timeout"
    assert receipt["elapsed_wall_seconds"] == elapsed
    assert receipt["reservation_id"] == str(fixture.reserved[0].reservations[0].reservation_id)
    assert receipt["budget"]["wall_seconds"] == elapsed
    assert receipt["budget"]["reservations"] == []
    before = copy.deepcopy(fixture.records)
    # Both a pre-completion budget checkpoint and an already charged one must resume once.
    for snapshot in (fixture.reserved[0], fixture.arguments["budget"].snapshot()):
        resumed = RunBudget(
            wall_limit=1000, token_limit=0, snapshot=snapshot, monotonic=lambda: fixture.now[0]
        )
        replay = module.execute_candidate_seed(**{**fixture.arguments, "budget": resumed})
        assert replay == result
        assert resumed.wall_seconds == elapsed
        assert resumed.snapshot().reservations == ()
        assert fixture.records == before and len(calls) == 1


@pytest.mark.parametrize("candidate_timeout", [False, True])
def test_seed_post_loop_overrun_records_actual_wall_and_terminal_timeout(
    monkeypatch: pytest.MonkeyPatch, candidate_timeout: bool,
) -> None:
    fixture = _deadline_arguments(monkeypatch, task_count=1)

    def evaluate(*args: Any, **kwargs: Any) -> dict[str, Any]:
        fixture.now[0] = 600.25
        if candidate_timeout:
            raise CandidateExecutionRejected("timeout")
        return {"task_id": kwargs["task"].task_id, "vus_pr": 0.5, "vus_roc": 0.6}

    monkeypatch.setattr(module, "evaluate_and_score_seed", evaluate)
    result = module.execute_candidate_seed(**fixture.arguments)
    assert result.status == "rejected" and result.terminal_code == "timeout"
    assert result.wall_seconds == fixture.arguments["budget"].wall_seconds == 600.25
    assert fixture.arguments["budget"].snapshot().reservations == ()
    assert len(result.measurements) == (0 if candidate_timeout else 1)
    assert fixture.outcomes == ({"task-0": "candidate_timeout"} if candidate_timeout else {})
    assert any(row["receipt"]["phase"] == "seed_complete" for row in fixture.records.values())
    assert module.execute_candidate_seed(**fixture.arguments) == result
    assert fixture.arguments["budget"].wall_seconds == 600.25


@pytest.mark.parametrize("elapsed", [599.5, 600.25])
def test_expired_seed_retry_preserves_original_infrastructure_failure(
    monkeypatch: pytest.MonkeyPatch, elapsed: float,
) -> None:
    fixture = _deadline_arguments(monkeypatch, task_count=2)
    failure = RuntimeError("owned Scorer cleanup is unverified")
    calls = []

    def evaluate(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        fixture.now[0] = elapsed
        raise failure

    monkeypatch.setattr(module, "evaluate_and_score_seed", evaluate)
    with pytest.raises(RuntimeError) as caught:
        module.execute_candidate_seed(**fixture.arguments)
    assert caught.value is failure
    assert len(calls) == 1 and fixture.outcomes == {}
    assert fixture.arguments["budget"].wall_seconds == elapsed
    assert all(row["receipt"]["phase"] != "seed_complete" for row in fixture.records.values())


def test_timeout_partial_pending_outcomes_resume_same_reservation_idempotently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _deadline_arguments(monkeypatch, task_count=3)
    calls = []

    def evaluate(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        fixture.now[0] = 600.25
        return {"task_id": kwargs["task"].task_id, "vus_pr": 0.5, "vus_roc": 0.6}

    def interrupted_terminal(*args: Any, **kwargs: Any) -> None:
        if kwargs["task_id"] == "task-2":
            raise RuntimeError("terminal receipt transaction unavailable")
        fixture.terminal(*args, **kwargs)

    monkeypatch.setattr(module, "evaluate_and_score_seed", evaluate)
    monkeypatch.setattr(module, "record_planner_terminal_outcome", interrupted_terminal)
    with pytest.raises(RuntimeError, match="terminal receipt transaction unavailable"):
        module.execute_candidate_seed(**fixture.arguments)
    assert fixture.outcomes == {"task-1": "candidate_timeout"}
    assert all(row["receipt"]["phase"] != "seed_complete" for row in fixture.records.values())
    monkeypatch.setattr(module, "record_planner_terminal_outcome", fixture.terminal)
    fixture.now[0] = 601.0
    result = module.execute_candidate_seed(**fixture.arguments)
    assert result.terminal_code == "timeout" and result.wall_seconds == 601.0
    assert len(calls) == 1 and len(fixture.reserved) == 1
    assert fixture.outcomes == {"task-1": "candidate_timeout", "task-2": "guard_rejected"}
    assert fixture.arguments["budget"].wall_seconds == 601.0


@pytest.mark.parametrize(
    "failure_reason",
    [
        "wrong execution owner",
        "stale execution generation",
        "Planner cannot resolve a task after a score job exists",
        "director_run_is_not_active",
    ],
)
def test_expired_seed_does_not_bypass_terminal_writer_or_lease_fences(
    monkeypatch: pytest.MonkeyPatch, failure_reason: str,
) -> None:
    fixture = _deadline_arguments(monkeypatch, task_count=2)
    failure = RuntimeError(failure_reason)

    def evaluate(*args: Any, **kwargs: Any) -> dict[str, Any]:
        fixture.now[0] = 600.25
        return {"task_id": kwargs["task"].task_id, "vus_pr": 0.5, "vus_roc": 0.6}

    def denied(*args: Any, **kwargs: Any) -> None:
        if fixture.now[0] > 600:
            raise failure

    monkeypatch.setattr(module, "evaluate_and_score_seed", evaluate)
    if failure_reason == "director_run_is_not_active":
        fixture.arguments["lease"].require_run_active = denied
    else:
        monkeypatch.setattr(module, "record_planner_terminal_outcome", denied)
    with pytest.raises(RuntimeError) as caught:
        module.execute_candidate_seed(**fixture.arguments)
    assert caught.value is failure and fixture.outcomes == {}
    assert all(row["receipt"]["phase"] != "seed_complete" for row in fixture.records.values())
    assert fixture.arguments["budget"].snapshot().reservations == fixture.reserved[0].reservations


def test_overrun_receipt_cannot_resume_as_a_successful_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _deadline_arguments(monkeypatch, task_count=1)

    def evaluate(*args: Any, **kwargs: Any) -> dict[str, Any]:
        fixture.now[0] = 600.25
        return {"task_id": kwargs["task"].task_id, "vus_pr": 0.5, "vus_roc": 0.6}

    monkeypatch.setattr(module, "evaluate_and_score_seed", evaluate)
    module.execute_candidate_seed(**fixture.arguments)
    for row in fixture.records.values():
        if row["receipt"]["phase"] == "seed_complete":
            row["payload"]["terminal_code"] = None
    with pytest.raises(
        RuntimeError, match="exceeded its reservation without a candidate rejection"
    ):
        module.execute_candidate_seed(**fixture.arguments)
