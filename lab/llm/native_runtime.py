"""Bounded, owned vLLM process lifecycle for one fair GPU turn.

The model server is a per-turn transient systemd service. Its only ingress is
an owner-only Unix socket, and it runs in a private network namespace. The
scheduler's drain callback inspects the durable unit identity, exact cgroup,
and GPU process table; request callers cannot assert that a drain occurred.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pwd
import re
import secrets
import select
import shutil
import socket
import sqlite3
import stat
import subprocess  # nosec B404 -- fixed systemd, busctl, and nvidia-smi executables
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import uuid4

from lab.llm.gpu_scheduler import (
    MAX_ACTIVATION_SECONDS,
    MAX_DRAIN_SECONDS,
    MAX_INFERENCE_SECONDS,
    MAX_TOTAL_SECONDS,
    GpuLease,
    LeaseConflict,
    PrincipalResolver,
    SharedGpuScheduler,
    SystemdPrincipalResolver,
    boottime,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODEL_REVISION = "c202236235762e1c871ad0ccb60c8ee5ba337b9a"
MODEL_REPOSITORY = "Qwen/Qwen3.5-9B"
MODEL_DIRECTORY = PROJECT_ROOT / "models" / "Qwen3.5-9B" / MODEL_REVISION
MODEL_SOURCE_MANIFEST = PROJECT_ROOT / "docs/ai-scientist/review-evidence/qwen-model-source.json"
VLLM_EXECUTABLE = PROJECT_ROOT / "data/runtime/vllm/.venv/bin/vllm"
SYSTEMD_RUN = "/usr/bin/systemd-run"
SYSTEMCTL = "/usr/bin/systemctl"
BUSCTL = "/usr/bin/busctl"
UNSHARE = "/usr/bin/unshare"
PYTHON = PROJECT_ROOT / ".venv/bin/python"
GPU_SLICE = "swapp-gpu.slice"
GPU_MEMORY_BYTES = 16 * 1024**3
GPU_CPU_QUOTA_USEC = 2_000_000
GPU_TASKS = 128
MODEL_UNIT_MEMORY_BYTES = 10 * 1024**3
MODEL_UNIT_CPU_PERCENT = 200
MODEL_UNIT_TASKS = 96
MODEL_TMPFS_BYTES = 1024**3
MINIMUM_FREE_DISK_BYTES = 20 * 1024**3
MODEL_RUNTIME_MAX_SECONDS = MAX_TOTAL_SECONDS
MAX_PROMPT_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
MAX_OUTPUT_TOKENS = 8_192
MAX_DIAGNOSTIC_LOG_BYTES = 16 * 1024 * 1024
SYSTEM_PROPERTIES = (
    "LoadState",
    "ActiveState",
    "InvocationID",
    "ControlGroup",
    "MainPID",
    "Description",
    "Environment",
    "MemoryMax",
    "MemorySwapMax",
    "CPUQuotaPerSecUSec",
    "TasksMax",
    "RuntimeMaxUSec",
)
HOST_RAM_RESERVE_BYTES = 6 * 1024**3
MAX_PREEXISTING_GPU_MEMORY_MIB = 512


def _pinned_cuda_toolkit_home() -> Path:
    """Return the CUDA compiler toolkit shipped in the pinned vLLM environment."""
    runtime_root = VLLM_EXECUTABLE.parent.parent.resolve(strict=True)
    candidates = tuple((runtime_root / "lib").glob("python*/site-packages/nvidia/cu13"))
    if len(candidates) != 1:
        raise ModelRuntimeError("pinned CUDA toolkit is missing or ambiguous")
    toolkit = candidates[0].resolve(strict=True)
    nvcc = toolkit / "bin/nvcc"
    required = (toolkit / "include/cuda.h", toolkit / "nvvm/libdevice/libdevice.10.bc")
    if (
        not toolkit.is_relative_to(runtime_root)
        or not toolkit.is_dir()
        or nvcc.is_symlink()
        or not nvcc.is_file()
        or not os.access(nvcc, os.X_OK)
        or any(not path.is_file() for path in required)
    ):
        raise ModelRuntimeError(
            "pinned CUDA toolkit files are incomplete or escape the vLLM runtime"
        )
    return toolkit


def _toolkit_version_from_headers(toolkit: Path) -> tuple[int, int]:
    versions: list[tuple[int, int]] = []
    for header, macro in (
        (toolkit / "include/cuda.h", "CUDA_VERSION"),
        (toolkit / "include/cuda_runtime_api.h", "CUDART_VERSION"),
    ):
        contents = header.read_bytes()
        match = re.search(
            rf"^\s*#\s*define\s+{macro}\s+(\d+)\s*$".encode("ascii"),
            contents,
            re.MULTILINE,
        )
        if match is None:
            raise ModelRuntimeError("pinned CUDA headers have no version macro")
        value = int(match.group(1))
        versions.append((value // 1000, (value % 1000) // 10))
    if versions[0] != versions[1]:
        raise ModelRuntimeError("pinned CUDA headers have different runtime versions")
    return versions[0]


def _known_display_gpu_consumer(pid: int, process_name: str) -> bool:
    """Exempt only the measured plasmalogin KWin GPU context on this host."""
    try:
        login_uid = pwd.getpwnam("plasmalogin").pw_uid
        process = Path("/proc") / str(pid)
        comm = (process / "comm").read_text(encoding="ascii").strip()
        cgroup = (process / "cgroup").read_text(encoding="ascii").strip()
        uid = process.stat().st_uid
    except (KeyError, OSError):
        return False
    expected = (
        f"0::/user.slice/user-{login_uid}.slice/user@{login_uid}.service/"
        "session.slice/plasma-login-kwin_wayland.service"
    )
    return (
        uid == login_uid
        and comm == "kwin_wayland"
        and process_name == "/usr/bin/kwin_wayland"
        and cgroup == expected
    )


def _parse_systemd_duration_usec(value: str) -> int:
    """Parse the bounded unit spellings emitted by `systemctl show`."""
    if not value or value in {"infinity", "[not set]"}:
        raise ValueError("systemd duration is unset or unbounded")
    units = {"us": 1, "µs": 1, "ms": 1_000, "s": 1_000_000, "min": 60_000_000}
    position = 0
    total = 0.0
    for match in re.finditer(r"\s*(\d+(?:\.\d+)?)\s*(min|ms|us|µs|s)", value):
        if value[position : match.start()].strip():
            raise ValueError("systemd duration has an unknown component")
        total += float(match.group(1)) * units[match.group(2)]
        position = match.end()
    if not position or value[position:].strip() or not math.isfinite(total):
        raise ValueError("systemd duration is malformed")
    integer = int(total)
    if integer <= 0:
        raise ValueError("systemd duration must be positive")
    return integer


class ModelRuntimeError(RuntimeError):
    """A bounded local model request failed without releasing ownership unsafely."""


class ModelTurnCancelled(ModelRuntimeError):
    """Trusted captured Director owner stopped, was fenced, or cannot be observed."""


class ModelOutputBudgetExceeded(ModelRuntimeError):
    """The model stopped because it reached the requested generation limit."""


class _ModelEndpointUnavailable(ModelRuntimeError):
    """The private vLLM UDS is not listening yet during bounded startup polling."""


class _ModelEndpointNotReady(ModelRuntimeError):
    """The server is reachable but has not finished loading the pinned model."""


@dataclass(frozen=True, slots=True)
class ModelPin:
    """Manifest-backed local model identity and exact file hashes."""

    directory: Path
    revision: str
    digest: str
    files: tuple[tuple[str, int, str], ...]
    _verified_stats: tuple[tuple[str, int, int, int, int], ...] | None = field(
        default=None, compare=False, repr=False
    )

    @classmethod
    def from_repository(cls) -> ModelPin:
        """Read the pinned source record without accepting caller-selected paths."""
        try:
            document = json.loads(MODEL_SOURCE_MANIFEST.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ModelRuntimeError("pinned model source record is unavailable") from exc
        if (
            document.get("repository") != MODEL_REPOSITORY
            or document.get("revision") != MODEL_REVISION
            or document.get("status") != "downloaded_and_verified"
            or document.get("local_directory")
            != MODEL_DIRECTORY.relative_to(PROJECT_ROOT).as_posix()
            or document.get("runtime_gpu_started") is not False
        ):
            raise ModelRuntimeError("model source record does not match the pinned runtime")
        rows: list[tuple[str, int, str]] = []
        for item in document.get("files", []):
            if not isinstance(item, dict):
                raise ModelRuntimeError("model file record is malformed")
            name = item.get("rfilename")
            size = item.get("size")
            digest = item.get("verified_sha256")
            if (
                not isinstance(name, str)
                or not name
                or name.startswith("/")
                or ".." in Path(name).parts
                or isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            ):
                raise ModelRuntimeError("model file record is invalid")
            rows.append((name, size, digest))
        if not rows:
            raise ModelRuntimeError("pinned model source record contains no files")
        rows.sort()
        canonical = json.dumps(rows, separators=(",", ":"), ensure_ascii=True).encode()
        return cls(
            MODEL_DIRECTORY, MODEL_REVISION, hashlib.sha256(canonical).hexdigest(), tuple(rows)
        )

    def verify_files(self) -> None:
        """Hash every pinned file before model start; reject symlinks and additions."""
        root = self.directory
        if root.is_symlink() or not root.is_dir():
            raise ModelRuntimeError("pinned model directory is unavailable")
        expected_names = {name for name, _, _ in self.files}
        actual_names: set[str] = set()
        for path in root.rglob("*"):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                raise ModelRuntimeError("model directory contains a symlink")
            if path.is_file():
                if _is_known_huggingface_cache_metadata(relative):
                    continue
                actual_names.add(relative)
        if actual_names != expected_names:
            raise ModelRuntimeError("local model file set differs from the pinned source record")
        current_stats: list[tuple[str, int, int, int, int]] = []
        for name, size, _ in self.files:
            path = root / name
            try:
                info = path.stat(follow_symlinks=False)
            except OSError as exc:
                raise ModelRuntimeError("pinned model file metadata is unavailable") from exc
            if not stat.S_ISREG(info.st_mode) or info.st_size != size or info.st_uid != os.getuid():
                raise ModelRuntimeError("pinned model file ownership, size, or type changed")
            current_stats.append((name, info.st_dev, info.st_ino, info.st_size, info.st_ctime_ns))
        frozen_stats = tuple(current_stats)
        if self._verified_stats == frozen_stats:
            return
        for name, size, digest in self.files:
            path = root / name
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            try:
                fd = os.open(path, flags)
            except OSError as exc:
                raise ModelRuntimeError("pinned model file could not be opened safely") from exc
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size != size:
                    raise ModelRuntimeError("pinned model file size or type changed")
                hasher = hashlib.sha256()
                while chunk := os.read(fd, 4 * 1024 * 1024):
                    hasher.update(chunk)
                if hasher.hexdigest() != digest:
                    raise ModelRuntimeError("pinned model file checksum changed")
            finally:
                os.close(fd)
        object.__setattr__(self, "_verified_stats", frozen_stats)


def _is_known_huggingface_cache_metadata(relative: str) -> bool:
    """Ignore only the exact auxiliary metadata Hugging Face stores beside a snapshot."""
    if relative in {".cache/huggingface/CACHEDIR.TAG", ".cache/huggingface/.gitignore"}:
        return True
    parts = Path(relative).parts
    if len(parts) == 4 and parts[:3] == (".cache", "huggingface", "download"):
        return parts[3].endswith((".lock", ".metadata"))
    if len(parts) == 4 and parts[:3] == (".cache", "huggingface", "trees"):
        return parts[3] == f"{MODEL_REVISION}.json"
    return False


@dataclass(frozen=True, slots=True)
class UnitSnapshot:
    """Verified systemd view of one transient runtime unit."""

    load_state: str
    active_state: str
    invocation_id: str
    control_group: str
    main_pid: int
    description: str
    environment: str
    memory_max: int
    memory_swap_max: int
    cpu_quota_usec: int
    tasks_max: int
    runtime_max_usec: int


@dataclass(frozen=True, slots=True)
class GpuSnapshot:
    """Read-only NVIDIA process and memory observation."""

    process_memory_mib: Mapping[int, int]
    used_memory_mib: int
    total_memory_mib: int
    exempt_display_pids: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class ModelMeasurements:
    """Separate startup, request, drain, host, and GPU observations."""

    startup_seconds: float
    inference_seconds: float
    drain_seconds: float
    peak_host_memory_bytes: int
    peak_gpu_memory_mib: int
    start_gpu_used_memory_mib: int
    end_gpu_used_memory_mib: int
    cpu_usage_usec: int


@dataclass(frozen=True, slots=True)
class ModelReply:
    """Bounded generated text and measured runtime data."""

    text: str
    prompt_tokens: int
    completion_tokens: int
    measurements: ModelMeasurements


@dataclass(frozen=True, slots=True)
class ModelTurnProfile:
    """One trusted, bounded diagnostic or research sampling profile."""

    profile_id: str
    system: Literal["S1", "S2"]
    max_context_tokens: int
    max_output_tokens: int
    model_max_len: int
    enable_thinking: bool
    thinking_token_budget: int | None
    temperature: float
    top_p: float
    top_k: int | None
    activation_seconds: int
    inference_seconds: int
    total_seconds: int


DIAGNOSTIC_S1_PROFILE = ModelTurnProfile(
    "qwen3.5-9b-fp8-per-tensor.s1-diagnostic.v1",
    "S1",
    3_072,
    128,
    4_096,
    False,
    None,
    0.0,
    1.0,
    None,
    600,
    30,
    630,
)
DIAGNOSTIC_S2_PROFILE = ModelTurnProfile(
    "qwen3.5-9b-fp8-per-tensor.s2-diagnostic.v2",
    "S2",
    3_584,
    512,
    4_096,
    True,
    128,
    0.6,
    0.95,
    20,
    600,
    30,
    630,
)
LOCAL_SMOKE_S1_PROFILE = ModelTurnProfile(
    "qwen3.5-9b-fp8-per-tensor.s1-bounded-smoke.v1",
    "S1",
    2_048,
    2_048,
    4_096,
    False,
    None,
    0.7,
    0.8,
    None,
    120,
    30,
    150,
)
LOCAL_SMOKE_S2_PROFILE = ModelTurnProfile(
    "qwen3.5-9b-fp8-per-tensor.s2-bounded-smoke.v2",
    "S2",
    2_048,
    4_096,
    6_144,
    True,
    512,
    0.6,
    0.95,
    20,
    120,
    90,
    210,
)
LOCAL_RESEARCH_S1_PROFILE = ModelTurnProfile(
    "qwen3.5-9b-fp8-per-tensor.s1-bounded-research.v1",
    "S1",
    8_192,
    2_048,
    10_240,
    False,
    None,
    0.7,
    0.8,
    None,
    120,
    30,
    150,
)
LOCAL_RESEARCH_S2_PROFILE = ModelTurnProfile(
    "qwen3.5-9b-fp8-per-tensor.s2-bounded-research.v1",
    "S2",
    8_192,
    8_192,
    16_384,
    True,
    512,
    0.6,
    0.95,
    20,
    120,
    90,
    210,
)
MODEL_TURN_PROFILES = {
    profile.profile_id: profile
    for profile in (
        DIAGNOSTIC_S1_PROFILE,
        DIAGNOSTIC_S2_PROFILE,
        LOCAL_SMOKE_S1_PROFILE,
        LOCAL_SMOKE_S2_PROFILE,
        LOCAL_RESEARCH_S1_PROFILE,
        LOCAL_RESEARCH_S2_PROFILE,
    )
}


class UnitManager(Protocol):
    """Trusted systemd and cgroup operations used by the runtime."""

    def ensure_slice(self) -> None: ...

    def launch(
        self,
        unit: str,
        nonce: str,
        uds_directory: Path,
        pin: ModelPin,
        diagnostic_log: Path,
        *,
        model_max_len: int,
    ) -> None: ...

    def inspect(self, unit: str) -> UnitSnapshot: ...

    def stop(self, unit: str, timeout_seconds: int) -> None: ...

    def cgroup_pids(self, control_group: str) -> frozenset[int]: ...

    def cgroup_empty(self, control_group: str) -> bool: ...

    def cgroup_memory_peak(self, control_group: str) -> int: ...

    def cgroup_cpu_usage(self, control_group: str) -> int: ...


class GpuObserver(Protocol):
    """Trusted read-only GPU process/memory observer."""

    def snapshot(self) -> GpuSnapshot: ...


observation_deadline: ContextVar[float | None] = ContextVar("observation_deadline", default=None)


def _observation_timeout(maximum: float) -> float:
    """Share one trusted readback deadline across systemd/NVIDIA queries."""
    deadline = observation_deadline.get()
    remaining = maximum if deadline is None else min(maximum, deadline - time.monotonic())
    if remaining <= 0:
        raise ModelRuntimeError("physical observation deadline expired")
    return remaining


class NvidiaSmiObserver:
    """Read GPU process and memory counters from the fixed nvidia-smi binary."""

    def snapshot(self) -> GpuSnapshot:
        app_rows = subprocess.run(  # nosec B603 -- fixed read-only NVIDIA query
            [
                "/usr/bin/nvidia-smi",
                "--query-compute-apps=pid,process_name,used_memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=_observation_timeout(3.0),
            check=False,
        )
        gpu_rows = subprocess.run(  # nosec B603 -- fixed read-only NVIDIA query
            [
                "/usr/bin/nvidia-smi",
                "--query-gpu=memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=_observation_timeout(3.0),
            check=False,
        )
        if app_rows.returncode != 0 or gpu_rows.returncode != 0:
            raise ModelRuntimeError("NVIDIA process or memory observation failed")
        processes: dict[int, int] = {}
        display_pids: set[int] = set()
        for line in app_rows.stdout.splitlines():
            if not line.strip():
                continue
            fields = [field.strip() for field in line.split(",")]
            if len(fields) != 3 or not fields[0].isdecimal() or not fields[2].isdecimal():
                raise ModelRuntimeError("NVIDIA process observation is malformed")
            pid, memory = int(fields[0]), int(fields[2])
            if pid <= 0 or memory < 0:
                raise ModelRuntimeError("NVIDIA process observation is out of range")
            processes[pid] = memory
            if _known_display_gpu_consumer(pid, fields[1]):
                display_pids.add(pid)
        gpu_entries = [line for line in gpu_rows.stdout.splitlines() if line.strip()]
        if len(gpu_entries) != 1:
            raise ModelRuntimeError("exactly one local GPU is required for the first runtime")
        fields = [field.strip() for field in gpu_entries[0].split(",")]
        if len(fields) != 2 or not all(field.isdecimal() for field in fields):
            raise ModelRuntimeError("NVIDIA GPU memory observation is malformed")
        used, total = map(int, fields)
        if not 0 <= used <= total:
            raise ModelRuntimeError("NVIDIA GPU memory observation is out of range")
        return GpuSnapshot(processes, used, total, frozenset(display_pids))


class SystemdUnitManager:
    """Start and inspect the exact bounded per-turn unit under the shared slice."""

    def __init__(self, *, memory_bytes: int = MODEL_UNIT_MEMORY_BYTES) -> None:
        if memory_bytes < 1024**3 or memory_bytes > GPU_MEMORY_BYTES:
            raise ValueError("model unit memory must fit the aggregate GPU slice")
        self.memory_bytes = memory_bytes

    @staticmethod
    def _run(args: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(  # nosec B603 -- internal callers construct fixed argv only
                list(args),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ModelRuntimeError("bounded systemd operation timed out") from exc

    @staticmethod
    def _show_properties(unit: str, properties: Sequence[str]) -> dict[str, str]:
        if (
            unit != GPU_SLICE
            and re.fullmatch(r"swapp-(?:aos|lab)-gpu-turn-[0-9a-f]{32}\.service", unit) is None
        ):
            raise ValueError("runtime unit is outside the fixed model namespace")
        result = SystemdUnitManager._run(
            [
                SYSTEMCTL,
                "--user",
                "show",
                "--no-pager",
                *[f"--property={name}" for name in properties],
                unit,
            ],
            timeout=_observation_timeout(5.0),
        )
        if result.returncode != 0:
            raise ModelRuntimeError("systemd runtime unit inspection failed")
        values: dict[str, str] = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value.strip()
        if not set(properties) <= set(values):
            raise ModelRuntimeError("systemd runtime properties are incomplete")
        return values

    def ensure_slice(self) -> None:
        """Create or strictly verify the aggregate no-swap GPU budget."""
        probe = self._run(
            [SYSTEMCTL, "--user", "show", GPU_SLICE, "--property=ControlGroup", "--value"],
            timeout=5,
        )
        if probe.returncode != 0 or not probe.stdout.strip():
            created = self._run(
                [
                    BUSCTL,
                    "--user",
                    "call",
                    "org.freedesktop.systemd1",
                    "/org/freedesktop/systemd1",
                    "org.freedesktop.systemd1.Manager",
                    "StartTransientUnit",
                    "ssa(sv)a(sa(sv))",
                    GPU_SLICE,
                    "fail",
                    "7",
                    "Description",
                    "s",
                    "SWAPP shared GPU runtime aggregate budget",
                    "MemoryMax",
                    "t",
                    str(GPU_MEMORY_BYTES),
                    "MemorySwapMax",
                    "t",
                    "0",
                    "CPUQuotaPerSecUSec",
                    "t",
                    str(GPU_CPU_QUOTA_USEC),
                    "TasksMax",
                    "t",
                    str(GPU_TASKS),
                    "CollectMode",
                    "s",
                    "inactive-or-failed",
                    "CPUAccounting",
                    "b",
                    "true",
                    "0",
                ],
                timeout=10,
            )
            if created.returncode != 0:
                probe = self._run(
                    [
                        SYSTEMCTL,
                        "--user",
                        "show",
                        GPU_SLICE,
                        "--property=ControlGroup",
                        "--value",
                    ],
                    timeout=5,
                )
                if probe.returncode != 0 or not probe.stdout.strip():
                    raise ModelRuntimeError("shared GPU resource slice is unavailable")
            else:
                probe = self._run(
                    [
                        SYSTEMCTL,
                        "--user",
                        "show",
                        GPU_SLICE,
                        "--property=ControlGroup",
                        "--value",
                    ],
                    timeout=5,
                )
                if probe.returncode != 0 or not probe.stdout.strip():
                    raise ModelRuntimeError("shared GPU resource slice did not become active")
        control_group = probe.stdout.strip()
        if not control_group.startswith("/") or ".." in Path(control_group).parts:
            raise ModelRuntimeError("shared GPU slice cgroup identity is invalid")
        cgroup = Path("/sys/fs/cgroup") / control_group.lstrip("/")
        expected = {
            "memory.max": str(GPU_MEMORY_BYTES),
            "memory.swap.max": "0",
            "cpu.max": f"{GPU_CPU_QUOTA_USEC // 10} 100000",
            "pids.max": str(GPU_TASKS),
        }
        for name, value in expected.items():
            try:
                actual = (cgroup / name).read_text(encoding="ascii").strip()
            except OSError as exc:
                raise ModelRuntimeError("shared GPU cgroup controls are unavailable") from exc
            if actual != value:
                raise ModelRuntimeError(f"shared GPU cgroup has an unexpected {name} limit")

    @staticmethod
    def _unit_name(owner: str, suffix: str) -> str:
        if owner not in {"aos", "lab"} or re.fullmatch(r"[0-9a-f]{32}", suffix) is None:
            raise ValueError("invalid fixed GPU owner or fencing identity")
        return f"swapp-{owner}-gpu-turn-{suffix}.service"

    def launch(
        self,
        unit: str,
        nonce: str,
        uds_directory: Path,
        pin: ModelPin,
        diagnostic_log: Path,
        *,
        model_max_len: int,
    ) -> None:
        """Start one offline, network-namespaced, UDS-only vLLM instance."""
        if re.fullmatch(r"swapp-(?:aos|lab)-gpu-turn-[0-9a-f]{32}\.service", unit) is None:
            raise ValueError("runtime unit is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", nonce) is None:
            raise ValueError("runtime nonce is invalid")
        registered_lengths = {item.model_max_len for item in MODEL_TURN_PROFILES.values()}
        if isinstance(model_max_len, bool) or model_max_len not in registered_lengths:
            raise ValueError("model maximum length must come from a registered profile")
        try:
            log_info = diagnostic_log.lstat()
            log_parent = diagnostic_log.parent.lstat()
        except OSError as exc:
            raise ModelRuntimeError("private model diagnostic log was not prepared") from exc
        if (
            not stat.S_ISREG(log_info.st_mode)
            or stat.S_ISLNK(log_info.st_mode)
            or log_info.st_uid != os.getuid()
            or log_info.st_nlink != 1
            or stat.S_IMODE(log_info.st_mode) & 0o077
            or not stat.S_ISDIR(log_parent.st_mode)
            or stat.S_ISLNK(log_parent.st_mode)
            or log_parent.st_uid != os.getuid()
            or stat.S_IMODE(log_parent.st_mode) & 0o077
            or log_info.st_size > MAX_DIAGNOSTIC_LOG_BYTES
        ):
            raise ModelRuntimeError("private model diagnostic log identity is unsafe")
        if not VLLM_EXECUTABLE.is_file() or VLLM_EXECUTABLE.is_symlink():
            raise ModelRuntimeError("pinned local vLLM executable is unavailable")
        cuda_home = _pinned_cuda_toolkit_home()
        nvcc = cuda_home / "bin/nvcc"
        compiler = self._run([str(nvcc), "--version"], timeout=5)
        compiler_match = re.search(r"release\s+(\d+)\.(\d+),\s+V", compiler.stdout)
        if (
            compiler.returncode != 0
            or compiler_match is None
            or (int(compiler_match.group(1)), int(compiler_match.group(2)))
            != _toolkit_version_from_headers(cuda_home)
        ):
            raise ModelRuntimeError("pinned CUDA compiler and runtime header versions differ")
        model_path = str(pin.directory)
        uds_path = str(uds_directory / "vllm.sock")
        if len(uds_path.encode()) >= 104:
            raise ModelRuntimeError("private model Unix socket path is too long")
        description = f"SWAPP GPU model turn {nonce}"
        runtime_directory = uds_directory.name
        command = [
            SYSTEMD_RUN,
            "--user",
            "--collect",
            "--quiet",
            "--service-type=exec",
            f"--unit={unit}",
            f"--slice={GPU_SLICE}",
            f"--description={description}",
            f"--property=MemoryMax={self.memory_bytes}",
            "--property=MemorySwapMax=0",
            f"--property=CPUQuota={MODEL_UNIT_CPU_PERCENT}%",
            f"--property=TasksMax={MODEL_UNIT_TASKS}",
            f"--property=RuntimeMaxSec={MODEL_RUNTIME_MAX_SECONDS}",
            "--property=TimeoutStartSec=20",
            "--property=TimeoutStopSec=10",
            "--property=KillMode=control-group",
            "--property=OOMPolicy=stop",
            "--property=NoNewPrivileges=yes",
            f"--property=LimitFSIZE={MAX_DIAGNOSTIC_LOG_BYTES}",
            f"--property=TemporaryFileSystem=/tmp:rw,size={MODEL_TMPFS_BYTES}",
            f"--property=TemporaryFileSystem=/var/tmp:rw,size={MODEL_TMPFS_BYTES // 4}",
            f"--property=RuntimeDirectory={runtime_directory}",
            "--property=RuntimeDirectoryMode=0700",
            f"--property=BindReadOnlyPaths={model_path}",
            f"--property=StandardOutput=append:{diagnostic_log}",
            f"--property=StandardError=append:{diagnostic_log}",
            f"--working-directory={PROJECT_ROOT}",
            "--setenv=HF_HUB_OFFLINE=1",
            "--setenv=TRANSFORMERS_OFFLINE=1",
            "--setenv=XDG_CACHE_HOME=/tmp/model-cache/xdg",
            "--setenv=VLLM_CACHE_ROOT=/tmp/model-cache/vllm",
            "--setenv=VLLM_USE_FLASHINFER_SAMPLER=0",
            "--setenv=FLASHINFER_WORKSPACE_BASE=/tmp/model-cache/flashinfer",
            "--setenv=VLLM_FLASHINFER_AUTOTUNE_CACHE_DIR=/tmp/model-cache/flashinfer-autotune",
            "--setenv=TRITON_CACHE_DIR=/tmp/model-cache/triton",
            "--setenv=TORCHINDUCTOR_CACHE_DIR=/tmp/model-cache/torchinductor",
            "--setenv=TORCH_EXTENSIONS_DIR=/tmp/model-cache/torch-extensions",
            "--setenv=TORCH_HOME=/tmp/model-cache/torch",
            "--setenv=CUDA_CACHE_PATH=/tmp/model-cache/cuda-driver",
            "--setenv=CUDA_CACHE_MAXSIZE=268435456",
            "--setenv=FLASH_ATTENTION_CUTE_DSL_CACHE_DIR=/tmp/model-cache/flash-attention-cute",
            "--setenv=CUTE_DSL_CACHE_DIR=/tmp/model-cache/cute-dsl",
            "--setenv=HF_HOME=/tmp/model-cache/huggingface",
            "--setenv=TRANSFORMERS_CACHE=/tmp/model-cache/transformers",
            "--setenv=TMPDIR=/tmp",
            f"--setenv=CUDA_HOME={cuda_home}",
            f"--setenv=CUDA_PATH={cuda_home}",
            f"--setenv=CUDACXX={nvcc}",
            f"--setenv=PATH={cuda_home / 'bin'}:/usr/bin:/bin",
            "--setenv=MAX_JOBS=2",
            "--setenv=FLASHINFER_NVCC_THREADS=1",
            "--setenv=TOKENIZERS_PARALLELISM=false",
            "--setenv=OPENBLAS_NUM_THREADS=1",
            "--setenv=OMP_NUM_THREADS=1",
            "--setenv=MKL_NUM_THREADS=1",
            "--setenv=NUMEXPR_NUM_THREADS=1",
            "--setenv=BLIS_NUM_THREADS=1",
            "--setenv=VLLM_LOGGING_LEVEL=WARNING",
            "--setenv=VLLM_NO_USAGE_STATS=1",
            "--setenv=HF_HUB_DISABLE_TELEMETRY=1",
            "--setenv=VLLM_HOST_IP=127.0.0.1",
            "--setenv=SWAPP_GPU_TURN_NONCE=" + nonce,
            "--setenv=SWAPP_GPU_HOST_NETNS_INODE=" + str(os.stat("/proc/self/ns/net").st_ino),
            UNSHARE,
            "--user",
            "--map-root-user",
            "--net",
            "--",
            str(PYTHON),
            "-m",
            "lab.llm.netns_exec",
            "--",
            str(VLLM_EXECUTABLE),
            "serve",
            model_path,
            "--revision",
            pin.revision,
            "--served-model-name",
            "qwen3.5-9b",
            "--uds",
            uds_path,
            "--quantization",
            "fp8_per_tensor",
            "--language-model-only",
            "--enforce-eager",
            "--max-num-seqs",
            "1",
            "--max-model-len",
            str(model_max_len),
            "--gpu-memory-utilization",
            "0.80",
            "--reasoning-parser",
            "qwen3",
            "--tool-call-parser",
            "qwen3_coder",
            "--enable-auto-tool-choice",
        ]
        result = SystemdUnitManager._run(command, timeout=25)
        if result.returncode != 0:
            raise ModelRuntimeError("bounded vLLM systemd launch failed")

    def inspect(self, unit: str) -> UnitSnapshot:
        values = self._show_properties(unit, SYSTEM_PROPERTIES)
        try:
            control_group = values["ControlGroup"].rstrip("/")
            if values["LoadState"] == "not-found":
                return UnitSnapshot(
                    values["LoadState"],
                    values["ActiveState"],
                    values["InvocationID"],
                    control_group,
                    int(values["MainPID"]),
                    values["Description"],
                    values["Environment"],
                    0,
                    0,
                    0,
                    0,
                    0,
                )
            memory_max = int(values["MemoryMax"])
            memory_swap_max = int(values["MemorySwapMax"])
            tasks_max = int(values["TasksMax"])
            cpu_quota_usec = _parse_systemd_duration_usec(values["CPUQuotaPerSecUSec"])
            runtime_max_usec = _parse_systemd_duration_usec(values["RuntimeMaxUSec"])
            group = self._cgroup_path(control_group)
            memory_actual = int((group / "memory.max").read_text(encoding="ascii").strip())
            swap_actual = int((group / "memory.swap.max").read_text(encoding="ascii").strip())
            cpu_fields = (group / "cpu.max").read_text(encoding="ascii").split()
            pids_actual = int((group / "pids.max").read_text(encoding="ascii").strip())
            if len(cpu_fields) != 2 or int(cpu_fields[1]) <= 0:
                raise ValueError("invalid cpu.max")
            quota_actual = int(cpu_fields[0]) * 1_000_000 // int(cpu_fields[1])
            if (
                memory_actual != memory_max
                or swap_actual != memory_swap_max
                or quota_actual != cpu_quota_usec
                or pids_actual != tasks_max
            ):
                raise ModelRuntimeError("systemd model cgroup limits do not match unit properties")
            return UnitSnapshot(
                values["LoadState"],
                values["ActiveState"],
                values["InvocationID"],
                control_group,
                int(values["MainPID"]),
                values["Description"],
                values["Environment"],
                memory_max,
                memory_swap_max,
                cpu_quota_usec,
                tasks_max,
                runtime_max_usec,
            )
        except (OSError, KeyError, ValueError) as exc:
            raise ModelRuntimeError("systemd runtime identity/limits are invalid") from exc

    def stop(self, unit: str, timeout_seconds: int) -> None:
        result = self._run([SYSTEMCTL, "--user", "stop", unit], timeout=timeout_seconds)
        if result.returncode != 0:
            raise ModelRuntimeError("could not stop the exact owned model unit")

    @staticmethod
    def _cgroup_path(control_group: str) -> Path:
        if not control_group.startswith("/") or ".." in Path(control_group).parts:
            raise ModelRuntimeError("model unit cgroup path is invalid")
        return Path("/sys/fs/cgroup") / control_group.lstrip("/")

    def cgroup_pids(self, control_group: str) -> frozenset[int]:
        path = self._cgroup_path(control_group)
        if path.is_symlink() or not path.is_dir():
            return frozenset()
        try:
            values = (path / "cgroup.procs").read_text(encoding="ascii").split()
            result = frozenset(int(item) for item in values)
        except (OSError, ValueError) as exc:
            raise ModelRuntimeError("model cgroup process list cannot be read") from exc
        if any(pid <= 0 for pid in result):
            raise ModelRuntimeError("model cgroup process identity is invalid")
        return result

    def cgroup_empty(self, control_group: str) -> bool:
        path = self._cgroup_path(control_group)
        if path.is_symlink():
            return False
        if not path.exists():
            return True
        if not path.is_dir():
            return False
        try:
            pids = (path / "cgroup.procs").read_text(encoding="ascii").split()
            events = dict(
                line.split()
                for line in (path / "cgroup.events").read_text(encoding="ascii").splitlines()
            )
        except (OSError, ValueError):
            return False
        return not pids and events.get("populated") == "0"

    def cgroup_memory_peak(self, control_group: str) -> int:
        path = self._cgroup_path(control_group)
        try:
            return int((path / "memory.peak").read_text(encoding="ascii").strip())
        except (OSError, ValueError) as exc:
            raise ModelRuntimeError("model host-memory peak is unavailable") from exc

    def cgroup_cpu_usage(self, control_group: str) -> int:
        path = self._cgroup_path(control_group)
        try:
            metrics = dict(
                line.split()
                for line in (path / "cpu.stat").read_text(encoding="ascii").splitlines()
            )
            return int(metrics["usage_usec"])
        except (OSError, KeyError, ValueError) as exc:
            raise ModelRuntimeError("model CPU usage is unavailable") from exc


def _strict_json_object(data: bytes, *, maximum: int) -> dict[str, object]:
    if not data or len(data) > maximum:
        raise ModelRuntimeError("local model response exceeds the bounded JSON limit")
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ModelRuntimeError("local model returned malformed JSON") from exc
    if not isinstance(parsed, dict):
        raise ModelRuntimeError("local model response must be a JSON object")
    return parsed


def _wait_socket(
    sock: socket.socket,
    *,
    writable: bool,
    deadline: float,
    progress: Callable[[], None] | None,
) -> None:
    """Wait in short slices against one absolute wall deadline."""
    while True:
        remaining = deadline - boottime()
        if remaining <= 0:
            raise ModelRuntimeError("local model HTTP absolute deadline expired")
        read_set = [] if writable else [sock]
        write_set = [sock] if writable else []
        ready_read, ready_write, _ = select.select(read_set, write_set, [], min(0.2, remaining))
        if progress is not None:
            progress()
        if ready_read or ready_write:
            return


def _read_exact_socket(
    sock: socket.socket,
    length: int,
    *,
    deadline: float,
    progress: Callable[[], None] | None,
) -> bytes:
    result = bytearray()
    while len(result) < length:
        _wait_socket(sock, writable=False, deadline=deadline, progress=progress)
        try:
            chunk = sock.recv(min(65536, length - len(result)))
        except BlockingIOError:
            continue
        if not chunk:
            raise ModelRuntimeError("local model closed an incomplete HTTP response")
        result.extend(chunk)
    return bytes(result)


def _read_uds_json(
    socket_path: Path,
    method: str,
    path: str,
    payload: Mapping[str, object] | None,
    *,
    timeout: float,
    maximum: int = MAX_RESPONSE_BYTES,
    progress: Callable[[], None] | None = None,
    metadata_sink: dict[str, object] | None = None,
) -> dict[str, object]:
    if method not in {"GET", "POST"} or not path.startswith("/") or len(path) > 128:
        raise ValueError("invalid fixed local model endpoint")
    body = (
        b""
        if payload is None
        else json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
            "utf-8"
        )
    )
    if len(body) > MAX_PROMPT_BYTES:
        raise ModelRuntimeError("model request exceeds the bounded prompt body size")
    deadline = boottime() + timeout
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.setblocking(False)
        try:
            result = sock.connect_ex(str(socket_path))
        except OSError as exc:
            raise _ModelEndpointUnavailable("private vLLM Unix socket is not listening") from exc
        if result not in {0, getattr(os, "EINPROGRESS", 115), 11, 115}:
            raise _ModelEndpointUnavailable("private vLLM Unix socket rejected connection")
        if result != 0:
            _wait_socket(sock, writable=True, deadline=deadline, progress=progress)
            error = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if error:
                raise _ModelEndpointUnavailable("private vLLM Unix socket rejected connection")
        request_body = body if payload is not None else b""
        headers = (
            f"{method} {path} HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Connection: close\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(request_body)}\r\n\r\n"
        ).encode("ascii") + request_body
        sent = 0
        while sent < len(headers):
            _wait_socket(sock, writable=True, deadline=deadline, progress=progress)
            try:
                sent += sock.send(headers[sent:])
            except BlockingIOError:
                continue
        response_head = bytearray()
        delimiter = b"\r\n\r\n"
        while delimiter not in response_head:
            if len(response_head) > 16384:
                raise ModelRuntimeError("local model HTTP headers exceed their bound")
            _wait_socket(sock, writable=False, deadline=deadline, progress=progress)
            try:
                chunk = sock.recv(4096)
            except BlockingIOError:
                continue
            if not chunk:
                raise ModelRuntimeError("local model closed before HTTP headers completed")
            response_head.extend(chunk)
        head_bytes, body_prefix = bytes(response_head).split(delimiter, maxsplit=1)
        lines = head_bytes.decode("iso-8859-1").split("\r\n")
        status_parts = lines[0].split()
        if len(status_parts) < 2 or status_parts[1] != "200":
            status = status_parts[1] if len(status_parts) >= 2 else "malformed"
            error_type = (
                _ModelEndpointNotReady if status in {"502", "503", "504"} else ModelRuntimeError
            )
            raise error_type(f"local model endpoint returned HTTP {status}")
        response_headers: dict[str, str] = {}
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            if not separator:
                raise ModelRuntimeError("local model response header is malformed")
            response_headers[name.strip().lower()] = value.strip()
        if "transfer-encoding" in response_headers or "content-length" not in response_headers:
            raise ModelRuntimeError("local model response must have a fixed Content-Length")
        try:
            body_length = int(response_headers["content-length"])
        except ValueError as exc:
            raise ModelRuntimeError("local model response Content-Length is invalid") from exc
        if body_length <= 0 or body_length > maximum:
            raise ModelRuntimeError("local model response exceeds the bounded response size")
        if metadata_sink is not None:
            content_type = response_headers.get("content-type", "")
            metadata_sink["http_content_type"] = "".join(
                char for char in content_type if char >= " " and char != "\x7f"
            )[:128]
            metadata_sink["content_length_bytes"] = body_length
        if len(body_prefix) > body_length:
            raise ModelRuntimeError("local model response contains trailing bytes")
        body_result = bytearray(body_prefix)
        body_result.extend(
            _read_exact_socket(
                sock,
                body_length - len(body_result),
                deadline=deadline,
                progress=progress,
            )
        )
        return _strict_json_object(bytes(body_result), maximum=maximum)
    except _ModelEndpointUnavailable:
        raise
    except (OSError, UnicodeError) as exc:
        raise ModelRuntimeError("local model Unix-socket request failed") from exc
    finally:
        sock.close()


def _capture_response_metadata(
    response: Mapping[str, object], transport_metadata: Mapping[str, object]
) -> dict[str, object]:
    """Retain bounded response shape and valid token counts, never model text."""
    choices = response.get("choices")
    choice = choices[0] if isinstance(choices, list) and len(choices) == 1 else None
    choice = choice if isinstance(choice, dict) else {}
    message = choice.get("message")
    message = message if isinstance(message, dict) else {}
    content = message.get("content")
    reasoning = message.get("reasoning")
    usage = response.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    valid_usage = (
        isinstance(prompt_tokens, int)
        and not isinstance(prompt_tokens, bool)
        and prompt_tokens >= 0
        and isinstance(completion_tokens, int)
        and not isinstance(completion_tokens, bool)
        and completion_tokens >= 0
    )
    finish_reason = choice.get("finish_reason")
    return {
        **dict(transport_metadata),
        "finish_reason": finish_reason[:32] if isinstance(finish_reason, str) else None,
        "usage_valid": valid_usage,
        "prompt_tokens": prompt_tokens if valid_usage else None,
        "completion_tokens": completion_tokens if valid_usage else None,
        "message_content_type": (
            type(content).__name__ if content is not None else "null_or_missing"
        ),
        "content_length_chars": len(content) if isinstance(content, str) else None,
        "reasoning_present": "reasoning" in message,
        "reasoning_length_chars": len(reasoning) if isinstance(reasoning, str) else None,
    }


def _reject_output_truncation(metadata: Mapping[str, object]) -> None:
    if metadata.get("finish_reason") == "length":
        raise ModelOutputBudgetExceeded("vLLM response exhausted its output token budget")


def _validate_thinking_token_budget(
    value: object, *, enable_thinking: bool, max_output_tokens: int
) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value >= max_output_tokens
    ):
        raise ValueError("thinking token budget must be a positive integer below output tokens")
    if not enable_thinking:
        raise ValueError("thinking token budget requires thinking to be enabled")
    return value


def _validate_sampling_parameters(temperature: object, top_p: object) -> tuple[float, float]:
    """Validate the trusted, bounded vLLM sampling profile."""
    if (
        isinstance(temperature, bool)
        or not isinstance(temperature, (int, float))
        or not math.isfinite(temperature)
        or not 0.0 <= temperature <= 2.0
    ):
        raise ValueError("temperature must be finite and within 0..2")
    if (
        isinstance(top_p, bool)
        or not isinstance(top_p, (int, float))
        or not math.isfinite(top_p)
        or not 0.0 < top_p <= 1.0
    ):
        raise ValueError("top_p must be finite and within (0, 1]")
    return float(temperature), float(top_p)


def _validate_top_k(value: object) -> int | None:
    """Validate the optional explicit top-k profile used by Qwen S2."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 128:
        raise ValueError("top_k must be an integer within 1..128 or null")
    return value


