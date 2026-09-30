"""Future worker identity regressions; fixture processes never launch services."""

import json
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import UUID

import pytest

from lab.scorer import supervisor

JOB = UUID("da64e3cb-7601-4313-a7ab-985a02a93aa1")
UNIT = f"swapp-ai-scientist-scorer-{JOB.hex}.service"
INVOCATION = "a" * 32


def completed_status():
    return {
        "job_id": str(JOB),
        "state": "completed",
        "worker_unit": UNIT,
        "worker_invocation_id": INVOCATION,
        "worker_claim_attempt": "2",
    }


def test_completed_identity_accepts_fast_and_observed_generation():
    result = completed_status()
    assert supervisor._completed_worker_metadata(JOB, UNIT, result) == (INVOCATION, 2)
    assert supervisor._completed_worker_metadata(
        JOB, UNIT, result, observed_invocation_id=INVOCATION
    ) == (INVOCATION, 2)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("job_id", str(UUID(int=1))),
        ("job_id", JOB.hex),
        ("worker_unit", "other.service"),
        ("worker_invocation_id", "A" * 32),
        ("worker_invocation_id", "a" * 31),
        ("worker_invocation_id", "g" * 32),
        ("worker_claim_attempt", "0"),
        ("worker_claim_attempt", "-1"),
        ("worker_claim_attempt", "+1"),
        ("worker_claim_attempt", "01"),
        ("worker_claim_attempt", "1.0"),
        ("state", "cancelled_after_drain"),
    ],
)
def test_completed_identity_rejects_contradictory_metadata(field, value):
    result = completed_status()
    result[field] = value
    with pytest.raises(RuntimeError, match="identity"):
        supervisor._completed_worker_metadata(JOB, UNIT, result)


@pytest.mark.parametrize("field", list(completed_status()))
def test_completed_identity_requires_each_field(field):
    result = completed_status()
    del result[field]
    with pytest.raises(RuntimeError, match="identity"):
        supervisor._completed_worker_metadata(JOB, UNIT, result)


def test_completed_identity_rejects_changed_observed_generation():
    with pytest.raises(RuntimeError, match="identity"):
        supervisor._completed_worker_metadata(
            JOB, UNIT, completed_status(), observed_invocation_id="b" * 32
        )


def test_completed_identity_rejects_wrong_launch_unit():
    with pytest.raises(RuntimeError, match="identity"):
        supervisor._completed_worker_metadata(JOB, "other.service", completed_status())


def test_previous_positional_result_constructors_remain_valid():
    for result in (
        supervisor.ScorerProcessResult(JOB, UNIT, 0, None),
        supervisor.ScorerProcessResult(JOB, UNIT, 0, None, "stderr"),
        supervisor.ScorerProcessResult(JOB, UNIT, 0, None, None, INVOCATION),
    ):
        assert result.attempt is None


def launch_fixture(monkeypatch, *, early, exit_code=0, observed=INVOCATION):
    process = SimpleNamespace(
        returncode=exit_code,
        poll=lambda: exit_code if early else None,
        communicate=lambda **kwargs: (json.dumps(completed_status()), ""),
    )
    states = iter(
        [
            {
                "LoadState": "not-found",
                "ActiveState": "inactive",
                "MainPID": "0",
                "InvocationID": "",
                "ControlGroup": "",
            },
            {"LoadState": "loaded", "ActiveState": "active", "InvocationID": observed},
        ]
    )
    slice_group = "/fixture/swapp-ai-scientist-scorer.slice"
    expected_group = f"{slice_group}/{UNIT}"

    def show(unit, *, timeout_seconds=5):
        assert timeout_seconds > 0
        if unit == supervisor.SCORER_SLICE:
            return {"LoadState": "loaded", "ControlGroup": slice_group}
        assert unit == UNIT
        state = next(states)
        if state["LoadState"] == "loaded":
            state["ControlGroup"] = expected_group
        return state

    monkeypatch.setattr(supervisor, "_ensure_aggregate_slice", lambda: None)
    monkeypatch.setattr(supervisor, "_job_lifecycle_lock", lambda job: nullcontext())
    monkeypatch.setattr(supervisor, "_systemctl_show", show)
    monkeypatch.setattr(
        supervisor, "_cgroup_is_absent_or_empty", lambda group: group == expected_group
    )
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *args, **kwargs: process)
    return process


@pytest.mark.parametrize("early", [True, False])
def test_actual_zero_waiter_retains_completed_identity(monkeypatch, early):
    launch_fixture(monkeypatch, early=early)
    result = supervisor.run_scorer_process(JOB)
    assert result.exit_code == 0
    assert result.invocation_id == INVOCATION
    assert result.attempt == 2


def test_active_branch_rejects_stdout_generation_mismatch(monkeypatch):
    process = launch_fixture(monkeypatch, early=False, observed="b" * 32)
    # The completed waiter has exited; failure cleanup must not stop a live unit.
    calls = iter([None, 0])
    process.poll = lambda: next(calls, 0)
    with pytest.raises(RuntimeError, match="identity"):
        supervisor.run_scorer_process(JOB)


def test_failed_waiter_never_uses_completed_stdout_as_success(monkeypatch):
    launch_fixture(monkeypatch, early=True, exit_code=1)
    result = supervisor.run_scorer_process(JOB)
    assert result.exit_code == 1
    assert result.result is None
    assert result.invocation_id is None
    assert result.attempt is None


def test_recovery_fast_branch_never_reinterprets_completed_stdout(monkeypatch):
    launch_fixture(monkeypatch, early=True)
    result = supervisor.run_scorer_process(JOB, _recovery_expected_invocation_id="b" * 32)
    assert result.invocation_id is None
    assert result.attempt is None


def test_fast_branch_rejects_wrong_completed_job(monkeypatch):
    process = launch_fixture(monkeypatch, early=True)
    status = completed_status()
    status["job_id"] = str(UUID(int=1))
    process.communicate = lambda **kwargs: (json.dumps(status), "")
    with pytest.raises(RuntimeError, match="identity"):
        supervisor.run_scorer_process(JOB)
