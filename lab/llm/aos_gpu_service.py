"""Bounded entry point for the isolated Lab/AOS shared GPU broker draft."""

from __future__ import annotations

import array
import concurrent.futures
import errno
import math
import os
import re
import select
import socket
import sqlite3
import stat
import threading
import time
from dataclasses import asdict
from pathlib import Path
from subprocess import SubprocessError  # nosec B404 -- exception type only; no process launch here
from typing import Any

from lab.llm.aos_gpu_broker import LabAOSBroker, PeerGeneration
from lab.llm.aos_gpu_control import (
    CALL_SECONDS,
    CONTROL_BACKLOG,
    CONTROL_WORKERS,
    ControlPolicy,
    LabAOSControl,
)
from lab.llm.aos_gpu_control_store import ControlStore, ControlStoreError, control_deadline
from lab.llm.aos_gpu_executor import (
    PROJECT_ROOT,
    AOSProfile,
    BrokerOwnedTurnExecutor,
    ProfileRegistry,
    SystemdAOSProfileRuntime,
    SystemdSocketPeerAuthenticator,
    TurnBudgets,
)
from lab.llm.aos_profile_output import decode as decode_output
from lab.llm.aos_retained_channel import RetainedChannelBootstrap
from lab.llm.aos_retained_provider import RetainedProviderServer, prepare_channel
from lab.llm.gpu_scheduler import (
    _boot_id,
    _process_cgroup,
    _read_process_identity,
    _systemctl_show,
    boottime,
)
from lab.llm.native_runtime import NvidiaSmiObserver, SystemdUnitManager
from lab.llm.shared_launch_authority import SharedLaunchAuthority
from lab.llm.shared_launch_ledger import LaunchLedgerError
from lab.llm.shared_launch_transport import WIRE_SCHEMA_SHA256

MAX_CONFIG_BYTES = 64 * 1024
WORKERS = 4
PROFILE_KEYS = frozenset({"aos.decider.turn.v1", "aos.bonsai.recovery.v1", "aos.bonsai.vision.v1"})
BROKER_UNIT = "swapp-lab-gpu-broker.service"
SHARED_DESKTOP_UNIT = "swapp-aos-gpu-shared-desktop-default.service"
_SHARED_ADMISSION_DENIALS = frozenset(
    {
        "draft_disabled",
        "entry_required",
        "entry_admission_closed",
        "admission_closed",
        "expired_or_not_yet_issued",
        "unauthorized_principal",
        "unreviewed_request",
        "request_conflict",
        "intent_conflict",
        "launch_policy_disabled",
        "policy_changed",
        "control_policy_disabled",
        "control_binding_changed",
        "runtime_admission_changed",
        "reviewed_file_changed",
        "private_review_required",
        "prerequisite_verifier_required",
        "workspace_changed",
        "invalid_provision",
        "plan_changed",
        "activation_changed",
        "scope_not_pristine",
    }
)
_SHARED_GENERATION_DENIALS = frozenset(
    {
        "wrong_boot",
        "stale_generation",
        "stale_broker",
        "stale_service",
        "service_mainpid_required",
        "runtime_caller_changed",
        "entered_generation_conflict",
        "pid_namespace_changed",
    }
)


def _verify_current_admission(
    connection: sqlite3.Connection,
    binding: dict[str, Any],
    *,
    control: LabAOSControl,
    shared_launch_authority: SharedLaunchAuthority | None = None,
) -> None:
    """Verify current turn authority, then require the original shared service entry.

    Trusted bootstrap supplies the authority with phase-aware prerequisites.
    No listener, policy path or prerequisite is created by this composition.
    Both verifiers use the caller's existing canonical arbiter transaction.
    """
    control.verify_saved_admission(connection, binding)
    caller = binding["caller_generation"]
    if caller["unit"] != SHARED_DESKTOP_UNIT:
        return
    if (
        shared_launch_authority is None
        or shared_launch_authority.ledger.database != Path(control.store.database).absolute()
        or shared_launch_authority.contract_sha256 != WIRE_SCHEMA_SHA256
    ):
        raise ControlStoreError("unauthorized")
    inherited = control_deadline.get()
    if inherited is not None and (
        type(inherited) not in (float, int) or not math.isfinite(inherited)
    ):
        raise ControlStoreError("deadline_exceeded")
    remaining = 3.0 if inherited is None else min(3.0, inherited - time.monotonic())
    if remaining <= 0:
        raise ControlStoreError("deadline_exceeded")
    try:
        shared_launch_authority.verify_runtime(connection, binding, deadline=boottime() + remaining)
    except LaunchLedgerError as error:
        # An unreadable observer/DB is not an affirmative revocation witness.
        if isinstance(error.__cause__, (OSError, sqlite3.Error, SubprocessError)):
            raise ControlStoreError("internal_unavailable") from error
        code = str(error)
        if code in _SHARED_ADMISSION_DENIALS:
            raise ControlStoreError("unauthorized") from error
        if code in _SHARED_GENERATION_DENIALS:
            raise ControlStoreError("stale_generation") from error
        if code == "deadline_exceeded":
            raise ControlStoreError("deadline_exceeded") from error
        raise ControlStoreError("internal_unavailable") from error
    except (OSError, sqlite3.Error, SubprocessError) as error:
        raise ControlStoreError("internal_unavailable") from error


