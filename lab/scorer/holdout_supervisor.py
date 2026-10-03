"""Start and reap one exact systemd Scorer process for holdout evaluation."""

from __future__ import annotations

import contextlib
import json
import re
import subprocess  # nosec B404 -- fixed systemd vectors and constant module entrypoint
import time
from dataclasses import dataclass
from uuid import UUID, uuid4

from lab.scorer.credential_path import scorer_deployment_environment
from lab.scorer.supervisor import (
    PROJECT_ROOT,
    SCORER_SLICE,
    SYSTEMD_RUN,
    WORKER_MEMORY,
    WORKER_TASKS,
    _cgroup_is_absent_or_empty,
    _cgroup_is_empty,
    _ensure_aggregate_slice,
    _job_lifecycle_lock,
    _owned_unit_cgroup,
    _systemctl_show,
    stop_owned_scorer_unit,
)

MAX_HOLDOUT_PROCESS_SECONDS = 600
_CLEANUP_RESERVE_SECONDS = 3


@dataclass(frozen=True, slots=True)
class HoldoutProcessResult:
    """Bounded status returned by the exact holdout worker generation."""

    reservation_id: UUID
    unit: str
    invocation_id: str | None
    exit_code: int
    state: str
    bit: bool | None
    stderr_tail: str | None = None


@dataclass(frozen=True, slots=True)
class HoldoutRecoveryProcessResult:
    """Bounded status from a distinct recovery-service generation."""

    reservation_id: UUID | None
    unit: str
    invocation_id: str
    exit_code: int
    state: str
    stderr_tail: str | None = None
    run_id: UUID | None = None


def _parse_status(stdout: str, reservation_id: UUID) -> tuple[str, bool | None]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("holdout worker returned invalid status JSON") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"reservation_id", "state", "bit"}
        or payload.get("reservation_id") != str(reservation_id)
        or payload.get("state") not in {"passed", "reverted", "failed"}
    ):
        raise RuntimeError("holdout worker status identity is invalid")
    state = payload["state"]
    bit = payload.get("bit")
    if state in {"passed", "reverted"}:
        if type(bit) is not bool or bit != (state == "passed"):
            raise RuntimeError("holdout worker result bit is invalid")
    elif bit is not None:
        raise RuntimeError("failed holdout worker must not emit a result bit")
    return state, bit


