"""Bounded entry point for the isolated Lab/AOS shared GPU broker draft."""

from __future__ import annotations

import concurrent.futures
import errno
import json
import os
import re
import socket
import stat
import threading
from pathlib import Path

from lab.llm.aos_gpu_broker import LabAOSBroker
from lab.llm.aos_gpu_executor import (
    AOSProfile,
    BrokerOwnedTurnExecutor,
    ProfileRegistry,
    SystemdAOSProfileRuntime,
    SystemdSocketPeerAuthenticator,
    TurnBudgets,
)
from lab.llm.native_runtime import NvidiaSmiObserver, SystemdUnitManager

MAX_CONFIG_BYTES = 64 * 1024
WORKERS = 4
PROFILE_KEYS = frozenset({"aos.decider.turn.v1", "aos.bonsai.recovery.v1", "aos.bonsai.vision.v1"})


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
    value = json.loads(raw)
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
            set(item) != keys
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


def serve() -> None:
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
    units = SystemdUnitManager()
    runtime_driver = SystemdAOSProfileRuntime(
        database,
        units=units,
        gpu=NvidiaSmiObserver(),
        work_root=state_dir / "worker-turns",
    )
    executor = BrokerOwnedTurnExecutor(
        database,
        authenticator=authenticator,
        lab_units={"aos": aos_unit, "lab": lab_unit},
        profiles=profiles,
        runtime=runtime_driver,
    )
    service = LabAOSBroker(authenticator, executor)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    bound_socket = socket_path.lstat()
    listener.listen(WORKERS)
    listener.settimeout(1)
    capacity = threading.BoundedSemaphore(WORKERS)

    def handle(connection: socket.socket) -> None:
        try:
            service.serve_connection(connection)
        except (OSError, ValueError, TimeoutError):
            pass
        finally:
            connection.close()
            capacity.release()

    try:
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
