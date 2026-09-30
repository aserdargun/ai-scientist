"""Authenticated worker implementation for the bounded M0.14 operations.

No model or CUDA library is imported by the product CLI. The QLoRA imports
live in `qlora_step_worker.py`, which is launched only by the explicitly
coordinated TRAIN dry-run operation.
"""

# pylint: disable=missing-function-docstring,missing-class-docstring,too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-branches,too-many-statements,too-many-return-statements,too-many-boolean-expressions,protected-access,import-outside-toplevel,broad-exception-caught,reimported,import-error

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess  # nosec B404 -- fixed systemd and Python executables only
import sys
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from lab.llm.gpu_scheduler import (
    MAX_DRAIN_SECONDS,
    MAX_INFERENCE_SECONDS,
    MAX_TOTAL_SECONDS,
    GpuLease,
    PrincipalResolver,
    ProcessIdentity,
    SharedGpuScheduler,
    SystemdPrincipalResolver,
    _process_cgroup,
    _process_identity_alive,
    _read_process_identity,
    boottime,
)
from lab.llm.native_runtime import (
    _DOCTOR_MESSAGES,
    DIAGNOSTIC_S1_PROFILE,
    SYSTEMCTL,
    SYSTEMD_RUN,
    ModelRuntimeError,
    NvidiaSmiObserver,
    OwnedVllmRuntime,
    SystemdUnitManager,
)
from lab.training.maintenance import (
    DEFAULT_TRAINING_PROFILE,
    HOST_RAM_RESERVE_BYTES,
    LOCAL_AGENT_VERSION,
    MINIMUM_FREE_DISK_BYTES,
    TRAIN_CPU_PERCENT,
    TRAIN_MEMORY_BYTES,
    TRAIN_PARENT_MEMORY_BYTES,
    TRAIN_PYTHON,
    TRAIN_RUNTIME,
    TRAIN_STATE_ROOT,
    TRAIN_STEPS,
    TRAIN_TASKS,
    TRAINING_UNIT,
    MaintenanceError,
    MaintenanceLedger,
    Operation,
    _unit_name,
    agent_version_identity,
    training_environment_identity,
)
from lab.training.runtime_paths import validated_gpu_runtime_database

GPU_RUNTIME_ROOT = Path(__file__).resolve().parents[2] / "data/runtime/gpu"

_HEARTBEAT_SECONDS = 8
_POLL_SECONDS = 0.25
_GPU_SAMPLE_SECONDS = 0.5
_TRAIN_JOB_RUNTIME_SECONDS = 600
_CHILD_GATE_SECONDS = 40


@dataclass(frozen=True, slots=True)
class ChildUnitGeneration:
    unit: str
    pid: int
    start_ticks: int
    boot_id: str
    invocation_id: str
    cgroup: str

    def as_json(self) -> dict[str, object]:
        return {
            "unit": self.unit,
            "pid": self.pid,
            "start_ticks": self.start_ticks,
            "boot_id": self.boot_id,
            "invocation_id": self.invocation_id,
            "cgroup": self.cgroup,
        }


