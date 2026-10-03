from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from lab.llm import native_runtime as runtime
from lab.llm.gpu_scheduler import GpuLease, LeaseConflict, PrincipalReceipt, _read_process_identity


class _Principal:
    def __init__(self) -> None:
        identity = _read_process_identity(os.getpid())
        assert identity is not None
        self.receipt = PrincipalReceipt("lab", identity, "swapp-lab.service", "e" * 32)
        self.current = True

    def resolve(self, owner: str) -> PrincipalReceipt:
        assert owner == "lab"
        return self.receipt

    def verify(self, receipt: PrincipalReceipt) -> bool:
        return self.current and receipt == self.receipt


def _case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[runtime.OwnedVllmRuntime, GpuLease, list[float], _Principal]:
    runtime_directory = tmp_path / "runtime"
    runtime_directory.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_directory))
    now = [10.0]
    principal = _Principal()
    instance = object.__new__(runtime.OwnedVllmRuntime)
    instance._database = tmp_path / "arbiter.sqlite3"
    instance._clock = lambda: now[0]
    instance._cancellation_observer = None
    instance.pin = runtime.ModelPin(tmp_path, runtime.MODEL_REVISION, "c" * 64, ())
    instance.profile = runtime.DIAGNOSTIC_S1_PROFILE
    instance.scheduler = runtime.SharedGpuScheduler(
        instance._database,
        principal_resolver=principal,
        drain_verifier=lambda _lease: False,
        clock=instance._clock,
    )
    instance._initialize_bindings()
    instance.scheduler.submit("lab", "launch-turn", b"fixture request")
    lease = instance.scheduler.try_acquire("lab", "launch-turn")
    assert lease is not None
    return instance, lease, now, principal


def _invalidate(
    instance: runtime.OwnedVllmRuntime, now: list[float], principal: _Principal, changed: str
) -> None:
    if changed == "heartbeat_expired":
        now[0] = 41.0
        return
    if changed == "caller_revoked":
        principal.current = False
        return
    with instance._connect() as connection:
        if changed == "allocation_released":
            connection.execute(
                "UPDATE gpu_turn_state SET active_owner=NULL,active_request_id=NULL,phase=NULL "
                "WHERE singleton=1"
            )
        elif changed == "ticket_expired":
            connection.execute("UPDATE gpu_turn_requests SET state='expired'")
        elif changed == "ticket_principal_changed":
            connection.execute("UPDATE gpu_turn_requests SET owner_start_ticks=owner_start_ticks+1")
        elif changed == "allocation_principal_changed":
            connection.execute("UPDATE gpu_turn_state SET owner_start_ticks=owner_start_ticks+1")
        elif changed == "fence_changed":
            connection.execute("UPDATE gpu_turn_state SET active_token=active_token+1")
        elif changed == "activation_expired":
            connection.execute("UPDATE gpu_turn_state SET activation_deadline=10")
        elif changed == "total_expired":
            connection.execute("UPDATE gpu_turn_state SET total_deadline=10")
        else:
            assert changed in {"quarantined", "inference"}
            connection.execute("UPDATE gpu_turn_state SET phase=?", (changed,))


_INVALID_ALLOCATIONS = (
    "heartbeat_expired",
    "caller_revoked",
    "allocation_released",
    "ticket_expired",
    "ticket_principal_changed",
    "allocation_principal_changed",
    "fence_changed",
    "activation_expired",
    "total_expired",
    "quarantined",
    "inference",
)


@pytest.mark.parametrize("changed", _INVALID_ALLOCATIONS)
def test_stale_allocation_cannot_persist_a_launch_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    instance, lease, now, principal = _case(tmp_path, monkeypatch)
    _invalidate(instance, now, principal, changed)

    with pytest.raises(LeaseConflict):
        instance._prepare_unit(lease)

    assert instance._binding(lease) is None


@pytest.mark.parametrize("changed", _INVALID_ALLOCATIONS)
def test_stale_allocation_cannot_mark_a_prepared_launch_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    instance, lease, now, principal = _case(tmp_path, monkeypatch)
    row = instance._prepare_unit(lease)
    before = dict(row)
    _invalidate(instance, now, principal, changed)

    with pytest.raises(LeaseConflict):
        instance._set_launch_result(lease, "uncertain")

    after = instance._binding(lease)
    assert after is not None and dict(after) == before


