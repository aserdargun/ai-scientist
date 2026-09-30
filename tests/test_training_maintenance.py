from __future__ import annotations

import json
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from lab.llm.gpu_scheduler import ProcessIdentity
from lab.training import maintenance
from lab.training.maintenance import MaintenanceError, MaintenanceLedger
from lab.training.qlora_step_worker import _await_start_gate
from lab.training.runtime_paths import validated_gpu_runtime_database
from lab.training.worker import (
    ChildUnitGeneration,
    _await_training_lease,
    _heartbeat_before_training_start,
    _host_memory_preflight,
    _matches_generation,
    _read_child_receipt,
    _recover_orphaned_training,
    _runtime_inputs,
    _training_child_argv,
    _training_child_execution_budget,
    _TrainingDrainVerifier,
    _unit_properties,
)


def test_fixed_training_unit_is_bound_to_single_maintenance_principal() -> None:
    operation_id = "a" * 32
    assert maintenance._unit_name(operation_id) == "swapp-lab-gpu-maintenance.service"
    with pytest.raises(ValueError):
        maintenance._unit_name("../bad")
    argv = maintenance.build_dispatch_argv(operation_id, "train_noop")
    assert argv[0] == "/usr/bin/systemd-run"
    assert "--unit=swapp-lab-gpu-maintenance.service" in argv
    assert "--slice=swapp-gpu.slice" in argv
    assert f"--property=MemoryMax={maintenance.TRAIN_PARENT_MEMORY_BYTES}" in argv
    assert "--property=MemorySwapMax=0" in argv
    assert "--property=RuntimeMaxSec=900" in argv
    assert "--property=CPUQuota=50%" in argv
    assert "--property=TasksMax=32" in argv
    assert "--property=NoNewPrivileges=yes" in argv
    assert "--property=PrivateTmp=yes" not in argv
    assert "--property=ProtectSystem=strict" not in argv
    assert not any(argument.startswith("--property=ReadWritePaths=") for argument in argv)
    assert f"--working-directory={maintenance.PROJECT_ROOT}" in argv
    assert f"--setenv=PYTHONPATH={maintenance.PROJECT_ROOT}" in argv
    assert argv[-3:] == [maintenance.sys.executable, "-m", "lab.training.worker"]


def test_training_profile_is_fixed_and_cannot_persist_an_adapter() -> None:
    profile = maintenance.DEFAULT_TRAINING_PROFILE.document()
    assert profile == {
        "adapter_rank": 32,
        "cpu_offload_embeddings": True,
        "gradient_checkpointing": "unsloth",
        "load_in_4bit": True,
        "model_repository": "Qwen/Qwen3.5-9B",
        "save_adapter": False,
        "sequence_length": 24_576,
        "steps": 3,
        "synthetic_data_only": True,
    }


def test_ledger_retries_are_immutable_and_completion_requires_receipt(tmp_path: Path) -> None:
    ledger = MaintenanceLedger(tmp_path / "maintenance.sqlite3")
    operation_id = "b" * 32
    request = {"operation": "train_noop", "unit": maintenance.TRAINING_UNIT}
    digest = ledger.create(operation_id, "train_noop", request)
    assert ledger.create(operation_id, "train_noop", request) == digest
    with pytest.raises(MaintenanceError, match="idempotency"):
        ledger.create(operation_id, "train_noop", {**request, "unit": "swapp-lab-gpu-bad.service"})
    with pytest.raises(MaintenanceError, match="completed"):
        ledger.read_completed(operation_id, digest)
    ledger.advance(
        operation_id,
        expected=("created",),
        state="queued",
        event="queued",
        details={"lease_request_id": operation_id},
    )
    with pytest.raises(MaintenanceError, match="fenced"):
        ledger.advance(
            operation_id,
            expected=("created",),
            state="active",
            event="lease_acquired",
            details={},
        )
    ledger.finish(
        operation_id, success=True, receipt={"operation_id": operation_id, "status": "completed"}
    )
    assert ledger.read_completed(operation_id, digest)["status"] == "completed"
    with pytest.raises(MaintenanceError, match="terminal"):
        ledger.advance(
            operation_id,
            expected=("completed",),
            state="active",
            event="lease_acquired",
            details={},
        )


