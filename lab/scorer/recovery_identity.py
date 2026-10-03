"""Physical identity proofs for already registered stopped recovery workers."""

from __future__ import annotations

import os
import re
import subprocess  # nosec B404 -- fixed read-only Docker inventory command
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from lab.scorer.supervisor import (
    _cgroup_is_absent_or_empty,
    _expected_unit_cgroup,
    _systemctl_show,
)
from lab.scorer.worker import ScorerInvocation, verify_systemd_invocation

_FIELDS = {
    "worker_pid",
    "worker_start_ticks",
    "worker_boot_id",
    "worker_unit",
    "worker_invocation_id",
    "worker_cgroup",
}
_PROPERTIES = {"LoadState", "ActiveState", "MainPID", "InvocationID", "ControlGroup"}


def capture_empty_baseline_native_observation(
    owner: Any, run_id: UUID, *, deadline: float
) -> dict[str, Any]:
    """Observe a retired run-specific native owner and empty children, without signalling.

    The zero-job exception requires an empty original native cgroup. A shared
    Director replacement cannot supply this proof. Sandbox labels are phase IDs,
    so the read-only inventory conservatively requires all live Lab sandboxes to
    be absent, as the existing outer stop blocker does.
    """
    from lab.director.recovery import OWNER_DRAIN_UNIT, _valid_owner_unit

    if owner.worker_unit == OWNER_DRAIN_UNIT or not _valid_owner_unit(owner.worker_unit, run_id):
        raise RuntimeError("empty baseline proof requires a run-specific native owner")
    if (
        not owner.worker_cgroup.startswith("/")
        or ".." in Path(owner.worker_cgroup).parts
        or not owner.worker_cgroup.endswith("/" + owner.worker_unit)
    ):
        raise RuntimeError("empty baseline native owner cgroup is malformed")

    def remaining() -> float:
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise RuntimeError("original empty baseline cleanup deadline expired")
        return min(3.0, seconds)

    def observe_owner() -> tuple[str, dict[str, str]]:
        remaining()
        boot = _boot_id()
        if boot == owner.worker_boot_id:
            try:
                ticks = _start_ticks(owner.worker_pid)
            except FileNotFoundError:
                ticks = None
            if ticks is not None:
                # A reused PID is ambiguous here, even if the old process is gone.
                raise RuntimeError("empty baseline owner PID remains present")
        properties = _systemctl_show(owner.worker_unit, timeout_seconds=remaining())
        if not _PROPERTIES.issubset(properties):
            raise RuntimeError("empty baseline native owner inspection is incomplete")
        identity = properties["InvocationID"]
        inactive = (
            properties["LoadState"] == "not-found"
            and properties["ActiveState"] == "inactive"
            and identity == ""
        ) or (
            properties["LoadState"] == "loaded"
            and properties["ActiveState"] in {"inactive", "failed"}
            and identity == owner.worker_invocation_id
        )
        if (
            not inactive
            or properties["MainPID"] != "0"
            or properties["ControlGroup"] not in {"", owner.worker_cgroup}
            or not _cgroup_is_absent_or_empty(owner.worker_cgroup)
        ):
            raise RuntimeError("empty baseline native owner or children remain uncertain")
        return boot, {key: properties[key] for key in sorted(_PROPERTIES)}

    observed = observe_owner()
    containers = subprocess.run(  # nosec B603 -- fixed read-only label query
        [
            "/usr/bin/docker",
            "container",
            "ls",
            "--quiet",
            "--filter=label=swapp.ai-scientist.sandbox.owner",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=remaining(),
    )
    if containers.returncode != 0 or containers.stdout.strip():
        raise RuntimeError("empty baseline sandbox inventory is active or unavailable")
    if observe_owner() != observed:
        raise RuntimeError("empty baseline native owner changed during observation")
    return {
        "schema": "empty-baseline-native-observation.v1",
        "run_id": str(run_id),
        "owner_json": asdict(owner),
        "observed_at": datetime.now(UTC).isoformat(),
        "observed_boot_id": observed[0],
        "unit_properties": observed[1],
        "process_retired": True,
        "native_children_empty": True,
        "sandbox_container_ids": [],
    }


def _start_ticks(pid: int) -> int:
    raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii")
    return int(raw[raw.rfind(")") + 2 :].split()[19])


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()


def _validate_identity(identity: dict[str, Any]) -> None:
    if set(identity) != _FIELDS:
        raise RuntimeError("recovery identity must contain the exact registered process tuple")
    for field in ("worker_pid", "worker_start_ticks"):
        if type(identity[field]) is not int or identity[field] < 1:
            raise RuntimeError("recovery process identity is invalid")
    if identity["worker_pid"] >= 2**31:
        raise RuntimeError("recovery PID is outside the native range")
    for field in ("worker_unit", "worker_invocation_id", "worker_boot_id", "worker_cgroup"):
        if not isinstance(identity[field], str):
            raise RuntimeError("recovery unit identity is invalid")
    if (
        re.fullmatch(r"swapp-ai-scientist-scorer-[0-9a-f]{32}\.service", identity["worker_unit"])
        is None
    ):
        raise RuntimeError("recovery worker is outside the canonical Scorer namespace")
    if re.fullmatch(r"[0-9a-f]{32}", identity["worker_invocation_id"]) is None:
        raise RuntimeError("recovery invocation identity is invalid")
    try:
        if str(UUID(identity["worker_boot_id"])) != identity["worker_boot_id"]:
            raise ValueError
    except ValueError as exc:
        raise RuntimeError("recovery boot identity is invalid") from exc
    group = identity["worker_cgroup"]
    if not group.startswith("/") or ".." in Path(group).parts:
        raise RuntimeError("recovery cgroup identity is invalid")
    if group != _expected_unit_cgroup(identity["worker_unit"]):
        raise RuntimeError("recovery worker escaped the canonical Scorer slice")


def capture_attempted_stop_recovery_identity(invocation: ScorerInvocation) -> dict[str, Any]:
    """Recheck this actual process immediately before normal role registration."""
    current = verify_systemd_invocation(invocation.unit)
    if current != invocation:
        raise RuntimeError("recovery unit generation changed before registration")
    identity = {
        "worker_pid": os.getpid(),
        "worker_start_ticks": _start_ticks(os.getpid()),
        "worker_boot_id": _boot_id(),
        "worker_unit": invocation.unit,
        "worker_invocation_id": invocation.invocation_id,
        "worker_cgroup": invocation.control_group,
    }
    _validate_identity(identity)
    return identity


def verify_attempted_stop_recovery_retirement(identity: dict[str, Any]) -> dict[str, Any]:
    """Prove the registered process and exact unit are retired; never signal them.

    The caller and SQL bind this tuple to the original stop, owner and plan.
    A SQL drained state or absent unit alone cannot prove process retirement.
    """
    _validate_identity(identity)
    boot = _boot_id()
    if boot == identity["worker_boot_id"]:
        try:
            ticks = _start_ticks(identity["worker_pid"])
        except FileNotFoundError:
            ticks = None
        except (OSError, ValueError, IndexError) as exc:
            raise RuntimeError("recovery PID retirement cannot be inspected") from exc
        if ticks == identity["worker_start_ticks"]:
            raise RuntimeError("registered recovery process remains alive")
    properties = _systemctl_show(identity["worker_unit"])
    if not _PROPERTIES.issubset(properties):
        raise RuntimeError("recovery unit retirement inspection is incomplete")
    group = identity["worker_cgroup"]
    if properties["LoadState"] == "not-found":
        retired = (
            properties["ActiveState"] == "inactive"
            and properties["MainPID"] == "0"
            and properties["InvocationID"] == ""
            and properties["ControlGroup"] in {"", group}
        )
    else:
        retired = (
            properties["LoadState"] == "loaded"
            and properties["ActiveState"] in {"inactive", "failed"}
            and properties["MainPID"] == "0"
            and properties["InvocationID"] == identity["worker_invocation_id"]
            and properties["ControlGroup"] in {"", group}
        )
    if not retired or not _cgroup_is_absent_or_empty(group):
        raise RuntimeError("exact recovery worker unit or cgroup remains uncertain")
    return {
        "schema": "attempted-stop-retirement-observation.v2",
        "worker_identity": dict(identity),
        "observed_boot_id": boot,
        "observed_at": datetime.now(UTC).isoformat(),
        "process_retired": True,
        "cgroup_empty": True,
        "unit_properties": {key: properties[key] for key in sorted(_PROPERTIES)},
    }
