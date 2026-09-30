#!/usr/bin/env python3
"""Drive an isolated real AOS + Lab coexistence acceptance run.

Default mode is preflight only. `--execute` is an explicit root-operated gate;
it starts only the named isolated AOS/Lab run-bound units supplied here.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import re
import socket
import sqlite3
import stat
import subprocess
import sys
import time
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[3]
LIVE_AOS_ROOT = Path("/home/cachyos/aos").resolve()
MAX_RUNTIME_SECONDS = 14_400
DIRECTOR_OVERHEAD_SECONDS = 600
GIB = 1024**3
MINIMUM_HOST_RAM_RESERVE = 16 * GIB
MINIMUM_DISK_RESERVE = 20 * GIB
MAXIMUM_GPU_TEMPERATURE_C = 83
BROKER_UNIT = "swapp-lab-gpu-broker.service"
UNIT_PROPERTIES = (
    "LoadState",
    "ActiveState",
    "Result",
    "MainPID",
    "InvocationID",
    "ControlGroup",
    "MemoryCurrent",
    "MemoryMax",
    "CPUUsageNSec",
    "CPUQuotaPerSecUSec",
    "TasksCurrent",
    "TasksMax",
    "MemorySwapMax",
    "Environment",
    "ExecStart",
    "FragmentPath",
)


def _boottime() -> float:
    clock_id = getattr(time, "CLOCK_BOOTTIME", None)
    if clock_id is None:
        raise RuntimeError("CLOCK_BOOTTIME is required for broker ticket correlation")
    return time.clock_gettime(clock_id)


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()


def _process_cgroup(pid: int) -> str:
    text = Path(f"/proc/{pid}/cgroup").read_text(encoding="ascii")
    for line in text.splitlines():
        hierarchy, _, rest = line.partition(":")
        if hierarchy == "0":
            _, _, path = rest.partition(":")
            if path.startswith("/"):
                return path.rstrip("/")
    raise RuntimeError("process cgroup is unavailable")


def _process_start_ticks(pid: int) -> int:
    fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
    return int(fields[19])


def _process_argv(pid: int) -> list[str]:
    return [
        part.decode("utf-8", errors="strict")
        for part in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        if part
    ]


def _principal_identity(unit: str, owner: str) -> dict[str, object]:
    values = _show(unit)
    pid = int(values.get("MainPID", "0"))
    invocation = values.get("InvocationID", "")
    control_group = values.get("ControlGroup", "").rstrip("/")
    if (
        owner not in {"aos", "lab"}
        or values.get("LoadState") != "loaded"
        or values.get("ActiveState") != "active"
        or pid <= 0
        or re.fullmatch(r"[0-9a-f]{32}", invocation) is None
        or not control_group.endswith("/" + unit)
        or "/swapp-gpu.slice/" not in control_group
        or _process_cgroup(pid) != control_group
    ):
        raise RuntimeError(f"{owner} owner unit has no current exact systemd generation")
    return {
        "unit": unit,
        "owner": owner,
        "main_pid": pid,
        "main_start_ticks": _process_start_ticks(pid),
        "boot_id": _boot_id(),
        "invocation_id": invocation,
        "control_group": control_group,
    }


def _same_principal_generation(
    current: dict[str, object], expected: dict[str, object] | None
) -> bool:
    return expected is not None and all(
        current.get(key) == expected.get(key)
        for key in (
            "unit",
            "owner",
            "main_pid",
            "main_start_ticks",
            "boot_id",
            "invocation_id",
            "control_group",
        )
    )


def _cgroup_population(control_group: str) -> int:
    if not control_group.startswith("/") or ".." in Path(control_group).parts:
        raise RuntimeError("captured systemd cgroup path is malformed")
    events = Path("/sys/fs/cgroup") / control_group.lstrip("/") / "cgroup.events"
    try:
        content = events.read_text(encoding="ascii")
    except FileNotFoundError:
        return 0
    values = dict(line.split() for line in content.splitlines() if line.strip())
    populated = values.get("populated")
    if populated not in {"0", "1"}:
        raise RuntimeError("systemd cgroup population state is unavailable")
    return int(populated)


def _process_environment(pid: int, names: set[str]) -> dict[str, str]:
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    selected: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        key, separator, value = entry.partition(b"=")
        if separator and key.decode("ascii", errors="ignore") in names:
            selected[key.decode("ascii")] = value.decode("utf-8", errors="strict")
    return selected


def _shared_database_preflight(args: argparse.Namespace) -> dict[str, object]:
    """Attest the one broker SQLite path without creating or mutating it."""
    expected_database = Path.home() / ".local/state/swapp-gpu/arbiter.sqlite3"
    blockers: list[str] = []
    if args.gpu_runtime_db != expected_database:
        blockers.append("gpu_runtime_db_not_the_fixed_shared_broker_database")
    try:
        _no_symlink_components(expected_database)
        home = Path.home()
        directories = tuple(
            current
            for current in _absolute_directory_chain(expected_database.parent)
        )
        for directory in directories:
            info = directory.lstat()
            sticky_root_directory = bool(info.st_mode & 0o1000) and info.st_uid == 0
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISDIR(info.st_mode)
                or info.st_uid not in {0, os.getuid()}
                or (info.st_mode & 0o022 and not sticky_root_directory)
            ):
                blockers.append("shared_gpu_database_directory_component_unsafe")
                break
            if sticky_root_directory:
                continue
            if (directory == home or home in directory.parents) and info.st_uid != os.getuid():
                blockers.append("shared_gpu_home_component_owner_mismatch")
                break
            if directory in {
                home / ".local/state",
                home / ".local/state/swapp-gpu",
            } and info.st_mode & 0o077:
                blockers.append("shared_gpu_database_state_directory_not_private")
                break
        database_info = expected_database.lstat()
        if (
            stat.S_ISLNK(database_info.st_mode)
            or not stat.S_ISREG(database_info.st_mode)
            or database_info.st_uid != os.getuid()
            or database_info.st_mode & 0o077
            or database_info.st_nlink != 1
        ):
            blockers.append("shared_gpu_database_file_identity_unsafe")
        else:
            uri = expected_database.as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=1) as db:
                table_names = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                required_tables = {
                    "gpu_turn_requests",
                    "gpu_turn_state",
                    "aos_gpu_child_bindings",
                }
                if not required_tables <= table_names:
                    blockers.append("shared_gpu_database_broker_schema_incomplete")
                elif db.execute(
                    "SELECT active_owner FROM gpu_turn_state WHERE singleton=1"
                ).fetchone() != (None,):
                    blockers.append("shared_gpu_database_has_active_gpu_turn")
    except FileNotFoundError:
        blockers.append("shared_gpu_database_or_state_directory_missing")
    except (OSError, sqlite3.Error, ValueError):
        blockers.append("shared_gpu_database_readonly_attestation_failed")

    broker = _show(BROKER_UNIT)
    broker_identity: dict[str, object] | None = None
    if broker.get("LoadState") != "loaded" or broker.get("ActiveState") != "active":
        blockers.append("fixed_gpu_broker_not_active")
    else:
        try:
            pid = int(broker.get("MainPID", "0"))
            invocation = broker.get("InvocationID", "")
            control_group = broker.get("ControlGroup", "").rstrip("/")
            if (
                pid <= 0
                or re.fullmatch(r"[0-9a-f]{32}", invocation) is None
                or not control_group.endswith("/" + BROKER_UNIT)
                or _process_cgroup(pid) != control_group
            ):
                raise RuntimeError("fixed broker process identity differs")
            broker_identity = {
                "unit": BROKER_UNIT,
                "main_pid": pid,
                "main_start_ticks": _process_start_ticks(pid),
                "boot_id": _boot_id(),
                "invocation_id": invocation,
                "control_group": control_group,
            }
            command_line = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
            source_bound = (
                Path(f"/proc/{pid}/cwd").resolve(strict=True) == PROJECT_ROOT.resolve(strict=True)
                and b"lab-aos-gpu-broker" in command_line
            )
            if not source_bound:
                blockers.append("fixed_gpu_broker_source_root_mismatch")
            try:
                broker_limits_ok = (
                    0 < int(broker.get("MemoryMax", "0")) <= 512 * 1024 * 1024
                    and int(broker.get("MemorySwapMax", "-1")) == 0
                    and 0 < int(broker.get("TasksMax", "0")) <= 32
                    and 0 < int(broker.get("CPUQuotaPerSecUSec", "0")) <= 1_000_000
                )
            except ValueError:
                broker_limits_ok = False
            if not broker_limits_ok:
                blockers.append("fixed_gpu_broker_resource_limits_unverified")
            allowed = {
                "SWAPP_GPU_STATE_DIR",
                "SWAPP_GPU_PROFILE_CONFIG",
                "SWAPP_AOS_GPU_UNIT",
                "SWAPP_LAB_GPU_UNIT",
            }
            environment = _process_environment(pid, allowed)
            expected_state = str(Path.home() / ".local/state/swapp-gpu")
            if (
                environment.get("SWAPP_GPU_STATE_DIR") != expected_state
                or environment.get("SWAPP_AOS_GPU_UNIT") != args.aos_unit
                or environment.get("SWAPP_LAB_GPU_UNIT") != args.lab_maintenance_unit
            ):
                blockers.append("fixed_gpu_broker_environment_mapping_mismatch")
            profile_path = Path(environment.get("SWAPP_GPU_PROFILE_CONFIG", "/invalid"))
            try:
                _private_file(profile_path, "broker profile configuration")
                profile_hash = _sha(profile_path)
            except (OSError, ValueError):
                profile_hash = None
                blockers.append("fixed_gpu_broker_profile_configuration_unverified")
        except (OSError, ValueError, RuntimeError, UnicodeError):
            profile_hash = None
            blockers.append("fixed_gpu_broker_process_identity_unverified")

    socket_path = Path("/run/user") / str(os.getuid()) / "swapp-gpu/broker.sock"
    try:
        _no_symlink_components(socket_path)
        socket_info = socket_path.lstat()
        socket_ready = (
            stat.S_ISSOCK(socket_info.st_mode)
            and socket_info.st_uid == os.getuid()
            and stat.S_IMODE(socket_info.st_mode) == 0o600
        )
    except OSError:
        socket_ready = False
    if not socket_ready:
        blockers.append("fixed_gpu_broker_socket_identity_unverified")

    return {
        "database_path": str(expected_database),
        "database_exists": expected_database.is_file() and not expected_database.is_symlink(),
        "broker_unit": BROKER_UNIT,
        "broker_active": broker.get("ActiveState") == "active",
        "broker_generation": broker_identity,
        "broker_source_bound": "fixed_gpu_broker_source_root_mismatch" not in blockers,
        "broker_limits_bounded": "fixed_gpu_broker_resource_limits_unverified" not in blockers,
        "broker_profile_configuration_sha256": locals().get("profile_hash"),
        "socket_path": str(socket_path),
        "socket_ready": socket_ready,
        "expected_aos_unit_matches": bool(broker_identity)
        and not any("environment_mapping_mismatch" in item for item in blockers),
        "lab_maintenance_unit": args.lab_maintenance_unit,
        "blockers": sorted(set(blockers)),
        "ready": not blockers,
    }


def _lab_shared_database_support(args: argparse.Namespace) -> dict[str, object]:
    source = args.lab_root / "lab/cli.py"
    try:
        _no_symlink_components(source)
        raw = source.read_bytes()
        tree = ast.parse(raw)
    except (OSError, SyntaxError, ValueError):
        return {"ready": False, "reason": "lab_cli_source_unavailable_or_invalid"}
    function = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_validated_gpu_runtime_database"
        ),
        None,
    )
    if function is None:
        return {
            "ready": False,
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "reason": "lab_cli_lacks_fixed_shared_database_allowance",
        }
    literals = {
        node.value
        for node in ast.walk(function)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    required_literals = {".local/state/swapp-gpu/arbiter.sqlite3"}
    return {
        "ready": required_literals <= literals,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "reason": None
        if required_literals <= literals
        else "lab_cli_fixed_database_path_contract_differs",
    }


def _postgres_target_fingerprint(args: argparse.Namespace) -> dict[str, object]:
    scorer_dsn = args.lab_root / "data/runtime/postgres/scorer.dsn"
    expected_paths = {
        "director": args.lab_root / "data/runtime/postgres/director.dsn",
        "planner": args.lab_root / "data/runtime/postgres/planner.dsn",
        "scorer": scorer_dsn,
    }
    if args.director_dsn_file != expected_paths["director"] or (
        args.planner_dsn_file != expected_paths["planner"]
    ):
        return {"same_private_target": False, "reason": "dsn_files_not_from_this_lab_clone"}
    try:
        for name, path in expected_paths.items():
            _private_file(path, f"{name} DSN")
    except (OSError, ValueError):
        return {"same_private_target": False, "reason": "private_dsn_file_unavailable"}
    code = r"""import hashlib,json,sys
