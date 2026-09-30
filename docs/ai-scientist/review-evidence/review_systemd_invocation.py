"""Observe systemd invocation reuse and bus failures with two tiny owned services."""
from __future__ import annotations

from datetime import UTC, datetime
import json
import hashlib
import os
from pathlib import Path
import subprocess
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
PROPERTIES = ("LoadState", "ActiveState", "SubState", "InvocationID", "ControlGroup", "MainPID")


def show(unit, *, bad_bus=False):
    environment = dict(os.environ)
    if bad_bus:
        unavailable = "/run/user/" + str(os.getuid()) + "/absent-owned-review-" + uuid4().hex
        environment["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + unavailable + "/bus"
        # systemctl may prefer the private manager socket under XDG_RUNTIME_DIR;
        # changing only DBUS_SESSION_BUS_ADDRESS does not break that connection.
        environment["XDG_RUNTIME_DIR"] = unavailable
    result = subprocess.run(
        ["/usr/bin/systemctl", "--user", "show", unit, *["--property=" + p for p in PROPERTIES]],
        capture_output=True, text=True, env=environment, timeout=5, check=False,
    )
    return {"exit_code": result.returncode,
            "properties": dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line),
            "stderr_present": bool(result.stderr.strip())}


def start(unit):
    command = ["/usr/bin/systemd-run", "--user", "--quiet", "--collect", "--unit=" + unit,
               "--property=Type=exec", "--property=MemoryMax=64M", "--property=MemorySwapMax=0",
               "--property=CPUQuota=25%", "--property=TasksMax=8", "--property=RuntimeMaxSec=15",
               "--property=TimeoutStopSec=1", "--property=KillMode=control-group",
               "--property=NoNewPrivileges=yes", "/usr/bin/sleep", "12"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
    if result.returncode != 0:
        raise RuntimeError("owned systemd capability fixture did not start")
    return command


def stop(unit):
    return subprocess.run(["/usr/bin/systemctl", "--user", "stop", unit], capture_output=True,
                          text=True, timeout=5, check=False).returncode


def empty_cgroup(path):
    if not path.startswith("/user.slice/") or ".." in Path(path).parts:
        return False
    cgroup = Path("/sys/fs/cgroup") / path.lstrip("/")
    if not cgroup.exists():
        return True
    return "populated 0" in (cgroup / "cgroup.events").read_text().splitlines() and (cgroup / "pids.current").read_text().strip() == "0"


def main():
    unit = "swapp-ai-scientist-invocation-review-" + uuid4().hex + ".service"
    sentinel = "swapp-ai-scientist-invocation-sentinel-" + uuid4().hex + ".service"
    record = {"checked_at": datetime.now(UTC).isoformat(), "checks": {}, "observations": {},
              "scope": "Host systemd metadata capability only: bounded owned sleep services, same unit name reused, deliberately unavailable session bus and runtime directory in one child command environment. No DB, production recovery helper, GPU or AOS acceptance."}
    checks, observations = record["checks"], record["observations"]
    groups = []
    try:
        observations["initial_absent"] = show(unit)
        checks["unknown_unit_explicitly_not_found"] = (
            observations["initial_absent"]["properties"].get("LoadState") == "not-found"
            and observations["initial_absent"]["properties"].get("ActiveState") == "inactive"
        )
        record["sentinel_command"] = start(sentinel)
        sentinel_before = show(sentinel)["properties"]
        groups.append(sentinel_before["ControlGroup"])
        record["worker_command"] = start(unit)
        first = show(unit)["properties"]
        observations["first_invocation"] = first
        groups.append(first["ControlGroup"])
        invocation = first["InvocationID"]
        checks["active_invocation_is_32_hex"] = (
            first["LoadState"] == "loaded" and first["ActiveState"] == "active"
            and len(invocation) == 32 and all(ch in "0123456789abcdef" for ch in invocation)
            and int(first["MainPID"]) > 0
        )
        pid = first["MainPID"]
        cgroups = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
        checks["actual_pid_belongs_to_reported_cgroup"] = "0::" + first["ControlGroup"] in cgroups
        observations["unavailable_bus"] = show(unit, bad_bus=True)
        checks["bus_failure_is_distinct_from_explicit_absence"] = (
            observations["unavailable_bus"]["exit_code"] != 0
            and observations["unavailable_bus"]["properties"].get("LoadState") != "not-found"
        )
        checks["first_owned_stop_succeeded"] = stop(unit) == 0 and empty_cgroup(first["ControlGroup"])
        observations["after_first_stop"] = show(unit)
        start(unit)
        second = show(unit)["properties"]
        observations["second_invocation"] = second
        groups.append(second["ControlGroup"])
        checks["same_name_gets_new_invocation"] = second["InvocationID"] != first["InvocationID"]
        checks["same_name_cgroup_path_can_be_reused"] = second["ControlGroup"] == first["ControlGroup"]
        after = show(sentinel)["properties"]
        checks["sentinel_survives_other_unit_stop_and_restart"] = (
            after["InvocationID"] == sentinel_before["InvocationID"]
            and after["MainPID"] == sentinel_before["MainPID"] and after["ActiveState"] == "active"
        )
    except Exception as error:
        record["error_type"] = type(error).__name__
    finally:
        stop(unit)
        stop(sentinel)
        observations["final_units"] = [show(item) for item in (unit, sentinel)]
        checks["owned_units_inactive"] = all(item["properties"].get("ActiveState") == "inactive"
                                               and item["properties"].get("MainPID") == "0"
                                               for item in observations["final_units"])
        checks["owned_cgroups_drained"] = all(empty_cgroup(group) for group in groups)
    record["all_passed"] = not record.get("error_type") and len(checks) == 10 and all(checks.values())
    record["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    Path(__file__).with_name("systemd-invocation-capability.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))
    if not record["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
