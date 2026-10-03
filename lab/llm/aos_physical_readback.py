"""Read-only physical verification of an independently read original AOS target.

The trusted snapshot callback must use the existing broker's current authority
and original Store. This module never stops a unit, releases a lease, or creates
a scheduler. A retained drain claim alone is insufficient physical proof.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable, Mapping
from typing import Any

from lab.llm.aos_gpu_control_store import canonical
from lab.llm.gpu_scheduler import ProcessIdentity, _boot_id, _process_identity_alive
from lab.llm.native_runtime import (
    MODEL_UNIT_CPU_PERCENT,
    MODEL_UNIT_MEMORY_BYTES,
    MODEL_UNIT_TASKS,
    GpuObserver,
    UnitManager,
    UnitSnapshot,
)


class PhysicalReadbackError(RuntimeError):
    """Current source, worker generation, or physical absence is unproven."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PhysicalReadbackError(message)


def _snapshot(reader: Callable[[], dict[str, Any]], evidence: Mapping[str, Any]) -> dict[str, Any]:
    # Freeze each independently read value before slow OS observations. The
    # source API validates original preimages and query-only SQL consistency.
    value: dict[str, Any] = json.loads(canonical(reader()))
    _require(
        value.get("schema") == "aos-scientist-physical-snapshot.v1"
        and type(value.get("version")) is int
        and value["version"] == 1,
        "unsupported original physical snapshot",
    )
    _require(value["evidence"] == evidence, "physical source differs from exact terminal evidence")
    arbiter = value["arbiter"]
    _require(
        type(arbiter["active_token"]) is int
        and arbiter["active_token"] >= 0
        and (arbiter["active_owner"], arbiter["active_request_id"])
        != ("aos", evidence["target"]["request_id"]),
        "original target remains active or quarantined",
    )
    return value


def verify_physical_readback(
    *,
    read_snapshot: Callable[[], dict[str, Any]],
    expected_evidence: Mapping[str, Any],
    units: UnitManager,
    gpu: GpuObserver,
    memory_bytes: int = MODEL_UNIT_MEMORY_BYTES,
    cpu_percent: int = MODEL_UNIT_CPU_PERCENT,
    task_limit: int = MODEL_UNIT_TASKS,
    boot_id: Callable[[], str] = _boot_id,
    process_alive: Callable[[ProcessIdentity], bool] = _process_identity_alive,
) -> dict[str, Any]:
    """Verify original Store bindings and fresh OS absence without cleanup effects.

    Other lanes may keep running. Only the original child's group/PIDs are
    checked. Reused original GPU PIDs cause conservative denial. The returned
    snapshot is not allocation authority or permission to resolve an AOS journal.
    """
    if (
        type(memory_bytes) is not int
        or not 1024**3 <= memory_bytes <= MODEL_UNIT_MEMORY_BYTES
        or type(cpu_percent) is not int
        or cpu_percent != MODEL_UNIT_CPU_PERCENT
        or type(task_limit) is not int
        or not 1 <= task_limit <= MODEL_UNIT_TASKS
    ):
        raise ValueError("physical observer limits differ from the pinned AOS runtime")
    evidence: dict[str, Any] = json.loads(canonical(dict(expected_evidence)))
    try:
        before = _snapshot(read_snapshot, evidence)
        terminal = json.loads(evidence["terminal_canonical"])
        child = before["child"]
        if evidence["allocation_canonical"] is None:
            _require(
                terminal["release_outcome"] == "never_admitted"
                and child is None
                and before["handoff_stage"] is None
                and evidence["drain_canonical"] is None,
                "no-admission source contains an allocation or child",
            )
        else:
            _require(isinstance(child, dict), "allocated target lacks its original worker")
            _require(
                terminal["release_outcome"] in {"released", "recovered_released"}
                and child["launch_state"] == "drained"
                and child["boot_id"] == boot_id()
                and type(child["main_pid"]) is int
                and child["main_pid"] > 1
                and type(child["main_start_ticks"]) is int
                and child["main_start_ticks"] > 0,
                "original worker generation is unproven",
            )

            def inspect_original() -> None:
                current: UnitSnapshot = units.inspect(child["unit"])
                _require(
                    current.load_state != "not-found"
                    or (current.main_pid == 0 and current.active_state == "inactive"),
                    "absent unit observation still claims a running process",
                )
                if current.load_state != "not-found":
                    nonce_values = [
                        token
                        for token in shlex.split(current.environment)
                        if token.startswith("SWAPP_GPU_TURN_NONCE=")
                    ]
                    _require(
                        current.load_state == "loaded"
                        and current.active_state == "inactive"
                        and current.main_pid == 0
                        and current.invocation_id == child["invocation_id"]
                        and current.control_group == child["control_group"]
                        and current.control_group.endswith("/swapp-gpu.slice/" + child["unit"])
                        and current.description == f"SWAPP AOS GPU turn {child['nonce']}"
                        and nonce_values == [f"SWAPP_GPU_TURN_NONCE={child['nonce']}"]
                        and current.memory_max == memory_bytes
                        and current.memory_swap_max == 0
                        and current.cpu_quota_usec == cpu_percent * 10_000
                        and current.tasks_max == task_limit
                        and current.runtime_max_usec == child["total_seconds"] * 1_000_000,
                        "current unit differs from the drained original generation",
                    )
                _require(units.cgroup_empty(child["control_group"]), "original cgroup is not empty")

            inspect_original()
            identity = ProcessIdentity(
                child["main_pid"], child["main_start_ticks"], child["boot_id"]
            )
            _require(not process_alive(identity), "original worker process is still alive")
            owned = set(child["observed_gpu_pids"]) | {child["main_pid"]}
            _require(
                not owned.intersection(gpu.snapshot().process_memory_mib),
                "original GPU PID remains",
            )
            inspect_original()
            _require(not process_alive(identity), "original worker process reappeared")
            _require(
                not owned.intersection(gpu.snapshot().process_memory_mib),
                "original GPU PID reappeared",
            )
            _require(child["boot_id"] == boot_id(), "host boot changed during physical observation")
        after = _snapshot(read_snapshot, evidence)
        _require(
            {key: value for key, value in before.items() if key != "arbiter"}
            == {key: value for key, value in after.items() if key != "arbiter"},
            "original source bindings changed during physical observation",
        )
        _require(
            after["arbiter"]["active_token"] >= before["arbiter"]["active_token"],
            "scheduler fence regressed during observation",
        )
        return after
    except (KeyError, TypeError, ValueError, OSError, IndexError) as error:
        raise PhysicalReadbackError("physical observation unavailable or malformed") from error
