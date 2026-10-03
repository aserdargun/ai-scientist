"""CPU fixtures for broker identity; no live unit or GPU admission."""

import subprocess
import time
from dataclasses import replace

import pytest

from lab.llm import aos_gpu_service as service
from lab.llm.aos_gpu_control import ControlError, LabAOSControl
from lab.llm.aos_gpu_control_store import control_deadline
from lab.llm.gpu_scheduler import ProcessIdentity


@pytest.fixture
def broker(monkeypatch):
    identity = ProcessIdentity(321, 456, "fixture-boot")
    values = {
        "LoadState": "loaded",
        "ActiveState": "active",
        "MainPID": "321",
        "InvocationID": "a" * 32,
        "ControlGroup": "/fixture/" + service.BROKER_UNIT,
    }
    monkeypatch.setattr(service.os, "getpid", lambda: 321)
    monkeypatch.setattr(service.os, "getuid", lambda: 1000)
    monkeypatch.setattr(service, "_boot_id", lambda: identity.boot_id)
    monkeypatch.setattr(service, "_read_process_identity", lambda _pid: identity)
    monkeypatch.setattr(service, "_process_cgroup", lambda _pid: values["ControlGroup"])
    monkeypatch.setattr(service, "_systemctl_show", lambda *_args, **_kwargs: values.copy())
    return identity, values


def test_broker_generation_binds_uid_unit_invocation_and_cgroup(broker):
    generation = service._broker_generation()
    assert generation == {
        "pid": 321,
        "start_ticks": 456,
        "boot_id": "fixture-boot",
        "uid": 1000,
        "unit": service.BROKER_UNIT,
        "invocation_id": "a" * 32,
        "control_group": "/fixture/" + service.BROKER_UNIT,
    }
    assert service._broker_still_current(generation)
    broker[1]["InvocationID"] = "b" * 32
    assert not service._broker_still_current(generation)


@pytest.mark.parametrize(
    "field,value",
    [
        ("MainPID", "322"),
        ("InvocationID", "invalid"),
        ("ActiveState", "inactive"),
        ("LoadState", "not-found"),
        ("ControlGroup", "/fixture/other.service"),
    ],
)
def test_broker_rejects_unbound_systemd_identity(broker, field, value):
    broker[1][field] = value
    with pytest.raises(ValueError, match="authenticated systemd generation"):
        service._broker_generation()


def test_broker_rejects_process_birth_change_during_lookup(broker, monkeypatch):
    original = broker[0]
    births = iter([original, replace(original, start_ticks=457)])
    monkeypatch.setattr(service, "_read_process_identity", lambda _pid: next(births))
    with pytest.raises(ValueError, match="authenticated systemd generation"):
        service._broker_generation()


def test_control_checks_server_before_authorizing_peer():
    control = LabAOSControl(
        policy=None,
        authenticator=None,
        store=None,
        server_generation={},
        clock=lambda: 0,
        server_current=lambda: False,
    )
    with pytest.raises(ControlError, match="stale_generation"):
        control._current(None)


def test_broker_lookup_obeys_original_control_deadline(broker, monkeypatch):
    timeouts = []

    def show(*_args, **kwargs):
        timeouts.append(kwargs["timeout"])
        return broker[1].copy()

    monkeypatch.setattr(service, "_systemctl_show", show)
    token = control_deadline.set(time.monotonic() + 0.5)
    try:
        service._broker_generation()
    finally:
        control_deadline.reset(token)
    assert len(timeouts) == 2
    assert 0 < timeouts[1] <= timeouts[0] <= 0.5


def test_broker_expired_deadline_and_subprocess_timeout_fail_closed(broker, monkeypatch):
    token = control_deadline.set(time.monotonic() - 1)
    try:
        assert not service._broker_still_current({})
    finally:
        control_deadline.reset(token)

    def timed_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("fixture", 0.1)

    monkeypatch.setattr(service, "_systemctl_show", timed_out)
    assert not service._broker_still_current({})
