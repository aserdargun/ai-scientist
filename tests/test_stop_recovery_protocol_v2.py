"""CPU fixtures for stop-v2 RPC plumbing, not native SQL/systemd drain acceptance."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from lab.director.recovery import _stored_owner
from lab.scorer import recovery_identity, stop_recovery
from lab.scorer.holdout_supervisor import _parse_recovery_status
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


class ScorerRoleFixture:
    """Record normal RPCs; no connection, SQL evaluation or privileged role exists."""

    def __init__(self):
        self.stop_id, self.run_id, self.job_id = uuid4(), uuid4(), uuid4()
        self.request = {
            "run_id": self.run_id,
            "remaining": 60,
            "owner_json": {},
            "expected_generation": 2,
            "execution_sha256": "e" * 64,
            "proposal_closure": True,
            "proposal_stop_protocol": "2",
        }
        self.context = {
            "schema": "attempted-stop-context.v2",
            "run_id": str(self.run_id),
            "stop_protocol_version": 2,
            "closure_state": "pending",
            "child_inventory_sha256": None,
            "expected_owner": {
                "generation": 2,
                "invocation_id": "1" * 32,
                "execution_sha256": "e" * 64,
            },
        }
        self.job = {
            "job_id": str(self.job_id),
            "state": "running",
            "claim_invocation_id": "2" * 32,
            "claim_unit": f"swapp-ai-scientist-scorer-{self.job_id.hex}.service",
            "admitted_generation": 2,
        }
        self.effects: list[tuple[str, dict]] = []
        self.registration_reply = "registered"

    @contextmanager
    def connect(self):
        yield self

    @contextmanager
    def begin(self):
        yield self

    def execute(self, statement, parameters):
        query = str(statement)
        if "FROM lab.director_stop_closures" in query:
            assert parameters == {"id": self.stop_id}
            return Result(self.request)
        if "SELECT job_id FROM scorer.score_jobs" in query:
            assert parameters["run"] == self.run_id
            return Result([self.job_id])
        if "lab.stopped_proposal_job_receipt" in query:
            assert parameters["stop"] == self.stop_id
            return Result(self.context if ":stop,NULL" in query else self.job)
        if "lab.finish_stopped_proposal_children" in query:
            assert parameters["id"] == self.stop_id
            envelope = json.loads(parameters["invocation"])
            self.effects.append((envelope["action"], parameters))
            return Result(
                self.registration_reply if envelope["action"] == "register" else "children_drained"
            )
        if "lab.reconcile_stopped_score_job" in query:
            self.effects.append(("job_cas", parameters))
            return Result(True)
        pytest.fail("unexpected RPC: " + query)


@pytest.fixture
def recovery(monkeypatch):
    engine = ScorerRoleFixture()
    unit = f"swapp-ai-scientist-scorer-{uuid4().hex}.service"
    group = "/fixture/scorer.slice/" + unit
    invocation = ScorerInvocation(unit, "3" * 32, group)
    monkeypatch.setattr(
        stop_recovery, "_stored_owner", lambda _row: SimpleNamespace(worker_invocation_id="1" * 32)
    )
    monkeypatch.setattr(stop_recovery, "prove_stopped_owner_dead", lambda *_args: True)
    monkeypatch.setattr(
        stop_recovery, "_job_lifecycle_lock", lambda *_args, **_kwargs: nullcontext()
    )
    monkeypatch.setattr(
        stop_recovery, "_expected_unit_cgroup", lambda selected: "/jobs/" + selected
    )
    monkeypatch.setattr(stop_recovery, "_cgroup_is_absent_or_empty", lambda _group: True)
    monkeypatch.setattr(
        stop_recovery,
        "_systemctl_show",
        lambda selected: {
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "MainPID": "0",
            "InvocationID": engine.job["claim_invocation_id"],
            "ControlGroup": "/jobs/" + selected,
        },
    )
    monkeypatch.setattr(recovery_identity, "verify_systemd_invocation", lambda _unit: invocation)
    monkeypatch.setattr(recovery_identity, "_start_ticks", lambda _pid: 123)
    monkeypatch.setattr(
        recovery_identity, "_boot_id", lambda: "4" * 8 + "-4444-4444-4444-" + "4" * 12
    )
    monkeypatch.setattr(recovery_identity, "_expected_unit_cgroup", lambda _unit: group)
    return engine, invocation


def reconcile(engine, invocation):
    return stop_recovery.reconcile_stopped_children(
        engine,
        engine.stop_id,
        recovery_invocation=invocation.invocation_id,
        worker_invocation=invocation,
    )


def test_actual_worker_registers_once_and_child_seal_keeps_same_identity(recovery):
    engine, invocation = recovery
    assert reconcile(engine, invocation) == "children_drained"
    assert [kind for kind, _ in engine.effects] == ["register", "job_cas", "children_drained"]
    registered = json.loads(engine.effects[0][1]["invocation"])
    sealed = json.loads(engine.effects[2][1]["invocation"])
    assert registered["schema"] == sealed["schema"] == "attempted-stop-worker.v2"
    assert registered["expected_owner"] == engine.context["expected_owner"]
    assert sealed["expected_owner"] == registered["expected_owner"]
    assert (
        sealed["worker_identity"]
        == registered["worker_identity"]
        == {
            "worker_pid": os.getpid(),
            "worker_start_ticks": 123,
            "worker_boot_id": "44444444-4444-4444-4444-444444444444",
            "worker_unit": invocation.unit,
            "worker_invocation_id": invocation.invocation_id,
            "worker_cgroup": invocation.control_group,
        }
    )
    cas = engine.effects[1][1]
    assert cas["stop"] == engine.stop_id and cas["job"] == engine.job_id
    assert cas["recovery"] == invocation.invocation_id and len(cas["recovery"]) == 32
    assert json.loads(cas["expected"]) == engine.job
    assert json.loads(cas["expected"])["claim_invocation_id"] == "2" * 32


@pytest.mark.parametrize(
    "change",
    [
        {"run_id": str(uuid4())},
        {"stop_protocol_version": True},
        {"stop_protocol_version": 1},
        {"closure_state": "children_drained"},
        {"child_inventory_sha256": "a" * 64},
        {
            "expected_owner": {
                "generation": 1,
                "invocation_id": "1" * 32,
                "execution_sha256": "e" * 64,
            }
        },
        {
            "expected_owner": {
                "generation": 2,
                "invocation_id": "9" * 32,
                "execution_sha256": "e" * 64,
            }
        },
        {
            "expected_owner": {
                "generation": 2,
                "invocation_id": "1" * 32,
                "execution_sha256": "f" * 64,
            }
        },
    ],
)
def test_foreign_stale_or_sealed_context_rejects_before_register_and_cas(recovery, change):
    engine, invocation = recovery
    engine.context.update(change)
    with pytest.raises(ValueError, match="foreign or already sealed"):
        reconcile(engine, invocation)
    assert engine.effects == []


def test_boolean_owner_generation_cannot_alias_native_generation_one(recovery, monkeypatch):
    engine, invocation = recovery
    engine.request["expected_generation"] = 1
    unit = f"swapp-ai-scientist-director-dispatch-{engine.run_id.hex}.service"
    engine.request["owner_json"] = {
        "generation": 1,
        "payload_sha256": "a" * 64,
        "worker_pid": 12345,
        "worker_start_ticks": 123,
        "worker_boot_id": "44444444-4444-4444-4444-444444444444",
        "worker_unit": unit,
        "worker_invocation_id": "1" * 32,
        "worker_cgroup": "/fixture/director.slice/" + unit,
    }
    monkeypatch.setattr(stop_recovery, "_stored_owner", _stored_owner)
    engine.context["expected_owner"]["generation"] = True
    with pytest.raises(ValueError, match="foreign or already sealed"):
        reconcile(engine, invocation)
    assert engine.effects == []


@pytest.mark.parametrize("protocol", [True, 2, "3"])
def test_unsupported_wire_marker_rejects_before_register_and_cas(recovery, protocol):
    engine, invocation = recovery
    engine.request["proposal_stop_protocol"] = protocol
    with pytest.raises(ValueError, match="unsupported"):
        reconcile(engine, invocation)
    assert engine.effects == []


@pytest.mark.parametrize("worker", [None, ScorerInvocation("fixture", "9" * 32, "/fixture")])
def test_missing_or_different_actual_worker_rejects_before_registration(recovery, worker):
    engine, invocation = recovery
    with pytest.raises(ValueError, match="actual recovery unit identity"):
        stop_recovery.reconcile_stopped_children(
            engine,
            engine.stop_id,
            recovery_invocation=invocation.invocation_id,
            worker_invocation=worker,
        )
    assert engine.effects == []


def test_missing_actual_invocation_cannot_be_registered(recovery, monkeypatch):
    engine, invocation = recovery
    invalid = replace(invocation, invocation_id="")
    monkeypatch.setattr(recovery_identity, "verify_systemd_invocation", lambda _unit: invalid)
    with pytest.raises(RuntimeError, match="invocation identity"):
        reconcile(engine, invalid)
    assert engine.effects == []


def test_changed_physical_generation_fails_before_registration(recovery, monkeypatch):
    engine, invocation = recovery
    monkeypatch.setattr(
        recovery_identity,
        "verify_systemd_invocation",
        lambda _unit: replace(invocation, invocation_id="f" * 32),
    )
    with pytest.raises(RuntimeError, match="generation changed"):
        reconcile(engine, invocation)
    assert engine.effects == []


def test_changed_capture_tuple_cannot_finish_children_drained(recovery, monkeypatch):
    engine, invocation = recovery
    ticks = iter([123, 124])
    monkeypatch.setattr(recovery_identity, "_start_ticks", lambda _pid: next(ticks))
    with pytest.raises(RuntimeError, match="identity changed before child seal"):
        reconcile(engine, invocation)
    assert [kind for kind, _ in engine.effects] == ["register", "job_cas"]


def test_unconfirmed_registration_cannot_reach_job_cas(recovery):
    engine, invocation = recovery
    engine.registration_reply = "drained"
    with pytest.raises(RuntimeError, match="registration was not confirmed"):
        reconcile(engine, invocation)
    assert [kind for kind, _ in engine.effects] == ["register"]


@pytest.mark.parametrize("kind", ["stop", "run", "restart", "reservation"])
def test_children_drained_status_is_stop_only(kind):
    target = uuid4()
    frame = json.dumps({kind + "_id": str(target), "state": "children_drained"})
    if kind == "stop":
        assert _parse_recovery_status(frame, target, target_kind=kind) == "children_drained"
    else:
        with pytest.raises(RuntimeError, match="identity is invalid"):
            _parse_recovery_status(frame, target, target_kind=kind)


def test_stop_children_drained_parser_rejects_foreign_identity():
    with pytest.raises(RuntimeError, match="identity is invalid"):
        _parse_recovery_status(
            json.dumps({"stop_id": str(uuid4()), "state": "children_drained"}),
            uuid4(),
            target_kind="stop",
        )
