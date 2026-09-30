"""CPU-only holdout recovery and generation-admission contract tests."""

from __future__ import annotations

import contextlib
import json
import threading
from typing import Any
from uuid import UUID, uuid4

import pandas as pd
import pytest

from harness.contracts import FitContext
from lab.sandbox.evaluation import run_candidate_fit_score
from lab.scorer import holdout
from lab.scorer.holdout_supervisor import _parse_recovery_status

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
RESERVATION_ID = UUID("22222222-2222-4222-8222-222222222222")
WORKER_UNIT = f"swapp-ai-scientist-scorer-{RESERVATION_ID.hex}.service"
WORKER_GROUP = f"/user.slice/swapp-ai-scientist-scorer.slice/{WORKER_UNIT}"
ADMITTED_GENERATION = 1
EXECUTION_SHA256 = "c" * 64


def _target(**changes: Any) -> dict[str, Any]:
    return {
        "reservation_id": str(RESERVATION_ID),
        "run_id": str(RUN_ID),
        "run_state": "running",
        "stop_requested": False,
        "state": "running",
        "worker_pid": 510,
        "worker_start_ticks": 9921,
        "worker_boot_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "worker_unit": WORKER_UNIT,
        "worker_invocation_id": "b" * 32,
        "worker_cgroup": WORKER_GROUP,
        "admitted_generation": ADMITTED_GENERATION,
        "execution_sha256": EXECUTION_SHA256,
        **changes,
    }


class _Scalar:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one(self) -> object:
        return self.value


class _Connection:
    def __init__(self, engine: _Engine) -> None:
        self.engine = engine

    def execute(self, statement: object, params: dict[str, object]) -> _Scalar:
        sql = str(statement)
        if "read_holdout_recovery_target" in sql:
            self.engine.target_reads += 1
            return _Scalar(dict(self.engine.target))
        if "recover_holdout_failure" in sql:
            self.engine.cas_calls += 1
            assert params["run_id"] == RUN_ID
            return _Scalar({"reservation_id": str(RESERVATION_ID), "state": "failed", "bit": None})
        if "recover_unclaimed_holdout_failure" in sql:
            self.engine.unclaimed_cas_calls += 1
            assert params["run_id"] == RUN_ID
            if self.engine.target["state"] != "reserved":
                raise RuntimeError("reservation is no longer reserved")
            self.engine.target["state"] = "failed"
            return _Scalar({"reservation_id": str(RESERVATION_ID), "state": "failed", "bit": None})
        if "list_holdout_recovery_targets" in sql:
            return _Scalar(self.engine.run_targets)
        if "claim_holdout_reservation" in sql:
            if self.engine.target["state"] != "reserved":
                raise RuntimeError("holdout reservation is not runnable")
            self.engine.target["state"] = "running"
            return _Scalar({"reservation_id": str(RESERVATION_ID)})
        if "check_holdout_admission" in sql:
            self.engine.admission_params = dict(params)
            return _Scalar(self.engine.admitted)
        raise AssertionError(f"unexpected SQL: {sql}")


class _Engine:
    def __init__(self, target: dict[str, Any] | None = None, *, admitted: bool = True) -> None:
        self.target = target or _target()
        self.target_reads = 0
        self.cas_calls = 0
        self.unclaimed_cas_calls = 0
        self.admitted = admitted
        self.admission_params: dict[str, object] | None = None
        self.run_targets: list[dict[str, object]] = []

    @contextlib.contextmanager
    def begin(self):
        yield _Connection(self)

    @contextlib.contextmanager
    def connect(self):
        yield _Connection(self)


def _noop_lock(*_args: object, **_kwargs: object):
    return contextlib.nullcontext()


def test_target_identity_binds_reservation_run_and_exact_worker(monkeypatch) -> None:
    monkeypatch.setattr(holdout, "_read_holdout_recovery_target", lambda *_: _target())
    identity = holdout._worker_identity_from_target(_target(), RESERVATION_ID)
    assert identity["worker_unit"] == WORKER_UNIT
    with pytest.raises(ValueError, match="generation is malformed"):
        holdout._worker_identity_from_target(
            _target(worker_unit=f"swapp-ai-scientist-scorer-{uuid4().hex}.service"),
            RESERVATION_ID,
        )


