"""Real owned process-tree checks for the production Scorer stop helper."""
from __future__ import annotations

from datetime import UTC, datetime
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from uuid import UUID, uuid4

from lab.scorer.supervisor import (
    SCORER_SLICE, _ensure_aggregate_slice, _job_lifecycle_lock, stop_owned_scorer_unit,
)
from review_systemd_invocation import show

ROOT = Path(__file__).resolve().parents[3]


def process_identity(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()
        cgroup = next(line[3:] for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines() if line.startswith("0::"))
        return {"pid": pid, "start_ticks": stat[19], "state": stat[0], "cgroup": cgroup}
    except (FileNotFoundError, ProcessLookupError):
        return None


def running(identity):
    actual = process_identity(identity["pid"])
    return actual is not None and actual["start_ticks"] == identity["start_ticks"] and actual["state"] != "Z"


def child_tree(marker):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    process = subprocess.Popen([sys.executable, "-c",
        "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(25)"])
    # The parent ignores TERM before spawning; the child inherits that disposition.
    marker.write_text(json.dumps({"parent": process_identity(os.getpid()), "child": process_identity(process.pid)}))
    time.sleep(25)


def cleanup_identity(identity):
    """PIDFD pins the process before checking its recorded birth and owned cgroup."""
    # The uv Python build omits Python's pidfd wrappers; the host libc exposes
    # both Linux calls. Do not fall back to a reusable numeric PID signal.
    libc = ctypes.CDLL(None, use_errno=True)
    open_pidfd = libc.pidfd_open
    open_pidfd.argtypes = [ctypes.c_int, ctypes.c_uint]
    open_pidfd.restype = ctypes.c_int
    send_signal = libc.pidfd_send_signal
    send_signal.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
    send_signal.restype = ctypes.c_int
    descriptor = open_pidfd(identity["pid"], 0)
    if descriptor < 0:
        if ctypes.get_errno() == errno.ESRCH:
            return
        raise OSError(ctypes.get_errno(), "owned fixture pidfd_open failed")
    try:
        actual = process_identity(identity["pid"])
        if actual is None or actual["start_ticks"] != identity["start_ticks"]:
            return
        if actual["cgroup"] != identity["cgroup"] or "swapp-ai-scientist-scorer-" not in actual["cgroup"]:
            raise RuntimeError("owned fixture process changed cgroup; refusing cleanup signal")
        if send_signal(descriptor, signal.SIGKILL, None, 0) < 0 and ctypes.get_errno() != errno.ESRCH:
            raise OSError(ctypes.get_errno(), "owned fixture pidfd_send_signal failed")
    finally:
        os.close(descriptor)


def launch(job_id, base, *, kill_mode="control-group"):
    unit = f"swapp-ai-scientist-scorer-{job_id.hex}.service"
    marker = base / f"{job_id.hex}.json"
    command = [
        "/usr/bin/systemd-run", "--user", "--quiet", "--collect", "--unit=" + unit,
        "--slice=" + SCORER_SLICE, "--property=Type=exec", "--property=MemoryMax=64M",
        "--property=MemorySwapMax=0", "--property=CPUQuota=25%", "--property=TasksMax=8",
        "--property=RuntimeMaxSec=30", "--property=TimeoutStopSec=1",
        "--property=KillMode=" + kill_mode, "--property=NoNewPrivileges=yes",
        "--working-directory=" + str(ROOT), str(ROOT / ".venv/bin/python"),
        str(Path(__file__).resolve()), "tree", str(marker),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
    if result.returncode != 0:
        raise RuntimeError("owned process tree could not start")
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not marker.exists():
        subprocess.run(["/usr/bin/systemctl", "--user", "stop", unit], capture_output=True, timeout=5, check=False)
        raise RuntimeError("owned process tree did not publish its identity")
    identities = json.loads(marker.read_text())
    properties = show(unit)["properties"]
    return {"job_id": str(job_id), "unit": unit, "properties": properties,
            "identities": identities, "command": command}


def stop_child(job_id, invocation, marker=None):
    if marker is not None:
        marker.write_text(str(os.getpid()))
    try:
        result = {"accepted": stop_owned_scorer_unit(UUID(job_id), invocation_id=invocation, timeout_seconds=5)}
    except RuntimeError as error:
        result = {"accepted": False, "error_type": type(error).__name__, "error": str(error)}
    print(json.dumps(result))


def main():
    source = ROOT / "lab/scorer/supervisor.py"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    record = {"checked_at": datetime.now(UTC).isoformat(), "source_sha256": {"lab/scorer/supervisor.py": digest},
              "scope": "Production stop helper, real bounded owned process trees and sentinel. Includes a deliberately nonstandard KillMode=process fixture to test surviving-child detection. No PostgreSQL claim/recovery writer or GPU/AOS acceptance.",
              "checks": {}, "fixtures": []}
    checks = record["checks"]
    _ensure_aggregate_slice()
    with tempfile.TemporaryDirectory(prefix="owned-scorer-drain-review-", dir=ROOT / "data/runtime") as temporary:
        base = Path(temporary)
        fixtures = record["fixtures"]
        waiter = None
        try:
            sentinel = launch(uuid4(), base)
            fixtures.append(sentinel)
            owned = launch(uuid4(), base)
            fixtures.append(owned)
            invocation = owned["properties"]["InvocationID"]
            job_id = UUID(owned["job_id"])
            try:
                stop_owned_scorer_unit(job_id, invocation_id=uuid4().hex, timeout_seconds=5)
                checks["wrong_invocation_rejected"] = False
            except RuntimeError:
                checks["wrong_invocation_rejected"] = True
            checks["wrong_invocation_preserves_tree"] = all(running(identity) for identity in owned["identities"].values())
            isolated_runtime = base / "isolated-runtime"
            isolated_runtime.mkdir(mode=0o700)
            environment = dict(os.environ, XDG_RUNTIME_DIR=str(isolated_runtime),
                               DBUS_SESSION_BUS_ADDRESS="unix:path=" + str(isolated_runtime / "missing-bus"))
            failure = subprocess.run([sys.executable, str(Path(__file__).resolve()), "stop", str(job_id), invocation],
                                     env=environment, capture_output=True, text=True, timeout=8, check=False)
            failure_result = json.loads(failure.stdout) if failure.returncode == 0 else None
            record["manager_failure_result"] = failure_result
            checks["manager_failure_rejected"] = failure_result is not None and not failure_result["accepted"] and failure_result.get("error_type") == "RuntimeError"
            checks["manager_failure_preserves_tree"] = all(running(identity) for identity in owned["identities"].values())

            marker = base / "stop-waiter.pid"
            with _job_lifecycle_lock(job_id):
                waiter = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "stop", str(job_id), invocation, str(marker)],
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                deadline = time.monotonic() + 3
                observed_wait = False
                while time.monotonic() < deadline and waiter.poll() is None:
                    if marker.exists():
                        locks = Path("/proc/locks").read_text().splitlines()
                        if any("->" in line and f" {waiter.pid} " in line and "FLOCK" in line for line in locks):
                            observed_wait = True
                            break
                    time.sleep(0.02)
                checks["stop_waits_on_real_lifecycle_flock"] = observed_wait
                checks["tree_alive_while_lifecycle_lock_held"] = all(running(identity) for identity in owned["identities"].values())
            stdout, _ = waiter.communicate(timeout=8)
            response = json.loads(stdout) if waiter.returncode == 0 else None
            checks["matching_invocation_stop_succeeded"] = response is not None and response["accepted"]
            checks["parent_and_child_drained"] = all(not running(identity) for identity in owned["identities"].values())
            checks["separate_sentinel_unchanged"] = all(running(identity) for identity in sentinel["identities"].values())

            orphan = launch(uuid4(), base, kill_mode="process")
            fixtures.append(orphan)
            try:
                accepted = stop_owned_scorer_unit(UUID(orphan["job_id"]), invocation_id=orphan["properties"]["InvocationID"], timeout_seconds=5)
            except RuntimeError:
                accepted = False
            child_survives = running(orphan["identities"]["child"])
            record["nonstandard_kill_mode"] = {"helper_accepted": accepted, "child_still_running": child_survives,
                                               "unit_after": show(orphan["unit"])}
            checks["surviving_child_never_reported_drained"] = not (accepted and child_survives)
        except Exception as error:
            record["error_type"] = type(error).__name__
        finally:
            if waiter is not None:
                if waiter.poll() is None:
                    waiter.terminate()
                try:
                    waiter.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    waiter.kill()
                    waiter.communicate(timeout=3)
            for fixture in fixtures:
                subprocess.run(["/usr/bin/systemctl", "--user", "stop", fixture["unit"]], capture_output=True, timeout=5, check=False)
                for identity in fixture["identities"].values():
                    cleanup_identity(identity)
            deadline = time.monotonic() + 2
            while any(running(identity) for fixture in fixtures for identity in fixture["identities"].values()) and time.monotonic() < deadline:
                time.sleep(0.02)
            checks["owned_processes_cleaned"] = all(not running(identity) for fixture in fixtures for identity in fixture["identities"].values())
    checks["private_fixture_root_removed"] = not base.exists()
    record["source_unchanged"] = digest == hashlib.sha256(source.read_bytes()).hexdigest()
    record["all_passed"] = not record.get("error_type") and len(checks) == 12 and all(checks.values()) and record["source_unchanged"]
    record["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    Path(__file__).with_name("scorer-unit-drain-review.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({key: value for key, value in record.items() if key != "fixtures"}, indent=2))
    if not record["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "tree":
        child_tree(Path(sys.argv[2]))
    elif len(sys.argv) in (4, 5) and sys.argv[1] == "stop":
        stop_child(sys.argv[2], sys.argv[3], Path(sys.argv[4]) if len(sys.argv) == 5 else None)
    else:
        main()
