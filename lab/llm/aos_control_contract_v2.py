"""Standalone, unadmitted control/terminal-v2 contract authoring and validation.

No socket, DB, allocator, process observation or release authority is provided.
Validation establishes shape/hash consistency only. Authenticated original
authority and physical proof remain separate mandatory runtime responsibilities.
Legacy receipts are readable as immutable cleanup evidence, never upgraded.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from lab.llm import aos_profile_output as output

NAME = "aos-scientist-control-contract.v2"
CONTROL = "aos-scientist-control.v2"
TERMINAL = "aos-scientist-terminal.v2"
VERSION = 2
SAFE_INTEGER = 2**53 - 1
MICROSECONDS = 1_000_000
REQUEST_LIMIT = 8192
FRAME_LIMIT = 128 * 1024
SCHEMA_LIMIT = 512 * 1024
PROFILES = output.PROFILES
OPS = ["capability", "status", "cancel", "reconcile"]
CLEANUP_OPS = ["cancel", "reconcile", "status"]
STATES = [
    "unknown",
    "intent",
    "queued",
    "activating",
    "running",
    "result_ready",
    "cancel_pending",
    "draining",
    "quarantined",
    "completed",
    "canceled",
    "expired",
    "failed",
]
ERRORS = [
    "invalid_frame",
    "unsupported_version",
    "unsupported_schema",
    "unauthorized",
    "stale_generation",
    "capability_mismatch",
    "capability_expired",
    "profile_mismatch",
    "deployment_mismatch",
    "request_conflict",
    "history_denied",
    "busy",
    "deadline_exceeded",
    "internal_unavailable",
]
OUTCOMES = [
    ("completed", "released", "success"),
    ("canceled", "released", "caller_cancel"),
    ("canceled", "recovered_released", "caller_cancel"),
    ("expired", "released", "turn_timeout"),
    ("expired", "recovered_released", "recovered_after_crash"),
    ("failed", "released", "execution_failed"),
    ("canceled", "never_admitted", "caller_cancel"),
    ("expired", "never_admitted", "generation_lost"),
    ("expired", "never_admitted", "queue_timeout"),
    ("expired", "never_admitted", "turn_timeout"),
]
ROOTS = {
    "request.schema.json": "request",
    "response.template.schema.json": "response",
    "terminal.schema.json": "terminal",
    "legacy-terminal.schema.json": "legacy_terminal",
    "admission-binding.schema.json": "binding",
    "capability.schema.json": "capability",
    "cleanup-grant.schema.json": "cleanup_grant",
    "terminal-evidence.schema.json": "terminal_evidence",
}
DURATIONS = ("activation", "inference", "total", "queue")
TIMESTAMPS = (
    "activation_deadline",
    "inference_deadline",
    "total_deadline",
    "envelope_deadline",
    "queue_deadline",
    "admitted_boottime",
)


class ContractError(ValueError):
    """Metadata is malformed, unbound or unsupported; no authority follows."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ContractError(reason)


def canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ContractError("invalid canonical JSON") from exc


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result, "duplicate key")
        result[key] = value
    return result


def _constant(_value: str) -> Any:
    raise ContractError("nonfinite number")


def decode(raw: bytes, *, limit: int = FRAME_LIMIT, exact: bool = True) -> dict[str, Any]:
    _require(type(raw) is bytes and 0 < len(raw) <= limit, "byte bound")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ContractError("invalid JSON") from exc
    _require(type(value) is dict, "object required")
    encoded = canonical(value)
    _require(not exact or encoded == raw, "noncanonical metadata")
    return cast(dict[str, Any], value)


def _scaled(value: int | float, *, ceil: bool) -> int:
    _require(type(value) in (int, float), "nonboolean seconds required")
    if type(value) is int:
        numerator, denominator = value, 1
    else:
        _require(math.isfinite(value), "finite seconds required")
        numerator, denominator = value.as_integer_ratio()
    _require(numerator >= 0, "negative seconds")
    numerator *= MICROSECONDS
    scaled = (numerator + denominator - 1) // denominator if ceil else numerator // denominator
    _require(0 <= scaled <= SAFE_INTEGER, "microsecond safe-integer overflow")
    return scaled


def boottime_us(seconds: int | float) -> int:
    """Floor the exact supplied value, never extending an existing deadline.

    For floats the binary value's exact rational representation is used; no
    rounded float multiplication or decimal-string reinterpretation occurs.
    """
    return _scaled(seconds, ceil=False)


def duration_us(seconds: int | float) -> int:
    """Ceil a configured duration; never derive or renew an assigned deadline."""
    return _scaled(seconds, ceil=True)


def _obj(properties: dict[str, Any], *, optional: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": [key for key in properties if key not in optional],
        "additionalProperties": False,
    }


def _ref(name: str) -> dict[str, str]:
    return {"$ref": "#/$defs/" + name}


def _nullable(schema: Any) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


def _integer(minimum: int = 0, maximum: int = SAFE_INTEGER) -> dict[str, Any]:
    return {"type": "integer", "minimum": minimum, "maximum": maximum}


def _const(value: Any) -> dict[str, Any]:
    kind = "boolean" if type(value) is bool else "integer" if type(value) is int else "string"
    return {"type": kind, "const": value}


def _schema_pin(name: str, version: int) -> dict[str, Any]:
    return _obj({"name": _const(name), "version": _const(version), "sha256": _ref("hash")})


