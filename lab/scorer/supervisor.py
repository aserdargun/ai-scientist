"""Run one Scorer worker inside an exact transient systemd cgroup."""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import subprocess  # nosec B404 -- only fixed systemd command vectors are used below
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from lab.scorer.credential_path import scorer_deployment_environment

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKER_MEMORY = "2G"
WORKER_CPU_QUOTA = "100%"
WORKER_TASKS = "32"
MAX_WORKER_RUNTIME_SECONDS = 600
SCORER_SLICE = "swapp-ai-scientist-scorer.slice"
SYSTEMCTL = "/usr/bin/systemctl"
BUSCTL = "/usr/bin/busctl"
SYSTEMD_RUN = "/usr/bin/systemd-run"
SLICE_MEMORY_BYTES = 2 * 1024**3
SLICE_CPU_QUOTA_USEC = 1_000_000
SLICE_TASKS = 32
SCORER_RUNTIME_DIRECTORY = "swapp-ai-scientist"


@dataclass(frozen=True, slots=True)
class ScorerProcessResult:
    """Bounded metadata returned from one isolated Scorer service."""

    job_id: UUID
    unit: str
    exit_code: int
    result: dict[str, str] | None
    stderr_tail: str | None = None
    invocation_id: str | None = None
    attempt: int | None = None


def _completed_worker_metadata(
    job_id: UUID,
    unit: str,
    result: dict[str, str],
    *,
    observed_invocation_id: str | None = None,
) -> tuple[str, int]:
    """Validate completed stdout identity; the caller separately proves waiter exit zero."""
    expected_unit = f"swapp-ai-scientist-scorer-{job_id.hex}.service"
    invocation = result.get("worker_invocation_id", "")
    attempt_text = result.get("worker_claim_attempt", "")
    if (
        result.get("state") != "completed"
        or result.get("job_id") != str(job_id)
        or unit != expected_unit
        or result.get("worker_unit") != expected_unit
        or re.fullmatch(r"[0-9a-f]{32}", invocation) is None
        or re.fullmatch(r"[1-9][0-9]*", attempt_text) is None
        or (observed_invocation_id is not None and invocation != observed_invocation_id)
    ):
        raise RuntimeError("completed Scorer worker identity does not match its launch")
    try:
        attempt = int(attempt_text)
    except ValueError as exc:
        raise RuntimeError("completed Scorer worker attempt is invalid") from exc
    return invocation, attempt


def _systemctl_show(unit: str, *, timeout_seconds: float = 5) -> dict[str, str]:
    """Read exact unit properties; manager errors are never treated as absence."""
    result = subprocess.run(  # nosec B603 -- fixed systemctl argv and validated unit identity
        [
            SYSTEMCTL,
            "--user",
            "show",
            "--no-pager",
            "--property=LoadState",
            "--property=ActiveState",
            "--property=InvocationID",
            "--property=ControlGroup",
            "--property=MainPID",
            unit,
        ],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("systemd unit inspection failed")
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


@contextmanager
def _job_lifecycle_lock(job_id: UUID, *, timeout_seconds: float = 30.0) -> Iterator[None]:
    """Serialize exact-unit start/stop transitions for one durable job identity."""
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 0 < timeout_seconds <= 600
    ):
        raise ValueError("Scorer lifecycle lock timeout must be greater than zero and at most 600")
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    if runtime.is_symlink() or not runtime.is_dir():
        raise RuntimeError("user runtime directory is unavailable")
    runtime_stat = runtime.stat()
    if runtime_stat.st_uid != os.getuid() or stat.S_IMODE(runtime_stat.st_mode) & 0o077:
        raise RuntimeError("user runtime directory ownership or mode is unsafe")
    private_root = runtime / SCORER_RUNTIME_DIRECTORY
    private_root.mkdir(mode=0o700, exist_ok=True)
    root_info = private_root.lstat()
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or root_info.st_uid != os.getuid()
        or stat.S_IMODE(root_info.st_mode) & 0o077
    ):
        raise RuntimeError("Scorer runtime directory is unsafe")
    directory = private_root / "job-lifecycle"
    directory.mkdir(mode=0o700, exist_ok=True)
    directory_info = directory.lstat()
    if (
        not stat.S_ISDIR(directory_info.st_mode)
        or directory_info.st_uid != os.getuid()
        or stat.S_IMODE(directory_info.st_mode) & 0o077
    ):
        raise RuntimeError("Scorer lifecycle lock directory is unsafe")
    path = directory / f"{job_id.hex}.lock"
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise RuntimeError("Scorer lifecycle lock file is unsafe")
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Scorer lifecycle lock deadline expired") from None
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        yield
    finally:
        os.close(descriptor)