def test_live_generation_on_running_run_is_pending_and_never_stopped(monkeypatch) -> None:
    from lab.scorer import supervisor

    engine = _Engine()
    monkeypatch.setattr(supervisor, "_job_lifecycle_lock", _noop_lock)
    monkeypatch.setattr(
        supervisor,
        "_systemctl_show",
        lambda _unit: {
            "LoadState": "loaded",
            "ActiveState": "active",
            "InvocationID": "b" * 32,
            "ControlGroup": WORKER_GROUP,
            "MainPID": "510",
        },
    )
    monkeypatch.setattr(supervisor, "_owned_unit_cgroup", lambda _unit, _props: WORKER_GROUP)
    monkeypatch.setattr(holdout, "_process_has_holdout_identity", lambda _identity: True)
    monkeypatch.setattr(
        supervisor,
        "_stop_owned_scorer_unit_locked",
        lambda *_args, **_kwargs: pytest.fail("live run worker must not be stopped"),
    )

    result = holdout.recover_holdout_reservation(engine, RESERVATION_ID)

    assert result.state == "pending"
    assert engine.cas_calls == 0


def test_dead_exact_generation_is_failed_only_after_unit_and_cgroup_drain(monkeypatch) -> None:
    from lab.scorer import supervisor

    engine = _Engine()
    monkeypatch.setattr(supervisor, "_job_lifecycle_lock", _noop_lock)
    monkeypatch.setattr(
        supervisor,
        "_systemctl_show",
        lambda _unit: {
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "InvocationID": "b" * 32,
            "ControlGroup": WORKER_GROUP,
            "MainPID": "0",
        },
    )
    monkeypatch.setattr(supervisor, "_owned_unit_cgroup", lambda _unit, _props: WORKER_GROUP)
    monkeypatch.setattr(supervisor, "_cgroup_is_empty", lambda _group: True)
    monkeypatch.setattr(holdout, "_process_has_holdout_identity", lambda _identity: False)
    monkeypatch.setattr(holdout, "_cleanup_holdout_orphan_if_owned", lambda *_a, **_k: True)

    result = holdout.recover_holdout_reservation(engine, RESERVATION_ID)

    assert result.state == "failed"
    assert engine.cas_calls == 1


@pytest.mark.parametrize("cleanup_result", [False, RuntimeError("busy/unknown orphan")])
def test_cleanup_failure_keeps_running_receipt_pending_without_cas(
    monkeypatch, cleanup_result: object
) -> None:
    from lab.scorer import supervisor

    engine = _Engine()
    monkeypatch.setattr(supervisor, "_job_lifecycle_lock", _noop_lock)
    monkeypatch.setattr(
        supervisor,
        "_systemctl_show",
        lambda _unit: {
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "InvocationID": "b" * 32,
            "ControlGroup": WORKER_GROUP,
            "MainPID": "0",
        },
    )
    monkeypatch.setattr(supervisor, "_owned_unit_cgroup", lambda _unit, _props: WORKER_GROUP)
    monkeypatch.setattr(supervisor, "_cgroup_is_absent_or_empty", lambda _group: True)
    monkeypatch.setattr(holdout, "_process_has_holdout_identity", lambda _identity: False)

    def cleanup(*_args: object, **_kwargs: object) -> bool:
        if isinstance(cleanup_result, Exception):
            raise cleanup_result
        return cleanup_result

    monkeypatch.setattr(holdout, "_cleanup_holdout_orphan_if_owned", cleanup)
    result = holdout.recover_holdout_reservation(engine, RESERVATION_ID)

    assert result.state == "pending"
    assert engine.cas_calls == 0