def _prepare_doctor_directories() -> tuple[Path, Path]:
    """Create private result/log roots before any model launch."""
    root = PROJECT_ROOT / "data/runtime/native-doctor"
    logs = root / "logs"
    for directory in (root, logs):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            info = directory.lstat()
        except OSError as exc:
            raise ModelRuntimeError("doctor diagnostics directory is unavailable") from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
        ):
            raise ModelRuntimeError("doctor diagnostics directory identity is unsafe")
        os.chmod(directory, 0o700)
    return root, logs


class OwnedVllmRuntime:
    """Fairly admit, launch, measure, and drain one local Qwen model turn."""

    def __init__(
        self,
        database: Path,
        *,
        principal_resolver: PrincipalResolver,
        pin: ModelPin | None = None,
        unit_manager: UnitManager | None = None,
        gpu_observer: GpuObserver | None = None,
        clock: Callable[[], float] = boottime,
        sleep: Callable[[float], None] = time.sleep,
        activation_seconds: int | None = None,
        inference_seconds: int | None = None,
        total_seconds: int | None = None,
        queue_timeout_seconds: int = 900,
        required_free_disk_bytes: int = MINIMUM_FREE_DISK_BYTES,
        profile: ModelTurnProfile = DIAGNOSTIC_S1_PROFILE,
        cancellation_observer: Callable[[], None] | None = None,
    ) -> None:
        self._database = database
        self._cancellation_observer = cancellation_observer
        if MODEL_TURN_PROFILES.get(profile.profile_id) != profile:
            raise ValueError("model turn profile is not a trusted registered profile")
        self.profile = profile
        self._secure_database_path(database)
        self.pin = pin or ModelPin.from_repository()
        self.units: UnitManager = unit_manager or SystemdUnitManager()
        self.gpu: GpuObserver = gpu_observer or NvidiaSmiObserver()
        self._clock = clock
        self._sleep = sleep
        self.required_free_disk_bytes = required_free_disk_bytes
        self._phase = "configured"
        self._failure_phase: str | None = None
        self._last_response_metadata: dict[str, object] | None = None
        self._last_request_profile: dict[str, object] | None = None
        self.scheduler = SharedGpuScheduler(
            database,
            principal_resolver=principal_resolver,
            drain_verifier=self.verify_drained,
            max_activation_seconds=(
                profile.activation_seconds if activation_seconds is None else activation_seconds
            ),
            max_inference_seconds=(
                profile.inference_seconds if inference_seconds is None else inference_seconds
            ),
            max_total_seconds=profile.total_seconds if total_seconds is None else total_seconds,
            queue_timeout_seconds=queue_timeout_seconds,
            clock=clock,
        )
        self._initialize_bindings()

    @staticmethod
    def _secure_database_path(database: Path) -> None:
        parent = database.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = parent.stat()
        if parent.is_symlink() or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ModelRuntimeError("GPU runtime state directory must be private and user-owned")
        if database.exists():
            if database.is_symlink() or not database.is_file():
                raise ModelRuntimeError("GPU scheduler database path is unsafe")
            current = database.stat()
            if current.st_uid != os.getuid() or current.st_nlink != 1:
                raise ModelRuntimeError("GPU scheduler database ownership is unsafe")
            os.chmod(database, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        os.chmod(self._database, 0o600)
        return connection

    def _initialize_bindings(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS gpu_runtime_bindings (
                    owner TEXT NOT NULL CHECK(owner IN ('aos','lab')),
                    request_id TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    unit TEXT NOT NULL UNIQUE,
                    nonce TEXT NOT NULL,
                    uds_directory TEXT NOT NULL,
                    model_sha256 TEXT NOT NULL CHECK(length(model_sha256)=64),
                    expected_description TEXT NOT NULL,
                    created_boottime REAL NOT NULL,
                    runtime_max_seconds INTEGER NOT NULL,
                    launch_state TEXT NOT NULL CHECK(
                        launch_state IN ('prepared','created','uncertain')
                    ),
                    launch_finished_boottime REAL,
                    invocation_id TEXT,
                    main_pid INTEGER,
                    main_start_ticks INTEGER,
                    boot_id TEXT,
                    control_group TEXT,
                    observed_gpu_pids_json TEXT NOT NULL DEFAULT '[]',
                    peak_gpu_memory_mib INTEGER NOT NULL DEFAULT 0,
                    start_gpu_used_memory_mib INTEGER,
                    end_gpu_used_memory_mib INTEGER,
                    startup_seconds REAL,
                    inference_seconds REAL,
                    drain_seconds REAL,
                    PRIMARY KEY(owner,request_id,fencing_token)
                )"""
            )

    def _preflight_disk(self) -> None:
        free = shutil.disk_usage(PROJECT_ROOT).free
        if free < self.required_free_disk_bytes:
            raise ModelRuntimeError("model runtime disk reserve is below the configured minimum")

    def _preflight_host(self) -> GpuSnapshot:
        """Require host RAM reserve and no unadmitted GPU compute process."""
        values: dict[str, int] = {}
        try:
            for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
                key, _, rest = line.partition(":")
                fields = rest.split()
                if key in {"MemTotal", "MemAvailable"} and len(fields) >= 2:
                    values[key] = int(fields[0]) * 1024
        except (OSError, ValueError) as exc:
            raise ModelRuntimeError("host memory preflight is unavailable") from exc
        if set(values) != {"MemTotal", "MemAvailable"}:
            raise ModelRuntimeError("host memory preflight is incomplete")
        if values["MemTotal"] < GPU_MEMORY_BYTES + HOST_RAM_RESERVE_BYTES:
            raise ModelRuntimeError("host RAM capacity is below runtime cap plus host reserve")
        if values["MemAvailable"] < MODEL_UNIT_MEMORY_BYTES + HOST_RAM_RESERVE_BYTES:
            raise ModelRuntimeError("host free RAM does not cover model cap plus host reserve")
        gpu = self.gpu.snapshot()
        unadmitted = set(gpu.process_memory_mib) - set(gpu.exempt_display_pids)
        if unadmitted:
            raise ModelRuntimeError("GPU has an unadmitted compute process; refusing model start")
        if gpu.used_memory_mib > MAX_PREEXISTING_GPU_MEMORY_MIB:
            raise ModelRuntimeError("GPU use exceeds the bounded idle baseline")
        if gpu.total_memory_mib < 15 * 1024:
            raise ModelRuntimeError("the first pinned model profile requires a 16 GiB GPU")
        return gpu

    @staticmethod
    def _secure_runtime_directory(path: Path) -> None:
        try:
            info = path.lstat()
        except OSError as exc:
            raise ModelRuntimeError("model UDS runtime directory was not created") from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ModelRuntimeError("model UDS runtime directory is not private")

    def _check_cancellation(self) -> None:
        # This observer has no release authority and is never consulted during drain.
        if self._cancellation_observer is not None:
            self._cancellation_observer()

    def _wait_runtime_directory(
        self, lease: GpuLease, row: sqlite3.Row, path: Path, *, call_deadline: float
    ) -> None:
        """Wait briefly for systemd RuntimeDirectory creation while fencing the unit."""
        deadline = min(
            self._clock() + 5.0, lease.activation_deadline, lease.total_deadline, call_deadline
        )
        while self._clock() < deadline:
            self._check_cancellation()
            fresh = self.scheduler.heartbeat(lease)
            if fresh is None:
                raise ModelRuntimeError("GPU activation lease expired before runtime directory")
            snapshot = self.units.inspect(row["unit"])
            if snapshot.load_state != "loaded":
                raise ModelRuntimeError("vLLM unit disappeared before runtime directory setup")
            if not self._validate_snapshot(row, snapshot):
                raise ModelRuntimeError("model unit identity changed during runtime setup")
            if snapshot.active_state not in {"active", "activating"}:
                raise ModelRuntimeError("vLLM process exited before runtime directory setup")
            gpu = self.gpu.snapshot()
            pids = self.units.cgroup_pids(snapshot.control_group)
            foreign = set(gpu.process_memory_mib) - set(gpu.exempt_display_pids) - set(pids)
            if foreign:
                raise ModelRuntimeError("an unadmitted GPU process appeared during model startup")
            if path.exists():
                self._secure_runtime_directory(path)
                return
            self._sleep(min(0.05, max(0, deadline - self._clock())))
        raise ModelRuntimeError("bounded systemd runtime-directory readiness expired")

    @staticmethod
    def _pid_start_ticks(pid: int) -> int:
        try:
            fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
            return int(fields[19])
        except (OSError, IndexError, ValueError) as exc:
            raise ModelRuntimeError("model systemd MainPID identity is unavailable") from exc

    @staticmethod
    def _expected_control_group(unit: str, observed: str) -> bool:
        try:
            slice_values = SystemdUnitManager._show_properties(
                GPU_SLICE, ("LoadState", "ControlGroup")
            )
        except (ModelRuntimeError, ValueError):
            return False
        slice_group = slice_values.get("ControlGroup", "").rstrip("/")
        return (
            slice_values.get("LoadState") == "loaded"
            and slice_group.startswith("/")
            and observed == f"{slice_group}/{unit}"
        )

    def _binding(self, lease: GpuLease) -> sqlite3.Row | None:
        with closing(self._connect()) as connection:
            return cast(
                sqlite3.Row | None,
                connection.execute(
                    "SELECT * FROM gpu_runtime_bindings "
                    "WHERE owner=? AND request_id=? AND fencing_token=?",
                    (lease.owner, lease.request_id, lease.fencing_token),
                ).fetchone(),
            )

    def unit_identity_receipt(self, owner: str, request_id: str) -> dict[str, object]:
        """Return the exact completed model-unit generation for a turn."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT unit,invocation_id,main_pid,main_start_ticks,control_group,"
                "launch_state,model_sha256 FROM gpu_runtime_bindings "
                "WHERE owner=? AND request_id=? ORDER BY fencing_token DESC LIMIT 1",
                (owner, request_id),
            ).fetchone()
        if (
            row is None
            or row["launch_state"] != "created"
            or not isinstance(row["invocation_id"], str)
            or re.fullmatch(r"[0-9a-f]{32}", row["invocation_id"]) is None
            or not isinstance(row["main_pid"], int)
            or row["main_pid"] <= 0
            or not isinstance(row["main_start_ticks"], int)
            or row["main_start_ticks"] <= 0
            or not isinstance(row["control_group"], str)
            or not row["control_group"].startswith("/")
        ):
            raise ModelRuntimeError("completed model turn has no verified unit generation")
        return {
            "unit": row["unit"],
            "invocation_id": row["invocation_id"],
            "main_pid": row["main_pid"],
            "main_start_ticks": row["main_start_ticks"],
            "control_group": row["control_group"],
            "model_sha256": row["model_sha256"],
        }

    def _persist_observed_gpu_pids(self, lease: GpuLease, pids: set[int]) -> None:
        """Durably remember owned GPU PIDs before systemd begins killing the unit."""
        if not pids:
            return
        row = self._binding(lease)
        if row is None or not row["invocation_id"] or not row["control_group"]:
            raise ModelRuntimeError("GPU PID observation has no persisted unit generation")
        try:
            prior = {int(pid) for pid in json.loads(row["observed_gpu_pids_json"])}
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ModelRuntimeError("persisted model GPU PID receipt is malformed") from exc
        observed = sorted(prior | pids)
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "UPDATE gpu_runtime_bindings SET observed_gpu_pids_json=? "
                "WHERE owner=? AND request_id=? AND fencing_token=? AND unit=? "
                "AND invocation_id=? AND control_group=?",
                (
                    json.dumps(observed, separators=(",", ":")),
                    lease.owner,
                    lease.request_id,
                    lease.fencing_token,
                    row["unit"],
                    row["invocation_id"],
                    row["control_group"],
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseConflict("GPU process identity changed while persisting drain evidence")

    def _prepare_unit(self, lease: GpuLease) -> sqlite3.Row:
        nonce = secrets.token_hex(32)
        suffix = uuid4().hex
        unit = SystemdUnitManager._unit_name(lease.owner, suffix)
        runtime_base = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
        if runtime_base.is_symlink() or not runtime_base.is_dir():
            raise ModelRuntimeError("user runtime directory is unavailable")
        runtime_info = runtime_base.stat()
        if runtime_info.st_uid != os.getuid() or stat.S_IMODE(runtime_info.st_mode) & 0o077:
            raise ModelRuntimeError("user runtime directory is not private")
        uds_directory = runtime_base / f"swapp-gpu-{suffix}"
        description = f"SWAPP GPU model turn {nonce}"
        created = self._clock()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO gpu_runtime_bindings(owner,request_id,fencing_token,unit,nonce,"
                "uds_directory,model_sha256,expected_description,created_boottime,"
                "runtime_max_seconds,launch_state) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,'prepared')",
                (
                    lease.owner,
                    lease.request_id,
                    lease.fencing_token,
                    unit,
                    nonce,
                    str(uds_directory),
                    self.pin.digest,
                    description,
                    created,
                    MODEL_RUNTIME_MAX_SECONDS,
                ),
            )
            connection.commit()
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    "SELECT * FROM gpu_runtime_bindings "
                    "WHERE owner=? AND request_id=? AND fencing_token=?",
                    (lease.owner, lease.request_id, lease.fencing_token),
                ).fetchone(),
            )
        if row is None:
            raise ModelRuntimeError("durable model unit intent could not be read back")
        return row

    @staticmethod
    def _diagnostic_log_path(lease: GpuLease, row: sqlite3.Row) -> Path:
        _, root = _prepare_doctor_directories()
        info = root.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ModelRuntimeError("private model diagnostics directory is unsafe")
        key = hashlib.sha256(
            f"{lease.owner}\0{lease.request_id}\0{lease.fencing_token}\0{row['nonce']}".encode()
        ).hexdigest()
        path = root / f"{key}.log"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        os.close(fd)
        return path

    @staticmethod
    def _diagnostic_receipt(path: Path) -> dict[str, object]:
        try:
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size > MAX_DIAGNOSTIC_LOG_BYTES
            ):
                return {"log_status": "unsafe"}
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(256 * 1024), b""):
                    digest.update(chunk)
            return {
                "log_status": "retained",
                "log_path": path.relative_to(PROJECT_ROOT).as_posix(),
                "log_bytes": info.st_size,
                "log_sha256": digest.hexdigest(),
                "per_file_write_limit_bytes": MAX_DIAGNOSTIC_LOG_BYTES,
            }
        except OSError:
            return {"log_status": "unavailable"}

    def _bind_unit(self, lease: GpuLease, row: sqlite3.Row, snapshot: UnitSnapshot) -> None:
        unit = row["unit"]
        if (
            snapshot.load_state != "loaded"
            or snapshot.active_state != "active"
            or re.fullmatch(r"[0-9a-f]{32}", snapshot.invocation_id) is None
            or snapshot.main_pid <= 0
            or snapshot.description != row["expected_description"]
            or f"SWAPP_GPU_TURN_NONCE={row['nonce']}" not in snapshot.environment
            or not self._expected_control_group(unit, snapshot.control_group)
            or snapshot.memory_max != MODEL_UNIT_MEMORY_BYTES
            or snapshot.memory_swap_max != 0
            or snapshot.cpu_quota_usec != MODEL_UNIT_CPU_PERCENT * 10_000
            or snapshot.tasks_max != MODEL_UNIT_TASKS
            or snapshot.runtime_max_usec != MODEL_RUNTIME_MAX_SECONDS * 1_000_000
        ):
            raise ModelRuntimeError("model unit identity or resource limits differ from its intent")
        identity_ticks = self._pid_start_ticks(snapshot.main_pid)
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE gpu_runtime_bindings SET launch_state='created',launch_finished_boottime=?,"
                "invocation_id=?,main_pid=?,main_start_ticks=?,boot_id=?,control_group=? "
                "WHERE owner=? AND request_id=? AND fencing_token=? AND unit=? "
                "AND launch_state IN ('prepared','created','uncertain')",
                (
                    self._clock(),
                    snapshot.invocation_id,
                    snapshot.main_pid,
                    identity_ticks,
                    boot_id,
                    snapshot.control_group,
                    lease.owner,
                    lease.request_id,
                    lease.fencing_token,
                    unit,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise LeaseConflict("model unit intent was replaced or already fenced")
            connection.commit()

    def _validate_snapshot(self, row: sqlite3.Row, snapshot: UnitSnapshot) -> bool:
        if snapshot.load_state == "not-found":
            return False
        return bool(
            snapshot.load_state == "loaded"
            and re.fullmatch(r"[0-9a-f]{32}", snapshot.invocation_id) is not None
            and snapshot.main_pid > 0
            and snapshot.description == row["expected_description"]
            and f"SWAPP_GPU_TURN_NONCE={row['nonce']}" in snapshot.environment
            and self._expected_control_group(row["unit"], snapshot.control_group)
            and snapshot.memory_max == MODEL_UNIT_MEMORY_BYTES
            and snapshot.memory_swap_max == 0
            and snapshot.cpu_quota_usec == MODEL_UNIT_CPU_PERCENT * 10_000
            and snapshot.tasks_max == MODEL_UNIT_TASKS
            and snapshot.runtime_max_usec == MODEL_RUNTIME_MAX_SECONDS * 1_000_000
        )

    def _wait_until_ready(
        self, lease: GpuLease, row: sqlite3.Row, socket_path: Path, *, call_deadline: float
    ) -> tuple[UnitSnapshot, GpuSnapshot, float]:
        start = self._clock()
        deadline = min(lease.activation_deadline, lease.total_deadline, call_deadline)
        while self._clock() < deadline:
            self._check_cancellation()
            current_lease = self.scheduler.heartbeat(lease)
            if current_lease is None:
                raise ModelRuntimeError("GPU activation lease expired or was fenced")
            snapshot = self.units.inspect(row["unit"])
            if snapshot.load_state == "not-found":
                raise ModelRuntimeError("vLLM unit disappeared before readiness")
            if snapshot.load_state == "loaded" and not self._validate_snapshot(row, snapshot):
                raise ModelRuntimeError(
                    "model unit identity or resource limits changed during startup"
                )
            gpu_observed = self.gpu.snapshot()
            cgroup_pids = (
                self.units.cgroup_pids(snapshot.control_group)
                if snapshot.load_state == "loaded" and snapshot.control_group
                else frozenset()
            )
            foreign_gpu_pids = (
                set(gpu_observed.process_memory_mib)
                - set(gpu_observed.exempt_display_pids)
                - set(cgroup_pids)
            )
            if foreign_gpu_pids:
                raise ModelRuntimeError("an unadmitted GPU process appeared during model startup")
            if snapshot.active_state not in {"active", "activating"}:
                if snapshot.load_state == "loaded" and snapshot.active_state in {
                    "failed",
                    "inactive",
                }:
                    raise ModelRuntimeError("vLLM model unit failed before readiness")
            else:
                try:
                    models = _read_uds_json(
                        socket_path,
                        "GET",
                        "/v1/models",
                        None,
                        timeout=1,
                        maximum=64 * 1024,
                        progress=self._check_cancellation,
                    )
                    data = models.get("data")
                    if isinstance(data, list) and data and isinstance(data[0], dict):
                        first = data[0]
                        if first.get("id") == "qwen3.5-9b":
                            if snapshot.invocation_id == "":
                                raise ModelRuntimeError("ready vLLM unit has no InvocationID")
                            self._bind_unit(lease, row, snapshot)
                            ready = self.scheduler.mark_ready(current_lease)
                            if ready is None:
                                raise ModelRuntimeError(
                                    "GPU inference lease could not be activated"
                                )
                            gpu_start = self.gpu.snapshot()
                            self._record_start_gpu(ready, gpu_start)
                            return snapshot, gpu_start, self._clock() - start
                except (_ModelEndpointUnavailable, _ModelEndpointNotReady):
                    # Startup readiness is polled only while the activation lease remains live.
                    pass
            self._sleep(min(0.25, max(0, deadline - self._clock())))
        raise ModelRuntimeError("bounded vLLM readiness deadline expired")

    def _record_start_gpu(self, lease: GpuLease, gpu: GpuSnapshot) -> None:
        row = self._binding(lease)
        if row is None:
            raise ModelRuntimeError("model runtime binding disappeared")
        snapshot = self.units.inspect(row["unit"])
        cgroup_pids = self.units.cgroup_pids(snapshot.control_group)
        foreign_gpu_pids = (
            set(gpu.process_memory_mib) - set(gpu.exempt_display_pids) - set(cgroup_pids)
        )
        if foreign_gpu_pids:
            raise ModelRuntimeError("an unadmitted GPU process appeared during model startup")
        owned_gpu_pids = sorted(pid for pid in gpu.process_memory_mib if pid in cgroup_pids)
        if not owned_gpu_pids:
            raise ModelRuntimeError("ready model unit has no observable GPU process")
        owned_memory = sum(gpu.process_memory_mib[pid] for pid in owned_gpu_pids)
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE gpu_runtime_bindings SET observed_gpu_pids_json=?,peak_gpu_memory_mib=?,"
                "start_gpu_used_memory_mib=? WHERE owner=? AND request_id=? AND fencing_token=?",
                (
                    json.dumps(owned_gpu_pids, separators=(",", ":")),
                    owned_memory,
                    gpu.used_memory_mib,
                    lease.owner,
                    lease.request_id,
                    lease.fencing_token,
                ),
            )

    def _update_measurement_peaks(self, lease: GpuLease, gpu: GpuSnapshot) -> None:
        row = self._binding(lease)
        if row is None or not row["control_group"]:
            raise ModelRuntimeError("model cgroup binding is missing")
        pids = self.units.cgroup_pids(row["control_group"])
        in_unit = [pid for pid in gpu.process_memory_mib if pid in pids]
        foreign = set(gpu.process_memory_mib) - set(gpu.exempt_display_pids) - set(pids)
        if foreign:
            raise ModelRuntimeError("an unadmitted GPU process appeared during inference")
        if not in_unit:
            return
        own_memory = sum(gpu.process_memory_mib[pid] for pid in in_unit)
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE gpu_runtime_bindings SET peak_gpu_memory_mib=max(peak_gpu_memory_mib,?) "
                "WHERE owner=? AND request_id=? AND fencing_token=?",
                (own_memory, lease.owner, lease.request_id, lease.fencing_token),
            )

    def _verify_and_drain(
        self, lease: GpuLease, *, deadline: float
    ) -> tuple[bool, float, GpuSnapshot | None]:
        started = self._clock()
        if lease.owner != "lab":
            # This callback may share SQLite with the AOS scheduler.  A missing
            # Lab binding says nothing about an AOS-owned child process.
            return False, 0.0, None
        row = self._binding(lease)
        if row is None:
            # No intent was persisted, so no model unit could have been started.
            return True, 0.0, None
        try:
            snapshot = self.units.inspect(row["unit"])
        except (OSError, ModelRuntimeError):
            return False, self._clock() - started, None
        try:
            observed_gpu = {int(pid) for pid in json.loads(row["observed_gpu_pids_json"])}
        except (json.JSONDecodeError, TypeError, ValueError):
            return False, self._clock() - started, None
        expected_cgroup = row["control_group"] or ""
        live_gpu_pids: set[int] = set()
        if snapshot.load_state != "not-found":
            if not self._validate_snapshot(row, snapshot):
                # The unit name or generation was reused by unknown code.
                return False, self._clock() - started, None
            if row["invocation_id"] and snapshot.invocation_id != row["invocation_id"]:
                return False, self._clock() - started, None
            if row["main_pid"] is not None and (
                snapshot.main_pid != row["main_pid"]
                or self._pid_start_ticks(snapshot.main_pid) != row["main_start_ticks"]
                or row["boot_id"]
                != Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
            ):
                return False, self._clock() - started, None
            expected_cgroup = expected_cgroup or snapshot.control_group
            try:
                live_pids = self.units.cgroup_pids(expected_cgroup)
                gpu_before_stop = self.gpu.snapshot()
            except (ModelRuntimeError, OSError):
                return False, self._clock() - started, None
            if not row["invocation_id"] and snapshot.active_state == "active":
                try:
                    self._bind_unit(lease, row, snapshot)
                    row = self._binding(lease) or row
                except (LeaseConflict, ModelRuntimeError, OSError):
                    return False, self._clock() - started, None
            live_gpu_pids = set(gpu_before_stop.process_memory_mib) & set(live_pids)
            if live_gpu_pids:
                try:
                    self._persist_observed_gpu_pids(lease, live_gpu_pids)
                except (LeaseConflict, ModelRuntimeError, OSError):
                    return False, self._clock() - started, None
                observed_gpu.update(live_gpu_pids)
            if snapshot.active_state in {"active", "activating", "reloading"} or live_pids:
                try:
                    self.units.stop(row["unit"], timeout_seconds=10)
                except (ModelRuntimeError, OSError):
                    return False, self._clock() - started, None
        elif row["launch_state"] == "uncertain" and self._clock() < (
            row["created_boottime"] + row["runtime_max_seconds"] + MAX_DRAIN_SECONDS
        ):
            # A timed-out systemd-run call may have reached the manager just
            # before its client was killed; RuntimeMax bounds the late unit.
            return False, self._clock() - started, None
        drain_deadline = min(deadline, started + MAX_DRAIN_SECONDS)
        while expected_cgroup and self._clock() < drain_deadline:
            try:
                same_generation_gone = True
                if snapshot.load_state != "not-found":
                    final = self.units.inspect(row["unit"])
                    same_generation_gone = final.load_state == "not-found" or (
                        final.load_state == "loaded"
                        and final.active_state == "inactive"
                        and final.invocation_id == snapshot.invocation_id
                        and final.control_group == expected_cgroup
                        and final.main_pid == 0
                    )
                pids_gone = self.units.cgroup_empty(expected_cgroup)
                gpu_now = self.gpu.snapshot()
                gpu_pids_gone = not (observed_gpu & gpu_now.process_memory_mib.keys())
                if same_generation_gone and pids_gone and gpu_pids_gone:
                    break
            except (OSError, ModelRuntimeError):
                pass
            self._sleep(0.1)
        else:
            if expected_cgroup:
                return False, self._clock() - started, None
        try:
            gpu_final = self.gpu.snapshot()
        except ModelRuntimeError:
            return False, self._clock() - started, None
        if observed_gpu & gpu_final.process_memory_mib.keys():
            return False, self._clock() - started, None
        if expected_cgroup and not self.units.cgroup_empty(expected_cgroup):
            return False, self._clock() - started, None
        if not expected_cgroup:
            foreign_gpu = set(gpu_final.process_memory_mib) - set(gpu_final.exempt_display_pids)
            if foreign_gpu:
                return False, self._clock() - started, None
        elif (
            set(gpu_final.process_memory_mib)
            - set(gpu_final.exempt_display_pids)
            - set(self.units.cgroup_pids(expected_cgroup))
        ):
            # Keep the turn quarantined while another unadmitted GPU PID remains.
            return False, self._clock() - started, None
        return True, self._clock() - started, gpu_final

    def verify_drained(self, lease: GpuLease) -> bool:
        """Trusted scheduler callback: drain exact unit and observe GPU PID release."""
        allowed, _, _ = self._verify_and_drain(
            lease,
            deadline=self._clock() + MAX_DRAIN_SECONDS,
        )
        return allowed

    def recover_quarantined(self) -> bool:
        """Drain a quarantined recorded model generation before releasing the queue."""
        return self.scheduler.recover_quarantined()

    def _set_launch_result(self, lease: GpuLease, outcome: str) -> None:
        if outcome not in {"created", "uncertain"}:
            raise ValueError("invalid durable model launch state")
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "UPDATE gpu_runtime_bindings SET launch_state=?,launch_finished_boottime=? "
                "WHERE owner=? AND request_id=? AND fencing_token=? "
                "AND launch_state IN ('prepared','uncertain')",
                (outcome, self._clock(), lease.owner, lease.request_id, lease.fencing_token),
            )
            if cursor.rowcount != 1:
                raise LeaseConflict("durable model launch intent was replaced")

    def _read_socket_text(
        self,
        lease: GpuLease,
        row: sqlite3.Row,
        messages: Sequence[Mapping[str, str]],
        *,
        max_output_tokens: int,
        enable_thinking: bool,
        thinking_token_budget: int | None,
        temperature: float,
        top_p: float,
        top_k: int | None,
        response_schema_name: str | None,
        response_schema: Mapping[str, object] | None,
        call_deadline: float,
    ) -> tuple[str, int, int, float]:
        initial_lease = self.scheduler.mark_ready(lease)
        if initial_lease is None:
            raise ModelRuntimeError("model turn no longer owns the GPU inference phase")
        current: GpuLease = initial_lease
        socket_path = Path(row["uds_directory"]) / "vllm.sock"
        body = {
            "model": "qwen3.5-9b",
            "messages": list(messages),
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_output_tokens,
            "chat_template_kwargs": {"enable_thinking": enable_thinking},
            "stream": False,
        }
        if top_k is not None:
            body["top_k"] = top_k
        if thinking_token_budget is not None:
            body["thinking_token_budget"] = thinking_token_budget
        if response_schema is not None:
            if response_schema_name is None:
                raise ValueError("structured response schema must have a trusted name")
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_schema_name,
                    "strict": True,
                    "schema": dict(response_schema),
                },
            }
        if (
            len(json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            > MAX_PROMPT_BYTES
        ):
            raise ValueError("encoded model messages exceed the configured request bound")
        started = self._clock()

        def progress() -> None:
            nonlocal current
            self._check_cancellation()
            fresh = self.scheduler.heartbeat(current)
            if fresh is None:
                raise ModelRuntimeError("model inference lease expired or was fenced")
            current = fresh
            unit_snapshot = self.units.inspect(row["unit"])
            if not self._validate_snapshot(row, unit_snapshot):
                raise ModelRuntimeError("model unit identity changed during inference")
            snapshot = self.gpu.snapshot()
            self._update_measurement_peaks(current, snapshot)

        progress()
        initial = self._clock()
        remaining = min(current.inference_deadline, current.total_deadline, call_deadline) - initial
        if remaining <= 0:
            raise ModelRuntimeError("model inference deadline expired")
        transport_metadata: dict[str, object] = {}
        response = _read_uds_json(
            socket_path,
            "POST",
            "/v1/chat/completions",
            body,
            timeout=remaining,
            progress=progress,
            metadata_sink=transport_metadata,
        )
        inference_seconds = self._clock() - started
        self._last_response_metadata = _capture_response_metadata(response, transport_metadata)
        _reject_output_truncation(self._last_response_metadata)
        choices = response.get("choices")
        usage = response.get("usage")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ModelRuntimeError("vLLM response has an invalid choice shape")
        message = choices[0].get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise ModelRuntimeError("vLLM response has an invalid message body")
        tool_calls = message.get("tool_calls", [])
        if tool_calls not in (None, []):
            raise ModelRuntimeError("doctor profile does not execute model tool calls")
        if not isinstance(usage, dict):
            raise ModelRuntimeError("vLLM response omitted token usage")
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        if (
            isinstance(prompt_tokens, bool)
            or not isinstance(prompt_tokens, int)
            or prompt_tokens < 0
            or isinstance(completion_tokens, bool)
            or not isinstance(completion_tokens, int)
            or not 0 <= completion_tokens <= max_output_tokens
        ):
            raise ModelRuntimeError("vLLM response token usage is outside its budget")
        return message["content"], prompt_tokens, completion_tokens, inference_seconds

    def _await_ticket(
        self, owner: str, request_id: str, payload: bytes, *, call_deadline: float
    ) -> GpuLease:
        if self.scheduler.max_total_seconds < (
            min(self.scheduler.max_activation_seconds, MAX_ACTIVATION_SECONDS)
            + min(self.scheduler.max_inference_seconds, MAX_INFERENCE_SECONDS)
        ):
            raise ModelRuntimeError("scheduler total budget cannot cover activation and inference")
        remaining = call_deadline - self._clock()
        if remaining <= 0:
            raise ModelRuntimeError("model episode deadline expired before GPU queue admission")
        queue_timeout = min(self.scheduler.queue_timeout_seconds, max(1, math.ceil(remaining)))
        self._check_cancellation()
        entry = self.scheduler.submit(
            owner,
            request_id,
            payload,
            activation_seconds=min(self.scheduler.max_activation_seconds, MAX_ACTIVATION_SECONDS),
            inference_seconds=min(self.scheduler.max_inference_seconds, MAX_INFERENCE_SECONDS),
            total_seconds=min(self.scheduler.max_total_seconds, MODEL_RUNTIME_MAX_SECONDS),
            queue_timeout_seconds=queue_timeout,
        )
        try:
            while self._clock() < min(entry.queue_deadline, call_deadline):
                self._check_cancellation()
                lease = self.scheduler.try_acquire(owner, request_id)
                if lease is not None:
                    return lease
                self._sleep(0.2)
        except BaseException:
            # Authenticated cancel only touches this invocation's queued request.
            self.scheduler.cancel_queued(owner, request_id)
            raise
        self.scheduler.cancel_queued(owner, request_id)
        raise ModelRuntimeError("GPU ticket expired while waiting for a fair turn")

    def run_turn(
        self,
        owner: str,
        request_id: str,
        messages: Sequence[Mapping[str, str]],
        *,
        enable_thinking: bool,
        max_output_tokens: int = 256,
        thinking_token_budget: int | None = None,
        temperature: float = 0.0,
        top_p: float = 1.0,
        top_k: int | None = None,
        response_schema_name: str | None = None,
        response_schema: Mapping[str, object] | None = None,
        output_token_limit: int | None = None,
        profile: ModelTurnProfile | None = None,
    ) -> ModelReply:
        """Run one bounded local model turn; labels and raw series are rejected by callers."""
        self._failure_phase = None
        self._last_response_metadata = None
        self._last_request_profile = None
        self._phase = "request_validation"
        call_started = self._clock()
        if owner != "lab":
            raise ValueError("native Qwen turns are Lab-owned; AOS uses fixed broker profiles")
        selected_profile = profile or self.profile
        if MODEL_TURN_PROFILES.get(selected_profile.profile_id) != selected_profile:
            raise ValueError("requested model profile is not a trusted registered profile")
        if selected_profile != self.profile:
            raise ValueError("requested model profile differs from this trusted runtime")
        if profile is not None:
            enable_thinking = profile.enable_thinking
            if output_token_limit is None:
                max_output_tokens = profile.max_output_tokens
            elif (
                isinstance(output_token_limit, bool)
                or not isinstance(output_token_limit, int)
                or not 1 <= output_token_limit <= profile.max_output_tokens
            ):
                raise ValueError("per-turn output limit must fit the trusted profile")
            else:
                max_output_tokens = output_token_limit
            thinking_token_budget = profile.thinking_token_budget
            temperature = profile.temperature
            top_p = profile.top_p
            top_k = profile.top_k
        effective_activation = min(
            selected_profile.activation_seconds, self.scheduler.max_activation_seconds
        )
        effective_inference = min(
            selected_profile.inference_seconds, self.scheduler.max_inference_seconds
        )
        effective_total = min(selected_profile.total_seconds, self.scheduler.max_total_seconds)
        if not request_id or len(request_id) > 128:
            raise ValueError("a bounded request ID is required")
        if (
            isinstance(max_output_tokens, bool)
            or not isinstance(max_output_tokens, int)
            or not 1 <= max_output_tokens <= MAX_OUTPUT_TOKENS
        ):
            raise ValueError("max output tokens exceed the registered model output bound")
        if not isinstance(enable_thinking, bool):
            raise ValueError("an explicit boolean enable_thinking profile is required")
        thinking_token_budget = _validate_thinking_token_budget(
            thinking_token_budget,
            enable_thinking=enable_thinking,
            max_output_tokens=max_output_tokens,
        )
        temperature, top_p = _validate_sampling_parameters(temperature, top_p)
        top_k = _validate_top_k(top_k)
        if (response_schema_name is None) != (response_schema is None):
            raise ValueError("structured response schema name and body must be supplied together")
        response_schema_sha256: str | None = None
        frozen_response_schema: dict[str, object] | None = None
        if response_schema is not None:
            if (
                not isinstance(response_schema_name, str)
                or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", response_schema_name)
                or not isinstance(response_schema, Mapping)
            ):
                raise ValueError("structured response schema identity is invalid")
            schema_bytes = json.dumps(
                response_schema,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            if not schema_bytes or len(schema_bytes) > 16 * 1024:
                raise ValueError("structured response schema exceeds its fixed size bound")
            response_schema_sha256 = hashlib.sha256(schema_bytes).hexdigest()
            frozen_schema = json.loads(schema_bytes)
            if not isinstance(frozen_schema, dict):
                raise ValueError("structured response schema must be one JSON object")
            frozen_response_schema = cast(dict[str, object], frozen_schema)
        if not isinstance(messages, Sequence) or not messages or len(messages) > 32:
            raise ValueError("one to 32 typed chat messages are required")
        normalized: list[dict[str, str]] = []
        for message in messages:
            if not isinstance(message, Mapping) or set(message) != {"role", "content"}:
                raise ValueError("each message must contain only role and content")
            role, content = message["role"], message["content"]
            if role not in {"system", "user", "assistant"} or not isinstance(content, str):
                raise ValueError("chat role or content has an invalid type")
            normalized.append({"role": role, "content": content})
        payload = json.dumps(
            {
                "model_sha256": self.pin.digest,
                "profile_id": selected_profile.profile_id,
                "model_max_len": selected_profile.model_max_len,
                "max_context_tokens": selected_profile.max_context_tokens,
                "activation_seconds": effective_activation,
                "inference_seconds": effective_inference,
                "total_seconds": effective_total,
                "messages": normalized,
                "max_output_tokens": max_output_tokens,
                "enable_thinking": enable_thinking,
                "thinking_token_budget": thinking_token_budget,
                "temperature": temperature,
                "top_p": top_p,
                "top_k": top_k,
                "response_schema_name": response_schema_name,
                "response_schema_sha256": response_schema_sha256,
                "response_schema": frozen_response_schema,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > MAX_PROMPT_BYTES:
            raise ValueError("model request exceeds the configured prompt size")
        self._last_request_profile = {
            "profile_id": selected_profile.profile_id,
            "system": selected_profile.system,
            "max_context_tokens": selected_profile.max_context_tokens,
            "model_max_len": selected_profile.model_max_len,
            "activation_seconds": effective_activation,
            "inference_seconds": effective_inference,
            "total_seconds": effective_total,
            "max_output_tokens": max_output_tokens,
            "enable_thinking": enable_thinking,
            "thinking_token_budget": thinking_token_budget,
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
            "response_schema_name": response_schema_name,
            "response_schema_sha256": response_schema_sha256,
        }
        self._preflight_disk()
        self._phase = "model_pin_verification"
        self.pin.verify_files()
        self._phase = "aggregate_slice_verification"
        self.units.ensure_slice()
        self._phase = "fair_queue_wait"
        call_deadline = call_started + effective_total
        lease = self._await_ticket(owner, request_id, payload, call_deadline=call_deadline)
        row: sqlite3.Row | None = None
        start_gpu: GpuSnapshot | None = None
        startup_seconds = 0.0
        inference_seconds = 0.0
        host_peak = 0
        cpu_usage = 0
        try:
            self._check_cancellation()
            # Recheck capacity immediately before the first external model side effect.
            self._phase = "host_preflight"
            self._preflight_host()
            self._phase = "durable_unit_intent"
            row = self._prepare_unit(lease)
            diagnostic_log = self._diagnostic_log_path(lease, row)
            self._phase = "unit_launch"
            self._check_cancellation()
            self._set_launch_result(lease, "uncertain")
            self.units.launch(
                row["unit"],
                row["nonce"],
                Path(row["uds_directory"]),
                self.pin,
                diagnostic_log,
                model_max_len=self.profile.model_max_len,
            )
            self._set_launch_result(lease, "created")
            self._wait_runtime_directory(
                lease, row, Path(row["uds_directory"]), call_deadline=call_deadline
            )
            self._phase = "model_startup"
            snapshot, start_gpu, startup_seconds = self._wait_until_ready(
                lease,
                row,
                Path(row["uds_directory"]) / "vllm.sock",
                call_deadline=call_deadline,
            )
            row = self._binding(lease) or row
            self._phase = "model_inference"
            text, prompt_tokens, completion_tokens, inference_seconds = self._read_socket_text(
                lease,
                row,
                normalized,
                max_output_tokens=max_output_tokens,
                enable_thinking=enable_thinking,
                thinking_token_budget=thinking_token_budget,
                temperature=temperature,
                top_p=top_p,
                top_k=top_k,
                response_schema_name=response_schema_name,
                response_schema=frozen_response_schema,
                call_deadline=call_deadline,
            )
            host_peak = self.units.cgroup_memory_peak(snapshot.control_group)
            cpu_usage = self.units.cgroup_cpu_usage(snapshot.control_group)
            return_value = (text, prompt_tokens, completion_tokens)
        except BaseException:
            # Never return the shared turn until the exact cgroup and its GPU PIDs drain.
            self._failure_phase = self._phase
            self._phase = "failure_drain"
            try:
                self.scheduler.release(lease)
            except LeaseConflict:
                try:
                    self.scheduler.recover_quarantined(lease)
                except (LeaseConflict, ModelRuntimeError, OSError, subprocess.SubprocessError):
                    pass
            except (ModelRuntimeError, OSError, subprocess.SubprocessError):
                pass
            raise
        self._phase = "normal_drain"
        drain_started = self._clock()
        self.scheduler.release(lease)
        drain_seconds = self._clock() - drain_started
        end_gpu = self.gpu.snapshot()
        if start_gpu is None:
            raise ModelRuntimeError("model GPU release observation is incomplete")
        measurements = ModelMeasurements(
            startup_seconds=startup_seconds,
            inference_seconds=inference_seconds,
            drain_seconds=drain_seconds,
            peak_host_memory_bytes=host_peak,
            peak_gpu_memory_mib=(self._binding(lease) or {"peak_gpu_memory_mib": 0})[
                "peak_gpu_memory_mib"
            ],
            start_gpu_used_memory_mib=start_gpu.used_memory_mib,
            end_gpu_used_memory_mib=end_gpu.used_memory_mib,
            cpu_usage_usec=cpu_usage,
        )
        self._phase = "completed"
        return ModelReply(return_value[0], return_value[1], return_value[2], measurements)


_DOCTOR_MESSAGES: dict[str, tuple[dict[str, str], ...]] = {
    "s1": (
        {"role": "system", "content": "Reply with one short sentence. Do not call tools."},
        {"role": "user", "content": "What is 17 + 25? Give the answer in one short sentence."},
    ),
    "s2": (
        {"role": "system", "content": "Think briefly, then give one concise answer. No tools."},
        {"role": "user", "content": "Name one safe way to verify a local file copy."},
    ),
    "research-s1": (
        {"role": "system", "content": "Reply with one short sentence. Do not call tools."},
        {"role": "user", "content": "What is 17 + 25? Give the answer in one short sentence."},
    ),
    "research-s2": (
        {"role": "system", "content": "Think briefly, then give one concise answer. No tools."},
        {"role": "user", "content": "Name one safe way to verify a local file copy."},
    ),
}
_DOCTOR_PROFILES = {
    "s1": DIAGNOSTIC_S1_PROFILE,
    "s2": DIAGNOSTIC_S2_PROFILE,
    "research-s1": LOCAL_RESEARCH_S1_PROFILE,
    "research-s2": LOCAL_RESEARCH_S2_PROFILE,
}


def _private_json_write(path: Path, document: Mapping[str, object]) -> Path:
    """Atomically persist a small owner-only JSON diagnostic."""
    root = path.parent
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root_info = root.lstat()
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or stat.S_ISLNK(root_info.st_mode)
        or root_info.st_uid != os.getuid()
    ):
        raise ModelRuntimeError("doctor output directory is not privately owned")
    os.chmod(root, 0o700)
    if path.exists() or path.is_symlink():
        raise ModelRuntimeError("doctor diagnostic already exists")
    encoded = json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2).encode() + b"\n"
    temporary = root / f".{path.name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
        temporary.unlink()
        directory_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def _write_doctor_result(
    request_id: str,
    profile: str,
    reply: ModelReply,
    diagnostics: Mapping[str, object],
) -> Path:
    """Persist bounded diagnostic output under an owned 0700 directory."""
    root = PROJECT_ROOT / "data/runtime/native-doctor"
    path = root / f"{request_id}.json"
    document = {
        "schema": "native-model-doctor.v1",
        "request_id": request_id,
        "model_repository": MODEL_REPOSITORY,
        "model_revision": MODEL_REVISION,
        "model_sha256": ModelPin.from_repository().digest,
        "profile": profile,
        "diagnostics": dict(diagnostics),
        "text": reply.text,
        "prompt_tokens": reply.prompt_tokens,
        "completion_tokens": reply.completion_tokens,
        "measurements": {
            "startup_seconds": reply.measurements.startup_seconds,
            "inference_seconds": reply.measurements.inference_seconds,
            "drain_seconds": reply.measurements.drain_seconds,
            "peak_host_memory_bytes": reply.measurements.peak_host_memory_bytes,
            "peak_gpu_memory_mib": reply.measurements.peak_gpu_memory_mib,
            "start_gpu_used_memory_mib": reply.measurements.start_gpu_used_memory_mib,
            "end_gpu_used_memory_mib": reply.measurements.end_gpu_used_memory_mib,
            "cpu_usage_usec": reply.measurements.cpu_usage_usec,
        },
    }
    return _private_json_write(path, document)


def _doctor_runtime_diagnostic(database: Path, request_id: str) -> dict[str, object]:
    """Read the exact latest owned binding and its capped retained log receipt."""
    if not database.is_file() or database.is_symlink():
        return {"unit_status": "not_started"}
    try:
        with closing(
            sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=2)
        ) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM gpu_runtime_bindings WHERE owner='lab' AND request_id=? "
                "ORDER BY fencing_token DESC LIMIT 1",
                (request_id,),
            ).fetchone()
        if row is None:
            return {"unit_status": "not_started"}
        key = hashlib.sha256(
            f"lab\0{request_id}\0{row['fencing_token']}\0{row['nonce']}".encode()
        ).hexdigest()
        log_path = PROJECT_ROOT / "data/runtime/native-doctor/logs" / f"{key}.log"
        receipt = {
            "unit": row["unit"],
            "invocation_id": row["invocation_id"],
            "main_pid": row["main_pid"],
            "control_group": row["control_group"],
            "launch_state": row["launch_state"],
            **OwnedVllmRuntime._diagnostic_receipt(log_path),
        }
        return receipt
    except (OSError, sqlite3.Error, KeyError, TypeError):
        return {"unit_status": "unavailable"}


