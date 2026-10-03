"""Inert zero-job stop proofs; no native process, service or database acceptance."""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace
from uuid import uuid4

import pytest

from lab.director.recovery import OwnerGeneration
from lab.scorer import recovery_identity, stop_recovery
from lab.scorer.worker import ScorerInvocation


class Result:
    def __init__(self, value):
        self.value = value

    def mappings(self):
        return self

    def scalars(self):
        return self

    def one(self):
        return self.value

    def all(self):
        return self.value

    def scalar_one(self):
        return self.value


class EmptyStop:
    def __init__(self):
        self.run, self.stop = uuid4(), uuid4()
        unit = f"swapp-ai-scientist-director-dispatch-{self.run.hex}.service"
        self.owner = OwnerGeneration(
            "a" * 64, 101, 900, str(uuid4()), unit, "b" * 32, "/user.slice/" + unit
        )
        self.request = dict(
            run_id=self.run,
            remaining=60,
            expected_generation=1,
            execution_sha256="c" * 64,
            owner_json=asdict(self.owner),
            proposal_closure=False,
        )
        self.context = dict(
            schema="empty-baseline-stop-context.v1",
            run_id=str(self.run),
            recovery_id=str(self.stop),
            expected_generation=1,
            execution_sha256="c" * 64,
            owner_json=asdict(self.owner),
            original_plan=[{"task_id": "fixture", "seed": 0}],
        )
        self.worker = dict(
            worker_pid=202,
            worker_start_ticks=901,
            worker_boot_id=str(uuid4()),
            worker_invocation_id="d" * 32,
            worker_unit=f"swapp-ai-scientist-scorer-{uuid4().hex}.service",
        )
        self.worker["worker_cgroup"] = (
            "/user.slice/swapp-ai-scientist-scorer.slice/" + (self.worker["worker_unit"])
        )
        self.invocation = ScorerInvocation(
            self.worker["worker_unit"], "d" * 32, self.worker["worker_cgroup"]
        )
        self.effects = []

    @contextmanager
    def connect(self):
        yield self

    @contextmanager
    def begin(self):
        yield self

    def execute(self, statement, params):
        import json

        sql = str(statement)
        if "SELECT *,extract" in sql:
            return Result(self.request)
        if "SELECT job_id" in sql:
            return Result([])
        if "lab.empty_baseline_stop_context" in sql:
            return Result(deepcopy(self.context))
        if "lab.record_empty_baseline_stop" in sql:
            evidence = json.loads(params["evidence"])
            self.effects.append((params["phase"], evidence))
            return Result("registered" if params["phase"] == "register" else "drained")
        pytest.fail("unexpected zero-job RPC: " + sql)


@pytest.fixture
def empty_stop(monkeypatch):
    fixture = EmptyStop()
    monkeypatch.setattr(stop_recovery, "prove_stopped_owner_dead", lambda *_args: True)
    monkeypatch.setattr(
        stop_recovery,
        "capture_attempted_stop_recovery_identity",
        lambda _invocation: deepcopy(fixture.worker),
    )
    monkeypatch.setattr(
        stop_recovery,
        "capture_empty_baseline_native_observation",
        lambda *_args, **_kwargs: {"fixture_native": "empty"},
    )
    return fixture


def reconcile(fixture, **kwargs):
    return stop_recovery.reconcile_stopped_children(
        fixture,
        fixture.stop,
        recovery_invocation=fixture.invocation.invocation_id,
        worker_invocation=fixture.invocation,
        **kwargs,
    )


def test_zero_jobs_registers_native_proof_then_seals_same_actual_worker(empty_stop):
    assert reconcile(empty_stop) == "drained"
    assert [phase for phase, _ in empty_stop.effects] == ["register", "seal"]
    first, second = (evidence for _, evidence in empty_stop.effects)
    assert first["worker_identity"] == second["worker_identity"] == empty_stop.worker
    assert first["context"] == second["context"] == empty_stop.context
    assert first["observation"] == second["observation"] == {"fixture_native": "empty"}


def test_zero_jobs_requires_actual_worker_before_any_mutation(empty_stop):
    with pytest.raises(ValueError, match="actual recovery"):
        stop_recovery.reconcile_stopped_children(
            empty_stop, empty_stop.stop, recovery_invocation="d" * 32
        )
    assert empty_stop.effects == []