def test_receipt_identity_cannot_be_replayed_under_another_digest(tmp_path: Path) -> None:
    ledger = MaintenanceLedger(tmp_path / "maintenance.sqlite3")
    operation_id = "c" * 32
    digest = ledger.create(operation_id, "train_noop", {"x": 1})
    ledger.finish(
        operation_id, success=True, receipt={"operation_id": operation_id, "status": "completed"}
    )
    with pytest.raises(MaintenanceError, match="completed trusted receipt"):
        ledger.read_completed(operation_id, "0" * 64)
    assert len(digest) == 64


def test_success_exit_without_durable_worker_receipt_fails_closed(
    monkeypatch, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    runtime = project / "data/runtime/train"
    state = runtime / "maintenance"
    gpu_root = project / "data/runtime/gpu"
    gpu_root.mkdir(mode=0o700, parents=True)
    state.mkdir(parents=True, mode=0o700)
    runtime.mkdir(mode=0o700, exist_ok=True)
    monkeypatch.setattr(maintenance, "TRAIN_RUNTIME", runtime)
    monkeypatch.setattr(maintenance, "PROJECT_ROOT", project)
    monkeypatch.setattr(maintenance, "TRAIN_STATE_ROOT", state)
    monkeypatch.setattr(maintenance, "TRAINING_LEDGER", state / "maintenance.sqlite3")
    monkeypatch.setattr(maintenance, "GPU_RUNTIME", gpu_root)
    native_runtime = project / "data/runtime/native-doctor"
    monkeypatch.setattr(maintenance, "NATIVE_DOCTOR_RUNTIME", native_runtime)
    monkeypatch.setattr(
        maintenance,
        "agent_version_identity",
        lambda: {"agent_version": "director.test", "agent_source_sha256": "a" * 64},
    )
    database = gpu_root / "arbiter.sqlite3"
    monkeypatch.setenv("SWAPP_AOS_GPU_UNIT", "swapp-aos-gpu-broker.service")
    monkeypatch.setenv("SWAPP_LAB_GPU_UNIT", maintenance.TRAINING_UNIT)
    monkeypatch.setenv("SWAPP_GPU_RUNTIME_DB", str(database))
    captured: list[list[str]] = []

    def fake_runner(*args, **kwargs):
        captured.append(args[0])
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    with pytest.raises(MaintenanceError, match="completed trusted receipt"):
        maintenance.dispatch("train_noop", runner=fake_runner)
    assert (state / "maintenance.sqlite3").is_file()
    assert len(captured) == 1
    assert not any(arg.startswith("--property=ReadWritePaths=") for arg in captured[0])
    assert "--property=NoNewPrivileges=yes" in captured[0]


def test_wrong_fixed_lab_principal_fails_before_systemd_dispatch(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SWAPP_AOS_GPU_UNIT", "swapp-aos-gpu-broker.service")
    monkeypatch.setenv("SWAPP_LAB_GPU_UNIT", "swapp-lab-gpu-broker.service")
    monkeypatch.setenv("SWAPP_GPU_RUNTIME_DB", str(tmp_path / "arbiter.sqlite3"))

    def should_not_run(*args, **kwargs):
        pytest.fail("systemd dispatch must not run with a mismatched principal")

    with pytest.raises(MaintenanceError, match="fixed maintenance principal"):
        maintenance.dispatch("train_noop", runner=should_not_run)


def test_worker_rejects_spoofed_owner_mapping_before_gpu_access(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SWAPP_TRAIN_OPERATION_ID", "d" * 32)
    monkeypatch.setenv("SWAPP_AOS_GPU_UNIT", "swapp-aos-gpu-broker.service")
    monkeypatch.setenv("SWAPP_LAB_GPU_UNIT", "swapp-lab-gpu-train-" + "d" * 32 + ".service")
    monkeypatch.setenv("SWAPP_GPU_RUNTIME_DB", str(tmp_path / "arbiter.sqlite3"))
    with pytest.raises(MaintenanceError, match="fixed maintenance identity"):
        _runtime_inputs()


def test_gpu_database_validation_rejects_symlinked_ancestor(tmp_path: Path) -> None:
    project = tmp_path / "project"
    gpu_root = project / "data/runtime/gpu"
    gpu_root.mkdir(parents=True, mode=0o700)
    database = gpu_root / "arbiter.sqlite3"
    assert (
        validated_gpu_runtime_database(database, project_root=project, gpu_runtime_root=gpu_root)
        == database
    )
    alias = tmp_path / "alias"
    alias.symlink_to(project / "data/runtime", target_is_directory=True)
    symlinked_database = alias / "gpu/arbiter.sqlite3"
    with pytest.raises(ValueError, match="direct child"):
        validated_gpu_runtime_database(
            symlinked_database, project_root=project, gpu_runtime_root=gpu_root
        )


def test_training_admission_fails_before_launch_when_shared_ram_reserve_is_short(
    monkeypatch, tmp_path: Path
) -> None:
    original_read_text = Path.read_text

    def read_meminfo(path: Path, *args, **kwargs) -> str:
        if path == Path("/proc/meminfo"):
            available_kib = (
                maintenance.TRAIN_MEMORY_BYTES
                + maintenance.TRAIN_PARENT_MEMORY_BYTES
                + maintenance.HOST_RAM_RESERVE_BYTES
            ) // 1024 - 1
            return f"MemTotal: 32768000 kB\nMemAvailable: {available_kib} kB\n"
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_meminfo)
    monkeypatch.setattr("lab.training.worker.TRAIN_RUNTIME", tmp_path)
    with pytest.raises(MaintenanceError, match="RAM reserve"):
        _host_memory_preflight()


def test_training_admission_fails_when_free_disk_reserve_is_short(
    monkeypatch, tmp_path: Path
) -> None:
    original_read_text = Path.read_text

    def read_meminfo(path: Path, *args, **kwargs) -> str:
        if path == Path("/proc/meminfo"):
            return "MemTotal: 32768000 kB\nMemAvailable: 32768000 kB\n"
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_meminfo)
    monkeypatch.setattr("lab.training.worker.TRAIN_RUNTIME", tmp_path)
    monkeypatch.setattr(
        "lab.training.worker.shutil.disk_usage",
        lambda _: SimpleNamespace(free=maintenance.MINIMUM_FREE_DISK_BYTES - 1),
    )
    with pytest.raises(MaintenanceError, match="free-disk reserve"):
        _host_memory_preflight()


def test_queued_training_deadline_uses_boottime_and_cancels_request(monkeypatch) -> None:
    class Scheduler:
        max_activation_seconds = 30
        max_inference_seconds = 30
        max_total_seconds = 60

        def __init__(self) -> None:
            self.cancelled: list[tuple[str, str]] = []

        def submit(self, *args, **kwargs):
            return SimpleNamespace(queue_deadline=10_000.0)

        def cancel_queued(self, owner: str, request_id: str) -> None:
            self.cancelled.append((owner, request_id))

        def try_acquire(self, *_args, **_kwargs):
            pytest.fail("zero-length queue admission must not claim the lease")

    scheduler = Scheduler()
    monkeypatch.setattr("lab.training.worker.boottime", lambda: 100.0)
    monkeypatch.setattr("lab.training.worker.time.monotonic", lambda: pytest.fail("wrong clock"))
    with pytest.raises(MaintenanceError, match="timed out"):
        _await_training_lease(scheduler, "1" * 32, b"payload", queue_seconds=0)
    assert scheduler.cancelled == [("lab", "1" * 32)]


def test_child_receipt_requires_real_fixed_profile_measurement(tmp_path: Path) -> None:
    receipt = {
        "adapter_rank": 32,
        "adapter_saved": False,
        "cpu_offload_embeddings": True,
        "embedding_device": "cpu",
        "schema": "training-dry-run.v1",
        "load_in_4bit": True,
        "status": "ok",
        "steps_completed": 3,
        "sequence_length": 24_576,
        "peak_gpu_allocated_bytes": 1_000_000,
        "peak_gpu_reserved_bytes": 1_500_000,
        "synthetic_data_only": True,
    }
    path = tmp_path / "result.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    assert _read_child_receipt(path)["steps_completed"] == 3
    receipt["steps_completed"] = 2
    path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(MaintenanceError, match="fixed measurement contract"):
        _read_child_receipt(path)


def test_agent_identity_is_bound_to_local_source_file() -> None:
    identity = maintenance.agent_version_identity()
    assert identity["agent_version"] == "director.v0.17.0"
    assert len(identity["agent_source_sha256"]) == 64


def test_child_generation_fence_rejects_reused_pid_or_changed_invocation(monkeypatch) -> None:
    generation = ChildUnitGeneration(
        unit="swapp-lab-train-job-" + "a" * 32 + ".service",
        pid=1234,
        start_ticks=88,
        boot_id="b" * 36,
        invocation_id="c" * 32,
        cgroup="/swapp-gpu.slice/swapp-lab-train-job-" + "a" * 32 + ".service",
    )
    monkeypatch.setattr(
        "lab.training.worker._read_process_identity",
        lambda pid: ProcessIdentity(pid, 89, generation.boot_id),
    )
    same_generation_unit = {
        "LoadState": "loaded",
        "ActiveState": "active",
        "MainPID": "1234",
        "InvocationID": generation.invocation_id,
        "ControlGroup": generation.cgroup,
    }
    assert not _matches_generation(same_generation_unit, generation)
    reused_invocation = {**same_generation_unit, "InvocationID": "d" * 32}
    assert not _matches_generation(reused_invocation, generation)


def test_inactive_child_still_requires_unchanged_generation() -> None:
    generation = ChildUnitGeneration(
        unit="swapp-lab-train-job-" + "e" * 32 + ".service",
        pid=4321,
        start_ticks=52,
        boot_id="f" * 36,
        invocation_id="1" * 32,
        cgroup="/swapp-gpu.slice/swapp-lab-train-job-" + "e" * 32 + ".service",
    )
    values = {
        "LoadState": "loaded",
        "ActiveState": "inactive",
        "MainPID": "0",
        "InvocationID": generation.invocation_id,
        "ControlGroup": generation.cgroup,
    }
    assert _matches_generation(values, generation)
    assert not _matches_generation(
        {**values, "ControlGroup": "/swapp-gpu.slice/foreign.service"}, generation
    )


def test_systemd_inspection_error_is_not_treated_as_missing_unit(monkeypatch) -> None:
    def failed_inspection(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 5, stdout="", stderr="systemctl unavailable")

    monkeypatch.setattr("lab.training.worker.subprocess.run", failed_inspection)
    with pytest.raises(MaintenanceError, match="inspection failed"):
        _unit_properties("swapp-lab-train-job-" + "a" * 32 + ".service")


def test_explicit_systemd_not_found_is_the_only_missing_unit_signal(monkeypatch) -> None:
    properties = {
        "LoadState": "not-found",
        "ActiveState": "inactive",
        "ControlGroup": "",
        "InvocationID": "",
        "MainPID": "0",
        "ExecMainCode": "0",
        "ExecMainStatus": "0",
    }
    output = "\n".join(f"{key}={value}" for key, value in properties.items())
    monkeypatch.setattr(
        "lab.training.worker.subprocess.run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout=output, stderr=""),
    )
    assert _unit_properties("swapp-lab-train-job-" + "b" * 32 + ".service") == properties


def test_drain_verifier_rejects_transient_systemd_inspection_failure(monkeypatch) -> None:
    generation = ChildUnitGeneration(
        unit="swapp-lab-train-job-" + "c" * 32 + ".service",
        pid=1234,
        start_ticks=123,
        boot_id="d" * 36,
        invocation_id="e" * 32,
        cgroup="/swapp-gpu.slice/swapp-lab-train-job-" + "c" * 32 + ".service",
    )

    class FakeUnits:
        @staticmethod
        def cgroup_empty(_: str) -> bool:
            return True

    class FakeGpu:
        @staticmethod
        def snapshot():
            pytest.fail("GPU snapshot is not read after unit inspection failure")

    verifier = _TrainingDrainVerifier(FakeGpu(), FakeUnits())  # type: ignore[arg-type]
    verifier.child_unit = generation.unit
    verifier.generation = generation
    verifier.child_finished = True

    def failed_inspection(_: str) -> dict[str, str]:
        raise MaintenanceError("training child systemd inspection failed")

    monkeypatch.setattr("lab.training.worker._unit_properties", failed_inspection)
    assert not verifier(SimpleNamespace(owner="lab"))  # type: ignore[arg-type]


def test_child_runtime_is_limited_by_remaining_inference_lease(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("lab.training.worker.boottime", lambda: 100.0)
    deadline, runtime_seconds = _training_child_execution_budget(
        SimpleNamespace(inference_deadline=640.0, total_deadline=820.0)  # type: ignore[arg-type]
    )
    assert deadline == 640.0 - 30.0
    assert runtime_seconds == 510
    _, argv = _training_child_argv(
        "f" * 32,
        tmp_path / "result.json",
        tmp_path / "gate.json",
        runtime_seconds=runtime_seconds,
    )
    assert f"--property=RuntimeMaxSec={runtime_seconds}" in argv


def test_child_does_not_launch_without_time_for_owned_drain(monkeypatch) -> None:
    monkeypatch.setattr("lab.training.worker.boottime", lambda: 100.0)
    lease = SimpleNamespace(inference_deadline=129.0, total_deadline=200.0)
    with pytest.raises(MaintenanceError, match="drain reserve"):
        _training_child_execution_budget(lease)  # type: ignore[arg-type]


def test_child_start_gate_requires_live_refreshed_lease_and_remaining_drain_window(
    monkeypatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr("lab.training.worker.boottime", lambda: now[0])
    lease = SimpleNamespace(inference_deadline=640.0, total_deadline=820.0)

    class Scheduler:
        def __init__(self, renewed):
            self.renewed = renewed
            self.calls = 0

        def heartbeat(self, supplied):
            assert supplied is lease
            self.calls += 1
            return self.renewed

    scheduler = Scheduler(lease)
    assert _heartbeat_before_training_start(scheduler, lease, 610.0) is lease  # type: ignore[arg-type]
    assert scheduler.calls == 1

    late_scheduler = Scheduler(lease)
    now[0] = 610.0
    with pytest.raises(MaintenanceError, match="start gate"):
        _heartbeat_before_training_start(late_scheduler, lease, 610.0)  # type: ignore[arg-type]
    assert late_scheduler.calls == 1

    fenced_scheduler = Scheduler(None)
    now[0] = 100.0
    with pytest.raises(MaintenanceError, match="expired or was fenced"):
        _heartbeat_before_training_start(fenced_scheduler, lease, 610.0)  # type: ignore[arg-type]


def test_child_cannot_cross_start_gate_without_parent_generation_receipt(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="did not durably bind"):
        _await_start_gate(tmp_path / "missing-gate", "9" * 32, timeout_seconds=0)


def test_child_generation_ledger_binding_is_immutable(tmp_path: Path) -> None:
    ledger = MaintenanceLedger(tmp_path / "maintenance.sqlite3")
    operation_id = "a" * 32
    ledger.create(operation_id, "train_dry_run", {"operation": "train_dry_run"})
    ledger.advance(
        operation_id,
        expected=("created",),
        state="queued",
        event="queued",
        details={},
    )
    ledger.advance(
        operation_id,
        expected=("queued",),
        state="active",
        event="lease_acquired",
        details={},
    )
    ledger.advance(
        operation_id,
        expected=("active",),
        state="training",
        event="training_started",
        details={},
    )
    generation = {
        "unit": "swapp-lab-train-job-" + "b" * 32 + ".service",
        "pid": 1234,
        "start_ticks": 45,
        "boot_id": "c" * 36,
        "invocation_id": "d" * 32,
        "cgroup": "/user.slice/swapp-gpu.slice/swapp-lab-train-job-" + "b" * 32 + ".service",
    }
    ledger.append_event(operation_id, "training_unit_bound", generation)
    assert ledger.child_generation(operation_id) == generation
    ledger.append_event(operation_id, "training_unit_bound", {**generation, "pid": 4567})
    with pytest.raises(MaintenanceError, match="generation changed"):
        ledger.child_generation(operation_id)


def test_recovery_stops_only_the_persisted_dead_owner_generation(
    monkeypatch, tmp_path: Path
) -> None:
    operation_id = "4" * 32
    ledger = MaintenanceLedger(tmp_path / "maintenance.sqlite3")
    ledger.create(operation_id, "train_dry_run", {"operation": "train_dry_run"})
    ledger.advance(operation_id, expected=("created",), state="queued", event="queued", details={})
    ledger.advance(
        operation_id, expected=("queued",), state="active", event="lease_acquired", details={}
    )
    ledger.advance(
        operation_id, expected=("active",), state="training", event="training_started", details={}
    )
    unit = "swapp-lab-train-job-" + "5" * 32 + ".service"
    cgroup = "/user.slice/user-1000.slice/user@1000.service/swapp-gpu.slice/" + unit
    generation = {
        "unit": unit,
        "pid": 1234,
        "start_ticks": 50,
        "boot_id": "6" * 36,
        "invocation_id": "7" * 32,
        "cgroup": cgroup,
    }
    ledger.append_event(operation_id, "training_unit_bound", generation)

    database = tmp_path / "scheduler.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "CREATE TABLE gpu_turn_state (singleton INTEGER, active_owner TEXT, "
            "active_request_id TEXT, phase TEXT, owner_pid INTEGER, owner_start_ticks INTEGER, "
            "owner_boot_id TEXT)"
        )
        connection.execute(
            "INSERT INTO gpu_turn_state VALUES(1,'lab',?,'quarantined',99,21,?)",
            (operation_id, "8" * 36),
        )
        connection.commit()

    class FakeScheduler:
        def __init__(self) -> None:
            self.drain_verifier = verifier

        def _connect(self):
            result = sqlite3.connect(database)
            result.row_factory = sqlite3.Row
            return result

        def recover_quarantined(self) -> bool:
            return bool(self.drain_verifier(SimpleNamespace(owner="lab")))

    class FakeUnits:
        @staticmethod
        def cgroup_empty(_: str) -> bool:
            return True

    class FakeGpu:
        @staticmethod
        def snapshot():
            return SimpleNamespace(
                process_memory_mib={}, exempt_display_pids=frozenset(), used_memory_mib=0
            )

    verifier = _TrainingDrainVerifier(FakeGpu(), FakeUnits())  # type: ignore[arg-type]
    monkeypatch.setattr("lab.training.worker._process_identity_alive", lambda _: False)
    monkeypatch.setattr(
        "lab.training.worker._unit_properties",
        lambda _: {
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "MainPID": "0",
            "InvocationID": generation["invocation_id"],
            "ControlGroup": generation["cgroup"],
        },
    )
    assert _recover_orphaned_training(FakeScheduler(), ledger, verifier) is True  # type: ignore[arg-type]
    assert ledger_state_for_test(ledger.path, operation_id) == "failed"


def ledger_state_for_test(path: Path, operation_id: str) -> str:
    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute(
            "SELECT state FROM training_maintenance WHERE operation_id=?", (operation_id,)
        ).fetchone()
    assert row is not None
    return str(row[0])