def test_foreign_marker_replacement_after_p1_lock_is_not_reconciled(tmp_path, monkeypatch) -> None:
    import fcntl

    real_flock = fcntl.flock
    lock_path = tmp_path / "admission.lock"
    marker_path = tmp_path / "admission.intent"
    work_root = tmp_path / "work"
    work_root.mkdir(mode=0o700)
    resolved_work_root = work_root.resolve()
    reconciled: list[bool] = []

    class FakeRunner:
        admission_lock = lock_path
        admission_marker = marker_path
        work_root = resolved_work_root

        def __init__(self, **_kwargs: object) -> None:
            pass

        def _reconcile_owned_containers(self) -> None:
            reconciled.append(True)

    marker_path.write_text("{}", encoding="ascii")
    marker_path.chmod(0o600)
    replaced = False

    def replace_after_lock(descriptor: int, operation: int) -> None:
        nonlocal replaced
        real_flock(descriptor, operation)
        if operation & fcntl.LOCK_EX and not replaced:
            marker_path.write_text(
                json.dumps(
                    {
                        "owner_pid": 999,
                        "owner_start": "1",
                        "boot_id": "c" * 36,
                        "work_root": str(work_root.resolve()),
                        "owner_label": "lab.scorer/sandbox-owner",
                    }
                ),
                encoding="ascii",
            )
            marker_path.chmod(0o600)
            replaced = True

    monkeypatch.setattr(holdout, "LocalDockerRunner", FakeRunner)
    monkeypatch.setattr(fcntl, "flock", replace_after_lock)
    identity = holdout._worker_identity_from_target(_target(), RESERVATION_ID)

    result = holdout._cleanup_holdout_orphan_if_owned(identity, work_root=work_root)

    assert result is False
    assert replaced is True
    assert reconciled == []


def test_busy_p1_lock_refuses_orphan_reconciliation(tmp_path, monkeypatch) -> None:
    lock_path = tmp_path / "admission.lock"
    marker_path = tmp_path / "admission.intent"
    work_root = tmp_path / "work"
    work_root.mkdir(mode=0o700)
    work_root_path = work_root.resolve()
    reconciled: list[bool] = []

    class FakeRunner:
        admission_lock = lock_path
        admission_marker = marker_path
        work_root = work_root_path

        def __init__(self, **_kwargs: object) -> None:
            pass

        def _reconcile_owned_containers(self) -> None:
            reconciled.append(True)

    monkeypatch.setattr(holdout, "LocalDockerRunner", FakeRunner)
    monkeypatch.setattr(
        holdout.fcntl,
        "flock",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(BlockingIOError()),
    )
    identity = holdout._worker_identity_from_target(_target(), RESERVATION_ID)

    result = holdout._cleanup_holdout_orphan_if_owned(identity, work_root=work_root)

    assert result is False
    assert reconciled == []


def test_collected_exact_unit_after_authorized_stop_uses_cgroup_absence_proof(monkeypatch) -> None:
    from lab.scorer import supervisor

    engine = _Engine(_target(stop_requested=True))
    properties = iter(
        (
            {
                "LoadState": "loaded",
                "ActiveState": "active",
                "InvocationID": "b" * 32,
                "ControlGroup": WORKER_GROUP,
                "MainPID": "510",
            },
            {"LoadState": "not-found", "ActiveState": "inactive", "MainPID": "0"},
        )
    )
    stopped: list[UUID] = []
    process_states = iter((True, False))
    monkeypatch.setattr(supervisor, "_job_lifecycle_lock", _noop_lock)
    monkeypatch.setattr(supervisor, "_systemctl_show", lambda _unit: next(properties))
    monkeypatch.setattr(supervisor, "_owned_unit_cgroup", lambda _unit, _props: WORKER_GROUP)
    monkeypatch.setattr(supervisor, "_expected_unit_cgroup", lambda _unit: WORKER_GROUP)
    monkeypatch.setattr(supervisor, "_cgroup_is_absent_or_empty", lambda _group: True)
    monkeypatch.setattr(
        supervisor,
        "_stop_owned_scorer_unit_locked",
        lambda job_id, **_kwargs: stopped.append(job_id),
    )
    monkeypatch.setattr(
        holdout, "_process_has_holdout_identity", lambda _identity: next(process_states)
    )
    monkeypatch.setattr(holdout, "_cleanup_holdout_orphan_if_owned", lambda *_a, **_k: True)

    result = holdout.recover_holdout_reservation(engine, RESERVATION_ID)

    assert result.state == "failed"
    assert stopped == [RESERVATION_ID]
    assert engine.cas_calls == 1


