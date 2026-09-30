"""Start and reconcile exact durable calibration cell worker generations."""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess  # nosec B404 -- fixed systemd vectors and fixed module worker
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy import Engine, text

from lab.scorer.care_calibration import (
    CARE_CELL_RESERVATION_SECONDS,
    CareBaselineCellClaim,
    fail_care_baseline_cell,
)
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
    _stop_owned_scorer_unit_locked,
    _systemctl_show,
    stop_owned_scorer_unit,
)

_CLEANUP_RESERVE_SECONDS = 3


@dataclass(frozen=True, slots=True)
class CareCellProcessResult:
    claim_id: UUID
    generation: int
    state: str
    invocation_id: str | None = None


def _start_ticks(pid: int) -> str:
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    close = raw.rfind(")")
    fields = raw[close + 2 :].split()
    if close < 0 or len(fields) <= 19 or not fields[19].isdigit():
        raise RuntimeError("CARE worker process start identity is unavailable")
    return fields[19]


def _boot_id() -> str:
    value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip().lower()
    if re.fullmatch(r"[0-9a-f-]{36}", value) is None:
        raise RuntimeError("CARE worker boot identity is unavailable")
    return value


def _recorded_worker_is_gone(row: dict[str, object]) -> bool:
    """Reject failure CAS while the exact recorded PID/start/boot still exists."""
    try:
        current_boot = _boot_id()
    except (OSError, ValueError, RuntimeError):
        return False
    if current_boot != str(row["worker_boot_id"]):
        return True
    try:
        current_start = _start_ticks(int(str(row["worker_pid"])))
    except (FileNotFoundError, ProcessLookupError):
        return True
    except (OSError, ValueError, RuntimeError):
        return False
    return current_start != str(row["worker_start_ticks"])


def _drain_exact_claim_sandbox(claim_id: UUID) -> bool:
    """Use the existing owner marker protocol only for this claim's private root."""
    from lab.sandbox.docker_runner import (
        DEFAULT_ADMISSION_LOCK,
        DEFAULT_SANDBOX_IMAGE,
        LocalDockerRunner,
    )
    from lab.sandbox.docker_runner import (
        PROJECT_ROOT as SANDBOX_PROJECT_ROOT,
    )
    from lab.scorer.worker import _try_process_admission_lock

    work_root = SANDBOX_PROJECT_ROOT / "data/runtime/care-calibration" / claim_id.hex
    marker = DEFAULT_ADMISSION_LOCK.with_suffix(".intent")
    if marker.is_symlink():
        return False
    if not marker.exists():
        return True
    try:
        intent = json.loads(marker.read_text(encoding="ascii"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(intent, dict) or intent.get("work_root") != str(
        work_root.resolve(strict=False)
    ):
        return False
    process_lock = _try_process_admission_lock()
    if process_lock is None:
        return False
    try:
        runner = LocalDockerRunner(image=DEFAULT_SANDBOX_IMAGE, work_root=work_root)
        runner._reconcile_owned_containers()
        return not marker.exists()
    except Exception:
        return False
    finally:
        os.close(process_lock)


def _parse_worker_result(
    stdout: str, calibration_id: UUID, claim_id: UUID, generation: int
) -> str:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("CARE worker returned invalid status JSON") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"calibration_id", "claim_id", "generation", "state"}
        or payload.get("calibration_id") != str(calibration_id)
        or payload.get("claim_id") != str(claim_id)
        or payload.get("generation") != generation
        or payload.get("state") not in {"succeeded", "failed", "capacity_busy"}
    ):
        raise RuntimeError("CARE worker status identity is invalid")
    return str(payload["state"])