def test_native_cleanup_is_independent_and_rechecked_before_seal(empty_stop, monkeypatch):
    observations = iter([{"fixture_native": "empty"}, RuntimeError("native child alive")])

    def observe(*_args, **_kwargs):
        value = next(observations)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(stop_recovery, "capture_empty_baseline_native_observation", observe)
    with pytest.raises(RuntimeError, match="native child alive"):
        reconcile(empty_stop)
    assert [phase for phase, _ in empty_stop.effects] == ["register"]


def test_changed_actual_worker_cannot_seal(empty_stop, monkeypatch):
    identities = iter(
        [
            empty_stop.worker,
            empty_stop.worker,
            {**empty_stop.worker, "worker_start_ticks": 902},
        ]
    )
    monkeypatch.setattr(
        stop_recovery,
        "capture_attempted_stop_recovery_identity",
        lambda _invocation: next(identities),
    )
    with pytest.raises(RuntimeError, match="identity changed"):
        reconcile(empty_stop)
    assert [phase for phase, _ in empty_stop.effects] == ["register"]


def test_expired_original_stop_performs_no_proof_write(empty_stop):
    empty_stop.request["remaining"] = 0
    assert reconcile(empty_stop) == "pending"
    assert not empty_stop.effects


@pytest.fixture
def native_fixture(monkeypatch):
    fixture = EmptyStop()
    monkeypatch.setattr(recovery_identity, "_boot_id", lambda: fixture.owner.worker_boot_id)
    monkeypatch.setattr(
        recovery_identity, "_start_ticks", lambda _pid: (_ for _ in ()).throw(FileNotFoundError())
    )
    properties = dict(
        LoadState="loaded",
        ActiveState="inactive",
        MainPID="0",
        InvocationID=fixture.owner.worker_invocation_id,
        ControlGroup=fixture.owner.worker_cgroup,
    )
    monkeypatch.setattr(recovery_identity, "_systemctl_show", lambda *_args, **_kw: properties)
    monkeypatch.setattr(recovery_identity, "_cgroup_is_absent_or_empty", lambda _group: True)
    monkeypatch.setattr(
        recovery_identity.subprocess,
        "run",
        lambda *_args, **_kw: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    return fixture, properties


def test_native_empty_inventory_proves_owner_and_children(native_fixture):
    import time

    fixture, _ = native_fixture
    result = recovery_identity.capture_empty_baseline_native_observation(
        fixture.owner, fixture.run, deadline=time.monotonic() + 10
    )
    assert result["owner_json"] == asdict(fixture.owner)
    assert result["native_children_empty"] is True
    assert result["sandbox_container_ids"] == []


@pytest.mark.parametrize(
    "changed",
    [
        dict(MainPID="101"),
        dict(ActiveState="active"),
        dict(InvocationID="f" * 32),
        dict(ControlGroup="/foreign"),
    ],
)
def test_native_uncertain_owner_blocks_even_with_zero_sql_jobs(native_fixture, changed):
    import time

    fixture, properties = native_fixture
    properties.update(changed)
    with pytest.raises(RuntimeError):
        recovery_identity.capture_empty_baseline_native_observation(
            fixture.owner, fixture.run, deadline=time.monotonic() + 10
        )


def test_live_sandbox_blocks_without_signalling_it(native_fixture, monkeypatch):
    import time

    fixture, _ = native_fixture
    calls = []

    def docker(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="abc123\n")

    monkeypatch.setattr(recovery_identity.subprocess, "run", docker)
    with pytest.raises(RuntimeError, match="sandbox"):
        recovery_identity.capture_empty_baseline_native_observation(
            fixture.owner, fixture.run, deadline=time.monotonic() + 10
        )
    assert len(calls) == 1 and calls[0][1:3] == ["container", "ls"]


@pytest.mark.parametrize("failure", ["pid_present", "cgroup_populated", "docker_error"])
def test_native_incomplete_cleanup_never_returns_empty(native_fixture, monkeypatch, failure):
    import time

    fixture, _ = native_fixture
    if failure == "pid_present":
        monkeypatch.setattr(recovery_identity, "_start_ticks", lambda _pid: 12345)
    elif failure == "cgroup_populated":
        monkeypatch.setattr(recovery_identity, "_cgroup_is_absent_or_empty", lambda _group: False)
    else:
        monkeypatch.setattr(
            recovery_identity.subprocess,
            "run",
            lambda *_a, **_kw: SimpleNamespace(returncode=1, stdout=""),
        )
    with pytest.raises(RuntimeError):
        recovery_identity.capture_empty_baseline_native_observation(
            fixture.owner, fixture.run, deadline=time.monotonic() + 10
        )


def test_foreign_run_or_shared_owner_cannot_use_empty_native_exception(native_fixture):
    import time
    from dataclasses import replace

    from lab.director.recovery import OWNER_DRAIN_UNIT

    fixture, _ = native_fixture
    for owner, run in (
        (fixture.owner, uuid4()),
        (replace(fixture.owner, worker_unit=OWNER_DRAIN_UNIT), fixture.run),
    ):
        with pytest.raises(RuntimeError, match="run-specific"):
            recovery_identity.capture_empty_baseline_native_observation(
                owner, run, deadline=time.monotonic() + 10
            )


def test_deadline_crossed_after_registration_does_not_seal(empty_stop, monkeypatch):
    ticks = iter([0.0, 1.0, 61.0])
    monkeypatch.setattr(stop_recovery.time, "monotonic", lambda: next(ticks))
    assert reconcile(empty_stop) == "pending"
    assert [phase for phase, _ in empty_stop.effects] == ["register"]


@pytest.mark.parametrize("failure", ["unsealed", "wrong_worker", "worker_live"])
def test_director_requires_independent_registered_worker_retirement(
    empty_stop, monkeypatch, failure
):
    import time
    from unittest.mock import MagicMock

    from lab.director import stop_closure
    from lab.director.recovery import RecoveryPending

    sealed = dict(
        context=empty_stop.context, worker_identity=empty_stop.worker, observation={"fixture": True}
    )
    rows = [
        SimpleNamespace(phase="register" if failure == "unsealed" else "seal", evidence_json=sealed)
    ]
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value.execute.return_value.all.return_value = rows
    monkeypatch.setattr(
        stop_closure, "capture_empty_baseline_native_observation", lambda *_a, **_kw: {}
    )
    monkeypatch.setattr(
        stop_closure,
        "verify_attempted_stop_recovery_retirement",
        MagicMock(side_effect=RuntimeError("registered worker remains alive")),
    )
    with pytest.raises((RecoveryPending, RuntimeError)):
        stop_closure._retire_empty_baseline_worker(
            engine,
            empty_stop.stop,
            empty_stop.owner,
            empty_stop.run,
            time.monotonic() + 20,
            expected_invocation="f" * 32 if failure == "wrong_worker" else "d" * 32,
        )
    engine.begin.assert_not_called()


def test_director_reuse_rechecks_native_worker_and_keeps_original_receipt(empty_stop, monkeypatch):
    import json
    import time
    from unittest.mock import MagicMock

    from lab.director import stop_closure

    sealed = dict(
        context=empty_stop.context,
        worker_identity=empty_stop.worker,
        observation={"fixture": "original native observation"},
    )
    retired = dict(
        context=empty_stop.context,
        worker_identity=empty_stop.worker,
        observation={"fixture": "original retirement"},
    )
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value.execute.return_value.all.return_value = [
        SimpleNamespace(phase="seal", evidence_json=sealed),
        SimpleNamespace(phase="retire", evidence_json=retired),
    ]
    write = engine.begin.return_value.__enter__.return_value.execute
    write.return_value.scalar_one.return_value = "retired"
    native = MagicMock(return_value={"fresh": True})
    retirement = MagicMock(return_value={"fresh": "retired"})
    monkeypatch.setattr(stop_closure, "capture_empty_baseline_native_observation", native)
    monkeypatch.setattr(stop_closure, "verify_attempted_stop_recovery_retirement", retirement)
    assert stop_closure._retire_empty_baseline_worker(
        engine, empty_stop.stop, empty_stop.owner, empty_stop.run, time.monotonic() + 20
    )
    native.assert_called_once()
    retirement.assert_called_once_with(empty_stop.worker)
    assert json.loads(write.call_args.args[1]["evidence"]) == retired