def doctor_main(argv: Sequence[str] | None = None) -> int:
    """Run one fixed S1/S2 diagnostic as an authenticated GPU service process."""
    parser = argparse.ArgumentParser(prog="lab-gpu-doctor")
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--profile", choices=tuple(_DOCTOR_MESSAGES), required=True)
    args = parser.parse_args(argv)
    if re.fullmatch(r"[0-9a-f]{32}", args.request_id) is None:
        parser.error("request-id must be 32 lowercase hexadecimal characters")
    aos_unit = os.environ.get("SWAPP_AOS_GPU_UNIT", "")
    lab_unit = os.environ.get("SWAPP_LAB_GPU_UNIT", "")
    database_value = os.environ.get("SWAPP_GPU_RUNTIME_DB", "")
    if not aos_unit or not lab_unit or not database_value:
        parser.error("trusted service unit mapping and runtime database are required")
    database = Path(database_value)
    allowed_root = PROJECT_ROOT / "data/runtime/gpu"
    if database.is_symlink() or allowed_root not in database.resolve().parents:
        parser.error("runtime database must be within the private GPU runtime directory")
    runtime: OwnedVllmRuntime | None = None
    try:
        _prepare_doctor_directories()
        resolver = SystemdPrincipalResolver({"aos": aos_unit, "lab": lab_unit})
        profile = _DOCTOR_PROFILES[args.profile]
        runtime = OwnedVllmRuntime(database, principal_resolver=resolver, profile=profile)
        reply = runtime.run_turn(
            "lab",
            args.request_id,
                _DOCTOR_MESSAGES[args.profile],
                enable_thinking=profile.enable_thinking,
                max_output_tokens=profile.max_output_tokens,
                thinking_token_budget=profile.thinking_token_budget,
                temperature=profile.temperature,
                top_p=profile.top_p,
                top_k=profile.top_k,
                profile=profile,
        )
        diagnostics = _doctor_runtime_diagnostic(database, args.request_id)
        diagnostics["request_profile"] = runtime._last_request_profile
        if runtime._last_response_metadata is not None:
            diagnostics["response_metadata"] = runtime._last_response_metadata
        result_path = _write_doctor_result(args.request_id, args.profile, reply, diagnostics)
        receipt = {
            "status": "ok",
            "capacity_status": "measurement_only_not_capacity_acceptance",
            "request_id": args.request_id,
            "profile": args.profile,
            "result_path": result_path.relative_to(PROJECT_ROOT).as_posix(),
            "text_sha256": hashlib.sha256(reply.text.encode("utf-8")).hexdigest(),
            "diagnostics": diagnostics,
            "startup_seconds": reply.measurements.startup_seconds,
            "inference_seconds": reply.measurements.inference_seconds,
            "drain_seconds": reply.measurements.drain_seconds,
            "peak_host_memory_bytes": reply.measurements.peak_host_memory_bytes,
            "peak_gpu_memory_mib": reply.measurements.peak_gpu_memory_mib,
        }
        print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception as exc:
        phase = (
            (runtime._failure_phase or runtime._phase) if runtime is not None else "configuration"
        )
        diagnostics = _doctor_runtime_diagnostic(database, args.request_id)
        if runtime is not None and runtime._last_request_profile is not None:
            diagnostics["request_profile"] = runtime._last_request_profile
        if runtime is not None and runtime._last_response_metadata is not None:
            diagnostics["response_metadata"] = runtime._last_response_metadata
        raw_message = str(exc)
        safe_message = "".join(char for char in raw_message if char >= " " and char != "\x7f")[:256]
        failure = {
            "schema": "native-model-doctor-failure.v1",
            "request_id": args.request_id,
            "profile": args.profile,
            "phase": phase,
            "failure_class": (
                "generation_budget_exhausted"
                if isinstance(exc, ModelOutputBudgetExceeded)
                else "runtime_failure"
            ),
            "capacity_status": "measurement_only_not_capacity_acceptance",
            "error_type": type(exc).__name__,
            "error_message": safe_message,
            "diagnostics": diagnostics,
        }
        try:
            failure_path = _private_json_write(
                PROJECT_ROOT / "data/runtime/native-doctor" / f"{args.request_id}.failure.json",
                failure,
            )
            output = {
                "status": "failed",
                "error": "native_model_doctor_failed",
                "failure_path": failure_path.relative_to(PROJECT_ROOT).as_posix(),
            }
        except (OSError, ModelRuntimeError):
            output = {"status": "failed", "error": "native_model_doctor_failed"}
        print(json.dumps(output, sort_keys=True, separators=(",", ":")), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(doctor_main())
