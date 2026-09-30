"""Read-only host diagnostics with bounded fixed NVIDIA telemetry query."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _memory() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            name, rest = line.split(":", maxsplit=1)
            if name in {"MemTotal", "MemAvailable"}:
                values[name] = int(rest.strip().split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return {"total_bytes": 0, "available_bytes": 0}
    return {
        "total_bytes": values.get("MemTotal", 0),
        "available_bytes": values.get("MemAvailable", 0),
    }


def _gpu() -> dict[str, Any]:
    unavailable = {
        "available": False,
        "name": None,
        "total_mib": None,
        "used_mib": None,
        "utilization_percent": None,
        "temperature_c": None,
        "reason": "nvidia-smi unavailable",
    }
    binary = shutil.which("nvidia-smi")
    if binary is None:
        return unavailable
    try:
        result = subprocess.run(
            [
                binary,
                "--query-gpu=name,memory.total,memory.used,utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {**unavailable, "reason": f"nvidia-smi query failed: {type(exc).__name__}"}
    if result.returncode:
        return {**unavailable, "reason": f"nvidia-smi exited {result.returncode}"}
    fields = (
        result.stdout.strip().splitlines()[0].split(",", maxsplit=4)
        if result.stdout.strip()
        else []
    )
    if len(fields) != 5:
        return {**unavailable, "reason": "nvidia-smi returned an unexpected response"}
    try:
        name = fields[0].strip()
        return {
            "available": True,
            "name": name,
            "total_mib": int(fields[1]),
            "used_mib": int(fields[2]),
            "utilization_percent": int(fields[3]),
            "temperature_c": int(fields[4]),
            "reason": None,
        }
    except ValueError:
        return {**unavailable, "reason": "nvidia-smi returned nonnumeric telemetry"}


def system_info() -> dict[str, Any]:
    try:
        disk_usage = shutil.disk_usage(ROOT)
        disk = {"total_bytes": disk_usage.total, "free_bytes": disk_usage.free}
    except OSError:
        disk = {"total_bytes": 0, "free_bytes": 0}
    try:
        load_1m = os.getloadavg()[0]
    except (AttributeError, OSError):
        load_1m = 0.0
    return {
        "memory": _memory(),
        "disk": disk,
        "cpu": {"logical_count": os.cpu_count() or 0, "load_1m": load_1m},
        "gpu": _gpu(),
    }
