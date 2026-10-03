"""Original SQL bindings plus synthetic read-only OS observations; no real GPU."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
import test_aos_original_physical_snapshot as source

from lab.llm.aos_physical_readback import PhysicalReadbackError, verify_physical_readback
from lab.llm.native_runtime import (
    MODEL_UNIT_CPU_PERCENT,
    MODEL_UNIT_MEMORY_BYTES,
    MODEL_UNIT_TASKS,
    GpuSnapshot,
    UnitSnapshot,
)

publication = source.publication
released = source.released


def observers(snapshot, **changes):
    child = snapshot["child"]
    unit = UnitSnapshot(
        "loaded",
        "inactive",
        child["invocation_id"],
        child["control_group"],
        0,
        f"SWAPP AOS GPU turn {child['nonce']}",
        f"SWAPP_GPU_TURN_NONCE={child['nonce']}",
        MODEL_UNIT_MEMORY_BYTES,
        0,
        MODEL_UNIT_CPU_PERCENT * 10_000,
        MODEL_UNIT_TASKS,
        child["total_seconds"] * 1_000_000,
    )
    units = SimpleNamespace(
        inspect=lambda _name: replace(unit, **changes),
        cgroup_empty=lambda _group: True,
        stop=lambda *_args, **_kwargs: pytest.fail("physical readback must not stop a process"),
    )
    # Another lane's model may run; target absence is distinct from an idle host.
    gpu = SimpleNamespace(snapshot=lambda: GpuSnapshot({999999: 256}, 256, 16376))
    return units, gpu


def verify(p, **options):
    snapshot = source._read(p)
    units, gpu = observers(snapshot)
    kwargs = dict(
        read_snapshot=lambda: source._read(p),
        expected_evidence=snapshot["evidence"],
        units=units,
        gpu=gpu,
        boot_id=lambda: p.peer.boot_id,
        process_alive=lambda _identity: False,
    )
    kwargs.update(options)
    return verify_physical_readback(**kwargs)


def test_original_source_and_two_observations_preserve_all_rows(released):
    before = source._rows(released)
    result = verify(released)
    assert result == source._read(released)
    assert source._rows(released) == before


def test_never_admitted_requires_no_os_or_gpu_observation(publication):
    p = publication
    p.register()
    p.cancel()
    snapshot = source._read(p)

    def fail(*_args):
        pytest.fail("never-admitted target has no physical child")

    result = verify_physical_readback(
        read_snapshot=lambda: source._read(p),
        expected_evidence=snapshot["evidence"],
        units=SimpleNamespace(inspect=fail, cgroup_empty=fail),
        gpu=SimpleNamespace(snapshot=fail),
    )
    assert result["child"] is None


@pytest.mark.parametrize("fault", ["process", "group", "gpu", "boot", "generation", "nonce"])
def test_unproven_physical_cleanup_fails_closed(released, fault):
    p = released
    snapshot = source._read(p)
    units, gpu = observers(snapshot)
    changes = dict(units=units, gpu=gpu)
    if fault == "process":
        changes["process_alive"] = lambda _identity: True
    elif fault == "group":
        units.cgroup_empty = lambda _group: False
    elif fault == "gpu":
        gpu.snapshot = lambda: GpuSnapshot({snapshot["child"]["main_pid"]: 1}, 1, 16376)
    elif fault == "boot":
        changes["boot_id"] = lambda: "different-boot"
    elif fault == "generation":
        changes["units"], _ = observers(snapshot, invocation_id="0" * 32)
    else:
        changes["units"], _ = observers(snapshot, environment="SWAPP_GPU_TURN_NONCE=other")
    with pytest.raises(PhysicalReadbackError):
        verify(p, **changes)


def test_second_observation_catches_late_gpu_consumer(released):
    snapshot = source._read(released)
    units, gpu = observers(snapshot)
    calls = iter(
        [GpuSnapshot({}, 0, 16376), GpuSnapshot({snapshot["child"]["main_pid"]: 1}, 1, 16376)]
    )
    gpu.snapshot = lambda: next(calls)
    with pytest.raises(PhysicalReadbackError, match="reappeared"):
        verify(released, units=units, gpu=gpu)


@pytest.mark.parametrize("fault", ["source", "token", "target_active", "revocation"])
def test_final_source_read_catches_changes(released, fault):
    first = source._read(released)
    second = deepcopy(first)
    if fault == "source":
        second["child"]["nonce"] = "0" * 64
    elif fault == "token":
        second["arbiter"]["active_token"] -= 1
    elif fault == "target_active":
        second["arbiter"].update(active_owner="aos", active_request_id=released.request_id)
    calls = [0]

    def read():
        calls[0] += 1
        if calls[0] == 1:
            return first
        if fault == "revocation":
            raise RuntimeError("current authority revoked")
        return second

    with pytest.raises((PhysicalReadbackError, RuntimeError)):
        verify(released, read_snapshot=read)
    assert calls[0] == 2