def test_reserved_receipt_is_failed_only_after_launch_unit_is_absent(monkeypatch) -> None:
    from lab.scorer import supervisor

    engine = _Engine(
        _target(
            state="reserved",
            worker_pid=None,
            worker_start_ticks=None,
            worker_boot_id=None,
            worker_unit=None,
            worker_invocation_id=None,
            worker_cgroup=None,
        )
    )
    monkeypatch.setattr(supervisor, "_job_lifecycle_lock", _noop_lock)
    monkeypatch.setattr(
        supervisor,
        "_systemctl_show",
        lambda _unit: {"LoadState": "not-found", "ActiveState": "inactive", "MainPID": "0"},
    )
    monkeypatch.setattr(supervisor, "_expected_unit_cgroup", lambda _unit: WORKER_GROUP)
    monkeypatch.setattr(supervisor, "_cgroup_is_absent_or_empty", lambda _group: True)

    result = holdout.recover_holdout_reservation(engine, RESERVATION_ID)

    assert result.state == "failed"
    assert engine.unclaimed_cas_calls == 1
    assert engine.cas_calls == 0
    with pytest.raises(RuntimeError, match="not runnable"):
        with engine.begin() as connection:
            connection.execute(
                "SELECT lab.claim_holdout_reservation(:reservation_id)",
                {"reservation_id": RESERVATION_ID},
            )


@pytest.mark.parametrize(
    "properties",
    [
        {"LoadState": "loaded", "ActiveState": "active", "MainPID": "700"},
        {"LoadState": "loaded", "ActiveState": "inactive", "MainPID": "0"},
    ],
)
def test_reserved_receipt_stays_pending_for_any_loaded_generation(monkeypatch, properties) -> None:
    from lab.scorer import supervisor

    engine = _Engine(
        _target(
            state="reserved",
            worker_pid=None,
            worker_start_ticks=None,
            worker_boot_id=None,
            worker_unit=None,
            worker_invocation_id=None,
            worker_cgroup=None,
        )
    )
    monkeypatch.setattr(supervisor, "_job_lifecycle_lock", _noop_lock)
    monkeypatch.setattr(supervisor, "_systemctl_show", lambda _unit: properties)
    monkeypatch.setattr(supervisor, "_expected_unit_cgroup", lambda _unit: WORKER_GROUP)
    monkeypatch.setattr(supervisor, "_cgroup_is_absent_or_empty", lambda _group: True)

    result = holdout.recover_holdout_reservation(engine, RESERVATION_ID)

    assert result.state == "pending"
    assert engine.unclaimed_cas_calls == 0


def test_run_recovery_lists_only_open_targets_and_returns_drained(monkeypatch) -> None:
    engine = _Engine()
    engine.run_targets = []

    assert holdout.recover_holdout_run(engine, RUN_ID) == "drained"


def test_run_recovery_status_binds_run_and_has_no_reservation_identity() -> None:
    from lab.scorer.holdout_supervisor import _parse_recovery_status

    assert (
        _parse_recovery_status(
            json.dumps({"run_id": str(RUN_ID), "state": "drained"}), RUN_ID, target_kind="run"
        )
        == "drained"
    )
    with pytest.raises(RuntimeError):
        _parse_recovery_status(
            json.dumps({"run_id": str(RUN_ID), "state": "drained", "bit": True}),
            RUN_ID,
            target_kind="run",
        )


