"""Synthetic profile-output.v2 boundaries; no AOS import or model execution."""

import base64
import hashlib
import json
import struct
from copy import deepcopy
from pathlib import Path

import pytest

from lab.llm.aos_profile_output import (
    FRAME_LIMIT,
    PROFILES,
    RESULT_LIMIT,
    ProfileOutputError,
    canonical,
    decode,
    instantiate_decider_schema,
    instantiate_result_schema,
    load_contract,
    project_profile_output,
    validate_result,
    write_bundle,
)

DIRECTORY = Path(__file__).resolve().parents[1] / "lab/llm/contracts/profile_output_v2"
DEPLOYMENT = "a" * 64
GENERATION = {
    "unit": "swapp-aos-gpu-turn-" + "b" * 32 + ".service",
    "invocation_id": "c" * 32,
    "main_pid": 123,
    "control_group": "/user.slice/swapp-aos-gpu-turn-" + "b" * 32 + ".service",
}


@pytest.fixture
def contract():
    bundle = json.loads((DIRECTORY / "bundle.json").read_bytes())
    return load_contract(DIRECTORY, hashlib.sha256(canonical(bundle)).hexdigest())


def request(profile, payload):
    return canonical(
        {
            "version": 1,
            "op": "infer",
            "request_id": "d" * 32,
            "profile_id": profile,
            "deployment_digest": DEPLOYMENT,
            "payload": payload,
        }
    )


def decider():
    raw = request(
        PROFILES[0],
        {
            "request": {
                "state": "state",
                "question": "question",
                "options": [{"id": "α", "label": "save"}, {"id": "b", "label": "cancel"}],
            }
        },
    )
    response = {
        "deployment_digest": DEPLOYMENT,
        "prediction": {"selected_option": "b", "probabilities": {"α": 0.75, "b": 0.25}},
        "metrics": {
            "latency_ms": 3.0,
            "load_ms": 0.0,
            "inference_ms": 2.0,
            "reused": False,
            "prepared_cpu": False,
            "input_tokens": 3,
            "peak_vram_bytes": 100,
            "broker_activation_load_ms": 4.0,
        },
    }
    return raw, response


