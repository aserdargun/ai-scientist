"""Opt-in profile-output.v2 validation, before result persistence or hashing.

No AOS import, model execution, admission or cleanup authority lives here.
The caller supplies the persisted canonical infer request and an already pinned
bundle. Native metadata is projected only through the explicit reviewed allowlist.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import struct
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

NAME = "aos-scientist-profile-output.v2"
VERSION = 2
SAFE_INTEGER = 2**53 - 1
RESULT_LIMIT = 96 * 1024
FRAME_LIMIT = 128 * 1024
CONTENT_LIMIT = 65536
PROFILES = ("aos.decider.turn.v1", "aos.bonsai.recovery.v1", "aos.bonsai.vision.v1")
SCHEMA_FILES = (
    "decider-response.template.schema.json",
    "decider-usage.schema.json",
    "bonsai-response.schema.json",
    "bonsai-usage.schema.json",
    "bonsai-recovery-content.schema.json",
    "bonsai-vision-content.schema.json",
    "result.schema.json",
    "bonsai-raw.schema.json",
)
TIMING_COUNTS = {"cache_n", "prompt_n", "predicted_n"}
TIMING_NUMBERS = {
    "prompt_ms",
    "prompt_per_token_ms",
    "prompt_per_second",
    "predicted_ms",
    "predicted_per_token_ms",
    "predicted_per_second",
}
RECOVERY_ACTIONS = (
    "observe_workspace",
    "create_authorized_file",
    "verify_exact_content",
)


class ProfileOutputError(ValueError):
    """A closed output contract or original-request binding failed."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ProfileOutputError(message)


def canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ProfileOutputError("invalid canonical JSON") from exc


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def _constant(_value: str) -> Any:
    raise ProfileOutputError("nonfinite JSON number")


def decode(raw: bytes, *, limit: int, canonical_required: bool = False) -> dict[str, Any]:
    _require(type(raw) is bytes and 0 < len(raw) <= limit, "JSON byte bound")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ProfileOutputError("invalid strict JSON") from exc
    _require(type(value) is dict, "JSON object required")
    encoded = canonical(value)  # Also rejects overflow-to-infinity and lone surrogates.
    if canonical_required:
        _require(raw == encoded, "noncanonical signed JSON")
    return cast(dict[str, Any], value)


def _object(value: Any, required: set[str], optional: set[str] | None = None) -> dict[str, Any]:
    _require(type(value) is dict, "object required")
    _require(required <= set(value) <= required | (optional or set()), "unknown/missing field")
    return cast(dict[str, Any], value)


def _integer(value: Any, lower: int = 0, upper: int = SAFE_INTEGER) -> int:
    _require(type(value) is int and lower <= value <= upper, "integer lexeme or bound")
    return cast(int, value)


def _number(value: Any, upper: float) -> float:
    _require(type(value) in (int, float), "nonboolean number required")
    try:
        valid = math.isfinite(value) and 0 <= value <= upper
    except OverflowError:
        valid = False
    _require(valid, "finite number bound")
    return float(value)


def _string(value: Any, minimum: int = 0, maximum: int = CONTENT_LIMIT) -> str:
    _require(type(value) is str and minimum <= len(value) <= maximum, "string bound")
    return cast(str, value)


def _hex(value: Any, length: int = 64) -> str:
    _require(
        type(value) is str and re.fullmatch(r"[a-f0-9]{" + str(length) + "}", value) is not None,
        "hex identity",
    )
    return cast(str, value)


@dataclass(frozen=True)
class OutputContract:
    """Trusted local schema bundle; construct with load_contract in production."""

    bundle_sha256: str
    schemas: dict[str, dict[str, Any]]

    def content_pin(self, profile_id: str) -> str:
        name = {
            PROFILES[0]: "decider-response.template.schema.json",
            PROFILES[1]: "bonsai-recovery-content.schema.json",
            PROFILES[2]: "bonsai-vision-content.schema.json",
        }.get(profile_id)
        if name is None:
            raise ProfileOutputError("unsupported profile")
        return hashlib.sha256(canonical(self.schemas[name])).hexdigest()