def run_holdout_process(
    reservation_id: UUID,
    *,
    admitted_generation: int,
    execution_sha256: str,
    remaining_seconds: int = MAX_HOLDOUT_PROCESS_SECONDS,
) -> HoldoutProcessResult:
    """Run one private Scorer worker in a fresh bounded systemd service.

    It uses the existing aggregate Scorer slice and local Docker P=1 lock; there
    is no second holdout queue or caller-selected command/path.
    """
    if not isinstance(reservation_id, UUID):
        raise TypeError("holdout reservation id must be a UUID")
    if (
        isinstance(admitted_generation, bool)
        or not isinstance(admitted_generation, int)
        or admitted_generation < 1
        or re.fullmatch(r"[0-9a-f]{64}", execution_sha256) is None
    ):
        raise ValueError("holdout admitted owner pair is malformed")
    if (
        isinstance(remaining_seconds, bool)
        or not isinstance(remaining_seconds, int)
        or not 1 <= remaining_seconds <= MAX_HOLDOUT_PROCESS_SECONDS
    ):
        raise ValueError("holdout worker deadline must be 1..600 seconds")
    credential_environment = scorer_deployment_environment()
    total_deadline = time.monotonic() + remaining_seconds
    unit = f"swapp-ai-scientist-scorer-{reservation_id.hex}.service"
    _ensure_aggregate_slice()
    process: subprocess.Popen[str] | None = None
    invocation_id: str | None = None
    observed_control_group: str | None = None
    command: list[str] = []
    lock_remaining = total_deadline - time.monotonic() - _CLEANUP_RESERVE_SECONDS
    with _job_lifecycle_lock(reservation_id, timeout_seconds=lock_remaining):
        # Keep the unit's stop grace inside the caller's total wall allowance.
        launch_seconds = int(total_deadline - time.monotonic() - _CLEANUP_RESERVE_SECONDS)
        if launch_seconds < 1:
            raise TimeoutError("holdout startup exhausted its total run deadline")
        current = _systemctl_show(unit)
        if current.get("LoadState") == "not-found":
            if current.get("ActiveState") != "inactive" or current.get("MainPID") != "0":
                raise RuntimeError("absent holdout unit has residual process state")
        elif current.get("LoadState") == "loaded" and current.get("ActiveState") == "inactive":
            old_group = _owned_unit_cgroup(unit, current)
            if current.get("MainPID") != "0" or not _cgroup_is_empty(old_group):
                raise RuntimeError("previous holdout unit generation has not drained")
        else:
            raise RuntimeError("holdout unit name is already active or unresolved")
        command = [
            SYSTEMD_RUN,
            "--user",
            "--wait",
            "--collect",
            "--quiet",
            "--pipe",
            f"--unit={unit}",
            f"--slice={SCORER_SLICE}",
            f"--property=MemoryMax={WORKER_MEMORY}",
            "--property=MemorySwapMax=0",
            "--property=CPUQuota=100%",
            f"--property=TasksMax={WORKER_TASKS}",
            f"--property=RuntimeMaxSec={launch_seconds}",
            "--property=TimeoutStopSec=3",
            "--property=KillMode=control-group",
            "--property=OOMPolicy=stop",
            "--property=NoNewPrivileges=yes",
            f"--working-directory={PROJECT_ROOT}",
            "--setenv=CUDA_VISIBLE_DEVICES=",
            "--setenv=OPENBLAS_NUM_THREADS=1",
            "--setenv=OMP_NUM_THREADS=1",
            "--setenv=MKL_NUM_THREADS=1",
            "--setenv=NUMEXPR_NUM_THREADS=1",
            *credential_environment,
            str(PROJECT_ROOT / ".venv/bin/python"),
            "-m",
            "lab.scorer.holdout_worker",
            "--reservation-id",
            str(reservation_id),
            "--admitted-generation",
            str(admitted_generation),
            "--execution-sha256",
            execution_sha256,
            "--total-seconds",
            str(launch_seconds),
        ]
        process = subprocess.Popen(  # nosec B603 -- fixed python/module argv and UUID identity
            command,
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    try:
        start_deadline = min(total_deadline, time.monotonic() + 15)
        while time.monotonic() < start_deadline:
            current = _systemctl_show(unit)
            if current.get("LoadState") == "loaded" and current.get("ActiveState") == "active":
                invocation_id = current.get("InvocationID", "").lower()
                if re.fullmatch(r"[0-9a-f]{32}", invocation_id) is None:
                    raise RuntimeError("active holdout worker has no systemd generation id")
                observed_control_group = _owned_unit_cgroup(unit, current)
                if current.get("MainPID") == "0":
                    raise RuntimeError("active holdout worker has no main process")
                break
            if process.poll() is not None:
                invocation_id = current.get("InvocationID", "").lower()
                if re.fullmatch(r"[0-9a-f]{32}", invocation_id) is None:
                    raise RuntimeError("early holdout exit has no observable generation id")
                observed_control_group = _owned_unit_cgroup(unit, current)
                if (
                    current.get("LoadState") != "loaded"
                    or current.get("ActiveState") != "inactive"
                    or current.get("MainPID") != "0"
                    or not _cgroup_is_empty(observed_control_group)
                ):
                    raise RuntimeError("early holdout exit lacks exact-generation drain proof")
                stdout, stderr = process.communicate()
                state, bit = _parse_status(stdout, reservation_id)
                return HoldoutProcessResult(
                    reservation_id,
                    unit,
                    invocation_id,
                    int(process.returncode or 0),
                    state,
                    bit,
                    stderr[-1000:] or None,
                )
            time.sleep(0.05)
        if invocation_id is None:
            raise RuntimeError("holdout worker did not become active within its startup bound")
        remaining = total_deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, 0)
        stdout, stderr = process.communicate(timeout=remaining)
        if observed_control_group is None:
            raise RuntimeError("holdout worker exited before its cgroup was bound")
        properties = _systemctl_show(unit)
        if properties.get("LoadState") != "not-found":
            if (
                properties.get("ActiveState") != "inactive"
                or properties.get("InvocationID", "").lower() != invocation_id
                or properties.get("MainPID") != "0"
                or properties.get("ControlGroup") != observed_control_group
                or not _cgroup_is_empty(observed_control_group)
            ):
                raise RuntimeError("holdout worker result lacks exact-generation drain proof")
        elif not _cgroup_is_absent_or_empty(observed_control_group):
            raise RuntimeError("collected holdout worker cgroup is not empty")
        state, bit = _parse_status(stdout, reservation_id)
        return HoldoutProcessResult(
            reservation_id,
            unit,
            invocation_id,
            int(process.returncode or 0),
            state,
            bit,
            stderr[-1000:] or None,
        )
    except BaseException:
        if process is not None:
            if invocation_id is None:
                with contextlib.suppress(Exception):
                    current = _systemctl_show(unit)
                    observed = current.get("InvocationID", "").lower()
                    if re.fullmatch(r"[0-9a-f]{32}", observed):
                        invocation_id = observed
            if invocation_id is not None and re.fullmatch(r"[0-9a-f]{32}", invocation_id):
                stop_owned_scorer_unit(
                    reservation_id,
                    invocation_id=invocation_id,
                    timeout_seconds=max(1, min(30, int(total_deadline - time.monotonic()))),
                )
            if process.poll() is None:
                process.kill()
                process.communicate()
        raise