def definitions(result_schema: Any = False) -> dict[str, Any]:
    """Complete static closure. Result is deny-all until original-request derivation."""
    defs: dict[str, Any] = {
        "hash": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "id": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
        "boot": {
            "type": "string",
            "pattern": "^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$",
        },
        "profile": {"type": "string", "enum": list(PROFILES)},
        "unit": {"type": "string", "maxLength": 255, "pattern": r"^[a-zA-Z0-9_.@-]+\.service$"},
        "cgroup": {"type": "string", "minLength": 1, "maxLength": 4096, "pattern": "^/"},
        "sources": _obj({"aos": _ref("hash"), "scientist": _ref("hash")}),
        "clock": _obj(
            {
                "name": _const("CLOCK_BOOTTIME"),
                "unit": _const("microseconds"),
                "boot_id": _ref("boot"),
            }
        ),
        "output_pin": _obj(
            {"name": _const(output.NAME), "version": _const(2), "bundle_sha256": _ref("hash")}
        ),
        "result": result_schema,
    }
    server = {
        "uid": _integer(),
        "pid": _integer(1, 2**31 - 1),
        "start_ticks": _integer(),
        "boot_id": _ref("boot"),
        "unit": _ref("unit"),
        "invocation_id": _ref("id"),
        "control_group": _ref("cgroup"),
    }
    defs["server"] = _obj(server)
    defs["caller"] = _obj(
        {**server, "parent_pid": _integer(1, 2**31 - 1), "parent_start_ticks": _integer()}
    )
    defs["child"] = _obj(
        {
            "unit": {
                "type": "string",
                "maxLength": 255,
                "pattern": r"^swapp-aos-gpu-turn-[a-f0-9]{32}\.service$",
            },
            "invocation_id": _ref("id"),
            "pid": _integer(2, 2**31 - 1),
            "start_ticks": _integer(),
            "boot_id": _ref("boot"),
            "control_group": {"type": "string", "maxLength": 512, "pattern": "^/"},
        }
    )
    pin = {
        key: _ref("hash")
        for key in (
            "deployment_digest",
            "manifest_sha256",
            "config_sha256",
            "response_schema_sha256",
        )
    }
    defs["profile_pin"] = _obj({**pin, "output_contract": _ref("output_pin")})
    defs["legacy_profile_pin"] = {"oneOf": [_obj(pin), _ref("profile_pin")]}
    binding = {
        "server_generation": _ref("server"),
        "caller_generation": _ref("caller"),
        "policy_sha256": _ref("hash"),
        "source_fingerprints": _ref("sources"),
        "profile_id": _ref("profile"),
        "profile_pin": _ref("profile_pin"),
        "infer_schema": _schema_pin("aos-scientist-runtime.v1", 1),
        "control_schema": _schema_pin(CONTROL, 2),
        "terminal_schema": _schema_pin(TERMINAL, 2),
    }
    defs["binding"] = _obj(binding)
    defs["legacy_binding"] = _obj(
        {
            **binding,
            "profile_pin": _ref("legacy_profile_pin"),
            "control_schema": _schema_pin("aos-scientist-control.v1", 1),
            "terminal_schema": _schema_pin("aos-scientist-terminal.v1", 1),
        }
    )
    defs["original_binding"] = {"oneOf": [_ref("binding"), _ref("legacy_binding")]}
    defs["target"] = _obj(
        {
            "request_id": _ref("id"),
            "request_sha256": _ref("hash"),
            "original_peer_generation_sha256": _ref("hash"),
        }
    )
    defs["cleanup_authorization"] = _obj(
        {
            "target": _ref("target"),
            "binding": _ref("original_binding"),
            "operations": {
                "const": ["cancel"],
                "type": "array",
                "items": _const("cancel"),
                "minItems": 1,
                "maxItems": 1,
            },
        }
    )
    operations = {
        "type": "array",
        "items": {"type": "string", "enum": CLEANUP_OPS},
        "minItems": 1,
        "maxItems": 3,
        "uniqueItems": True,
    }
    original = {
        "original_admission_binding": _nullable(_ref("original_binding")),
        "original_admission_binding_sha256": _nullable(_ref("hash")),
        "original_cleanup_authorization": _nullable(_ref("cleanup_authorization")),
        "original_cleanup_authorization_sha256": _nullable(_ref("hash")),
    }
    defs["cleanup_grant"] = _obj(
        {
            "schema": _const("aos-scientist-cleanup-grant.v2"),
            "version": _const(2),
            "target": _ref("target"),
            "profile_id": _ref("profile"),
            "deployment_digest": _ref("hash"),
            "caller_generation": _ref("caller"),
            "resolver_generation": _ref("server"),
            "policy_sha256": _ref("hash"),
            "source_fingerprints": _ref("sources"),
            "control_schema_sha256": _ref("hash"),
            "operations": operations,
            **original,
        }
    )
    freshness = {
        "clock": _ref("clock"),
        "issued_boottime_us": _integer(),
        "expires_boottime_us": _integer(),
    }
    defs["infer_capability"] = _obj(
        {
            "admission_binding": _ref("binding"),
            "admission_binding_sha256": _ref("hash"),
            "server_generation_sha256": _ref("hash"),
            "caller_generation": _ref("caller"),
            "caller_generation_sha256": _ref("hash"),
            "policy_sha256": _ref("hash"),
            "source_fingerprints": _ref("sources"),
            "profile_id": _ref("profile"),
            **pin,
            "output_contract": _ref("output_pin"),
            "infer_schema": _schema_pin("aos-scientist-runtime.v1", 1),
            "control_schema": _schema_pin(CONTROL, 2),
            "history_schema_sha256": {"type": "null"},
            "operations": {
                "type": "array",
                "const": sorted(OPS),
                "items": {"type": "string", "enum": OPS},
                "minItems": 4,
                "maxItems": 4,
            },
            "request_bytes": _const(REQUEST_LIMIT),
            "response_bytes": _const(FRAME_LIMIT),
            "frame_us": _integer(1),
            "call_us": _integer(1),
            **freshness,
        }
    )
    defs["target_capability"] = _obj(
        {
            "target": _ref("target"),
            "control_binding": _ref("binding"),
            "operations": {**operations, "const": CLEANUP_OPS},
            **freshness,
        }
    )
    defs["cleanup_capability"] = _obj(
        {"cleanup_grant": _ref("cleanup_grant"), "cleanup_grant_sha256": _ref("hash"), **freshness}
    )
    defs["capability"] = {
        "oneOf": [
            _ref(key) for key in ("infer_capability", "target_capability", "cleanup_capability")
        ]
    }
    maxima = {
        "activation_us": _integer(1, 600 * MICROSECONDS),
        "inference_us": _integer(1, 540 * MICROSECONDS),
        "total_us": _integer(1, 720 * MICROSECONDS),
        "queue_us": _integer(1),
        "max_output_tokens": _integer(1, 512),
        "context_tokens": _integer(256, 16384),
    }
    defs["budget"] = _obj(
        {
            **maxima,
            **{key + "_us": _nullable(_integer()) for key in TIMESTAMPS},
            "clock": _ref("clock"),
        }
    )
    legacy_number = {"type": "number", "minimum": 0, "maximum": SAFE_INTEGER}
    defs["legacy_budget"] = _obj(
        {
            **{
                key + "_seconds": _integer(1, bound)
                for key, bound in (
                    ("activation", 600),
                    ("inference", 540),
                    ("total", 720),
                    ("queue", SAFE_INTEGER),
                )
            },
            "max_output_tokens": _integer(1, 512),
            "context_tokens": _integer(256, 16384),
            **{key: _nullable(legacy_number) for key in TIMESTAMPS},
            "boot_id": _ref("boot"),
        }
    )
    terminal = {
        "schema": _const(TERMINAL),
        "version": _const(2),
        "request_id": _ref("id"),
        "request_sha256": _ref("hash"),
        "original_principal": _ref("caller"),
        "profile_id": _ref("profile"),
        "deployment_digest": _ref("hash"),
        "profile_config_sha256": _nullable(_ref("hash")),
        "response_schema_sha256": _nullable(_ref("hash")),
        "admission_binding": _nullable(_ref("binding")),
        "admission_binding_sha256": _nullable(_ref("hash")),
        "original_budget": _nullable(_ref("budget")),
        "allocation_binding_sha256": _nullable(_ref("hash")),
        "child_generation": _nullable(_ref("child")),
        "drain_evidence_sha256": _nullable(_ref("hash")),
        "no_admission_evidence_sha256": _nullable(_ref("hash")),
        "release_outcome": {"type": "string", "enum": sorted({item[1] for item in OUTCOMES})},
        "terminal_state": {"type": "string", "enum": sorted({item[0] for item in OUTCOMES})},
        "reason_code": {"type": "string", "enum": sorted({item[2] for item in OUTCOMES})},
        "result_sha256": _nullable(_ref("hash")),
        "recorded_clock": _ref("clock"),
        "recorded_boottime_us": _integer(),
        "receipt_sha256": _ref("hash"),
    }
    defs["terminal"] = _obj(terminal)
    legacy_terminal = {
        key: value
        for key, value in terminal.items()
        if key not in {"version", "recorded_clock", "recorded_boottime_us"}
    }
    legacy_terminal.update(
        schema=_const("aos-scientist-terminal.v1"),
        admission_binding=_nullable(_ref("legacy_binding")),
        original_budget=_nullable(_ref("legacy_budget")),
        recorded_boot_id=_ref("boot"),
        recorded_boottime=legacy_number,
    )
    defs["legacy_terminal"] = _obj(legacy_terminal)
    defs["observation"] = _obj(
        {
            "target": _ref("target"),
            "state": {"type": "string", "enum": STATES},
            "cancel_requested": {"type": "boolean"},
            "cancel_clock": _nullable(_ref("clock")),
            "first_cancel_boottime_us": _nullable(_integer()),
            "terminal_receipt": _nullable(_ref("terminal")),
            "result": _nullable(_ref("result")),
        }
    )
    defs["cleanup_observation"] = _obj(
        {
            "cleanup_grant": _ref("cleanup_grant"),
            "cleanup_grant_sha256": _ref("hash"),
            "observation": _ref("observation"),
        }
    )
    request = {
        "schema": _const(CONTROL),
        "version": _const(2),
        "op": {"type": "string", "enum": OPS},
        "control_id": _ref("id"),
        "expected_capability_sha256": _nullable(_ref("hash")),
        "profile_id": _ref("profile"),
        "deployment_digest": _ref("hash"),
        "target": _nullable(_ref("target")),
    }
    defs["request"] = {
        "oneOf": [
            _obj(
                {
                    **request,
                    "op": _const("capability"),
                    "expected_capability_sha256": {"type": "null"},
                }
            ),
            _obj(
                {
                    **request,
                    "op": {"type": "string", "enum": CLEANUP_OPS},
                    "expected_capability_sha256": _ref("hash"),
                    "target": _ref("target"),
                }
            ),
        ]
    }
    cap_data = {
        "oneOf": [
            _obj(
                {
                    "capability": _ref("infer_capability"),
                    "admission": _const(admission),
                    "reason_code": _const(reason),
                }
            )
            for admission, reason in (("enabled", "enabled"), ("denied", "policy_disabled"))
        ]
        + [
            _obj(
                {
                    "capability": _ref(kind),
                    "admission": _const("denied"),
                    "reason_code": _const(reason),
                }
            )
            for kind, reason in (
                ("target_capability", "retained_target_only"),
                ("cleanup_capability", "cleanup_only"),
            )
        ]
    }
    response = {
        "schema": _const(CONTROL),
        "version": _const(2),
        "control_id": _ref("id"),
        "op": {"type": "string", "enum": OPS},
        "ok": _const(True),
        "capability_sha256": _ref("hash"),
        "error": {"type": "null"},
    }
    defs["response"] = {
        "oneOf": [
            _obj({**response, "op": _const("capability"), "data": cap_data}),
            _obj(
                {
                    **response,
                    "op": {"type": "string", "enum": CLEANUP_OPS},
                    "data": {"oneOf": [_ref("observation"), _ref("cleanup_observation")]},
                }
            ),
            _obj(
                {
                    **response,
                    "ok": _const(False),
                    "capability_sha256": {"type": "null"},
                    "data": {"type": "null"},
                    "error": _obj(
                        {
                            "code": {"type": "string", "enum": ERRORS},
                            "retryable": {"type": "boolean"},
                        }
                    ),
                }
            ),
        ]
    }
    # These are exact CURRENT stored preimages. Their seconds and original
    # hashes are immutable historical evidence, not new v2 clock metadata.
    process = _obj(
        {"pid": _integer(1, 2**31 - 1), "start_ticks": _integer(), "boot_id": _ref("boot")}
    )
    legacy_maxima = {
        key: spec
        for key, spec in defs["legacy_budget"]["properties"].items()
        if key.endswith("_seconds") or key in {"max_output_tokens", "context_tokens"}
    }
    defs["legacy_allocation"] = _obj(
        {
            "lease": _obj(
                {
                    "owner": _const("aos"),
                    "request_id": _ref("id"),
                    "fencing_token": _integer(1),
                    "phase": _const("activating"),
                    "activation_deadline": legacy_number,
                    "inference_deadline": legacy_number,
                    "total_deadline": legacy_number,
                    "heartbeat_deadline": legacy_number,
                    "slice_seconds": _integer(1, 720),
                    "owner_identity": process,
                    "owner_unit": _ref("unit"),
                    "owner_invocation_id": _ref("id"),
                }
            ),
            "admission_binding_sha256": _ref("hash"),
            "original_principal": _ref("caller"),
            "request_sha256": _ref("hash"),
            "original_deadline": legacy_number,
            "original_budget": _obj(legacy_maxima),
        }
    )
    child_intent = _obj(
        {
            "unit": defs["child"]["properties"]["unit"],
            "nonce": _ref("hash"),
            "created_boottime": legacy_number,
            "total_seconds": _integer(1, 720),
        }
    )
    empty_pids = {"type": "array", "items": _integer(1, 2**31 - 1), "maxItems": 0}
    drain = {
        "boot_id": _ref("boot"),
        "observed_boottime": legacy_number,
        "child_generation": _nullable(_ref("child")),
        "late_start_fenced": _const(True),
        "cgroup_empty": _const(True),
        "gpu_absent": _const(True),
        "observed_gpu_pids": {
            "type": "array",
            "items": _integer(1, 2**31 - 1),
            "maxItems": 4096,
            "uniqueItems": True,
        },
        "remaining_owned_gpu_pids": empty_pids,
        "remaining_foreign_gpu_pids": empty_pids,
        "allocation_binding_sha256": _ref("hash"),
        "handoff_stage": {"enum": [None, "plan", "start", "go"]},
    }
    # Explicit type-bearing variants avoid JSON Schema's permissive enum typing.
    drain["handoff_stage"] = {
        "anyOf": [{"type": "null"}, {"type": "string", "enum": ["plan", "start", "go"]}]
    }
    defs["legacy_drain"] = {
        "oneOf": [
            _obj(
                {
                    **drain,
                    "kind": _const("no_child_intent"),
                    "no_child_intent": _const(True),
                    "child_generation": {"type": "null"},
                    "handoff_stage": {"type": "null"},
                    "observed_gpu_pids": empty_pids,
                }
            ),
            _obj(
                {
                    **drain,
                    "kind": _const("physical_drain"),
                    "never_started": {"type": "boolean"},
                    "child_intent": child_intent,
                    "late_start_fence": legacy_number,
                    "final_unit_state": {"type": "string", "enum": ["inactive", "failed", ""]},
                    "final_main_pid": _const(0),
                }
            ),
        ]
    }
    defs["legacy_no_admission"] = _obj(
        {
            "request_id": _ref("id"),
            "request_sha256": _ref("hash"),
            "principal_sha256": _ref("hash"),
            "cancel_before_intent": {"type": "boolean"},
            "never_allocated": _const(True),
            "reason": terminal["reason_code"],
        }
    )

    def encoded(root: str, *, limit: int = FRAME_LIMIT) -> dict[str, Any]:
        return {
            "type": "string",
            "minLength": 2,
            "maxLength": limit,
            "contentMediaType": "application/json",
            "contentSchema": _ref(root),
        }

    defs["terminal_evidence"] = _obj(
        {
            "schema": _const("aos-scientist-terminal-evidence.v2"),
            "version": _const(2),
            "target": _ref("target"),
            "terminal_canonical": encoded("legacy_terminal"),
            "allocation_canonical": _nullable(encoded("legacy_allocation")),
            "drain_canonical": _nullable(encoded("legacy_drain")),
            "no_admission_canonical": _nullable(encoded("legacy_no_admission")),
            "result_canonical": _nullable(encoded("result", limit=output.RESULT_LIMIT)),
        }
    )
    return defs