def _claim_row(engine: Engine, claim_id: UUID) -> dict[str, object] | None:
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT claim.claim_id,claim.calibration_id,claim.generation,claim.state,"
                    "claim.worker_pid,claim.worker_start_ticks,claim.worker_boot_id,"
                    "claim.worker_unit,claim.worker_invocation_id,claim.worker_cgroup,"
                    "floor(extract(epoch FROM (claim.deadline_at-clock_timestamp())))::integer "
                    "AS claim_seconds_left,"
                    "floor(extract(epoch FROM (job.deadline_at-clock_timestamp())))::integer "
                    "AS job_seconds_left,"
                    "control.state AS control_state "
                    "FROM scorer.care_baseline_calibration_claims claim "
                    "JOIN scorer.care_baseline_calibration_jobs job USING(calibration_id) "
                    "JOIN scorer.care_baseline_calibration_control control USING(calibration_id) "
                    "WHERE claim.claim_id=:claim_id"
                ),
                {"claim_id": claim_id},
            )
            .mappings()
            .one_or_none()
        )
    return None if row is None else dict(row)


def _row_int(row: dict[str, object], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError("CARE durable claim budget is malformed")
    return value


def _fail_drained_claim(
    engine: Engine,
    claim_id: UUID,
    generation: int,
    *,
    failure_code: str,
    invocation_id: str | None,
) -> CareCellProcessResult:
    fail_care_baseline_cell(
        engine,
        claim_id,
        generation,
        failure_code,
        worker_invocation_id=invocation_id,
    )
    return CareCellProcessResult(claim_id, generation, "failed", invocation_id)


def _terminal_worker_is_drained(row: dict[str, object], claim_id: UUID) -> bool:
    """Keep a terminal receipt from advancing the grid while its worker owns P=1."""
    unit_value = row.get("worker_unit")
    unit = (
        unit_value
        if isinstance(unit_value, str)
        else f"swapp-ai-scientist-scorer-{claim_id.hex}.service"
    )
    try:
        properties = _systemctl_show(unit)
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return False
    if properties.get("LoadState") == "not-found":
        if row.get("worker_pid") is None:
            slice_properties = _systemctl_show(SCORER_SLICE)
            slice_group = slice_properties.get("ControlGroup", "")
            canonical_group = f"{slice_group.rstrip('/')}/{unit}" if slice_group else ""
            cgroup_drained = bool(canonical_group) and _cgroup_is_absent_or_empty(
                canonical_group
            )
            process_gone = True
        else:
            cgroup_value = row.get("worker_cgroup")
            if not isinstance(cgroup_value, str):
                return False
            cgroup_drained = _cgroup_is_absent_or_empty(cgroup_value)
            process_gone = _recorded_worker_is_gone(row)
        return cgroup_drained and process_gone and _drain_exact_claim_sandbox(claim_id)

    if row.get("worker_pid") is None:
        # A reserved-before-bind row has no trusted generation to stop. A loaded
        # unit of the same name is ambiguous, even if it currently looks idle.
        return False
    invocation = row.get("worker_invocation_id")
    cgroup = row.get("worker_cgroup")
    pid = row.get("worker_pid")
    if (
        not isinstance(invocation, str)
        or not isinstance(cgroup, str)
        or isinstance(pid, bool)
        or not isinstance(pid, int)
        or properties.get("InvocationID", "").lower() != invocation
        or properties.get("ControlGroup") != cgroup
    ):
        return False
    if properties.get("ActiveState") == "active":
        if properties.get("MainPID") != str(pid):
            return False
        try:
            if _boot_id() != str(row["worker_boot_id"]):
                return False
            if _start_ticks(pid) != str(row["worker_start_ticks"]):
                return False
            drained = _stop_owned_scorer_unit_locked(
                claim_id, invocation_id=invocation, timeout_seconds=3
            )
        except (OSError, ValueError, RuntimeError, TimeoutError, subprocess.TimeoutExpired):
            return False
        return (
            drained
            and _recorded_worker_is_gone(row)
            and _drain_exact_claim_sandbox(claim_id)
        )
    if properties.get("ActiveState") != "inactive" or properties.get("MainPID") != "0":
        return False
    try:
        owned_group = _owned_unit_cgroup(unit, properties)
    except RuntimeError:
        return False
    return (
        owned_group == cgroup
        and _cgroup_is_empty(owned_group)
        and _recorded_worker_is_gone(row)
        and _drain_exact_claim_sandbox(claim_id)
    )


def reconcile_care_baseline_claim(
    engine: Engine, claim: CareBaselineCellClaim, claim_id: UUID, generation: int
) -> CareCellProcessResult:
    """Observe or fail a prior generation only after exact systemd/cgroup proof."""
    with _job_lifecycle_lock(claim_id, timeout_seconds=25):
        return _reconcile_care_baseline_claim_locked(engine, claim, claim_id, generation)


def _reconcile_care_baseline_claim_locked(
    engine: Engine, claim: CareBaselineCellClaim, claim_id: UUID, generation: int
) -> CareCellProcessResult:
    row = _claim_row(engine, claim_id)
    if row is None or row["generation"] != generation:
        raise RuntimeError("CARE claim identity changed during recovery")
    state = row["state"]
    if state in {"succeeded", "failed"}:
        invocation = row.get("worker_invocation_id")
        if not _terminal_worker_is_drained(row, claim_id):
            return CareCellProcessResult(
                claim_id, generation, "pending", invocation if isinstance(invocation, str) else None
            )
        return CareCellProcessResult(
            claim_id, generation, str(state), invocation if isinstance(invocation, str) else None
        )
    unit = f"swapp-ai-scientist-scorer-{claim_id.hex}.service"
    properties = _systemctl_show(unit)
    if state == "reserved":
        if properties.get("LoadState") == "not-found":
            slice_properties = _systemctl_show(SCORER_SLICE)
            slice_group = slice_properties.get("ControlGroup", "")
            canonical_group = f"{slice_group.rstrip('/')}/{unit}" if slice_group else ""
            if canonical_group and _cgroup_is_absent_or_empty(canonical_group):
                if _drain_exact_claim_sandbox(claim_id):
                    return _fail_drained_claim(
                        engine,
                        claim_id,
                        generation,
                        failure_code="worker_start_failure",
                        invocation_id=None,
                    )
            return CareCellProcessResult(claim_id, generation, "pending")
        if properties.get("ActiveState") == "inactive":
            try:
                group = _owned_unit_cgroup(unit, properties)
            except RuntimeError:
                return CareCellProcessResult(claim_id, generation, "pending")
            if properties.get("MainPID") == "0" and _cgroup_is_empty(group):
                if _drain_exact_claim_sandbox(claim_id):
                    return _fail_drained_claim(
                        engine,
                        claim_id,
                        generation,
                        failure_code="worker_start_failure",
                        invocation_id=None,
                    )
        return CareCellProcessResult(claim_id, generation, "pending")
    if state != "running":
        raise RuntimeError("CARE claim state is invalid")
    expected = {
        "InvocationID": str(row["worker_invocation_id"]),
        "ControlGroup": str(row["worker_cgroup"]),
    }
    if properties.get("LoadState") == "loaded" and properties.get("ActiveState") == "active":
        if (
            properties.get("InvocationID", "").lower() == expected["InvocationID"]
            and properties.get("ControlGroup") == expected["ControlGroup"]
            and properties.get("MainPID") == str(row["worker_pid"])
        ):
            try:
                same_process = _start_ticks(int(str(row["worker_pid"]))) == str(
                    row["worker_start_ticks"]
                ) and _boot_id() == str(row["worker_boot_id"])
            except (OSError, ValueError, RuntimeError):
                same_process = False
            if same_process:
                if (
                    min(
                        _row_int(row, "claim_seconds_left"),
                        _row_int(row, "job_seconds_left"),
                    )
                    > 0
                ):
                    return CareCellProcessResult(
                        claim_id, generation, "pending", expected["InvocationID"]
                    )
                try:
                    drained = _stop_owned_scorer_unit_locked(
                        claim_id,
                        invocation_id=expected["InvocationID"],
                        timeout_seconds=3,
                    )
                except (RuntimeError, TimeoutError, subprocess.TimeoutExpired):
                    return CareCellProcessResult(
                        claim_id, generation, "pending", expected["InvocationID"]
                    )
                if not drained or not _drain_exact_claim_sandbox(claim_id):
                    return CareCellProcessResult(
                        claim_id, generation, "pending", expected["InvocationID"]
                    )
                return _fail_drained_claim(
                    engine,
                    claim_id,
                    generation,
                    failure_code="deadline_exhausted",
                    invocation_id=expected["InvocationID"],
                )
        return CareCellProcessResult(claim_id, generation, "pending")
    if properties.get("LoadState") == "loaded" and properties.get("ActiveState") == "inactive":
        try:
            group = _owned_unit_cgroup(unit, properties)
        except RuntimeError:
            return CareCellProcessResult(claim_id, generation, "pending")
        if (
            properties.get("InvocationID", "").lower() == expected["InvocationID"]
            and properties.get("ControlGroup") == expected["ControlGroup"]
            and properties.get("MainPID") == "0"
            and group == expected["ControlGroup"]
            and _cgroup_is_empty(group)
            and _recorded_worker_is_gone(row)
        ):
            if not _drain_exact_claim_sandbox(claim_id):
                return CareCellProcessResult(claim_id, generation, "pending")
            return _fail_drained_claim(
                engine,
                claim_id,
                generation,
                failure_code="worker_crash",
                invocation_id=expected["InvocationID"],
            )
        return CareCellProcessResult(claim_id, generation, "pending")
    if (
        properties.get("LoadState") == "not-found"
        and _cgroup_is_absent_or_empty(str(row["worker_cgroup"]))
        and _recorded_worker_is_gone(row)
        and _drain_exact_claim_sandbox(claim_id)
    ):
        return _fail_drained_claim(
            engine,
            claim_id,
            generation,
            failure_code="worker_crash",
            invocation_id=expected["InvocationID"],
        )
    return CareCellProcessResult(claim_id, generation, "pending")


def run_care_baseline_cell_process(
    engine: Engine,
    *,
    calibration_id: UUID,
    claim_id: UUID,
    generation: int,
    remaining_seconds: int = CARE_CELL_RESERVATION_SECONDS,
) -> CareCellProcessResult:
    """Start only a newly reserved generation, bind it, and prove exact drain."""
    if not 1 <= remaining_seconds <= CARE_CELL_RESERVATION_SECONDS:
        raise ValueError("CARE worker deadline must be within its durable 100-second reservation")
    claim_row = _claim_row(engine, claim_id)
    if (
        claim_row is None
        or claim_row["calibration_id"] != calibration_id
        or claim_row["generation"] != generation
        or claim_row["state"] != "reserved"
        or claim_row["control_state"] != "running"
    ):
        raise RuntimeError("only the exact newly reserved CARE claim may start a worker")
    durable_left = (
        min(
            _row_int(claim_row, "claim_seconds_left"),
            _row_int(claim_row, "job_seconds_left"),
        )
        - 1
    )
    effective_seconds = min(remaining_seconds, durable_left)
    if effective_seconds < 1:
        raise TimeoutError("CARE claim deadline expired before worker startup")
    total_deadline = time.monotonic() + effective_seconds
    unit = f"swapp-ai-scientist-scorer-{claim_id.hex}.service"
    _ensure_aggregate_slice()
    process: subprocess.Popen[str] | None = None
    invocation_id: str | None = None
    observed_cgroup: str | None = None
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
        f"--property=RuntimeMaxSec={effective_seconds}",
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
        str(PROJECT_ROOT / ".venv/bin/python"),
        "-m",
        "lab.scorer.care_calibration_worker",
        "--calibration-id",
        str(calibration_id),
        "--claim-id",
        str(claim_id),
        "--generation",
        str(generation),
        "--total-seconds",
        str(effective_seconds),
    ]
    lock_timeout = total_deadline - time.monotonic() - _CLEANUP_RESERVE_SECONDS
    if lock_timeout <= 0:
        raise TimeoutError("CARE worker lifecycle lock exhausted its cell reservation")
    try:
        with _job_lifecycle_lock(claim_id, timeout_seconds=lock_timeout):
            # Recovery and launch share this lifecycle lock. Re-read after acquiring
            # it so a terminal or separately recovered claim cannot be relaunched.
            claim_row = _claim_row(engine, claim_id)
            if (
                claim_row is None
                or claim_row["calibration_id"] != calibration_id
                or claim_row["generation"] != generation
                or claim_row["state"] != "reserved"
                or claim_row["control_state"] != "running"
            ):
                raise RuntimeError("CARE claim changed before its worker could start")
            durable_left = (
                min(
                    _row_int(claim_row, "claim_seconds_left"),
                    _row_int(claim_row, "job_seconds_left"),
                )
                - 1
            )
            if durable_left < 1:
                raise TimeoutError("CARE claim deadline expired while waiting for lifecycle lock")
            total_deadline = min(total_deadline, time.monotonic() + durable_left)
            old = _systemctl_show(unit)
            if old.get("LoadState") == "not-found":
                if old.get("ActiveState") != "inactive" or old.get("MainPID") != "0":
                    raise RuntimeError("absent CARE worker unit has residual process state")
            elif old.get("LoadState") == "loaded" and old.get("ActiveState") == "inactive":
                old_group = _owned_unit_cgroup(unit, old)
                if old.get("MainPID") != "0" or not _cgroup_is_empty(old_group):
                    raise RuntimeError("prior CARE worker unit generation has not drained")
            else:
                raise RuntimeError("CARE worker unit name is already active or unresolved")
            launch_seconds = int(total_deadline - time.monotonic() - _CLEANUP_RESERVE_SECONDS)
            if launch_seconds < 1:
                raise TimeoutError("CARE worker startup exhausted its reserved deadline")
            command[command.index(f"--property=RuntimeMaxSec={effective_seconds}")] = (
                f"--property=RuntimeMaxSec={launch_seconds}"
            )
            command[-1] = str(launch_seconds)
            process = subprocess.Popen(  # nosec B603 -- all arguments and module are fixed
                command,
                cwd=PROJECT_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            startup_deadline = min(total_deadline - _CLEANUP_RESERVE_SECONDS, time.monotonic() + 12)
            while time.monotonic() < startup_deadline:
                properties = _systemctl_show(unit)
                if (
                    properties.get("LoadState") == "loaded"
                    and properties.get("ActiveState") == "active"
                ):
                    invocation_id = properties.get("InvocationID", "").lower()
                    if re.fullmatch(r"[0-9a-f]{32}", invocation_id) is None:
                        raise RuntimeError("CARE worker has no exact systemd invocation id")
                    observed_cgroup = _owned_unit_cgroup(unit, properties)
                    pid = int(properties.get("MainPID", "0"))
                    if pid < 1:
                        raise RuntimeError("CARE worker unit has no main process")
                    row = _claim_row(engine, claim_id)
                    try:
                        start_ticks = _start_ticks(pid)
                        boot_id = _boot_id()
                    except (OSError, RuntimeError):
                        start_ticks, boot_id = "", ""
                    if (
                        row is not None
                        and row["calibration_id"] == calibration_id
                        and row["generation"] == generation
                        and row["state"] == "running"
                        and row["worker_pid"] == pid
                        and row["worker_start_ticks"] == start_ticks
                        and row["worker_boot_id"] == boot_id
                        and row["worker_unit"] == unit
                        and row["worker_invocation_id"] == invocation_id
                        and row["worker_cgroup"] == observed_cgroup
                    ):
                        break
                if process.poll() is not None:
                    current_id = properties.get("InvocationID", "").lower()
                    if properties.get("LoadState") == "loaded":
                        group = _owned_unit_cgroup(unit, properties)
                        drained = (
                            properties.get("ActiveState") == "inactive"
                            and properties.get("MainPID") == "0"
                            and _cgroup_is_empty(group)
                        )
                    else:
                        slice_properties = _systemctl_show(SCORER_SLICE)
                        slice_group = slice_properties.get("ControlGroup", "")
                        group = f"{slice_group.rstrip('/')}/{unit}" if slice_group else ""
                        drained = bool(group) and _cgroup_is_absent_or_empty(group)
                    if not drained:
                        raise RuntimeError("early CARE worker exit lacks exact drain proof")
                    invocation_id = (
                        current_id if re.fullmatch(r"[0-9a-f]{32}", current_id) else None
                    )
                    stdout, _stderr = process.communicate()
                    reported = _parse_worker_result(
                        stdout, calibration_id, claim_id, generation
                    )
                    row = _claim_row(engine, claim_id)
                    if row is not None and row["state"] == "succeeded":
                        return CareCellProcessResult(
                            claim_id, generation, "succeeded", invocation_id
                        )
                    if row is not None and row["state"] == "failed":
                        return CareCellProcessResult(claim_id, generation, "failed", invocation_id)
                    if not _drain_exact_claim_sandbox(claim_id):
                        return CareCellProcessResult(claim_id, generation, "pending", invocation_id)
                    if (
                        reported == "capacity_busy"
                        and row is not None
                        and row["state"] == "reserved"
                    ):
                        return _fail_drained_claim(
                            engine,
                            claim_id,
                            generation,
                            failure_code="worker_start_failure",
                            invocation_id=None,
                        )
                    if row is not None and row["state"] == "running":
                        return _fail_drained_claim(
                            engine,
                            claim_id,
                            generation,
                            failure_code="worker_crash",
                            invocation_id=str(row["worker_invocation_id"]),
                        )
                    raise RuntimeError("CARE worker exited without a bound claim state")
                time.sleep(0.05)
            if invocation_id is None:
                raise TimeoutError("CARE worker did not become active in its startup bound")
    except BaseException:
        if invocation_id is not None:
            with contextlib.suppress(Exception):
                stop_owned_scorer_unit(
                    claim_id,
                    invocation_id=invocation_id,
                    timeout_seconds=max(1, min(3, int(total_deadline - time.monotonic()))),
                )
        if process is not None and process.poll() is None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.communicate(timeout=3)
        raise
    try:
        left = total_deadline - time.monotonic()
        if left <= 0:
            raise subprocess.TimeoutExpired(command, 0)
        stdout, _stderr = process.communicate(timeout=left)
        properties = _systemctl_show(unit)
        if observed_cgroup is None:
            raise RuntimeError("CARE worker exited before cgroup ownership was recorded")
        if properties.get("LoadState") != "not-found":
            if (
                properties.get("ActiveState") != "inactive"
                or properties.get("InvocationID", "").lower() != invocation_id
                or properties.get("MainPID") != "0"
                or properties.get("ControlGroup") != observed_cgroup
                or not _cgroup_is_empty(observed_cgroup)
            ):
                raise RuntimeError("CARE worker result lacks exact generation drain proof")
        elif not _cgroup_is_absent_or_empty(observed_cgroup):
            raise RuntimeError("collected CARE worker cgroup is not empty")
        reported = _parse_worker_result(stdout, calibration_id, claim_id, generation)
        row = _claim_row(engine, claim_id)
        if row is None or row["generation"] != generation:
            raise RuntimeError("CARE worker claim disappeared after process completion")
        if row["state"] == "succeeded":
            return CareCellProcessResult(claim_id, generation, "succeeded", invocation_id)
        if row["state"] == "failed":
            return CareCellProcessResult(claim_id, generation, "failed", invocation_id)
        if reported != "failed":
            raise RuntimeError("CARE worker returned success without a durable receipt")
        if not _drain_exact_claim_sandbox(claim_id):
            return CareCellProcessResult(claim_id, generation, "pending", invocation_id)
        current_invocation = row.get("worker_invocation_id")
        if not isinstance(current_invocation, str) or current_invocation != invocation_id:
            return CareCellProcessResult(claim_id, generation, "pending", invocation_id)
        return _fail_drained_claim(
            engine,
            claim_id,
            generation,
            failure_code="worker_crash",
            invocation_id=current_invocation,
        )
    except BaseException:
        if invocation_id is not None:
            with contextlib.suppress(Exception):
                stop_owned_scorer_unit(
                    claim_id,
                    invocation_id=invocation_id,
                    timeout_seconds=max(1, min(3, int(total_deadline - time.monotonic()))),
                )
        if process is not None and process.poll() is None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.communicate(timeout=3)
        raise