def _parse_recovery_status(
    stdout: str, target_id: UUID, *, target_kind: str = "reservation"
) -> str:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("holdout recovery worker returned invalid JSON") from exc
    identity_key = f"{target_kind}_id"
    allowed_states = (
        {"pending", "drained", "children_drained"}
        if target_kind == "stop"
        else {"pending", "drained"}
        if target_kind in {"run", "restart"}
        else {"pending", "passed", "reverted", "failed", "exhausted"}
    )
    if (
        not isinstance(payload, dict)
        or set(payload) != {identity_key, "state"}
        or payload.get(identity_key) != str(target_id)
        or payload.get("state") not in allowed_states
    ):
        raise RuntimeError("holdout recovery worker status identity is invalid")
    return str(payload["state"])


def _run_holdout_recovery_process(
    target_id: UUID, *, target_kind: str, remaining_seconds: int
) -> HoldoutRecoveryProcessResult:
    """Run identity-only Scorer recovery in a fresh bounded unit.

    The recovery unit has a fresh UUID under the validated Scorer unit prefix,
    separate from any immutable reservation-bound evaluation unit.
    """
    if not isinstance(target_id, UUID) or target_kind not in {
        "reservation",
        "run",
        "restart",
        "stop",
    }:
        raise TypeError("holdout recovery target must be a UUID and known kind")
    if (
        isinstance(remaining_seconds, bool)
        or not isinstance(remaining_seconds, int)
        or not 1 <= remaining_seconds <= 120
    ):
        raise ValueError("holdout recovery deadline must be 1..120 seconds")
    credential_environment = scorer_deployment_environment()
    total_deadline = time.monotonic() + remaining_seconds
    cleanup_reserve = min(_CLEANUP_RESERVE_SECONDS, max(0, remaining_seconds - 1))
    recovery_id = uuid4()
    unit = f"swapp-ai-scientist-scorer-{recovery_id.hex}.service"
    _ensure_aggregate_slice()
    process: subprocess.Popen[str] | None = None
    invocation_id: str | None = None
    control_group: str | None = None
    command: list[str] = []
    lock_seconds = total_deadline - time.monotonic() - cleanup_reserve
    with _job_lifecycle_lock(target_id, timeout_seconds=max(0.01, lock_seconds)):
        current = _systemctl_show(unit)
        if current.get("LoadState") != "not-found":
            raise RuntimeError("fresh holdout recovery unit name already exists")
        launch_seconds = int(total_deadline - time.monotonic() - cleanup_reserve)
        if launch_seconds < 1:
            raise TimeoutError("holdout recovery startup exhausted its deadline")
        command = [
            SYSTEMD_RUN,
            "--user",
            "--wait",
            "--collect",
            "--quiet",
            "--pipe",
            f"--unit={unit}",
            f"--slice={SCORER_SLICE}",
            f"--property=MemoryMax={WORKER_MEMORY}",
            "--property=MemorySwapMax=0",
            "--property=CPUQuota=100%",
            f"--property=TasksMax={WORKER_TASKS}",
            f"--property=RuntimeMaxSec={launch_seconds}",
            "--property=TimeoutStopSec=3",
            "--property=KillMode=control-group",
            "--property=OOMPolicy=stop",
            "--property=NoNewPrivileges=yes",
            f"--working-directory={PROJECT_ROOT}",
            "--setenv=CUDA_VISIBLE_DEVICES=",
            "--setenv=OPENBLAS_NUM_THREADS=1",
            "--setenv=OMP_NUM_THREADS=1",
            "--setenv=MKL_NUM_THREADS=1",
            "--setenv=NUMEXPR_NUM_THREADS=1",
            *credential_environment,
            str(PROJECT_ROOT / ".venv/bin/python"),
            "-m",
            "lab.scorer.stop_recovery"
            if target_kind == "stop"
            else "lab.scorer.resume_recovery"
            if target_kind == "restart"
            else "lab.scorer.holdout_recovery",
            f"--{target_kind}-id",
            str(target_id),
            "--recovery-unit-id",
            str(recovery_id),
            "--total-seconds",
            str(launch_seconds),
        ]
        process = subprocess.Popen(  # nosec B603 -- fixed Python module and UUID arguments
            command,
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            start_deadline = min(total_deadline - cleanup_reserve, time.monotonic() + 10)
            while time.monotonic() < start_deadline:
                current = _systemctl_show(unit)
                if current.get("LoadState") == "loaded" and current.get("ActiveState") == "active":
                    invocation_id = current.get("InvocationID", "").lower()
                    if re.fullmatch(r"[0-9a-f]{32}", invocation_id) is None:
                        raise RuntimeError("active holdout recovery unit has no generation id")
                    control_group = _owned_unit_cgroup(unit, current)
                    if current.get("MainPID") == "0":
                        raise RuntimeError("active holdout recovery unit has no main process")
                    break
                if process.poll() is not None:
                    raise RuntimeError("holdout recovery worker exited before generation capture")
                time.sleep(0.05)
            if invocation_id is None:
                raise RuntimeError("holdout recovery worker did not become active")
        except BaseException:
            if invocation_id is None:
                with contextlib.suppress(Exception):
                    current = _systemctl_show(unit)
                    observed = current.get("InvocationID", "").lower()
                    if (
                        current.get("LoadState") == "loaded"
                        and current.get("ActiveState") in {"active", "inactive"}
                        and re.fullmatch(r"[0-9a-f]{32}", observed)
                    ):
                        control_group = _owned_unit_cgroup(unit, current)
                        invocation_id = observed
            if invocation_id is not None:
                with contextlib.suppress(Exception):
                    stop_owned_scorer_unit(
                        recovery_id,
                        invocation_id=invocation_id,
                        timeout_seconds=max(1, min(30, int(total_deadline - time.monotonic()))),
                    )
            if process.poll() is None:
                process.kill()
                process.communicate()
            raise
    try:
        remaining = total_deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, 0)
        stdout, stderr = process.communicate(timeout=remaining)
        properties = _systemctl_show(unit)
        if properties.get("LoadState") == "not-found":
            if not _cgroup_is_absent_or_empty(str(control_group)):
                raise RuntimeError("collected holdout recovery cgroup is not empty")
        elif (
            properties.get("LoadState") != "loaded"
            or properties.get("ActiveState") != "inactive"
            or properties.get("InvocationID", "").lower() != invocation_id
            or properties.get("MainPID") != "0"
            or properties.get("ControlGroup") != control_group
            or not _cgroup_is_empty(str(control_group))
        ):
            raise RuntimeError("holdout recovery result lacks exact-generation drain proof")
        if process.returncode != 0:
            raise RuntimeError("holdout recovery worker failed without a receipt")
        state = _parse_recovery_status(stdout, target_id, target_kind=target_kind)
        return HoldoutRecoveryProcessResult(
            reservation_id=target_id if target_kind == "reservation" else None,
            unit=unit,
            invocation_id=str(invocation_id),
            exit_code=int(process.returncode or 0),
            state=state,
            stderr_tail=stderr[-1000:] or None,
            run_id=target_id if target_kind == "run" else None,
        )
    except BaseException:
        if invocation_id is not None:
            with contextlib.suppress(Exception):
                stop_owned_scorer_unit(
                    recovery_id,
                    invocation_id=invocation_id,
                    timeout_seconds=max(1, min(30, int(total_deadline - time.monotonic()))),
                )
        if process.poll() is None:
            process.kill()
            process.communicate()
        raise