def schema_document(root: str, *, result_schema: Any = False) -> dict[str, Any]:
    defs = definitions(result_schema)
    _require(root in defs, "unknown schema root")
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$defs": defs, **_ref(root)}


def _check(value: Any, schema: Any, defs: dict[str, Any], depth: int = 0) -> None:
    """Strict interpreter for this bundle's closed JSON Schema vocabulary only."""
    _require(depth <= 48, "metadata depth")
    if schema is False:
        raise ContractError("unspecialized or forbidden value")
    _require(type(schema) is dict, "unsupported schema")
    if "$ref" in schema:
        ref = schema["$ref"]
        _require(ref.startswith("#/$defs/") and ref[8:] in defs, "nonlocal schema reference")
        _check(value, defs[ref[8:]], defs, depth + 1)
        return
    for union in ("anyOf", "oneOf"):
        if union in schema:
            matches = 0
            for variant in schema[union]:
                try:
                    _check(value, variant, defs, depth + 1)
                    matches += 1
                except ContractError:
                    continue
            _require(matches == 1 if union == "oneOf" else matches > 0, "schema variant mismatch")
            return
    kind = schema.get("type")
    types = {
        "object": dict,
        "array": list,
        "string": str,
        "integer": int,
        "boolean": bool,
        "null": type(None),
    }
    if kind == "number":
        _require(type(value) in (int, float), "number type")
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        _require(finite, "finite number")
    else:
        _require(kind in types and type(value) is types[kind], "strict JSON type")
    if "const" in schema:
        _require(canonical(value) == canonical(schema["const"]), "constant mismatch")
    if "enum" in schema:
        _require(value in schema["enum"], "enum mismatch")
    if kind == "object":
        properties = schema["properties"]
        _require(
            schema.get("additionalProperties") is False
            and set(schema["required"]) <= set(value) <= set(properties),
            "object closure",
        )
        for key, item in value.items():
            _check(item, properties[key], defs, depth + 1)
    elif kind == "array":
        _require(
            schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", SAFE_INTEGER),
            "array bound",
        )
        if schema.get("uniqueItems"):
            _require(len({canonical(item) for item in value}) == len(value), "duplicate array item")
        for item in value:
            _check(item, schema["items"], defs, depth + 1)
    elif kind == "string":
        _require(
            schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", FRAME_LIMIT),
            "string bound",
        )
        if "pattern" in schema:
            _require(re.search(schema["pattern"], value) is not None, "string pattern")
    elif kind in {"integer", "number"}:
        _require(
            schema.get("minimum", -SAFE_INTEGER) <= value <= schema.get("maximum", SAFE_INTEGER),
            "numeric bound",
        )


