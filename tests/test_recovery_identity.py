import os

import pytest

from lab.scorer import recovery_identity as module


@pytest.fixture
def retired(monkeypatch):
    unit = "swapp-ai-scientist-scorer-" + "2" * 32 + ".service"
    group = "/user.slice/swapp-ai-scientist-scorer.slice/" + unit
    identity = {
        "worker_pid": 2**30,
        "worker_start_ticks": 1,
        "worker_boot_id": module._boot_id(),
        "worker_unit": unit,
        "worker_invocation_id": "3" * 32,
        "worker_cgroup": group,
    }
    properties = {
        "LoadState": "not-found",
        "ActiveState": "inactive",
        "MainPID": "0",
        "InvocationID": "",
        "ControlGroup": "",
    }
    monkeypatch.setattr(module, "_expected_unit_cgroup", lambda unit: group)
    monkeypatch.setattr(module, "_systemctl_show", lambda unit: properties)
    monkeypatch.setattr(module, "_cgroup_is_absent_or_empty", lambda path: True)
    return identity, properties


def test_absent_unit_is_insufficient_when_registered_pid_is_actually_live(retired):
    identity, _ = retired
    identity.update(worker_pid=os.getpid(), worker_start_ticks=module._start_ticks(os.getpid()))
    with pytest.raises(RuntimeError, match="remains alive"):
        module.verify_attempted_stop_recovery_retirement(identity)


def test_retired_pid_and_collected_empty_unit_return_bound_observation(retired):
    identity, _ = retired
    proof = module.verify_attempted_stop_recovery_retirement(identity)
    assert proof["worker_identity"] == identity
    assert proof["process_retired"] and proof["cgroup_empty"]
    assert proof["unit_properties"]["LoadState"] == "not-found"


@pytest.mark.parametrize(
    "change",
    [
        {"ActiveState": "active", "MainPID": "42"},
        {"LoadState": "loaded", "InvocationID": "4" * 32},
        {"ControlGroup": "/foreign"},
        {"LoadState": "error"},
    ],
)
def test_foreign_or_uncertain_unit_cannot_be_retired(retired, change):
    identity, properties = retired
    properties.update(change)
    with pytest.raises(RuntimeError, match="uncertain"):
        module.verify_attempted_stop_recovery_retirement(identity)


def test_same_retired_invocation_still_requires_empty_cgroup(retired, monkeypatch):
    identity, properties = retired
    properties.update(LoadState="loaded", InvocationID=identity["worker_invocation_id"])
    monkeypatch.setattr(module, "_cgroup_is_absent_or_empty", lambda path: False)
    with pytest.raises(RuntimeError, match="uncertain"):
        module.verify_attempted_stop_recovery_retirement(identity)


def test_unreadable_pid_is_not_retirement(retired, monkeypatch):
    identity, _ = retired

    def denied(pid):
        raise PermissionError("denied")

    monkeypatch.setattr(module, "_start_ticks", denied)
    with pytest.raises(RuntimeError, match="cannot be inspected"):
        module.verify_attempted_stop_recovery_retirement(identity)


@pytest.mark.parametrize(
    "change",
    [
        {"worker_pid": True},
        {"worker_start_ticks": 0},
        {"worker_unit": "unrelated.service"},
        {"worker_invocation_id": "wrong"},
        {"worker_boot_id": "wrong"},
        {"worker_cgroup": "/foreign"},
        {"unregistered_extra": True},
    ],
)
def test_unregistered_identity_is_rejected_before_physical_inspection(retired, monkeypatch, change):
    identity, _ = retired
    identity.update(change)

    def unexpected(unit):
        pytest.fail("malformed tuple reached unit inspection")

    monkeypatch.setattr(module, "_systemctl_show", unexpected)
    with pytest.raises(RuntimeError):
        module.verify_attempted_stop_recovery_retirement(identity)