def bonsai(contract, *, vision=False):
    if vision:
        raster = (
            b"\x89PNG\r\n\x1a\n"
            + struct.pack(">I4sII", 13, b"IHDR", 640, 360)
            + b"\x00\x00\x00\x00IEND\xaeB\x60\x82"
        )
        metadata = {
            "problem": "describe",
            "capture_id": "e" * 32,
            "state_version": 3,
            "width": 640,
            "height": 360,
            "sha256": hashlib.sha256(raster).hexdigest(),
        }
        user = [
            {"type": "text", "text": canonical(metadata).decode()},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64," + base64.b64encode(raster).decode()},
            },
        ]
        content = {key: metadata[key] for key in ("capture_id", "state_version", "width", "height")}
        content.update(
            {
                "needs_human": False,
                "elements": [
                    {
                        "role": "button",
                        "label": "SAVE",
                        "bbox": {"x": 0, "y": 0, "width": 20, "height": 20},
                    },
                    {
                        "role": "button",
                        "label": "CANCEL",
                        "bbox": {"x": 20, "y": 0, "width": 20, "height": 20},
                    },
                ],
            }
        )
    else:
        user = canonical(
            {"problem": "missing file", "evidence": [{"id": "e1", "kind": "missing"}]}
        ).decode()
        content = {
            "diagnosis": "missing",
            "evidence_refs": ["e1", "e1"],
            "assumptions": [],
            "revised_plan": [],
            "verification_criteria": ["exact_file_content"],
            "needs_human": True,
        }
    kind = "vision" if vision else "recovery"
    payload = {
        "model": "bonsai-" + DEPLOYMENT,
        "temperature": 0.0,
        "max_tokens": 32,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [
            {"role": "system", "content": "fixed profile prompt"},
            {"role": "user", "content": user},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "aos_visual_scene" if vision else "aos_recovery_plan",
                "strict": True,
                "schema": deepcopy(contract.schemas[f"bonsai-{kind}-content.schema.json"]),
            },
        },
    }
    response = {
        "choices": [
            {
                "finish_reason": "stop",
                "index": 0,
                "message": {"role": "assistant", "content": canonical(content).decode()},
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
        "model": "bonsai-" + DEPLOYMENT,
        "id": "chatcmpl-" + "S" * 32,
        "object": "chat.completion",
        "created": 1,
        "system_fingerprint": "b10709-9a9394a89",
        "timings": {
            "cache_n": 0,
            "prompt_n": 10,
            "predicted_n": 20,
            "prompt_ms": 10.0,
            "prompt_per_token_ms": 1.0,
            "prompt_per_second": 1000.0,
            "predicted_ms": 20.0,
            "predicted_per_token_ms": 1.0,
            "predicted_per_second": 1000.0,
        },
    }
    return request(PROFILES[2] if vision else PROFILES[1], payload), response


def project(raw, response, contract, **kwargs):
    return project_profile_output(
        raw, canonical(response), deepcopy(GENERATION), contract, **kwargs
    )


def test_decider_request_closure_and_non_argmax_selection(contract):
    raw, response = decider()
    result = project(raw, response, contract)
    assert result["response"]["prediction"]["selected_option"] == "b"
    assert result["usage"] == response["metrics"]
    schema = instantiate_decider_schema(raw, contract)
    prediction = schema["properties"]["prediction"]["properties"]
    assert prediction["selected_option"]["enum"] == ["α", "b"]
    assert prediction["probabilities"]["required"] == ["α", "b"]
    assert prediction["probabilities"]["additionalProperties"] is False
    assert instantiate_result_schema(raw, contract)["$ref"] == "#/$defs/" + PROFILES[0]
    assert (
        validate_result(raw, canonical(result), contract, expected_generation=GENERATION) == result
    )


@pytest.mark.parametrize("vision", [False, True])
def test_bonsai_projection_preserves_content_and_validates_native_semantics(contract, vision):
    raw, response = bonsai(contract, vision=vision)
    result = project(raw, response, contract)
    assert set(result["response"]) == {"choices", "usage", "model"}
    assert result["response"]["model"] == response["model"]
    assert result["response"]["choices"][0]["message"] == response["choices"][0]["message"]
    assert result["usage"] == {"prompt_tokens": 10, "completion_tokens": 20}
    assert validate_result(raw, canonical(result), contract) == result


@pytest.mark.parametrize("vision", [False, True])
@pytest.mark.parametrize("optional", [set(), {"model"}, {"role"}, {"model", "role"}])
def test_note80_optional_fields_are_preserved_without_invention(contract, vision, optional):
    raw, response = bonsai(contract, vision=vision)
    if "model" not in optional:
        del response["model"]
    if "role" not in optional:
        del response["choices"][0]["message"]["role"]
    result = project(raw, response, contract)
    assert ("model" in result["response"]) == ("model" in optional)
    assert result["response"]["choices"][0]["message"] == response["choices"][0]["message"]
    signed = canonical(result)
    assert canonical(validate_result(raw, signed, contract)) == signed
    assert hashlib.sha256(
        canonical(contract.schemas["bonsai-response.schema.json"])
    ).hexdigest() == ("c07e0f14089840858b6a1d688fac81ba0dc9ae07870ef2c6a501ab110080467d")


@pytest.mark.parametrize("empty_reasoning", [None, ""])
def test_source_defined_empty_metadata_and_paired_draft_counts(contract, empty_reasoning):
    raw, response = bonsai(contract)
    response["system_fingerprint"] = "source-compatible-build-metadata"
    response["timings"].update({"draft_n": 3, "draft_n_accepted": 2})
    response["choices"][0]["message"]["reasoning_content"] = empty_reasoning
    result = project(raw, response, contract)
    assert "reasoning_content" not in result["response"]["choices"][0]["message"]
    assert result["response"]["model"] == response["model"]
    assert validate_result(raw, canonical(result), contract) == result
    for draft, accepted in ((0, 0), (1, 2), (True, 0)):
        response["timings"].update({"draft_n": draft, "draft_n_accepted": accepted})
        with pytest.raises(ProfileOutputError):
            project(raw, response, contract)


@pytest.mark.parametrize(
    "path,value",
    [
        (("deployment_digest",), "f" * 64),
        (("prediction", "selected_option"), "save"),
        (("prediction", "probabilities", "save"), 0),
        (("prediction", "probabilities", "α"), True),
        (("prediction", "probabilities", "α"), 0.5),
        (("metrics", "input_tokens"), 3.0),
        (("metrics", "input_tokens"), True),
        (("metrics", "input_tokens"), 1537),
        (("metrics", "reused"), True),
        (("metrics", "prepared_cpu"), 0),
        (("metrics", "latency_ms"), 720001),
        (("metrics", "broker_activation_load_ms"), 600001),
        (("metrics", "peak_vram_bytes"), 2**53),
        (("metrics", "invented"), 0),
    ],
)
def test_decider_rejects_unbound_values(contract, path, value):
    raw, response = decider()
    target = response
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ProfileOutputError):
        project(raw, response, contract)


@pytest.mark.parametrize(
    "path,value",
    [
        (("model",), "bonsai-" + "f" * 64),
        (("system_fingerprint",), ""),
        (("system_fingerprint",), "α" * 257),
        (("id",), "chatcmpl-too-short"),
        (("unknown",), None),
        (("choices", 0, "index"), 0.0),
        (("choices", 0, "finish_reason"), "tool_calls"),
        (("choices", 0, "message", "role"), "tool"),
        (("choices", 0, "message", "tool_calls"), []),
        (("choices", 0, "message", "refusal"), "refused"),
        (("choices", 0, "message", "reasoning_content"), "hidden"),
        (("usage", "completion_tokens"), True),
        (("usage", "completion_tokens"), 20.0),
        (("usage", "completion_tokens"), 33),
        (("usage", "total_tokens"), 31),
        (("usage", "prompt_tokens_details", "cached_tokens"), 11),
        (("usage", "prompt_tokens_details", "unknown"), 0),
        (("timings", "cache_n"), 0.0),
        (("timings", "prompt_n"), 16385),
        (("timings", "predicted_n"), 33),
        (("timings", "draft_n_accepted"), 0),
        (("timings", "unknown"), 0),
    ],
)
def test_raw_bonsai_allowlist_rejects_hidden_actions_and_unbounded_usage(contract, path, value):
    raw, response = bonsai(contract)
    target = response
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ProfileOutputError):
        project(raw, response, contract)