@dataclass(frozen=True)
class ControlContract:
    """Pinned local artifacts, not an admission or trusted physical proof."""

    bundle_sha256: str
    schemas: dict[str, dict[str, Any]]

    def schema_hash(self, filename: str) -> str:
        _require(filename in ROOTS, "unknown schema file")
        return digest(self.schemas[filename])


def load_contract(directory: Path, expected_sha256: str) -> ControlContract:
    bundle = decode((directory / "bundle.json").read_bytes(), exact=False)
    _require(
        set(bundle) == {"name", "version", "schemas", "adapter_source_sha256"}, "bundle fields"
    )
    _require(
        bundle["name"] == NAME and type(bundle["version"]) is int and bundle["version"] == 2,
        "bundle version",
    )
    _require(
        digest(bundle) == expected_sha256
        and bundle["adapter_source_sha256"]
        == hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "bundle/source pin mismatch",
    )
    _require(
        type(bundle["schemas"]) is dict and set(bundle["schemas"]) == set(ROOTS), "bundle files"
    )
    schemas = {}
    for filename, root in ROOTS.items():
        schema = decode((directory / filename).read_bytes(), limit=SCHEMA_LIMIT, exact=False)
        _require(
            digest(schema) == bundle["schemas"][filename] and schema == schema_document(root),
            "schema mismatch",
        )
        schemas[filename] = schema
    return ControlContract(expected_sha256, schemas)


