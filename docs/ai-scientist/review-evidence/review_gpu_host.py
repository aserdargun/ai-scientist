"""Read-only host snapshot before a separately bounded native model experiment.

This is review instrumentation, not GPU admission or a coexistence acceptance.
Call again immediately before activation; a snapshot cannot prevent a later
uncoordinated consumer from appearing. No process, unit, cache or model is changed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[3]
GIB = 1024**3
DISPLAY_EXECUTABLE = "/usr/bin/kwin_wayland"


def command(arguments: list[str]) -> str:
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=15, check=False)
    if result.returncode:
        raise RuntimeError(f"{arguments[0]} exited {result.returncode}: {result.stderr[:1000]}")
    return result.stdout.strip()


def snapshot() -> dict[str, object]:
    """Capture physical resources and full Linux identity for visible GPU users."""
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        memory[key] = int(value.strip().split()[0]) * 1024
    gpu_output = command([
        "nvidia-smi",
        "--query-gpu=uuid,name,memory.total,memory.used,utilization.gpu,temperature.gpu,power.draw",
        "--format=csv,noheader,nounits",
    ])
    gpu_rows = list(csv.reader(io.StringIO(gpu_output), skipinitialspace=True))
    if len(gpu_rows) != 1:
        raise RuntimeError("this review expects exactly one GPU")
    row = gpu_rows[0]
    gpu = dict(zip(
        ("uuid", "name", "total_mib", "used_mib", "utilization_percent", "temperature_c", "power_w"),
        row,
        strict=True,
    ))
    for key in ("total_mib", "used_mib", "utilization_percent", "temperature_c", "power_w"):
        gpu[key] = float(gpu[key])
    consumers = []
    process_output = command([
        "nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
        "--format=csv,noheader,nounits",
    ])
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    for record in csv.reader(io.StringIO(process_output), skipinitialspace=True):
        if not record:
            continue
        pid, process_name, used_mib = record
        process = Path("/proc") / pid
        try:
            fields = (process / "stat").read_text().rsplit(")", 1)[1].split()
            identity = {
                "start_ticks": int(fields[19]),
                "boot_id": boot_id,
                "uid": process.stat().st_uid,
                "comm": (process / "comm").read_text().strip(),
                "cgroup": (process / "cgroup").read_text().strip(),
                "identity_readable": True,
            }
        except (OSError, ValueError, IndexError):
            # A disappearing or unreadable identity never counts as an exemption.
            identity = {"identity_readable": False}
        try:
            identity["executable"] = str((process / "exe").resolve(strict=True))
        except OSError:
            identity["executable"] = None
        # The known login-screen compositor runs as the system plasmalogin
        # account; /proc/PID/exe is intentionally inaccessible to this user.
        # Exempt that narrow existing display identity, never arbitrary Python.
        try:
            login_uid = pwd.getpwnam("plasmalogin").pw_uid
        except KeyError:
            login_uid = None
        login_display = (
            login_uid is not None
            and identity.get("uid") == login_uid
            and identity.get("cgroup") == (
                f"0::/user.slice/user-{login_uid}.slice/user@{login_uid}.service/"
                "session.slice/plasma-login-kwin_wayland.service"
            )
        )
        user_display = (
            identity.get("executable") == DISPLAY_EXECUTABLE
            and identity.get("uid") == os.getuid()
        )
        consumers.append({
            "pid": int(pid),
            "process_name": process_name,
            "used_mib": float(used_mib),
            **identity,
            "existing_display_exemption": (
                identity.get("identity_readable") is True
                and identity.get("comm") == "kwin_wayland"
                and (login_display or user_display)
                and process_name == DISPLAY_EXECUTABLE
            ),
        })
    physical_cores = set()
    for cpu in os.sched_getaffinity(0):
        topology = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology")
        physical_cores.add((
            (topology / "physical_package_id").read_text().strip(),
            (topology / "core_id").read_text().strip(),
        ))
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "monotonic_seconds": time.monotonic(),
        "boot_id": boot_id,
        "memory_total_bytes": memory["MemTotal"],
        "memory_available_bytes": memory["MemAvailable"],
        "swap_used_bytes": memory["SwapTotal"] - memory["SwapFree"],
        "swap_counts_as_capacity": False,
        "disk_available_bytes": shutil.disk_usage(ROOT).free,
        "logical_cpus_in_affinity": len(os.sched_getaffinity(0)),
        "physical_cores_in_affinity": len(physical_cores),
        "gpu": gpu,
        "gpu_consumers": consumers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    observed = snapshot()
    checks = {
        "disk_reserve_20_gib": observed["disk_available_bytes"] >= 20 * GIB,
        "single_model_cap_10_gib_plus_host_reserve_6_gib": (
            observed["memory_available_bytes"] >= 16 * GIB
        ),
        "no_non_display_gpu_consumer": all(
            item["existing_display_exemption"] for item in observed["gpu_consumers"]
        ),
        "gpu_temperature_below_83_c": observed["gpu"]["temperature_c"] < 83,
    }
    record = {
        "schema": "native-gpu-host-preflight-review.v1",
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "observation": observed,
        "checks": checks,
        "scope": "Point-in-time prerequisite for one native model with a proposed 10 GiB cgroup RAM cap; no model started.",
        "limitations": [
            "This snapshot is not an atomic GPU reservation or protection against a later consumer.",
            "The runtime must enforce its own process/cgroup, CPU, RAM, disk, activation and inference bounds.",
            "The 10 GiB cap is a proposed first experiment limit, not measured model host memory use.",
            "Combined AOS/Lab/sandbox/Scorer/API/DB capacity and runtime behavior are not tested here.",
        ],
        "passed": all(checks.values()),
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(record, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"output": str(arguments.output), "checks": checks, "passed": record["passed"]}))
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