@pytest.mark.parametrize(
    "content",
    [
        '{"needs_human":true,"needs_human":false}',
        '{"needs_human":NaN}',
        "```json\n{}\n```",
        "{} {}",
    ],
)
def test_inner_json_strict_bytes(contract, content):
    raw, response = bonsai(contract)
    response["choices"][0]["message"]["content"] = content
    with pytest.raises(ProfileOutputError):
        project(raw, response, contract)


@pytest.mark.parametrize(
    "vision,mutation",
    [
        (False, "unknown"),
        (False, "evidence"),
        (False, "human"),
        (False, "action"),
        (True, "unknown"),
        (True, "capture"),
        (True, "state_float"),
        (True, "overlap"),
        (True, "box_bool"),
        (True, "box_unknown"),
        (True, "human"),
    ],
)
def test_native_content_bindings(contract, vision, mutation):
    raw, response = bonsai(contract, vision=vision)
    message = response["choices"][0]["message"]
    content = json.loads(message["content"])
    if mutation == "unknown":
        content["extra"] = 1
    elif mutation == "evidence":
        content["evidence_refs"] = ["another"]
    elif mutation == "human":
        content["needs_human"] = not content["needs_human"]
    elif mutation == "action":
        content["needs_human"] = False
        content["revised_plan"] = [
            {"order": n, "action": "execute_anything", "expected_result": "x"} for n in (1, 2, 3)
        ]
    elif mutation == "capture":
        content["capture_id"] = "f" * 32
    elif mutation == "state_float":
        content["state_version"] = 3.0
    elif mutation == "overlap":
        content["elements"][1]["bbox"]["x"] = 19
    elif mutation == "box_bool":
        content["elements"][0]["bbox"]["y"] = False
    else:
        content["elements"][0]["bbox"]["z"] = 0
    message["content"] = canonical(content).decode()
    with pytest.raises(ProfileOutputError):
        project(raw, response, contract)


