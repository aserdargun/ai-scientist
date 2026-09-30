"""Review-only S2 probe retaining marker counts, never generated token arrays.

The single production request is unchanged except return_token_ids=True, a vLLM
response metadata option. Existing runtime parsing, budgets and cleanup remain
in force. This is a diagnostic intervention, not an unmodified production run.
"""
from __future__ import annotations

import itertools
import json
from pathlib import Path
import sys

import review_local_qwen_s2_candidate as base
import lab.llm.native_runtime as native

ORIGINAL_READ = native._read_uds_json
ORIGINAL_CAPTURE = native._capture_response_metadata
ORIGINAL_INSTALLED = base.installed_source_hashes
MARKER_NAMES = ("<think>", "</think>", "<tool_call>", "</tool_call>", "<|im_end|>")
EXTRA_VLLM_PATHS = (
    "vllm/config/reasoning.py",
    "vllm/v1/worker/gpu/sample/thinking_budget.py",
    "vllm/v1/worker/gpu/sample/sampler.py",
    "vllm/v1/structured_output/__init__.py",
    "vllm/parser/qwen3.py",
    "vllm/entrypoints/openai/chat_completion/serving.py",
)


def marker_summary(response, markers):
    choices = response.get("choices")
    choice = choices[0] if isinstance(choices, list) and len(choices) == 1 else {}
    ids = choice.get("token_ids") if isinstance(choice, dict) else None
    prompt = response.get("prompt_token_ids")
    valid = isinstance(ids, list) and len(ids) <= base.LOCAL_SMOKE_S2_PROFILE.max_output_tokens and all(
        type(item) is int and 0 <= item < 1_000_000 for item in ids
    )
    usage = response.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    count = details.get("reasoning_tokens")
    result = {
        "token_array_valid": valid,
        "generated_token_count": len(ids) if valid else None,
        "reported_reasoning_tokens": count if type(count) is int else None,
        "generated_tokens_retained": False,
        "prompt_tokens_retained": False,
    }
    if valid:
        result["marker_positions"] = {
            name: [i for i, token in enumerate(ids) if token == marker][:32]
            for name, marker in markers.items()
        }
        result["marker_counts"] = {
            name: ids.count(marker) for name, marker in markers.items()
        }
        result["distinct_token_count"] = len(set(ids))
        result["maximum_identical_token_run"] = max(
            (sum(1 for _ in group) for _, group in itertools.groupby(ids)), default=0
        )
    if isinstance(prompt, list) and len(prompt) <= base.LOCAL_SMOKE_S2_PROFILE.max_context_tokens:
        result["prompt_marker_counts"] = {
            name: prompt.count(marker) for name, marker in markers.items()
        }
    return result


def read_with_ids(socket_path, method, path, payload, **kwargs):
    if method == "POST" and path == "/v1/chat/completions":
        payload = {**payload, "return_token_ids": True}
    return ORIGINAL_READ(socket_path, method, path, payload, **kwargs)


def capture_with_counts(response, transport_metadata):
    pin = native.ModelPin.from_repository()
    source = pin.directory / "tokenizer.json"
    expected = next(item[2] for item in pin.files if item[0] == source.name)
    raw = source.read_bytes()
    if base.digest(raw) != expected:
        raise ValueError("tokenizer marker source differs from pinned source")
    tokenizer = json.loads(raw)
    markers = {
        item["content"]: item["id"] for item in tokenizer["added_tokens"]
        if item["content"] in MARKER_NAMES
    }
    if set(markers) != set(MARKER_NAMES):
        raise ValueError("pinned tokenizer is missing diagnostic marker tokens")
    return {
        **ORIGINAL_CAPTURE(response, transport_metadata),
        "token_boundary_diagnostic": marker_summary(response, markers),
    }


def installed_sources():
    result = ORIGINAL_INSTALLED()
    site = base.ROOT / "data/runtime/vllm/.venv/lib/python3.12/site-packages"
    result.update({name: base.digest((site / name).read_bytes()) for name in EXTRA_VLLM_PATHS})
    return result


def configure():
    base.__file__ = __file__
    base.PROBE_SCHEMA = "local-qwen-s2-token-boundary-diagnostic.v1"
    base.SAFE_RESPONSE_KEYS |= {"token_boundary_diagnostic"}
    base.SOURCE_PATHS += (
        "docs/ai-scientist/review-evidence/review_local_qwen_s2_candidate.py",
        "docs/ai-scientist/review-evidence/review_local_qwen_s2_token_boundaries.py",
    )
    base.installed_source_hashes = installed_sources
    native._read_uds_json = read_with_ids
    native._capture_response_metadata = capture_with_counts


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-check"]:
        response = {"choices": [{"token_ids": [1, 4, 4, 2, 9]}],
                    "usage": {"completion_tokens_details": {"reasoning_tokens": 3}}}
        result = marker_summary(response, {"<think>": 1, "</think>": 2})
        assert result["generated_token_count"] == 5
        assert result["marker_positions"]["</think>"] == [3]
        assert result["maximum_identical_token_run"] == 2
        assert result["reported_reasoning_tokens"] == 3
        assert not marker_summary({"choices": [{"token_ids": [True]}]}, {})["token_array_valid"]
        print(json.dumps({"fixture_checks": 5, "passed": True, "model_invoked": False}))
    else:
        configure()
        raise SystemExit(base.main_cli())