def load_contract(directory: Path, expected_bundle_sha256: str) -> OutputContract:
    """Verify the entire bundle and both source pins, without importing AOS."""
    _hex(expected_bundle_sha256)
    bundle = decode((directory / "bundle.json").read_bytes(), limit=CONTENT_LIMIT)
    _object(
        bundle,
        {"name", "version", "schemas", "adapter_source_sha256", "request_binding_source_sha256"},
    )
    _require(
        bundle["name"] == NAME and type(bundle["version"]) is int and bundle["version"] == VERSION,
        "output contract version",
    )
    _require(
        hashlib.sha256(canonical(bundle)).hexdigest() == expected_bundle_sha256,
        "bundle digest mismatch",
    )
    hashes = _object(bundle["schemas"], set(SCHEMA_FILES))
    source_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    _require(
        bundle["adapter_source_sha256"] == source_hash
        and bundle["request_binding_source_sha256"] == source_hash,
        "adapter source changed",
    )
    schemas = {}
    for name in SCHEMA_FILES:
        _hex(hashes[name])
        schema = decode((directory / name).read_bytes(), limit=FRAME_LIMIT)
        _require(hashlib.sha256(canonical(schema)).hexdigest() == hashes[name], "schema changed")
        schemas[name] = schema
    return OutputContract(expected_bundle_sha256, schemas)


def _request(raw: bytes) -> dict[str, Any]:
    request = decode(raw, limit=FRAME_LIMIT - 1, canonical_required=True)
    _object(request, {"version", "op", "request_id", "profile_id", "deployment_digest", "payload"})
    _require(
        type(request["version"]) is int
        and request["version"] == 1
        and request["op"] == "infer"
        and request["profile_id"] in PROFILES,
        "infer identity",
    )
    _hex(request["request_id"], 32)
    _hex(request["deployment_digest"])
    _require(type(request["payload"]) is dict, "payload object")
    return request


def _options(request: dict[str, Any]) -> list[str]:
    body = _object(request["payload"], {"request"})["request"]
    body = _object(body, {"state", "question", "options"})
    for key, limit in (("state", 16384), ("question", 1024)):
        text = _string(body[key], 1)
        _require(bool(text.strip()) and len(text.encode("utf-8")) <= limit, "Decider text bound")
    options = body["options"]
    _require(type(options) is list and 2 <= len(options) <= 10, "option count")
    ids, labels = [], []
    for item in options:
        _object(item, {"id", "label"})
        ids.append(_string(item["id"], 1, FRAME_LIMIT))
        labels.append(_string(item["label"], 1, FRAME_LIMIT))
    _require(
        all(value.strip() for value in ids + labels)
        and len(set(ids)) == len(ids)
        and len(set(labels)) == len(labels),
        "option identity",
    )
    return ids


def instantiate_decider_schema(request_bytes: bytes, contract: OutputContract) -> dict[str, Any]:
    """Only persisted canonical request IDs may specialize this pinned template."""
    request = _request(request_bytes)
    _require(request["profile_id"] == PROFILES[0], "Decider profile required")
    ids = _options(request)
    schema = deepcopy(contract.schemas["decider-response.template.schema.json"])
    prediction = schema["properties"]["prediction"]["properties"]
    prediction["selected_option"] = {"type": "string", "enum": ids}
    prediction["probabilities"] = {
        "type": "object",
        "properties": {key: {"type": "number", "minimum": 0, "maximum": 1} for key in ids},
        "required": ids,
        "additionalProperties": False,
    }
    schema["properties"]["deployment_digest"] = {
        "type": "string",
        "const": request["deployment_digest"],
    }
    return schema


