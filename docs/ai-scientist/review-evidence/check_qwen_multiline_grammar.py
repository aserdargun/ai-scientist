"""Offline xgrammar acceptance check for a normal multiline proposal source."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

from lab.director.local_llm import (
    _CANDIDATE_PROPOSAL_SCHEMA,
    LocalQwenProposalProvider,
)
from lab.llm.native_runtime import ModelPin

PINNED_MODEL_ROOT = Path("/home/cachyos/ai-scientist")
TOKENIZER_RELATIVE_DIRECTORY = Path(
    "models/Qwen3.5-9B/c202236235762e1c871ad0ccb60c8ee5ba337b9a"
)
TOKENIZER_DIRECTORY = PINNED_MODEL_ROOT / TOKENIZER_RELATIVE_DIRECTORY
VLLM_PYTHON = Path("/home/cachyos/ai-scientist/data/runtime/vllm/.venv/bin/python")
DIAGNOSTIC_RECEIPT = Path(__file__).with_name("qwen-multiline-grammar-diagnostics.v1.json")
SOURCE = (
    "import numpy as np\n\n"
    "from harness.contracts import AlarmPolicy\n\n"
    "class Candidate:\n"
    "    def fit(self, train, ctx):\n"
    "        self.center = train.mean().to_numpy()\n"
    "    def score(self, data):\n"
    "        return np.abs(data.to_numpy() - self.center).mean(axis=1)\n"
    "    def alarm_policy(self, train_scores):\n"
    "        threshold = float(np.quantile(train_scores, 0.99))\n"
    "        return AlarmPolicy(threshold, 0.0, 1)\n\n"
    "def build_candidate():\n"
    "    return Candidate()\n"
)
WORKER = r"""
import hashlib, json, sys
from importlib.metadata import version
import xgrammar
from transformers import AutoTokenizer
from xgrammar import GrammarCompiler, GrammarMatcher, TokenizerInfo

request = json.load(sys.stdin)
tokenizer = AutoTokenizer.from_pretrained(
    request["model_directory"], local_files_only=True, trust_remote_code=False
)
tokenizer_info = TokenizerInfo.from_huggingface(tokenizer)
compiler = GrammarCompiler(tokenizer_info, max_threads=1, cache_enabled=False)
grammar = compiler.compile_json_schema(request["schema"], any_whitespace=True)
matcher = GrammarMatcher(grammar, terminate_without_stop_token=True)
payload_tokens = tokenizer.encode(request["proposal_json"], add_special_tokens=False)
accepted = True
rejected_at = None
for index, token in enumerate(payload_tokens):
    if not matcher.accept_token(token):
        accepted = False
        rejected_at = index
        break
schema_diagnostics = {}
for name, schema in request["diagnostic_schemas"].items():
    diagnostic = GrammarMatcher(
        compiler.compile_json_schema(schema, any_whitespace=True),
        terminate_without_stop_token=True,
    )
    diagnostic_accepted = True
    diagnostic_rejected_at = None
    for index, token in enumerate(payload_tokens):
        if not diagnostic.accept_token(token):
            diagnostic_accepted = False
            diagnostic_rejected_at = index
            break
    schema_diagnostics[name] = {
        "accepted": diagnostic_accepted,
        "completed": diagnostic.is_completed(),
        "rejected_token_index": diagnostic_rejected_at,
    }