def test_reserved_launch_race_serializes_recovery_before_late_claim(monkeypatch) -> None:
    from lab.scorer import supervisor

    engine = _Engine(
        _target(
            state="reserved",
            worker_pid=None,
            worker_start_ticks=None,
            worker_boot_id=None,
            worker_unit=None,
            worker_invocation_id=None,
            worker_cgroup=None,
        )
    )
    lifecycle = threading.Lock()
    inspect_started = threading.Event()
    allow_inspect = threading.Event()
    launch_attempted = threading.Event()
    claim_outcome: list[str] = []

    @contextlib.contextmanager
    def shared_lock(*_args: object, **_kwargs: object):
        lifecycle.acquire()
        try:
            yield
        finally:
            lifecycle.release()

    def show(_unit: str) -> dict[str, str]:
        inspect_started.set()
        assert allow_inspect.wait(timeout=2)
        return {"LoadState": "not-found", "ActiveState": "inactive", "MainPID": "0"}

    monkeypatch.setattr(supervisor, "_job_lifecycle_lock", shared_lock)
    monkeypatch.setattr(supervisor, "_systemctl_show", show)
    monkeypatch.setattr(supervisor, "_expected_unit_cgroup", lambda _unit: WORKER_GROUP)
    monkeypatch.setattr(supervisor, "_cgroup_is_absent_or_empty", lambda _group: True)

    recovery = threading.Thread(
        target=lambda: holdout.recover_holdout_reservation(engine, RESERVATION_ID), daemon=True
    )
    recovery.start()
    assert inspect_started.wait(timeout=2)

    def late_launcher() -> None:
        launch_attempted.set()
        with shared_lock(RESERVATION_ID):
            try:
                with engine.begin() as connection:
                    connection.execute(
                        "SELECT lab.claim_holdout_reservation(:reservation_id)",
                        {"reservation_id": RESERVATION_ID},
                    )
            except RuntimeError:
                claim_outcome.append("rejected")
            else:
                claim_outcome.append("claimed")

    launcher = threading.Thread(target=late_launcher, daemon=True)
    launcher.start()
    assert launch_attempted.wait(timeout=2)
    allow_inspect.set()
    recovery.join(timeout=2)
    launcher.join(timeout=2)

    assert not recovery.is_alive()
    assert not launcher.is_alive()
    assert engine.target["state"] == "failed"
    assert claim_outcome == ["rejected"]


def test_owner_stop_request_authorizes_only_exact_generation_stop(monkeypatch) -> None:
    from lab.scorer import supervisor

    engine = _Engine(_target(stop_requested=True))
    unit_states = iter(
        (
            {
                "LoadState": "loaded",
                "ActiveState": "active",
                "InvocationID": "b" * 32,
                "ControlGroup": WORKER_GROUP,
                "MainPID": "510",
            },
            {
                "LoadState": "loaded",
                "ActiveState": "inactive",
                "InvocationID": "b" * 32,
                "ControlGroup": WORKER_GROUP,
                "MainPID": "0",
            },
        )
    )
    stopped: list[tuple[object, ...]] = []
    process_states = iter((True, False))
    monkeypatch.setattr(supervisor, "_job_lifecycle_lock", _noop_lock)
    monkeypatch.setattr(supervisor, "_systemctl_show", lambda _unit: next(unit_states))
    monkeypatch.setattr(supervisor, "_owned_unit_cgroup", lambda _unit, _props: WORKER_GROUP)
    monkeypatch.setattr(supervisor, "_cgroup_is_empty", lambda _group: True)
    monkeypatch.setattr(
        supervisor,
        "_stop_owned_scorer_unit_locked",
        lambda *args, **kwargs: stopped.append((*args, kwargs)),
    )
    monkeypatch.setattr(
        holdout, "_process_has_holdout_identity", lambda _identity: next(process_states)
    )
    monkeypatch.setattr(holdout, "_cleanup_holdout_orphan_if_owned", lambda *_a, **_k: True)

    result = holdout.recover_holdout_reservation(engine, RESERVATION_ID)

    assert result.state == "failed"
    assert len(stopped) == 1
    assert stopped[0][0] == RESERVATION_ID
    assert stopped[0][1]["invocation_id"] == "b" * 32
    assert engine.target_reads >= 3  # fresh locked authorization immediately preceded stop
    assert engine.cas_calls == 1