def _decider(request: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    _object(response, {"deployment_digest", "prediction", "metrics"})
    _require(response["deployment_digest"] == request["deployment_digest"], "Decider deployment")
    ids = _options(request)
    prediction = _object(response["prediction"], {"selected_option", "probabilities"})
    selected = _string(prediction["selected_option"], 1)
    _require(selected in ids, "unknown selected ID")
    probabilities = _object(prediction["probabilities"], set(ids))
    total = math.fsum(_number(value, 1) for value in probabilities.values())
    _require(abs(total - 1) <= 1e-6, "probability sum")
    metrics = _object(
        response["metrics"],
        {
            "latency_ms",
            "load_ms",
            "inference_ms",
            "reused",
            "prepared_cpu",
            "input_tokens",
            "peak_vram_bytes",
            "broker_activation_load_ms",
        },
    )
    for key in ("latency_ms", "load_ms", "inference_ms"):
        _number(metrics[key], 720000)
    _number(metrics["broker_activation_load_ms"], 600000)
    _require(
        metrics["reused"] is False and metrics["prepared_cpu"] is False, "fresh session required"
    )
    _integer(metrics["input_tokens"], 1, 1536)
    _integer(metrics["peak_vram_bytes"])
    return deepcopy(metrics)


def _generation(value: Any) -> dict[str, Any]:
    value = _object(value, {"unit", "invocation_id", "main_pid", "control_group"})
    unit = _string(value["unit"], 1, 255)
    _require(
        re.fullmatch(r"swapp-aos-gpu-turn-[a-f0-9]{32}\.service", unit) is not None, "worker unit"
    )
    _hex(value["invocation_id"], 32)
    _integer(value["main_pid"], 2, 2**31 - 1)
    cgroup = _string(value["control_group"], 1, 512)
    parts = cgroup.split("/")
    _require(
        cgroup.startswith("/")
        and parts[-1] == unit
        and not any(part in {".", "..", ""} for part in parts[1:]),
        "worker cgroup",
    )
    return deepcopy(cast(dict[str, Any], value))


def _bonsai_context(
    request: dict[str, Any], contract: OutputContract, context_tokens: int, max_output_tokens: int
) -> tuple[dict[str, Any], int]:
    _integer(context_tokens, 1, 16384)
    _integer(max_output_tokens, 1, 512)
    body = _object(
        request["payload"],
        {
            "model",
            "temperature",
            "max_tokens",
            "stream",
            "chat_template_kwargs",
            "messages",
            "response_format",
        },
    )
    _require(
        body["model"] == "bonsai-" + request["deployment_digest"]
        and type(body["temperature"]) in (int, float)
        and body["temperature"] == 0
        and body["stream"] is False,
        "fixed Bonsai request",
    )
    _object(body["chat_template_kwargs"], {"enable_thinking"})
    _require(body["chat_template_kwargs"]["enable_thinking"] is False, "thinking disabled")
    output_limit = _integer(body["max_tokens"], 1, max_output_tokens)
    fmt = _object(body["response_format"], {"type", "json_schema"})
    spec = _object(fmt["json_schema"], {"name", "strict", "schema"})
    role = "recovery" if request["profile_id"] == PROFILES[1] else "vision"
    expected_name = "aos_recovery_plan" if role == "recovery" else "aos_visual_scene"
    _require(
        fmt["type"] == "json_schema"
        and spec["strict"] is True
        and spec["name"] == expected_name
        and hashlib.sha256(canonical(spec["schema"])).hexdigest()
        == contract.content_pin(request["profile_id"]),
        "inner schema pin",
    )
    messages = body["messages"]
    _require(type(messages) is list and len(messages) == 2, "fixed message count")
    for message, expected_role in zip(messages, ("system", "user"), strict=True):
        _object(message, {"role", "content"})
        _require(message["role"] == expected_role, "message role")
    _string(messages[0]["content"], 1)
    if role == "recovery":
        text = _string(messages[1]["content"], 1)
        context = decode(text.encode("utf-8"), limit=CONTENT_LIMIT)
        _object(context, {"problem", "evidence"})
        _string(context["problem"], 1)
        evidence = context["evidence"]
        _require(type(evidence) is list and 1 <= len(evidence) <= 4, "recovery evidence count")
        for item in evidence:
            _require(type(item) is dict and "id" in item, "trusted evidence ID")
            _string(item["id"], 1)
        return context, output_limit
    content = messages[1]["content"]
    _require(type(content) is list and len(content) == 2, "vision message content")
    _object(content[0], {"type", "text"})
    _object(content[1], {"type", "image_url"})
    image = _object(content[1]["image_url"], {"url"})
    _require(content[0]["type"] == "text" and content[1]["type"] == "image_url", "vision parts")
    text = _string(content[0]["text"], 1)
    context = decode(text.encode("utf-8"), limit=CONTENT_LIMIT)
    _object(context, {"problem", "capture_id", "width", "height", "sha256", "state_version"})
    _string(context["problem"], 1)
    _hex(context["capture_id"], 32)
    _hex(context["sha256"])
    _integer(context["state_version"])
    _integer(context["width"], 640, 640)
    _integer(context["height"], 360, 360)
    url = _string(image["url"], 1, 60022)
    _require(url.startswith("data:image/png;base64,"), "PNG data URL")
    try:
        raster = base64.b64decode(url[22:], validate=True)
    except ValueError as exc:
        raise ProfileOutputError("invalid image encoding") from exc
    _require(
        len(raster) >= 24
        and raster[:8] == b"\x89PNG\r\n\x1a\n"
        and struct.unpack(">I4s", raster[8:16]) == (13, b"IHDR")
        and struct.unpack(">II", raster[16:24]) == (640, 360)
        and raster[-12:] == b"\x00\x00\x00\x00IEND\xaeB\x60\x82"
        and hashlib.sha256(raster).hexdigest() == context["sha256"],
        "capture bytes or hash",
    )
    return context, output_limit


def _recovery(value: dict[str, Any], context: dict[str, Any]) -> None:
    _object(
        value,
        {
            "diagnosis",
            "evidence_refs",
            "assumptions",
            "revised_plan",
            "verification_criteria",
            "needs_human",
        },
    )
    _string(value["diagnosis"], 1, 600)
    refs, assumptions, plan = value["evidence_refs"], value["assumptions"], value["revised_plan"]
    _require(type(refs) is list and 1 <= len(refs) <= 4, "evidence_refs count")
    for ref in refs:
        _string(ref)
    _require(set(refs) == {item["id"] for item in context["evidence"]}, "evidence binding")
    _require(type(assumptions) is list and len(assumptions) <= 3, "assumptions count")
    for assumption in assumptions:
        _string(assumption)
    _require(type(value["needs_human"]) is bool and type(plan) is list, "recovery shape")
    _require(value["verification_criteria"] == ["exact_file_content"], "verification criteria")
    _require(len(plan) == (0 if value["needs_human"] else 3), "recovery abstention")
    for index, step in enumerate(plan):
        _object(step, {"order", "action", "expected_result"})
        _integer(step["order"], index + 1, index + 1)
        _require(step["action"] == RECOVERY_ACTIONS[index], "recovery action")
        _string(step["expected_result"], 1, 300)


def _vision(value: dict[str, Any], context: dict[str, Any]) -> None:
    _object(value, {"capture_id", "state_version", "width", "height", "elements", "needs_human"})
    _hex(value["capture_id"], 32)
    _integer(value["state_version"])
    _integer(value["width"], 640, 640)
    _integer(value["height"], 360, 360)
    _require(
        all(
            value[key] == context[key] for key in ("capture_id", "state_version", "width", "height")
        ),
        "capture binding",
    )
    elements = value["elements"]
    _require(
        type(value["needs_human"]) is bool and type(elements) is list and len(elements) <= 2,
        "vision shape",
    )
    labels, boxes = [], []
    for element in elements:
        _object(element, {"role", "label", "bbox"})
        _require(
            element["role"] == "button" and element["label"] in ("SAVE", "CANCEL"), "scene label"
        )
        box = _object(element["bbox"], {"x", "y", "width", "height"})
        _integer(box["x"], 0, 639)
        _integer(box["y"], 0, 359)
        _integer(box["width"], 10, 640)
        _integer(box["height"], 10, 360)
        _require(box["x"] + box["width"] <= 640 and box["y"] + box["height"] <= 360, "box bounds")
        boxes.append(box)
        labels.append(element["label"])
    _require(
        not elements if value["needs_human"] else sorted(labels) == ["CANCEL", "SAVE"],
        "vision abstention or distinct labels",
    )
    if len(boxes) == 2:
        a, b = boxes
        overlap = max(a["x"], b["x"]) < min(a["x"] + a["width"], b["x"] + b["width"]) and max(
            a["y"], b["y"]
        ) < min(a["y"] + a["height"], b["y"] + b["height"])
        _require(not overlap, "overlapping boxes")


def _bonsai(
    request: dict[str, Any],
    response: dict[str, Any],
    contract: OutputContract,
    context_tokens: int,
    max_output_tokens: int,
    *,
    raw: bool,
) -> dict[str, Any]:
    context, output_limit = _bonsai_context(request, contract, context_tokens, max_output_tokens)
    extras = (
        {"model", "id", "object", "created", "system_fingerprint", "timings"} if raw else {"model"}
    )
    _object(response, {"choices", "usage"}, extras)
    if "model" in response:
        _require(
            response["model"] == "bonsai-" + request["deployment_digest"], "raw model identity"
        )
    if "id" in response:
        _require(
            re.fullmatch(r"chatcmpl-[0-9A-Za-z]{32}", _string(response["id"])) is not None,
            "native completion ID",
        )
    if "object" in response:
        _require(response["object"] == "chat.completion", "native object type")
    if "created" in response:
        _integer(response["created"])
    if "system_fingerprint" in response:
        fingerprint = _string(response["system_fingerprint"], 1, 512)
        _require(len(fingerprint.encode("utf-8")) <= 512, "native fingerprint byte bound")
    if "timings" in response:
        timings = _object(
            response["timings"], TIMING_COUNTS | TIMING_NUMBERS, {"draft_n", "draft_n_accepted"}
        )
        for key in TIMING_COUNTS:
            _integer(timings[key], 0, output_limit if key == "predicted_n" else context_tokens)
        for key in TIMING_NUMBERS:
            _number(timings[key], SAFE_INTEGER)
        _require(("draft_n" in timings) == ("draft_n_accepted" in timings), "paired draft counters")
        if "draft_n" in timings:
            _integer(timings["draft_n"], 1)
            _integer(timings["draft_n_accepted"], 0, timings["draft_n"])
    choices = response["choices"]
    _require(type(choices) is list and len(choices) == 1, "single choice required")
    choice = _object(choices[0], {"finish_reason", "message"}, {"index"} if raw else set())
    _require(choice["finish_reason"] == "stop", "incomplete or alternate completion")
    if "index" in choice:
        _integer(choice["index"], 0, 0)
    message = _object(
        choice["message"], {"content"}, {"role", "reasoning_content"} if raw else {"role"}
    )
    if "role" in message:
        _require(message["role"] == "assistant", "assistant role required when present")
    if "reasoning_content" in message:
        _require(message["reasoning_content"] in (None, ""), "forbidden reasoning channel")
    content = _string(message["content"])
    decoded = decode(content.encode("utf-8"), limit=CONTENT_LIMIT)
    if request["profile_id"] == PROFILES[1]:
        _recovery(decoded, context)
    else:
        _vision(decoded, context)
    usage = _object(
        response["usage"],
        {"prompt_tokens", "completion_tokens"},
        {"total_tokens", "prompt_tokens_details"} if raw else set(),
    )
    prompt = _integer(usage["prompt_tokens"], 0, context_tokens)
    completion = _integer(usage["completion_tokens"], 0, output_limit)
    _require(prompt + completion <= context_tokens, "context token overflow")
    if "total_tokens" in usage:
        _require(
            _integer(usage["total_tokens"], 0, context_tokens) == prompt + completion,
            "native total tokens disagree",
        )
    if "prompt_tokens_details" in usage:
        details = _object(usage["prompt_tokens_details"], {"cached_tokens"})
        _integer(details["cached_tokens"], 0, prompt)
    projected_message = {"content": content}
    if "role" in message:
        projected_message["role"] = message["role"]
    projected = {
        "choices": [{"finish_reason": "stop", "message": projected_message}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }
    if "model" in response:
        projected["model"] = response["model"]
    return projected


def project_profile_output(
    request_bytes: bytes,
    raw_response: bytes,
    generation: dict[str, Any],
    contract: OutputContract,
    *,
    context_tokens: int = 16384,
    max_output_tokens: int = 512,
) -> dict[str, Any]:
    """Validate raw producer bytes, then return the exact future stored wrapper.

    Caller must bind generation to its independently witnessed child and verify
    current task/admission authority. This pure validator cannot grant either.
    """
    request = _request(request_bytes)
    response = decode(raw_response, limit=FRAME_LIMIT)
    if request["profile_id"] == PROFILES[0]:
        usage = _decider(request, response)
    else:
        response = _bonsai(request, response, contract, context_tokens, max_output_tokens, raw=True)
        usage = deepcopy(response["usage"])
    result = {"response": response, "usage": usage, "generation": _generation(generation)}
    _require(len(canonical(result)) <= RESULT_LIMIT, "stored result byte bound")
    return result


def validate_result(
    request_bytes: bytes,
    result_bytes: bytes,
    contract: OutputContract,
    *,
    context_tokens: int = 16384,
    max_output_tokens: int = 512,
    expected_generation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Check canonical stored bytes; never project or rehash legacy evidence."""
    request = _request(request_bytes)
    result = decode(result_bytes, limit=RESULT_LIMIT, canonical_required=True)
    _object(result, {"response", "usage", "generation"})
    actual_generation = _generation(result["generation"])
    if expected_generation is not None:
        _require(actual_generation == _generation(expected_generation), "terminal child mismatch")
    response = result["response"]
    if request["profile_id"] == PROFILES[0]:
        usage = _decider(request, response)
    else:
        projected = _bonsai(
            request, response, contract, context_tokens, max_output_tokens, raw=False
        )
        _require(canonical(projected) == canonical(response), "nonprojected stored response")
        usage = projected["usage"]
    _require(canonical(usage) == canonical(result["usage"]), "response/usage mismatch")
    return result


def instantiate_result_schema(request_bytes: bytes, contract: OutputContract) -> dict[str, Any]:
    """Select exactly one profile; the unselected static artifact denies all data."""
    request = _request(request_bytes)
    schema = deepcopy(contract.schemas["result.schema.json"])
    schema.pop("not")
    schema["$ref"] = "#/$defs/" + request["profile_id"]
    if request["profile_id"] == PROFILES[0]:
        schema["$defs"][PROFILES[0]]["properties"]["response"] = instantiate_decider_schema(
            request_bytes,
            contract,
        )
    return schema


def write_bundle(directory: Path) -> str:
    """Authoring only: refresh source/schema pins after formatting; no admission.

    Both implementation roles intentionally pin this whole module. The resulting
    digest must be reviewed and configured externally; it is not a self-hash.
    """
    source_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    schemas = {
        name: hashlib.sha256(
            canonical(decode((directory / name).read_bytes(), limit=FRAME_LIMIT))
        ).hexdigest()
        for name in SCHEMA_FILES
    }
    bundle = {
        "name": NAME,
        "version": VERSION,
        "schemas": schemas,
        "adapter_source_sha256": source_hash,
        "request_binding_source_sha256": source_hash,
    }
    data = canonical(bundle)
    (directory / "bundle.json").write_bytes(data + b"\n")
    return hashlib.sha256(data).hexdigest()
