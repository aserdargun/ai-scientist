"""Measure bounded-smoke prompt sizes using the pinned local tokenizer on CPU."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from uuid import uuid4

from lab.director.baselines import baseline_candidate_source
from lab.director.fake_llm import AgentContext
from lab.director.local_llm import LocalQwenProposalProvider
from lab.llm.native_runtime import LOCAL_SMOKE_S1_PROFILE, LOCAL_SMOKE_S2_PROFILE, ModelPin
from review_scorer_queue import ROOT

TOKENIZER_PROGRAM = """
import hashlib, json, pathlib, sys
from transformers import AutoTokenizer
source = json.loads(pathlib.Path(sys.argv[1]).read_text())
tokenizer = AutoTokenizer.from_pretrained(source['model_directory'], local_files_only=True, trust_remote_code=False)
results = []
for item in source['cases']:
    ids = tokenizer.apply_chat_template(item['messages'], tokenize=True, return_dict=False, add_generation_prompt=True, enable_thinking=item['enable_thinking'])
    if not isinstance(ids, list) or not all(isinstance(token, int) for token in ids):
        raise TypeError('tokenizer did not return a flat list of token IDs')
    results.append({'case': item['case'], 'prompt_tokens': len(ids), 'context_limit': item['context_limit'], 'within_limit': len(ids) <= item['context_limit'] and len(ids) + item['output_limit'] <= item['model_max_len'], 'token_ids_sha256': hashlib.sha256(json.dumps(ids, separators=(',', ':')).encode()).hexdigest()})
pathlib.Path(sys.argv[2]).write_text(json.dumps({'tokenizer_class': type(tokenizer).__name__, 'cases': results}, indent=2) + '\\n')
"""

SELECTOR_PROGRAM = """
import json, pathlib, sys
from uuid import UUID
from lab.director.fake_llm import AgentContext
from lab.director.local_llm import LocalQwenProposalProvider
from lab.llm.native_runtime import MODEL_TURN_PROFILES
source = json.loads(pathlib.Path(sys.argv[1]).read_text())
provider = LocalQwenProposalProvider(run_id=UUID('7089275f-d7d2-4f46-b04b-27e809d15326'), owner='lab', principal_resolver=object(), runtime_database=pathlib.Path(sys.argv[2]).parent / 'unused.sqlite3', registry_entry_sha256='c' * 64)
selected = []
for item in source['cases']:
    context = AgentContext.model_validate_json(json.dumps(item['context']), strict=True)
    profile = MODEL_TURN_PROFILES[item['profile_id']]
    messages, count = provider._select_messages(context, profile, timeout_seconds=10.0)
    actual_context = json.loads(messages[1]['content'].split('\\n', 1)[1])
    kept = actual_context['recent_feedback']
    original_feedback = list(context.recent_feedback)
    selected.append({**item, 'messages': list(messages), 'preflight_prompt_tokens': count,
        'required_context_preserved': actual_context['task_cards'] == list(context.task_cards) and actual_context['champion_source'] == context.champion_source,
        'oldest_feedback_removed_first': kept == (original_feedback[-len(kept):] if kept else []),
        'feedback_before': len(original_feedback), 'feedback_after': len(kept)})