def test_new_or_foreign_systemd_generation_is_left_untouched(monkeypatch) -> None:
    from lab.scorer import supervisor

    engine = _Engine()
    monkeypatch.setattr(supervisor, "_job_lifecycle_lock", _noop_lock)
    monkeypatch.setattr(
        supervisor,
        "_systemctl_show",
        lambda _unit: {
            "LoadState": "loaded",
            "ActiveState": "active",
            "InvocationID": "c" * 32,
            "ControlGroup": WORKER_GROUP,
            "MainPID": "510",
        },
    )
    monkeypatch.setattr(supervisor, "_owned_unit_cgroup", lambda _unit, _props: WORKER_GROUP)
    monkeypatch.setattr(holdout, "_process_has_holdout_identity", lambda _identity: True)

    result = holdout.recover_holdout_reservation(engine, RESERVATION_ID)

    assert result.state == "pending"
    assert engine.cas_calls == 0


def test_admission_rpc_binds_run_and_worker_identity() -> None:
    engine = _Engine(admitted=True)
    identity = holdout._worker_identity_from_target(_target(), RESERVATION_ID)

    holdout._assert_holdout_admission(
        engine,
        RESERVATION_ID,
        RUN_ID,
        identity,
        admitted_generation=ADMITTED_GENERATION,
        execution_sha256=EXECUTION_SHA256,
    )

    assert engine.admission_params is not None
    assert engine.admission_params["run_id"] == RUN_ID
    assert engine.admission_params["worker_invocation_id"] == "b" * 32


def test_admission_rejection_prevents_next_fit_score_phase() -> None:
    engine = _Engine(admitted=False)
    identity = holdout._worker_identity_from_target(_target(), RESERVATION_ID)

    with pytest.raises(RuntimeError, match="no longer admitted"):
        holdout._assert_holdout_admission(
            engine,
            RESERVATION_ID,
            RUN_ID,
            identity,
            admitted_generation=ADMITTED_GENERATION,
            execution_sha256=EXECUTION_SHA256,
        )


def test_rejected_admission_stops_before_docker_runner_phase() -> None:
    class NeverRunDocker:
        called = False

        def run_phase(self, **_kwargs: object) -> object:
            self.called = True
            raise AssertionError("revoked holdout work must not reach Docker")

    runner = NeverRunDocker()
    frame = pd.DataFrame({"sensor": [0.0, 1.0, 2.0]})
    context = FitContext(
        seed=1,
        signals=("sensor",),
        regime_signals=(),
        sampling_s=1,
        time_budget_s=10.0,
    )

    with pytest.raises(RuntimeError, match="reservation generation changed"):
        run_candidate_fit_score(
            runner,  # type: ignore[arg-type]
            candidate_source=b"source",
            train=frame,
            evaluation=frame,
            context=context,
            admission_check=lambda: (_ for _ in ()).throw(
                RuntimeError("reservation generation changed")
            ),
        )
    assert runner.called is False


@pytest.mark.parametrize("state", ["pending", "passed", "reverted", "failed", "exhausted"])
def test_recovery_supervisor_accepts_only_identity_and_operational_state(state: str) -> None:
    payload = json.dumps({"reservation_id": str(RESERVATION_ID), "state": state})
    assert _parse_recovery_status(payload, RESERVATION_ID) == state
    with pytest.raises(RuntimeError):
        _parse_recovery_status(
            json.dumps({"reservation_id": str(RESERVATION_ID), "state": state, "bit": True}),
            RESERVATION_ID,
        )