def _expected_unit_cgroup(unit: str, *, timeout_seconds: float = 5) -> str:
    """Build one exact unit path under the configured aggregate Scorer slice."""
    if not re.fullmatch(r"swapp-ai-scientist-scorer-[0-9a-f]{32}\.service", unit):
        raise RuntimeError("Scorer unit name is invalid")
    slice_properties = _systemctl_show(SCORER_SLICE, timeout_seconds=timeout_seconds)
    slice_group = slice_properties.get("ControlGroup", "")
    if (
        slice_properties.get("LoadState") != "loaded"
        or not slice_group.startswith("/")
        or ".." in Path(slice_group).parts
    ):
        raise RuntimeError("aggregate Scorer slice cgroup is invalid")
    return f"{slice_group.rstrip('/')}/{unit}"


def _owned_unit_cgroup(unit: str, properties: dict[str, str]) -> str:
    """Require a unit to live under the aggregate Scorer resource slice."""
    control_group = properties.get("ControlGroup", "")
    if control_group != _expected_unit_cgroup(unit):
        raise RuntimeError("Scorer unit is outside the aggregate resource slice")
    return control_group


def _cgroup_is_empty(control_group: str) -> bool:
    path = Path("/sys/fs/cgroup") / control_group.lstrip("/")
    if path.is_symlink() or not path.is_dir():
        return False
    try:
        processes = (path / "cgroup.procs").read_text(encoding="ascii").split()
        event_lines = (path / "cgroup.events").read_text(encoding="ascii").splitlines()
        events = dict(line.split() for line in event_lines)
    except (OSError, ValueError):
        return False
    return not processes and events.get("populated") == "0"


def _cgroup_is_absent_or_empty(control_group: str) -> bool:
    path = Path("/sys/fs/cgroup") / control_group.lstrip("/")
    if path.is_symlink():
        return False
    return not path.exists() or _cgroup_is_empty(control_group)


def stop_owned_scorer_unit(job_id: UUID, *, invocation_id: str, timeout_seconds: int = 10) -> bool:
    """Stop only the recorded generation and require its exact cgroup to drain."""
    if re.fullmatch(r"[0-9a-f]{32}", invocation_id) is None:
        raise ValueError("Scorer invocation id must be lowercase 128-bit hex")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 1 <= timeout_seconds <= 30
    ):
        raise ValueError("Scorer drain timeout must be 1..30 seconds")
    with _job_lifecycle_lock(job_id):
        return _stop_owned_scorer_unit_locked(
            job_id, invocation_id=invocation_id, timeout_seconds=timeout_seconds
        )