from pathlib import Path
from sqlalchemy.engine import make_url
targets=[]
for value in sys.argv[1:]:
 url=make_url(Path(value).read_text(encoding='utf-8').strip())
 targets.append((url.drivername,url.host,url.port,url.database))
same=len(set(targets))==1
target_hash=(hashlib.sha256(json.dumps(targets[0],separators=(',',':')).encode()).hexdigest()
             if same else None)
print(json.dumps({'same':same,'target_sha256':target_hash}))"""
    result = subprocess.run(
        [str(args.lab_python), "-c", code, *(str(path) for path in expected_paths.values())],
        cwd=args.lab_root,
        env={**os.environ, "PYTHONPATH": str(args.lab_root)},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if result.returncode != 0:
        return {"same_private_target": False, "reason": "dsn_target_parse_failed"}
    try:
        value = json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"same_private_target": False, "reason": "dsn_target_receipt_missing"}
    return {
        "same_private_target": value.get("same") is True,
        "target_sha256": value.get("target_sha256"),
        "reason": None if value.get("same") is True else "dsn_targets_differ",
    }


def _absolute_directory_chain(path: Path) -> tuple[Path, ...]:
    if not path.is_absolute():
        raise ValueError("runtime directory path must be absolute")
    current = Path(path.anchor)
    result = [current]
    for part in path.parts[1:]:
        current /= part
        result.append(current)
    return tuple(result)


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _private_file(path: Path, name: str) -> Path:
    _no_symlink_components(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{name} must be an existing non-symlink file")
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f"{name} must be private to the current owner")
    return path.resolve(strict=True)


def _pinned_lab_interpreter(lab_root: Path, candidate: Path) -> None:
    """Allow only this clone's deliberate read-only venv symlink."""
    expected = lab_root / ".venv/bin/python"
    if candidate != expected:
        raise ValueError("Lab interpreter path must be this clone's .venv/bin/python")
    env_link = lab_root / ".venv"
    if not env_link.is_symlink() or env_link.resolve(strict=True) != (
        PROJECT_ROOT / ".venv"
    ).resolve(strict=True):
        raise ValueError("Lab clone venv is not the expected shared read-only environment")
    target = expected.resolve(strict=True)
    _no_symlink_components(target)
    if not target.is_file() or not os.access(target, os.X_OK):
        raise ValueError("Lab clone interpreter target is not executable")


