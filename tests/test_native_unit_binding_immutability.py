from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from lab.llm import native_runtime as runtime
from lab.llm.gpu_scheduler import GpuLease, LeaseConflict, PrincipalReceipt, _read_process_identity


class _Principal:
    def __init__(self) -> None:
        identity = _read_process_identity(os.getpid())
        assert identity is not None
        self.receipt = PrincipalReceipt(
            "lab", identity, "swapp-lab.service", "e" * 32
        )

    def resolve(self, owner: str) -> PrincipalReceipt:
        assert owner == "lab"
        return self.receipt

    def verify(self, receipt: PrincipalReceipt) -> bool:
        return receipt == self.receipt


def _case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, bind_initial: bool = True
) -> tuple[runtime.OwnedVllmRuntime, GpuLease, runtime.UnitSnapshot, list[int]]:
    runtime_directory = tmp_path / "runtime"
    runtime_directory.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_directory))
    ticks = [1234]
    monkeypatch.setattr(
        runtime.OwnedVllmRuntime, "_pid_start_ticks", staticmethod(lambda _pid: ticks[0])
    )
    monkeypatch.setattr(
        runtime.OwnedVllmRuntime, "_expected_control_group", staticmethod(lambda *_args: True)
    )
    instance = object.__new__(runtime.OwnedVllmRuntime)
    instance._database = tmp_path / "arbiter.sqlite3"
    instance._clock = lambda: 10.0
    instance.pin = runtime.ModelPin(tmp_path, runtime.MODEL_REVISION, "c" * 64, ())
    instance.scheduler = runtime.SharedGpuScheduler(
        instance._database,
        principal_resolver=_Principal(),
        drain_verifier=lambda _lease: False,
        clock=instance._clock,
    )
    instance._initialize_bindings()
    instance.scheduler.submit("lab", "binding-turn", b"fixture request")
    lease = instance.scheduler.try_acquire("lab", "binding-turn")
    assert lease is not None
    row = instance._prepare_unit(lease)
    snapshot = runtime.UnitSnapshot(
        "loaded",
        "active",
        "a" * 32,
        "/user.slice/swapp-gpu.slice/" + row["unit"],
        4321,
        row["expected_description"],
        "SWAPP_GPU_TURN_NONCE=" + row["nonce"],
        runtime.MODEL_UNIT_MEMORY_BYTES,
        0,
        runtime.MODEL_UNIT_CPU_PERCENT * 10_000,
        runtime.MODEL_UNIT_TASKS,
        runtime.MODEL_RUNTIME_MAX_SECONDS * 1_000_000,
    )
    if bind_initial:
        instance._bind_unit(lease, row, snapshot)
    return instance, lease, snapshot, ticks


@pytest.mark.parametrize("changed_field", ["invocation_id", "main_pid", "start_ticks", "cgroup"])
def test_bound_unit_rejects_a_different_process_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_field: str
) -> None:
    instance, lease, snapshot, ticks = _case(tmp_path, monkeypatch)
    row = instance._binding(lease)
    assert row is not None
    before = dict(row)
    if changed_field == "invocation_id":
        snapshot = replace(snapshot, invocation_id="b" * 32)
    elif changed_field == "main_pid":
        snapshot = replace(snapshot, main_pid=5432)
    elif changed_field == "start_ticks":
        ticks[0] += 1
    else:
        snapshot = replace(snapshot, control_group=snapshot.control_group + "/other")

    with pytest.raises(LeaseConflict):
        instance._bind_unit(lease, row, snapshot)

    after = instance._binding(lease)
    assert after is not None and dict(after) == before


def test_exact_binding_replay_preserves_the_original_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, lease, snapshot, _ticks = _case(tmp_path, monkeypatch)
    row = instance._binding(lease)
    assert row is not None
    before = dict(row)
    instance._clock = lambda: 30.0

    instance._bind_unit(lease, row, snapshot)

    after = instance._binding(lease)
    assert after is not None and dict(after) == before


def test_stale_unbound_row_cannot_replace_the_committed_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, lease, snapshot, _ticks = _case(tmp_path, monkeypatch, bind_initial=False)
    row = instance._binding(lease)
    assert row is not None and row["invocation_id"] is None
    instance._bind_unit(lease, row, snapshot)
    before = instance._binding(lease)
    assert before is not None

    with pytest.raises(LeaseConflict):
        instance._bind_unit(lease, row, replace(snapshot, invocation_id="b" * 32))

    after = instance._binding(lease)
    assert after is not None and dict(after) == dict(before)


@pytest.mark.parametrize("launch_state", ["prepared", "created", "uncertain"])
def test_quarantine_recovery_can_capture_an_unbound_worker_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launch_state: str
) -> None:
    instance, lease, snapshot, _ticks = _case(tmp_path, monkeypatch, bind_initial=False)
    with instance._connect() as connection:
        connection.execute(
            "UPDATE gpu_runtime_bindings SET launch_state=?", (launch_state,)
        )
        connection.execute(
            "UPDATE gpu_turn_state SET phase='quarantined',activation_deadline=1,"
            "inference_deadline=1,total_deadline=1,heartbeat_deadline=1"
        )
    row = instance._binding(lease)
    assert row is not None

    instance._bind_unit(replace(lease, phase="quarantined"), row, snapshot)

    bound = instance._binding(lease)
    assert bound is not None and bound["invocation_id"] == snapshot.invocation_id
    assert bound["main_start_ticks"] == 1234 and bound["launch_state"] == "created"


@pytest.mark.parametrize(
    ("column", "replacement"),
    [
        ("active_owner", "aos"),
        ("active_request_id", "another-turn"),
        ("active_token", 2),
        ("owner_pid", 9999),
        ("owner_start_ticks", 9999),
        ("owner_boot_id", "another-boot"),
        ("owner_unit", "another.service"),
        ("owner_invocation_id", "d" * 32),
        ("phase", "invalid"),
    ],
)
def test_binding_rejects_changed_scheduler_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, column: str, replacement: object
) -> None:
    instance, lease, snapshot, _ticks = _case(tmp_path, monkeypatch)
    row = instance._binding(lease)
    assert row is not None
    before = dict(row)
    with instance._connect() as connection:
        connection.execute(f"UPDATE gpu_turn_state SET {column}=?", (replacement,))

    with pytest.raises(LeaseConflict):
        instance._bind_unit(lease, row, snapshot)

    after = instance._binding(lease)
    assert after is not None and dict(after) == before


@pytest.mark.parametrize("changed_field", ["owner", "request_id", "fencing_token"])
def test_binding_rejects_a_lease_for_another_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_field: str
) -> None:
    instance, lease, snapshot, _ticks = _case(tmp_path, monkeypatch)
    row = instance._binding(lease)
    assert row is not None
    before = dict(row)
    if changed_field == "owner":
        another_lease = replace(lease, owner="aos")
    elif changed_field == "request_id":
        another_lease = replace(lease, request_id="another-turn")
    else:
        another_lease = replace(lease, fencing_token=lease.fencing_token + 1)

    with pytest.raises(LeaseConflict):
        instance._bind_unit(another_lease, row, snapshot)

    after = instance._binding(lease)
    assert after is not None and dict(after) == before
