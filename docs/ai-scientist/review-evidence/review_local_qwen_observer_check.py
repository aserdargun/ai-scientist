"""Verify observer ownership with real short CPU services and a synthetic DB.

No CUDA, model, Lab run or AOS service is involved. The synthetic runtime rows
test only the review observer's ability to distinguish service generations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
from uuid import uuid4

from review_local_qwen_observer import ROOT, QwenReviewObserver, unit_state


def main(output: Path) -> int:
    if output.exists():
        raise ValueError("refusing to overwrite observer evidence")
    os.umask(0o077)
    identity = uuid4().hex
    directory = ROOT / "data/runtime/local-qwen-observer-check" / identity
    directory.mkdir(mode=0o700, parents=True)
    database = directory / "observer.sqlite3"
    parent = f"swapp-lab-gpu-review-{identity}.service"
    description = f"SWAPP CPU observer fixture {identity}"
    model_units = [f"swapp-lab-gpu-turn-{uuid4().hex}.service" for _ in range(2)]
    unrelated = f"swapp-lab-gpu-review-{uuid4().hex}.service"
    specifications = [(parent, description), *[(unit, f"SWAPP CPU fixture {unit}") for unit in model_units],
                      (unrelated, f"SWAPP observer excluded fixture {identity}")]
    observer = QwenReviewObserver(database, parent, description)
    processes = []
    record = {"schema": "local-qwen-observer-cpu-review.v1", "checks": {},
              "scope": "Actual bounded CPU sleep services, synthetic binding rows; "
                       "review-observer ownership only. No model, GPU, AOS or research acceptance.",
              "commands": [], "cleanup": []}
    sources = [Path(__file__), Path(__file__).with_name("review_local_qwen_observer.py")]
    record["source_sha256"] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    try:
        for unit, expected_description in specifications:
            command = [
                "/usr/bin/systemd-run", "--user", "--quiet", "--wait", "--pipe", "--collect",
                f"--unit={unit}", "--slice=swapp-gpu.slice", f"--description={expected_description}",
                "--property=MemoryMax=32M", "--property=MemorySwapMax=0", "--property=CPUQuota=1%",
                "--property=TasksMax=4", "--property=RuntimeMaxSec=90", "--property=TimeoutStopSec=3",
                "--property=KillMode=control-group", "/usr/bin/sleep", "60",
            ]
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            processes.append((unit, expected_description, process, None))
            record["commands"].append(command)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = unit_state(unit)
                if int(state.get("MainPID", "0")) and state.get("ActiveState") == "active":
                    break
                if process.poll() is not None:
                    raise RuntimeError("CPU fixture exited before readiness")
                time.sleep(0.05)
            else:
                raise TimeoutError("CPU fixture did not become active")
            if state["Description"] != expected_description:
                raise RuntimeError("fixture description differs")
            processes[-1] = (unit, expected_description, process, state["InvocationID"])
        parent_state = observer.capture_parent()
        with sqlite3.connect(database) as connection:
            connection.executescript(
                "CREATE TABLE gpu_runtime_bindings(owner TEXT,request_id TEXT,fencing_token INTEGER,"
                "unit TEXT,expected_description TEXT,invocation_id TEXT,main_pid INTEGER,boot_id TEXT,"
                "main_start_ticks INTEGER,control_group TEXT);"
                "CREATE TABLE gpu_turn_requests(owner TEXT,request_id TEXT,owner_unit TEXT,owner_invocation_id TEXT);"
            )
            for index, unit in enumerate(model_units):
                request_id = f"synthetic-observer-{index}"
                connection.execute("INSERT INTO gpu_runtime_bindings VALUES(?,?,?,?,?,NULL,NULL,NULL,NULL,NULL)",
                                   ("lab", request_id, index + 1, unit, f"SWAPP CPU fixture {unit}"))
                connection.execute("INSERT INTO gpu_turn_requests VALUES(?,?,?,?)",
                                   ("lab", request_id, parent, parent_state["InvocationID"]))
        captured = observer.capture_models()
        record["checks"]["two_distinct_model_generations_observed"] = (
            len(captured) == 2 and all(item["identity"] for item in captured)
            and captured[0]["identity"] != captured[1]["identity"]
        )
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE gpu_turn_requests SET owner_invocation_id=?", ("0" * 32,))
        try:
            observer.capture_models()
        except RuntimeError:
            record["checks"]["different_principal_generation_rejected"] = True
        else:
            record["checks"]["different_principal_generation_rejected"] = False
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE gpu_turn_requests SET owner_invocation_id=?", (parent_state["InvocationID"],))
        observer.stop_owned_parent()
        observer.stop_owned_models()
        record["observer_cleanup"] = observer.cleanup
        record["checks"]["only_registered_generations_stopped"] = (
            {entry["unit"] for entry in observer.cleanup} == {parent, *model_units}
            and all(entry["stop_exit_code"] == 0 for entry in observer.cleanup)
        )
        excluded = unit_state(unrelated)
        record["checks"]["unregistered_service_still_running"] = (
            excluded.get("ActiveState") == "active" and int(excluded.get("MainPID", "0")) > 0
            and excluded.get("InvocationID") == processes[-1][3]
        )
        record["checks"]["registered_services_inactive"] = all(
            int(unit_state(unit).get("MainPID", "0")) == 0 for unit in (parent, *model_units)
        )
    except Exception as error:
        record["error_type"] = type(error).__name__
    finally:
        for unit, expected_description, process, invocation in reversed(processes):
            current = unit_state(unit)
            if int(current.get("MainPID", "0")):
                if current.get("Description") != expected_description or current.get("InvocationID") != invocation:
                    record["cleanup"].append({"unit": unit, "unknown_generation_untouched": True})
                    continue
                result = subprocess.run(["/usr/bin/systemctl", "--user", "stop", unit],
                                        capture_output=True, timeout=10, check=False)
                record["cleanup"].append({"unit": unit, "stop_exit_code": result.returncode})
            process.wait(timeout=10)
        record["checks"]["sources_unchanged"] = all(
            hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
            for name, digest in record["source_sha256"].items()
        )
        record["checks"]["all_cpu_fixtures_finally_inactive"] = all(
            int(unit_state(unit).get("MainPID", "0")) == 0 for unit, _ in specifications
        )
        record["passed"] = not record.get("error_type") and len(record["checks"]) == 7 and all(record["checks"].values())
        with output.open("x") as stream:
            json.dump(record, stream, indent=2)
            stream.write("\n")
    print(json.dumps({"passed": record["passed"], "checks": record["checks"], "error_type": record.get("error_type")}))
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    raise SystemExit(main(parser.parse_args().output))