def _inside(path: Path, parent: Path) -> bool:
    try:
        _no_symlink_components(path)
        _no_symlink_components(parent)
        path.resolve(strict=True).relative_to(parent.resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def _no_symlink_components(path: Path) -> None:
    """Check the caller's lexical path and each existing ancestor before resolve."""
    if ".." in path.parts:
        raise ValueError("path traversal components are not accepted")
    absolute = path if path.is_absolute() else Path.cwd() / path
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("symlink path components are not accepted")


def _show(unit: str) -> dict[str, str]:
    if re.fullmatch(r"[a-zA-Z0-9_.@:-]{1,255}", unit) is None:
        raise ValueError("invalid systemd unit identity")
    completed = subprocess.run(
        [
            "/usr/bin/systemctl",
            "--user",
            "show",
            unit,
            *[f"--property={name}" for name in UNIT_PROPERTIES],
            "--no-pager",
        ],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
        env={**os.environ, "PATH": "/usr/bin:/bin"},
    )
    if completed.returncode != 0:
        return {"LoadState": "not-found"}
    result = dict(line.split("=", 1) for line in completed.stdout.splitlines() if "=" in line)
    return {key: result.get(key, "") for key in UNIT_PROPERTIES}


def _host_sample() -> dict[str, object]:
    observer = PROJECT_ROOT / "docs/ai-scientist/review-evidence/review_gpu_host.py"
    spec = importlib.util.spec_from_file_location("swapp_gpu_host_observer", observer)
    if spec is None or spec.loader is None:
        raise RuntimeError("reviewed GPU host observer could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sample = module.snapshot()
    if not isinstance(sample, dict):
        raise RuntimeError("reviewed GPU host observer returned no snapshot")
    return sample


def _assert_host_capacity(
    sample: dict[str, object], *, owned_cgroups: set[str]
) -> list[dict[str, object]]:
    memory_available = sample.get("memory_available_bytes")
    disk_available = sample.get("disk_available_bytes")
    gpu = sample.get("gpu")
    if not isinstance(memory_available, int) or memory_available < MINIMUM_HOST_RAM_RESERVE:
        raise RuntimeError("host memory reserve fell below the reviewed 16 GiB floor")
    if not isinstance(disk_available, int) or disk_available < MINIMUM_DISK_RESERVE:
        raise RuntimeError("host free disk fell below the reviewed 20 GiB floor")
    if not isinstance(gpu, dict) or not isinstance(gpu.get("temperature_c"), (int, float)):
        raise RuntimeError("GPU temperature could not be verified")
    if gpu["temperature_c"] >= MAXIMUM_GPU_TEMPERATURE_C:
        raise RuntimeError("GPU temperature reached the 83 C stop threshold")
    consumers = sample.get("gpu_consumers")
    if not isinstance(consumers, list):
        raise RuntimeError("GPU consumer identities could not be enumerated")
    foreign: list[dict[str, object]] = []
    for consumer in consumers:
        if not isinstance(consumer, dict):
            raise RuntimeError("GPU consumer identity is malformed")
        if consumer.get("existing_display_exemption") is True:
            continue
        raw_cgroup = consumer.get("cgroup")
        cgroup = None
        if isinstance(raw_cgroup, str):
            for line in raw_cgroup.splitlines():
                hierarchy, separator, path = line.partition("::")
                if separator and hierarchy == "0" and path.startswith("/"):
                    cgroup = path.rstrip("/")
                    break
        if cgroup not in owned_cgroups:
            foreign.append(consumer)
    if foreign:
        raise RuntimeError("uncoordinated GPU consumer appeared; preserve foreign process")
    return consumers


def _gpu_ledger(
    path: Path,
    since: float,
    expected_principals: dict[str, dict[str, object]],
) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        return {"available": False, "reason": "runtime_db_not_created"}
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=1.0) as db:
            db.row_factory = sqlite3.Row
            names = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            required = {
                "gpu_turn_requests",
                "gpu_turn_state",
                "gpu_runtime_bindings",
                "aos_gpu_child_bindings",
            }
            if not required <= names:
                return {"available": False, "reason": "expected_broker_ledger_absent"}
            rows = db.execute(
                "SELECT owner,request_id,sequence,state,submitted_at,owner_unit,"
                "owner_invocation_id FROM gpu_turn_requests "
                "WHERE submitted_at>=? ORDER BY sequence",
                (since,),
            ).fetchall()
            matched: list[dict[str, object]] = []
            unexpected: list[dict[str, object]] = []
            binding_errors: list[dict[str, str]] = []
            owned_cgroups: set[str] = set()
            request_ids: dict[str, set[str]] = {"aos": set(), "lab": set()}
            for row in rows:
                item = {
                    "owner": row["owner"],
                    "request_id": row["request_id"],
                    "sequence": row["sequence"],
                    "state": row["state"],
                    "submitted_at": row["submitted_at"],
                    "unit": row["owner_unit"],
                    "invocation_id": row["owner_invocation_id"],
                }
                expected = expected_principals.get(row["owner"])
                if expected is None or row["owner_unit"] != expected["unit"] or (
                    row["owner_invocation_id"] != expected["invocation_id"]
                ):
                    unexpected.append(item)
                    continue
                matched.append(item)
                request_ids[row["owner"]].add(row["request_id"])
            bindings: list[dict[str, object]] = []
            for owner, ids in request_ids.items():
                for request_id in ids:
                    if owner == "aos":
                        binding = db.execute(
                            "SELECT owner,request_id,fencing_token,unit,invocation_id,main_pid,"
                            "main_start_ticks,boot_id,control_group,launch_state "
                            "FROM aos_gpu_child_bindings WHERE owner='aos' AND request_id=? "
                        "ORDER BY fencing_token DESC LIMIT 1",
                        (request_id,),
                        ).fetchone()
                        binding_kind = "aos_child"
                    else:
                        binding = db.execute(
                            "SELECT owner,request_id,fencing_token,unit,invocation_id,main_pid,"
                            "main_start_ticks,boot_id,control_group,launch_state "
                            "FROM gpu_runtime_bindings WHERE owner='lab' AND request_id=? "
                            "ORDER BY fencing_token DESC LIMIT 1", (request_id,)
                        ).fetchone()
                        binding_kind = "lab_runtime"
                    if binding is not None:
                        item = {"kind": binding_kind, **dict(binding)}
                        bindings.append(item)
                        unit = str(item.get("unit", ""))
                        control_group = str(item.get("control_group", ""))
                        expected_prefix = (
                            "swapp-aos-gpu-turn-"
                            if owner == "aos"
                            else "swapp-lab-gpu-turn-"
                        )
                        valid_binding = (
                            re.fullmatch(
                                re.escape(expected_prefix) + r"[0-9a-f]{32}\.service", unit
                            ) is not None
                            and re.fullmatch(
                                r"[0-9a-f]{32}", str(item.get("invocation_id", ""))
                            ) is not None
                            and isinstance(item.get("main_pid"), int)
                            and int(item["main_pid"]) > 0
                            and isinstance(item.get("main_start_ticks"), int)
                            and int(item["main_start_ticks"]) > 0
                            and item.get("boot_id") == _boot_id()
                            and control_group.endswith("/" + unit)
                            and "/swapp-gpu.slice/" in control_group
                        )
                        if valid_binding:
                            owned_cgroups.add(control_group)
                        else:
                            binding_errors.append({"owner": owner, "request_id": request_id})
                    elif any(
                        turn.get("owner") == owner
                        and turn.get("request_id") == request_id
                        and turn.get("state") == "done"
                        for turn in matched
                    ):
                        binding_errors.append({"owner": owner, "request_id": request_id})
            state = db.execute(
                "SELECT active_owner,phase,next_owner FROM gpu_turn_state WHERE singleton=1"
            ).fetchone()
            return {
                "available": True,
                "turns": matched,
                "unexpected_turns": unexpected,
                "bindings": bindings,
                "binding_errors": binding_errors,
                "owned_cgroups": sorted(owned_cgroups),
                "active_owner": state["active_owner"] if state else None,
                "phase": state["phase"] if state else None,
                "next_owner": state["next_owner"] if state else None,
            }
    except sqlite3.Error as exc:
        return {"available": False, "reason": type(exc).__name__}


def _suite_metadata(args: argparse.Namespace) -> dict[str, object]:
    code = r"""import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from lab.api.registry import load_suite_registry
registry=load_suite_registry(Path(sys.argv[2]),Path(sys.argv[3]))
entry=registry.get(sys.argv[4]); manifest,_=registry.verify_entry(entry)
print(json.dumps({"suite_id":entry.suite_id,"provider":entry.provider,"track":entry.track,
"program_version":entry.program_version,"proposal_limit":entry.proposal_limit,
"suite_manifest_sha256":entry.suite_manifest_sha256,
"registry_entry_sha256":registry.entry_sha256(entry),
"manifest_path":str(manifest)}))"""
    env = {**os.environ, "PYTHONPATH": str(args.lab_root)}
    result = subprocess.run(
        [
            str(args.lab_python),
            "-c",
            code,
            str(args.lab_root),
            str(args.suite_registry),
            str(args.suite_runtime_root),
            args.suite,
        ],
        cwd=args.lab_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("trusted Lab suite registry verification failed")
    try:
        metadata = json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise ValueError("trusted Lab suite registry returned no metadata") from exc
    if metadata.get("provider") != "local-qwen":
        raise ValueError("acceptance requires a registered local-qwen suite")
    if metadata.get("track") != "anomaly" or metadata.get("program_version") != "director.v1":
        raise ValueError("registered suite contract differs from the AOS Lab adapter")
    if args.experiments > metadata["proposal_limit"]:
        raise ValueError("requested experiment budget exceeds trusted suite registration")
    return metadata


class Console:
    def __init__(self, origin: str, token: str, timeout: float = 30.0) -> None:
        self.origin = origin.rstrip("/")
        self.timeout = timeout
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self._request("POST", "/api/login", {"token": token}, authenticated=False)

    def _request(
        self, method: str, path: str, value: object | None = None, *, authenticated: bool = True
    ) -> tuple[dict[str, Any], float]:
        body = None if value is None else json.dumps(value, separators=(",", ":")).encode()
        headers = {"Origin": self.origin, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.origin + path, data=body, headers=headers, method=method)
        started = time.monotonic()
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                result = json.loads(response.read(2 * 1024 * 1024))
                if not isinstance(result, dict):
                    raise ValueError("AOS API returned a non-object response")
                return result, round((time.monotonic() - started) * 1000, 3)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            status = getattr(exc, "code", None)
            raise RuntimeError(
                f"AOS API {method} {path} failed ({status or type(exc).__name__})"
            ) from None

    def get(self, path: str) -> tuple[dict[str, Any], float]:
        return self._request("GET", path)

    def post(self, path: str, value: object) -> tuple[dict[str, Any], float]:
        return self._request("POST", path, value)


def _unused_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _fresh_console_token(source: Path, before: set[Path], deadline: float) -> tuple[Path, str]:
    runs = source / "runs"
    while time.monotonic() < deadline:
        candidates = {path for path in runs.glob("desktop-console-*.token") if path not in before}
        if len(candidates) == 1:
            token_path = candidates.pop()
            _private_file(token_path, "AOS console token")
            return token_path, token_path.read_text(encoding="utf-8").strip()
        if len(candidates) > 1:
            raise RuntimeError("ambiguous fresh AOS console token files")
        time.sleep(0.2)
    raise TimeoutError("isolated AOS console did not create its private API token")


def _stop_owned_unit(
    unit: str,
    owner: str,
    expected: dict[str, object] | None,
    *,
    timeout: float,
) -> dict[str, object]:
    if expected is None:
        return {"unit": unit, "owner": owner, "result": "no_owned_generation_recorded"}
    values = _show(unit)
    if values.get("ActiveState") != "active":
        try:
            populated = _cgroup_population(str(expected["control_group"]))
        except (OSError, RuntimeError, KeyError):
            populated = 1
        return {
            "unit": unit,
            "owner": owner,
            "invocation_id": expected.get("invocation_id"),
            "result": "drained" if populated == 0 else "inactive_but_cgroup_not_proven_empty",
        }
    try:
        current = _principal_identity(unit, owner)
    except (OSError, RuntimeError, ValueError):
        return {"unit": unit, "owner": owner, "result": "refused_unverified_current_generation"}
    if not _same_principal_generation(current, expected):
        return {
            "unit": unit,
            "owner": owner,
            "expected_invocation_id": expected.get("invocation_id"),
            "current_invocation_id": current.get("invocation_id"),
            "result": "refused_generation_mismatch",
        }
    command = subprocess.run(
        ["/usr/bin/systemctl", "--user", "stop", unit],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        env={**os.environ, "PATH": "/usr/bin:/bin"},
    )
    if command.returncode != 0:
        return {"unit": unit, "owner": owner, "result": "systemctl_stop_failed"}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current_state = _show(unit)
        if current_state.get("ActiveState") != "active":
            try:
                populated = _cgroup_population(str(expected["control_group"]))
            except (OSError, RuntimeError, KeyError):
                populated = 1
            if populated == 0:
                return {
                    "unit": unit,
                    "owner": owner,
                    "invocation_id": expected.get("invocation_id"),
                    "result": "drained",
                }
        else:
            try:
                current = _principal_identity(unit, owner)
            except (OSError, RuntimeError, ValueError):
                return {
                    "unit": unit,
                    "owner": owner,
                    "result": "refused_after_stop_generation_changed",
                }
            if not _same_principal_generation(current, expected):
                return {
                    "unit": unit,
                    "owner": owner,
                    "result": "new_generation_present_after_stop",
                }
        time.sleep(0.2)
    return {"unit": unit, "owner": owner, "result": "drain_timeout"}


class CoexistenceRun:
    def __init__(self, args: argparse.Namespace, output: Path, suite: dict[str, object]) -> None:
        self.args, self.output, self.suite = args, output, suite
        self.run_key = uuid4().hex
        self.port = _unused_port()
        self.unit = args.aos_unit
        self.base = output / self.run_key
        self.base.mkdir(mode=0o700, parents=True)
        self.workspace = self.base / "desktop-workspace"
        self.workspace.mkdir(mode=0o700)
        self.database = self.base / "desktop-console.sqlite"
        self.token_before = set((args.aos_source / "runs").glob("desktop-console-*.token"))
        self.console: Console | None = None
        self.token_path: Path | None = None
        self.dispatch_unit: str | None = None
        self.dispatch_wrapper: subprocess.Popen | None = None
        self.aos_principal: dict[str, object] | None = None
        self.dispatch_principal: dict[str, object] | None = None
        self.samples: list[dict[str, object]] = []
        self.api_ms: dict[str, list[float]] = {}
        self.phase_receipts: list[dict[str, object]] = []
        self.outcome = "running"
        self.failure_type: str | None = None
        self.cleanup_receipts: list[dict[str, object]] = []
        self.overlap_ticket_window: list[float] | None = None

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _api_get(self, key: str, path: str) -> dict[str, Any]:
        assert self.console is not None
        value, latency = self.console.get(path)
        self.api_ms.setdefault(key, []).append(latency)
        return value

    def _api_post(self, key: str, path: str, value: object) -> dict[str, Any]:
        assert self.console is not None
        result, latency = self.console.post(path, value)
        self.api_ms.setdefault(key, []).append(latency)
        return result

    def _launch_aos(self) -> None:
        existing = _show(self.unit)
        if existing.get("LoadState") != "not-found":
            raise RuntimeError(
                "configured AOS unit already exists; refusing to attach or replace it"
            )
        run_command = [
            "/usr/bin/systemd-run",
            "--user",
            "--collect",
            "--quiet",
            f"--unit={self.unit}",
            "--slice=swapp-gpu.slice",
            "-p",
            "MemoryMax=2G",
            "-p",
            "MemorySwapMax=0",
            "-p",
            "CPUQuota=100%",
            "-p",
            "TasksMax=128",
            "-p",
            f"RuntimeMaxSec={MAX_RUNTIME_SECONDS}s",
            f"--working-directory={self.args.aos_source}",
            f"--setenv=PYTHONPATH={self.args.aos_source / 'src'}",
            f"--setenv=SWAPP_AOS_GPU_UNIT={self.unit}",
            "--setenv=OPENBLAS_NUM_THREADS=1",
            "--setenv=OMP_NUM_THREADS=1",
            "--setenv=MKL_NUM_THREADS=1",
            str(self.args.aos_python),
            str(self.args.aos_source / "scripts/serve_desktop.py"),
            "--port",
            str(self.port),
            "--workspace",
            str(self.workspace),
            "--database",
            str(self.database),
            "--trajectory-database",
            str(self.database),
            "--desktop-manifest",
            str(self.args.desktop_manifest),
            "--engine",
            "decider",
            "--reuse-decider",
            "--shared-gpu-turns",
            "--decider-manifest",
            str(self.args.decider_manifest),
            "--model-python",
            str(self.args.model_python),
            "--lab-external-api-url",
            self.args.lab_api_url,
            "--lab-external-token-file",
            str(self.args.lab_token_file),
            "--lab-external-suite",
            self.args.suite,
        ]
        result = subprocess.run(
            run_command, capture_output=True, text=True, timeout=15, check=False
        )
        if result.returncode != 0:
            raise RuntimeError("isolated AOS systemd unit could not be launched")
        token_path, token = _fresh_console_token(
            self.args.aos_source, self.token_before, time.monotonic() + self.args.startup_seconds
        )
        self.token_path = token_path
        self.console = Console(self.origin, token)
        deadline = time.monotonic() + self.args.startup_seconds
        while time.monotonic() < deadline:
            state = self._api_get("state", "/api/state")
            if state.get("runtime", {}).get("status") == "running":
                principal = _principal_identity(self.unit, "aos")
                argv = _process_argv(int(principal["main_pid"]))
                process_environment = _process_environment(
                    int(principal["main_pid"]), {"SWAPP_AOS_GPU_UNIT"}
                )
                if (
                    Path(f"/proc/{principal['main_pid']}/cwd").resolve(strict=True)
                    != self.args.aos_source
                    or str(self.args.aos_source / "scripts/serve_desktop.py") not in argv
                    or "--shared-gpu-turns" not in argv
                    or process_environment.get("SWAPP_AOS_GPU_UNIT") != self.unit
                ):
                    raise RuntimeError("active AOS owner process differs from this isolated launch")
                self.aos_principal = principal
                self.phase_receipts.append(
                    {
                        "phase": "aos_bootstrap",
                        "result": "running",
                        "principal": self.aos_principal,
                        "session_id": state["control"]["session_id"],
                        "runtime_id": state["control"]["runtime_id"],
                    }
                )
                return
            time.sleep(0.25)
        raise TimeoutError("isolated desktop runtime did not become ready")

    def _stop_aos(self) -> None:
        current_matches = False
        if self.aos_principal is not None:
            try:
                current_matches = _same_principal_generation(
                    _principal_identity(self.unit, "aos"), self.aos_principal
                )
            except (OSError, RuntimeError, ValueError):
                current_matches = False
        if current_matches and self.console is not None:
            try:
                self._api_post("control_stop", "/api/control", {"command": "stop"})
            except RuntimeError:
                self.cleanup_receipts.append(
                    {"owner": "aos", "action": "api_stop", "result": "api_unavailable"}
                )
        receipt = _stop_owned_unit(self.unit, "aos", self.aos_principal, timeout=45)
        self.cleanup_receipts.append(receipt)
        if receipt.get("result") == "drained":
            self.aos_principal = None
        else:
            self.outcome = "failed"
            self.failure_type = "aos_owned_service_drain_failed"
        self.console = None
        self.token_path = None

    def _take_human(self) -> dict[str, Any]:
        return self._api_post("control_take_human", "/api/control", {"command": "take-control"})

    def _start_lab(self) -> dict[str, Any]:
        state = self._api_get("state_for_lab_start", "/api/state")["control"]
        if state.get("owner") != "HUMAN":
            state = self._take_human()
        request = {
            "lease_id": state["lease_id"],
            "generation": state["generation"],
            "idempotency_key": "aos-coexistence-" + uuid4().hex,
            "suite": self.args.suite,
            "experiments": self.args.experiments,
            "wall_seconds": self.args.wall_seconds,
            "model_tokens": self.args.model_tokens,
        }
        started = self._api_post("lab_start", "/api/lab/start", request)
        if not started.get("lab_run_id") or not started.get("aos_action_id"):
            raise RuntimeError("AOS typed Lab start returned no durable run/action identity")
        return started

    def _launch_dispatch(self, run_id: str) -> str:
        normalized = run_id.replace("-", "")
        if re.fullmatch(r"[0-9a-f]{32}", normalized) is None:
            raise ValueError("AOS returned a malformed Lab run UUID")
        unit = f"swapp-ai-scientist-director-dispatch-{normalized}.service"
        if _show(unit).get("LoadState") != "not-found":
            raise RuntimeError("run-bound Lab dispatcher unit already exists")
        self.dispatch_unit = unit
        environment = {
            "LAB_DIRECTOR_DSN_FILE": str(self.args.director_dsn_file),
            "LAB_PLANNER_DSN_FILE": str(self.args.planner_dsn_file),
            "LAB_SUITE_REGISTRY_FILE": str(self.args.suite_registry),
            "SWAPP_AOS_GPU_UNIT": self.args.aos_unit,
            "SWAPP_LAB_GPU_UNIT": unit,
            "SWAPP_GPU_RUNTIME_DB": str(self.args.gpu_runtime_db),
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
        }
        command = [
            "/usr/bin/systemd-run",
            "--user",
            "--collect",
            "--wait",
            "--pipe",
            "--slice=swapp-gpu.slice",
            f"--unit={unit}",
            "-p",
            "MemoryMax=2G",
            "-p",
            "MemorySwapMax=0",
            "-p",
            "CPUQuota=100%",
            "-p",
            "TasksMax=128",
            "-p",
            f"RuntimeMaxSec={self.args.wall_seconds + DIRECTOR_OVERHEAD_SECONDS}s",
            f"--working-directory={self.args.lab_root}",
        ]
        command.extend(f"--setenv={name}={value}" for name, value in environment.items())
        command.extend(
            [
                str(self.args.lab_python),
                "-m",
                "lab.cli",
                "director",
                "dispatch-one",
                "--run-id",
                run_id,
            ]
        )
        log_path = self.base / f"{normalized}-director-dispatch.log"
        log = log_path.open("xb")
        os.chmod(log_path, 0o600)
        self.dispatch_wrapper = subprocess.Popen(
            command, cwd=self.args.lab_root, stdout=log, stderr=subprocess.STDOUT, close_fds=True
        )
        log.close()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            current = _show(unit)
            if current.get("ActiveState") == "active":
                principal = _principal_identity(unit, "lab")
                pid = int(principal["main_pid"])
                argv = _process_argv(pid)
                process_environment = _process_environment(
                    pid,
                    {
                        "SWAPP_AOS_GPU_UNIT",
                        "SWAPP_LAB_GPU_UNIT",
                        "SWAPP_GPU_RUNTIME_DB",
                    },
                )
                if (
                    Path(f"/proc/{pid}/cwd").resolve(strict=True) != self.args.lab_root
                    or "lab.cli" not in argv
                    or "dispatch-one" not in argv
                    or run_id not in argv
                    or process_environment.get("SWAPP_AOS_GPU_UNIT") != self.args.aos_unit
                    or process_environment.get("SWAPP_LAB_GPU_UNIT") != unit
                    or process_environment.get("SWAPP_GPU_RUNTIME_DB")
                    != str(self.args.gpu_runtime_db)
                ):
                    raise RuntimeError(
                        "active Lab dispatcher differs from its immutable run binding"
                    )
                self.dispatch_principal = principal
                self.phase_receipts.append(
                    {
                        "phase": "lab_dispatch",
                        "unit": unit,
                        "principal": self.dispatch_principal,
                    }
                )
                return unit
            if self.dispatch_wrapper.poll() is not None:
                raise RuntimeError("run-bound Lab dispatcher exited before becoming active")
            time.sleep(0.1)
        raise TimeoutError("run-bound Lab dispatcher did not become active")

    def _poll_status(self, job_id: str) -> dict[str, Any]:
        return self._api_get("lab_status", f"/api/lab/jobs/{job_id}")

    def _poll_foreground(self, foreground_id: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        tasks = self._api_get("foreground_status", "/api/tasks")
        jobs = tasks.get("jobs", [])
        match = next((job for job in jobs if job.get("job_id") == foreground_id), None)
        if match is not None:
            return match, tasks
        return None, tasks

    def _sample_once(self, foreground_id: str, lab_job_id: str, since: float) -> tuple[bool, bool]:
        state = self._poll_status(lab_job_id)
        foreground, tasks = self._poll_foreground(foreground_id)
        unit_samples = {self.unit: _show(self.unit)}
        if self.dispatch_unit:
            unit_samples[self.dispatch_unit] = _show(self.dispatch_unit)
        expected = {
            owner: principal
            for owner, principal in (
                ("aos", self.aos_principal),
                ("lab", self.dispatch_principal),
            )
            if principal is not None
        }
        ledger = _gpu_ledger(self.args.gpu_runtime_db, since, expected)
        if not ledger.get("available"):
            raise RuntimeError("shared GPU ledger is unavailable for this overlap sample")
        if ledger.get("unexpected_turns") or ledger.get("binding_errors"):
            raise RuntimeError("GPU ledger contains an untrusted owner or missing child binding")
        owned_cgroups = set(ledger.get("owned_cgroups", []))
        host = _host_sample()
        _assert_host_capacity(host, owned_cgroups=owned_cgroups)
        sample = {
            "boottime": _boottime(),
            "host": host,
            "units": unit_samples,
            "lab_state": state.get("lab_state"),
            "lab_job_state": state.get("state"),
            "foreground": None
            if foreground is None
            else {
                "status": foreground.get("status"),
                "real_model": foreground.get("real_model"),
                "runtime_id": foreground.get("runtime_id"),
            },
            "foreground_slot_busy": tasks.get("busy"),
            "lab_job_in_foreground_scheduler": any(
                job.get("job_id") == lab_job_id
                for job in tasks.get("jobs", [])
                if isinstance(job, dict)
            ),
            "gpu_turn_ledger": ledger,
        }
        self.samples.append(sample)
        both_working = bool(
            foreground
            and foreground.get("status") == "running"
            and state.get("lab_state") == "running"
        )
        both_active = bool(
            unit_samples.get(self.unit, {}).get("ActiveState") == "active"
            and self.dispatch_unit
            and unit_samples.get(self.dispatch_unit, {}).get("ActiveState") == "active"
        )
        if both_working and both_active:
            sample_time = float(sample["boottime"])
            if self.overlap_ticket_window is None:
                self.overlap_ticket_window = [sample_time, sample_time]
            else:
                self.overlap_ticket_window[1] = sample_time
        return both_working, both_active

    def _wait_coexistence(self, started: dict[str, Any], since: float) -> dict[str, Any]:
        assert self.console is not None
        agent = self._api_post(
            "control_return_agent", "/api/control", {"command": "return-control"}
        )
        session = self._api_get("state", "/api/state")["control"]
        task = self._api_post(
            "foreground_start",
            "/api/tasks",
            {"kind": "hello", "lease_id": agent["lease_id"], "generation": agent["generation"]},
        )
        foreground_id = task.get("job_id")
        if not isinstance(foreground_id, str):
            raise RuntimeError("foreground desktop task returned no job id")
        deadline = time.monotonic() + min(MAX_RUNTIME_SECONDS, self.args.wall_seconds + 900)
        overlap_work = False
        overlap_units = False
        lab_job: dict[str, Any] = {}
        foreground: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            both_working, both_active = self._sample_once(foreground_id, started["job_id"], since)
            overlap_work = overlap_work or both_working
            overlap_units = overlap_units or both_active
            lab_job = self._poll_status(started["job_id"])
            foreground, _ = self._poll_foreground(foreground_id)
            foreground_done = foreground is not None and foreground.get("status") in {
                "succeeded",
                "failed",
                "cancelled",
                "waiting_human",
            }
            if foreground_done and lab_job.get("lab_state") in {"completed", "stopped", "failed"}:
                break
            if self.dispatch_wrapper and self.dispatch_wrapper.poll() not in {None, 0}:
                raise RuntimeError("run-bound Lab dispatcher exited nonzero")
            time.sleep(self.args.poll_seconds)
        if (
            not foreground
            or foreground.get("status") != "succeeded"
            or foreground.get("real_model") is not True
        ):
            raise RuntimeError("foreground hello task did not succeed with the real Decider")
        if (self.workspace / "hello.txt").read_bytes() != b"Hello from the local agent.\n":
            raise RuntimeError("verified foreground workspace content differs")
        if lab_job.get("lab_state") != "completed":
            raise RuntimeError("Lab run did not reach completed state")
        expected = {
            owner: principal
            for owner, principal in (
                ("aos", self.aos_principal),
                ("lab", self.dispatch_principal),
            )
            if principal is not None
        }
        ledger = _gpu_ledger(self.args.gpu_runtime_db, since, expected)
        turns = ledger.get("turns", []) if ledger.get("available") else []
        if not self.overlap_ticket_window:
            raise RuntimeError("no sampled interval showed both real tasks and owner units working")
        overlap_start, overlap_end = self.overlap_ticket_window
        completed_overlap_owners = {
            turn.get("owner")
            for turn in turns
            if turn.get("state") == "done"
            and overlap_start <= float(turn.get("submitted_at", -1)) <= overlap_end
        }
        if not {"aos", "lab"}.issubset(completed_overlap_owners):
            raise RuntimeError(
                "both owners did not complete exact broker tickets during measured overlap"
            )
        if ledger.get("unexpected_turns") or ledger.get("binding_errors"):
            raise RuntimeError("completed GPU turns lack trusted parent or child provenance")
        if not overlap_work or not overlap_units:
            raise RuntimeError("measured AOS/Lab process and work intervals did not overlap")
        if any(sample["lab_job_in_foreground_scheduler"] for sample in self.samples):
            raise RuntimeError("detached Lab run appeared in the foreground desktop scheduler")
        return {
            "session_id": session["session_id"],
            "aos_job_id": started["job_id"],
            "aos_run_id": started["aos_run_id"],
            "lab_run_id": started["lab_run_id"],
            "foreground_job_id": foreground_id,
            "foreground_status": foreground["status"],
            "foreground_real_model": foreground["real_model"],
            "lab_terminal_state": lab_job["lab_state"],
            "overlap_work": overlap_work,
            "overlap_systemd_units": overlap_units,
            "overlap_ticket_window_boottime": [overlap_start, overlap_end],
            "gpu_turns": ledger,
            "workspace_file_sha256": _sha(self.workspace / "hello.txt"),
        }

    def _recover_after_restart(self, started: dict[str, Any]) -> dict[str, Any]:
        assert self.console is not None
        agent = self._api_post(
            "control_return_agent", "/api/control", {"command": "return-control"}
        )
        del agent
        old_session = self._api_get("state", "/api/state")["control"]["session_id"]
        quiesced = self._api_post(
            "restart_quiesce", "/api/restart/quiesce", {"session_id": old_session}
        )
        state = self._poll_status(started["job_id"])
        self.phase_receipts.append(
            {
                "phase": "restart_quiesce",
                "result": quiesced,
                "lab_state_after_drain": state.get("lab_state"),
            }
        )
        self._stop_aos()
        self.token_before = set((self.args.aos_source / "runs").glob("desktop-console-*.token"))
        self._launch_aos()
        new_session = self._api_get("state", "/api/state")["control"]
        if new_session.get("session_id") == old_session:
            raise RuntimeError("AOS process restart did not create a new desktop session")
        human = self._take_human()
        recovered = self._api_post(
            "lab_recover",
            f"/api/lab/jobs/{started['job_id']}/recover",
            {"lease_id": human["lease_id"], "generation": human["generation"]},
        )
        if recovered.get("lab_run_id") != started["lab_run_id"]:
            raise RuntimeError("explicit recovery rebound a different Lab run")
        deadline = time.monotonic() + min(180, self.args.wall_seconds + 60)
        while time.monotonic() < deadline:
            current = self._poll_status(started["job_id"])
            if current.get("lab_state") in {"completed", "stopped", "failed"}:
                report = self._api_get("lab_report", f"/api/lab/jobs/{started['job_id']}/report")
                return {
                    "old_session_id": old_session,
                    "new_session_id": new_session["session_id"],
                    "recovered_lab_run_id": recovered.get("lab_run_id"),
                    "terminal_lab_state": current.get("lab_state"),
                    "report_sha256": report.get("report_sha256"),
                }
            time.sleep(self.args.poll_seconds)
        raise TimeoutError("recovered Lab run did not reach a verified terminal state")

    def _make_html_report(self, run_id: str) -> dict[str, object]:
        env = {
            **os.environ,
            "LAB_DIRECTOR_DSN_FILE": str(self.args.director_dsn_file),
            "LAB_PLANNER_DSN_FILE": str(self.args.planner_dsn_file),
            "LAB_SUITE_REGISTRY_FILE": str(self.args.suite_registry),
            "SWAPP_AOS_GPU_UNIT": self.args.aos_unit,
            "SWAPP_LAB_GPU_UNIT": (self.dispatch_unit or ""),
            "SWAPP_GPU_RUNTIME_DB": str(self.args.gpu_runtime_db),
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
        }
        result = subprocess.run(
            [str(self.args.lab_python), "-m", "lab.cli", "report", run_id],
            cwd=self.args.lab_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("verified Lab HTML report command failed")
        try:
            payload = json.loads(result.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as exc:
            raise RuntimeError("Lab report command returned no receipt") from exc
        report_path = Path(payload["report"])
        if not _inside(report_path, self.args.lab_root / "data/runtime/reports"):
            raise RuntimeError("Lab report escaped the isolated runtime directory")
        if _sha(report_path) != payload.get("sha256"):
            raise RuntimeError("Lab HTML report digest differs from CLI receipt")
        return {
            "run_id": run_id,
            "path": str(report_path),
            "sha256": payload["sha256"],
            "bytes": report_path.stat().st_size,
            "summary": payload.get("summary"),
        }

    def _stop_dispatch(self) -> None:
        if self.dispatch_unit is not None:
            receipt = _stop_owned_unit(
                self.dispatch_unit, "lab", self.dispatch_principal, timeout=30
            )
            self.cleanup_receipts.append(receipt)
            if receipt.get("result") == "drained":
                self.dispatch_principal = None
            else:
                self.outcome = "failed"
                self.failure_type = "lab_owned_dispatch_drain_failed"
        if self.dispatch_wrapper is not None and self.dispatch_wrapper.poll() is None:
            self.dispatch_wrapper.terminate()
            try:
                self.dispatch_wrapper.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.dispatch_wrapper.kill()
                self.dispatch_wrapper.wait(timeout=5)

    def execute(self) -> dict[str, object]:
        started_at = time.time()
        since = _boottime()
        try:
            self._launch_aos()
            started = self._start_lab()
            # Start the run-bound Lab process before returning the foreground
            # scheduler slot to AOS; the subsequent hello task can then overlap.
            self._launch_dispatch(started["lab_run_id"])
            if self.args.phase == "coexistence":
                coexistence = self._wait_coexistence(started, since)
                html_report = self._make_html_report(started["lab_run_id"])
                self.outcome = "passed"
                return {
                    "phase": "coexistence",
                    "started": started,
                    "coexistence": coexistence,
                    "html_report": html_report,
                }
            recovery = self._recover_after_restart(started)
            html_report = self._make_html_report(started["lab_run_id"])
            self.outcome = "passed"
            return {
                "phase": "restart-recovery",
                "started": started,
                "restart_recovery": recovery,
                "html_report": html_report,
            }
        except Exception as exc:
            self.outcome = "failed"
            self.failure_type = type(exc).__name__
            raise
        finally:
            try:
                self._stop_aos()
            except Exception as exc:
                self.cleanup_receipts.append(
                    {
                        "owner": "aos",
                        "result": "cleanup_exception",
                        "error_type": type(exc).__name__,
                    }
                )
                self.outcome = "failed"
            try:
                self._stop_dispatch()
            except Exception as exc:
                self.cleanup_receipts.append(
                    {
                        "owner": "lab",
                        "result": "cleanup_exception",
                        "error_type": type(exc).__name__,
                    }
                )
                self.outcome = "failed"
            if self.token_path is not None:
                self.token_path.unlink(missing_ok=True)
            report = {
                "schema": "aos-lab-coexistence-review.v1",
                "started_at": started_at,
                "finished_at": time.time(),
                "phase": self.args.phase,
                "outcome": self.outcome,
                "failure_type": self.failure_type,
                "aos_source": str(self.args.aos_source),
                "aos_unit": self.unit,
                "lab_root": str(self.args.lab_root),
                "suite": self.args.suite,
                "suite_manifest_sha256": self.suite["suite_manifest_sha256"],
                "desktop_manifest_sha256": _sha(self.args.desktop_manifest),
                "decider_manifest_sha256": _sha(self.args.decider_manifest),
                "phase_receipts": self.phase_receipts,
                "cleanup_receipts": self.cleanup_receipts,
                "api_latency_ms": self.api_ms,
                "resource_samples": self.samples,
                "limitations": [
                    "A real acceptance requires --execute and success in every phase.",
                    "No hidden model reasoning or prompt content is collected.",
                ],
            }
            (self.base / "driver-observations.json").write_text(json.dumps(report, indent=2) + "\n")
            os.chmod(self.base / "driver-observations.json", 0o600)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aos-source", type=Path, required=True)
    parser.add_argument("--aos-python", type=Path)
    parser.add_argument("--desktop-manifest", type=Path)
    parser.add_argument("--decider-manifest", type=Path)
    parser.add_argument("--model-python", type=Path)
    parser.add_argument("--aos-unit", required=True)
    parser.add_argument("--lab-root", type=Path, required=True)
    parser.add_argument("--lab-python", type=Path, required=True)
    parser.add_argument("--lab-api-url", required=True)
    parser.add_argument("--lab-token-file", type=Path, required=True)
    parser.add_argument("--suite-registry", type=Path, required=True)
    parser.add_argument("--suite-runtime-root", type=Path, required=True)
    parser.add_argument("--director-dsn-file", type=Path, required=True)
    parser.add_argument("--planner-dsn-file", type=Path, required=True)
    parser.add_argument(
        "--gpu-runtime-db",
        type=Path,
        default=Path.home() / ".local/state/swapp-gpu/arbiter.sqlite3",
    )
    parser.add_argument(
        "--lab-maintenance-unit", default="swapp-lab-gpu-maintenance.service"
    )
    parser.add_argument("--suite", required=True)
    parser.add_argument("--experiments", type=int, required=True)
    parser.add_argument("--wall-seconds", type=int, required=True)
    parser.add_argument("--model-tokens", type=int, required=True)
    parser.add_argument(
        "--phase", choices=("coexistence", "restart-recovery"), default="coexistence"
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--startup-seconds", type=int, default=300)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="start the isolated actual AOS/Lab acceptance services",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    runner: CoexistenceRun | None = None
    try:
        _no_symlink_components(args.aos_source)
        _no_symlink_components(args.lab_root)
        args.aos_source = args.aos_source.resolve(strict=True)
        args.lab_root = args.lab_root.resolve(strict=True)
        if args.aos_source == LIVE_AOS_ROOT or _inside(args.aos_source, LIVE_AOS_ROOT):
            raise ValueError("live AOS source is outside this driver scope")
        aos_runtime_root = PROJECT_ROOT / "data/runtime/aos-coexistence"
        if args.aos_source == aos_runtime_root or not _inside(
            args.aos_source, aos_runtime_root
        ):
            raise ValueError("AOS source must be an isolated copy under private runtime")
        if args.lab_root == PROJECT_ROOT or not _inside(
            args.lab_root, PROJECT_ROOT / "data/runtime"
        ):
            raise ValueError(
                "Lab must use a separate worktree under this project's private runtime"
            )
        if args.lab_root == LIVE_AOS_ROOT or _inside(args.lab_root, LIVE_AOS_ROOT):
            raise ValueError("Lab worktree cannot be inside live AOS")
        expected_lab_python = (args.lab_root / ".venv/bin/python").resolve(strict=True)
        if args.lab_python.resolve(strict=True) != expected_lab_python:
            raise ValueError(
                "Lab interpreter must be the read-only interpreter linked by this clone"
            )
        preflight_blockers: list[str] = []
        for name in (
            "aos_python",
            "lab_python",
            "model_python",
            "desktop_manifest",
            "decider_manifest",
            "lab_token_file",
            "suite_registry",
            "director_dsn_file",
            "planner_dsn_file",
        ):
            path = getattr(args, name)
            if path is None:
                if name in {"aos_python", "model_python", "desktop_manifest", "decider_manifest"}:
                    preflight_blockers.append(f"{name}_not_supplied")
                    continue
                raise ValueError(f"required isolated input unavailable: {name}")
            if name == "lab_python":
                _pinned_lab_interpreter(args.lab_root, path)
            else:
                _no_symlink_components(path)
            if not path.exists() or (
                name not in {"aos_python", "lab_python", "model_python"} and path.is_symlink()
            ):
                if name in {"aos_python", "model_python", "desktop_manifest", "decider_manifest"}:
                    preflight_blockers.append(f"{name}_unavailable")
                    continue
                raise ValueError(f"required isolated input unavailable: {name}")
        for name in ("aos_python", "lab_python", "model_python"):
            executable_value = getattr(args, name)
            if executable_value is None:
                continue
            executable = executable_value.resolve(strict=True)
            if not executable.is_file() or not os.access(executable, os.X_OK):
                raise ValueError(f"required isolated executable unavailable: {name}")
        _private_file(args.lab_token_file, "Lab API token")
        _private_file(args.director_dsn_file, "Director DSN")
        _private_file(args.planner_dsn_file, "Planner DSN")
        if args.lab_token_file != args.lab_root / "data/runtime/aos-coexistence/aos-token":
            raise ValueError("Lab token must be the token registered for this private clone")
        expected_registry = args.lab_root / "data/runtime/aos-coexistence/suite-registry.json"
        if args.suite_registry != expected_registry:
            raise ValueError(
                "suite registry must be the registry prepared for this private clone"
            )
        if not _inside(args.suite_runtime_root, args.lab_root / "data/runtime"):
            raise ValueError("suite runtime root must remain under this Lab worktree")
        _no_symlink_components(args.gpu_runtime_db)
        fixed_gpu_database = Path.home() / ".local/state/swapp-gpu/arbiter.sqlite3"
        if args.gpu_runtime_db != fixed_gpu_database:
            preflight_blockers.append("gpu_runtime_db_not_the_fixed_shared_broker_database")
        if args.lab_api_url != args.lab_api_url.rstrip("/"):
            raise ValueError("Lab API URL must not have a trailing slash")
        parsed = urlsplit(args.lab_api_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.path
        ):
            raise ValueError("Lab API must be an exact loopback origin")
        if re.fullmatch(r"swapp-aos-gpu-review-[0-9a-f]{32}\.service", args.aos_unit) is None:
            raise ValueError("AOS owner unit must be a unique review UUID principal")
        if not 1 <= args.experiments <= 35 or not 1 <= args.wall_seconds <= MAX_RUNTIME_SECONDS:
            raise ValueError("run budget is outside the bounded Lab API contract")
        if not 0 <= args.model_tokens <= 350_000 or not 1 <= args.startup_seconds <= 900:
            raise ValueError("token or startup budget is outside its allowed bound")
        if not 0.5 <= args.poll_seconds <= 10:
            raise ValueError("poll cadence must be between 0.5 and 10 seconds")
        if args.aos_source.resolve() == args.lab_root.resolve():
            raise ValueError("AOS and Lab sources must remain separate")
        for path in (args.desktop_manifest, args.decider_manifest):
            if path is None or not path.exists():
                continue
            _no_symlink_components(path)
            resolved = path.resolve(strict=True)
            if (
                resolved == LIVE_AOS_ROOT
                or LIVE_AOS_ROOT in resolved.parents
                or not _inside(resolved, args.aos_source)
            ):
                raise ValueError("caller-supplied manifests must come from isolated AOS source")
            _private_file(path, "AOS runtime manifest")
        suite = _suite_metadata(args)
        shared_database = _shared_database_preflight(args)
        lab_shared_database = _lab_shared_database_support(args)
        postgres_targets = _postgres_target_fingerprint(args)
        preflight_blockers.extend(shared_database["blockers"])
        if not lab_shared_database.get("ready"):
            preflight_blockers.append(str(lab_shared_database.get("reason")))
        if not postgres_targets.get("same_private_target"):
            preflight_blockers.append(str(postgres_targets.get("reason")))
        preflight_blockers = sorted(set(preflight_blockers))
        if not args.execute:
            print(
                json.dumps(
                    {
                        "schema": "aos-lab-coexistence-preflight.v1",
                        "ready_for_explicit_execute": not preflight_blockers,
                        "aos_source": str(args.aos_source),
                        "lab_root": str(args.lab_root),
                        "aos_unit": args.aos_unit,
                        "suite": suite,
                        "shared_gpu_database": shared_database,
                        "lab_shared_database_support": lab_shared_database,
                        "private_postgres_targets": postgres_targets,
                        "blockers": preflight_blockers,
                        "phase": args.phase,
                        "gpu_called": False,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if preflight_blockers:
            raise RuntimeError("CPU preflight has unresolved blockers; no service was started")
        output_parent = args.output_dir.parent.resolve(strict=True)
        if not _inside(output_parent, PROJECT_ROOT / "data/runtime"):
            raise ValueError("acceptance outputs must stay inside this project's private runtime")
        output_root = output_parent / args.output_dir.name
        if output_root.is_symlink():
            raise ValueError("acceptance output directory cannot be a symlink")
        output_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(output_root, 0o700)
        output_root = output_root.resolve(strict=True)
        runner = CoexistenceRun(args, output_root, suite)
        result = runner.execute()
        result.update(
            {
                "status": "passed",
                "gpu_called": True,
                "receipt_path": str(runner.base / "driver-observations.json"),
                "receipt_sha256": _sha(runner.base / "driver-observations.json"),
            }
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError, TimeoutError, KeyError, TypeError) as exc:
        failure = {
            "status": "failed",
            "error_type": type(exc).__name__,
            "gpu_called": bool(args.execute),
        }
        if runner is not None:
            receipt = runner.base / "driver-observations.json"
            if receipt.is_file():
                failure.update({"receipt_path": str(receipt), "receipt_sha256": _sha(receipt)})
        print(json.dumps(failure), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