def _broker_unit_snapshot() -> dict[str, str]:
    deadline = control_deadline.get()
    remaining = 3.0 if deadline is None else min(3.0, deadline - time.monotonic())
    if remaining <= 0:
        raise ValueError("broker identity deadline expired")
    return _systemctl_show(BROKER_UNIT, owner="lab", timeout=remaining)


def _broker_generation() -> dict[str, object]:
    """Bind the broker to its actual fixed unit, invocation and process birth."""
    pid = os.getpid()
    identity = _read_process_identity(pid)
    values = _broker_unit_snapshot()
    group = values["ControlGroup"]
    if (
        identity is None
        or identity.boot_id != _boot_id()
        or values["LoadState"] != "loaded"
        or values["ActiveState"] != "active"
        or values["MainPID"] != str(pid)
        or re.fullmatch(r"[0-9a-f]{32}", values["InvocationID"]) is None
        or not group.startswith("/")
        or ".." in Path(group).parts
        or not group.endswith("/" + BROKER_UNIT)
        or _process_cgroup(pid) != group
        or _read_process_identity(pid) != identity
        or _broker_unit_snapshot() != values
    ):
        raise ValueError("broker must run in its exact authenticated systemd generation")
    return {
        **asdict(identity),
        "uid": os.getuid(),
        "unit": BROKER_UNIT,
        "invocation_id": values["InvocationID"],
        "control_group": group,
    }


def _broker_still_current(generation: dict[str, object]) -> bool:
    try:
        return _broker_generation() == generation
    except (OSError, ValueError, RuntimeError, SubprocessError):
        return False