def validate_shape(value: dict[str, Any], filename: str, contract: ControlContract) -> None:
    _require(filename in ROOTS, "unknown schema file")
    schema = contract.schemas[filename]
    _check(value, schema, schema["$defs"])


def project_budget(
    configured: dict[str, Any], assigned: dict[str, Any], boot_id: str
) -> dict[str, Any]:
    """Project supplied first-assigned evidence only; no clock read or deadline creation.

    Use only while creating NEW v2 metadata. Never call this to rewrite, rehash
    or elevate a stored v1 receipt/binding. None stays None, including tombstones.
    """
    _require(
        set(configured)
        == {key + "_seconds" for key in DURATIONS} | {"max_output_tokens", "context_tokens"}
        and set(assigned) == set(TIMESTAMPS),
        "budget fields",
    )
    result: dict[str, Any] = {
        key + "_us": duration_us(configured[key + "_seconds"]) for key in DURATIONS
    }
    result.update(
        {
            key + "_us": None if assigned[key] is None else boottime_us(assigned[key])
            for key in TIMESTAMPS
        }
    )
    result.update(
        max_output_tokens=configured["max_output_tokens"],
        context_tokens=configured["context_tokens"],
        clock={"name": "CLOCK_BOOTTIME", "unit": "microseconds", "boot_id": boot_id},
    )
    defs = definitions()
    _check(result, defs["budget"], defs)
    _budget(result, legacy=False)
    return result


def _budget(value: dict[str, Any], *, legacy: bool) -> None:
    unit, suffix = (1, "_seconds") if legacy else (MICROSECONDS, "_us")
    _require(
        value["total" + suffix] >= value["activation" + suffix] + value["inference" + suffix]
        and value["queue" + suffix] >= value["total" + suffix] + 30 * unit,
        "budget coverage",
    )
    time_suffix = "" if legacy else "_us"
    admitted, envelope = (
        value[key + time_suffix] for key in ("admitted_boottime", "envelope_deadline")
    )
    deadlines = [value[key + time_suffix] for key in TIMESTAMPS[:5]]
    if admitted is None:
        _require(all(item is None for item in deadlines), "unadmitted deadline fabrication")
    else:
        _require(envelope is not None and admitted < envelope, "original envelope deadline")
        _require(
            all(item is None or admitted < item <= envelope for item in deadlines), "phase deadline"
        )
        total = value["total_deadline" + time_suffix]
        _require(
            all(
                value[key + time_suffix] is None
                or total is not None
                and value[key + time_suffix] <= total
                for key in ("activation_deadline", "inference_deadline")
            ),
            "total deadline bound",
        )


def _generation(value: dict[str, Any], *, child: bool = False) -> None:
    parts = value["control_group"].split("/")
    _require(
        parts[0] == "" and all(part not in {"", ".", ".."} for part in parts[1:]), "cgroup path"
    )
    if child:
        _require(parts[-1] == value["unit"], "child cgroup unit")


def validate_binding(value: dict[str, Any], contract: ControlContract) -> None:
    validate_shape(value, "admission-binding.schema.json", contract)
    _generation(value["server_generation"])
    _generation(value["caller_generation"])
    _require(
        value["server_generation"]["boot_id"] == value["caller_generation"]["boot_id"],
        "admission boot mismatch",
    )
    _require(
        value["control_schema"]["sha256"] == contract.schema_hash("response.template.schema.json")
        and value["terminal_schema"]["sha256"] == contract.schema_hash("terminal.schema.json"),
        "complete schema pin mismatch",
    )