def test_recovery_supervisor_uses_distinct_unit_generation(monkeypatch) -> None:
    from lab.scorer import holdout_supervisor

    recovery_id = UUID("33333333-3333-4333-8333-333333333333")
    recovery_unit = f"swapp-ai-scientist-scorer-{recovery_id.hex}.service"
    observed_units: list[str] = []

    class _Process:
        returncode = 0

        def poll(self) -> None:
            return None

        def communicate(self, timeout: float) -> tuple[str, str]:
            assert timeout > 0
            return (
                json.dumps({"reservation_id": str(RESERVATION_ID), "state": "pending"}),
                "",
            )

    def show(unit: str) -> dict[str, str]:
        observed_units.append(unit)
        if unit == recovery_unit and len(observed_units) == 1:
            return {"LoadState": "not-found"}
        if unit == recovery_unit and len(observed_units) == 2:
            return {
                "LoadState": "loaded",
                "ActiveState": "active",
                "InvocationID": "d" * 32,
                "ControlGroup": f"/user.slice/swapp-ai-scientist-scorer.slice/{recovery_unit}",
                "MainPID": "901",
            }
        return {
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "InvocationID": "d" * 32,
            "ControlGroup": f"/user.slice/swapp-ai-scientist-scorer.slice/{recovery_unit}",
            "MainPID": "0",
        }

    monkeypatch.setattr(holdout_supervisor, "uuid4", lambda: recovery_id)
    monkeypatch.setattr(holdout_supervisor, "_ensure_aggregate_slice", lambda: None)
    monkeypatch.setattr(holdout_supervisor, "_job_lifecycle_lock", _noop_lock)
    monkeypatch.setattr(holdout_supervisor, "_systemctl_show", show)
    monkeypatch.setattr(
        holdout_supervisor,
        "_owned_unit_cgroup",
        lambda unit, _props: f"/user.slice/swapp-ai-scientist-scorer.slice/{unit}",
    )
    monkeypatch.setattr(holdout_supervisor, "_cgroup_is_empty", lambda _group: True)
    monkeypatch.setattr(holdout_supervisor.subprocess, "Popen", lambda *_a, **_k: _Process())

    result = holdout_supervisor.run_holdout_recovery_process(RESERVATION_ID)

    assert result.state == "pending"
    assert result.unit == recovery_unit
    assert recovery_unit != WORKER_UNIT
    assert observed_units == [recovery_unit, recovery_unit, recovery_unit]


def test_stop_cleanup_supervisor_passes_only_run_id_to_distinct_unit(monkeypatch) -> None:
    from lab.scorer import holdout_supervisor

    recovery_id = UUID("44444444-4444-4444-8444-444444444444")
    recovery_unit = f"swapp-ai-scientist-scorer-{recovery_id.hex}.service"
    commands: list[list[str]] = []

    class _Process:
        returncode = 0

        def poll(self) -> None:
            return None

        def communicate(self, timeout: float) -> tuple[str, str]:
            assert timeout > 0
            return json.dumps({"run_id": str(RUN_ID), "state": "drained"}), ""

        def kill(self) -> None:
            raise AssertionError("completed stop cleanup should not be killed")

    properties = iter(
        (
            {"LoadState": "not-found"},
            {
                "LoadState": "loaded",
                "ActiveState": "active",
                "InvocationID": "e" * 32,
                "ControlGroup": f"/user.slice/swapp-ai-scientist-scorer.slice/{recovery_unit}",
                "MainPID": "700",
            },
            {
                "LoadState": "loaded",
                "ActiveState": "inactive",
                "InvocationID": "e" * 32,
                "ControlGroup": f"/user.slice/swapp-ai-scientist-scorer.slice/{recovery_unit}",
                "MainPID": "0",
            },
        )
    )

    def popen(command: list[str], **_kwargs: object) -> _Process:
        commands.append(command)
        return _Process()

    monkeypatch.setattr(holdout_supervisor, "uuid4", lambda: recovery_id)
    monkeypatch.setattr(holdout_supervisor, "_ensure_aggregate_slice", lambda: None)
    monkeypatch.setattr(holdout_supervisor, "_job_lifecycle_lock", _noop_lock)
    monkeypatch.setattr(holdout_supervisor, "_systemctl_show", lambda _unit: next(properties))
    monkeypatch.setattr(
        holdout_supervisor,
        "_owned_unit_cgroup",
        lambda unit, _props: f"/user.slice/swapp-ai-scientist-scorer.slice/{unit}",
    )
    monkeypatch.setattr(holdout_supervisor, "_cgroup_is_empty", lambda _group: True)
    monkeypatch.setattr(holdout_supervisor.subprocess, "Popen", popen)

    result = holdout_supervisor.run_holdout_run_recovery_process(RUN_ID)

    assert result.run_id == RUN_ID
    assert result.reservation_id is None
    assert result.state == "drained"
    assert f"--unit={recovery_unit}" in commands[0]
    assert "--run-id" in commands[0]
    assert str(RUN_ID) in commands[0]
    assert "--reservation-id" not in commands[0]