def _private_json(path: Path) -> dict[str, object]:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or path.is_symlink()
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
        or info.st_size > MAX_CONFIG_BYTES
    ):
        raise ValueError("broker profile config must be a bounded private regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        raw = os.read(descriptor, MAX_CONFIG_BYTES + 1)
    finally:
        os.close(descriptor)
    if not raw or len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("broker profile config exceeds its bound")
    value = decode_output(raw, limit=MAX_CONFIG_BYTES)
    if not isinstance(value, dict):
        raise ValueError("broker profile config must be an object")
    return value


def load_profiles(path: Path, *, source_root: Path) -> ProfileRegistry:
    config = _private_json(path)
    if (
        set(config) != {"schema", "source_root", "profiles"}
        or config.get("schema") != "swapp-aos-gpu-profiles.v1"
    ):
        raise ValueError("broker profile config schema differs")
    configured_root_value = config.get("source_root")
    if not isinstance(configured_root_value, str):
        raise ValueError("broker profile config source root must be text")
    configured_root = Path(configured_root_value)
    if (
        configured_root != source_root
        or configured_root.is_symlink()
        or not configured_root.is_dir()
    ):
        raise ValueError("broker source root is not the fixed rebased AOS snapshot")
    entries = config["profiles"]
    if not isinstance(entries, dict) or set(entries) != PROFILE_KEYS:
        raise ValueError("broker config must pin all three AOS profile entries")
    profiles: dict[str, AOSProfile] = {}
    for profile_id, item in entries.items():
        if not isinstance(item, dict):
            raise ValueError("broker profile entry must be an object")
        keys = {
            "kind",
            "manifest",
            "manifest_sha256",
            "deployment_digest",
            "python",
            "model_paths",
            "budgets",
            "response_schema_sha256",
            "temperature",
            "max_output_tokens",
            "context_tokens",
        }
        if (
            set(item) not in (keys, keys | {"output_contract"})
            or ("output_contract" in item and not isinstance(item["output_contract"], dict))
            or not isinstance(item["model_paths"], list)
            or not isinstance(item["budgets"], dict)
        ):
            raise ValueError("broker profile entry fields differ")
        if set(item["budgets"]) != {
            "activation_seconds",
            "inference_seconds",
            "total_seconds",
            "queue_seconds",
        }:
            raise ValueError("broker profile deadline fields differ")
        profile = AOSProfile(
            profile_id=profile_id,
            kind=item["kind"],
            manifest=Path(item["manifest"]),
            manifest_sha256=item["manifest_sha256"],
            deployment_digest=item["deployment_digest"],
            python=Path(item["python"]),
            source_root=configured_root,
            model_paths=tuple(Path(value) for value in item["model_paths"]),
            budgets=TurnBudgets(**item["budgets"]),
            response_schema_sha256=item["response_schema_sha256"],
            temperature=item["temperature"],
            max_output_tokens=item["max_output_tokens"],
            context_tokens=item["context_tokens"],
            output_contract=item.get("output_contract"),
        )
        profiles[profile_id] = profile
    return ProfileRegistry(profiles)


def _runtime_directory() -> Path:
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", ""))
    expected = Path("/run/user") / str(os.getuid())
    if (
        runtime != expected
        or runtime.is_symlink()
        or not runtime.is_dir()
        or runtime.stat().st_uid != os.getuid()
    ):
        raise ValueError("broker requires the current user's fixed systemd runtime directory")
    return runtime


def _prepare_socket_path(socket_path: Path) -> None:
    """Remove only a same-user stale socket; preserve live or foreign paths."""
    try:
        prior = socket_path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(prior.st_mode) or prior.st_uid != os.getuid():
        raise FileExistsError("refusing to replace a non-owned broker socket path")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.2)
    try:
        probe.connect(str(socket_path))
    except OSError as exc:
        if exc.errno not in {errno.ECONNREFUSED, errno.ENOENT}:
            raise FileExistsError("existing broker socket may still be active") from exc
    else:
        raise FileExistsError("broker socket is already served")
    finally:
        probe.close()
    current = socket_path.lstat()
    if (current.st_dev, current.st_ino) != (prior.st_dev, prior.st_ino):
        raise FileExistsError("broker socket changed while checking stale ownership")
    socket_path.unlink()


def _control_listener(
    listener: socket.socket,
    control: LabAOSControl,
    stopping: threading.Event,
    channels: RetainedChannelBootstrap | None = None,
) -> None:
    """Independent bounded capacity; never share the four inference handlers."""
    capacity = threading.BoundedSemaphore(CONTROL_WORKERS)

    def handle(connection: socket.socket) -> None:
        try:
            deadline = time.monotonic() + CALL_SECONDS
            if channels is not None and channels.is_request(
                _peek_control_frame(connection, deadline)
            ):
                channels.serve(connection, deadline=deadline)
            else:
                control.serve_connection(connection, deadline=deadline)
        except (OSError, ValueError, RuntimeError):
            pass
        finally:
            connection.close()
            capacity.release()

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=CONTROL_WORKERS, thread_name_prefix="gpu-control"
    ) as pool:
        while not stopping.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if stopping.is_set():
                    break
                raise
            if not capacity.acquire(blocking=False):
                connection.close()
                continue
            pool.submit(handle, connection)


def _peek_control_frame(connection: socket.socket, deadline: float) -> bytes:
    """Route one bounded frame without consuming its sender credentials or FDs.

    MSG_PEEK duplicates delivered SCM_RIGHTS; close every duplicate, including
    malformed requests. The selected handler consumes and validates the actual
    frame. This routing wait shares the handler's original deadline.
    """
    frame_deadline = min(deadline, time.monotonic() + 1.0)
    while True:
        remaining = frame_deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("original control frame deadline expired")
        connection.settimeout(remaining)
        raw, ancillary, flags, _address = connection.recvmsg(
            8193,
            socket.CMSG_SPACE(253 * 4) + socket.CMSG_SPACE(12),
            socket.MSG_PEEK | socket.MSG_CMSG_CLOEXEC,
        )
        for level, kind, payload in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                descriptors = array.array("i")
                descriptors.frombytes(payload[: len(payload) - len(payload) % descriptors.itemsize])
                for descriptor in descriptors:
                    os.close(descriptor)
        if not raw or len(raw) > 8192 or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
            raise ValueError("invalid control routing frame")
        line, separator, trailing = raw.partition(b"\n")
        if separator:
            if trailing:
                raise ValueError("multiple control routing frames")
            return line
        time.sleep(min(0.005, max(0.0, frame_deadline - time.monotonic())))