pathlib.Path(sys.argv[2]).write_text(json.dumps({'model_directory': source['model_directory'], 'cases': selected}))
"""


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--production", action="store_true",
                        help="Verify production token-based selection before independent tokenization")
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("refusing to overwrite evidence")
    paths = [ROOT / "lab/director/local_llm.py", ROOT / "lab/director/fake_llm.py",
             ROOT / "lab/llm/native_runtime.py", Path(__file__)]
    before = {str(path.relative_to(ROOT)): sha(path) for path in paths}
    pin = ModelPin.from_repository()
    manifest = json.loads((ROOT / "docs/ai-scientist/review-evidence/qwen-model-source.json").read_text())
    tokenizer_files = {}
    for item in manifest["files"]:
        if item["rfilename"] in {"config.json", "tokenizer.json", "tokenizer_config.json",
                                 "chat_template.jinja", "vocab.json", "merges.txt"}:
            actual = sha(pin.directory / item["rfilename"])
            if actual != item["verified_sha256"]:
                raise ValueError("pinned tokenizer source digest mismatch")
            tokenizer_files[item["rfilename"]] = actual
    record = {"schema": "local-qwen-tokenizer-review.v1", "checked_at": datetime.now(UTC).isoformat(),
              "scope": "CPU-only real pinned tokenizer/template over explicitly synthetic metadata and feedback. "
                       "No model weights, GPU, AOS or research proposal execution.",
              "source_sha256": before, "tokenizer_file_sha256": tokenizer_files,
              "production_selection": args.production, "checks": {}}
    cases = []
    cards = tuple(f"task=synthetic-tokenizer-review-{index}; family=EVT; weight=0.25; "
                  "allowed_signals=sensor_a,sensor_b; regime_signals=; sampling_s=60; "
                  "fit_budget_s=90; train_rows=128; eval_rows=128; "
                  "champion_seed0=0.321; champion_seed1=0.321; base=0.211; reference=0.411"
                  for index in range(4))
    source = baseline_candidate_source("robust_z").decode()
    for profile in (LOCAL_SMOKE_S1_PROFILE, LOCAL_SMOKE_S2_PROFILE):
        for feedback in (False, True):
            context = AgentContext(phase="LOOP", experiment_number=6 if feedback else 1,
                system=profile.system, move_type="features" if profile.system == "S2" else "hparam",
                task_cards=cards, champion_source=source,
                recent_feedback=tuple(
                    f"experiment={index}; verdict=DISCARD; " +
                    "measured development residual threshold sensor alarm variance score feedback; " * 15
                    for index in range(30)) if feedback else ())
            messages = LocalQwenProposalProvider._messages(context, profile)
            cases.append({"case": profile.system + ("_feedback" if feedback else "_initial"),
                          "context_limit": profile.max_context_tokens,
                          "output_limit": profile.max_output_tokens, "model_max_len": profile.model_max_len,
                          "enable_thinking": profile.enable_thinking, "messages": list(messages),
                          "context": context.model_dump(mode="json"), "profile_id": profile.profile_id})
    with tempfile.TemporaryDirectory(prefix="local-qwen-tokenizer-", dir=ROOT / "data/runtime") as temporary:
        runtime = Path(temporary)
        inputs, outputs = runtime / "inputs.json", runtime / "outputs.json"
        inputs.write_text(json.dumps({"model_directory": str(pin.directory), "cases": cases}))
        unit = f"swapp-review-qwen-tokenizer-{uuid4().hex}.service"
        command = ["/usr/bin/systemd-run", "--user", "--quiet", "--wait", "--pipe", "--collect",
                   f"--unit={unit}", "--description=SWAPP CPU tokenizer review",
                   f"--working-directory={ROOT}", "--property=MemoryMax=2G", "--property=MemorySwapMax=0",
                   "--property=CPUQuota=100%", "--property=TasksMax=64", "--property=RuntimeMaxSec=55",
                   "--property=TimeoutStopSec=5", "--property=KillMode=control-group",
                   "--property=UMask=0077", "--property=LimitFSIZE=1048576",
                   "--property=RestrictAddressFamilies=AF_UNIX",
                   "--setenv=HF_HUB_OFFLINE=1", "--setenv=TRANSFORMERS_OFFLINE=1",
                   "--setenv=USE_TORCH=0", "--setenv=USE_TF=0", "--setenv=CUDA_VISIBLE_DEVICES=",
                   "--setenv=OPENBLAS_NUM_THREADS=1", "--setenv=OMP_NUM_THREADS=1",
                   "--setenv=TOKENIZERS_PARALLELISM=false",
                   str(ROOT / "data/runtime/vllm/.venv/bin/python"), "-c", TOKENIZER_PROGRAM,
                   str(inputs), str(outputs)]
        selection_ok = True
        if args.production:
            selected = runtime / "selected.json"
            selector_unit = f"swapp-review-qwen-selection-{uuid4().hex}.service"
            selector_command = [argument.replace(unit, selector_unit) for argument in command[:-5]]
            selector_command.extend([str(ROOT / ".venv/bin/python"), "-c", SELECTOR_PROGRAM,
                                     str(inputs), str(selected)])
            selection = subprocess.run(selector_command, capture_output=True, text=True, timeout=65, check=False)
            selection_ok = selection.returncode == 0 and selected.exists()
            record["selection_process"] = {"unit": selector_unit, "exit_code": selection.returncode,
                                           "stderr": selection.stderr[-4096:]}
            record["checks"]["production_selector_exited_zero"] = selection_ok
            if selection_ok:
                selected_cases = json.loads(selected.read_text())["cases"]
                record["selection_cases"] = [
                    {key: item[key] for key in ("case", "preflight_prompt_tokens", "feedback_before",
                        "feedback_after", "required_context_preserved", "oldest_feedback_removed_first")}
                    for item in selected_cases]
                record["checks"]["required_context_and_feedback_order_preserved"] = all(
                    item["required_context_preserved"] and item["oldest_feedback_removed_first"]
                    for item in selected_cases)
                command[-2] = str(selected)
        completed = subprocess.run(command, capture_output=True, text=True, timeout=65, check=False) if selection_ok else None
        record.update({"unit": unit, "process_exit_code": completed.returncode if completed else None,
                       "stderr": completed.stderr[-4096:] if completed else "",
                       "input_sha256": sha(inputs)})
        record["checks"]["actual_tokenizer_exited_zero"] = completed is not None and completed.returncode == 0 and outputs.exists()
        if outputs.exists():
            record["tokenization"] = json.loads(outputs.read_text())
            record["checks"]["all_accepted_prompts_fit_declared_context"] = all(
                item["within_limit"] for item in record["tokenization"]["cases"])
            if args.production:
                counts = {item["case"]: item["prompt_tokens"] for item in record["tokenization"]["cases"]}
                record["checks"]["production_count_matches_independent_tokenizer"] = all(
                    counts[item["case"]] == item["preflight_prompt_tokens"]
                    for item in record["selection_cases"])
        after = {str(path.relative_to(ROOT)): sha(path) for path in paths}
        record["checks"]["source_unchanged"] = before == after
        record["source_after_sha256"] = after
    code = 0 if len(record["checks"]) == (6 if args.production else 3) and all(record["checks"].values()) else 1
    record["overall_exit_code"] = code
    with args.output.open("x") as handle:
        json.dump(record, handle, indent=2)
        handle.write("\n")
    print(json.dumps({"output": str(args.output), "checks": record["checks"],
                      "tokenization": record.get("tokenization"), "overall_exit_code": code}))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