def _terminal_checks(value: dict[str, Any], *, legacy: bool, contract: ControlContract) -> None:
    _require(
        digest({key: item for key, item in value.items() if key != "receipt_sha256"})
        == value["receipt_sha256"],
        "terminal hash",
    )
    _generation(value["original_principal"])
    binding = value["admission_binding"]
    if binding is None:
        _require(
            value["admission_binding_sha256"] is None
            and value["terminal_state"] == "canceled"
            and value["release_outcome"] == "never_admitted",
            "null admission variant",
        )
    else:
        if not legacy:
            validate_binding(binding, contract)
        _require(
            digest(binding) == value["admission_binding_sha256"]
            and binding["caller_generation"] == value["original_principal"]
            and binding["profile_id"] == value["profile_id"],
            "terminal original binding",
        )
        for outer, inner in (
            ("deployment_digest", "deployment_digest"),
            ("profile_config_sha256", "config_sha256"),
            ("response_schema_sha256", "response_schema_sha256"),
        ):
            _require(value[outer] == binding["profile_pin"][inner], "terminal original profile")
    state, outcome, reason = (
        value[key] for key in ("terminal_state", "release_outcome", "reason_code")
    )
    _require((state, outcome, reason) in OUTCOMES, "unsupported terminal outcome")
    _require(
        (state == "completed") == (value["result_sha256"] is not None), "terminal result presence"
    )
    budget, child = value["original_budget"], value["child_generation"]
    if budget is not None:
        _budget(budget, legacy=legacy)
        suffix = "" if legacy else "_us"
        _require(
            (binding is not None) == (budget["admitted_boottime" + suffix] is not None),
            "budget admission variant",
        )
    else:
        _require(binding is None, "admitted budget missing")
    if outcome == "never_admitted":
        _require(
            value["no_admission_evidence_sha256"] is not None
            and all(
                value[key] is None
                for key in (
                    "allocation_binding_sha256",
                    "drain_evidence_sha256",
                    "child_generation",
                )
            ),
            "never allocated evidence",
        )
        if budget is not None:
            _require(
                all(budget[key + ("" if legacy else "_us")] is None for key in TIMESTAMPS[:3]),
                "never allocated phase",
            )
    else:
        _require(
            binding is not None
            and value["allocation_binding_sha256"] is not None
            and value["drain_evidence_sha256"] is not None
            and value["no_admission_evidence_sha256"] is None,
            "allocated evidence binding",
        )
        # No-child allocated release is source-shaped evidence only. The separate
        # physical verifier must prove no launch; a hash does not establish that.
        _require(state != "completed" or child is not None, "completed child missing")
    if child is not None:
        _generation(child, child=True)
    if not legacy:
        recorded_boot = value["recorded_clock"]["boot_id"]
        if binding is not None:
            _require(
                recorded_boot == binding["server_generation"]["boot_id"],
                "cross-boot terminal needs separate recovery authority",
            )
        if budget is not None:
            _require(
                budget["clock"]["boot_id"] == recorded_boot
                and (
                    budget["admitted_boottime_us"] is None
                    or budget["admitted_boottime_us"] <= value["recorded_boottime_us"]
                ),
                "terminal original clock",
            )
        if child is not None:
            _require(child["boot_id"] == recorded_boot, "terminal child clock")


def validate_terminal(raw: bytes, contract: ControlContract) -> dict[str, Any]:
    """Validate new v2 metadata only; never attest physical release from hashes."""
    value = decode(raw)
    validate_shape(value, "terminal.schema.json", contract)
    _terminal_checks(value, legacy=False, contract=contract)
    return value


@dataclass(frozen=True)
class LegacyTerminalEvidence:
    """Immutable v1 bytes, with no new admission/output/release authority."""

    canonical_bytes: bytes
    receipt_sha256: str
    cleanup_only: bool = True


def read_legacy_terminal(raw: bytes, contract: ControlContract) -> LegacyTerminalEvidence:
    value = decode(raw)
    validate_shape(value, "legacy-terminal.schema.json", contract)
    _terminal_checks(value, legacy=True, contract=contract)
    return LegacyTerminalEvidence(raw, value["receipt_sha256"])


def validate_request(raw: bytes, contract: ControlContract) -> dict[str, Any]:
    value = decode(raw, limit=REQUEST_LIMIT - 1)
    validate_shape(value, "request.schema.json", contract)
    return value


def derive_response_schema(
    request_bytes: bytes, contract: ControlContract, output_contract: output.OutputContract
) -> dict[str, Any]:
    """Original infer frame specializes a recursively closed completed-result shape."""
    selected = output.instantiate_result_schema(request_bytes, output_contract)
    request = decode(request_bytes)
    result_schema = selected["$defs"][request["profile_id"]]
    schema = deepcopy(contract.schemas["response.template.schema.json"])
    schema["$defs"]["result"] = result_schema
    return schema


def validate_cleanup_grant(value: dict[str, Any], contract: ControlContract) -> None:
    """Check exact original identity, never grant current ACL or task authority."""
    validate_shape(value, "cleanup-grant.schema.json", contract)
    _generation(value["caller_generation"])
    _generation(value["resolver_generation"])
    _require(
        value["control_schema_sha256"] == contract.schema_hash("response.template.schema.json"),
        "cleanup schema pin",
    )
    binding, authorization = (
        value[key] for key in ("original_admission_binding", "original_cleanup_authorization")
    )
    if binding is None:
        _require(
            value["original_admission_binding_sha256"] is None
            and authorization is not None
            and digest(authorization) == value["original_cleanup_authorization_sha256"]
            and authorization["target"] == value["target"],
            "tombstone cleanup authority",
        )
        binding = authorization["binding"]
    else:
        _require(
            digest(binding) == value["original_admission_binding_sha256"]
            and authorization is None
            and value["original_cleanup_authorization_sha256"] is None,
            "original admission cleanup authority",
        )
    _require(
        binding["caller_generation"] == value["caller_generation"]
        and digest(value["caller_generation"]) == value["target"]["original_peer_generation_sha256"]
        and binding["profile_id"] == value["profile_id"]
        and binding["profile_pin"]["deployment_digest"] == value["deployment_digest"],
        "cleanup target identity",
    )


