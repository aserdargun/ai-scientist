"""Local Docker runner with separate bounded fit/score containers."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import math
import os
import shutil
import signal
import stat
import subprocess  # nosec B404
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from harness.contracts import FitContext
from lab.sandbox.syscall_observer import AUDIT_EXIT, AUDIT_MARKER, OBSERVER_EXIT

Phase = Literal["fit", "score", "guard", "mode_stream_fit", "mode_stream_predict"]
MAX_CODE_BYTES = 1 * 1024 * 1024
MAX_INPUT_BYTES = 128 * 1024 * 1024
MAX_ARTIFACT_INPUT_BYTES = 512 * 1024 * 1024
MAX_OUTPUT_BYTES = 256 * 1024 * 1024
MAX_LOG_BYTES = 2 * 1024 * 1024
MIN_FREE_DISK_BYTES = 20 * 1024**3
ARTIFACT_MAGIC = b"SWAPP-SANDBOX-ARTIFACTS-V1\n"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIRECTORY = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
DEFAULT_ADMISSION_LOCK = RUNTIME_DIRECTORY / "swapp-ai-scientist/sandbox-p1.lock"
OWNED_CONTAINER_LABEL = "swapp.ai-scientist.sandbox.owner"
try:
    from importlib.resources import files

    DEFAULT_SANDBOX_IMAGE = (
        files("lab.sandbox").joinpath("image.lock").read_text(encoding="ascii").strip()
    )
except (FileNotFoundError, ModuleNotFoundError):
    DEFAULT_SANDBOX_IMAGE = (
        (PROJECT_ROOT / "ops/sandbox-image.lock").read_text(encoding="ascii").strip()
    )


def _process_start_time(pid: int) -> str:
    """Read Linux proc start ticks to distinguish PID reuse."""
    return _process_identity(pid)[1]


def _process_identity(pid: int) -> tuple[str, str]:
    """Read Linux proc state and start ticks from a single stat snapshot."""
    stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    fields = stat_line[stat_line.rfind(")") + 2 :].split()
    return fields[0], fields[19]


def _same_process_identity(pid: int, start_time: str, boot_id: str) -> bool:
    """Return true only while the exact recorded host process still exists."""
    try:
        current_boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError as exc:
        raise SandboxRunError("cannot verify sandbox owner boot identity") from exc
    if current_boot != boot_id:
        return False
    try:
        state, current_start = _process_identity(pid)
    except ProcessLookupError:
        return False
    except FileNotFoundError:
        return False
    except (OSError, IndexError, ValueError) as exc:
        raise SandboxRunError("cannot verify sandbox owner process identity") from exc
    return state != "Z" and current_start == start_time


class SandboxRunError(RuntimeError):
    """A sandbox execution failed or violated a resource/output limit."""

    def __init__(self, reason: str, *, exit_code: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.exit_code = exit_code


@dataclass(frozen=True, slots=True)
class SandboxProfile:
    """Hard limits for a single phase; values cannot exceed the M0 P=1 cap."""

    memory_bytes: int = 4 * 1024**3
    cpus: float = 2.0
    pids: int = 128
    timeout_seconds: int = 600
    output_bytes: int = MAX_OUTPUT_BYTES
    log_bytes: int = MAX_LOG_BYTES

    def __post_init__(self) -> None:
        if not 1 <= self.memory_bytes <= 4 * 1024**3:
            raise ValueError("sandbox memory must be within the 4 GiB P=1 cap")
        if not 0 < self.cpus <= 2:
            raise ValueError("sandbox CPU budget must be within the 2 CPU P=1 cap")
        if not 1 <= self.pids <= 128:
            raise ValueError("sandbox PID budget must be in [1, 128]")
        if not 1 <= self.timeout_seconds <= 600:
            raise ValueError("sandbox phase timeout must be in [1, 600] seconds")
        if not 1 <= self.output_bytes <= MAX_OUTPUT_BYTES:
            raise ValueError("sandbox output budget exceeds 256 MiB")
        if not 1 <= self.log_bytes <= MAX_LOG_BYTES:
            raise ValueError("sandbox log budget exceeds 2 MiB")


@dataclass(frozen=True, slots=True)
class OutputArtifact:
    """Verified byte artifact emitted by the trusted wrapper."""

    name: str
    sha256: str
    size_bytes: int
    content: bytes


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """Completed isolated phase result with decoded artifact payloads."""

    phase: Phase
    exit_code: int
    stdout: bytes
    stderr: bytes
    artifacts: tuple[OutputArtifact, ...]
    elapsed_seconds: float
    container_name: str


class LocalDockerRunner:
    """Run candidate code with no network, GPU, host data, or shared phase state."""

    def __init__(
        self,
        *,
        image: str,
        work_root: Path,
        profile: SandboxProfile | None = None,
        docker_binary: str = "docker",
        admission_lock: Path = DEFAULT_ADMISSION_LOCK,
    ) -> None:
        if "@sha256:" in image:
            digest = image.rsplit("@sha256:", 1)[1]
        elif image.startswith("sha256:"):
            digest = image.removeprefix("sha256:")
        else:
            raise ValueError("sandbox image must be pinned by a full sha256 image digest")
        if image != DEFAULT_SANDBOX_IMAGE:
            raise ValueError("sandbox image digest differs from the trusted image lock")
        if len(digest) != 64 or not all(ch in "0123456789abcdef" for ch in digest):
            raise ValueError("sandbox image digest must be lowercase SHA-256 hex")
        if work_root.is_symlink():
            raise ValueError("sandbox work root cannot be a symlink")
        work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not work_root.is_dir():
            raise ValueError("sandbox work root must be a directory")
        os.chmod(work_root, 0o700)
        self.image = image
        self.work_root = work_root.resolve(strict=True)
        self.profile = profile or SandboxProfile()
        self.docker_binary = docker_binary
        if admission_lock.is_symlink():
            raise ValueError("sandbox admission lock cannot be a symlink")
        admission_lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(admission_lock.parent, 0o700)
        self.admission_lock = admission_lock
        self.admission_marker = admission_lock.with_suffix(".intent")
        self.owner_label = OWNED_CONTAINER_LABEL

    def run_phase(
        self,
        *,
        phase: Phase,
        candidate_source: bytes,
        arrow_input: bytes,
        fit_artifact: bytes | None = None,
        contract_context: FitContext | None = None,
        trusted_baseline_name: str | None = None,
        timeout_seconds: int | None = None,
        stream_context: dict[str, Any] | None = None,
        deadline: float | None = None,
        admission_check: Callable[[], None] | None = None,
    ) -> SandboxResult:
        """Execute one phase; candidate pickle bytes are only mounted in-container."""
        if phase not in ("fit", "score", "guard", "mode_stream_fit", "mode_stream_predict"):
            raise ValueError("unsupported sandbox phase")
        if not candidate_source or len(candidate_source) > MAX_CODE_BYTES:
            raise ValueError("candidate source must be non-empty and at most 1 MiB")
        if len(arrow_input) > MAX_INPUT_BYTES:
            raise ValueError("Arrow input exceeds 128 MiB")
        if fit_artifact is not None and len(fit_artifact) > MAX_ARTIFACT_INPUT_BYTES:
            raise ValueError("fit artifact exceeds 512 MiB")
        if phase in {"score", "mode_stream_predict"} and fit_artifact is None:
            raise ValueError("score phase requires a fit artifact")
        if contract_context is not None and phase not in {
            "fit",
            "score",
            "mode_stream_fit",
            "mode_stream_predict",
        }:
            raise ValueError("typed contract execution supports fit and score phases only")
        if phase in {"mode_stream_fit", "mode_stream_predict"}:
            from lab.operating_modes.candidate import candidate_source as mode_source
            from lab.operating_modes.stream import MAX_CHUNK_BYTES, MAX_TRAIN_BYTES
            from lab.sandbox.mode_stream import MAX_FIT_ARTIFACT_BYTES, StreamPhaseContext

            binding = StreamPhaseContext.model_validate(stream_context)
            if (
                contract_context is None
                or candidate_source != mode_source(binding.configuration)
                or (phase == "mode_stream_fit") != (binding.fit_artifact_sha256 is None)
                or phase == "mode_stream_fit"
                and fit_artifact is not None
                or len(arrow_input)
                > (MAX_TRAIN_BYTES if phase == "mode_stream_fit" else MAX_CHUNK_BYTES)
                or fit_artifact is not None
                and (
                    len(fit_artifact) > MAX_FIT_ARTIFACT_BYTES
                    or hashlib.sha256(fit_artifact).hexdigest() != binding.fit_artifact_sha256
                )
            ):
                raise ValueError("stream phase requires exact source and bounded frozen inputs")
        elif stream_context is not None:
            raise ValueError("stream context is only valid for explicit stream phases")
        if trusted_baseline_name is not None:
            from lab.director.baselines import baseline_candidate_source

            if (
                contract_context is None
                or phase not in {"fit", "score"}
                or candidate_source != baseline_candidate_source(trusted_baseline_name)
            ):
                raise ValueError("trusted baseline mode requires exact allowlisted source bytes")
        phase_timeout = self.profile.timeout_seconds if timeout_seconds is None else timeout_seconds
        if (
            isinstance(phase_timeout, bool)
            or not isinstance(phase_timeout, int)
            or not 1 <= phase_timeout <= self.profile.timeout_seconds
        ):
            raise ValueError("phase timeout must be within the configured sandbox budget")
        if deadline is not None:
            if (
                isinstance(deadline, bool)
                or not isinstance(deadline, (int, float))
                or not math.isfinite(deadline)
            ):
                raise ValueError("invalid original sandbox deadline")
            deadline = min(deadline, time.monotonic() + phase_timeout)
            self._remaining_deadline(deadline)
        if admission_check is not None:
            admission_check()
        if shutil.disk_usage(self.work_root).free < MIN_FREE_DISK_BYTES:
            raise SandboxRunError("minimum 20 GiB disk reserve is not available")
        with self._admit_p1_capacity():
            if deadline is not None:
                self._remaining_deadline(deadline)
            if admission_check is not None:
                admission_check()
            return self._invoke(
                phase,
                candidate_source,
                arrow_input,
                fit_artifact,
                contract_context=contract_context,
                trusted_baseline_name=trusted_baseline_name,
                timeout_seconds=phase_timeout,
                stream_context=stream_context,
                deadline=deadline,
                admission_check=admission_check,
            )

    @staticmethod
    def _remaining_deadline(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SandboxRunError("sandbox phase timed out")
        return remaining

    @contextmanager
    def _admit_p1_capacity(self) -> Iterator[None]:
        """Hold one process-shared lock for the full P=1 container lifetime."""
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.admission_lock, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise SandboxRunError("sandbox P=1 capacity busy") from exc
                raise SandboxRunError("sandbox admission lock failed") from exc
            self._reconcile_owned_containers()
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def reconcile_stopped_run(
        self,
        run_id: UUID,
        *,
        owner_pid: int,
        owner_start_ticks: int,
        owner_boot_id: str,
        deadline: float | None = None,
    ) -> None:
        """Drain this run's recorded container intent without touching another run."""
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.admission_lock, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise SandboxRunError("sandbox P=1 capacity busy") from exc
                raise SandboxRunError("sandbox admission lock failed") from exc
            if self.admission_marker.exists():
                try:
                    intent = json.loads(self.admission_marker.read_text(encoding="ascii"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise SandboxRunError("sandbox admission intent is unreadable") from exc
                if not isinstance(intent, dict):
                    raise SandboxRunError("sandbox admission marker is invalid")
                root_stat = self.work_root.lstat()
                if (
                    self.work_root.name != str(run_id)
                    or intent.get("work_root") != str(self.work_root)
                    or intent.get("work_root_device") != root_stat.st_dev
                    or intent.get("work_root_inode") != root_stat.st_ino
                    or intent.get("owner_pid") != owner_pid
                    or intent.get("owner_start") != str(owner_start_ticks)
                    or intent.get("boot_id") != owner_boot_id
                    or owner_pid != os.getpid()
                ):
                    raise SandboxRunError("sandbox admission marker belongs to another run")
                self._reconcile_owned_containers(deadline=deadline)
                if self.admission_marker.exists():
                    raise SandboxRunError("stopped run still owns a sandbox admission marker")
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _invoke(
        self,
        phase: Phase,
        candidate_source: bytes,
        arrow_input: bytes,
        fit_artifact: bytes | None,
        *,
        contract_context: FitContext | None,
        trusted_baseline_name: str | None,
        timeout_seconds: int,
        stream_context: dict[str, Any] | None = None,
        deadline: float | None = None,
        admission_check: Callable[[], None] | None = None,
    ) -> SandboxResult:
        run_id = uuid.uuid4().hex
        container_name = f"swapp-lab-{phase}-{run_id}"
        owner_label_value = run_id
        start = time.monotonic()
        output_limit = self.profile.output_bytes
        if phase in {"mode_stream_fit", "mode_stream_predict"}:
            from lab.operating_modes.stream import MAX_STREAM_OUTPUT_BYTES
            from lab.sandbox.mode_stream import MAX_FIT_ARTIFACT_BYTES

            output_limit = min(
                output_limit,
                MAX_STREAM_OUTPUT_BYTES
                + (MAX_FIT_ARTIFACT_BYTES if phase == "mode_stream_fit" else 0),
            )
        with tempfile.TemporaryDirectory(prefix=f"{run_id}-", dir=self.work_root) as raw_dir:
            run_dir = Path(raw_dir)
            os.chmod(run_dir, 0o700)
            source_dir = run_dir / "candidate"
            source_dir.mkdir(mode=0o700)
            source_file = source_dir / "candidate.py"
            source_file.write_bytes(candidate_source)
            os.chmod(source_file, 0o444)
            # The candidate runs as UID 10001, so the bind-mounted input must be traversable.
            os.chmod(source_dir, 0o555)  # nosec B103
            mounts = [
                f"type=bind,src={source_dir},dst=/candidate,readonly",
            ]
            command = [
                self.docker_binary,
                "create",
                "--pull=never",
                "--rm",
                "--interactive",
                "--log-driver=none",
                "--name",
                container_name,
                "--label",
                f"{self.owner_label}={owner_label_value}",
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--user=10001:10001",
                "--pids-limit",
                str(self.profile.pids),
                "--cpus",
                str(self.profile.cpus),
                "--memory",
                str(self.profile.memory_bytes),
                "--memory-swap",
                str(self.profile.memory_bytes),
                "--shm-size=64m",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",  # nosec B108
                "--tmpfs",
                "/dev/shm:rw,noexec,nosuid,nodev,size=64m,mode=1777",  # nosec B108
                "--tmpfs",
                (
                    f"/output:rw,noexec,nosuid,nodev,size={output_limit},"
                    "nr_inodes=1024,uid=10001,gid=10001,mode=0700"
                ),
                "--ulimit",
                "core=0:0",
                "--ulimit",
                "fsize=268435456:268435456",
            ]
            for mount in mounts:
                command.extend(("--mount", mount))
            if fit_artifact is not None:
                artifact_dir = run_dir / "fit-artifact"
                artifact_dir.mkdir(mode=0o700)
                artifact = artifact_dir / "model.bin"
                artifact.write_bytes(fit_artifact)
                os.chmod(artifact, 0o444)
                os.chmod(artifact_dir, 0o555)  # nosec B103
                command.extend(
                    ("--mount", f"type=bind,src={artifact_dir},dst=/fit-artifact,readonly")
                )
            if contract_context is not None:
                command.extend(
                    (
                        "--env",
                        "OMP_NUM_THREADS=2",
                        "--env",
                        "OPENBLAS_NUM_THREADS=2",
                        "--env",
                        "MKL_NUM_THREADS=2",
                        "--env",
                        "NUMEXPR_NUM_THREADS=2",
                        "--env",
                        "BLIS_NUM_THREADS=2",
                        "--env",
                        "SWAPP_FIT_CONTEXT="
                        + json.dumps(
                            asdict(contract_context), sort_keys=True, separators=(",", ":")
                        ),
                    )
                )
                if trusted_baseline_name is not None:
                    command.extend(("--env", "SWAPP_TRUSTED_BASELINE=1"))
            if stream_context is not None:
                command.extend(
                    (
                        "--env",
                        "SWAPP_STREAM_CONTEXT="
                        + json.dumps(
                            stream_context, sort_keys=True, separators=(",", ":"), allow_nan=False
                        ),
                    )
                )
            if deadline is not None:
                timeout_seconds = min(timeout_seconds, int(self._remaining_deadline(deadline)))
                if timeout_seconds < 1:
                    raise SandboxRunError("sandbox phase timed out")
            command.extend(
                (
                    "--env",
                    "HOME=/tmp",
                    "--env",
                    "PYTHONHASHSEED=0",
                    "--entrypoint",
                    "python",
                    self.image,
                    "-I",
                    "/opt/swapp-ai-scientist/lab/sandbox/sandbox_wrapper.py",
                    (
                        "/opt/swapp-ai-scientist/lab/sandbox/candidate_entrypoint.py"
                        if contract_context is not None
                        else "/candidate/candidate.py"
                    ),
                    phase,
                    str(timeout_seconds),
                    str(output_limit),
                )
            )
            container_id: str | None = None
            try:
                if admission_check is not None:
                    admission_check()
                if deadline is not None:
                    self._remaining_deadline(deadline)
                self._write_admission_intent(container_name, owner_label_value, run_dir)
                container_id = (
                    self._create_container(command)
                    if deadline is None
                    else self._create_container(
                        command, timeout_seconds=min(30, self._remaining_deadline(deadline))
                    )
                )
                self._persist_container_id(container_id)
                if admission_check is not None:
                    admission_check()
                if deadline is not None:
                    self._remaining_deadline(deadline)
                attach_command = [
                    self.docker_binary,
                    "start",
                    "--attach",
                    "--interactive",
                    container_id,
                ]
                exit_code, stdout, stderr = self._run_bounded_process(
                    attach_command,
                    arrow_input,
                    timeout_seconds=timeout_seconds,
                    deadline=deadline,
                    output_limit_bytes=output_limit,
                )
            finally:
                if container_id is None:
                    self._reconcile_owned_containers()
                else:
                    self._remove_named_container(container_id)
                    self._cleanup_run_directory(run_dir, expected_root=self.work_root)
                    self._clear_admission_intent()
            if admission_check is not None:
                admission_check()
            if deadline is not None:
                self._remaining_deadline(deadline)
            artifacts = self._decode_output_frame(stdout)
        return SandboxResult(
            phase=phase,
            exit_code=exit_code,
            stdout=b"",
            stderr=stderr,
            artifacts=artifacts,
            elapsed_seconds=time.monotonic() - start,
            container_name=container_name,
        )

    def _reconcile_owned_containers(self, *, deadline: float | None = None) -> None:
        """Drain only Lab-owned orphan containers before granting the P=1 slot."""
        if not self.admission_marker.exists():
            return
        try:
            intent = json.loads(self.admission_marker.read_text(encoding="ascii"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SandboxRunError("sandbox admission intent is unreadable") from exc
        required = {
            "schema",
            "container_name",
            "owner_label",
            "owner_label_value",
            "owner_pid",
            "owner_start",
            "boot_id",
            "image",
            "work_root",
            "work_dir",
            "work_root_device",
            "work_root_inode",
            "work_dir_device",
            "work_dir_inode",
        }
        if (
            not isinstance(intent, dict)
            or not required.issubset(intent)
            or set(intent).difference(required | {"container_id"})
            or intent["schema"] != "sandbox-admission-intent.v1"
            or intent["owner_label"] != self.owner_label
            or not isinstance(intent["container_name"], str)
            or not intent["container_name"].startswith("swapp-lab-")
            or len(intent["container_name"].rsplit("-", 1)[-1]) != 32
            or not all(
                character in "0123456789abcdef"
                for character in intent["container_name"].rsplit("-", 1)[-1]
            )
            or intent["owner_label_value"] != intent["container_name"].rsplit("-", 1)[-1]
            or not isinstance(intent["work_dir"], str)
            or not Path(intent["work_dir"]).name.startswith(
                f"{intent['container_name'].rsplit('-', 1)[-1]}-"
            )
            or not isinstance(intent["owner_pid"], int)
            or isinstance(intent["owner_pid"], bool)
            or intent["owner_pid"] < 2
            or not isinstance(intent["owner_start"], str)
            or not intent["owner_start"].isdecimal()
            or not isinstance(intent["boot_id"], str)
            or len(intent["boot_id"]) != 36
            or not isinstance(intent["image"], str)
            or intent["image"] != self.image
            or (
                "container_id" in intent
                and (
                    not isinstance(intent["container_id"], str)
                    or len(intent["container_id"]) != 64
                    or any(char not in "0123456789abcdef" for char in intent["container_id"])
                )
            )
            or not self._valid_intent_work_directory(intent)
        ):
            raise SandboxRunError("sandbox admission intent identity is invalid")
        owner_pid = intent["owner_pid"]
        if owner_pid != os.getpid() and _same_process_identity(
            owner_pid, str(intent["owner_start"]), str(intent["boot_id"])
        ):
            raise SandboxRunError("sandbox admission owner is still alive")
        blank_observations = 0
        local_deadline = time.monotonic() + 20
        if deadline is not None:
            local_deadline = min(local_deadline, deadline)
        while time.monotonic() < local_deadline:
            if "container_id" not in intent and self._matching_docker_create_cli_alive(intent):
                blank_observations = 0
                time.sleep(min(0.1, max(0.0, local_deadline - time.monotonic())))
                continue
            remaining = local_deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                listed = subprocess.run(
                    [
                        self.docker_binary,
                        "container",
                        "ls",
                        "--all",
                        "--quiet",
                        "--no-trunc",
                        "--filter",
                        f"label={self.owner_label}={intent['owner_label_value']}",
                    ],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=min(5, remaining),
                    check=False,
                )  # nosec B603
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise SandboxRunError("sandbox orphan reconciliation failed") from exc
            if listed.returncode != 0:
                raise SandboxRunError("sandbox orphan reconciliation failed")
            container_ids = listed.stdout.decode("ascii", errors="strict").splitlines()
            if (
                "container_id" in intent
                and container_ids
                and container_ids != [intent["container_id"]]
            ):
                raise SandboxRunError("persisted container ID conflicts with owner label")
            if not container_ids:
                blank_observations += 1
                # The exact foreground Docker CLI must be gone before five daemon
                # queries over one second can establish that its request is absent.
                if blank_observations >= 5:
                    self._cleanup_intent_work_directory(intent)
                    self._clear_admission_intent()
                    return
                time.sleep(min(0.25, max(0.0, local_deadline - time.monotonic())))
                continue
            blank_observations = 0
            for container_id in container_ids:
                remaining = local_deadline - time.monotonic()
                if remaining <= 0:
                    raise SandboxRunError("sandbox orphan reconciliation deadline expired")
                if not container_id.startswith("sha256:") and not (
                    len(container_id) == 64
                    and all(char in "0123456789abcdef" for char in container_id)
                ):
                    raise SandboxRunError("sandbox orphan identity is invalid")
                details = subprocess.run(
                    [
                        self.docker_binary,
                        "container",
                        "inspect",
                        "--format",
                        "{{json .Config.Labels}}|{{.Name}}|{{.Config.Image}}",
                        container_id,
                    ],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=min(10, remaining),
                    check=False,
                )  # nosec B603
                if details.returncode != 0:
                    raise SandboxRunError("owned orphan identity could not be verified")
                expected_name = "/" + intent["container_name"]
                expected_label = intent["owner_label_value"]
                label_json, separator, inspected = (
                    details.stdout.decode("utf-8").strip().partition("|")
                )
                try:
                    labels = json.loads(label_json)
                except json.JSONDecodeError as exc:
                    raise SandboxRunError("owned orphan labels are invalid") from exc
                if (
                    not separator
                    or inspected.split("|", 1)[0] != expected_name
                    or labels.get(self.owner_label) != expected_label
                    or inspected.split("|", 1)[1] != self.image
                ):
                    raise SandboxRunError("owned orphan does not match the persisted intent")
                remaining = local_deadline - time.monotonic()
                if remaining <= 0:
                    raise SandboxRunError("sandbox orphan reconciliation deadline expired")
                removed = subprocess.run(
                    [self.docker_binary, "container", "rm", "--force", container_id],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=min(10, remaining),
                    check=False,
                )  # nosec B603
                if removed.returncode != 0:
                    raise SandboxRunError("owned orphan sandbox could not be drained")
                self._cleanup_intent_work_directory(intent)
        raise SandboxRunError("sandbox orphan reconciliation did not reach a stable empty state")

    def _run_bounded_process(
        self,
        command: list[str],
        input_bytes: bytes,
        *,
        timeout_seconds: int,
        deadline: float | None = None,
        output_limit_bytes: int | None = None,
    ) -> tuple[int, bytes, bytes]:
        if deadline is not None:
            self._remaining_deadline(deadline)
        try:
            process = subprocess.Popen(  # nosec B603  # pylint: disable=consider-using-with
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            raise SandboxRunError("docker runner could not start") from exc
        buffers = [bytearray(), bytearray()]
        output_sizes = [0, 0]
        size_lock = threading.Lock()
        overflow = threading.Event()
        output_limits = [
            (self.profile.output_bytes if output_limit_bytes is None else output_limit_bytes)
            + 64 * 1024
            + len(ARTIFACT_MAGIC)
            + 4,
            self.profile.log_bytes,
        ]

        def drain(stream: object, index: int) -> None:
            while True:
                chunk = stream.read(65536)  # type: ignore[attr-defined]
                if not chunk:
                    break
                with size_lock:
                    room = output_limits[index] - output_sizes[index]
                    if room > 0:
                        buffers[index].extend(chunk[:room])
                        output_sizes[index] += min(len(chunk), room)
                    if len(chunk) > room:
                        overflow.set()

        def send_input() -> None:
            if process.stdin is None:
                return
            try:
                process.stdin.write(input_bytes)
                process.stdin.flush()
            except BrokenPipeError:
                pass
            finally:
                process.stdin.close()

        readers = [
            threading.Thread(target=drain, args=(process.stdout, 0), daemon=True),
            threading.Thread(target=drain, args=(process.stderr, 1), daemon=True),
        ]
        for reader in readers:
            reader.start()
        writer = threading.Thread(target=send_input, daemon=True)
        writer.start()
        deadline = (
            min(time.monotonic() + timeout_seconds, deadline)
            if deadline is not None
            else time.monotonic() + timeout_seconds
        )
        failure: str | None = None
        while process.poll() is None:
            if overflow.is_set():
                failure = "sandbox log output limit exceeded"
                break
            if time.monotonic() >= deadline:
                failure = "sandbox phase timed out"
                break
            time.sleep(0.025)
        if failure is not None:
            self._terminate_process_group(process)
        for reader in readers:
            reader.join(timeout=2)
        writer.join(timeout=2)
        exit_code = process.wait()
        if failure is not None:
            raise SandboxRunError(failure, exit_code=exit_code)
        if overflow.is_set():
            raise SandboxRunError("sandbox log output limit exceeded", exit_code=exit_code)
        self._check_trusted_exit(exit_code, bytes(buffers[0]), bytes(buffers[1]))
        return exit_code, bytes(buffers[0]), bytes(buffers[1])

    @staticmethod
    def _check_trusted_exit(exit_code: int, stdout: bytes, stderr: bytes) -> None:
        """Only the protected wrapper's exact bounded audit frame is evidence."""
        if exit_code == AUDIT_EXIT:
            if stdout or stderr != AUDIT_MARKER:
                raise SandboxRunError("sandbox audit protocol failed")
            raise SandboxRunError("forbidden_access", exit_code=AUDIT_EXIT)
        if exit_code == OBSERVER_EXIT:
            raise SandboxRunError("sandbox trusted supervisor failed")
        if exit_code != 0:
            error = stderr.decode("utf-8", errors="replace")[-2048:]
            detail = f": {error}" if error else ""
            raise SandboxRunError(f"candidate phase failed{detail}", exit_code=exit_code)

    def _create_container(self, command: list[str], *, timeout_seconds: float = 30) -> str:
        """Create without starting and return Docker's full immutable ID."""
        try:
            created = subprocess.run(  # nosec B603
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxRunError("Docker container creation did not complete") from exc
        if created.returncode != 0:
            detail = created.stderr.decode("utf-8", errors="replace")[-2048:]
            raise SandboxRunError(f"Docker container creation failed: {detail}")
        container_id = created.stdout.decode("ascii", errors="strict").strip()
        if len(container_id) != 64 or any(char not in "0123456789abcdef" for char in container_id):
            raise SandboxRunError("Docker did not return a full container ID")
        return container_id

    def _persist_container_id(self, container_id: str) -> None:
        """Fsync the immutable ID before issuing any command that can start it."""
        intent = json.loads(self.admission_marker.read_text(encoding="ascii"))
        intent["container_id"] = container_id
        self._replace_admission_intent(intent)

    def _replace_admission_intent(self, intent: dict[str, object]) -> None:
        payload = json.dumps(intent, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
        temporary = self.admission_marker.with_name(
            f"{self.admission_marker.name}.update-{uuid.uuid4().hex}"
        )
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        try:
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, self.admission_marker)
        directory_fd = os.open(self.admission_marker.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _clear_admission_intent(self) -> None:
        self.admission_marker.unlink(missing_ok=True)
        directory_fd = os.open(self.admission_marker.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    @staticmethod
    def _valid_intent_work_directory(intent: dict[str, object]) -> bool:
        """Check persisted path and inode identity before recovery cleanup."""
        root_value, work_value = intent.get("work_root"), intent.get("work_dir")
        if not isinstance(root_value, str) or not isinstance(work_value, str):
            return False
        root, work_dir = Path(root_value), Path(work_value)
        if not root.is_absolute() or not work_dir.is_absolute() or work_dir.parent != root:
            return False
        try:
            root_stat = root.lstat()
        except OSError:
            return False
        numeric_fields = (
            intent.get("work_root_device"),
            intent.get("work_root_inode"),
            intent.get("work_dir_device"),
            intent.get("work_dir_inode"),
        )
        if any(not isinstance(value, int) or isinstance(value, bool) for value in numeric_fields):
            return False
        root_is_same = (
            stat.S_ISDIR(root_stat.st_mode)
            and not stat.S_ISLNK(root_stat.st_mode)
            and root_stat.st_uid == os.getuid()
            and stat.S_IMODE(root_stat.st_mode) & 0o022 == 0
            and root_stat.st_dev == intent["work_root_device"]
            and root_stat.st_ino == intent["work_root_inode"]
        )
        if not root_is_same:
            return False
        try:
            work_stat = work_dir.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        run_id = str(intent["container_name"]).rsplit("-", 1)[-1]
        return (
            work_dir.name.startswith(f"{run_id}-")
            and stat.S_ISDIR(work_stat.st_mode)
            and not stat.S_ISLNK(work_stat.st_mode)
            and work_stat.st_uid == os.getuid()
            and work_stat.st_dev == intent["work_dir_device"]
            and work_stat.st_ino == intent["work_dir_inode"]
        )

    def _cleanup_intent_work_directory(self, intent: dict[str, object]) -> None:
        if not self._valid_intent_work_directory(intent):
            raise SandboxRunError("sandbox work directory identity could not be verified")
        device, inode = intent["work_dir_device"], intent["work_dir_inode"]
        root_value, work_value = intent["work_root"], intent["work_dir"]
        if (
            not isinstance(device, int)
            or not isinstance(inode, int)
            or not isinstance(root_value, str)
            or not isinstance(work_value, str)
        ):
            raise SandboxRunError("sandbox work directory identity is invalid")
        self._cleanup_run_directory(
            Path(work_value),
            expected_root=Path(root_value),
            expected_inode=(device, inode),
        )

    def _cleanup_run_directory(
        self,
        run_dir: Path,
        *,
        expected_root: Path,
        expected_inode: tuple[int, int] | None = None,
    ) -> None:
        """Remove only a private direct child of its recorded workspace."""
        if run_dir.parent != expected_root:
            raise SandboxRunError("sandbox temporary directory parent changed")
        try:
            run_stat = run_dir.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise SandboxRunError("sandbox temporary directory cleanup failed") from exc
        try:
            if stat.S_ISLNK(run_stat.st_mode) or not stat.S_ISDIR(run_stat.st_mode):
                raise SandboxRunError("sandbox temporary directory identity is invalid")
            if expected_inode is not None and (run_stat.st_dev, run_stat.st_ino) != expected_inode:
                raise SandboxRunError("sandbox temporary directory inode changed")
            for directory, child_directories, _files in os.walk(run_dir, followlinks=False):
                os.chmod(directory, 0o700)
                for child_name in child_directories:
                    child = Path(directory) / child_name
                    if not child.is_symlink():
                        os.chmod(child, 0o700)
            shutil.rmtree(run_dir)
        except OSError as exc:
            raise SandboxRunError("sandbox temporary directory cleanup failed") from exc

    def _matching_docker_cli_alive(self, intent: dict[str, object]) -> bool:
        """Find the exact owner create/start CLI after its parent crashed."""
        create_alive = self._matching_docker_create_cli_alive(intent)
        start_alive = self._matching_docker_start_cli_alive(intent)
        return create_alive or start_alive

    def _matching_docker_create_cli_alive(self, intent: dict[str, object]) -> bool:
        """Find the exact owner create CLI, whose result ID is not yet durable."""
        name = str(intent["container_name"])
        label_pair = f"{self.owner_label}={intent['owner_label_value']}"
        return self._matching_docker_cli_arguments(name=name, label_pair=label_pair)

    def _matching_docker_start_cli_alive(self, intent: dict[str, object]) -> bool:
        """Find the attach CLI for a durable ID; recovery can remove its container."""
        container_id = intent.get("container_id")
        return isinstance(container_id, str) and self._matching_docker_cli_arguments(
            container_id=container_id, command="start"
        )

    def _matching_docker_cli_arguments(
        self,
        *,
        name: str | None = None,
        label_pair: str | None = None,
        container_id: str | None = None,
        command: str | None = None,
    ) -> bool:
        """Match only the configured Docker CLI with exact persisted arguments."""
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                executable = Path(f"/proc/{entry.name}/exe").resolve().name
                arguments = Path(f"/proc/{entry.name}/cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            decoded = [argument.decode("utf-8", errors="replace") for argument in arguments]
            matches_name_and_label = (
                name is not None
                and label_pair is not None
                and name in decoded
                and label_pair in decoded
            )
            matches_id_and_command = (
                container_id is not None
                and command is not None
                and container_id in decoded
                and command in decoded
            )
            if executable == Path(self.docker_binary).name and (
                matches_name_and_label or matches_id_and_command
            ):
                return True
        return False

    def _write_admission_intent(
        self, container_name: str, owner_label_value: str, work_dir: Path
    ) -> None:
        """Persist ownership before asking Docker to create the container."""
        root_stat = self.work_root.stat()
        work_stat = work_dir.stat()
        payload = (
            json.dumps(
                {
                    "schema": "sandbox-admission-intent.v1",
                    "container_name": container_name,
                    "owner_label": self.owner_label,
                    "owner_pid": os.getpid(),
                    "owner_start": _process_start_time(os.getpid()),
                    "boot_id": Path("/proc/sys/kernel/random/boot_id")
                    .read_text(encoding="ascii")
                    .strip(),
                    "image": self.image,
                    "owner_label_value": owner_label_value,
                    "work_root": str(self.work_root),
                    "work_dir": str(work_dir),
                    "work_root_device": root_stat.st_dev,
                    "work_root_inode": root_stat.st_ino,
                    "work_dir_device": work_stat.st_dev,
                    "work_dir_inode": work_stat.st_ino,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            + b"\n"
        )
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.admission_marker, flags, 0o600)
        try:
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory_fd = os.open(self.admission_marker.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _remove_named_container(self, container_name: str) -> None:
        try:
            removed = subprocess.run(  # nosec B603
                [self.docker_binary, "container", "rm", "--force", container_name],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                check=False,
            )
            if removed.returncode == 0:
                return
            exists = subprocess.run(  # nosec B603
                [self.docker_binary, "container", "inspect", container_name],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxRunError("sandbox container cleanup could not be verified") from exc
        if exists.returncode == 0:
            raise SandboxRunError("sandbox container remains after cleanup")
        error = exists.stderr.decode("utf-8", errors="replace").lower()
        if "no such object" not in error and "no such container" not in error:
            raise SandboxRunError("sandbox container cleanup could not be verified")

    def _decode_output_frame(self, frame: bytes) -> tuple[OutputArtifact, ...]:
        if not frame.startswith(ARTIFACT_MAGIC) or len(frame) < len(ARTIFACT_MAGIC) + 4:
            raise SandboxRunError("trusted sandbox artifact frame is missing")
        offset = len(ARTIFACT_MAGIC)
        header_size = int.from_bytes(frame[offset : offset + 4], "big")
        offset += 4
        if header_size > 64 * 1024 or offset + header_size > len(frame):
            raise SandboxRunError("trusted sandbox artifact header is invalid")
        try:
            manifest = json.loads(frame[offset : offset + header_size])
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SandboxRunError("trusted sandbox artifact header is invalid") from exc
        if (
            not isinstance(manifest, dict)
            or set(manifest) != {"schema", "items"}
            or manifest["schema"] != "sandbox-artifacts.v1"
            or not isinstance(manifest["items"], list)
            or len(manifest["items"]) > 1024
        ):
            raise SandboxRunError("trusted sandbox artifact manifest is invalid")
        cursor = offset + header_size
        total = 0
        outputs: list[OutputArtifact] = []
        names: set[str] = set()
        for item in manifest["items"]:
            if not isinstance(item, dict) or set(item) != {"name", "size_bytes", "sha256"}:
                raise SandboxRunError("trusted sandbox artifact entry is invalid")
            name, size, expected_hash = item["name"], item["size_bytes"], item["sha256"]
            if (
                not isinstance(name, str)
                or not name
                or Path(name).name != name
                or name in {".", ".."}
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
                or not isinstance(expected_hash, str)
                or len(expected_hash) != 64
                or any(char not in "0123456789abcdef" for char in expected_hash)
                or name in names
            ):
                raise SandboxRunError("trusted sandbox artifact entry is invalid")
            names.add(name)
            total += size
            if total > self.profile.output_bytes or cursor + size > len(frame):
                raise SandboxRunError("trusted sandbox artifact byte budget exceeded")
            content = frame[cursor : cursor + size]
            cursor += size
            actual_hash = hashlib.sha256(content).hexdigest()
            if actual_hash != expected_hash:
                raise SandboxRunError("trusted sandbox artifact hash mismatch")
            outputs.append(OutputArtifact(name, actual_hash, size, content))
        if cursor != len(frame):
            raise SandboxRunError("trusted sandbox artifact frame has trailing bytes")
        return tuple(outputs)
