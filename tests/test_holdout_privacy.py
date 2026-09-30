"""Holdout receipts keep metrics private and expose only one terminal bit."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from uuid import UUID, uuid4

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select

from harness.contracts import AlarmPolicy
from lab.api.app import create_app
from lab.api.registry import SuiteEntry, SuiteRegistry
from lab.db.schema import metadata, runs
from lab.director import holdout as director_holdout
from lab.director.holdout import HoldoutReceipt
from lab.director.loop import _budget_to_json
from lab.scorer.holdout import (
    _remaining_seconds,
    _semantics_digest,
    _task_metric,
    _validate_semantics,
)
from lab.scorer.holdout_supervisor import _parse_status


@pytest.mark.parametrize(
    ("wall", "elapsed", "reserved_wall", "tokens", "reserved_tokens", "expected"),
    [
        (0, 0, 0, 0, 0, False),
        (9.25, 0, 0, 0, 0, True),
        (0, 10, 0, 0, 0, True),
        (0, 0, 0, 10, 0, True),
        (0, 0, 0, 0, 0, False),
        (0, 0, 9, 9, 0, False),
    ],
)
def test_run_end_budget_predicate_matches_run_budget_admission(
    wall, elapsed, reserved_wall, tokens, reserved_tokens, expected
) -> None:
    from lab.director.holdout import _budget_is_exhausted

    snapshot = {
        "wall_seconds": wall,
        "elapsed_wall_seconds": elapsed,
        "reserved_wall_seconds": reserved_wall,
        "model_tokens": tokens,
        "reserved_model_tokens": reserved_tokens,
    }
    assert _budget_is_exhausted(snapshot, wall_limit=10, token_limit=10) is expected


def test_zero_token_limit_is_exhausted_like_run_budget() -> None:
    from lab.director.holdout import _budget_is_exhausted

    snapshot = {
        "wall_seconds": 0,
        "elapsed_wall_seconds": 0,
        "reserved_wall_seconds": 0,
        "model_tokens": 0,
        "reserved_model_tokens": 0,
    }
    assert _budget_is_exhausted(snapshot, wall_limit=10, token_limit=0)


@pytest.mark.parametrize(
    "snapshot",
    [
        {"wall_seconds": None},
        {"wall_seconds": float("nan")},
        {"model_tokens": 0.5},
        {"reserved_model_tokens": True},
    ],
)
def test_run_end_budget_predicate_rejects_malformed_snapshots(snapshot) -> None:
    from lab.director.holdout import _budget_is_exhausted

    valid = {
        "wall_seconds": 0,
        "elapsed_wall_seconds": 0,
        "reserved_wall_seconds": 0,
        "model_tokens": 0,
        "reserved_model_tokens": 0,
    }
    valid.update(snapshot)
    with pytest.raises(ValueError):
        _budget_is_exhausted(valid, wall_limit=10, token_limit=10)


def test_run_end_ordinal_coverage_requires_verified_abandonment_checkpoint() -> None:
    from lab.director.holdout import _validated_abandonment_claims

    run_id = uuid4()
    digest = "a" * 64
    checkpoint = {
        "payload": {
            "run_id": str(run_id),
            "ordinal": 2,
            "reason": "tool_parse",
        },
        "receipt": {"phase": "proposal_abandoned", "payload_sha256": digest},
    }

    class Lease:
        def read_checkpoint(self, *, key, artifact_root):
            assert artifact_root == "private-artifacts"
            return checkpoint if key == "proposal-abandoned:2" else None

    claims = _validated_abandonment_claims(
        Lease(),
        run_id=run_id,
        proposal_ordinals={1},
        completed_proposals=2,
        artifact_root="private-artifacts",
    )
    assert claims == [{"ordinal": 2, "key": "proposal-abandoned:2", "payload_sha256": digest}]

    checkpoint["payload"]["ordinal"] = 3
    with pytest.raises(ValueError, match="not a validated checkpoint"):
        _validated_abandonment_claims(
            Lease(),
            run_id=run_id,
            proposal_ordinals={1},
            completed_proposals=2,
            artifact_root="private-artifacts",
        )


@pytest.mark.parametrize(
    ("state", "bit"),
    [("passed", True), ("reverted", False)],
)
def test_terminal_holdout_receipt_is_exactly_one_bit(state: str, bit: bool) -> None:
    receipt = HoldoutReceipt(reservation_id=uuid4(), state=state, bit=bit)  # type: ignore[arg-type]

    assert receipt.bit is bit
    assert not hasattr(receipt, "delta")
    assert not hasattr(receipt, "task_scores")
    assert not hasattr(receipt, "labels")


@pytest.mark.parametrize(
    ("state", "bit"),
    [("reserved", None), ("running", None), ("failed", None), ("exhausted", None)],
)
def test_nonterminal_or_operational_holdout_receipt_has_no_bit(state: str, bit: None) -> None:
    receipt = HoldoutReceipt(reservation_id=uuid4(), state=state, bit=bit)  # type: ignore[arg-type]

    assert receipt.bit is None


def test_holdout_receipt_rejects_score_material_and_wrong_bits() -> None:
    with pytest.raises(ValueError, match="requires exactly one boolean bit"):
        HoldoutReceipt(reservation_id=uuid4(), state="passed", bit=None)
    with pytest.raises(ValueError, match="state and privacy bit disagree"):
        HoldoutReceipt(reservation_id=uuid4(), state="reverted", bit=True)
    with pytest.raises(ValueError, match="cannot expose a result bit"):
        HoldoutReceipt(reservation_id=uuid4(), state="failed", bit=False)


def test_quota_exhaustion_marks_manual_review_without_changing_dev_champion() -> None:
    from lab.director.loop import DirectorLoop, DirectorLoopState

    run_id = uuid4()
    state = DirectorLoopState.model_validate(
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
            "champion_experiment_id": "baseline",
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
    receipt = HoldoutReceipt(
        reservation_id=uuid4(),
        state="exhausted",
        bit=None,
        run_id=run_id,
        candidate_experiment_id="baseline",
        trigger_kind="run_end",
        trigger_index=1,
    )
    loop = object.__new__(DirectorLoop)
    from lab.director.budget import BudgetSnapshot, RunBudget

    loop.budget = RunBudget(
        proposal_limit=1,
        wall_limit=60,
        token_limit=100,
        snapshot=BudgetSnapshot(wall_seconds=59),
    )
    frozen_budget = _budget_to_json(BudgetSnapshot(wall_seconds=3, elapsed_wall_seconds=4))
    next_state = loop._state_after_holdout(
        state,
        receipt,
        trigger_kind="run_end",
        trigger_index=1,
        budget_snapshot=frozen_budget,
    )

    assert next_state.budget == frozen_budget
    assert next_state.champion_experiment_id == "baseline"
    assert next_state.holdout_quota_exhausted is True
    assert next_state.holdout_last_status == "quota_exhausted"
    assert next_state.holdout_checks_passed == 0
    assert "human review" in next_state.recent_feedback[-1]


def test_holdout_application_rejects_receipt_bound_to_another_run() -> None:
    from lab.director.loop import DirectorLoop

    expected_run = uuid4()
    receipt = HoldoutReceipt(
        reservation_id=uuid4(),
        state="exhausted",
        bit=None,
        run_id=uuid4(),
        candidate_experiment_id="candidate",
        trigger_kind="run_end",
        trigger_index=1,
    )
    loop = object.__new__(DirectorLoop)
    loop.run_id = expected_run

    with pytest.raises(ValueError, match="identity differs"):
        loop._record_holdout_application(
            object(), {}, receipt, trigger_kind="run_end", trigger_index=1, candidate_id="candidate"
        )


def test_holdout_trigger_does_not_relaunch_an_existing_reservation(monkeypatch) -> None:
    run_id = uuid4()
    reservation_id = uuid4()
    receipt = HoldoutReceipt(
        reservation_id=reservation_id,
        state="running",
        bit=None,
        run_id=run_id,
        candidate_experiment_id="exp_existing",
        trigger_kind="keep_interval",
        trigger_index=10,
    )
    monkeypatch.setattr(director_holdout, "reserve_holdout_check", lambda *args, **kwargs: receipt)

    def forbidden_launch(*args, **kwargs):
        raise AssertionError("existing running holdout must not launch another worker")

    from lab.scorer import holdout_supervisor

    monkeypatch.setattr(holdout_supervisor, "run_holdout_process", forbidden_launch)
    recovery_calls: list[UUID] = []

    def recover(received_id, *, remaining_seconds):
        recovery_calls.append(received_id)
        assert remaining_seconds >= 1
        return type("RecoveryResult", (), {"state": "pending"})()

    monkeypatch.setattr(holdout_supervisor, "run_holdout_recovery_process", recover)
    monkeypatch.setattr(
        director_holdout,
        "read_holdout_bit",
        lambda *args, **kwargs: receipt,
    )
    result = director_holdout.evaluate_holdout_check(
        object(),
        run_id=run_id,
        candidate_experiment_id="exp_existing",
        trigger_kind="keep_interval",
        trigger_index=10,
    )
    assert result == receipt
    assert recovery_calls == [reservation_id]


def test_reserved_retry_uses_recovery_and_never_starts_first_worker(monkeypatch) -> None:
    run_id = uuid4()
    reservation_id = uuid4()
    reserved = HoldoutReceipt(
        reservation_id=reservation_id,
        state="reserved",
        bit=None,
        admitted_generation=1,
        execution_sha256="c" * 64,
        run_id=run_id,
        candidate_experiment_id="exp_reserved",
        trigger_kind="keep_interval",
        trigger_index=10,
    )
    failed = HoldoutReceipt(
        reservation_id=reservation_id,
        state="failed",
        bit=None,
        run_id=run_id,
        candidate_experiment_id="exp_reserved",
        trigger_kind="keep_interval",
        trigger_index=10,
        admitted_generation=1,
        execution_sha256="c" * 64,
    )
    monkeypatch.setattr(director_holdout, "reserve_holdout_check", lambda *_a, **_kw: reserved)
    from lab.scorer import holdout_supervisor

    recovery_calls: list[UUID] = []
    monkeypatch.setattr(
        holdout_supervisor,
        "run_holdout_process",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("retry must not launch")),
    )
    monkeypatch.setattr(
        holdout_supervisor,
        "run_holdout_recovery_process",
        lambda received_id, **_kw: (
            recovery_calls.append(received_id) or type("RecoveryResult", (), {"state": "failed"})()
        ),
    )
    monkeypatch.setattr(director_holdout, "read_holdout_bit", lambda *_a, **_kw: failed)

    result = director_holdout.evaluate_holdout_check(
        object(),
        run_id=run_id,
        candidate_experiment_id="exp_reserved",
        trigger_kind="keep_interval",
        trigger_index=10,
    )

    assert result == failed
    assert recovery_calls == [reservation_id]


def test_fresh_quota_exhaustion_reads_bitless_receipt_without_worker(monkeypatch) -> None:
    run_id = uuid4()
    candidate_id = "exp_candidate"
    exhausted_reservation = uuid4()
    exhausted = HoldoutReceipt(
        reservation_id=exhausted_reservation,
        state="exhausted",
        bit=None,
        run_id=run_id,
        candidate_experiment_id=candidate_id,
        trigger_kind="run_end",
        trigger_index=1,
    )
    reservation_calls: list[UUID] = []
    reads: list[UUID] = []

    def reserve(_engine, **kwargs):
        reservation_calls.append(kwargs["reservation_id"])
        return HoldoutReceipt(reservation_id=kwargs["reservation_id"], state="exhausted", bit=None)

    def read(_engine, *, run_id: UUID, reservation_id: UUID):
        assert run_id == exhausted.run_id
        reads.append(reservation_id)
        return replace(exhausted, reservation_id=reservation_id)

    from lab.scorer import holdout_supervisor

    monkeypatch.setattr(director_holdout, "reserve_holdout_check", reserve)
    monkeypatch.setattr(director_holdout, "read_holdout_bit", read)
    monkeypatch.setattr(
        holdout_supervisor,
        "run_holdout_process",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("exhausted query has no worker")),
    )
    monkeypatch.setattr(
        holdout_supervisor,
        "run_holdout_recovery_process",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError("new quota exhaustion has no prior reservation to recover")
        ),
    )

    receipt = director_holdout.evaluate_holdout_check(
        object(),
        run_id=run_id,
        candidate_experiment_id=candidate_id,
        trigger_kind="run_end",
        trigger_index=1,
    )

    assert receipt.state == "exhausted"
    assert receipt.bit is None
    assert reads == reservation_calls


def test_holdout_trigger_launches_one_new_reservation_and_checks_durable_bit(monkeypatch) -> None:
    run_id = uuid4()
    reservation_id = uuid4()
    reserved = HoldoutReceipt(
        reservation_id=reservation_id,
        state="reserved",
        bit=None,
        run_id=run_id,
        candidate_experiment_id="exp_candidate",
        trigger_kind="run_end",
        trigger_index=1,
        admitted_generation=1,
        execution_sha256="c" * 64,
    )
    passed = HoldoutReceipt(
        reservation_id=reservation_id,
        state="passed",
        bit=True,
        run_id=run_id,
        candidate_experiment_id="exp_candidate",
        trigger_kind="run_end",
        trigger_index=1,
        admitted_generation=1,
        execution_sha256="c" * 64,
    )
    call_log: list[object] = []

    def reserve(_engine, **kwargs):
        call_log.append(("reserve", kwargs))
        return replace(reserved, reservation_id=kwargs["reservation_id"])

    def run_process(received_id, *, admitted_generation, execution_sha256, remaining_seconds):
        call_log.append(
            ("worker", received_id, admitted_generation, execution_sha256, remaining_seconds)
        )
        return type("WorkerResult", (), {"state": "passed", "bit": True})()

    def read_result(_engine, *, run_id: UUID, reservation_id: UUID):
        call_log.append(("read", run_id, reservation_id))
        return replace(passed, reservation_id=reservation_id)

    from lab.scorer import holdout_supervisor

    monkeypatch.setattr(director_holdout, "reserve_holdout_check", reserve)
    monkeypatch.setattr(director_holdout, "read_holdout_bit", read_result)
    monkeypatch.setattr(holdout_supervisor, "run_holdout_process", run_process)

    result = director_holdout.evaluate_holdout_check(
        object(),
        run_id=run_id,
        candidate_experiment_id="exp_candidate",
        trigger_kind="run_end",
        trigger_index=1,
        remaining_seconds=180,
    )
    assert result.state == "passed"
    assert result.reservation_id == call_log[1][1]
    assert [str(item[0]) for item in call_log] == ["reserve", "worker", "read"]
    request_key = call_log[0][1]["request_key"]
    assert request_key.startswith("run_end:1:")
    assert "exp_candidate" not in request_key
    assert call_log[1][0] == "worker"
    assert call_log[1][1] == call_log[0][1]["reservation_id"]
    assert call_log[1][2:4] == (1, "c" * 64)
    assert 1 <= call_log[1][4] <= 180


def test_holdout_reservation_rpc_time_is_charged_to_worker_deadline(monkeypatch) -> None:
    ticks = iter((100.0, 102.25))
    monkeypatch.setattr(director_holdout.time, "monotonic", lambda: next(ticks))
    run_id = uuid4()
    reservation_id = uuid4()
    reserved = HoldoutReceipt(
        reservation_id=reservation_id,
        state="reserved",
        bit=None,
        admitted_generation=1,
        execution_sha256="c" * 64,
    )
    monkeypatch.setattr(
        director_holdout,
        "reserve_holdout_check",
        lambda _engine, **kwargs: replace(reserved, reservation_id=kwargs["reservation_id"]),
    )
    from lab.scorer import holdout_supervisor

    calls: list[int] = []
    monkeypatch.setattr(
        holdout_supervisor,
        "run_holdout_process",
        lambda _reservation_id, *, admitted_generation, execution_sha256, remaining_seconds: (
            (calls.append(remaining_seconds))
            or type("WorkerResult", (), {"state": "passed", "bit": True})()
        ),
    )
    monkeypatch.setattr(
        director_holdout,
        "read_holdout_bit",
        lambda _engine, *, reservation_id, **_kwargs: HoldoutReceipt(
            reservation_id=reservation_id,
            state="passed",
            bit=True,
            run_id=run_id,
            candidate_experiment_id="exp_candidate",
            trigger_kind="run_end",
            trigger_index=1,
            admitted_generation=1,
            execution_sha256="c" * 64,
        ),
    )

    director_holdout.evaluate_holdout_check(
        object(),
        run_id=run_id,
        candidate_experiment_id="exp_candidate",
        trigger_kind="run_end",
        trigger_index=1,
        remaining_seconds=20,
    )

    assert calls == [17]


def test_holdout_reservation_consuming_deadline_does_not_start_worker(monkeypatch) -> None:
    ticks = iter((100.0, 102.0))
    monkeypatch.setattr(director_holdout.time, "monotonic", lambda: next(ticks))
    reservation_id = uuid4()
    reserved = HoldoutReceipt(
        reservation_id=reservation_id,
        state="reserved",
        bit=None,
        admitted_generation=1,
        execution_sha256="c" * 64,
    )
    monkeypatch.setattr(
        director_holdout,
        "reserve_holdout_check",
        lambda _engine, **kwargs: replace(reserved, reservation_id=kwargs["reservation_id"]),
    )
    from lab.scorer import holdout_supervisor

    monkeypatch.setattr(
        holdout_supervisor,
        "run_holdout_process",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("worker must not start")),
    )
    with pytest.raises(TimeoutError, match="reservation consumed"):
        director_holdout.evaluate_holdout_check(
            object(),
            run_id=uuid4(),
            candidate_experiment_id="exp_candidate",
            trigger_kind="run_end",
            trigger_index=1,
            remaining_seconds=1,
        )


def test_worker_status_parser_binds_reservation_and_rejects_metrics() -> None:
    reservation_id = uuid4()
    valid = '{"reservation_id":"' + str(reservation_id) + '","state":"passed","bit":true}'

    assert _parse_status(valid, reservation_id) == ("passed", True)
    with pytest.raises(RuntimeError, match="identity"):
        _parse_status(valid.replace(str(reservation_id), str(uuid4())), reservation_id)
    with pytest.raises(RuntimeError, match="identity"):
        _parse_status(
            '{"reservation_id":"'
            + str(reservation_id)
            + '","state":"passed","bit":true,"delta":0.23}',
            reservation_id,
        )


def test_holdout_remaining_budget_fails_closed_after_deadline() -> None:
    import time

    with pytest.raises(TimeoutError, match="total evaluation budget"):
        _remaining_seconds(time.monotonic() - 1)


def test_holdout_worker_deadline_begins_before_generation_claim(monkeypatch) -> None:
    from lab.scorer import holdout as scorer_holdout

    ticks = iter((100.0, 102.0))
    monkeypatch.setattr(scorer_holdout.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(
        "lab.scorer.worker.verify_systemd_invocation",
        lambda _unit: (_ for _ in ()).throw(AssertionError("claim setup must not run")),
    )

    with pytest.raises(TimeoutError, match="total evaluation budget"):
        scorer_holdout.evaluate_holdout_reservation(
            object(),
            reservation_id=uuid4(),
            total_seconds=1,
            admitted_generation=1,
            execution_sha256="c" * 64,
        )


def test_early_holdout_process_exit_requires_exact_generation_drain_proof(monkeypatch) -> None:
    import contextlib

    from lab.scorer import holdout_supervisor

    reservation_id = uuid4()
    invocation_id = "a" * 32
    stdout = '{"reservation_id":"' + str(reservation_id) + '","state":"passed","bit":true}'

    class ExitedProcess:
        returncode = 0

        def __init__(self, *args, **kwargs):
            self.stdout = stdout
            self.stderr = ""

        def poll(self):
            return 0

        def communicate(self, timeout=None):
            return self.stdout, self.stderr

    monkeypatch.setattr(holdout_supervisor, "_ensure_aggregate_slice", lambda: None)
    monkeypatch.setattr(
        holdout_supervisor,
        "_job_lifecycle_lock",
        lambda _id, **_kwargs: contextlib.nullcontext(),
    )
    monkeypatch.setattr(
        holdout_supervisor,
        "_systemctl_show",
        lambda _unit: {
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "InvocationID": invocation_id,
            "MainPID": "0",
            "ControlGroup": "/user.slice/holdout.service",
        },
    )
    monkeypatch.setattr(holdout_supervisor, "_owned_unit_cgroup", lambda _unit, _props: "/owned")
    monkeypatch.setattr(holdout_supervisor, "_cgroup_is_empty", lambda _group: True)
    monkeypatch.setattr(holdout_supervisor.subprocess, "Popen", ExitedProcess)

    result = holdout_supervisor.run_holdout_process(
        reservation_id,
        admitted_generation=1,
        execution_sha256="c" * 64,
        remaining_seconds=30,
    )

    assert result.invocation_id == invocation_id
    assert result.exit_code == 0
    assert result.state == "passed"


def test_early_holdout_process_exit_without_generation_fails_closed(monkeypatch) -> None:
    import contextlib

    from lab.scorer import holdout_supervisor

    reservation_id = uuid4()

    class ExitedProcess:
        returncode = 0

        def __init__(self, *args, **kwargs):
            self.stdout = "{}"
            self.stderr = ""

        def poll(self):
            return 0

        def communicate(self, timeout=None):
            return self.stdout, self.stderr

    monkeypatch.setattr(holdout_supervisor, "_ensure_aggregate_slice", lambda: None)
    monkeypatch.setattr(
        holdout_supervisor,
        "_job_lifecycle_lock",
        lambda _id, **_kwargs: contextlib.nullcontext(),
    )
    monkeypatch.setattr(
        holdout_supervisor,
        "_systemctl_show",
        lambda _unit: {
            "LoadState": "not-found",
            "ActiveState": "inactive",
            "MainPID": "0",
        },
    )
    monkeypatch.setattr(holdout_supervisor.subprocess, "Popen", ExitedProcess)

    with pytest.raises(RuntimeError, match="generation id"):
        holdout_supervisor.run_holdout_process(
            reservation_id,
            admitted_generation=1,
            execution_sha256="c" * 64,
            remaining_seconds=30,
        )


def test_holdout_lifecycle_lock_wait_is_bounded_by_total_deadline(monkeypatch) -> None:
    from lab.scorer import holdout_supervisor

    observed: list[float] = []

    class DeadlineLock:
        def __enter__(self):
            raise TimeoutError("lock acquisition exceeds holdout deadline")

        def __exit__(self, *_args):
            return False

    def timed_lock(_reservation_id, *, timeout_seconds):
        observed.append(timeout_seconds)
        return DeadlineLock()

    monkeypatch.setattr(holdout_supervisor, "_ensure_aggregate_slice", lambda: None)
    monkeypatch.setattr(holdout_supervisor, "_job_lifecycle_lock", timed_lock)
    monkeypatch.setattr(
        holdout_supervisor.subprocess,
        "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )

    with pytest.raises(TimeoutError, match="holdout deadline"):
        holdout_supervisor.run_holdout_process(
            uuid4(),
            admitted_generation=1,
            execution_sha256="c" * 64,
            remaining_seconds=5,
        )
    assert observed and 0 < observed[0] <= 2


def test_completed_wrapper_still_requires_and_attempts_cgroup_drain(monkeypatch) -> None:
    import contextlib

    from lab.scorer import holdout_supervisor

    reservation_id = uuid4()
    invocation_id = "b" * 32
    stdout = '{"reservation_id":"' + str(reservation_id) + '","state":"passed","bit":true}'
    properties = iter(
        (
            {"LoadState": "not-found", "ActiveState": "inactive", "MainPID": "0"},
            {
                "LoadState": "loaded",
                "ActiveState": "active",
                "InvocationID": invocation_id,
                "MainPID": "23",
                "ControlGroup": "/user.slice/holdout.service",
            },
            {
                "LoadState": "loaded",
                "ActiveState": "inactive",
                "InvocationID": invocation_id,
                "MainPID": "0",
                "ControlGroup": "/user.slice/holdout.service",
            },
        )
    )

    class ExitedProcess:
        returncode = 0

        def __init__(self, *args, **kwargs):
            self.stdout = stdout
            self.stderr = ""

        def poll(self):
            return 0

        def communicate(self, timeout=None):
            return self.stdout, self.stderr

    drained: list[tuple[object, str]] = []
    monkeypatch.setattr(holdout_supervisor, "_ensure_aggregate_slice", lambda: None)
    monkeypatch.setattr(
        holdout_supervisor,
        "_job_lifecycle_lock",
        lambda _id, **_kwargs: contextlib.nullcontext(),
    )
    monkeypatch.setattr(holdout_supervisor, "_systemctl_show", lambda _unit: next(properties))
    monkeypatch.setattr(holdout_supervisor, "_owned_unit_cgroup", lambda _unit, _props: "/owned")
    monkeypatch.setattr(holdout_supervisor, "_cgroup_is_empty", lambda _group: False)
    monkeypatch.setattr(holdout_supervisor, "_cgroup_is_absent_or_empty", lambda _group: False)
    monkeypatch.setattr(holdout_supervisor.subprocess, "Popen", ExitedProcess)
    monkeypatch.setattr(
        holdout_supervisor,
        "stop_owned_scorer_unit",
        lambda received_id, *, invocation_id, timeout_seconds: (
            drained.append((received_id, invocation_id)) or True
        ),
    )

    with pytest.raises(RuntimeError, match="drain proof"):
        holdout_supervisor.run_holdout_process(
            reservation_id,
            admitted_generation=1,
            execution_sha256="c" * 64,
            remaining_seconds=30,
        )
    assert drained == [(reservation_id, invocation_id)]


def test_registered_holdout_identity_keeps_development_and_private_hashes_distinct() -> None:
    """The trusted registration accepts two independently pinned suite digests."""
    from lab.scorer.holdout import register_holdout_suite

    private_manifest_sha = "a" * 64
    with pytest.raises(ValueError, match="development suite manifest digest"):
        register_holdout_suite(
            object(),
            suite_id="fixture.suite.v1",
            suite_version=1,
            manifest_sha256=private_manifest_sha,
            development_manifest_sha256="not-a-digest",
            epsilon=0.01,
            tasks=(),
        )
    with pytest.raises(ValueError, match="holdout suite must contain"):
        register_holdout_suite(
            object(),
            suite_id="fixture.suite.v1",
            suite_version=1,
            manifest_sha256=private_manifest_sha,
            development_manifest_sha256="b" * 64,
            epsilon=0.01,
            tasks=(),
        )


def test_api_request_uses_suite_key_and_no_caller_selected_version(tmp_path) -> None:
    """Production API identity is the suite key plus the registry digest."""
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(mode=0o700)
    manifest = b'{"schema":"director-suite.v1","suite_id":"synthetic.holdout.v1"}'
    scenario = b'{"schema":"review-fake-provider-input.v1","items":[]}'
    (runtime_root / "suite.json").write_bytes(manifest)
    (runtime_root / "scenario.json").write_bytes(scenario)
    (runtime_root / "suite.json").chmod(0o600)
    (runtime_root / "scenario.json").chmod(0o600)
    entry = SuiteEntry(
        suite_id="synthetic.holdout.v1",
        track="anomaly",
        program_version="fixture-only",
        suite_manifest_path="suite.json",
        suite_manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        provider="fake-json",
        scenario_path="scenario.json",
        scenario_sha256=hashlib.sha256(scenario).hexdigest(),
        proposal_limit=1,
    )
    registry = SuiteRegistry((entry,), runtime_root)
    from sqlalchemy.pool import StaticPool

    raw = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    engine = raw.execution_options(schema_translate_map={"lab": None, "scorer": None})
    metadata.create_all(engine)
    token = "u" * 32
    app = create_app(
        director_engine=engine,
        director_token=token,
        suite_registry=registry,
    )
    request = {
        "idempotency_key": "holdout-identity-fixture-001",
        "track": "anomaly",
        "suite": entry.suite_id,
        "budget": {"experiments": 1, "wall_seconds": 120, "model_tokens": 500},
        "program_version": entry.program_version,
    }
    with TestClient(app) as client:
        response = client.post(
            "/v1/runs",
            headers={"Authorization": f"Bearer {token}"},
            json=request,
        )
    assert response.status_code == 202
    run_id = UUID(response.json()["run_id"])
    with engine.connect() as connection:
        stored = connection.execute(
            select(runs.c.request_json).where(runs.c.run_id == run_id)
        ).scalar_one()
    assert stored["suite"] == entry.suite_id
    assert stored["suite_manifest_sha256"] == entry.suite_manifest_sha256
    assert "suite_id" not in stored
    assert "suite_version" not in stored
    raw.dispose()


def test_evt_holdout_accepts_masked_clock_reset_and_rejects_label_leak() -> None:
    times = [0, 60, 0, 60]
    masks = [False, False, True, False]
    semantics = {
        "task_family": "EVT",
        "sampling_s": 60,
        "evaluation_times_json": times,
        "masked_samples_json": masks,
        "failure_windows_json": [],
        "semantics_sha256": _semantics_digest(
            family="EVT",
            sampling_s=60,
            times=times,
            masks=masks,
            failures=[],
        ),
    }

    result = _validate_semantics(
        semantics,
        task_family="EVT",
        sample_count=4,
        sampling_s=60,
        labels=np.asarray([False, False, False, False]),
    )
    assert result["masks"] == masks

    with pytest.raises(ValueError, match="positive labels"):
        _validate_semantics(
            semantics,
            task_family="EVT",
            sample_count=4,
            sampling_s=60,
            labels=np.asarray([False, False, True, False]),
        )


@pytest.mark.parametrize("family", ["PDM", "NRM"])
def test_temporal_holdout_metric_uses_registered_alarm_policy_and_masks(family: str) -> None:
    labels = np.asarray([False, False, True, True, False, False], dtype=np.bool_)
    scores = [0.1, 0.9, 0.8, 0.2, 0.1, 0.1]
    semantics = {
        "masked_samples_json": [False, False, False, True, False, False],
        "failure_windows_json": [[2, 3]] if family == "PDM" else [],
    }
    score, metrics = _task_metric(
        family=family,
        labels=labels,
        scores=scores,
        policy=AlarmPolicy(threshold=0.7, release=0.3, dwell=1),
        semantics=semantics,
        sliding_window=2,
        sampling_s=60,
    )
    assert 0 <= score <= 1
    assert set(metrics) == {"fa_per_day", "duty_fraction"}
    with pytest.raises(ValueError, match="verified sampling seconds"):
        _task_metric(
            family=family,
            labels=labels,
            scores=scores,
            policy=AlarmPolicy(threshold=0.7, release=0.3, dwell=1),
            semantics=semantics,
            sliding_window=2,
            sampling_s=None,
        )