print(json.dumps({
    "accepted": accepted,
    "completed": matcher.is_completed(),
    "rejected_at": rejected_at,
    "schema_diagnostics": schema_diagnostics,
    "xgrammar_module": xgrammar.__file__,
    "xgrammar_version": version("xgrammar"),
    "tokenizer_vocab_size": tokenizer_info.vocab_size,
    "tokenizer_payload_tokens": len(tokenizer.encode(request["proposal_json"])),
}, sort_keys=True, separators=(",", ":")))
"""


def main() -> None:
    if not VLLM_PYTHON.is_file() or not VLLM_PYTHON.resolve(strict=True).is_file():
        raise RuntimeError("pinned local xgrammar Python is unavailable")
    if TOKENIZER_DIRECTORY.is_symlink() or not TOKENIZER_DIRECTORY.is_dir():
        raise RuntimeError("pinned local tokenizer directory is unavailable")
    proposal = {
        "hypothesis": "Use a robust sensor center",
        "move_type": "features",
        "candidate_source": SOURCE,
        "predicted_delta": 0.0,
    }
    # Canonical sorted JSON follows XGrammar's schema member order.
    proposal_json = json.dumps(
        proposal, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    decoded_proposal = json.loads(proposal_json)
    if decoded_proposal.get("candidate_source") != SOURCE:
        raise RuntimeError("JSON escaped line breaks did not roundtrip as source newlines")
    LocalQwenProposalProvider._parse_proposal(proposal_json, "features")
    oversized_source = dict(proposal, candidate_source="x" * 6_001)
    oversized_hypothesis = dict(proposal, hypothesis="h" * 385)
    source_bound_rejected = _is_local_parse_rejected(oversized_source)
    hypothesis_bound_rejected = _is_local_parse_rejected(oversized_hypothesis)
    if not source_bound_rejected or not hypothesis_bound_rejected:
        raise RuntimeError("trusted local parser did not enforce its compact text bounds")
    diagnostic_schemas = {}
    for name, included in (
        ("source_min_and_max", ("minLength", "maxLength")),
        ("source_min_only", ("minLength",)),
        ("source_max_only", ("maxLength",)),
    ):
        schema = json.loads(
            json.dumps(
                _CANDIDATE_PROPOSAL_SCHEMA,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
        )
        schema["properties"]["candidate_source"].update({"minLength": 1, "maxLength": 6_000})
        for bound in {"minLength", "maxLength"} - set(included):
            schema["properties"]["candidate_source"].pop(bound, None)
        diagnostic_schemas[name] = schema
    request = {
        "schema": _CANDIDATE_PROPOSAL_SCHEMA,
        "model_directory": str(TOKENIZER_DIRECTORY),
        "proposal_json": proposal_json,
        "diagnostic_schemas": diagnostic_schemas,
    }
    child_environment = {
        "PATH": "/usr/bin:/bin",
        "CUDA_VISIBLE_DEVICES": "",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "TOKENIZERS_PARALLELISM": "false",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "PYTHONNOUSERSITE": "1",
    }
    child = subprocess.run(
        [str(VLLM_PYTHON), "-c", WORKER],
        input=json.dumps(request, sort_keys=True, separators=(",", ":")),
        text=True,
        capture_output=True,
        check=False,
        timeout=180,
        env=child_environment,
    )
    if child.returncode != 0:
        raise RuntimeError(f"pinned xgrammar check failed ({child.returncode})")
    result = json.loads(child.stdout)
    diagnostics = result.get("schema_diagnostics")
    if result.get("accepted") is not True or result.get("completed") is not True:
        raise RuntimeError(
            "pinned xgrammar did not accept tokenizer tokens for multiline candidate JSON "
            f"(accepted={result.get('accepted')!r}, completed={result.get('completed')!r}, "
            f"rejected_token_index={result.get('rejected_at')!r})"
        )
    if not isinstance(diagnostics, dict) or set(diagnostics) != {
        "source_min_and_max",
        "source_min_only",
        "source_max_only",
    } or any(
        not isinstance(outcome, dict) or outcome.get("accepted") is not False
        for outcome in diagnostics.values()
    ):
        raise RuntimeError("source length-bound grammar diagnosis did not reproduce")
    recorded_pin = ModelPin.from_repository()
    pin = replace(recorded_pin, directory=TOKENIZER_DIRECTORY)
    expected_tokenizer_files = {
        name: (size, digest)
        for name, size, digest in recorded_pin.files
        if name in {"tokenizer.json", "tokenizer_config.json"}
    }
    if set(expected_tokenizer_files) != {"tokenizer.json", "tokenizer_config.json"}:
        raise RuntimeError("pinned tokenizer file records are incomplete")
    for name, (expected_size, expected_sha256) in expected_tokenizer_files.items():
        path = TOKENIZER_DIRECTORY / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size != expected_size:
            raise RuntimeError("pinned tokenizer file does not match its source record")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected_sha256:
            raise RuntimeError("pinned tokenizer file does not match its source record")
    receipt = {
        "schema": "qwen-multiline-grammar-diagnostics.v1",
        "exit_status": 0,
        "model_revision": pin.revision,
        "model_manifest_sha256": pin.digest,
        "tokenizer_directory": TOKENIZER_RELATIVE_DIRECTORY.as_posix(),
        "candidate_schema_sha256": hashlib.sha256(
            json.dumps(
                _CANDIDATE_PROPOSAL_SCHEMA,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest(),
        "proposal_json_sha256": hashlib.sha256(proposal_json.encode("utf-8")).hexdigest(),
        "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "command": "check_qwen_multiline_grammar.py",
        "tokenizer_file_sha256": {
            name: digest for name, (_, digest) in sorted(expected_tokenizer_files.items())
        },
        "python_source_characters": len(SOURCE),
        "python_source_newline_count": SOURCE.count("\n"),
        "current_schema": {
            "accepted": result["accepted"],
            "completed": result["completed"],
        },
        "trusted_parser_rejects_source_6001": source_bound_rejected,
        "trusted_parser_rejects_hypothesis_385": hypothesis_bound_rejected,
        "runtime_gpu_started": False,
        "xgrammar_version": result["xgrammar_version"],
        "tokenizer_payload_tokens": result["tokenizer_payload_tokens"],
        "schema_diagnostics": {
            name: {
                "schema_sha256": hashlib.sha256(
                    json.dumps(
                        schema,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        allow_nan=False,
                    ).encode("utf-8")
                ).hexdigest(),
                **outcome,
            }
            for name, schema in diagnostic_schemas.items()
            for outcome in (diagnostics[name],)
        },
    }
    diagnostic_receipt = {
        "schema": "qwen-multiline-grammar-diagnostics.v1",
        "exit_status": 0,
        "command": "check_qwen_multiline_grammar.py",
        "driver_sha256": receipt["driver_sha256"],
        "model_revision": pin.revision,
        "model_manifest_sha256": pin.digest,
        "tokenizer_file_sha256": receipt["tokenizer_file_sha256"],
        "candidate_schema_sha256": receipt["candidate_schema_sha256"],
        "proposal_json_sha256": receipt["proposal_json_sha256"],
        "python_source_characters": len(SOURCE),
        "python_source_newline_count": SOURCE.count("\n"),
        "schema_diagnostics": {
            name: {
                "schema_sha256": hashlib.sha256(
                    json.dumps(
                        schema,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        allow_nan=False,
                    ).encode("utf-8")
                ).hexdigest(),
                **outcome,
            }
            for name, schema in diagnostic_schemas.items()
            for outcome in (diagnostics[name],)
        },
        "xgrammar_version": result["xgrammar_version"],
        "tokenizer_payload_tokens": result["tokenizer_payload_tokens"],
        "current_schema": receipt["current_schema"],
        "trusted_parser_rejects_source_6001": source_bound_rejected,
        "trusted_parser_rejects_hypothesis_385": hypothesis_bound_rejected,
        "runtime_gpu_started": False,
    }
    DIAGNOSTIC_RECEIPT.write_text(
        json.dumps(diagnostic_receipt, sort_keys=True, separators=(",", ":")) + "\n"
    )
    os.chmod(DIAGNOSTIC_RECEIPT, 0o600)
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))


def _is_local_parse_rejected(proposal: dict[str, object]) -> bool:
    try:
        LocalQwenProposalProvider._parse_proposal(
            json.dumps(proposal, separators=(",", ":")), "features"
        )
    except (ValueError, TypeError):
        return True
    return False


if __name__ == "__main__":
    main()
