"""Internal factual observation of workers recorded by the sole GPU arbiter.

This reader creates no scheduler, Store, schema, grant, release or exemption.
Its records are diagnostics, including historical rows and unknown provenance.
They cannot be used as an interpreter allowlist or native absence evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from lab.llm.aos_gpu_control_store import canonical, digest, validate_admission_binding
from lab.llm.gpu_scheduler import (
    PrincipalReceipt,
    ProcessIdentity,
    _boot_id,
    _process_cgroup,
    _read_process_identity,
    _systemctl_show,
    boottime,
)
from lab.llm.native_runtime import (
    MODEL_UNIT_CPU_PERCENT,
    MODEL_UNIT_MEMORY_BYTES,
    MODEL_UNIT_TASKS,
    GpuObserver,
    SystemdUnitManager,
    UnitManager,
    _observation_timeout,
    observation_deadline,
)

_UNIT = re.compile(r"swapp-(aos|lab)-gpu-turn-[0-9a-f]{32}\.service\Z")
_MAX_RECORDS = 10_000
_MAX_PROCESSES = 8192
_NAMESPACES = ("pid", "net", "user", "mnt")
_COLUMNS = {
    "gpu_turn_state": "*",
    "gpu_turn_requests": "*",
    "gpu_runtime_bindings": (
        "owner,request_id,fencing_token,unit,nonce,model_sha256,expected_description,"
        "created_boottime,runtime_max_seconds,launch_state,invocation_id,main_pid,"
        "main_start_ticks,boot_id,control_group,observed_gpu_pids_json"
    ),
    "aos_gpu_child_bindings": (
        "owner,request_id,fencing_token,unit,nonce,profile_id,deployment_digest,request_sha256,"
        "created_boottime,total_seconds,launch_state,invocation_id,main_pid,main_start_ticks,"
        "boot_id,control_group,observed_gpu_pids_json"
    ),
    "aos_control_requests": (
        "request_id,request_sha256,principal_json,principal_sha256,profile_id,deployment_digest,"
        "profile_config_sha256,admission_binding_json,admission_binding_sha256,allocation_json,"
        "allocation_sha256,budget_json,original_deadline,state,handoff_stage,child_json"
    ),
    "aos_gpu_output_bindings": (
        "request_id,request_sha256,request_json,principal_json,profile_id,deployment_digest,"
        "profile_config_sha256"
    ),
}


@dataclass(frozen=True, slots=True)
class ProcessObservation:
    identity: ProcessIdentity
    uid: int
    control_group: str
    namespaces: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class WorkerObservation:
    owner: str
    request_id: str | None
    fencing_token: int | None
    unit: str | None
    launch_state: str | None
    allocation_active: bool
    status: str
    physical_presence: Literal["observed", "not_observed", "unknown"]
    principal: PrincipalReceipt | None = None
    model_sha256: str | None = None
    profile_id: str | None = None
    deployment_digest: str | None = None
    provider_config_sha256: str | None = None
    broker_generation_sha256: str | None = None
    processes: tuple[ProcessObservation, ...] = ()
    gpu_pids: tuple[int, ...] = ()
    blockers: tuple[str, ...] = ()
    # Current namespace measurements are not original launch provenance.
    namespace_provenance: None = None


@dataclass(frozen=True, slots=True)
class InventoryObservation:
    workers: tuple[WorkerObservation, ...]
    blockers: tuple[str, ...]
    before_source_sha256: str | None
    after_source_sha256: str | None
    source_changed: bool | None
    authority: Literal[False] = field(default=False, init=False)


class _Unavailable(ValueError):
    pass


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise _Unavailable("observation_deadline_exceeded")


def _snapshot(database: Path, deadline: float) -> tuple[dict[str, Any], tuple[int, int]]:
    _check_deadline(deadline)
    if (
        not database.is_absolute()
        or ".." in database.parts
        or database.resolve(strict=True) != database
    ):
        raise _Unavailable("database_path_not_canonical")
    parent = database.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) & 0o077
    ):
        raise _Unavailable("database_parent_not_private")
    info = database.lstat()
    if (
        not database.is_absolute()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise _Unavailable("database_identity_unavailable")
    identity = (info.st_dev, info.st_ino)
    # SQLite's WAL reader can create/write shared-memory sidecars. This narrow
    # diagnostic declines that mode instead of using immutable=1 and hiding WAL.
    with database.open("rb") as stream:
        header = stream.read(20)
    if header[18:20] == b"\x02\x02":
        raise _Unavailable("readonly_wal_snapshot_unsupported")
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.25)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        db.execute("BEGIN")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result: dict[str, Any] = {}
        for table, columns in _COLUMNS.items():
            _check_deadline(deadline)
            if table not in tables:
                result[table] = None
                continue
            # Names come from the fixed internal map; the row limit is bound.
            rows = db.execute(
                f"SELECT {columns} FROM {table} LIMIT ?",  # nosec B608
                (_MAX_RECORDS + 1,),
            ).fetchall()
            if len(rows) > _MAX_RECORDS:
                raise _Unavailable("source_record_limit_exceeded")
            records = [dict(row) for row in rows]
            if table == "aos_gpu_output_bindings":
                for record in records:
                    raw = record.pop("request_json")
                    frame = json.loads(raw)
                    record["frame_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
                    record["payload_sha256"] = digest(frame["payload"])
                    record["canonical_frame"] = canonical(frame) == raw
            result[table] = sorted(records, key=canonical)
        db.rollback()
    after = database.lstat()
    if (after.st_dev, after.st_ino) != identity:
        raise _Unavailable("database_replaced")
    return result, identity


def _runtime_units() -> Sequence[str]:
    result = SystemdUnitManager._run(
        [
            "/usr/bin/systemctl",
            "--user",
            "list-units",
            "--all",
            "--plain",
            "--no-legend",
            "--no-pager",
            "swapp-lab-gpu-turn-*.service",
            "swapp-aos-gpu-turn-*.service",
        ],
        timeout=_observation_timeout(3),
    )
    if result.returncode or len(result.stdout.encode()) > 1024 * 1024:
        raise _Unavailable("unit_enumeration_unavailable")
    return tuple(line.split()[0] for line in result.stdout.splitlines() if line.strip())


def _owned_processes() -> Sequence[int]:
    entries = sorted(
        (path for path in Path("/proc").iterdir() if path.name.isdecimal()),
        key=lambda path: int(path.name),
    )
    if len(entries) > _MAX_PROCESSES:
        raise _Unavailable("process_enumeration_limit_exceeded")
    pids = []
    for path in entries:
        try:
            if path.stat().st_uid == os.getuid():
                pids.append(int(path.name))
        except FileNotFoundError:
            continue
    return pids


def observe_process(pid: int) -> ProcessObservation | None:
    """Read current birth, cgroup and namespace facts; disappearance is explicit."""
    identity = _read_process_identity(pid)
    if identity is None:
        return None
    root = Path("/proc") / str(pid)
    uid = root.stat().st_uid
    group = _process_cgroup(pid)
    namespaces = tuple((name, (root / "ns" / name).stat().st_ino) for name in _NAMESPACES)
    if (
        _read_process_identity(pid) != identity
        or root.stat().st_uid != uid
        or _process_cgroup(pid) != group
    ):
        raise _Unavailable("process_changed_during_observation")
    return ProcessObservation(identity, uid, group, namespaces)


def observe_principal(receipt: PrincipalReceipt) -> bool:
    values = _systemctl_show(receipt.unit, owner=receipt.owner, timeout=_observation_timeout(3))
    return (
        values["LoadState"] == "loaded"
        and values["ActiveState"] == "active"
        and values["MainPID"] == str(receipt.identity.pid)
        and values["InvocationID"] == receipt.invocation_id
        and values["ControlGroup"].endswith("/" + receipt.unit)
        and _read_process_identity(receipt.identity.pid) == receipt.identity
        and _process_cgroup(receipt.identity.pid) == values["ControlGroup"]
    )


def _principal(ticket: Mapping[str, Any]) -> PrincipalReceipt:
    return PrincipalReceipt(
        ticket["owner"],
        ProcessIdentity(ticket["owner_pid"], ticket["owner_start_ticks"], ticket["owner_boot_id"]),
        ticket["owner_unit"],
        ticket["owner_invocation_id"],
    )


def _aos_provenance(
    row: Mapping[str, Any],
    control: Mapping[str, Any] | None,
    ticket: Mapping[str, Any] | None,
    output: Mapping[str, Any] | None,
) -> tuple[str | None, str | None]:
    if control is None or ticket is None or output is None:
        raise _Unavailable("aos_original_control_missing")
    binding = validate_admission_binding(json.loads(control["admission_binding_json"]))
    allocation = json.loads(control["allocation_json"])
    peer = json.loads(control["principal_json"])
    lease = allocation["lease"]
    if (
        canonical(binding) != control["admission_binding_json"]
        or digest(binding) != control["admission_binding_sha256"]
        or canonical(allocation) != control["allocation_json"]
        or digest(allocation) != control["allocation_sha256"]
        or digest(peer) != control["principal_sha256"]
        or peer != binding["caller_generation"]
        or peer != allocation["original_principal"]
        or allocation["admission_binding_sha256"] != control["admission_binding_sha256"]
        or allocation["request_sha256"] != control["request_sha256"]
        or allocation["original_budget"] != json.loads(control["budget_json"])
        or allocation["original_deadline"] != control["original_deadline"]
        or ticket["payload_sha256"] != control["request_sha256"]
        or output["canonical_frame"] is not True
        or output["frame_sha256"] != control["request_sha256"]
        or output["request_sha256"] != control["request_sha256"]
        or output["payload_sha256"] != row["request_sha256"]
        or output["principal_json"] != control["principal_json"]
        or any(
            output[key] != control[key]
            for key in ("request_id", "profile_id", "deployment_digest", "profile_config_sha256")
        )
        or any(lease[key] != row[key] for key in ("owner", "request_id", "fencing_token"))
        or lease["owner_identity"] != {key: peer[key] for key in ("pid", "start_ticks", "boot_id")}
        or lease["owner_unit"] != peer["unit"]
        or lease["owner_invocation_id"] != peer["invocation_id"]
        or any(
            peer[key] != ticket[column]
            for key, column in (
                ("pid", "owner_pid"),
                ("start_ticks", "owner_start_ticks"),
                ("boot_id", "owner_boot_id"),
                ("unit", "owner_unit"),
                ("invocation_id", "owner_invocation_id"),
            )
        )
        or control["profile_id"] != row["profile_id"]
        or binding["profile_id"] != row["profile_id"]
        or control["deployment_digest"] != row["deployment_digest"]
        or binding["profile_pin"]["deployment_digest"] != row["deployment_digest"]
        or binding["profile_pin"]["config_sha256"] != control["profile_config_sha256"]
    ):
        raise _Unavailable("aos_original_control_mismatch")
    return control["profile_config_sha256"], digest(binding["server_generation"])


def observe_worker_inventory(
    database: Path,
    *,
    units: UnitManager,
    gpu: GpuObserver,
    enumerate_units: Callable[[], Sequence[str]] = _runtime_units,
    enumerate_processes: Callable[[], Sequence[int]] = _owned_processes,
    process_reader: Callable[[int], ProcessObservation | None] = observe_process,
    principal_reader: Callable[[PrincipalReceipt], bool] = observe_principal,
    timeout_seconds: float = 10.0,
) -> InventoryObservation:
    """Observe existing records and physical facts, always without authority.

    All OS readers run after the first SQL transaction has closed. Unknown
    historical provenance stays on its record; current uncertainties also
    appear in global blockers. No raw requests, configuration paths or prompts
    are returned. A missing or changed source can never become an empty result.
    """
    if (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 30
    ):
        raise ValueError("observation timeout must be finite and at most 30 seconds")
    deadline = time.monotonic() + timeout_seconds
    token = observation_deadline.set(deadline)
    workers: list[WorkerObservation] = []
    blockers: list[str] = []
    before_hash = after_hash = None
    changed: bool | None = None
    try:
        boot = _boot_id()
        before, database_identity = _snapshot(database, deadline)
        before_hash = digest(before)
        if any(
            before[name] is None
            for name in ("gpu_turn_state", "gpu_turn_requests", "gpu_runtime_bindings")
        ):
            raise _Unavailable("canonical_source_schema_incomplete")
        states = before["gpu_turn_state"]
        if len(states) != 1 or states[0]["singleton"] != 1:
            raise _Unavailable("canonical_allocation_state_incomplete")
        state = states[0]
        active = (state["active_owner"], state["active_request_id"], state["active_token"])
        if (active[0] is None) != (active[1] is None) or (
            active[0] is None and state["phase"] is not None
        ):
            blockers.append("canonical_allocation_state_mismatch")
        tickets = {(row["owner"], row["request_id"]): row for row in before["gpu_turn_requests"]}
        controls = {row["request_id"]: row for row in before["aos_control_requests"] or ()}
        outputs = {row["request_id"]: row for row in before["aos_gpu_output_bindings"] or ()}
        rows = [*before["gpu_runtime_bindings"], *(before["aos_gpu_child_bindings"] or ())]
        if (before["aos_gpu_child_bindings"] is None) != (before["aos_control_requests"] is None):
            blockers.append("aos_source_schema_incomplete")
        for ticket in tickets.values():
            if ticket["state"] == "active" and (
                (ticket["owner"], ticket["request_id"]) != active[:2]
                or not any(
                    (row["owner"], row["request_id"], row["fencing_token"]) == active
                    for row in rows
                )
            ):
                blockers.append("active_ticket_without_matching_worker_allocation")
        names = tuple(enumerate_units())
        pids = tuple(enumerate_processes())
        if len(names) > _MAX_RECORDS or len(pids) > _MAX_PROCESSES:
            raise _Unavailable("physical_enumeration_limit_exceeded")
        processes: dict[int, ProcessObservation] = {}
        process_scan_incomplete = False
        for pid in pids:
            _check_deadline(deadline)
            try:
                process = process_reader(pid)
                if process is not None:
                    if process.identity.pid != pid or process.identity.boot_id != boot:
                        raise _Unavailable("process_identity_mismatch")
                    processes[pid] = process
            except (OSError, ValueError, RuntimeError):
                blockers.append("process_observation_unavailable")
                process_scan_incomplete = True
        _check_deadline(deadline)
        gpu_snapshot = gpu.snapshot()
        seen_units: set[str] = set()
        covered_gpu: set[int] = set()
        for row in rows:
            _check_deadline(deadline)
            key = (row["owner"], row["request_id"], row["fencing_token"])
            ticket = tickets.get(key[:2])
            is_active = key == active
            issues = ["original_namespace_provenance_unknown"]
            presence: Literal["observed", "not_observed", "unknown"] = "unknown"
            status = "unavailable"
            principal = _principal(ticket) if ticket is not None else None
            profile = row.get("profile_id")
            deployment = row.get("deployment_digest")
            provider = broker = None
            own_processes: tuple[ProcessObservation, ...] = ()
            own_gpu: tuple[int, ...] = ()
            if row["owner"] == "lab":
                issues.append("lab_profile_provider_deployment_provenance_unknown")
            else:
                try:
                    provider, broker = _aos_provenance(
                        row, controls.get(row["request_id"]), ticket, outputs.get(row["request_id"])
                    )
                    server = json.loads(controls[row["request_id"]]["admission_binding_json"])[
                        "server_generation"
                    ]
                    if is_active and not principal_reader(
                        PrincipalReceipt(
                            "lab",
                            ProcessIdentity(
                                server["pid"], server["start_ticks"], server["boot_id"]
                            ),
                            server["unit"],
                            server["invocation_id"],
                        )
                    ):
                        issues.append("aos_broker_generation_not_observed")
                except (OSError, KeyError, TypeError, ValueError, RuntimeError):
                    issues.append("aos_original_control_unavailable_or_mismatched")
                issues.append("aos_memory_task_configuration_provenance_unknown")
            if ticket is None:
                issues.append("ticket_missing")
            if is_active:
                if state["phase"] == "quarantined":
                    issues.append("allocation_quarantined")
                elif state["phase"] not in {"activating", "inference"}:
                    issues.append("allocation_phase_mismatch")
                if (
                    ticket is None
                    or ticket["state"] != "active"
                    or principal != _principal({**state, "owner": state["active_owner"]})
                ):
                    issues.append("allocation_ticket_principal_mismatch")
                if any(
                    state[name] is None or not boottime() < state[name]
                    for name in (
                        "activation_deadline"
                        if state["phase"] == "activating"
                        else "inference_deadline",
                        "heartbeat_deadline",
                        "total_deadline",
                    )
                ):
                    issues.append("allocation_deadline_expired")
            elif ticket is not None and ticket["state"] == "active":
                issues.append("active_ticket_without_matching_allocation")
            unit = row["unit"]
            match = _UNIT.fullmatch(unit)
            if match is None or match[1] != row["owner"] or unit in seen_units:
                issues.append("worker_unit_binding_mismatch")
            else:
                seen_units.add(unit)
                try:
                    snapshot = units.inspect(unit)
                    group = snapshot.control_group or row["control_group"]
                    own_processes = tuple(
                        process
                        for process in processes.values()
                        if group
                        and (
                            process.control_group == group
                            or process.control_group.startswith(group + "/")
                        )
                    )
                    own_gpu = tuple(
                        sorted(
                            set(gpu_snapshot.process_memory_mib)
                            & {process.identity.pid for process in own_processes}
                        )
                    )
                    covered_gpu.update(own_gpu)
                    group_pids = units.cgroup_pids(group) if group else frozenset()
                    if len(group_pids) > _MAX_PROCESSES or set(group_pids) != {
                        process.identity.pid for process in own_processes
                    }:
                        issues.append("cgroup_process_observation_mismatch")
                    if (
                        snapshot.load_state == "not-found"
                        and not own_processes
                        and not group_pids
                        and not process_scan_incomplete
                    ):
                        presence, status = (
                            "not_observed",
                            "historical_not_observed" if not is_active else "not_observed",
                        )
                    elif snapshot.load_state == "loaded" and snapshot.active_state in {
                        "active",
                        "activating",
                    }:
                        presence, status = "observed", "observed" if is_active else "orphan"
                        main = processes.get(snapshot.main_pid)
                        expected_description = row.get(
                            "expected_description", f"SWAPP AOS GPU turn {row['nonce']}"
                        )
                        expected_runtime = row.get("runtime_max_seconds", row.get("total_seconds"))
                        if (
                            main is None
                            or main.uid != os.getuid()
                            or main.control_group != snapshot.control_group
                            or snapshot.invocation_id != row["invocation_id"]
                            or snapshot.main_pid != row["main_pid"]
                            or main.identity.start_ticks != row["main_start_ticks"]
                            or main.identity.boot_id != row["boot_id"]
                            or snapshot.control_group != row["control_group"]
                            or not snapshot.control_group.endswith("/swapp-gpu.slice/" + unit)
                            or snapshot.description != expected_description
                            or f"SWAPP_GPU_TURN_NONCE={row['nonce']}"
                            not in snapshot.environment.split()
                            or (
                                snapshot.memory_max != MODEL_UNIT_MEMORY_BYTES
                                if row["owner"] == "lab"
                                else not 1024**3 <= snapshot.memory_max <= MODEL_UNIT_MEMORY_BYTES
                            )
                            or snapshot.memory_swap_max != 0
                            or snapshot.cpu_quota_usec != MODEL_UNIT_CPU_PERCENT * 10_000
                            or (
                                snapshot.tasks_max != MODEL_UNIT_TASKS
                                if row["owner"] == "lab"
                                else not 1 <= snapshot.tasks_max <= MODEL_UNIT_TASKS
                            )
                            or snapshot.runtime_max_usec != expected_runtime * 1_000_000
                        ):
                            status = "physical_mismatch"
                            issues.append("worker_physical_generation_mismatch")
                        elif (
                            any(
                                process_reader(process.identity.pid) != process
                                for process in own_processes
                            )
                            or units.inspect(unit) != snapshot
                        ):
                            status = "physical_mismatch"
                            issues.append("worker_changed_during_observation")
                        if is_active and (principal is None or not principal_reader(principal)):
                            issues.append("allocation_principal_not_observed")
                    elif (
                        snapshot.active_state == "inactive"
                        and snapshot.main_pid == 0
                        and not own_processes
                        and not group_pids
                        and not process_scan_incomplete
                    ):
                        presence, status = (
                            "not_observed",
                            "historical_not_observed" if not is_active else "not_observed",
                        )
                    else:
                        issues.append("worker_physical_state_unknown")
                    if presence == "observed" and not is_active:
                        issues.append("live_worker_without_matching_allocation")
                except (OSError, KeyError, TypeError, ValueError, RuntimeError):
                    issues.append("worker_physical_observation_unavailable")
            if row["launch_state"] in {"prepared", "uncertain", "planned", "starting"}:
                status = "pending"
                issues.append("worker_launch_pending_or_uncertain")
            if is_active and state["phase"] == "quarantined":
                status = "quarantined"
            if is_active and presence != "observed":
                issues.append("active_allocation_worker_not_observed")
            if ticket is None and status not in {"pending", "quarantined"}:
                status = "orphan"
            record = WorkerObservation(
                row["owner"],
                row["request_id"],
                row["fencing_token"],
                unit,
                row["launch_state"],
                is_active,
                status,
                presence,
                principal,
                row.get("model_sha256"),
                profile,
                deployment,
                provider,
                broker,
                own_processes,
                own_gpu,
                tuple(issues),
            )
            workers.append(record)
            if is_active or status in {
                "pending",
                "quarantined",
                "orphan",
                "physical_mismatch",
                "unavailable",
            }:
                blockers.extend(issues)
            else:
                blockers.extend(
                    issue
                    for issue in issues
                    if issue
                    not in {
                        "original_namespace_provenance_unknown",
                        "lab_profile_provider_deployment_provenance_unknown",
                        "aos_memory_task_configuration_provenance_unknown",
                    }
                )
        if active[0] is not None and not any(worker.allocation_active for worker in workers):
            blockers.append("active_allocation_without_worker_binding")
        process_units = {
            part
            for process in processes.values()
            for part in process.control_group.split("/")
            if _UNIT.fullmatch(part)
        }
        for unit in sorted((set(names) | process_units) - seen_units):
            match = _UNIT.fullmatch(unit)
            if match is None:
                blockers.append("unit_enumeration_mismatch")
                continue
            workers.append(
                WorkerObservation(
                    match[1],
                    None,
                    None,
                    unit,
                    None,
                    False,
                    "orphan",
                    "unknown",
                    blockers=("unit_without_durable_worker_binding",),
                )
            )
            blockers.append("unit_without_durable_worker_binding")
        if (
            set(gpu_snapshot.process_memory_mib)
            - set(gpu_snapshot.exempt_display_pids)
            - covered_gpu
        ):
            blockers.append("gpu_process_without_observed_worker_binding")
        _check_deadline(deadline)
        after, after_identity = _snapshot(database, deadline)
        after_hash = digest(after)
        changed = (
            before_hash != after_hash or database_identity != after_identity or _boot_id() != boot
        )
        if changed:
            blockers.append("source_changed_during_observation")
    except _Unavailable as error:
        blockers.append(str(error))
    except (OSError, sqlite3.Error, KeyError, TypeError, ValueError, RuntimeError):
        blockers.append("inventory_observation_unavailable")
    finally:
        observation_deadline.reset(token)
    return InventoryObservation(
        tuple(workers), tuple(sorted(set(blockers))), before_hash, after_hash, changed
    )