def test_expiry_after_intent_does_not_reach_the_model_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, lease, now, _principal = _case(tmp_path, monkeypatch)
    launches: list[tuple[Any, ...]] = []

    class _ReachedLauncher(RuntimeError):
        pass

    class _Units:
        @staticmethod
        def ensure_slice() -> None:
            pass

        @staticmethod
        def launch(*args: Any, **_kwargs: Any) -> None:
            launches.append(args)
            raise _ReachedLauncher("expired allocation reached launch")

    instance.units = _Units()
    monkeypatch.setattr(runtime.ModelPin, "verify_files", lambda _self: None)
    monkeypatch.setattr(instance, "_preflight_disk", lambda: None)
    monkeypatch.setattr(instance, "_preflight_host", lambda: None)
    monkeypatch.setattr(instance, "_await_ticket", lambda *_args, **_kwargs: lease)
    monkeypatch.setattr(instance.scheduler, "release", lambda _lease: None)

    def expire_after_intent(_lease: GpuLease, _row: Any) -> Path:
        now[0] = 41.0
        return tmp_path / "inert-model.log"

    monkeypatch.setattr(instance, "_diagnostic_log_path", expire_after_intent)
    with pytest.raises(LeaseConflict):
        instance.run_turn(
            "lab",
            "launch-turn",
            ({"role": "user", "content": "inert fixture"},),
            enable_thinking=False,
        )

    assert launches == []


def test_successful_launch_can_be_recorded_after_allocation_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, lease, now, _principal = _case(tmp_path, monkeypatch)
    instance._prepare_unit(lease)
    instance._set_launch_result(lease, "uncertain")
    now[0] = 41.0
    assert instance.scheduler.heartbeat(lease) is None

    instance._set_launch_result(lease, "created")

    row = instance._binding(lease)
    assert row is not None and row["launch_state"] == "created"


def test_compiler_preflight_expiry_does_not_reach_systemd_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, lease, now, _principal = _case(tmp_path, monkeypatch)
    row = instance._prepare_unit(lease)
    instance._set_launch_result(lease, "uncertain")
    executable = tmp_path / "inert-vllm"
    executable.touch()
    log = tmp_path / "inert-model.log"
    log.touch(mode=0o600)
    toolkit = tmp_path / "inert-toolkit"
    monkeypatch.setattr(runtime, "VLLM_EXECUTABLE", executable)
    monkeypatch.setattr(runtime, "_pinned_cuda_toolkit_home", lambda: toolkit)
    monkeypatch.setattr(runtime, "_toolkit_version_from_headers", lambda _path: (13, 2))
    commands: list[list[str]] = []

    def compiler_delay(args: list[str], *, timeout: float) -> Any:
        commands.append(args)
        assert args == [str(toolkit / "bin/nvcc"), "--version"] and timeout == 5
        now[0] = 41.0
        return runtime.subprocess.CompletedProcess(
            args, 0, "Cuda compilation tools, release 13.2, V13.2.78", ""
        )

    monkeypatch.setattr(runtime.SystemdUnitManager, "_run", staticmethod(compiler_delay))
    with pytest.raises(LeaseConflict):
        runtime.SystemdUnitManager().launch(
            row["unit"],
            row["nonce"],
            Path("/tmp/inert-model-uds"),
            instance.pin,
            log,
            model_max_len=instance.profile.model_max_len,
            before_launch=lambda: instance._before_model_launch(lease, row),
        )

    assert len(commands) == 1
    after = instance._binding(lease)
    assert after is not None and after["launch_state"] == "uncertain"


def test_absent_uncertain_launch_is_not_proof_of_drain_after_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, lease, now, _principal = _case(tmp_path, monkeypatch)
    instance._prepare_unit(lease)
    instance._set_launch_result(lease, "uncertain")
    now[0] = 41.0
    assert instance.scheduler.heartbeat(lease) is None

    class _AbsentUnits:
        @staticmethod
        def inspect(_unit: str) -> runtime.UnitSnapshot:
            return runtime.UnitSnapshot("not-found", "inactive", "", "", 0, "", "", 0, 0, 0, 0, 0)

    instance.units = _AbsentUnits()
    assert instance.verify_drained(lease) is False
    with instance._connect() as connection:
        state = connection.execute("SELECT * FROM gpu_turn_state").fetchone()
    assert state is not None and state["phase"] == "quarantined"
    assert (state["active_owner"], state["active_request_id"], state["active_token"]) == (
        lease.owner,
        lease.request_id,
        lease.fencing_token,
    )