def _control_recovery(executor: BrokerOwnedTurnExecutor, stopping: threading.Event) -> None:
    """Trusted internal drain only; this thread has no admission/launch operation."""
    while not stopping.is_set():
        try:
            executor.scheduler.recover_controlled_turn()
            executor.cleanup_terminal_workdirs(limit=8)
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            # Durable quarantine remains intact. The next observation retries
            # the same original allocation; it never fabricates a release.
            pass
        stopping.wait(1.0)


def _retained_channel_worker(
    channel: socket.socket, provider: RetainedProviderServer, stopping: threading.Event
) -> None:
    """Readback uses separate bounded capacity on the existing broker object."""
    sequence = 1
    try:
        while not stopping.is_set():
            ready, _, _ = select.select([channel], [], [], 1.0)
            if not ready:
                continue
            provider.serve_once(channel, sequence)
            sequence += 1
    except (OSError, ValueError, RuntimeError, EOFError, sqlite3.Error):
        # A lost readback does not resolve an AOS journal or free a GPU lease.
        pass
    finally:
        channel.close()


def serve(
    *,
    retained_channel: socket.socket | None = None,
    shared_launch_authority: SharedLaunchAuthority | None = None,
) -> None:
    """Serve the canonical broker; trusted bootstrap may supply one private FD.

    No environment flag creates a provider listener. Bootstrap must pass the
    other endpoint to the separately authorized AOS service generation.
    Shared Desktop execution additionally requires an explicitly supplied
    launch authority; absent composition denies that caller's admission.
    """
    if retained_channel is not None:
        prepare_channel(retained_channel)
    runtime = _runtime_directory()
    bus = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    expected_bus = re.compile(
        rf"unix:path={re.escape(str(runtime / 'bus'))}(?:,guid=[0-9a-f]{{32}})?\Z"
    )
    if expected_bus.fullmatch(bus) is None:
        raise ValueError("broker requires the authenticated current user's systemd bus")
    directory = runtime / "swapp-gpu"
    if directory.is_symlink() or not directory.is_dir() or directory.stat().st_uid != os.getuid():
        raise ValueError("shared GPU runtime directory is not provisioned and owned")
    if stat.S_IMODE(directory.stat().st_mode) & 0o077:
        raise ValueError("shared GPU runtime directory must be private")
    socket_path = directory / "broker.sock"
    _prepare_socket_path(socket_path)
    state_dir = Path(os.environ["SWAPP_GPU_STATE_DIR"])
    expected_state_dir = Path.home() / ".local/state/swapp-gpu"
    if state_dir != expected_state_dir:
        raise ValueError("shared GPU state directory differs from the fixed user state path")
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_info = state_dir.lstat()
    if (
        not stat.S_ISDIR(state_info.st_mode)
        or state_dir.is_symlink()
        or state_info.st_uid != os.getuid()
        or stat.S_IMODE(state_info.st_mode) & 0o077
    ):
        raise ValueError("shared GPU state directory must be private and user-owned")
    database = state_dir / "arbiter.sqlite3"
    if database.exists() and database.is_symlink():
        raise ValueError("shared GPU scheduler database cannot be a symlink")
    profile_config = Path(os.environ["SWAPP_GPU_PROFILE_CONFIG"])
    aos_unit = os.environ["SWAPP_AOS_GPU_UNIT"]
    lab_unit = os.environ["SWAPP_LAB_GPU_UNIT"]
    config_snapshot = _private_json(profile_config)
    source_root_value = config_snapshot.get("source_root")
    if not isinstance(source_root_value, str):
        raise ValueError("broker profile config omits its fixed AOS source root")
    source_root = Path(source_root_value).resolve(strict=True)
    profiles = load_profiles(profile_config, source_root=source_root)
    authenticator = SystemdSocketPeerAuthenticator(aos_unit)
    control_store = ControlStore(
        database,
        clock=boottime,
        boot_id=_boot_id,
        peer_verifier=lambda peer: authenticator.still_current(PeerGeneration(**peer)),
        current_admission_verifier=lambda connection, binding: _verify_current_admission(
            connection, binding, control=control, shared_launch_authority=shared_launch_authority
        ),
    )
    policy_path = os.environ.get("SWAPP_GPU_CONTROL_POLICY")
    policy = ControlPolicy(
        None if not policy_path else Path(policy_path),
        profiles=profiles,
        source_root=PROJECT_ROOT,
    )
    server_generation = _broker_generation()
    control = LabAOSControl(
        policy=policy,
        authenticator=authenticator,
        store=control_store,
        server_generation=server_generation,
        server_current=lambda: _broker_still_current(server_generation),
        clock=boottime,
    )
    units = SystemdUnitManager()
    gpu = NvidiaSmiObserver()
    runtime_driver = SystemdAOSProfileRuntime(
        database,
        units=units,
        gpu=gpu,
        work_root=state_dir / "worker-turns",
        control_store=control_store,
    )
    executor = BrokerOwnedTurnExecutor(
        database,
        authenticator=authenticator,
        lab_units={"aos": aos_unit, "lab": lab_unit},
        profiles=profiles,
        runtime=runtime_driver,
        control_store=control_store,
    )
    service = LabAOSBroker(authenticator, executor, admission=control.admit_infer)
    provider = (
        None if retained_channel is None else RetainedProviderServer(control, units=units, gpu=gpu)
    )
    if provider is not None:
        provider.verify_configuration()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    bound_socket = socket_path.lstat()
    listener.listen(WORKERS)
    listener.settimeout(1)
    capacity = threading.BoundedSemaphore(WORKERS)
    stopping = threading.Event()
    control_socket: socket.socket | None = None
    control_path: Path | None = None
    control_bound: os.stat_result | None = None
    control_thread: threading.Thread | None = None
    channels = RetainedChannelBootstrap(control, units=units, gpu=gpu)
    retained_thread: threading.Thread | None = None
    recovery_thread = threading.Thread(
        target=_control_recovery,
        args=(executor, stopping),
        name="gpu-control-recovery",
        daemon=True,
    )

    def handle(connection: socket.socket) -> None:
        try:
            service.serve_connection(connection)
        except (OSError, ValueError, RuntimeError, TimeoutError):
            pass
        finally:
            connection.close()
            capacity.release()

    try:
        if policy.config is not None:
            if policy.config["caller_unit"] != aos_unit:
                raise ValueError("control policy and peer authenticator units differ")
            control_path = Path(policy.config["control_socket"])
            if (
                not control_path.is_absolute()
                or control_path.parent != directory
                or control_path == socket_path
                or re.fullmatch(r"[a-z0-9-]+\.sock", control_path.name) is None
            ):
                raise ValueError("control socket must be distinct in the private runtime directory")
            _prepare_socket_path(control_path)
            control_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            control_socket.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            control_socket.bind(str(control_path))
            os.chmod(control_path, 0o600)
            control_bound = control_path.lstat()
            control_socket.listen(CONTROL_BACKLOG)
            control_socket.settimeout(1.0)
            control_thread = threading.Thread(
                target=_control_listener,
                args=(control_socket, control, stopping, channels),
                name="gpu-control-listener",
                daemon=True,
            )
            control_thread.start()
        if provider is not None and retained_channel is not None:
            retained_thread = threading.Thread(
                target=_retained_channel_worker,
                args=(retained_channel, provider, stopping),
                name="gpu-retained-readback",
                daemon=True,
            )
            retained_thread.start()
        recovery_thread.start()
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=WORKERS, thread_name_prefix="gpu-turn"
        ) as pool:
            while True:
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue
                if not capacity.acquire(blocking=False):
                    connection.close()
                    continue
                pool.submit(handle, connection)
    finally:
        channels.close()
        stopping.set()
        if retained_channel is not None:
            try:
                retained_channel.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            retained_channel.close()
        if retained_thread is not None:
            retained_thread.join(timeout=4.0)
        if control_socket is not None:
            control_socket.close()
        if control_thread is not None:
            control_thread.join(timeout=CALL_SECONDS + 1.0)
        if recovery_thread.is_alive():
            recovery_thread.join(timeout=1.0)
        if control_path is not None and control_bound is not None:
            try:
                current_control = control_path.lstat()
            except FileNotFoundError:
                current_control = None
            if (
                current_control is not None
                and stat.S_ISSOCK(current_control.st_mode)
                and current_control.st_uid == os.getuid()
                and (current_control.st_dev, current_control.st_ino)
                == (control_bound.st_dev, control_bound.st_ino)
            ):
                control_path.unlink()
        listener.close()
        try:
            current = socket_path.lstat()
        except FileNotFoundError:
            current = None
        if (
            current is not None
            and stat.S_ISSOCK(current.st_mode)
            and current.st_uid == os.getuid()
            and (current.st_dev, current.st_ino) == (bound_socket.st_dev, bound_socket.st_ino)
        ):
            socket_path.unlink()


def main() -> None:
    serve()


if __name__ == "__main__":
    main()