def _stop_owned_scorer_unit_locked(
    job_id: UUID, *, invocation_id: str, timeout_seconds: int
) -> bool:
    """Drain one generation while caller already holds its lifecycle lock."""
    unit = f"swapp-ai-scientist-scorer-{job_id.hex}.service"
    properties = _systemctl_show(unit)
    if properties.get("LoadState") == "not-found":
        return (
            properties.get("ActiveState") == "inactive"
            and properties.get("MainPID") == "0"
            and properties.get("InvocationID") == ""
            and properties.get("ControlGroup") == ""
            and _cgroup_is_absent_or_empty(_expected_unit_cgroup(unit))
        )
    if properties.get("LoadState") != "loaded":
        raise RuntimeError("Scorer unit has an unknown systemd load state")
    if properties.get("InvocationID") != invocation_id:
        raise RuntimeError("Scorer unit generation changed before drain")
    control_group = _owned_unit_cgroup(unit, properties)
    if properties.get("ActiveState") == "active":
        stopped = subprocess.run(  # nosec B603 -- exact validated transient unit, no shell
            [SYSTEMCTL, "--user", "stop", unit],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        if stopped.returncode != 0:
            raise RuntimeError("could not stop the exact owned Scorer unit")
    final = _systemctl_show(unit)
    if final.get("LoadState") == "not-found":
        return (
            final.get("ActiveState") == "inactive"
            and final.get("MainPID") == "0"
            and final.get("InvocationID") == ""
            and final.get("ControlGroup") == ""
            and _cgroup_is_absent_or_empty(control_group)
        )
    if (
        final.get("LoadState") != "loaded"
        or final.get("ActiveState") != "inactive"
        or final.get("InvocationID") != invocation_id
        or final.get("MainPID") != "0"
        or final.get("ControlGroup") != control_group
    ):
        raise RuntimeError("Scorer unit is not the same inactive generation")
    return _cgroup_is_empty(control_group)


def _ensure_aggregate_slice() -> None:
    """Create or verify the shared Scorer cgroup before any worker starts."""
    probe = subprocess.run(  # nosec B603 -- fixed systemctl argv; no shell or user command
        [SYSTEMCTL, "--user", "show", SCORER_SLICE, "--property=ControlGroup", "--value"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if probe.returncode != 0 or not probe.stdout.strip():
        created = subprocess.run(  # nosec B603 -- fixed busctl argv; no shell or user command
            [
                BUSCTL,
                "--user",
                "call",
                "org.freedesktop.systemd1",
                "/org/freedesktop/systemd1",
                "org.freedesktop.systemd1.Manager",
                "StartTransientUnit",
                "ssa(sv)a(sa(sv))",
                SCORER_SLICE,
                "fail",
                "7",
                "Description",
                "s",
                "SWAPP AI Scientist aggregate Scorer budget",
                "MemoryMax",
                "t",
                str(SLICE_MEMORY_BYTES),
                "MemorySwapMax",
                "t",
                "0",
                "CPUQuotaPerSecUSec",
                "t",
                str(SLICE_CPU_QUOTA_USEC),
                "TasksMax",
                "t",
                str(SLICE_TASKS),
                "CollectMode",
                "s",
                "inactive-or-failed",
                "CPUAccounting",
                "b",
                "true",
                "0",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if created.returncode != 0:
            # A concurrent dispatcher may have created the same fixed slice.
            # The cgroup values below decide whether it is safe to use.
            probe = subprocess.run(  # nosec B603 -- fixed systemctl argv; no shell or user command
                [SYSTEMCTL, "--user", "show", SCORER_SLICE, "--property=ControlGroup", "--value"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if probe.returncode != 0 or not probe.stdout.strip():
                raise RuntimeError("could not create the aggregate Scorer slice")
        else:
            probe = subprocess.run(  # nosec B603 -- fixed systemctl argv; no shell or user command
                [SYSTEMCTL, "--user", "show", SCORER_SLICE, "--property=ControlGroup", "--value"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if probe.returncode != 0 or not probe.stdout.strip():
                raise RuntimeError("aggregate Scorer slice did not become active")
    control_group = probe.stdout.strip()
    if not control_group.startswith("/") or ".." in Path(control_group).parts:
        raise RuntimeError("aggregate Scorer slice has an invalid cgroup path")
    cgroup = Path("/sys/fs/cgroup") / control_group.lstrip("/")
    expected = {
        "memory.max": str(SLICE_MEMORY_BYTES),
        "memory.swap.max": "0",
        "cpu.max": f"{SLICE_CPU_QUOTA_USEC // 10} 100000",
        "pids.max": str(SLICE_TASKS),
    }
    for name, value in expected.items():
        try:
            actual = (cgroup / name).read_text(encoding="ascii").strip()
        except OSError as exc:
            raise RuntimeError("aggregate Scorer cgroup controls are unavailable") from exc
        if actual != value:
            raise RuntimeError(f"aggregate Scorer cgroup has an unexpected {name} limit")


def _artifact_root_environment(artifact_root: Path | None) -> list[str]:
    """Validate an explicit private deployment root before provisioning its worker."""
    if artifact_root is None:
        return []
    resolved = artifact_root.resolve(strict=True)
    runtime = (PROJECT_ROOT / "data/runtime").resolve(strict=True)
    info = resolved.stat()
    if (
        artifact_root.absolute() != resolved
        or not stat.S_ISDIR(info.st_mode)
        or not resolved.is_relative_to(runtime)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("Scorer artifact root must be a private project runtime directory")
    return [f"--setenv=LAB_ARTIFACT_ROOT={resolved}"]


def run_scorer_process(
    job_id: UUID,
    *,
    remaining_seconds: int = MAX_WORKER_RUNTIME_SECONDS,
    artifact_root: Path | None = None,
    _recovery_expected_invocation_id: str | None = None,
) -> ScorerProcessResult:
    """Start one fresh CPU-bound Python process with deadline and resource limits."""
    if (
        isinstance(remaining_seconds, bool)
        or not isinstance(remaining_seconds, int)
        or not 1 <= remaining_seconds <= MAX_WORKER_RUNTIME_SECONDS
    ):
        raise ValueError("Scorer deadline must be 1..600 remaining task seconds")
    if (
        _recovery_expected_invocation_id is not None
        and re.fullmatch(r"[0-9a-f]{32}", _recovery_expected_invocation_id) is None
    ):
        raise ValueError("recovery target must be a lowercase systemd InvocationID")
    credential_environment = scorer_deployment_environment()
    root_environment = _artifact_root_environment(artifact_root)
    unit = f"swapp-ai-scientist-scorer-{job_id.hex}.service"
    _ensure_aggregate_slice()
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
        f"--property=CPUQuota={WORKER_CPU_QUOTA}",
        f"--property=TasksMax={WORKER_TASKS}",
        f"--property=RuntimeMaxSec={remaining_seconds}",
        "--property=TimeoutStopSec=3",
        "--property=KillMode=control-group",
        "--property=OOMPolicy=stop",
        "--property=NoNewPrivileges=yes",
        f"--working-directory={PROJECT_ROOT}",
        "--setenv=CUDA_VISIBLE_DEVICES=",
        "--setenv=OPENBLAS_NUM_THREADS=2",
        "--setenv=OMP_NUM_THREADS=2",
        "--setenv=MKL_NUM_THREADS=2",
        "--setenv=NUMEXPR_NUM_THREADS=2",
        "--setenv=BLIS_NUM_THREADS=2",
        *root_environment,
        *credential_environment,
        str(PROJECT_ROOT / ".venv/bin/python"),
        "-m",
        "lab.scorer.worker",
    ]
    if _recovery_expected_invocation_id is None:
        command.extend(["--job-id", str(job_id)])
    else:
        command.extend(
            [
                "--recover-job-id",
                str(job_id),
                "--expected-claim-invocation-id",
                _recovery_expected_invocation_id,
            ]
        )
    process: subprocess.Popen[str] | None = None
    invocation_id: str | None = None
    try:
        with _job_lifecycle_lock(job_id):
            current = _systemctl_show(unit)
            if current.get("LoadState") == "loaded" and current.get("ActiveState") == "active":
                if _recovery_expected_invocation_id is None:
                    raise RuntimeError("the exact Scorer job unit is already active")
                if current.get("InvocationID") != _recovery_expected_invocation_id:
                    raise RuntimeError("active Scorer generation differs from recovery target")
                if not _stop_owned_scorer_unit_locked(
                    job_id,
                    invocation_id=_recovery_expected_invocation_id,
                    timeout_seconds=30,
                ):
                    raise RuntimeError("Scorer recovery could not prove the old unit drained")
                current = _systemctl_show(unit)
            if current.get("LoadState") not in {"not-found", "loaded"}:
                raise RuntimeError("Scorer job unit has an unknown systemd load state")
            if current.get("LoadState") == "not-found":
                if (
                    current.get("ActiveState") != "inactive"
                    or current.get("MainPID") != "0"
                    or current.get("InvocationID") != ""
                    or current.get("ControlGroup") != ""
                    or not _cgroup_is_absent_or_empty(_expected_unit_cgroup(unit))
                ):
                    raise RuntimeError("absent Scorer unit has residual process state")
            elif current.get("ActiveState") == "inactive":
                old_invocation = current.get("InvocationID", "")
                old_group = _owned_unit_cgroup(unit, current)
                if (
                    re.fullmatch(r"[0-9a-f]{32}", old_invocation) is None
                    or current.get("MainPID") != "0"
                    or not _cgroup_is_empty(old_group)
                ):
                    raise RuntimeError("inactive Scorer unit generation is not fully drained")
            else:
                raise RuntimeError("Scorer job unit is not in a startable state")
            process = subprocess.Popen(  # nosec B603 -- fixed executable/argv; UUID-typed ID
                command,
                cwd=PROJECT_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            start_deadline = time.monotonic() + 15
            while time.monotonic() < start_deadline:
                if process.poll() is not None:
                    stdout, stderr = process.communicate()
                    early_result: dict[str, str] | None = None
                    if process.returncode == 0:
                        parsed = json.loads(stdout)
                        if isinstance(parsed, dict) and all(
                            isinstance(key, str) and isinstance(value, str)
                            for key, value in parsed.items()
                        ):
                            early_result = parsed
                        else:
                            raise RuntimeError("Scorer worker returned an invalid status record")
                    early_invocation: str | None = None
                    early_attempt: int | None = None
                    if (
                        process.returncode == 0
                        and _recovery_expected_invocation_id is None
                        and early_result is not None
                        and early_result.get("state") == "completed"
                    ):
                        early_invocation, early_attempt = _completed_worker_metadata(
                            job_id, unit, early_result
                        )
                    return ScorerProcessResult(
                        job_id,
                        unit,
                        process.returncode,
                        early_result,
                        stderr[-1000:] or None,
                        early_invocation,
                        early_attempt,
                    )
                state = _systemctl_show(unit)
                if state.get("LoadState") == "loaded" and state.get("ActiveState") == "active":
                    observed = state.get("InvocationID")
                    if re.fullmatch(r"[0-9a-f]{32}", observed or "") is None:
                        raise RuntimeError("active Scorer unit has no valid InvocationID")
                    if (
                        _recovery_expected_invocation_id is not None
                        and observed == _recovery_expected_invocation_id
                    ):
                        raise RuntimeError("recovery worker reused the drained claim generation")
                    _owned_unit_cgroup(unit, state)
                    invocation_id = observed
                    break
                time.sleep(0.05)
            if invocation_id is None:
                raise RuntimeError("Scorer job unit did not become active within its start bound")
        try:
            stdout, stderr = process.communicate(timeout=remaining_seconds + 30)
        except subprocess.TimeoutExpired as exc:
            if not stop_owned_scorer_unit(job_id, invocation_id=invocation_id, timeout_seconds=30):
                raise RuntimeError("timed-out Scorer unit could not be proven drained") from exc
            raise RuntimeError("Scorer supervisor exceeded its bounded wait") from exc
        result: dict[str, str] | None = None
        if process.returncode == 0:
            parsed = json.loads(stdout)
            if isinstance(parsed, dict) and all(
                isinstance(key, str) and isinstance(value, str) for key, value in parsed.items()
            ):
                result = parsed
            else:
                raise RuntimeError("Scorer worker returned an invalid bounded status record")
        attempt: int | None = None
        if (
            process.returncode == 0
            and _recovery_expected_invocation_id is None
            and result is not None
            and result.get("state") == "completed"
        ):
            _, attempt = _completed_worker_metadata(
                job_id, unit, result, observed_invocation_id=invocation_id
            )
        return ScorerProcessResult(
            job_id, unit, process.returncode, result, stderr[-1000:] or None, invocation_id, attempt
        )
    except BaseException:
        # If the generation was observed, stop only that exact unit. Before
        # observation, never guess which generation owns the fixed unit name.
        # In either case reap our systemd-run client so no host-side child leaks.
        if process is not None and process.poll() is None:
            if invocation_id is not None:
                try:
                    stop_owned_scorer_unit(job_id, invocation_id=invocation_id, timeout_seconds=30)
                except (OSError, RuntimeError, subprocess.TimeoutExpired):
                    pass
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
        raise


def run_scorer_recovery_process(
    job_id: UUID,
    *,
    expected_claim_invocation_id: str,
    remaining_seconds: int = MAX_WORKER_RUNTIME_SECONDS,
    artifact_root: Path | None = None,
) -> ScorerProcessResult:
    """Drain a recorded Scorer generation, then start a fresh same-unit writer."""
    return run_scorer_process(
        job_id,
        remaining_seconds=remaining_seconds,
        artifact_root=artifact_root,
        _recovery_expected_invocation_id=expected_claim_invocation_id,
    )


def scorer_job_unit_is_quiescent(
    job_id: UUID,
    *,
    expected_invocation_id: str | None,
    timeout_seconds: int = 30,
) -> bool:
    """Verify exact generation and empty cgroup for a terminal Scorer job."""
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 1 <= timeout_seconds <= 30
    ):
        raise ValueError("Scorer quiescence timeout must be 1..30 seconds")
    if (
        expected_invocation_id is not None
        and re.fullmatch(r"[0-9a-f]{32}", expected_invocation_id) is None
    ):
        raise ValueError("Scorer invocation id must be lowercase 128-bit hex")
    unit = f"swapp-ai-scientist-scorer-{job_id.hex}.service"
    deadline = time.monotonic() + timeout_seconds
    try:
        lock_remaining = deadline - time.monotonic()
        if lock_remaining <= 0:
            return False
        with _job_lifecycle_lock(job_id, timeout_seconds=lock_remaining):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            properties = _systemctl_show(unit, timeout_seconds=min(5, remaining))
            if properties.get("LoadState") == "not-found":
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                expected_group = _expected_unit_cgroup(unit, timeout_seconds=min(5, remaining))
                return (
                    properties.get("ActiveState") == "inactive"
                    and properties.get("MainPID") == "0"
                    and properties.get("InvocationID") == ""
                    and properties.get("ControlGroup") == ""
                    and _cgroup_is_absent_or_empty(expected_group)
                )
            if (
                properties.get("LoadState") != "loaded"
                or expected_invocation_id is None
                or properties.get("InvocationID") != expected_invocation_id
                or properties.get("ActiveState") not in {"inactive", "failed"}
                or properties.get("MainPID") != "0"
            ):
                return False
            control_group = properties.get("ControlGroup", "")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            expected_group = _expected_unit_cgroup(unit, timeout_seconds=min(5, remaining))
            # systemd may keep the exact inactive generation loaded after its
            # cgroup has been collected. In that case ControlGroup is empty;
            # accept it only when the trusted expected path is absent/empty.
            if control_group == "":
                return _cgroup_is_absent_or_empty(expected_group)
            if control_group != expected_group:
                return False
            return _cgroup_is_empty(control_group)
    except (OSError, RuntimeError, TimeoutError):
        return False


def run_scorer_finalize_process(
    run_id: UUID,
    *,
    admitted_generation: int,
    execution_sha256: str,
    remaining_seconds: int = MAX_WORKER_RUNTIME_SECONDS,
    artifact_root: Path | None = None,
) -> ScorerProcessResult:
    """Run a separately supervised Scorer finalization after plan sealing."""
    if (
        isinstance(admitted_generation, bool)
        or not isinstance(admitted_generation, int)
        or admitted_generation < 1
        or not isinstance(execution_sha256, str)
        or len(execution_sha256) != 64
        or any(character not in "0123456789abcdef" for character in execution_sha256)
    ):
        raise ValueError("Scorer finalization requires a captured generation/hash pair")
    return _run_scorer_finalize_process(
        run_id,
        admitted_generation=admitted_generation,
        execution_sha256=execution_sha256,
        empty_baseline_stop=False,
        artifact_root=artifact_root,
        remaining_seconds=remaining_seconds,
    )


def run_scorer_empty_baseline_stop_process(
    run_id: UUID, *, remaining_seconds: int = MAX_WORKER_RUNTIME_SECONDS
) -> ScorerProcessResult:
    """Finalize only the verified ownerless, zero-work baseline stop path."""
    return _run_scorer_finalize_process(
        run_id,
        admitted_generation=None,
        execution_sha256=None,
        empty_baseline_stop=True,
        remaining_seconds=remaining_seconds,
    )


def _run_scorer_finalize_process(
    run_id: UUID,
    *,
    admitted_generation: int | None,
    execution_sha256: str | None,
    empty_baseline_stop: bool,
    remaining_seconds: int,
    artifact_root: Path | None = None,
) -> ScorerProcessResult:
    if (
        isinstance(remaining_seconds, bool)
        or not isinstance(remaining_seconds, int)
        or not 1 <= remaining_seconds <= MAX_WORKER_RUNTIME_SECONDS
    ):
        raise ValueError("Scorer deadline must be 1..600 remaining task seconds")
    deadline = time.monotonic() + remaining_seconds
    # API repeat-stop callbacks and Director recovery share this canonical unit.
    # Waiting consumes the caller's original budget; it never grants another window.
    with _job_lifecycle_lock(run_id, timeout_seconds=remaining_seconds):
        remaining = int(deadline - time.monotonic())
        if remaining < 1:
            raise TimeoutError("Scorer finalizer lifecycle deadline expired")
        return _run_scorer_finalize_process_locked(
            run_id,
            admitted_generation=admitted_generation,
            execution_sha256=execution_sha256,
            empty_baseline_stop=empty_baseline_stop,
            remaining_seconds=remaining,
            artifact_root=artifact_root,
            deadline=deadline,
        )


def _run_scorer_finalize_process_locked(
    run_id: UUID,
    *,
    admitted_generation: int | None,
    execution_sha256: str | None,
    empty_baseline_stop: bool,
    remaining_seconds: int,
    artifact_root: Path | None,
    deadline: float,
) -> ScorerProcessResult:
    """Start and await the unchanged canonical finalizer while owning its run lock."""
    credential_environment = scorer_deployment_environment()
    root_environment = _artifact_root_environment(artifact_root)
    _ensure_aggregate_slice()
    remaining_seconds = min(remaining_seconds, int(deadline - time.monotonic()))
    if remaining_seconds < 1:
        raise TimeoutError("Scorer finalizer deadline expired before unit start")
    unit = f"swapp-ai-scientist-finalize-{run_id.hex}.service"
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
        f"--property=CPUQuota={WORKER_CPU_QUOTA}",
        f"--property=TasksMax={WORKER_TASKS}",
        f"--property=RuntimeMaxSec={remaining_seconds}",
        "--property=TimeoutStopSec=3",
        "--property=KillMode=control-group",
        "--property=OOMPolicy=stop",
        "--property=NoNewPrivileges=yes",
        f"--working-directory={PROJECT_ROOT}",
        "--setenv=CUDA_VISIBLE_DEVICES=",
        "--setenv=OPENBLAS_NUM_THREADS=2",
        "--setenv=OMP_NUM_THREADS=2",
        "--setenv=MKL_NUM_THREADS=2",
        "--setenv=NUMEXPR_NUM_THREADS=2",
        "--setenv=BLIS_NUM_THREADS=2",
        *root_environment,
        *credential_environment,
        str(PROJECT_ROOT / ".venv/bin/python"),
        "-m",
        "lab.scorer.worker",
        "--finalize-run-id",
        str(run_id),
    ]
    if empty_baseline_stop:
        command.append("--finalize-empty-baseline-stop")
    else:
        command.extend(
            [
                "--finalize-admitted-generation",
                str(admitted_generation),
                "--finalize-execution-sha256",
                str(execution_sha256),
            ]
        )
    completed = subprocess.run(  # nosec B603 -- fixed executable/argv; IDs are UUID-typed
        command,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=remaining_seconds + 30,
        check=False,
    )
    result: dict[str, str] | None = None
    if completed.returncode == 0:
        parsed = json.loads(completed.stdout)
        if isinstance(parsed, dict) and all(
            isinstance(key, str) and isinstance(value, str) for key, value in parsed.items()
        ):
            result = parsed
        else:
            raise RuntimeError("Scorer finalizer returned an invalid bounded status record")
    return ScorerProcessResult(
        run_id, unit, completed.returncode, result, completed.stderr[-1000:] or None
    )


def run_mode_snapshot_install(snapshot_sha256: str) -> dict[str, str]:
    """Run only the fixed immutable-snapshot installer, bounded to the Scorer slot."""
    if re.fullmatch(r"[0-9a-f]{64}", snapshot_sha256) is None:
        raise ValueError("invalid snapshot identity")
    credential_environment = scorer_deployment_environment()
    _ensure_aggregate_slice()
    unit = f"swapp-ai-scientist-mode-install-{snapshot_sha256[:32]}.service"
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
        f"--property=CPUQuota={WORKER_CPU_QUOTA}",
        f"--property=TasksMax={WORKER_TASKS}",
        "--property=RuntimeMaxSec=30",
        "--property=TimeoutStopSec=3",
        "--property=KillMode=control-group",
        "--property=OOMPolicy=stop",
        "--property=NoNewPrivileges=yes",
        f"--working-directory={PROJECT_ROOT}",
        "--setenv=CUDA_VISIBLE_DEVICES=",
        "--setenv=OPENBLAS_NUM_THREADS=1",
        "--setenv=OMP_NUM_THREADS=1",
        "--setenv=MKL_NUM_THREADS=1",
        *credential_environment,
        str(PROJECT_ROOT / ".venv/bin/python"),
        "-m",
        "lab.scorer.mode_snapshot_worker",
        "--snapshot-sha256",
        snapshot_sha256,
    ]
    completed = subprocess.run(  # nosec B603 -- fixed worker and validated SHA, no shell
        command, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=45, check=False
    )
    if completed.returncode != 0 or len(completed.stdout) > 2048:
        raise RuntimeError("bounded Scorer snapshot installer did not complete")
    result = json.loads(completed.stdout)
    if not isinstance(result, dict) or result.get("state") not in {"installed", "capacity_busy"}:
        raise RuntimeError("invalid Scorer installation receipt")
    return result