def validate_capability(
    value: dict[str, Any], contract: ControlContract, *, boot_id: str, now_us: int
) -> None:
    """Freshness/identity check only; caller must separately authenticate policy/peer."""
    validate_shape(value, "capability.schema.json", contract)
    _require(type(now_us) is int and 0 <= now_us <= SAFE_INTEGER, "integer current clock")
    issued, expires = value["issued_boottime_us"], value["expires_boottime_us"]
    _require(
        value["clock"]["boot_id"] == boot_id
        and issued <= now_us < expires
        and 0 < expires - issued <= 60 * MICROSECONDS,
        "capability clock/freshness",
    )
    if "cleanup_grant" in value:
        grant = value["cleanup_grant"]
        validate_cleanup_grant(grant, contract)
        _require(
            digest(grant) == value["cleanup_grant_sha256"]
            and grant["resolver_generation"]["boot_id"] == boot_id,
            "cleanup capability binding",
        )
        return
    binding = value.get("admission_binding", value.get("control_binding"))
    if not isinstance(binding, dict):
        raise ContractError("capability binding missing")
    validate_binding(binding, contract)
    _require(binding["server_generation"]["boot_id"] == boot_id, "capability server boot")
    if "control_binding" in value:
        _require(
            value["target"]["original_peer_generation_sha256"]
            == digest(binding["caller_generation"]),
            "retained caller identity",
        )
        return
    _require(
        value["admission_binding_sha256"] == digest(binding)
        and value["server_generation_sha256"] == digest(binding["server_generation"])
        and value["caller_generation_sha256"] == digest(binding["caller_generation"]),
        "capability digest",
    )
    for key in (
        "caller_generation",
        "policy_sha256",
        "source_fingerprints",
        "profile_id",
        "infer_schema",
        "control_schema",
    ):
        _require(value[key] == binding[key], "capability stable identity")
    for key, item in binding["profile_pin"].items():
        _require(value[key] == item, "capability profile identity")
    _require(
        value["frame_us"] <= 5 * MICROSECONDS and value["call_us"] <= 10 * MICROSECONDS,
        "control transport bounds",
    )


def validate_response(
    raw: bytes,
    request_bytes: bytes,
    contract: ControlContract,
    *,
    boot_id: str,
    now_us: int,
    original_infer_bytes: bytes | None = None,
    output_contract: output.OutputContract | None = None,
) -> dict[str, Any]:
    """Check a correlated response; this never resolves an intent or trusts release."""
    request = validate_request(request_bytes, contract)
    value = decode(raw, limit=FRAME_LIMIT - 1)
    schema = contract.schemas["response.template.schema.json"]
    if original_infer_bytes is not None and output_contract is not None:
        schema = derive_response_schema(original_infer_bytes, contract, output_contract)
    _check(value, schema, schema["$defs"])
    _require(
        value["op"] == request["op"] and value["control_id"] == request["control_id"],
        "control response correlation",
    )
    if not value["ok"]:
        _require(
            value["error"]["retryable"]
            == (value["error"]["code"] in {"busy", "deadline_exceeded", "internal_unavailable"}),
            "retryable error variant",
        )
        return value
    data = value["data"]
    if request["op"] == "capability":
        capability = data["capability"]
        validate_capability(capability, contract, boot_id=boot_id, now_us=now_us)
        _require(digest(capability) == value["capability_sha256"], "capability response hash")
        if "cleanup_grant" in capability:
            authority = capability["cleanup_grant"]
            _require(authority["target"] == request["target"], "capability exact cleanup target")
            profile, deployment = authority["profile_id"], authority["deployment_digest"]
        else:
            binding = capability.get("admission_binding", capability.get("control_binding"))
            profile, deployment = binding["profile_id"], binding["profile_pin"]["deployment_digest"]
            _require(capability.get("target") == request["target"], "capability exact target")
        _require(
            (profile, deployment) == (request["profile_id"], request["deployment_digest"]),
            "capability requested profile",
        )
        return value
    _require(
        value["capability_sha256"] == request["expected_capability_sha256"], "response capability"
    )
    if "cleanup_grant" in data:
        grant = data["cleanup_grant"]
        validate_cleanup_grant(grant, contract)
        _require(
            digest(grant) == data["cleanup_grant_sha256"]
            and request["op"] in grant["operations"]
            and grant["target"] == request["target"]
            and grant["profile_id"] == request["profile_id"]
            and grant["deployment_digest"] == request["deployment_digest"],
            "response cleanup grant",
        )
        data = data["observation"]
    _require(data["target"] == request["target"], "observation target")
    first, cancel_clock = data["first_cancel_boottime_us"], data["cancel_clock"]
    _require(
        data["cancel_requested"] == (first is not None) == (cancel_clock is not None),
        "cancel observation variant",
    )
    terminal, result = data["terminal_receipt"], data["result"]
    is_terminal = data["state"] in {"completed", "canceled", "expired", "failed"}
    _require(is_terminal == (terminal is not None), "terminal state presence")
    if terminal is None:
        _require(result is None, "nonterminal result")
        if data["state"] == "unknown":
            _require(data["cancel_requested"] is False, "unknown is not a tombstone")
        return value
    validate_terminal(canonical(terminal), contract)
    _require(
        terminal["terminal_state"] == data["state"]
        and terminal["request_id"] == data["target"]["request_id"]
        and terminal["request_sha256"] == data["target"]["request_sha256"]
        and digest(terminal["original_principal"])
        == data["target"]["original_peer_generation_sha256"]
        and terminal["profile_id"] == request["profile_id"]
        and terminal["deployment_digest"] == request["deployment_digest"],
        "terminal target",
    )
    if terminal["terminal_state"] != "completed":
        _require(result is None, "noncompleted result")
        return value
    _require(
        result is not None and original_infer_bytes is not None and output_contract is not None,
        "completed original-request validator required",
    )
    if original_infer_bytes is None or output_contract is None:
        raise ContractError("completed validation context missing")
    infer = decode(original_infer_bytes)
    _require(
        hashlib.sha256(original_infer_bytes).hexdigest() == terminal["request_sha256"]
        and infer["request_id"] == terminal["request_id"]
        and infer["profile_id"] == terminal["profile_id"]
        and infer["deployment_digest"] == terminal["deployment_digest"]
        and terminal["admission_binding"]["profile_pin"]["output_contract"]["bundle_sha256"]
        == output_contract.bundle_sha256
        and digest(result) == terminal["result_sha256"],
        "completed original result pins",
    )
    child = terminal["child_generation"]
    generation = {key: child[key] for key in ("unit", "invocation_id", "control_group")}
    generation["main_pid"] = child["pid"]
    budget = terminal["original_budget"]
    output.validate_result(
        original_infer_bytes,
        canonical(result),
        output_contract,
        expected_generation=generation,
        context_tokens=budget["context_tokens"],
        max_output_tokens=budget["max_output_tokens"],
    )
    return value


