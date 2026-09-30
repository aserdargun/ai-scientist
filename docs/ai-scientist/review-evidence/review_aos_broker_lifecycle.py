"""Exercise the draft broker's real systemd lifecycle with CPU fixture workers.

Scheduler/SQLite, namespaces, systemd limits, process identity and drain are real.
Peer authentication, inference, and GPU observations are explicit fixtures.
The worker argv alone is replaced; no model or live AOS service is started.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import traceback
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
DRAFT = ROOT / "data/runtime/gpu-next-draft"

WORKER = r'''
import json, os, sys, time
from pathlib import Path
ready = Path(sys.argv[1]); mode = sys.argv[2]
gate = {
 "request_id": os.environ["SWAPP_GPU_REQUEST_ID"],
 "profile_id": os.environ["SWAPP_GPU_PROFILE_ID"],
 "deployment_digest": os.environ["SWAPP_GPU_DEPLOYMENT_DIGEST"],
 "nonce": os.environ["SWAPP_GPU_TURN_NONCE"],
}
ready.write_text(json.dumps(gate)); ready.chmod(0o600)
go = Path(os.environ["SWAPP_GPU_GO_FILE"])
deadline = time.monotonic()+8
while not go.exists() and time.monotonic()<deadline: time.sleep(.025)
if not go.exists() or json.loads(go.read_bytes()) != gate: raise SystemExit(19)
if mode == "failure": raise SystemExit(17)
print(json.dumps({"deployment_digest": gate["deployment_digest"],
 "prediction": {"selected_option": "x", "probabilities": {"x": 1.0, "y": 0.0}},
 "metrics": {"cpu_fixture": True, "pid": os.getpid(), "netns": os.stat("/proc/self/ns/net").st_ino}}), flush=True)
'''


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(output: Path) -> int:
    if output.exists():
        raise ValueError("refusing to overwrite existing evidence")
    os.umask(0o077)
    identity = uuid4().hex
    frozen = ROOT / "data/runtime" / ("gpu-cpu-review-" + identity)
    frozen.mkdir(mode=0o700)
    hashes = {}
    for name in ("lab_gpu_broker.py", "lab_gpu_executor.py"):
        raw = (DRAFT / name).read_bytes()
        (frozen / name).write_bytes(raw)
        hashes[name] = hashlib.sha256(raw).hexdigest()
    worker = frozen / "cpu_worker.py"
    worker.write_text(WORKER)
    sys.path[:0] = [str(frozen), str(ROOT)]
    import lab_gpu_executor as executor_module
    from lab_gpu_broker import PeerGeneration
    from lab_gpu_executor import (
        AOSProfile, BrokerOwnedTurnExecutor, ProfileRegistry,
        SystemdAOSProfileRuntime, TurnBudgets,
    )
    from lab.llm.gpu_scheduler import _read_process_identity
    from lab.llm.native_runtime import GpuSnapshot, SystemdUnitManager

    class AuthFixture:
        def still_current(self, value):
            return value == peer

        def authenticate(self, pid, uid):
            if (pid, uid) != (peer.pid, peer.uid):
                raise ValueError("fixture peer differs")
            return peer

    class GpuFixture:
        def snapshot(self):
            return GpuSnapshot({}, 0, 16384, frozenset())

    class CpuRuntime(SystemdAOSProfileRuntime):
        mode = "success"

        def _worker_argv(self, profile, manifest, ready_path):
            return [str(ROOT / ".venv/bin/python"), str(worker), str(ready_path), self.mode]

    proc = _read_process_identity(os.getpid())
    assert proc is not None
    peer = PeerGeneration(
        os.getuid(), proc.pid, proc.start_ticks, proc.boot_id,
        "swapp-aos-gpu-cpu-review-" + identity + ".service", "a" * 32,
        "/explicit-peer-fixture",
    )
    manifest = Path("/home/cachyos/aos/models/decider-manifest.json")
    pins = json.loads(manifest.read_bytes())
    profiles = {}
    for profile_id, kind in (
        ("aos.decider.turn.v1", "decider"),
        ("aos.bonsai.recovery.v1", "bonsai-recovery"),
        ("aos.bonsai.vision.v1", "bonsai-vision"),
    ):
        profiles[profile_id] = AOSProfile(
            profile_id, kind, manifest, digest(manifest), "f" * 64,
            ROOT / ".venv/bin/python", executor_module.SOURCE_ROOT,
            tuple(Path(pins[key]) for key in ("model_path", "code_path")),
            TurnBudgets(10, 10, 20, 60), response_schema_sha256="e" * 64,
        )
    units = SystemdUnitManager(memory_bytes=1024**3)
    work_root = Path(f"/run/user/{os.getuid()}") / ("swapp-aos-cpu-review-" + identity)
    runtime = CpuRuntime(
        frozen / "scheduler.sqlite3", units=units, gpu=GpuFixture(),
        work_root=work_root, memory_bytes=1024**3, task_limit=32,
    )
    executor = BrokerOwnedTurnExecutor(
        runtime.database, authenticator=AuthFixture(),
        lab_units={"aos": peer.unit, "lab": "swapp-lab-gpu-cpu-review-" + identity + ".service"},
        profiles=ProfileRegistry(profiles), runtime=runtime,
    )
    record = {
        "schema": "aos-broker-systemd-cpu-review.v1", "scope": __doc__,
        "frozen_directory": str(frozen.relative_to(ROOT)), "draft_sha256": hashes,
        "production_sha256": {name: digest(ROOT / name) for name in (
            "lab/llm/native_runtime.py", "lab/llm/gpu_scheduler.py", "lab/llm/netns_exec.py")},
        "checks": {}, "cases": [], "cleanup": [],
    }
    host_netns = os.stat("/proc/self/ns/net").st_ino
    started = time.monotonic()
    try:
        for mode in ("success", "failure"):
            runtime.mode = mode
            request_id = uuid4().hex
            payload = {"request": {"state": "CPU fixture", "question": "Choose one.",
                "options": [{"id": "x", "label": "X"}, {"id": "y", "label": "Y"}]}}
            wire = {"version": 1, "op": "infer", "request_id": request_id,
                "profile_id": "aos.decider.turn.v1", "deployment_digest": "f" * 64,
                "payload": payload}
            raw = json.dumps(wire, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            case = {"mode": mode, "request_id": request_id}
            record["cases"].append(case)
            try:
                receipt = executor.run_turn(
                    peer=peer, request_id=request_id, request_bytes=raw,
                    request_sha256=hashlib.sha256(raw).hexdigest(),
                    profile_id=wire["profile_id"], deployment_digest=wire["deployment_digest"],
                    payload=payload, deadline=time.monotonic() + 30,
                )
                case["receipt"] = dataclasses.asdict(receipt)
                record["checks"][mode + "_outcome"] = mode == "success"
                record["checks"]["private_network_namespace"] = receipt.usage["netns"] != host_netns
            except Exception as exc:
                case["error"] = {"type": type(exc).__name__, "message": str(exc),
                    "traceback": traceback.format_exc()}
                record["checks"][mode + "_outcome"] = mode == "failure"
            with sqlite3.connect(runtime.database) as db:
                db.row_factory = sqlite3.Row
                binding = db.execute("SELECT * FROM aos_gpu_child_bindings WHERE request_id=?", (request_id,)).fetchone()
                result = db.execute("SELECT * FROM aos_gpu_turn_results WHERE request_id=?", (request_id,)).fetchone()
                case["binding"] = dict(binding) if binding else None
                case["result"] = dict(result) if result else None
            record["checks"][mode + "_drained"] = bool(binding and binding["launch_state"] == "drained")
            record["checks"][mode + "_ledger"] = bool(result and result["state"] == ("completed" if mode == "success" else "failed"))
            if binding:
                unit = units.inspect(binding["unit"])
                case["unit_after"] = dataclasses.asdict(unit)
                record["checks"][mode + "_no_child"] = unit.load_state == "not-found" or (unit.main_pid == 0 and units.cgroup_empty(binding["control_group"]))
    except Exception as exc:
        record["error"] = {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
        record["checks"]["review_completed"] = False
    finally:
        with sqlite3.connect(runtime.database) as db:
            db.row_factory = sqlite3.Row
            bindings = db.execute("SELECT * FROM aos_gpu_child_bindings").fetchall()
        for row in bindings:
            cleanup = {"unit": row["unit"]}
            try:
                state = units.inspect(row["unit"])
                if state.load_state != "not-found" and state.main_pid:
                    if state.description != "SWAPP AOS GPU turn " + row["nonce"]:
                        raise RuntimeError("cleanup ownership mismatch; left untouched")
                    if row["invocation_id"] and state.invocation_id != row["invocation_id"]:
                        raise RuntimeError("cleanup generation mismatch; left untouched")
                    units.stop(row["unit"], timeout_seconds=5)
                final = units.inspect(row["unit"])
                cleanup["state"] = dataclasses.asdict(final)
                cleanup["empty"] = final.load_state == "not-found" or final.main_pid == 0
            except Exception as exc:
                cleanup["error"] = repr(exc)
            record["cleanup"].append(cleanup)
        record["checks"]["owned_process_cleanup"] = all(item.get("empty") for item in record["cleanup"])
        record["checks"]["frozen_source_stable"] = all(digest(frozen / name) == sha for name, sha in hashes.items())
        record["elapsed_seconds"] = time.monotonic() - started
        record["overall_exit_code"] = 0 if record["checks"] and all(record["checks"].values()) else 1
        output.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"checks": record["checks"], "exit_code": record["overall_exit_code"], "output": str(output)}))
    return record["overall_exit_code"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(run(parser.parse_args().output))
