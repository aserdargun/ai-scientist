"""Independent multi-turn observation for the local Qwen integration review.

This utility does not grant GPU leases or start models. It follows all model
bindings in one new review database and may stop only their exact generations.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time

from review_gpu_host import GIB, ROOT, snapshot


def unit_state(unit: str) -> dict[str, str]:
    if not re.fullmatch(r"swapp-lab-gpu-(?:review|turn)-[0-9a-f]{32}\.service", unit):
        raise ValueError("unit is outside the integration review namespace")
    result = subprocess.run([
        "/usr/bin/systemctl", "--user", "show", "--no-pager", unit,
        "--property=LoadState,ActiveState,InvocationID,MainPID,ControlGroup,Description,"
        "MemoryMax,MemorySwapMax,CPUQuotaPerSecUSec,TasksMax,MemoryPeak,RuntimeMaxUSec,"
        "Result,ExecMainStatus",
    ], capture_output=True, text=True, timeout=5, check=False)
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if result.returncode and fields.get("LoadState") != "not-found":
        raise RuntimeError("review unit observation failed")
    return fields


class ProcessExiting(RuntimeError):
    """A sampled MainPID is already a zombie while systemd updates its state."""


def process_identity(pid: int) -> tuple[str, int, str]:
    process = Path("/proc") / str(pid)
    fields = (process / "stat").read_text().rsplit(")", 1)[1].split()
    if fields[0] == "Z":
        raise ProcessExiting("unit MainPID is a zombie")
    group = (process / "cgroup").read_text().strip()
    return (Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            int(fields[19]), group)


def stable_unit_process(unit: str) -> tuple[dict[str, str], tuple[str, int, str] | None]:
    """Resample a disappearing PID; only systemd can establish its terminal state.

    A missing /proc entry alone is not drain evidence. Retry this observation for
    at most one second, then fail closed. Surviving/new generations still pass
    through the caller's exact InvocationID/PID/start/cgroup checks.
    """
    deadline = time.monotonic() + 1.0
    while True:
        state = unit_state(unit)
        pid = int(state.get("MainPID", "0"))
        if pid == 0:
            return state, None
        try:
            return state, process_identity(pid)
        except (FileNotFoundError, ProcessLookupError, ProcessExiting) as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError("unit process transition did not settle within one second") from exc
            time.sleep(0.02)


def cgroup_state(group: str) -> dict[str, str]:
    if not group.startswith("/") or ".." in Path(group).parts:
        raise ValueError("invalid cgroup path")
    root = Path("/sys/fs/cgroup") / group.lstrip("/")
    observed = {}
    for name in ("memory.current", "memory.peak", "memory.events", "cpu.stat", "pids.current", "cgroup.events"):
        try:
            observed[name] = (root / name).read_text().strip()
        except FileNotFoundError:
            pass
    return observed


class QwenReviewObserver:
    """Follow one Director generation and every model it owns in a private DB."""

    def __init__(self, database: Path, parent: str, description: str):
        self.database = database
        self.parent = parent
        self.description = description
        self.parent_identity: tuple | None = None
        self.models: dict[str, dict] = {}
        self.cleanup: list[dict] = []

    def read_bindings(self) -> list[dict]:
        if not self.database.exists():
            return []
        info = self.database.lstat()
        if (self.database.is_symlink() or not self.database.is_file()
                or info.st_uid != os.getuid() or info.st_mode & 0o077
                or not self.database.resolve().is_relative_to(ROOT / "data/runtime")):
            raise RuntimeError("GPU review database is not a private owned regular file")
        with sqlite3.connect(self.database.resolve().as_uri() + "?mode=ro", uri=True, timeout=2) as connection:
            connection.row_factory = sqlite3.Row
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "gpu_runtime_bindings" not in tables:
                return []
            rows = connection.execute(
                "SELECT b.*,q.owner_unit,q.owner_invocation_id FROM gpu_runtime_bindings b "
                "LEFT JOIN gpu_turn_requests q ON q.owner=b.owner AND q.request_id=b.request_id "
                "ORDER BY b.fencing_token"
            ).fetchall()
        if len(rows) > 128:
            raise RuntimeError("unexpected number of model bindings in bounded review")
        return [dict(row) for row in rows]

    def capture_parent(self) -> dict:
        state, identity = stable_unit_process(self.parent)
        if identity is None:
            return state
        if (state.get("Description") != self.description
                or not re.fullmatch(r"[0-9a-f]{32}", state.get("InvocationID", ""))
                or "/swapp-gpu.slice/" not in state.get("ControlGroup", "")
                or not state["ControlGroup"].endswith("/" + self.parent)
                or identity[2] != "0::" + state["ControlGroup"]):
            raise RuntimeError("Director review principal identity mismatch")
        current = (state["InvocationID"], int(state["MainPID"]), *identity)
        if self.parent_identity is not None and current != self.parent_identity:
            raise RuntimeError("Director review generation changed")
        self.parent_identity = current
        state["cgroup"] = cgroup_state(state["ControlGroup"])
        return state

    def capture_models(self) -> list[dict]:
        observed = []
        for row in self.read_bindings():
            unit = row["unit"]
            if (row["owner"] != "lab"
                    or re.fullmatch(r"swapp-lab-gpu-turn-[0-9a-f]{32}\.service", unit) is None
                    or row["owner_unit"] != self.parent
                    or self.parent_identity is None
                    or row["owner_invocation_id"] != self.parent_identity[0]):
                raise RuntimeError("model binding belongs to an unexpected principal")
            state, process = stable_unit_process(unit)
            identity = None
            if process is not None:
                if (state.get("Description") != row["expected_description"]
                        or not re.fullmatch(r"[0-9a-f]{32}", state.get("InvocationID", ""))
                        or "/swapp-gpu.slice/" not in state.get("ControlGroup", "")
                        or not state["ControlGroup"].endswith("/" + unit)
                        or process[2] != "0::" + state["ControlGroup"]):
                    raise RuntimeError("owned model identity mismatch")
                identity = (state["InvocationID"], int(state["MainPID"]), *process)
                if row.get("invocation_id") and (
                    row["invocation_id"] != identity[0] or row["main_pid"] != identity[1]
                    or row["boot_id"] != identity[2] or row["main_start_ticks"] != identity[3]
                    or "0::" + row["control_group"] != identity[4]
                ):
                    raise RuntimeError("model generation differs from durable runtime receipt")
                prior = self.models.get(unit, {}).get("identity")
                if prior is not None and prior != identity:
                    raise RuntimeError("owned model generation changed")
                state["cgroup"] = cgroup_state(state["ControlGroup"])
            previous = self.models.get(unit, {})
            entry = {"binding": row, "unit": unit, "state": state,
                     "identity": identity or previous.get("identity")}
            if "cgroup" not in state and entry["identity"]:
                state["cgroup"] = cgroup_state(entry["identity"][4].removeprefix("0::"))
            self.models[unit] = entry
            observed.append(entry)
        return observed

    def observe(self) -> dict:
        parent = self.capture_parent()
        models = self.capture_models()
        host = snapshot()
        groups = {entry["identity"][4] for entry in self.models.values() if entry["identity"]}
        host.update({"parent": parent, "models": models})
        host["foreign_gpu_consumers"] = [item for item in host["gpu_consumers"]
            if not item["existing_display_exemption"] and item.get("cgroup") not in groups]
        host["reserves_maintained"] = (
            host["memory_available_bytes"] >= 6 * GIB
            and host["disk_available_bytes"] >= 20 * GIB
            and host["gpu"]["temperature_c"] < 83
        )
        return host

    def stop_owned_parent(self) -> None:
        state = self.capture_parent()
        if int(state.get("MainPID", "0")):
            self._stop(self.parent, state["InvocationID"])

    def stop_owned_models(self) -> None:
        # Call only after the parent is stopped, so no new model can be launched.
        if int(unit_state(self.parent).get("MainPID", "0")):
            raise RuntimeError("stop Director before model cleanup")
        for entry in self.capture_models():
            state = entry["state"]
            if int(state.get("MainPID", "0")):
                self._stop(entry["unit"], state["InvocationID"])

    def _stop(self, unit: str, invocation: str) -> None:
        # Recheck immediately before stopping; never stop by process-name pattern.
        current = unit_state(unit)
        if current.get("InvocationID") != invocation:
            raise RuntimeError("refusing to stop a different unit generation")
        result = subprocess.run(["/usr/bin/systemctl", "--user", "stop", unit],
                                capture_output=True, timeout=20, check=False)
        self.cleanup.append({"unit": unit, "invocation_id": invocation,
                             "stop_exit_code": result.returncode, "after": unit_state(unit)})
        if result.returncode:
            raise RuntimeError("owned unit stop failed")
