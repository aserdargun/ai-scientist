"""Automatic stop lifecycle: generation/deadline fences and normal queue behavior."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from subprocess import CompletedProcess
from unittest.mock import Mock
from uuid import uuid4

import pytest

from lab import cli
from lab.director.ownership import ExecutionOwner


@pytest.fixture(autouse=True)
def slice_limits(monkeypatch):
    """Automatic-stop unit tests never touch the actual shared systemd slice."""
    check = Mock()
    monkeypatch.setattr(cli.SystemdUnitManager, "ensure_slice", check)
    return check


class Result:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def one_or_none(self):
        return self.row

    def scalar_one(self):
        return self.row


class Engine:
    def __init__(self, row):
        self.row, self.disposed = row, False

    @contextmanager
    def connect(self):
        yield self

    def execute(self, *args, **kwargs):
        return Result(self.row)

    def dispose(self):
        self.disposed = True


def setup_recovery(monkeypatch, *, closure_seconds=None, original_seconds=60):
    run = uuid4()
    row = dict(
        state="stop_requested",
        request_json={"purpose": "baseline"},
        generation=2,
        worker_invocation_id="a" * 32,
        execution_sha256="b" * 64,
        deadline_at=datetime.now(UTC) + timedelta(seconds=original_seconds),
        stop_cleanup_deadline=None
        if closure_seconds is None
        else datetime.now(UTC) + timedelta(seconds=closure_seconds),
        requested_stop_deadline=datetime.now(UTC) + timedelta(seconds=120),
    )
    director, planner = Engine(row), Engine(None)
    monkeypatch.setattr(cli, "_director_engine", lambda: director)
    monkeypatch.setattr(cli, "_planner_engine", lambda: planner)
    monkeypatch.setattr(cli, "_with_director_global_slot", lambda _run, operation: operation())
    monkeypatch.setattr(cli, "_private_runtime_directory", lambda path: path)
    apply = Mock(
        return_value={
            "recovery_state": "completed",
            "action": "finalized_stopped",
            "report_sha256": "c" * 64,
        }
    )
    monkeypatch.setattr(cli, "apply_stop_and_finalize", apply)
    return (
        run,
        row,
        director,
        planner,
        apply,
        dict(generation=2, invocation_id="a" * 32, execution_sha256="b" * 64),
    )


def test_one_normal_attempt_binds_exact_owner_and_original_deadline(monkeypatch):
    run, row, director, planner, apply, owner = setup_recovery(monkeypatch, closure_seconds=20)
    result = cli._automatic_stopped_baseline_recovery(run, owner)
    assert result["state"] == "stopped" and result["report_sha256"] == "c" * 64
    assert apply.call_count == 1
    kwargs = apply.call_args.kwargs
    assert kwargs["expected_owner"] == ExecutionOwner(run, 2, "a" * 32, "b" * 64)
    assert 0 < kwargs["remaining_seconds"] <= 20
    assert kwargs["reconcile_interrupted_baseline"] is True
    assert director.disposed and planner.disposed


def test_registered_proposal_uses_proposal_recovery_with_same_owner_and_deadline(monkeypatch):
    run, row, _, _, apply, owner = setup_recovery(monkeypatch, closure_seconds=20)
    row["proposal_stop"] = True
    result = cli._automatic_stopped_baseline_recovery(run, owner)
    assert result["state"] == "stopped"
    kwargs = apply.call_args.kwargs
    assert kwargs["reconcile_interrupted_proposal"] is True
    assert kwargs["reconcile_interrupted_baseline"] is False
    assert kwargs["expected_owner"] == ExecutionOwner(run, 2, "a" * 32, "b" * 64)
    assert kwargs["cleanup_deadline"] == row["stop_cleanup_deadline"]
    assert 0 < kwargs["remaining_seconds"] <= 20


@pytest.mark.parametrize(
    "field,value", [("generation", 3), ("invocation_id", "d" * 32), ("execution_sha256", "e" * 64)]
)
def test_replaced_generation_never_enters_recovery(monkeypatch, field, value):
    run, row, director, planner, apply, owner = setup_recovery(monkeypatch)
    owner[field] = value
    result = cli._automatic_stopped_baseline_recovery(run, owner)
    assert (
        result["state"] == "stop_requested"
        and result["recovery_reason"] == "stop_generation_changed"
    )
    apply.assert_not_called()


@pytest.mark.parametrize("original_seconds,closure_seconds", [(-1, None), (60, -1)])
def test_expired_original_or_existing_cleanup_deadline_is_not_renewed(
    monkeypatch, original_seconds, closure_seconds
):
    run, row, director, planner, apply, owner = setup_recovery(
        monkeypatch, original_seconds=original_seconds, closure_seconds=closure_seconds
    )
    assert cli._automatic_stopped_baseline_recovery(run, owner)["state"] == "stop_requested"
    apply.assert_not_called()


def test_normal_recovery_pending_is_not_reported_stopped(monkeypatch):
    run, row, director, planner, apply, owner = setup_recovery(monkeypatch)
    apply.return_value = {
        "recovery_state": "pending",
        "action": "pending",
        "reason": "owner_generation_active",
    }
    result = cli._automatic_stopped_baseline_recovery(run, owner)
    assert result["state"] == "stop_requested" and result["dispatch"] == "recovery_pending"
    assert "report_sha256" not in result and apply.call_count == 1


def test_direct_recovery_occurs_after_waiter_and_preserves_original_exit(monkeypatch):
    run = uuid4()
    events = []
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *a, **k: (
            events.append("waiter"),
            CompletedProcess(
                [],
                1,
                stdout='{"run_id":"'
                + str(run)
                + '","state":"stop_requested","admitted_owner":{"generation":1}}\n',
            ),
        )[1],
    )

    def recover(requested, owner):
        assert events == ["waiter"] and requested == run and owner == {"generation": 1}
        events.append("recovery")
        return {"run_id": str(run), "state": "stopped", "automatic_stop_recovery": "completed"}

    monkeypatch.setattr(cli, "_automatic_stopped_baseline_recovery", recover)
    result = cli._launch_director_unit(run, unit_runtime=60)
    assert (
        events == ["waiter", "recovery"]
        and result["state"] == "stopped"
        and result["owner_exit_code"] == 1
    )


def test_shared_drain_retires_itself_only_after_stopped_callback_returns(monkeypatch):
    run = uuid4()
    events = []
    engine = Engine(None)
    monkeypatch.setattr(cli, "_director_engine", lambda: engine)
    monkeypatch.setattr(
        cli, "_recover_stopped_baselines_at_drain_start", lambda e: events.append("startup")
    )
    monkeypatch.setattr(cli, "_next_queued_run", lambda e: run)

    def dispatch(requested):
        events.append("callback_released")
        return {"run_id": str(requested), "state": "stop_requested"}

    monkeypatch.setattr(cli, "_dispatch_director_run_owned", dispatch)
    assert cli._drain_director_queue(poll_seconds=2) == 75
    assert events == ["startup", "callback_released"] and engine.disposed


@pytest.mark.parametrize("state", ["failed", "capacity_busy", "completed"])
def test_other_queue_results_keep_normal_polling(monkeypatch, state):
    run = uuid4()
    engine = Engine(None)
    calls = []
    monkeypatch.setattr(cli, "_director_engine", lambda: engine)
    monkeypatch.setattr(cli, "_recover_stopped_baselines_at_drain_start", lambda e: None)

    def next_run(_):
        calls.append(True)
        if len(calls) > 1:
            raise KeyboardInterrupt
        return run

    monkeypatch.setattr(cli, "_next_queued_run", next_run)
    monkeypatch.setattr(
        cli, "_dispatch_director_run_owned", lambda r: {"run_id": str(r), "state": state}
    )
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    assert cli._drain_director_queue(poll_seconds=2) == 0 and len(calls) == 2


def test_first_api_stop_deadline_clamps_when_no_closure_has_started(monkeypatch):
    run, row, director, planner, apply, owner = setup_recovery(monkeypatch)
    row["requested_stop_deadline"] = datetime.now(UTC) + timedelta(seconds=9)
    cli._automatic_stopped_baseline_recovery(run, owner)
    assert 0 < apply.call_args.kwargs["remaining_seconds"] <= 9
    assert apply.call_args.kwargs["cleanup_deadline"] == row["requested_stop_deadline"]


def test_expired_first_api_stop_is_not_given_fresh_closure_window(monkeypatch):
    run, row, director, planner, apply, owner = setup_recovery(monkeypatch)
    row["requested_stop_deadline"] = datetime.now(UTC) - timedelta(seconds=1)
    assert cli._automatic_stopped_baseline_recovery(run, owner)["state"] == "stop_requested"
    apply.assert_not_called()


def test_startup_filters_expired_original_and_stop_windows_before_limit(monkeypatch):
    # Regression guard: expired old requests cannot occupy the bounded snapshot
    # and hide a newly stopped run. Predicates must execute in SQL before LIMIT.
    captured = []

    class Rows:
        def mappings(self):
            return self

        def all(self):
            return []

    class SnapshotEngine(Engine):
        def execute(self, query, *args, **kwargs):
            captured.append(str(query))
            return Rows()

    cli._recover_stopped_baselines_at_drain_start(SnapshotEngine(None))
    sql = captured[0]
    for predicate in (
        "e.deadline_at>clock_timestamp()",
        "sc.created_at+interval '120 seconds'>clock_timestamp()",
        "event_type='run.stop_requested')>clock_timestamp()",
    ):
        assert predicate in sql and sql.index(predicate) < sql.index("LIMIT 16")


def test_recovery_rechecks_expected_generation_before_durable_intent(monkeypatch):
    from lab.director import recovery

    run = uuid4()
    payload = "f" * 64

    class OrderedEngine(Engine):
        def __init__(self):
            super().__init__(None)
            self.reads = 0

        def execute(self, *args, **kwargs):
            self.reads += 1
            return Result(
                {
                    "state": "stop_requested",
                    "payload_sha256": payload,
                    "report_sha256": None,
                    "request_json": {},
                }
                if self.reads == 1
                else {
                    "generation": 2,
                    "worker_invocation_id": "a" * 32,
                    "execution_sha256": "b" * 64,
                }
            )

    old = recovery.OwnerGeneration(payload, 42, 100, "boot", "unit", "a" * 32, "/group")
    monkeypatch.setattr(recovery, "_current_owner_row", lambda *_: old)
    monkeypatch.setattr(recovery, "_stored_owner", lambda owner: owner)
    intent = Mock()
    monkeypatch.setattr(recovery, "_ensure_recovery_intent", intent)
    recovery_id = recovery._recovery_id(run, recovery._request_sha256(run, "stop_and_finalize"))
    with pytest.raises(recovery.RecoveryPending, match="generation changed"):
        recovery.apply_stop_and_finalize(
            OrderedEngine(),
            Engine(None),
            run_id=run,
            recovery_id=recovery_id,
            expected_owner=ExecutionOwner(run, 1, "a" * 32, "b" * 64),
        )
    intent.assert_not_called()


class StopRaceResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalar_one(self):
        return self.value


class StopRaceEngine:
    """Model the API stop and the existing running-only closure RPC fence."""

    def __init__(self, run, owner, *, stopped=False, race_at=None, stale=False, inactive=False):
        self.run = run
        self.owner = owner
        self.stopped = stopped
        self.race_at = race_at
        self.stale = stale
        self.inactive = inactive
        self.closures = []
        self.observations = 0

    @contextmanager
    def connect(self):
        yield self

    @contextmanager
    def begin(self):
        yield self

    def execute(self, query, parameters):
        if str(query).startswith("SELECT 1"):
            self.observations += 1
            assert parameters == {
                "run_id": self.run,
                "generation": self.owner.generation,
                "invocation_id": self.owner.invocation_id,
                "execution_sha256": self.owner.execution_sha256,
            }
            assert "c.mode='active'" in str(query)
            assert "e.execution_sha256=:execution_sha256" in str(query)
            return StopRaceResult(
                1 if self.stopped and not self.stale and not self.inactive else None
            )
        target = parameters["target_state"]
        self.closures.append(target)
        if self.race_at == len(self.closures):
            self.stopped = True
        if self.stopped or self.stale or self.inactive:
            raise RuntimeError("existing running-only closure fence")
        if target == "failed":
            raise RuntimeError("existing incomplete-ledger terminal fence")
        self.stopped = True
        return StopRaceResult({"state": "stop_requested"})


def stop_race_owner():
    run = uuid4()
    return run, ExecutionOwner(run, 2, "a" * 32, "b" * 64)


def test_existing_exact_api_stop_returns_without_failure_mutation():
    run, owner = stop_race_owner()
    engine = StopRaceEngine(run, owner, stopped=True)
    result = cli._record_claimed_dispatch_failure(engine, run, RuntimeError(), owner=owner)
    assert result["state"] == "stop_requested" and result["dispatch"] == "recovery_required"
    assert engine.closures == [] and engine.observations == 1


@pytest.mark.parametrize(
    "race_at, expected_closures", [(1, ["failed"]), (2, ["failed", "stop_requested"])]
)
def test_api_stop_race_preserves_stop_when_running_only_close_rejects(race_at, expected_closures):
    run, owner = stop_race_owner()
    engine = StopRaceEngine(run, owner, race_at=race_at)
    result = cli._record_claimed_dispatch_failure(engine, run, RuntimeError(), owner=owner)
    assert result["state"] == "stop_requested" and result["dispatch"] == "recovery_required"
    assert engine.closures == expected_closures


@pytest.mark.parametrize("stale,inactive", [(True, False), (False, True)])
def test_changed_or_inactive_owner_cannot_use_already_stopped_fallback(stale, inactive):
    run, owner = stop_race_owner()
    engine = StopRaceEngine(run, owner, stopped=True, stale=stale, inactive=inactive)
    with pytest.raises(RuntimeError, match="running-only"):
        cli._record_claimed_dispatch_failure(engine, run, RuntimeError(), owner=owner)


def test_old_incomplete_ledger_still_requests_stop_through_guarded_rpc():
    run, owner = stop_race_owner()
    engine = StopRaceEngine(run, owner)
    result = cli._record_claimed_dispatch_failure(engine, run, RuntimeError(), owner=owner)
    assert result["state"] == "stop_requested"
    assert engine.closures == ["failed", "stop_requested"]


def test_actual_api_stop_failure_result_reaches_normal_drain_retirement(monkeypatch):
    run, owner = stop_race_owner()
    engine = StopRaceEngine(run, owner, stopped=True)
    monkeypatch.setattr(cli, "_director_engine", lambda: Engine(None))
    monkeypatch.setattr(cli, "_recover_stopped_baselines_at_drain_start", lambda _: None)
    monkeypatch.setattr(cli, "_next_queued_run", lambda _: run)
    monkeypatch.setattr(
        cli,
        "_dispatch_director_run_owned",
        lambda requested: cli._record_claimed_dispatch_failure(
            engine, requested, RuntimeError(), owner=owner
        ),
    )
    assert cli._drain_director_queue(poll_seconds=2) == 75
    assert engine.closures == []
