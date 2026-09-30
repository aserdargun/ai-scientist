"""One bounded actual S2 proposal-format probe on the synthetic research context.

The probe uses the trusted LOCAL_SMOKE_S2_PROFILE and production CandidateProposal
schema/parser, under the production Lab principal resolver and owned child unit.
It makes exactly one model request; it does not retry, score, execute candidate
source, or expose labels, generated source, or hidden reasoning in its record.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from uuid import UUID, uuid4

from review_gpu_host import GIB, ROOT, snapshot
from review_local_qwen_observer import QwenReviewObserver, unit_state

sys.path.insert(0, str(ROOT))
from lab.director.fake_llm import AgentContext
from lab.director.local_llm import (
    LOCAL_SMOKE_S2_PROFILE,
    LocalQwenProposalProvider,
    _CANDIDATE_PROPOSAL_SCHEMA,
    _CANDIDATE_SCHEMA_NAME,
    _CANDIDATE_SCHEMA_SHA256,
    provider_config_sha256,
)
from lab.director.contracts import CandidateProposal
from lab.director.baselines import baseline_candidate_source
from lab.director.journal import canonical_bytes
from lab.llm.gpu_scheduler import SystemdPrincipalResolver
from lab.llm.native_runtime import (
    LOCAL_SMOKE_S2_PROFILE as NATIVE_LOCAL_SMOKE_S2_PROFILE,
    ModelOutputBudgetExceeded,
    OwnedVllmRuntime,
    _prepare_doctor_directories,
)

PROBE_SCHEMA = "local-qwen-single-s2-proposal-probe.v1"
SAFE_RESPONSE_KEYS = frozenset({
    "http_content_type", "content_length_bytes", "finish_reason", "usage_valid",
    "prompt_tokens", "completion_tokens", "message_content_type",
    "content_length_chars", "reasoning_present", "reasoning_length_chars",
})
SOURCE_PATHS = (
    "lab/director/baselines.py",
    "lab/director/contracts.py",
    "lab/director/fake_llm.py",
    "lab/director/local_llm.py",
    "lab/llm/native_runtime.py",
    "lab/llm/gpu_scheduler.py",
    "harness/VERSION",
    "ops/sandbox-image.lock",
    "docs/ai-scientist/review-evidence/qwen-model-source.json",
    "docs/ai-scientist/review-evidence/vllm-cuda132-resolved.lock",
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_hashes() -> dict[str, str]:
    return {path: digest((ROOT / path).read_bytes()) for path in SOURCE_PATHS}


def runtime_versions() -> dict[str, str]:
    code = (
        "import importlib.metadata as m,json; "
        "print(json.dumps({n:m.version(n) for n in "
        "['vllm','transformers','xgrammar','torch']}))"
    )
    result = subprocess.run(
        [str(ROOT / "data/runtime/vllm/.venv/bin/python"), "-c", code],
        check=True, capture_output=True, text=True, timeout=10,
    )
    return json.loads(result.stdout)


def pinned_runtime_packages() -> dict[str, str]:
    from review_native_doctor import runtime_packages

    return runtime_packages()


def installed_source_hashes() -> dict[str, str]:
    code = (
        "import hashlib,importlib.metadata as m,json; "
        "d=m.distribution('vllm'); "
        "names=['vllm/entrypoints/openai/chat_completion/protocol.py',"
        "'vllm/sampling_params.py','vllm/v1/sample/thinking_budget_state.py',"
        "'vllm/v1/worker/gpu/sample/thinking_budget.py',"
        "'vllm/v1/worker/gpu/sample/sampler.py',"
        "'vllm/v1/sample/ops/topk_topp_sampler.py',"
        "'vllm/v1/sample/ops/topk_topp_triton.py']; "
        "print(json.dumps({n:hashlib.sha256(d.locate_file(n).read_bytes()).hexdigest() "
        "for n in names}))"
    )
    result = subprocess.run(
        [str(ROOT / "data/runtime/vllm/.venv/bin/python"), "-c", code],
        check=True, capture_output=True, text=True, timeout=10,
    )
    return json.loads(result.stdout)


def profile_dict() -> dict[str, object]:
    from dataclasses import asdict

    return asdict(LOCAL_SMOKE_S2_PROFILE)


def expected_context() -> AgentContext:
    # Metadata-only fixture. No values, labels, raw series, or holdout details.
    return AgentContext(
        phase="proposal",
        experiment_number=1,
        system="S2",
        move_type="features",
        task_cards=(
            "task=synthetic.s2-review; family=EVT; fixture_dev_score=unmeasured; "
            "signals=sensor_a,sensor_b; sampling_s=1; task_weight=1.0",
        ),
        champion_source=baseline_candidate_source("robust_z").decode("utf-8"),
        recent_feedback=(),
    )


def metadata_only(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return {key: value[key] for key in SAFE_RESPONSE_KEYS if key in value}


def model_generation_drained(entry: dict[str, object]) -> bool:
    state = entry.get("state")
    identity = entry.get("identity")
    if not isinstance(state, dict) or state.get("MainPID", "0") != "0":
        return False
    cgroup = state.get("cgroup")
    if isinstance(cgroup, dict) and "pids.current" in cgroup:
        return cgroup["pids.current"] == "0"
    if not isinstance(identity, tuple) or len(identity) < 5:
        return False
    raw = str(identity[4])
    if not raw.startswith("0::/"):
        return False
    group_path = Path("/sys/fs/cgroup") / raw.removeprefix("0::/")
    # With systemd --collect, an absent exact cgroup is terminal evidence.
    return not group_path.exists()


def plan_record(script_path: Path) -> dict[str, object]:
    context = expected_context()
    context_digest = digest(canonical_bytes(context.model_dump(mode="json")))
    schema = CandidateProposal.model_json_schema()
    schema_digest = digest(canonical_bytes(schema))
    previous_metadata_paths = (
        "docs/ai-scientist/review-evidence/native-qwen-s2-bounded-response-review.json",
        "docs/ai-scientist/review-evidence/native-qwen-s2-thinking-budget-review.json",
    )
    return {
        "schema": "local-qwen-s2-cpu-diagnosis.v1",
        "scope": "Pinned local source diagnosis and one-call probe plan; no model/GPU invocation.",
        "checked_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "installed_versions": runtime_versions(),
        "profile": profile_dict(),
        "profile_validation": {
            "native_profile_equal": LOCAL_SMOKE_S2_PROFILE == NATIVE_LOCAL_SMOKE_S2_PROFILE,
            "thinking_budget_strictly_below_output": (
                LOCAL_SMOKE_S2_PROFILE.thinking_token_budget is not None
                and LOCAL_SMOKE_S2_PROFILE.thinking_token_budget
                < LOCAL_SMOKE_S2_PROFILE.max_output_tokens
            ),
            "queue_startup_inference_share_one_total_deadline": True,
        },
        "vllm_semantics": {
            "source": "installed pinned vLLM package",
            "protocol_path": "vllm/entrypoints/openai/chat_completion/protocol.py",
            "sampling_path": "vllm/sampling_params.py",
            "sampler_path": "vllm/v1/worker/gpu/sample/thinking_budget.py",
            "installed_file_sha256": installed_source_hashes(),
            "verified": [
                "thinking_token_budget is a nonnegative integer forwarded into SamplingParams",
                "the V2 sampler raises the end-marker logit at the thinking cap; final sampling must preserve it",
                "max_tokens separately bounds total completion tokens including reasoning",
                "finish_reason=length is a total-output truncation and must fail closed",
            ],
            "measured_sampler_failure_evidence": "vllm-forced-token-sampler-review.json",
        },
        "previous_bounded_512_metadata": [
            {
                "evidence": "native-qwen-s2-bounded-response-review.json",
                "finish_reason": "length", "prompt_tokens": 38,
                "completion_tokens": 512, "content_length_chars": None,
                "reasoning_length_chars": 1807,
            },
            {
                "evidence": "native-qwen-s2-thinking-budget-review.json",
                "finish_reason": "length", "prompt_tokens": 38,
                "completion_tokens": 512, "content_length_chars": 1315,
                "reasoning_length_chars": 454,
            },
        ],
        "previous_failure_metadata_sha256": {
            path: digest((ROOT / path).read_bytes()) for path in previous_metadata_paths
        },
        "output_schema": {
            "name": _CANDIDATE_SCHEMA_NAME,
            "sha256": _CANDIDATE_SCHEMA_SHA256,
            "global_candidate_schema_sha256": schema_digest,
            "local_hypothesis_max_chars": _CANDIDATE_PROPOSAL_SCHEMA["properties"]["hypothesis"]["maxLength"],
            "local_source_max_chars": _CANDIDATE_PROPOSAL_SCHEMA["properties"]["candidate_source"]["maxLength"],
        },
        "provider_config_sha256": provider_config_sha256(),
        "synthetic_context_sha256": context_digest,
        "source_sha256": source_hashes(),
        "review_helper_sha256": {
            "docs/ai-scientist/review-evidence/review_gpu_host.py": digest(
                (ROOT / "docs/ai-scientist/review-evidence/review_gpu_host.py").read_bytes()
            ),
            "docs/ai-scientist/review-evidence/review_local_qwen_observer.py": digest(
                (ROOT / "docs/ai-scientist/review-evidence/review_local_qwen_observer.py").read_bytes()
            ),
        },
        "runtime_packages": pinned_runtime_packages(),
        "installed_vllm_source_sha256": installed_source_hashes(),
        "runtime_lock_sha256": digest(
            (ROOT / "docs/ai-scientist/review-evidence/vllm-cuda132-resolved.lock").read_bytes()
        ),
        "probe_driver_sha256": digest(script_path.read_bytes()),
        "probe_contract": {
            "calls": 1,
            "strict_candidate_parse": True,
            "trusted_move_type": context.move_type,
            "max_output_tokens": LOCAL_SMOKE_S2_PROFILE.max_output_tokens,
            "thinking_token_budget": LOCAL_SMOKE_S2_PROFILE.thinking_token_budget,
            "activation_seconds": LOCAL_SMOKE_S2_PROFILE.activation_seconds,
            "inference_seconds": LOCAL_SMOKE_S2_PROFILE.inference_seconds,
            "total_seconds_including_queue": LOCAL_SMOKE_S2_PROFILE.total_seconds,
            "peer_parent_memory_limit_gib": 2,
            "failure_receipt_fields": sorted(SAFE_RESPONSE_KEYS),
            "raw_response_or_reasoning_persisted": False,
        },
    }


def peer_main(argv: list[str]) -> int:
    if len(argv) != 5:
        return 64
    database = Path(argv[0])
    parent_unit, aos_unit, request_id = argv[1:4]
    output_path = Path(argv[4])
    resolver = SystemdPrincipalResolver({"aos": aos_unit, "lab": parent_unit})
    run_id = UUID(bytes=hashlib.sha256(request_id.encode()).digest()[:16])
    context = expected_context()
    provider = LocalQwenProposalProvider(
        run_id=run_id,
        owner="lab",
        principal_resolver=resolver,
        runtime_database=database,
        registry_entry_sha256=digest(b"single-s2-review-profile-entry.v1"),
    )
    profile = LOCAL_SMOKE_S2_PROFILE
    result: dict[str, object] = {
        "schema": PROBE_SCHEMA,
        "request_id": request_id,
        "requested_system": context.system,
        "profile": profile_dict(),
        "provider_config_sha256": provider.configuration_sha256,
        "model_sha256": provider.model_pin.digest,
        "response_schema_name": _CANDIDATE_SCHEMA_NAME,
        "response_schema_sha256": _CANDIDATE_SCHEMA_SHA256,
        "synthetic_context_sha256": digest(canonical_bytes(context.model_dump(mode="json"))),
    }
    runtime: OwnedVllmRuntime | None = None
    try:
        _prepare_doctor_directories()
        messages, prompt_tokens = provider._select_messages(
            context, profile, timeout_seconds=30.0
        )
        if prompt_tokens > profile.max_context_tokens:
            raise ValueError("preflight context exceeds the trusted profile cap")
        if prompt_tokens + profile.max_output_tokens > profile.model_max_len:
            raise ValueError("prompt plus output exceeds the trusted model length")
        result["preflight_prompt_tokens"] = prompt_tokens
        result["prompt_sha256"] = digest(canonical_bytes({"messages": list(messages)}))
        runtime = OwnedVllmRuntime(
            database,
            principal_resolver=resolver,
            pin=provider.model_pin,
            profile=profile,
            activation_seconds=profile.activation_seconds,
            inference_seconds=profile.inference_seconds,
            total_seconds=profile.total_seconds,
            queue_timeout_seconds=900,
        )
        reply = runtime.run_turn(
            "lab",
            request_id,
            messages,
            enable_thinking=profile.enable_thinking,
            profile=profile,
            response_schema_name=_CANDIDATE_SCHEMA_NAME,
            response_schema=_CANDIDATE_PROPOSAL_SCHEMA,
        )
        proposal = provider._parse_proposal(reply.text, expected_move=context.move_type)
        result.update({
            "candidate_schema_valid": True,
            "move_type": proposal.move_type,
            "candidate_source_chars": len(proposal.candidate_source),
            "candidate_source_lines": len(proposal.candidate_source.splitlines()),
            "candidate_source_literal_newlines": proposal.candidate_source.count("\\n"),
            "candidate_source_sha256": digest(proposal.candidate_source.encode("utf-8")),
            "hypothesis_chars": len(proposal.hypothesis),
            "prompt_tokens": reply.prompt_tokens,
            "completion_tokens": reply.completion_tokens,
            "measurements": {
                "startup_seconds": reply.measurements.startup_seconds,
                "inference_seconds": reply.measurements.inference_seconds,
                "drain_seconds": reply.measurements.drain_seconds,
                "peak_host_memory_bytes": reply.measurements.peak_host_memory_bytes,
                "peak_gpu_memory_mib": reply.measurements.peak_gpu_memory_mib,
                "cpu_usage_usec": reply.measurements.cpu_usage_usec,
            },
        })
        if proposal.move_type != context.move_type:
            raise ValueError("model changed trusted move intent")
        try:
            syntax = ast.parse(proposal.candidate_source)
        except SyntaxError as error:
            # SyntaxError.text and free-form messages may contain source tokens.
            # Retain only locations and a fixed classification, never source text.
            result["candidate_python_syntax_valid"] = False
            result["syntax_error"] = {
                "line": error.lineno,
                "offset": error.offset,
                "end_line": error.end_lineno,
                "end_offset": error.end_offset,
                "kind": "indentation" if isinstance(error, IndentationError) else "syntax",
            }
            raise ValueError("candidate source is not complete valid Python syntax") from None
        if not any(isinstance(node, ast.FunctionDef) and node.name == "build_candidate"
                for node in syntax.body):
            raise ValueError("candidate source lacks its top-level build_candidate entrypoint")
        result.update({
            "status": "ok",
            "candidate_valid": True,
            "candidate_python_syntax_valid": True,
            "candidate_entrypoint_present": True,
            "move_type": proposal.move_type,
            "candidate_source_chars": len(proposal.candidate_source),
            "candidate_source_sha256": digest(proposal.candidate_source.encode("utf-8")),
            "hypothesis_chars": len(proposal.hypothesis),
            "prompt_tokens": reply.prompt_tokens,
            "completion_tokens": reply.completion_tokens,
            "measurements": {
                "startup_seconds": reply.measurements.startup_seconds,
                "inference_seconds": reply.measurements.inference_seconds,
                "drain_seconds": reply.measurements.drain_seconds,
                "peak_host_memory_bytes": reply.measurements.peak_host_memory_bytes,
                "peak_gpu_memory_mib": reply.measurements.peak_gpu_memory_mib,
                "cpu_usage_usec": reply.measurements.cpu_usage_usec,
            },
        })
        return_code = 0
    except Exception as exc:
        result.update({
            "status": "failed",
            "error_type": type(exc).__name__,
            "failure_phase": (
                runtime._failure_phase or runtime._phase if runtime is not None else "preflight"
            ),
            "error_message": "".join(
                ch for ch in str(exc) if ch >= " " and ch != "\x7f"
            )[:240],
            "response_metadata": metadata_only(
                runtime._last_response_metadata if runtime is not None else None
            ),
        })
        return_code = 1
    if runtime is not None and result.get("status") == "ok":
        result["response_metadata"] = metadata_only(runtime._last_response_metadata)
    output_path.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    output_path.chmod(0o600)
    return return_code


def show(unit: str) -> dict[str, str]:
    return unit_state(unit)


def main(output: Path) -> int:
    if output.exists() or output.is_symlink():
        raise ValueError("review output already exists")
    os.umask(0o077)
    request_id = uuid4().hex
    parent = f"swapp-lab-gpu-review-{request_id}.service"
    description = f"SWAPP isolated single S2 candidate review {request_id}"
    private = ROOT / "data/runtime/gpu" / f"local-s2-review-{request_id}"
    private.mkdir(mode=0o700)
    database = private / "arbiter.sqlite3"
    result_file = private / "peer-result.json"
    log_file = private / "supervisor.log"
    resolver_aos = f"swapp-aos-gpu-review-{request_id}.service"
    observer = QwenReviewObserver(database, parent, description)
    record: dict[str, object] = {
        "schema": PROBE_SCHEMA,
        "request_id": request_id,
        "scope": "One S2 structured proposal generation on a fixed synthetic metadata context; no execution/scoring/research run.",
        "sources_before": source_hashes(),
        "review_helper_sha256": {
            "docs/ai-scientist/review-evidence/review_gpu_host.py": digest(
                (ROOT / "docs/ai-scientist/review-evidence/review_gpu_host.py").read_bytes()
            ),
            "docs/ai-scientist/review-evidence/review_local_qwen_observer.py": digest(
                (ROOT / "docs/ai-scientist/review-evidence/review_local_qwen_observer.py").read_bytes()
            ),
        },
        "runtime_packages": pinned_runtime_packages(),
        "installed_vllm_source_sha256": installed_source_hashes(),
        "runtime_lock_sha256": digest(
            (ROOT / "docs/ai-scientist/review-evidence/vllm-cuda132-resolved.lock").read_bytes()
        ),
        "runtime_versions": runtime_versions(),
        "profile": profile_dict(),
        "candidate_schema_sha256": _CANDIDATE_SCHEMA_SHA256,
        "review_driver_sha256": digest(Path(__file__).read_bytes()),
        "private_directory": str(private.relative_to(ROOT)),
        "samples": [],
        "cleanup": [],
    }
    command = [
        "/usr/bin/systemd-run", "--user", "--quiet", "--wait", "--collect",
        f"--unit={parent}", "--slice=swapp-gpu.slice",
        f"--description={description}", "--property=MemoryMax=2G",
        "--property=MemorySwapMax=0", "--property=CPUQuota=50%",
        "--property=TasksMax=16", "--property=RuntimeMaxSec=300",
        "--property=TimeoutStopSec=10", "--property=KillMode=control-group",
        "--property=NoNewPrivileges=yes", "--property=UMask=0077",
        f"--working-directory={ROOT}", f"--setenv=SWAPP_LAB_GPU_UNIT={parent}",
        f"--setenv=SWAPP_AOS_GPU_UNIT={resolver_aos}",
        f"--setenv=SWAPP_GPU_RUNTIME_DB={database}",
        "--setenv=OPENBLAS_NUM_THREADS=1", "--setenv=OMP_NUM_THREADS=1",
        "--setenv=MKL_NUM_THREADS=1", str(ROOT / ".venv/bin/python"),
        str(Path(__file__).resolve()), "--peer", str(database), parent,
        resolver_aos, request_id, str(result_file),
    ]
    record["command"] = command
    process: subprocess.Popen | None = None
    started = time.monotonic()
    try:
        from lab.llm.native_runtime import SystemdUnitManager

        units = SystemdUnitManager()
        units.ensure_slice()
        pre = snapshot()
        record["preflight"] = pre
        if (
            pre["memory_available_bytes"] < 16 * GIB
            or pre["disk_available_bytes"] < 20 * GIB
            or pre["gpu"]["temperature_c"] >= 83
            or any(not item["existing_display_exemption"] for item in pre["gpu_consumers"])
        ):
            raise RuntimeError("fresh preflight refuses model activation")
        with log_file.open("xb") as log:
            process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            while process.poll() is None:
                if time.monotonic() - started > 330:
                    raise TimeoutError("single S2 review exceeded its 330-second outer bound")
                observation = observer.observe()
                observation["elapsed_seconds"] = time.monotonic() - started
                record["samples"].append(observation)
                if observation["foreign_gpu_consumers"]:
                    raise RuntimeError("uncoordinated GPU consumer appeared; stop only owned review units")
                if not observation["reserves_maintained"]:
                    raise RuntimeError("host reserve or GPU temperature threshold reached")
                time.sleep(1)
            record["supervisor_exit_code"] = process.wait(timeout=2)
    except Exception as exc:
        record["observer_error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
    finally:
        try:
            current = show(parent)
            if int(current.get("MainPID", "0")):
                if current.get("Description") != description:
                    record["cleanup"].append({"foreign_generation_left_untouched": True})
                else:
                    observer.stop_owned_parent()
            if process is not None:
                try:
                    record["supervisor_exit_code"] = process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    record["cleanup"].append({"supervisor_wait_timeout": True})
            try:
                observer.stop_owned_models()
            except Exception as cleanup_exc:
                record["cleanup"].append({
                    "model_cleanup_error": type(cleanup_exc).__name__
                })
            record["supervisor_final"] = show(parent)
            record["model_bindings_final"] = observer.read_bindings()
        except Exception as cleanup_exc:
            record["cleanup"].append({"cleanup_error": type(cleanup_exc).__name__})

    if result_file.is_file() and not result_file.is_symlink():
        result = json.loads(result_file.read_text())
        record["peer_result"] = result
        record["peer_result_sha256"] = digest(result_file.read_bytes())
    else:
        record["peer_result"] = None
    if log_file.is_file() and not log_file.is_symlink():
        info = log_file.stat(follow_symlinks=False)
        if info.st_size <= 64 * 1024:
            record["supervisor_log_sha256"] = digest(log_file.read_bytes())
            record["supervisor_log_bytes"] = info.st_size
    post = snapshot()
    try:
        final_observation = observer.observe()
    except Exception as observe_exc:
        final_observation = None
        record["final_observation_error"] = type(observe_exc).__name__
    record["postflight"] = post
    record["final_observation"] = final_observation
    final_bindings = observer.read_bindings()
    record["model_bindings_final"] = final_bindings
    ticket_final = None
    if database.is_file():
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=2) as connection:
            connection.row_factory = sqlite3.Row
            try:
                row = connection.execute(
                    "SELECT state FROM gpu_turn_requests WHERE owner='lab' AND request_id=?",
                    (request_id,),
                ).fetchone()
                ticket_final = dict(row) if row else None
            except sqlite3.OperationalError:
                ticket_final = None
    record["ticket_final"] = ticket_final
    from review_native_doctor import runtime_packages

    record["runtime_packages_after"] = runtime_packages()
    record["installed_vllm_source_sha256_after"] = installed_source_hashes()
    pinned = dict(re.findall(
        r"^([A-Za-z0-9_.-]+)==([^\s\\]+)",
        (ROOT / "docs/ai-scientist/review-evidence/vllm-cuda132-resolved.lock").read_text(),
        re.M,
    ))
    pinned = {name.lower().replace("_", "-"): version for name, version in pinned.items()}
    record["sources_after"] = source_hashes()
    result = record.get("peer_result")
    metadata = result.get("response_metadata") if isinstance(result, dict) else None
    checks = {
        "source_unchanged": record["sources_before"] == record["sources_after"],
        "one_s2_profile": LOCAL_SMOKE_S2_PROFILE.system == "S2",
        "profile_matches_peer_and_is_bounded": isinstance(result, dict)
            and result.get("profile") == record["profile"]
            and 0 < LOCAL_SMOKE_S2_PROFILE.total_seconds <= 240
            and 0 < LOCAL_SMOKE_S2_PROFILE.max_output_tokens <= 8192
            and LOCAL_SMOKE_S2_PROFILE.max_context_tokens
                + LOCAL_SMOKE_S2_PROFILE.max_output_tokens <= LOCAL_SMOKE_S2_PROFILE.model_max_len,
        "request_generation_or_failure_recorded": isinstance(result, dict),
        "successful_candidate_schema_parse": isinstance(result, dict)
            and result.get("candidate_schema_valid") is True,
        "complete_candidate_python_syntax": isinstance(result, dict)
            and result.get("candidate_python_syntax_valid") is True
            and result.get("candidate_entrypoint_present") is True,
        "trusted_move_binding": isinstance(result, dict)
            and result.get("move_type") == "features",
        "response_finished_normally": isinstance(metadata, dict)
            and metadata.get("finish_reason") == "stop",
        "completion_within_trusted_profile_budget": isinstance(metadata, dict)
            and metadata.get("usage_valid") is True
            and isinstance(metadata.get("completion_tokens"), int)
            and 0 < metadata["completion_tokens"] <= LOCAL_SMOKE_S2_PROFILE.max_output_tokens,
        "reasoning_length_only_metadata": isinstance(metadata, dict)
            and metadata.get("reasoning_present") is True
            and isinstance(metadata.get("reasoning_length_chars"), int)
            and metadata["reasoning_length_chars"] > 0,
        "preflight_and_response_prompt_tokens_match": isinstance(result, dict)
            and isinstance(metadata, dict)
            and result.get("preflight_prompt_tokens") == metadata.get("prompt_tokens"),
        "no_model_or_reasoning_text_in_record": not any(
            key in metadata for key in ("content", "reasoning", "text")
        ) if isinstance(metadata, dict) else False,
        "supervisor_exited_zero": record.get("supervisor_exit_code") == 0,
        "supervisor_terminal": record.get("supervisor_final", {}).get("MainPID", "0") == "0",
        "one_durable_model_generation": len(final_bindings) == 1
            and final_bindings[0].get("owner") == "lab"
            and final_bindings[0].get("request_id") == request_id
            and final_bindings[0].get("owner_unit") == parent
            and bool(final_bindings[0].get("invocation_id")),
        "durable_ticket_done": ticket_final == {"state": "done"},
        "final_model_units_drained": final_observation is not None
            and len(final_observation.get("models", [])) == 1
            and all(model_generation_drained(entry)
                    for entry in final_observation.get("models", [])),
        "runtime_packages_unchanged": record["runtime_packages"] == record["runtime_packages_after"],
        "runtime_packages_match_lock": pinned == record["runtime_packages"],
        "installed_vllm_sources_unchanged": (
            record["installed_vllm_source_sha256"]
            == record["installed_vllm_source_sha256_after"]
        ),
        "runtime_lock_unchanged": record["runtime_lock_sha256"] == digest(
            (ROOT / "docs/ai-scientist/review-evidence/vllm-cuda132-resolved.lock").read_bytes()
        ),
        "review_helpers_unchanged": record["review_helper_sha256"] == {
            "docs/ai-scientist/review-evidence/review_gpu_host.py": digest(
                (ROOT / "docs/ai-scientist/review-evidence/review_gpu_host.py").read_bytes()
            ),
            "docs/ai-scientist/review-evidence/review_local_qwen_observer.py": digest(
                (ROOT / "docs/ai-scientist/review-evidence/review_local_qwen_observer.py").read_bytes()
            ),
        },
        "no_non_display_gpu_consumer_after": all(
            item["existing_display_exemption"] for item in post["gpu_consumers"]
        ),
        "cleanup_clear": not record["cleanup"] and "observer_error" not in record,
    }
    record["checks"] = checks
    record["elapsed_seconds"] = time.monotonic() - started
    record["passed"] = all(checks.values())
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, indent=2, allow_nan=False) + "\n")
    output.chmod(0o600)
    print(json.dumps({"output": str(output), "passed": record["passed"], "checks": checks}))
    return 0 if record["passed"] else 1


def main_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cpu-diagnosis-output", type=Path)
    parser.add_argument("--peer", action="store_true")
    parser.add_argument("peer_args", nargs="*")
    args = parser.parse_args(argv)
    if args.peer:
        return peer_main(args.peer_args)
    if args.cpu_diagnosis_output is not None:
        if args.output is not None:
            parser.error("choose only one of --output and --cpu-diagnosis-output")
        target = args.cpu_diagnosis_output
        if target.exists() or target.is_symlink():
            parser.error("CPU diagnosis output already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(plan_record(Path(__file__).resolve()), indent=2) + "\n")
        print(json.dumps({"output": str(target), "model_invoked": False}))
        return 0
    if args.output is None:
        parser.error("--output is required")
    return main(args.output)


if __name__ == "__main__":
    raise SystemExit(main_cli())
