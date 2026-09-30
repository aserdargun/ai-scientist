"""Opt-in CPU-only test of the pidfd proof observer, using one owned sleeper.

This does not start a Director workload, PostgreSQL, Docker, API, model or AOS.
The synthetic unit name exercises the observer's exact production name check.
It is instrumentation evidence only, never baseline or restart acceptance.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import UTC, datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[3]
HELPER = Path(__file__).with_name("baseline-proof-owned-process-033.py")


def cgroup_caps() -> dict[str, object]:
    rows = Path("/proc/self/cgroup").read_text().splitlines()
    paths = [row[3:] for row in rows if row.startswith("0::")]
    if len(paths) != 1 or ".." in Path(paths[0]).parts:
        raise RuntimeError("probe requires a bounded unified cgroup")
    group = Path("/sys/fs/cgroup") / paths[0].lstrip("/")
    memory = int((group / "memory.max").read_text())
    swap = int((group / "memory.swap.max").read_text())
    quota, period = map(int, (group / "cpu.max").read_text().split())
    tasks = int((group / "pids.max").read_text())
    if memory > 1024**3 or swap != 0 or quota / period > 0.5 or tasks > 64:
        raise RuntimeError("probe exceeds 1GiB / 50% CPU / 64 tasks / no swap")
    return {"path": paths[0], "memory_max": memory, "swap_max": swap,
            "cpu_quota": quota, "cpu_period": period, "tasks_max": tasks}


def main() -> int:
    if sys.argv[1:] != ["--execute-reviewed-observer"]:
        print("No execution; requires --execute-reviewed-observer")
        return 2
    if not callable(getattr(os, "pidfd_open", None)) or not callable(
        getattr(signal, "pidfd_send_signal", None)
    ):
        print("No launch: this interpreter lacks the required pidfd APIs", file=sys.stderr)
        return 2
    outer = cgroup_caps()
    source_hash = hashlib.sha256(HELPER.read_bytes()).hexdigest()
    spec = importlib.util.spec_from_file_location("baseline_pidfd_observer_review", HELPER)
    if spec is None or spec.loader is None:
        raise RuntimeError("observer module unavailable")
    helper = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = helper
    spec.loader.exec_module(helper)

    run_id = uuid4()
    unit = f"swapp-ai-scientist-director-dispatch-{run_id.hex}.service"
    deadline = time.monotonic() + 35
    identity = None
    descriptor = None
    created = False
    record: dict[str, object] = {
        "schema": "baseline-owned-process-observer-probe.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "fixture_run_id": str(run_id), "unit": unit, "outer_cgroup": outer,
        "helper_sha256": source_hash,
        "observer_python": sys.version,
        "observer_executable": sys.executable,
        "sleeper_executable": str(ROOT / ".venv/bin/python"),
        "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "scope": "One synthetic CPU sleeper; observer instrumentation only. No database, "
                 "Director workload, baseline score, API, Docker, model, GPU or AOS execution.",
        "cases": [],
    }
    try:
        # The UUID unit and very small independent sibling budget are fixed here.
        command = [
            "/usr/bin/systemd-run", "--user", "--collect", "--quiet",
            "--service-type=exec", f"--unit={unit}",
            "--property=MemoryMax=64M", "--property=MemorySwapMax=0",
            "--property=CPUQuota=10%", "--property=TasksMax=8",
            "--property=RuntimeMaxSec=45", "--property=TimeoutStopSec=3",
            "--property=KillMode=control-group", str(ROOT / ".venv/bin/python"),
            "-I", "-c", "import time; time.sleep(40)",
        ]
        created = True  # Conservatively retain cleanup obligation if launch times out.
        launched = subprocess.run(command, capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, timeout=5, check=False)
        record["launch_exit_code"] = launched.returncode
        if launched.returncode != 0:
            raise RuntimeError("owned observer fixture unit launch failed")
        props = helper.unit_properties(unit, deadline)
        pid = int(props["MainPID"])
        _state, start = helper.process_state(pid)
        identity = helper.DirectorIdentity(
            run_id, pid, start,
            Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            unit, props["InvocationID"], props["ControlGroup"],
        )
        descriptor = os.pidfd_open(pid, 0)
        helper.verify_identity(identity, deadline)
        record["identity"] = {**asdict(identity), "run_id": str(run_id)}
        group = Path("/sys/fs/cgroup") / identity.worker_cgroup.lstrip("/")
        memory = int((group / "memory.max").read_text())
        swap = int((group / "memory.swap.max").read_text())
        quota, period = map(int, (group / "cpu.max").read_text().split())
        tasks = int((group / "pids.max").read_text())
        if memory != 64 * 1024**2 or swap or quota / period != 0.1 or tasks != 8:
            raise AssertionError("owned sleeper resource limits differ")
        record["child_cgroup"] = {
            "memory_max": memory, "swap_max": swap, "cpu_quota": quota,
            "cpu_period": period, "tasks_max": tasks,
        }

        trace = []
        try:
            with helper.paused_director(replace(identity, worker_start_ticks=start + 1),
                                        deadline=deadline, trace=trace):
                raise AssertionError("wrong-start negative unexpectedly yielded")
        except RuntimeError:
            pass
        else:
            raise AssertionError("wrong start time was accepted")
        if trace or helper.process_state(pid)[0] in {"T", "t"}:
            raise AssertionError("negative case sent a signal or stopped the process")
        record["cases"].append({"name": "wrong_start_rejected_without_signal", "passed": True})

        for name in ("normal_scope", "caller_exception", "expired_deadline"):
            trace = []
            case_deadline = min(deadline, time.monotonic() + 0.5) if (
                name == "expired_deadline"
            ) else deadline
            caught = None
            try:
                with helper.paused_director(identity, deadline=case_deadline, trace=trace):
                    if helper.process_state(pid)[0] != "T":
                        raise AssertionError("observer yielded while child was running")
                    if name == "caller_exception":
                        raise LookupError("intentional caller failure")
                    if name == "expired_deadline":
                        time.sleep(max(0.0, case_deadline - time.monotonic()) + 0.01)
                        helper.remaining(case_deadline)
            except (LookupError, TimeoutError) as exc:
                caught = type(exc).__name__
            expected = {"normal_scope": None, "caller_exception": "LookupError",
                        "expired_deadline": "TimeoutError"}[name]
            if caught != expected:
                raise AssertionError("observer did not preserve the expected caller result")
            while helper.process_state(pid)[0] == "T":
                time.sleep(helper.remaining(deadline, 0.01))
            helper.verify_identity(identity, deadline)
            if [event["event"] for event in trace] != [
                "sigstop_sent", "pause_observed", "sigcont_sent"
            ]:
                raise AssertionError("pause/resume trace is incomplete")
            record["cases"].append({"name": name, "passed": True, "caught": caught,
                                    "trace": trace})
        record["probe_passed"] = True
    except BaseException as exc:
        record["error_type"] = type(exc).__name__
        record["probe_passed"] = False
    finally:
        cleanup_ok = not created
        if descriptor is not None:
            try:
                # Retained pidfd binds cleanup to the exact launched child, even
                # if its numeric PID or unit name has since been reused.
                try:
                    signal.pidfd_send_signal(descriptor, signal.SIGCONT)
                    signal.pidfd_send_signal(descriptor, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                cleanup_deadline = time.monotonic() + 5
                while time.monotonic() < cleanup_deadline:
                    try:
                        state, start = helper.process_state(identity.worker_pid)
                    except FileNotFoundError:
                        cleanup_ok = True
                        break
                    if start != identity.worker_start_ticks or state in {"Z", "X", "x"}:
                        cleanup_ok = True
                        break
                    time.sleep(0.02)
            except BaseException as exc:
                record["cleanup_error_type"] = type(exc).__name__
            finally:
                os.close(descriptor)
        if identity is not None:
            group = Path("/sys/fs/cgroup") / identity.worker_cgroup.lstrip("/")
            # Process exit and cgroup population accounting may settle separately.
            group_deadline = time.monotonic() + 2
            while True:
                try:
                    populated = "populated 1" in (group / "cgroup.events").read_text()
                except FileNotFoundError:
                    populated = False
                if not populated or time.monotonic() >= group_deadline:
                    break
                time.sleep(0.02)
            cleanup_ok = cleanup_ok and not populated
            record["owned_cgroup_populated_after_cleanup"] = populated
        record["owned_sleeper_drained"] = cleanup_ok
        record["helper_unchanged"] = hashlib.sha256(HELPER.read_bytes()).hexdigest() == source_hash
        record["exit_code"] = int(not (record.get("probe_passed") and cleanup_ok
                                       and record["helper_unchanged"]))
        evidence = HELPER.with_name(f"baseline-owned-process-probe-{run_id.hex[:12]}.json")
        evidence.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        print(json.dumps({"evidence": str(evidence.relative_to(ROOT)),
                          "exit_code": record["exit_code"],
                          "probe_passed": record.get("probe_passed"),
                          "owned_sleeper_drained": cleanup_ok}), flush=True)
    return int(record["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
