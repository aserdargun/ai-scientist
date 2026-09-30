"""Observe one real, bounded native Qwen turn; never activate live AOS.

This independent review invokes the production doctor, with its real systemd
principal and private SQLite state. It measures startup as well as inference.
Only this review's exact service generations may be stopped by its observer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

from review_gpu_host import snapshot

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from lab.llm.native_runtime import SystemdUnitManager

GIB = 1024**3
SOURCES = (
    "lab/llm/native_runtime.py", "lab/llm/netns_exec.py",
    "lab/llm/gpu_scheduler.py", "harness/fingerprint.py", "harness/VERSION",
    "docs/ai-scientist/review-evidence/vllm-cuda132-resolved.lock",
)


def hashes() -> dict[str, str]:
    return {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in SOURCES}


def runtime_packages() -> dict[str, str]:
    query = ('import importlib.metadata as m,json; '
             'print(json.dumps({d.metadata["Name"].lower().replace("_","-"):d.version '
             'for d in m.distributions()}))')
    result = subprocess.run(
        [str(ROOT / "data/runtime/vllm/.venv/bin/python"), "-c", query],
        check=True, capture_output=True, text=True, timeout=10,
    )
    return json.loads(result.stdout)


def show(unit: str) -> dict[str, str]:
    result = subprocess.run(
        ["/usr/bin/systemctl", "--user", "show", unit, "--no-pager",
         "-p", "LoadState", "-p", "ActiveState", "-p", "InvocationID",
         "-p", "MainPID", "-p", "ControlGroup", "-p", "Description",
         "-p", "Result", "-p", "ExecMainStatus"],
        capture_output=True, text=True, timeout=5, check=False,
    )
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def binding(database: Path) -> dict | None:
    if not database.exists():
        return None
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=1) as connection:
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute("SELECT * FROM gpu_runtime_bindings").fetchone()
        except sqlite3.OperationalError:
            return None
        return dict(row) if row else None


def cgroup_observation(group: str) -> dict:
    root = Path("/sys/fs/cgroup") / group.lstrip("/")
    result = {}
    for name in ("memory.current", "memory.peak", "memory.events", "cpu.stat", "pids.current"):
        try:
            result[name] = (root / name).read_text().strip()
        except FileNotFoundError:
            pass
    return result


def main(profile: str, output: Path) -> int:
    if output.exists():
        raise ValueError("review output already exists")
    request = uuid4().hex
    parent = f"swapp-lab-gpu-review-{request}.service"
    description = f"SWAPP isolated Qwen review {request}"
    private = ROOT / "data/runtime/gpu" / f"native-review-{request}"
    private.mkdir(parents=True, mode=0o700)
    database = private / "arbiter.sqlite3"
    os.umask(0o077)
    record = {
        "schema": "native-real-qwen-doctor-review.v1", "profile": profile,
        "request_id": request, "sources_before": hashes(),
        "runtime_packages_before": runtime_packages(),
        "review_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "private_directory": str(private.relative_to(ROOT)), "samples": [],
        "scope": "One fixed real local Qwen prompt; no AOS model, research run, training or full capacity acceptance.",
        "cleanup": [], "checks": {},
    }
    units = SystemdUnitManager()
    process = None
    parent_invocation = ""
    model_invocation = ""
    model_group = ""
    row = None
    started = time.monotonic()
    command = [
        "/usr/bin/systemd-run", "--user", "--wait", "--pipe", "--collect",
        f"--unit={parent}", "--slice=swapp-gpu.slice", f"--description={description}",
        "--property=MemoryMax=1G", "--property=MemorySwapMax=0",
        "--property=CPUQuota=50%", "--property=TasksMax=16",
        "--property=RuntimeMaxSec=720", "--property=KillMode=control-group",
        "--property=TimeoutStopSec=10", "--property=NoNewPrivileges=yes",
        "--property=UMask=0077",
        f"--working-directory={ROOT}", f"--setenv=SWAPP_LAB_GPU_UNIT={parent}",
        f"--setenv=SWAPP_AOS_GPU_UNIT=swapp-aos-gpu-review-{request}.service",
        f"--setenv=SWAPP_GPU_RUNTIME_DB={database}",
        "--setenv=OPENBLAS_NUM_THREADS=1", "--setenv=OMP_NUM_THREADS=1",
        "--setenv=MKL_NUM_THREADS=1", str(ROOT / ".venv/bin/python"),
        "-m", "lab.llm.native_runtime", "--request-id", request, "--profile", profile,
    ]
    record["command"] = command
    try:
        units.ensure_slice()
        pre = snapshot()
        record["preflight"] = pre
        if (pre["memory_available_bytes"] < 16 * GIB
                or pre["disk_available_bytes"] < 20 * GIB
                or pre["gpu"]["temperature_c"] >= 83
                or any(not p["existing_display_exemption"] for p in pre["gpu_consumers"])):
            raise RuntimeError("fresh host preflight refuses model activation")
        with (private / "supervisor.log").open("xb") as log:
            process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            while process.poll() is None:
                if time.monotonic() - started > 735:
                    raise RuntimeError("external review deadline exceeded")
                parent_state = show(parent)
                if parent_state.get("MainPID", "0") != "0":
                    if parent_state.get("Description") != description:
                        raise RuntimeError("supervisor identity mismatch")
                    current = parent_state["InvocationID"]
                    if parent_invocation and current != parent_invocation:
                        raise RuntimeError("supervisor generation changed")
                    parent_invocation = current
                row = binding(database) or row
                model = None
                if row:
                    if (row["owner"] != "lab" or row["request_id"] != request
                            or not re.fullmatch(r"swapp-lab-gpu-turn-[0-9a-f]{32}\.service", row["unit"])):
                        raise RuntimeError("unexpected model intent in private review database")
                    model = show(row["unit"])
                    if model.get("MainPID", "0") != "0":
                        if model.get("Description") != row["expected_description"]:
                            raise RuntimeError("model identity mismatch")
                        current = model["InvocationID"]
                        if model_invocation and current != model_invocation:
                            raise RuntimeError("model generation changed")
                        model_invocation = current
                        model_group = model["ControlGroup"]
                observation = snapshot()
                observation["elapsed_seconds"] = time.monotonic() - started
                observation["supervisor"] = parent_state
                observation["model"] = model
                if model_group:
                    observation["model_cgroup"] = cgroup_observation(model_group)
                record["samples"].append(observation)
                foreign = [p for p in observation["gpu_consumers"]
                           if not p["existing_display_exemption"]
                           and p.get("cgroup") != "0::" + model_group]
                if foreign:
                    raise RuntimeError("uncoordinated GPU consumer appeared; stop own test only")
                if (observation["memory_available_bytes"] < 6 * GIB
                        or observation["disk_available_bytes"] < 20 * GIB
                        or observation["gpu"]["temperature_c"] >= 83):
                    raise RuntimeError("external observer resource reserve reached")
                time.sleep(1)
            record["supervisor_exit_code"] = process.wait(timeout=1)
    except Exception as error:
        record["error"] = f"{type(error).__name__}: {error}"
    finally:
        row = binding(database) or row
        if row:
            current = show(row["unit"])
            if current.get("MainPID", "0") != "0":
                valid = (current.get("Description") == row["expected_description"]
                         and (not model_invocation or current.get("InvocationID") == model_invocation))
                if valid:
                    units.stop(row["unit"], timeout_seconds=10)
                else:
                    record["cleanup"].append({"unit": row["unit"], "unknown_generation_untouched": True})
            record["model_final"] = show(row["unit"])
            record["model_cgroup_empty"] = not model_group or units.cgroup_empty(model_group)
        current = show(parent)
        if current.get("MainPID", "0") != "0":
            valid = (current.get("Description") == description
                     and (not parent_invocation or current.get("InvocationID") == parent_invocation))
            if valid:
                units.stop(parent, timeout_seconds=10)
            else:
                record["cleanup"].append({"unit": parent, "unknown_generation_untouched": True})
        if process:
            record["supervisor_exit_code"] = process.wait(timeout=20)
        record["supervisor_final"] = show(parent)
    post = snapshot()
    record["postflight"] = post
    record["sources_after"] = hashes()
    record["runtime_packages_after"] = runtime_packages()
    result = ROOT / "data/runtime/native-doctor" / f"{request}.json"
    if result.exists():
        record["doctor_result_sha256"] = hashlib.sha256(result.read_bytes()).hexdigest()
        record["doctor_result"] = json.loads(result.read_text())
    failure = result.with_suffix(".failure.json")
    if failure.exists():
        record["doctor_failure_sha256"] = hashlib.sha256(failure.read_bytes()).hexdigest()
        record["doctor_failure"] = json.loads(failure.read_text())
    diagnostic = record.get("doctor_result", record.get("doctor_failure", {})).get("diagnostics", {})
    if diagnostic.get("log_status") == "retained":
        log_path = ROOT / diagnostic["log_path"]
        log_root = ROOT / "data/runtime/native-doctor/logs"
        if log_path.parent == log_root and not log_path.is_symlink():
            with log_path.open("rb") as log:
                log.seek(max(0, log_path.stat().st_size - 16000))
                record["model_log_tail"] = log.read(16000).decode("utf-8", errors="replace")
    samples = record["samples"]
    record["observed_peaks"] = {
        "gpu_used_mib": max((s["gpu"]["used_mib"] for s in samples), default=0),
        "gpu_temperature_c": max((s["gpu"]["temperature_c"] for s in samples), default=0),
        "model_cgroup_memory_peak_bytes": max((int(s.get("model_cgroup", {}).get("memory.peak", 0)) for s in samples), default=0),
        "minimum_host_available_bytes": min((s["memory_available_bytes"] for s in samples), default=0),
    }
    checks = record["checks"]
    checks["production_doctor_exit_zero"] = record.get("supervisor_exit_code") == 0
    checks["real_output_recorded"] = result.exists() and bool(record["doctor_result"].get("text"))
    if profile == "s1":
        checks["arithmetic_answer_42"] = bool(re.search(r"\b42\b", record.get("doctor_result", {}).get("text", "")))
    else:
        metadata = diagnostic.get("response_metadata", {})
        checks["response_stopped_normally"] = metadata.get("finish_reason") == "stop"
        checks["thinking_generated"] = (
            metadata.get("reasoning_present") is True
            and isinstance(metadata.get("reasoning_length_chars"), int)
            and metadata["reasoning_length_chars"] > 0
        )
        checks["final_answer_nonempty"] = bool(
            record.get("doctor_result", {}).get("text", "").strip()
        )
        checks["completion_within_512_token_budget"] = (
            metadata.get("usage_valid") is True
            and isinstance(metadata.get("completion_tokens"), int)
            and 0 < metadata["completion_tokens"] <= 512
        )
    checks["sources_unchanged"] = record["sources_before"] == record["sources_after"]
    checks["runtime_packages_unchanged"] = record["runtime_packages_before"] == record["runtime_packages_after"]
    pinned = dict(re.findall(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)",
                            (ROOT / SOURCES[-1]).read_text(), re.M))
    pinned = {name.lower().replace("_", "-"): version for name, version in pinned.items()}
    checks["runtime_packages_match_resolved_lock"] = pinned == record["runtime_packages_before"]
    checks["model_unit_terminal"] = record.get("model_final", {}).get("MainPID", "0") == "0"
    checks["supervisor_unit_terminal"] = record["supervisor_final"].get("MainPID", "0") == "0"
    checks["model_cgroup_empty"] = record.get("model_cgroup_empty", True)
    checks["no_remaining_non_display_gpu_consumer"] = all(p["existing_display_exemption"] for p in post["gpu_consumers"])
    checks["no_observer_abort"] = "error" not in record
    record["elapsed_seconds"] = time.monotonic() - started
    record["passed"] = all(checks.values()) and not record["cleanup"]
    output.write_text(json.dumps(record, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"output": str(output), "request_id": request, "passed": record["passed"],
                      "checks": checks, "error": record.get("error"), "peaks": record["observed_peaks"]}))
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("s1", "s2"), required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    raise SystemExit(main(args.profile, args.output))