def run_holdout_recovery_process(
    reservation_id: UUID, *, remaining_seconds: int = 30
) -> HoldoutRecoveryProcessResult:
    """Recover one exact reservation from a separate bounded Scorer unit."""
    return _run_holdout_recovery_process(
        reservation_id, target_kind="reservation", remaining_seconds=remaining_seconds
    )


def run_holdout_run_recovery_process(
    run_id: UUID, *, remaining_seconds: int = 30
) -> HoldoutRecoveryProcessResult:
    """Reconcile all open reservations after API stop without blocking its response."""
    return _run_holdout_recovery_process(
        run_id, target_kind="run", remaining_seconds=remaining_seconds
    )


def run_director_restart_recovery_process(
    restart_id: UUID, *, remaining_seconds: int = 120
) -> HoldoutRecoveryProcessResult:
    """Reconcile existing old-generation children for one durable restart intent."""
    return _run_holdout_recovery_process(
        restart_id, target_kind="restart", remaining_seconds=remaining_seconds
    )


def run_stopped_director_recovery_process(
    recovery_id: UUID, *, remaining_seconds: int = 120
) -> HoldoutRecoveryProcessResult:
    """Close only existing jobs bound to the durable operator stop intent."""
    return _run_holdout_recovery_process(
        recovery_id, target_kind="stop", remaining_seconds=remaining_seconds
    )
