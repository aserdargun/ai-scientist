"""Two real systemd CPU clients using production GPU principal arbitration.

No CUDA/model is loaded. The drain observer checks only a short owned CPU child;
it is not the production GPU runtime observer or proof of VRAM reclamation.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from lab.llm.gpu_scheduler import (
    LeaseConflict, SharedGpuScheduler, SystemdPrincipalResolver, boottime,
)

SOURCE = ROOT / "lab/llm/gpu_scheduler.py"
PYTHON = ROOT / ".venv/bin/python"
SCRIPT = Path(__file__).resolve()
TURNS_PER_OWNER = 10


def run(arguments: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, capture_output=True, text=True, timeout=15, check=check)


def units_for(directory: Path) -> dict[str, str]:
    return {owner: f"swapp-{owner}-gpu-review-{directory.name}.service" for owner in ("aos", "lab")}


def unit_status(unit: str) -> dict[str, str]:
    properties = (
        "LoadState", "ActiveState", "SubState", "MainPID", "InvocationID", "ControlGroup",
        "ExecMainCode", "ExecMainStatus", "Result", "MemoryMax", "MemoryPeak",
        "MemorySwapMax", "CPUQuotaPerSecUSec", "TasksMax",
    )
    result = run([
        "systemctl", "--user", "show", "--no-pager",
        *[f"--property={prop}" for prop in properties], unit,
    ])
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def worker(directory: Path, owner: str) -> int:
    events = []
    children = {}
    output = directory / (owner + "-result.json")
    source_sha = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    resolver = SystemdPrincipalResolver(units_for(directory))

    def drained(lease) -> bool:
        child = children.get(lease.fencing_token)
        return child is not None and child.poll() == 0

    scheduler = SharedGpuScheduler(
        directory / "queue.sqlite", principal_resolver=resolver, drain_verifier=drained,
        max_activation_seconds=10, max_inference_seconds=5, max_total_seconds=15,
        queue_timeout_seconds=60,
    )
    receipt = resolver.resolve(owner)
    error = None
    try:
        scheduler.submit(owner, f"{owner}-0", b"cpu-turn-0")
        (directory / (owner + "-ready")).write_text("ready\n")
        deadline = boottime() + 35
        while not all((directory / (lane + "-ready")).exists() for lane in ("aos", "lab")):
            if boottime() >= deadline:
                raise TimeoutError("peer did not submit its initial ticket")
            time.sleep(0.01)
        for index in range(TURNS_PER_OWNER):
            lease = None
            while lease is None:
                if boottime() >= deadline:
                    raise TimeoutError("fair queue did not grant the pending ticket")
                lease = scheduler.try_acquire(owner, f"{owner}-{index}")
                if lease is None:
                    time.sleep(0.01)
            acquired = boottime()
            if index + 1 < TURNS_PER_OWNER:
                scheduler.submit(owner, f"{owner}-{index + 1}", f"cpu-turn-{index + 1}".encode())
            # A real owned process remains alive across the negative drain probe.
            child = subprocess.Popen(
                [str(PYTHON), "-c", "import sys; sys.stdin.buffer.read(1)"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            children[lease.fencing_token] = child
            ready = scheduler.mark_ready(lease)
            if ready is None:
                raise RuntimeError("turn expired before the CPU child was ready")
            try:
                scheduler.release(ready)
            except LeaseConflict:
                blocked_live_release = child.poll() is None
            else:
                blocked_live_release = False
            child.communicate(b"x", timeout=3)
            child_exited = boottime()
            if child.returncode != 0 or not blocked_live_release:
                raise RuntimeError("live drain guard or owned CPU child failed")
            scheduler.release(ready)
            events.append({
                "owner": owner, "request_id": lease.request_id, "token": lease.fencing_token,
                "acquired_at": acquired, "child_pid": child.pid, "child_exit_code": child.returncode,
                "child_exited_at": child_exited, "released_at": boottime(),
                "release_while_child_alive_rejected": blocked_live_release,
            })
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        for child in children.values():
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)
        output.write_text(json.dumps({
            "receipt": asdict(receipt), "events": events, "error": error,
            "source_sha256": source_sha,
            "source_unchanged": hashlib.sha256(SOURCE.read_bytes()).hexdigest() == source_sha,
        }, indent=2) + "\n")
    return 0 if error is None else 1


def review(output: Path) -> int:
    if output.exists():
        raise RuntimeError("refusing to overwrite existing review evidence")
    directory = ROOT / "data/runtime/gpu-systemd-review" / uuid.uuid4().hex
    directory.mkdir(parents=True, mode=0o700)
    units = units_for(directory)
    source_sha = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    record = {
        "schema": "gpu-systemd-arbitration-review.v1",
        "source_sha256": source_sha,
        "script_sha256": hashlib.sha256(SCRIPT.read_bytes()).hexdigest(),
        "runtime_directory": str(directory.relative_to(ROOT)),
        "scope": "Two real bounded systemd CPU services, production principal resolver and SQLite scheduler, real owned CPU child drain observer; no GPU/model/CUDA.",
        "limitations": [
            "CPU child death is not proof of CUDA drain or VRAM release.",
            "Runtime GPU observer, model activation, AOS hooks and combined hardware acceptance remain open.",
        ],
        "checks": {},
    }
    started = []
    try:
        for owner, unit in units.items():
            if unit_status(unit).get("LoadState") != "not-found":
                raise RuntimeError("refusing an existing service name")
            run([
                "systemd-run", "--user", "--quiet", f"--unit={unit}", "--slice=swapp-gpu.slice",
                "--service-type=exec", "--property=RemainAfterExit=yes",
                "--property=MemoryMax=256M", "--property=MemorySwapMax=0",
                "--property=CPUQuota=50%", "--property=TasksMax=16",
                "--property=RuntimeMaxSec=45", "--property=KillMode=control-group",
                "--property=TimeoutStopSec=3", f"--property=WorkingDirectory={ROOT}",
                f"--property=StandardOutput=append:{directory / (owner + '.log')}",
                f"--property=StandardError=append:{directory / (owner + '.log')}",
                str(PYTHON), str(SCRIPT), "--worker", owner, "--directory", str(directory),
            ])
            started.append(unit)
        # This reviewer is outside both mapped units and must not claim either lane.
        resolver = SystemdPrincipalResolver(units)
        for owner in units:
            try:
                resolver.resolve(owner)
            except LeaseConflict:
                record["checks"][f"outside_process_cannot_claim_{owner}_principal"] = True
            else:
                record["checks"][f"outside_process_cannot_claim_{owner}_principal"] = False
        deadline = time.monotonic() + 40
        states = {}
        while time.monotonic() < deadline:
            states = {owner: unit_status(unit) for owner, unit in units.items()}
            if all(state.get("MainPID") == "0" for state in states.values()):
                break
            time.sleep(0.2)
        record["unit_states"] = states
        results = {
            owner: json.loads((directory / (owner + "-result.json")).read_text())
            for owner in units
        }
        record["workers"] = results
        events = sorted(
            (event for result in results.values() for event in result["events"]),
            key=lambda event: event["token"],
        )
        record["turn_order"] = [event["owner"] for event in events]
        record["checks"].update({
            "both_services_exited_zero": all(
                state.get("MainPID") == "0" and state.get("ExecMainStatus") == "0"
                and state.get("Result") == "success" for state in states.values()
            ),
            "both_services_completed_ten_turns": all(
                len(result["events"]) == TURNS_PER_OWNER and result["error"] is None
                for result in results.values()
            ),
            "twenty_strictly_alternating_principal_turns": record["turn_order"] == ["aos", "lab"] * 10,
            "strictly_increasing_fencing_tokens": [event["token"] for event in events] == list(range(1, 21)),
            "live_child_release_rejected_every_turn": len(events) == 20 and all(
                event["release_while_child_alive_rejected"] for event in events
            ),
            "previous_child_dead_before_next_lease": all(
                left["child_exited_at"] <= right["acquired_at"]
                for left, right in zip(events, events[1:])
            ),
            "measured_same_production_source": all(
                result["source_sha256"] == source_sha and result["source_unchanged"]
                for result in results.values()
            ) and hashlib.sha256(SOURCE.read_bytes()).hexdigest() == source_sha,
        })
        with sqlite3.connect(directory / "queue.sqlite") as connection:
            terminal = dict(connection.execute("SELECT state,count(*) FROM gpu_turn_requests GROUP BY state"))
            owner = connection.execute("SELECT active_owner FROM gpu_turn_state WHERE singleton=1").fetchone()[0]
        record["terminal_ticket_counts"] = terminal
        record["checks"]["all_twenty_tickets_terminal_and_no_active_owner"] = terminal == {"done": 20} and owner is None
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        for unit in started:
            run(["systemctl", "--user", "stop", unit], check=False)
        record["cleanup_units"] = {unit: unit_status(unit) for unit in started}
        record["checks"]["owned_units_stopped"] = bool(started) and all(
            state.get("MainPID", "0") == "0" and state.get("ActiveState") in {"inactive", "failed"}
            for state in record["cleanup_units"].values()
        )
    record["passed"] = "error" not in record and all(record["checks"].values())
    output.write_text(json.dumps(record, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"output": str(output), "checks": record["checks"], "error": record.get("error"), "passed": record["passed"]}))
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=("aos", "lab"))
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.worker:
        raise SystemExit(worker(args.directory, args.worker))
    if args.output is None:
        parser.error("--output is required for the parent review")
    raise SystemExit(review(args.output))
