"""One actual bounded production provider episode; no candidate execution.

Unlike the earlier single-wire probe, this exercises the provider's existing
repair/fallback logic and aggregate budget. Only receipt metadata and source
length/hash are persisted; generated source and hidden reasoning are excluded.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from uuid import UUID, uuid4

from review_gpu_host import GIB, ROOT, snapshot
from review_local_qwen_observer import QwenReviewObserver, unit_state
from review_local_qwen_s2_candidate import (
    expected_context, installed_source_hashes, model_generation_drained,
    pinned_runtime_packages, source_hashes,
)

sys.path.insert(0, str(ROOT))
from lab.director.local_llm import LocalQwenProposalProvider, ProviderOutputError
from lab.llm.gpu_scheduler import SystemdPrincipalResolver
from lab.llm.native_runtime import SystemdUnitManager, _prepare_doctor_directories

SCHEMA = "local-qwen-production-provider-episode-review.v1"
EPISODE_SECONDS = 600
EPISODE_TOKENS = 24_576


def sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def peer(arguments: list[str]) -> int:
    database, parent, aos_unit, run_hex, target = arguments
    result = {"schema": SCHEMA, "episode_seconds": EPISODE_SECONDS,
              "episode_tokens": EPISODE_TOKENS, "status": "failed"}
    provider = LocalQwenProposalProvider(
        run_id=UUID(hex=run_hex), owner="lab",
        principal_resolver=SystemdPrincipalResolver({"aos": aos_unit, "lab": parent}),
        runtime_database=Path(database),
        registry_entry_sha256=sha(b"synthetic-provider-episode-review.v1"),
    )
    started = time.monotonic()
    try:
        _prepare_doctor_directories()
        turn = provider.propose_bounded(
            expected_context(), remaining_wall_seconds=EPISODE_SECONDS,
            remaining_model_tokens=EPISODE_TOKENS,
        )
        result["receipt"] = turn.provider_receipt.model_dump(mode="json", by_alias=True)
        source = turn.proposal.candidate_source
        tree = ast.parse(source)
        has_entrypoint = any(isinstance(n, ast.FunctionDef) and n.name == "build_candidate"
                             for n in tree.body)
        result.update({"proposal_valid": True, "python_syntax_valid": True,
                       "entrypoint_present": has_entrypoint,
                       "move_type": turn.proposal.move_type,
                       "candidate_source_chars": len(source),
                       "candidate_source_lines": len(source.splitlines()),
                       "candidate_source_sha256": sha(source.encode()),
                       "input_tokens": turn.input_tokens, "output_tokens": turn.output_tokens,
                       "transcript_count": len(turn.provider_attempts)})
        if not has_entrypoint:
            result["error_type"] = "MissingCandidateEntrypoint"
        elif turn.provider_receipt.actual_system != "S2":
            result["error_type"] = "FallbackDidNotVerifyS2"
        else:
            result["status"] = "ok"
    except ProviderOutputError as error:
        result["error_type"] = type(error).__name__
        result["reason_code"] = error.reason_code
        result["receipt"] = error.provider_receipt.model_dump(mode="json", by_alias=True)
    except Exception as error:
        result["error_type"] = type(error).__name__
    result["elapsed_seconds"] = time.monotonic() - started
    output = Path(target)
    output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    output.chmod(0o600)
    return 0 if result["status"] == "ok" else 1


def main(output: Path) -> int:
    if output.exists() or output.is_symlink():
        raise ValueError("existing evidence cannot be overwritten")
    os.umask(0o077)
    key = uuid4().hex
    parent = f"swapp-lab-gpu-review-{key}.service"
    aos_unit = f"swapp-aos-gpu-review-{key}.service"
    description = f"SWAPP one production provider episode {key}"
    private = ROOT / "data/runtime/gpu" / f"provider-episode-{key}"
    private.mkdir(mode=0o700)
    database = private / "arbiter.sqlite3"
    peer_path = private / "peer.json"
    log_path = private / "supervisor.log"
    observer = QwenReviewObserver(database, parent, description)
    record = {
        "schema": SCHEMA, "scope": "One real S2 provider episode on synthetic metadata; "
        "existing bounded repair/fallback; no candidate execution, Scorer, research run or AOS task.",
        "private_directory": str(private.relative_to(ROOT)), "sources_before": source_hashes(),
        "runtime_packages_before": pinned_runtime_packages(),
        "vllm_sources_before": installed_source_hashes(),
        "driver_sha256": sha(Path(__file__).read_bytes()),
        "helper_sha256": {name: sha((Path(__file__).parent / name).read_bytes()) for name in (
            "review_gpu_host.py", "review_local_qwen_observer.py", "review_local_qwen_s2_candidate.py")},
        "samples": [], "cleanup": [],
    }
    command = [
        "/usr/bin/systemd-run", "--user", "--quiet", "--wait", "--collect",
        f"--unit={parent}", "--slice=swapp-gpu.slice", f"--description={description}",
        "--property=MemoryMax=2G", "--property=MemorySwapMax=0", "--property=CPUQuota=50%",
        "--property=TasksMax=16", "--property=RuntimeMaxSec=620",
        "--property=TimeoutStopSec=10", "--property=KillMode=control-group",
        "--property=NoNewPrivileges=yes", "--property=UMask=0077",
        f"--working-directory={ROOT}", f"--setenv=SWAPP_LAB_GPU_UNIT={parent}",
        f"--setenv=SWAPP_AOS_GPU_UNIT={aos_unit}", f"--setenv=SWAPP_GPU_RUNTIME_DB={database}",
        "--setenv=OPENBLAS_NUM_THREADS=1", "--setenv=OMP_NUM_THREADS=1", "--setenv=MKL_NUM_THREADS=1",
        str(ROOT / ".venv/bin/python"), str(Path(__file__).resolve()), "--peer",
        str(database), parent, aos_unit, key, str(peer_path),
    ]
    record["command"] = command
    started = time.monotonic()
    process = None
    try:
        SystemdUnitManager().ensure_slice()
        pre = snapshot()
        record["preflight"] = pre
        if (pre["memory_available_bytes"] < 16 * GIB
                or pre["disk_available_bytes"] < 20 * GIB
                or pre["gpu"]["temperature_c"] >= 83
                or any(not p["existing_display_exemption"] for p in pre["gpu_consumers"])):
            raise RuntimeError("fresh preflight refuses model activation")
        with log_path.open("xb") as log:
            process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            while process.poll() is None:
                if time.monotonic() - started > 650:
                    raise TimeoutError("provider episode exceeded outer bound")
                sample = observer.observe()
                sample["elapsed_seconds"] = time.monotonic() - started
                record["samples"].append(sample)
                if sample["foreign_gpu_consumers"]:
                    raise RuntimeError("uncoordinated GPU consumer appeared; preserve foreign process")
                if not sample["reserves_maintained"]:
                    raise RuntimeError("host reserve or temperature threshold reached")
                time.sleep(1)
            record["supervisor_exit_code"] = process.wait(timeout=2)
    except Exception as error:
        record["observer_error"] = f"{type(error).__name__}: {str(error)[:240]}"
    finally:
        try:
            state = unit_state(parent)
            if int(state.get("MainPID", "0")):
                if state.get("Description") == description:
                    observer.stop_owned_parent()
                else:
                    record["cleanup"].append({"foreign_generation_left_untouched": True})
            if process is not None:
                record["supervisor_exit_code"] = process.wait(timeout=20)
        except Exception as error:
            record["cleanup"].append({"parent_cleanup_error": type(error).__name__})
        try:
            observer.stop_owned_models()
        except Exception as error:
            record["cleanup"].append({"model_cleanup_error": type(error).__name__})
    record["supervisor_final"] = unit_state(parent)
    record["peer"] = json.loads(peer_path.read_text()) if peer_path.is_file() else None
    record["postflight"] = snapshot()
    record["model_bindings"] = observer.read_bindings()
    try:
        record["final_observation"] = observer.observe()
    except Exception as error:
        record["final_observation"] = None
        record["final_observation_error"] = type(error).__name__
    if log_path.is_file():
        record["supervisor_log_bytes"] = log_path.stat().st_size
        record["supervisor_log_sha256"] = sha(log_path.read_bytes())
    tickets = []
    if database.is_file():
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            tickets = [dict(r) for r in connection.execute(
                "SELECT owner,request_id,state,owner_unit FROM gpu_turn_requests ORDER BY rowid")]
    record["tickets"] = tickets
    record["sources_after"] = source_hashes()
    record["runtime_packages_after"] = pinned_runtime_packages()
    record["vllm_sources_after"] = installed_source_hashes()
    result = record["peer"] or {}
    receipt = result.get("receipt") or {}
    attempts = receipt.get("attempts", [])
    final = record["final_observation"] or {}
    checks = {
        "valid_production_s2_proposal": result.get("status") == "ok"
            and receipt.get("requested_system") == receipt.get("actual_system") == "S2"
            and result.get("move_type") == "features" and result.get("entrypoint_present") is True,
        "bounded_episode": result.get("elapsed_seconds", 1e6) <= EPISODE_SECONDS
            and receipt.get("input_tokens", EPISODE_TOKENS) + receipt.get("output_tokens", 1) <= EPISODE_TOKENS
            and receipt.get("output_tokens", 8193) <= 8192,
        "at_most_one_repair": 1 <= len(attempts) <= 3
            and sum(a["attempt_kind"] == "repair" for a in attempts) <= 1,
        "attempt_token_bounds": bool(attempts) and all(
            a.get("completion_tokens") is not None and a["completion_tokens"] <= a["output_token_limit"]
            and a.get("prompt_tokens") == a.get("preflight_prompt_tokens") for a in attempts),
        "receipt_model_bindings": bool(attempts)
            and {a["unit"] for a in attempts} == {b["unit"] for b in record["model_bindings"]},
        "all_tickets_done": bool(tickets) and all(t["owner"] == "lab" and t["owner_unit"] == parent
            and t["state"] == "done" for t in tickets),
        "all_models_drained": bool(final.get("models"))
            and all(model_generation_drained(m) for m in final["models"]),
        "parent_terminal": record["supervisor_final"].get("MainPID", "0") == "0",
        "supervisor_exit_zero": record.get("supervisor_exit_code") == 0,
        "cleanup_clear": not record["cleanup"] and not observer.cleanup,
        "no_foreign_gpu_after": not any(not p["existing_display_exemption"]
            for p in record["postflight"]["gpu_consumers"]),
        "sources_unchanged": record["sources_before"] == record["sources_after"],
        "runtime_unchanged": record["runtime_packages_before"] == record["runtime_packages_after"]
            and record["vllm_sources_before"] == record["vllm_sources_after"],
    }
    record["checks"] = checks
    record["passed"] = all(checks.values())
    record["elapsed_seconds"] = time.monotonic() - started
    record["cleanup"].extend(observer.cleanup)
    output.write_text(json.dumps(record, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"passed": record["passed"], "checks": checks,
                      "elapsed_seconds": record["elapsed_seconds"], "output": str(output)}))
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--peer", nargs=5)
    arguments = parser.parse_args()
    if arguments.peer:
        raise SystemExit(peer(arguments.peer))
    if arguments.output is None:
        parser.error("--output is required")
    raise SystemExit(main(arguments.output))