def validate_terminal_evidence(
    raw: bytes,
    contract: ControlContract,
    *,
    expected_target: dict[str, Any],
    original_infer_bytes: bytes | None = None,
    output_contract: output.OutputContract | None = None,
) -> dict[str, Any]:
    """Read current v1 proof preimages inside a v2 container, without rewriting.

    Checks consistency, NOT provenance or physical truth. A separate authenticated
    resolver/proof verifier is mandatory before any journal resolution. This
    adapter never admits v1 evidence as a new v2 terminal or inference authority.
    """
    value = decode(raw)
    validate_shape(value, "terminal-evidence.schema.json", contract)
    _require(value["target"] == expected_target, "evidence target")
    terminal_raw = value["terminal_canonical"].encode("utf-8")
    read_legacy_terminal(terminal_raw, contract)
    terminal = decode(terminal_raw)
    _require(
        terminal["request_id"] == expected_target["request_id"]
        and terminal["request_sha256"] == expected_target["request_sha256"]
        and digest(terminal["original_principal"])
        == expected_target["original_peer_generation_sha256"],
        "evidence original target",
    )
    parts: dict[str, Any] = {}
    defs = contract.schemas["terminal-evidence.schema.json"]["$defs"]
    for name, root, hash_key in (
        ("allocation", "legacy_allocation", "allocation_binding_sha256"),
        ("drain", "legacy_drain", "drain_evidence_sha256"),
        ("no_admission", "legacy_no_admission", "no_admission_evidence_sha256"),
    ):
        encoded = value[name + "_canonical"]
        if encoded is None:
            _require(terminal[hash_key] is None, "missing preimage")
            parts[name] = None
            continue
        part = decode(encoded.encode("utf-8"))
        _check(part, defs[root], defs)
        _require(digest(part) == terminal[hash_key], "preimage hash")
        parts[name] = part
    allocation, drain, no_admission = (
        parts[key] for key in ("allocation", "drain", "no_admission")
    )
    if no_admission is not None:
        _require(
            no_admission["request_id"] == terminal["request_id"]
            and no_admission["request_sha256"] == terminal["request_sha256"]
            and no_admission["principal_sha256"] == digest(terminal["original_principal"])
            and no_admission["reason"] == terminal["reason_code"]
            and no_admission["cancel_before_intent"] == (terminal["admission_binding"] is None),
            "no-admission preimage identity",
        )
    if allocation is not None:
        _require(drain is not None, "allocated drain missing")
        principal, lease = terminal["original_principal"], allocation["lease"]
        budget = terminal["original_budget"]
        _require(
            allocation["original_principal"] == principal
            and allocation["admission_binding_sha256"] == terminal["admission_binding_sha256"]
            and allocation["request_sha256"] == terminal["request_sha256"]
            and lease["request_id"] == terminal["request_id"]
            and lease["owner_identity"]
            == {key: principal[key] for key in ("pid", "start_ticks", "boot_id")}
            and lease["owner_unit"] == principal["unit"]
            and lease["owner_invocation_id"] == principal["invocation_id"]
            and budget is not None
            and allocation["original_deadline"] == budget["envelope_deadline"]
            and allocation["original_budget"]
            == {key: budget[key] for key in allocation["original_budget"]},
            "allocation original identity/budget",
        )
        _require(
            drain["allocation_binding_sha256"] == terminal["allocation_binding_sha256"]
            and drain["child_generation"] == terminal["child_generation"]
            and drain["boot_id"] == terminal["recorded_boot_id"]
            and drain["observed_boottime"] <= terminal["recorded_boottime"],
            "drain terminal binding",
        )
        if drain["child_generation"] is None:
            _require(drain["handoff_stage"] not in {"start", "go"}, "unbound late-start fence")
        else:
            _require(
                drain["kind"] == "physical_drain"
                and drain["never_started"] is False
                and drain["child_generation"]["boot_id"] == drain["boot_id"]
                and drain["child_intent"]["unit"] == drain["child_generation"]["unit"],
                "exact child drain",
            )
        if drain["kind"] == "physical_drain" and drain["child_generation"] is None:
            _require(
                drain["never_started"] is True
                and drain["handoff_stage"] == "plan"
                and drain["observed_boottime"] >= drain["late_start_fence"],
                "planned child fence",
            )
    result_raw = value["result_canonical"]
    _require(
        (terminal["terminal_state"] == "completed") == (result_raw is not None),
        "evidence result variant",
    )
    if result_raw is not None:
        _require(
            original_infer_bytes is not None and output_contract is not None,
            "original result validator required",
        )
        if original_infer_bytes is None or output_contract is None:
            raise ContractError("original result validator missing")
        result_bytes = result_raw.encode("utf-8")
        result = decode(result_bytes, limit=output.RESULT_LIMIT)
        request = decode(original_infer_bytes)
        _require(
            hashlib.sha256(original_infer_bytes).hexdigest() == terminal["request_sha256"]
            and request["request_id"] == terminal["request_id"]
            and request["profile_id"] == terminal["profile_id"]
            and request["deployment_digest"] == terminal["deployment_digest"]
            and digest(result) == terminal["result_sha256"]
            and terminal["admission_binding"]["profile_pin"].get("output_contract")
            == {"name": output.NAME, "version": 2, "bundle_sha256": output_contract.bundle_sha256},
            "legacy completed result pins",
        )
        child = terminal["child_generation"]
        generation = {key: child[key] for key in ("unit", "invocation_id", "control_group")}
        generation["main_pid"] = child["pid"]
        budget = terminal["original_budget"]
        output.validate_result(
            original_infer_bytes,
            result_bytes,
            output_contract,
            expected_generation=generation,
            context_tokens=budget["context_tokens"],
            max_output_tokens=budget["max_output_tokens"],
        )
    return value


def write_bundle(directory: Path) -> str:
    """Authoring-only deterministic artifacts; does not install or enable anything."""
    directory.mkdir(parents=True, exist_ok=True)
    schemas = {}
    for filename, root in ROOTS.items():
        schema = schema_document(root)
        (directory / filename).write_bytes(canonical(schema) + b"\n")
        schemas[filename] = digest(schema)
    bundle = {
        "name": NAME,
        "version": VERSION,
        "schemas": schemas,
        "adapter_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    (directory / "bundle.json").write_bytes(canonical(bundle) + b"\n")
    return digest(bundle)