def _unit_properties(unit: str) -> dict[str, str]:
    if re.fullmatch(r"swapp-lab-train-job-[0-9a-f]{32}\.service", unit) is None:
        raise ValueError("training child unit is outside the fixed namespace")
    result = subprocess.run(  # nosec B603 -- fixed systemctl binary and strict unit allowlist.
        [
            SYSTEMCTL,
            "--user",
            "show",
            "--no-pager",
            "--property=LoadState",
            "--property=ActiveState",
            "--property=ControlGroup",
            "--property=InvocationID",
            "--property=MainPID",
            "--property=ExecMainCode",
            "--property=ExecMainStatus",
            unit,
        ],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    if result.returncode != 0:
        raise MaintenanceError("training child systemd inspection failed")
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if set(values) != {
        "LoadState",
        "ActiveState",
        "ControlGroup",
        "InvocationID",
        "MainPID",
        "ExecMainCode",
        "ExecMainStatus",
    }:
        raise MaintenanceError("training child unit inspection was incomplete")
    return values


def _child_generation(unit: str, values: dict[str, str]) -> ChildUnitGeneration:
    if values.get("LoadState") != "loaded":
        raise MaintenanceError("training child unit was not loaded")
    try:
        pid = int(values["MainPID"])
    except (KeyError, ValueError) as exc:
        raise MaintenanceError("training child main process identity is invalid") from exc
    invocation = values.get("InvocationID", "")
    cgroup = values.get("ControlGroup", "").rstrip("/")
    if pid <= 1 or re.fullmatch(r"[0-9a-f]{32}", invocation) is None:
        raise MaintenanceError("training child systemd generation is invalid")
    if "/swapp-gpu.slice/" not in cgroup or not cgroup.endswith("/" + unit):
        raise MaintenanceError("training child cgroup does not match its exact unit")
    identity = _read_process_identity(pid)
    if identity is None or _process_cgroup(pid) != cgroup:
        raise MaintenanceError("training child main process could not be generation-fenced")
    return ChildUnitGeneration(
        unit, pid, identity.start_ticks, identity.boot_id, invocation, cgroup
    )


def _matches_generation(values: dict[str, str], generation: ChildUnitGeneration) -> bool:
    if (
        values.get("LoadState") != "loaded"
        or values.get("InvocationID") != generation.invocation_id
        or values.get("ControlGroup", "").rstrip("/") != generation.cgroup
    ):
        return False
    if values.get("ActiveState") in {"inactive", "failed"}:
        return True
    try:
        current = _read_process_identity(int(values["MainPID"]))
    except (KeyError, OSError, ValueError, IndexError):
        return False
    return current == ProcessIdentity(generation.pid, generation.start_ticks, generation.boot_id)


def _same_unit_invocation(values: dict[str, str], generation: ChildUnitGeneration) -> bool:
    return (
        values.get("LoadState") == "loaded"
        and values.get("InvocationID") == generation.invocation_id
        and values.get("ControlGroup", "").rstrip("/") == generation.cgroup
    )


def _atomic_gate(path: Path, generation: ChildUnitGeneration, operation_id: str) -> None:
    """Open the model-import gate only after the exact child generation is durable."""
    if path.exists() or path.is_symlink():
        raise MaintenanceError("training start gate already exists")
    payload = json.dumps(
        {"operation_id": operation_id, **generation.as_json()},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class _TrainingDrainVerifier:
    """Permit lease release only after the child unit and GPU process set drain."""

    def __init__(self, gpu: NvidiaSmiObserver, units: SystemdUnitManager) -> None:
        self.gpu = gpu
        self.units = units
        self.child_unit: str | None = None
        self.generation: ChildUnitGeneration | None = None
        self.recovery_mode = False
        self.child_finished = False

    def __call__(self, lease: GpuLease) -> bool:
        if lease.owner != "lab":
            return False
        if self.child_unit is not None:
            generation = self.generation
            if generation is None or generation.unit != self.child_unit:
                return False
            try:
                values = _unit_properties(self.child_unit)
            except (OSError, subprocess.SubprocessError, MaintenanceError):
                return False
            if values["LoadState"] == "not-found":
                if not (self.recovery_mode or self.child_finished):
                    return False
                try:
                    if not self.units.cgroup_empty(generation.cgroup):
                        return False
                except (OSError, ModelRuntimeError, subprocess.SubprocessError):
                    return False
            else:
                if not _matches_generation(values, generation):
                    return False
                if values["ActiveState"] not in {"inactive", "failed"}:
                    if not (self.recovery_mode or self.child_finished):
                        return False
                    current = _unit_properties(self.child_unit)
                    if not _matches_generation(current, generation):
                        return False
                    stopped = subprocess.run(  # nosec B603 -- exact generation just revalidated.
                        [SYSTEMCTL, "--user", "stop", self.child_unit],
                        capture_output=True,
                        text=True,
                        timeout=MAX_DRAIN_SECONDS,
                        check=False,
                    )
                    if stopped.returncode != 0:
                        return False
                    deadline = boottime() + MAX_DRAIN_SECONDS
                    while boottime() < deadline:
                        current = _unit_properties(self.child_unit)
                        if current["LoadState"] == "not-found":
                            if self.units.cgroup_empty(generation.cgroup):
                                break
                        elif (
                            _same_unit_invocation(current, generation)
                            and current["ActiveState"] in {"inactive", "failed"}
                            and self.units.cgroup_empty(generation.cgroup)
                        ):
                            break
                        time.sleep(_POLL_SECONDS)
                    else:
                        return False
                else:
                    try:
                        if not self.units.cgroup_empty(generation.cgroup):
                            return False
                    except (OSError, ModelRuntimeError, subprocess.SubprocessError):
                        return False
            try:
                if not self.units.cgroup_empty(generation.cgroup):
                    return False
            except (OSError, ModelRuntimeError, subprocess.SubprocessError):
                return False
        try:
            snapshot = self.gpu.snapshot()
        except (OSError, subprocess.SubprocessError, ModelRuntimeError):
            return False
        compute_pids = set(snapshot.process_memory_mib) - set(snapshot.exempt_display_pids)
        return not compute_pids and snapshot.used_memory_mib <= 512


def _runtime_inputs() -> tuple[Path, str, str]:
    aos_unit = os.environ.get("SWAPP_AOS_GPU_UNIT", "")
    lab_unit = os.environ.get("SWAPP_LAB_GPU_UNIT", "")
    database_text = os.environ.get("SWAPP_GPU_RUNTIME_DB", "")
    if re.fullmatch(r"swapp-aos-gpu-[a-z0-9_.@-]+\.service", aos_unit) is None:
        raise MaintenanceError("trusted AOS GPU service mapping is unavailable")
    expected_lab_unit = _unit_name(os.environ.get("SWAPP_TRAIN_OPERATION_ID", ""))
    if lab_unit != expected_lab_unit or lab_unit != TRAINING_UNIT:
        raise MaintenanceError("training worker unit differs from fixed maintenance identity")
    database = Path(database_text)
    try:
        resolved = validated_gpu_runtime_database(
            database,
            project_root=Path(__file__).resolve().parents[2],
            gpu_runtime_root=GPU_RUNTIME_ROOT,
        )
    except ValueError as exc:
        raise MaintenanceError("GPU runtime database path is unsafe") from exc
    return resolved, aos_unit, lab_unit


def _validate_main_process(resolver: SystemdPrincipalResolver) -> None:
    receipt = resolver.resolve("lab")
    if receipt.identity.pid != os.getpid() or not resolver.verify(receipt):
        raise MaintenanceError(
            "training worker is not the authenticated transient service main process"
        )


def _training_lease_scheduler(
    database: Path,
    resolver: PrincipalResolver,
    verifier: _TrainingDrainVerifier,
) -> SharedGpuScheduler:
    return SharedGpuScheduler(
        database,
        principal_resolver=resolver,
        drain_verifier=verifier,
        max_activation_seconds=min(300, MAX_TOTAL_SECONDS - MAX_INFERENCE_SECONDS),
        max_inference_seconds=MAX_INFERENCE_SECONDS,
        max_total_seconds=MAX_TOTAL_SECONDS,
        queue_timeout_seconds=900,
    )


def _await_training_lease(
    scheduler: SharedGpuScheduler,
    request_id: str,
    payload: bytes,
    *,
    queue_seconds: int = 900,
    recovery_hook: Callable[[], object] = lambda: None,
) -> GpuLease:
    entry = scheduler.submit(
        "lab",
        request_id,
        payload,
        activation_seconds=min(300, scheduler.max_activation_seconds),
        inference_seconds=min(600, scheduler.max_inference_seconds),
        total_seconds=scheduler.max_total_seconds,
        queue_timeout_seconds=queue_seconds,
    )
    deadline = min(entry.queue_deadline, boottime() + queue_seconds)
    while boottime() < deadline:
        lease = scheduler.try_acquire("lab", request_id)
        if lease is not None:
            ready = scheduler.mark_ready(lease)
            if ready is None:
                raise MaintenanceError("training lease could not enter the active phase")
            return ready
        recovery_hook()
        time.sleep(_POLL_SECONDS)
    scheduler.cancel_queued("lab", request_id)
    raise MaintenanceError("training lease timed out in the fair AOS/Lab queue")


def _heartbeat(scheduler: SharedGpuScheduler, lease: GpuLease) -> GpuLease:
    renewed = scheduler.heartbeat(lease)
    if renewed is None:
        raise MaintenanceError("training GPU lease expired or was fenced")
    return renewed


def _heartbeat_before_training_start(
    scheduler: SharedGpuScheduler, lease: GpuLease, execution_deadline: float
) -> GpuLease:
    """Refresh ownership and reject a late child before opening its import gate."""
    renewed = _heartbeat(scheduler, lease)
    if (
        boottime() >= execution_deadline
        or boottime() >= min(renewed.inference_deadline, renewed.total_deadline) - MAX_DRAIN_SECONDS
    ):
        raise MaintenanceError("training child start gate is past its lease-bound deadline")
    return renewed


def _recover_orphaned_training(
    scheduler: SharedGpuScheduler,
    ledger: MaintenanceLedger,
    verifier: _TrainingDrainVerifier,
) -> bool:
    """Recover only a quarantined dead owner with its immutable child binding."""
    with closing(scheduler._connect()) as connection:
        state = connection.execute(
            "SELECT active_owner,active_request_id,phase,owner_pid,owner_start_ticks,owner_boot_id "
            "FROM gpu_turn_state WHERE singleton=1"
        ).fetchone()
    if state is None or state["phase"] != "quarantined" or state["active_owner"] != "lab":
        return False
    try:
        owner = ProcessIdentity(
            int(state["owner_pid"]), int(state["owner_start_ticks"]), str(state["owner_boot_id"])
        )
    except (TypeError, ValueError):
        return False
    if _process_identity_alive(owner):
        return False
    operation_id = str(state["active_request_id"])
    with closing(ledger._connect()) as connection:
        row = connection.execute(
            "SELECT operation,state FROM training_maintenance WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
    if row is None or row["state"] not in {"active", "training"}:
        return False
    if row["state"] == "training" and row["operation"] != "train_dry_run":
        return False
    raw_generation = ledger.child_generation(operation_id)
    verifier.child_unit = None
    verifier.generation = None
    gate = TRAIN_STATE_ROOT / f"{operation_id}.start-gate.json"
    if raw_generation is not None:
        try:
            if set(raw_generation) != {
                "unit",
                "pid",
                "start_ticks",
                "boot_id",
                "invocation_id",
                "cgroup",
            }:
                return False
            if any(
                isinstance(raw_generation[name], bool) or not isinstance(raw_generation[name], int)
                for name in ("pid", "start_ticks")
            ) or any(
                not isinstance(raw_generation[name], str)
                for name in ("unit", "boot_id", "invocation_id", "cgroup")
            ):
                return False
            generation = ChildUnitGeneration(
                unit=cast(str, raw_generation["unit"]),
                pid=cast(int, raw_generation["pid"]),
                start_ticks=cast(int, raw_generation["start_ticks"]),
                boot_id=cast(str, raw_generation["boot_id"]),
                invocation_id=cast(str, raw_generation["invocation_id"]),
                cgroup=cast(str, raw_generation["cgroup"]),
            )
            if generation.pid <= 1 or generation.start_ticks <= 0:
                return False
            if re.fullmatch(r"[0-9a-f-]{36}", generation.boot_id) is None:
                return False
            if re.fullmatch(r"[0-9a-f]{32}", generation.invocation_id) is None:
                return False
            if generation.unit != raw_generation["unit"] or not generation.cgroup.endswith(
                "/" + generation.unit
            ):
                return False
            verifier.child_unit = generation.unit
            verifier.generation = generation
        except (KeyError, TypeError, ValueError):
            return False
    elif gate.exists() or gate.is_symlink():
        # The gate can only be atomically created after its binding event commits.
        return False
    verifier.recovery_mode = True
    try:
        recovered = scheduler.recover_quarantined()
    finally:
        verifier.recovery_mode = False
    if not recovered:
        return False
    verifier.child_unit = None
    verifier.generation = None
    ledger.finish(
        operation_id,
        success=False,
        receipt={
            "operation_id": operation_id,
            "status": "failed",
            "failure_class": "RecoveredAfterWorkerLoss",
            "recovery": "exact_owner_and_child_generation_drained",
        },
    )
    return True


def _training_child_argv(
    operation_id: str, result_path: Path, gate_path: Path, *, runtime_seconds: int
) -> tuple[str, list[str]]:
    if isinstance(runtime_seconds, bool) or not 1 <= runtime_seconds <= _TRAIN_JOB_RUNTIME_SECONDS:
        raise ValueError("training child runtime exceeds the fixed operation ceiling")
    child_id = uuid4().hex
    unit = f"swapp-lab-train-job-{child_id}.service"
    if re.fullmatch(r"swapp-lab-train-job-[0-9a-f]{32}\.service", unit) is None:
        raise AssertionError("generated training unit name is invalid")
    argv = [
        SYSTEMD_RUN,
        "--user",
        "--no-block",
        "--quiet",
        f"--unit={unit}",
        "--slice=swapp-gpu.slice",
        "--property=Description=SWAPP bounded synthetic QLoRA measurement",
        f"--property=MemoryMax={TRAIN_MEMORY_BYTES}",
        "--property=MemorySwapMax=0",
        f"--property=CPUQuota={TRAIN_CPU_PERCENT}%",
        f"--property=TasksMax={TRAIN_TASKS}",
        f"--property=RuntimeMaxSec={runtime_seconds}",
        "--property=KillMode=control-group",
        "--property=OOMPolicy=stop",
        "--property=NoNewPrivileges=yes",
        "--property=PrivateTmp=yes",
        "--property=PrivateNetwork=yes",
        "--property=ProtectSystem=strict",
        "--property=ReadWritePaths=" + str(TRAIN_STATE_ROOT.resolve()),
        "--setenv=HF_HUB_OFFLINE=1",
        "--setenv=TRANSFORMERS_OFFLINE=1",
        "--setenv=HF_HUB_DISABLE_TELEMETRY=1",
        "--setenv=WANDB_DISABLED=true",
        "--setenv=OPENBLAS_NUM_THREADS=1",
        "--setenv=OMP_NUM_THREADS=1",
        "--setenv=MKL_NUM_THREADS=1",
        "--setenv=NUMEXPR_NUM_THREADS=1",
        "--setenv=PYTHONDONTWRITEBYTECODE=1",
        f"--setenv=HF_HOME={TRAIN_STATE_ROOT / (operation_id + '.hf')}",
        f"--setenv=XDG_CACHE_HOME={TRAIN_STATE_ROOT / (operation_id + '.cache')}",
        f"--setenv=TRANSFORMERS_CACHE={TRAIN_STATE_ROOT / (operation_id + '.cache/transformers')}",
        f"--setenv=TORCH_HOME={TRAIN_STATE_ROOT / (operation_id + '.cache/torch')}",
        f"--setenv=TORCH_EXTENSIONS_DIR={TRAIN_STATE_ROOT / (operation_id + '.cache/extensions')}",
        f"--setenv=TRITON_CACHE_DIR={TRAIN_STATE_ROOT / (operation_id + '.triton')}",
        f"--setenv=TORCHINDUCTOR_CACHE_DIR={TRAIN_STATE_ROOT / (operation_id + '.inductor')}",
        f"--setenv=CUDA_CACHE_PATH={TRAIN_STATE_ROOT / (operation_id + '.cuda')}",
        str(TRAIN_PYTHON),
        str(Path(__file__).with_name("qlora_step_worker.py")),
        "--operation-id",
        operation_id,
        "--result-file",
        str(result_path),
        "--start-gate",
        str(gate_path),
    ]
    return unit, argv


def _training_child_execution_budget(lease: GpuLease) -> tuple[float, int]:
    """Reserve drain time inside the scheduler's immutable hard lease deadline."""
    lease_deadline = min(lease.inference_deadline, lease.total_deadline)
    execution_deadline = lease_deadline - MAX_DRAIN_SECONDS
    remaining = execution_deadline - boottime()
    runtime_seconds = min(_TRAIN_JOB_RUNTIME_SECONDS, math.floor(remaining))
    if runtime_seconds < 1:
        raise MaintenanceError("GPU lease has insufficient remaining time for child drain reserve")
    return execution_deadline, runtime_seconds


def _host_memory_preflight() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, _, rest = line.partition(":")
            fields = rest.split()
            if key in {"MemTotal", "MemAvailable"} and fields:
                values[key] = int(fields[0]) * 1024
    except (OSError, ValueError) as exc:
        raise MaintenanceError("host memory preflight is unavailable") from exc
    if set(values) != {"MemTotal", "MemAvailable"}:
        raise MaintenanceError("host memory preflight is incomplete")
    if values["MemTotal"] < 24 * 1024**3:
        raise MaintenanceError("host RAM is below the supported shared-host floor")
    if values["MemAvailable"] < (
        TRAIN_MEMORY_BYTES + TRAIN_PARENT_MEMORY_BYTES + HOST_RAM_RESERVE_BYTES
    ):
        raise MaintenanceError("host RAM reserve is insufficient for the maintenance cap")
    free_bytes = shutil.disk_usage(TRAIN_RUNTIME).free
    if free_bytes < MINIMUM_FREE_DISK_BYTES:
        raise MaintenanceError("training operation would violate the free-disk reserve")
    return {"mem_available_bytes": values["MemAvailable"], "free_disk_bytes": free_bytes}


def _run_child_with_heartbeats(
    scheduler: SharedGpuScheduler,
    lease: GpuLease,
    child_unit: str,
    argv: list[str],
    operation_id: str,
    execution_deadline: float,
    gate_path: Path,
    ledger: MaintenanceLedger,
    *,
    runner: Any = subprocess.Popen,
) -> tuple[GpuLease, int, int, int]:
    verifier = scheduler._drain_verifier  # trusted worker composition, not user input
    if isinstance(verifier, _TrainingDrainVerifier):
        verifier.child_unit = child_unit
    process = runner(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    bound: ChildUnitGeneration | None = None
    bind_deadline = min(boottime() + _CHILD_GATE_SECONDS, execution_deadline)
    next_heartbeat = boottime() + _HEARTBEAT_SECONDS
    while bound is None:
        if boottime() >= bind_deadline:
            raise MaintenanceError("training child generation binding timed out")
        if boottime() >= next_heartbeat:
            lease = _heartbeat(scheduler, lease)
            next_heartbeat = boottime() + _HEARTBEAT_SECONDS
        values = _unit_properties(child_unit)
        if values["ActiveState"] in {"activating", "active"}:
            bound = _child_generation(child_unit, values)
            ledger.append_event(operation_id, "training_unit_bound", bound.as_json())
            if isinstance(verifier, _TrainingDrainVerifier):
                verifier.generation = bound
            lease = _heartbeat_before_training_start(scheduler, lease, execution_deadline)
            _atomic_gate(gate_path, bound, operation_id)
            break
        time.sleep(_POLL_SECONDS)
    next_heartbeat = boottime() + _HEARTBEAT_SECONDS
    next_gpu_sample = 0.0
    start_gpu_memory_mib: int | None = None
    peak_gpu_memory_mib = 0
    while True:
        process.poll()  # reap the short-lived `systemd-run --no-block` launcher.
        if not isinstance(verifier, _TrainingDrainVerifier) or bound is None:
            raise MaintenanceError("training drain verifier or generation is unavailable")
        values = _unit_properties(child_unit)
        if values["LoadState"] == "loaded" and not _matches_generation(values, bound):
            raise MaintenanceError("training child systemd invocation disappeared or changed")
        result_path = TRAIN_STATE_ROOT / f"{operation_id}.result.json"
        result_present = result_path.exists() or result_path.is_symlink()
        result_ready = result_present and boottime() < execution_deadline
        if result_ready:
            _read_child_receipt(result_path)
        unit_gone = values["LoadState"] == "not-found"
        if result_ready or unit_gone:
            verifier.child_finished = True
            drain_deadline = min(
                boottime() + MAX_DRAIN_SECONDS,
                lease.inference_deadline,
                lease.total_deadline,
            )
            while boottime() < drain_deadline and not verifier(lease):
                time.sleep(_POLL_SECONDS)
            if not verifier(lease):
                raise MaintenanceError("training child failed to drain within its bounded deadline")
            final_gpu = verifier.gpu.snapshot()
            if start_gpu_memory_mib is None:
                start_gpu_memory_mib = final_gpu.used_memory_mib
            peak_gpu_memory_mib = max(peak_gpu_memory_mib, final_gpu.used_memory_mib)
            return lease, (0 if result_ready else 1), peak_gpu_memory_mib, start_gpu_memory_mib
        if boottime() >= execution_deadline:
            verifier.child_finished = True
            drain_deadline = min(
                boottime() + MAX_DRAIN_SECONDS,
                lease.inference_deadline,
                lease.total_deadline,
            )
            while boottime() < drain_deadline and not verifier(lease):
                time.sleep(_POLL_SECONDS)
            if verifier(lease):
                final_gpu = verifier.gpu.snapshot()
                if start_gpu_memory_mib is None:
                    start_gpu_memory_mib = final_gpu.used_memory_mib
                peak_gpu_memory_mib = max(peak_gpu_memory_mib, final_gpu.used_memory_mib)
                return lease, 1, peak_gpu_memory_mib, start_gpu_memory_mib
            raise MaintenanceError("training child exceeded its lease-bound execution deadline")
        if boottime() >= next_heartbeat:
            lease = _heartbeat(scheduler, lease)
            next_heartbeat = boottime() + _HEARTBEAT_SECONDS
        if boottime() >= next_gpu_sample:
            if not isinstance(verifier, _TrainingDrainVerifier):
                raise MaintenanceError("training drain verifier is unavailable")
            snapshot = verifier.gpu.snapshot()
            if start_gpu_memory_mib is None:
                start_gpu_memory_mib = snapshot.used_memory_mib
            peak_gpu_memory_mib = max(peak_gpu_memory_mib, snapshot.used_memory_mib)
            child = _unit_properties(child_unit)
            if (
                bound is None
                or child["LoadState"] == "not-found"
                or not _matches_generation(child, bound)
            ):
                raise MaintenanceError("training child systemd generation changed while running")
            compute_pids = set(snapshot.process_memory_mib) - set(snapshot.exempt_display_pids)
            if compute_pids:
                owned_pids = set(verifier.units.cgroup_pids(bound.cgroup))
                if not compute_pids <= owned_pids:
                    # Stop only after rechecking the exact invocation, PID start, and cgroup.
                    current = _unit_properties(child_unit)
                    if not _matches_generation(current, bound):
                        raise MaintenanceError(
                            "refusing to stop a changed training unit generation"
                        )
                    subprocess.run(  # nosec B603 -- exact generation was just revalidated.
                        [SYSTEMCTL, "--user", "stop", child_unit],
                        capture_output=True,
                        text=True,
                        timeout=MAX_DRAIN_SECONDS,
                        check=False,
                    )
                    raise MaintenanceError("unadmitted GPU process appeared during training")
            next_gpu_sample = boottime() + _GPU_SAMPLE_SECONDS
        time.sleep(_POLL_SECONDS)


def _read_child_receipt(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
        raise MaintenanceError("bounded training result receipt is missing or invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MaintenanceError("bounded training result receipt is malformed") from exc
    required = {
        "adapter_rank",
        "adapter_saved",
        "cpu_offload_embeddings",
        "embedding_device",
        "schema",
        "load_in_4bit",
        "status",
        "steps_completed",
        "sequence_length",
        "peak_gpu_allocated_bytes",
        "peak_gpu_reserved_bytes",
        "synthetic_data_only",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value.get("schema") != "training-dry-run.v1"
        or value.get("status") != "ok"
        or value.get("steps_completed") != TRAIN_STEPS
        or value.get("sequence_length") != DEFAULT_TRAINING_PROFILE.sequence_length
        or value.get("adapter_rank") != DEFAULT_TRAINING_PROFILE.adapter_rank
        or value.get("load_in_4bit") is not True
        or value.get("cpu_offload_embeddings") is not True
        or value.get("embedding_device") != "cpu"
        or value.get("synthetic_data_only") is not True
        or value.get("adapter_saved") is not False
        or isinstance(value.get("peak_gpu_allocated_bytes"), bool)
        or not isinstance(value.get("peak_gpu_allocated_bytes"), int)
        or value["peak_gpu_allocated_bytes"] <= 0
        or isinstance(value.get("peak_gpu_reserved_bytes"), bool)
        or not isinstance(value.get("peak_gpu_reserved_bytes"), int)
        or value["peak_gpu_reserved_bytes"] < value["peak_gpu_allocated_bytes"]
    ):
        raise MaintenanceError("training result did not meet the fixed measurement contract")
    return value


def execute_worker(
    operation: Operation,
    operation_id: str,
    *,
    ledger: MaintenanceLedger | None = None,
    runtime_factory: Any = OwnedVllmRuntime,
    lease_waiter: Any = _await_training_lease,
    child_runner: Any = _run_child_with_heartbeats,
) -> dict[str, object]:
    """Execute a real, authenticated maintenance operation in its own unit."""
    if re.fullmatch(r"[0-9a-f]{32}", operation_id) is None:
        raise ValueError("maintenance operation identity is invalid")
    if ledger is None:
        ledger_text = os.environ.get("LAB_TRAINING_LEDGER", "")
        if not ledger_text:
            raise MaintenanceError("private training ledger path is unavailable")
        ledger = MaintenanceLedger(Path(ledger_text))
    training_environment = training_environment_identity() if operation == "train_dry_run" else {}
    current_agent = agent_version_identity()
    expected_request: dict[str, object] = {
        "agent_version_before": current_agent["agent_version"],
        "agent_source_sha256_before": current_agent["agent_source_sha256"],
        "operation": operation,
        "profile": DEFAULT_TRAINING_PROFILE.document() if operation == "train_dry_run" else None,
        "scheduler_owner": "lab",
        "unit": _unit_name(operation_id),
        "training_environment": training_environment,
    }
    request_sha256 = ledger.verify_request(operation_id, operation, expected_request)
    database, aos_unit, lab_unit = _runtime_inputs()
    resolver = SystemdPrincipalResolver({"aos": aos_unit, "lab": lab_unit})
    _validate_main_process(resolver)
    preflight = _host_memory_preflight()
    from lab.llm.native_runtime import _prepare_doctor_directories

    _prepare_doctor_directories()
    model_runtime = runtime_factory(
        database,
        principal_resolver=resolver,
        profile=DIAGNOSTIC_S1_PROFILE,
        required_free_disk_bytes=MINIMUM_FREE_DISK_BYTES,
    )
    model_runtime.units.ensure_slice()
    model_runtime._preflight_disk()
    model_runtime._preflight_host()
    verifier = _TrainingDrainVerifier(model_runtime.gpu, model_runtime.units)
    train_scheduler = _training_lease_scheduler(database, resolver, verifier)
    request_id = operation_id
    payload = json.dumps(
        {
            "agent_version": LOCAL_AGENT_VERSION,
            "operation": operation,
            "profile": DEFAULT_TRAINING_PROFILE.document()
            if operation == "train_dry_run"
            else None,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    ledger.advance(
        operation_id,
        expected=("created",),
        state="queued",
        event="queued",
        details={
            "lease_request_id": request_id,
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
        },
    )
    lease: GpuLease | None = None
    receipt: dict[str, object] = {
        "operation": operation,
        "operation_id": operation_id,
        "status": "failed",
        "agent_version_before": LOCAL_AGENT_VERSION,
        "agent_version_after": LOCAL_AGENT_VERSION,
        "agent_version_unchanged": True,
        "agent_source_sha256_before": current_agent["agent_source_sha256"],
        "request_sha256": request_sha256,
        "model_sha256": model_runtime.pin.digest,
        "training_environment": training_environment,
        "profile": DEFAULT_TRAINING_PROFILE.document() if operation == "train_dry_run" else None,
        "host_preflight": preflight,
        "capacity_status": "measurement_only_not_capacity_acceptance",
    }
    result_path = TRAIN_STATE_ROOT / f"{operation_id}.result.json"
    gate_path = TRAIN_STATE_ROOT / f"{operation_id}.start-gate.json"
    try:
        lease = lease_waiter(
            train_scheduler,
            request_id,
            payload,
            recovery_hook=lambda: _recover_orphaned_training(train_scheduler, ledger, verifier),
        )
        ledger.advance(
            operation_id,
            expected=("queued",),
            state="active",
            event="lease_acquired",
            details={
                "fencing_token": lease.fencing_token,
                "owner": lease.owner,
                "owner_pid": lease.owner_identity.pid,
                "owner_start_ticks": lease.owner_identity.start_ticks,
                "owner_boot_id": lease.owner_identity.boot_id,
                "owner_unit": lease.owner_unit,
                "owner_invocation_id": lease.owner_invocation_id,
            },
        )
        if operation == "train_dry_run":
            for suffix in ("hf", "cache", "triton", "inductor", "cuda"):
                (TRAIN_STATE_ROOT / f"{operation_id}.{suffix}").mkdir(
                    mode=0o700, parents=False, exist_ok=False
                )
            if result_path.exists() or result_path.is_symlink():
                raise MaintenanceError("training result path already exists")
            execution_deadline, child_runtime_seconds = _training_child_execution_budget(lease)
            child_unit, argv = _training_child_argv(
                operation_id,
                result_path,
                gate_path,
                runtime_seconds=child_runtime_seconds,
            )
            ledger.advance(
                operation_id,
                expected=("active",),
                state="training",
                event="training_started",
                details={
                    "child_unit": child_unit,
                    "child_unit_sha256": hashlib.sha256(child_unit.encode("ascii")).hexdigest(),
                    "sequence_length": DEFAULT_TRAINING_PROFILE.sequence_length,
                    "steps": DEFAULT_TRAINING_PROFILE.steps,
                },
            )
            lease, exit_code, peak_gpu_memory_mib, start_gpu_memory_mib = child_runner(
                train_scheduler,
                lease,
                child_unit,
                argv,
                operation_id=operation_id,
                execution_deadline=execution_deadline,
                gate_path=gate_path,
                ledger=ledger,
            )
            if exit_code != 0:
                train_scheduler.release(lease)
                lease = None
                ledger.advance(
                    operation_id,
                    expected=("training",),
                    state="drained",
                    event="training_finished",
                    details={"exit_code": exit_code, "measurement_status": "not_recorded"},
                )
                raise MaintenanceError("bounded training unit failed; measurement was not recorded")
            child = _read_child_receipt(result_path)
            result_path.unlink()
            threshold_bytes = int(15.5 * 1024**3)
            receipt["measurement"] = {
                **child,
                "vram_target_bytes": threshold_bytes,
                "start_gpu_used_memory_mib": start_gpu_memory_mib,
                "peak_gpu_used_memory_mib": peak_gpu_memory_mib,
                "peak_gpu_used_estimate_bytes": max(
                    peak_gpu_memory_mib * 1024**2,
                    start_gpu_memory_mib * 1024**2 + child["peak_gpu_reserved_bytes"],
                ),
                "within_target": max(
                    peak_gpu_memory_mib * 1024**2,
                    start_gpu_memory_mib * 1024**2 + child["peak_gpu_reserved_bytes"],
                )
                <= threshold_bytes,
                "capacity_status": "measurement_only_not_capacity_acceptance",
            }
            ledger.advance(
                operation_id,
                expected=("training",),
                state="active",
                event="training_finished",
                details={
                    "steps_completed": child["steps_completed"],
                    "peak_gpu_allocated_bytes": child["peak_gpu_allocated_bytes"],
                },
            )
        # Release must pass the independent child/GPU drain verifier.
        if operation == "train_noop":
            ledger.advance(
                operation_id,
                expected=("active",),
                state="active",
                event="noop_completed",
                details={"status": "no_change", "agent_version": LOCAL_AGENT_VERSION},
            )
        train_scheduler.release(lease)
        lease = None
        ledger.advance(
            operation_id,
            expected=("active",),
            state="drained",
            event="lease_drained",
            details={"status": "verified", "vllm_lifecycle": "per_call_transient_and_drained"},
        )
        ledger.advance(
            operation_id,
            expected=("drained",),
            state="smoke",
            event="smoke_started",
            details={"profile": "s1-diagnostic"},
        )
        smoke = model_runtime.run_turn(
            "lab",
            uuid4().hex,
            _DOCTOR_MESSAGES["s1"],
            enable_thinking=False,
            max_output_tokens=DIAGNOSTIC_S1_PROFILE.max_output_tokens,
            temperature=DIAGNOSTIC_S1_PROFILE.temperature,
            top_p=DIAGNOSTIC_S1_PROFILE.top_p,
            profile=DIAGNOSTIC_S1_PROFILE,
        )
        if re.search(r"\b42\b", smoke.text) is None:
            raise MaintenanceError("post-maintenance model smoke check returned an invalid answer")
        smoke_measurements = (
            smoke.measurements.startup_seconds,
            smoke.measurements.inference_seconds,
            smoke.measurements.drain_seconds,
        )
        if any(not math.isfinite(value) or value < 0 for value in smoke_measurements):
            raise MaintenanceError("post-maintenance smoke measurements are invalid")
        smoke_details: dict[str, object] = {
            "status": "passed",
            "text_sha256": hashlib.sha256(smoke.text.encode("utf-8")).hexdigest(),
            "prompt_tokens": smoke.prompt_tokens,
            "completion_tokens": smoke.completion_tokens,
            "startup_seconds": smoke.measurements.startup_seconds,
            "inference_seconds": smoke.measurements.inference_seconds,
            "drain_seconds": smoke.measurements.drain_seconds,
            "peak_gpu_memory_mib": smoke.measurements.peak_gpu_memory_mib,
        }
        receipt["smoke"] = smoke_details
        ledger.advance(
            operation_id,
            expected=("smoke",),
            state="drained",
            event="smoke_finished",
            details=smoke_details,
        )
        final_agent = agent_version_identity()
        if final_agent != current_agent:
            raise MaintenanceError("agent version or source changed during maintenance operation")
        receipt["agent_version_after"] = final_agent["agent_version"]
        receipt["agent_source_sha256_after"] = final_agent["agent_source_sha256"]
        receipt["status"] = "completed"
        ledger.finish(operation_id, success=True, receipt=receipt)
        return receipt
    except BaseException as exc:
        # Do not force-release on failure: scheduler quarantine remains until
        # its trusted drain callback proves that the recorded GPU child is gone.
        receipt["failure_class"] = type(exc).__name__
        try:
            current = ledger_state(ledger.path, operation_id)
            if current not in {"completed", "failed"}:
                if lease is None:
                    ledger.finish(operation_id, success=False, receipt=receipt)
                else:
                    ledger.append_event(
                        operation_id,
                        "failed",
                        {"failure_class": type(exc).__name__, "lease_still_fenced": True},
                    )
        except Exception as finalization_error:
            receipt["ledger_finalization_error_class"] = type(finalization_error).__name__
        raise
    finally:
        for suffix in () if lease is not None else ("hf", "cache", "triton", "inductor", "cuda"):
            path = TRAIN_STATE_ROOT / f"{operation_id}.{suffix}"
            try:
                if path.is_symlink():
                    path.unlink()
                elif path.is_dir():
                    shutil.rmtree(path)
            except OSError:
                # Non-removal is reflected by the private unit journal; never
                # follow a cache path or delete outside this operation scope.
                pass
        if lease is None:
            for path in (result_path, gate_path):
                try:
                    if path.is_symlink():
                        path.unlink()
                    elif path.is_file():
                        path.unlink()
                except OSError:
                    pass


def ledger_state(path: Path, operation_id: str) -> str:
    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute(
            "SELECT state FROM training_maintenance WHERE operation_id=?", (operation_id,)
        ).fetchone()
    if row is None:
        raise MaintenanceError("training maintenance receipt is unavailable")
    return str(row[0])


def worker_main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="lab-training-worker")
    parser.add_argument("--operation-id", default=os.environ.get("SWAPP_TRAIN_OPERATION_ID"))
    parser.add_argument(
        "--operation",
        choices=("train_noop", "train_dry_run"),
        default=os.environ.get("SWAPP_TRAIN_OPERATION"),
    )
    args = parser.parse_args(argv)
    if (
        not isinstance(args.operation_id, str)
        or not isinstance(args.operation, str)
        or args.operation != os.environ.get("SWAPP_TRAIN_OPERATION")
        or args.operation not in {"train_noop", "train_dry_run"}
    ):
        parser.error("immutable operation identity is missing")
    try:
        result = execute_worker(cast(Operation, args.operation), args.operation_id)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(worker_main())