def test_alt_choice_context_overflow_and_schema_swap(contract):
    raw, response = bonsai(contract)
    alternate = deepcopy(response)
    alternate["choices"].append(deepcopy(alternate["choices"][0]))
    with pytest.raises(ProfileOutputError):
        project(raw, alternate, contract)
    with pytest.raises(ProfileOutputError):
        project(raw, response, contract, context_tokens=29)
    changed = json.loads(raw)
    changed["payload"]["response_format"]["json_schema"]["schema"]["additionalProperties"] = True
    with pytest.raises(ProfileOutputError):
        project(canonical(changed), response, contract)


def test_stored_result_never_reprojects_or_normalizes_signed_bytes(contract):
    raw, response = bonsai(contract)
    result = project(raw, response, contract)
    altered = deepcopy(result)
    altered["usage"]["completion_tokens"] += 1
    with pytest.raises(ProfileOutputError):
        validate_result(raw, canonical(altered), contract)
    altered = deepcopy(result)
    altered["response"]["model"] = "bonsai-" + "f" * 64
    with pytest.raises(ProfileOutputError):
        validate_result(raw, canonical(altered), contract)
    altered = deepcopy(result)
    altered["response"]["choices"][0]["message"]["reasoning_content"] = None
    with pytest.raises(ProfileOutputError):
        validate_result(raw, canonical(altered), contract)
    with pytest.raises(ProfileOutputError):
        validate_result(raw, json.dumps(result, indent=2).encode(), contract)
    wrong = {**GENERATION, "main_pid": 124}
    with pytest.raises(ProfileOutputError):
        validate_result(raw, canonical(result), contract, expected_generation=wrong)
    with pytest.raises(ProfileOutputError):
        validate_result(raw, b" " * (RESULT_LIMIT + 1), contract)
    with pytest.raises(ProfileOutputError):
        project_profile_output(raw, b"{}" + b" " * FRAME_LIMIT, GENERATION, contract)


@pytest.mark.parametrize("raw", [b'{"n":1,"n":2}', b'{"n":NaN}', b'{"n":1e999}'])
def test_raw_decoder_rejects_duplicate_and_nonfinite(raw):
    with pytest.raises(ProfileOutputError):
        decode(raw, limit=128)


def test_bundle_pins_source_and_exact_schema_files(contract, tmp_path):
    for name in (*contract.schemas, "bundle.json"):
        (tmp_path / name).write_bytes((DIRECTORY / name).read_bytes())
    before = write_bundle(tmp_path)
    assert before == contract.bundle_sha256
    assert load_contract(tmp_path, before).bundle_sha256 == before
    target = tmp_path / "bonsai-usage.schema.json"
    altered = json.loads(target.read_bytes())
    altered["additionalProperties"] = True
    target.write_bytes(canonical(altered))
    with pytest.raises(ProfileOutputError):
        load_contract(tmp_path, before)
