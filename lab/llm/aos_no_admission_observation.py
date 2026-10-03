"""Proposed, unadmitted no-admission-observation.v1 shape/hash contract.

This module supplies no provider, transport, DB, process observation or authority.
Successful validation proves only consistency with separately supplied preimages;
those preimages are not authenticated here. It never proves physical absence,
allows closure, allocates a budget, releases GPU ownership or permits admission.
The historical AOS admission capture is preserved as a legacy static definition.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

NAME = "aos-scientist-no-admission-observation.v1"
FEATURE = "no-admission-observation.v1"
VERSION = 1
PURPOSE = "observe_no_admission"
SAFE_INTEGER = 2**53 - 1
MAX_AGE_US = 60_000_000
FRAME_LIMIT = 1024 * 1024
REQUEST_LIMIT = 128 * 1024
SCHEMA_PATH = (
    Path(__file__).parent / "contracts/no_admission_observation_v1/observation.schema.json"
)
LEGACY_RECORD_SCHEMA_SHA256 = "f2a3d671f8f56aa70833e59a51783254fd962dd4721d6a315199c8f219ddbe06"


class ObservationError(ValueError):
    """Proposed metadata is malformed or inconsistent; no authority follows."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ObservationError(reason)


def canonical(value: Any) -> bytes:
    """The existing UTF-8, finite-number canonical JSON convention."""
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ObservationError("invalid canonical JSON") from exc


def digest(value: Any) -> str:
    """SHA of canonical JSON bytes, never an authentication decision."""
    return hashlib.sha256(canonical(value)).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def _constant(_value: str) -> Any:
    raise ObservationError("nonfinite JSON constant")


def decode(raw: bytes, *, limit: int = FRAME_LIMIT, exact: bool = True) -> dict[str, Any]:
    """Reject duplicate keys, nonfinite numbers and noncanonical wire bytes."""
    _require(type(raw) is bytes and 0 < len(raw) <= limit, "JSON byte bound")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ObservationError("invalid JSON") from exc
    _require(type(value) is dict, "JSON object required")
    encoded = canonical(value)
    _require(not exact or encoded == raw, "noncanonical JSON")
    return cast(dict[str, Any], value)


def schema_document() -> dict[str, Any]:
    """Load only this standalone static schema, never runtime AOS definitions."""
    document = decode(SCHEMA_PATH.read_bytes(), exact=False)
    definitions = document["$defs"]
    names = {name for name in definitions if name.startswith("Scientist")}
    legacy = {
        "$schema": document["$schema"],
        "$defs": {
            name: definitions[name] for name in names if name != "ScientistAdmissionRecordV2"
        },
        **definitions["ScientistAdmissionRecordV2"],
    }
    _require(digest(legacy) == LEGACY_RECORD_SCHEMA_SHA256, "legacy static schema changed")
    return document


def schema_hash() -> str:
    """Full canonical schema SHA, including all recursive legacy definitions."""
    return digest(schema_document())


def source_hash() -> str:
    """SHA of this module's source bytes; a fingerprint, never a trust decision."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _check(value: Any, schema: dict[str, Any], definitions: dict[str, Any], depth: int = 0) -> None:
    """Strict interpreter for this closed schema's local vocabulary."""
    _require(depth <= 48, "schema depth bound")
    if "$ref" in schema:
        reference = schema["$ref"]
        _require(reference.startswith("#/$defs/"), "nonlocal schema reference")
        _check(value, definitions[reference[8:]], definitions, depth + 1)
        return
    if "anyOf" in schema:
        for variant in schema["anyOf"]:
            try:
                _check(value, variant, definitions, depth + 1)
                return
            except ObservationError:
                continue
        raise ObservationError("schema variant mismatch")
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
        _require(type(value) in (int, float), "nonboolean numeric type required")
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        _require(finite, "finite number required")
    else:
        _require(kind in types and type(value) is types[kind], "strict JSON type required")
    if "const" in schema:
        _require(canonical(value) == canonical(schema["const"]), "constant mismatch")
    if "enum" in schema:
        _require(value in schema["enum"], "enum mismatch")
    if kind == "object":
        properties = schema["properties"]
        _require(
            schema.get("additionalProperties") is False
            and set(value) == set(schema["required"]) == set(properties),
            "object closure",
        )
        for key, item in value.items():
            _check(item, properties[key], definitions, depth + 1)
    elif kind == "array":
        _require(schema.get("minItems", 0) <= len(value) <= schema["maxItems"], "array bound")
        if schema.get("uniqueItems"):
            _require(len({canonical(item) for item in value}) == len(value), "duplicate array item")
        for item in value:
            _check(item, schema["items"], definitions, depth + 1)
    elif kind == "string":
        _require(
            schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", FRAME_LIMIT),
            "string bound",
        )
        if "pattern" in schema:
            _require(re.search(schema["pattern"], value) is not None, "string pattern")
    elif kind in ("integer", "number"):
        # Legacy AOS emits ge/le for its seconds union. Enforce those explicitly.
        lower = schema.get("minimum", schema.get("ge", 0))
        upper = schema.get("maximum", schema.get("le", SAFE_INTEGER))
        _require(lower <= value <= upper, "numeric bound")


@dataclass(frozen=True)
class ObservationExpectations:
    """Independent canonical section preimages/pin, not authentication callbacks.

    Future trusted consumers must obtain these through separately reviewed
    sources. Supplying fields copied from the incoming observation establishes
    no independent evidence, and this type does not confer permission.
    """

    schema_sha256: str
    original: bytes
    observer: bytes
    canonical: bytes
    physical: bytes
    cleanup_scope: bytes


def _generation(generation: dict[str, Any]) -> None:
    group = generation["control_group"]
    _require(
        ".." not in PurePosixPath(group).parts
        and "\x00" not in group
        and "\n" not in group
        and not group.startswith("//")
        and str(PurePosixPath(group)) == group,
        "ambiguous generation cgroup",
    )


def _original(value: dict[str, Any]) -> None:
    original = value["original"]
    record = original["admission_record"]
    binding = record["admission_binding"]
    caller = binding["caller_generation"]
    broker = original["broker_generation"]
    target = value["target"]
    request = decode(original["request_canonical"].encode("utf-8"), limit=REQUEST_LIMIT)
    _require(
        set(request)
        == {"version", "op", "request_id", "profile_id", "deployment_digest", "payload"},
        "original request closure",
    )
    _require(
        type(request["version"]) is int
        and request["version"] == 1
        and request["op"] == "infer"
        and type(request["payload"]) is dict,
        "original request version/op/payload",
    )
    _require(
        target["request_id"] == request["request_id"] == record["request_id"]
        and target["request_sha256"] == digest(request) == record["request_sha256"]
        and target["original_caller_generation_sha256"] == digest(caller),
        "original exact target mismatch",
    )
    _require(
        request["profile_id"] == binding["profile_id"]
        and request["deployment_digest"] == binding["profile_pin"]["deployment_digest"],
        "original profile/deployment mismatch",
    )
    _require(
        original["admission_record_sha256"] == digest(record)
        and original["admission_binding_sha256"]
        == record["admission_binding_sha256"]
        == digest(binding),
        "original admission record/binding hash mismatch",
    )
    capture = {
        key: record[key]
        for key in ("admission_binding", "capability_sha256", "capability_freshness")
    }
    _require(original["admission_capture_sha256"] == digest(capture), "original capture hash")
    intent = original["intent_binding"]
    _require(
        original["intent_binding_sha256"] == record["intent_binding_sha256"] == digest(intent)
        and intent["session_id"] == record["session_id"],
        "original intent binding mismatch",
    )
    _require(
        broker == binding["server_generation"]
        and original["broker_generation_sha256"] == digest(broker),
        "original broker mismatch",
    )
    _require(digest(caller) != digest(broker), "caller and broker must be distinct")
    freshness = record["capability_freshness"]
    _require(
        caller["boot_id"] == broker["boot_id"] == freshness["boot_id"],
        "original generation/capture boot mismatch",
    )
    _require(
        0
        <= freshness["issued_boottime"]
        <= record["captured_boottime"]
        < freshness["expires_boottime"]
        <= SAFE_INTEGER,
        "original capture freshness mismatch",
    )
    _generation(caller)
    _generation(broker)


def _observer(value: dict[str, Any], expected_schema: str) -> None:
    observer = value["observer"]
    generation = observer["generation"]
    capability = observer["capability"]
    _require(
        observer["generation_sha256"] == digest(generation)
        and observer["capability_sha256"] == digest(capability),
        "observer hash mismatch",
    )
    _require(
        capability["schema_sha256"] == expected_schema
        and capability["generation_sha256"] == observer["generation_sha256"]
        and capability["source_sha256"] == observer["source_sha256"]
        and capability["config_sha256"] == observer["config_sha256"],
        "observer capability pins",
    )
    original = value["original"]
    _require(
        observer["generation_sha256"]
        not in (
            value["target"]["original_caller_generation_sha256"],
            original["broker_generation_sha256"],
        ),
        "original process cannot serve as current observer",
    )
    _generation(generation)


def _proofs(value: dict[str, Any]) -> None:
    target, original = value["target"], value["original"]
    physical, canonical_section = value["physical"], value["canonical"]
    tombstone = canonical_section["tombstone"]
    _require(
        physical["target"] == canonical_section["target"] == tombstone["target"] == target,
        "proof target mismatch",
    )
    _require(
        physical["caller_generation"]
        == original["admission_record"]["admission_binding"]["caller_generation"]
        and physical["caller_generation_sha256"] == target["original_caller_generation_sha256"]
        and physical["broker_generation"] == original["broker_generation"]
        and physical["broker_generation_sha256"] == original["broker_generation_sha256"],
        "physical original provenance mismatch",
    )
    seen: set[str] = set()
    for child in physical["children"]:
        generation = child["generation"]
        checksum = digest(generation)
        _require(
            child["generation_sha256"] == checksum and checksum not in seen,
            "child provenance hash/duplicate",
        )
        _require(
            generation["boot_id"] == original["broker_generation"]["boot_id"],
            "child original boot mismatch",
        )
        seen.add(checksum)
        _generation(generation)
    _require(
        physical["proof_sha256"]
        == digest({key: item for key, item in physical.items() if key != "proof_sha256"}),
        "physical proof hash",
    )
    _require(
        tombstone["store"] == canonical_section["store"]
        and tombstone["admission_record_sha256"] == original["admission_record_sha256"]
        and tombstone["intent_binding_sha256"] == original["intent_binding_sha256"]
        and tombstone["observer_generation_sha256"] == value["observer"]["generation_sha256"]
        and tombstone["physical_proof_sha256"] == physical["proof_sha256"]
        and tombstone["freshness"] == value["freshness"],
        "tombstone cross-binding mismatch",
    )
    _require(canonical_section["tombstone_sha256"] == digest(tombstone), "canonical tombstone hash")
    cleanup = value["cleanup_scope"]
    intent = original["intent_binding"]
    _require(
        cleanup["store"] == original["store"]
        and cleanup["session_id"] == intent["session_id"]
        and cleanup["runtime_id"] == intent["runtime_id"]
        and cleanup["original_generation"] == intent["generation"]
        and cleanup["current_generation"] > cleanup["original_generation"],
        "cleanup original scope mismatch",
    )


def validate_observation(
    raw: bytes, *, expected: ObservationExpectations, now_boottime_us: int, current_boot_id: str
) -> dict[str, Any]:
    """Check closed shape and independent preimage/hash consistency only.

    A successful return is proposed metadata, never trusted proof or permission.
    No time/process/DB/authority observation occurs; callers supply the clock and
    separate expected preimages. Existing terminal/budget verifiers are untouched.
    """
    _require(type(expected) is ObservationExpectations, "independent expectations required")
    document = schema_document()
    _require(expected.schema_sha256 == digest(document), "full observation schema pin mismatch")
    value = decode(raw)
    _check(value, document, document["$defs"])
    for field in ("original", "observer", "canonical", "physical", "cleanup_scope"):
        retained = getattr(expected, field)
        decode(retained)
        _require(canonical(value[field]) == retained, "independent " + field + " preimage mismatch")
    _original(value)
    _observer(value, expected.schema_sha256)
    _proofs(value)
    freshness = value["freshness"]
    _require(
        type(now_boottime_us) is int and 0 <= now_boottime_us <= SAFE_INTEGER,
        "current boottime requires bounded integer microseconds",
    )
    _require(
        type(current_boot_id) is str
        and current_boot_id == freshness["boot_id"] == value["observer"]["generation"]["boot_id"],
        "current observer boot mismatch",
    )
    observed, expires = freshness["observed_boottime_us"], freshness["expires_boottime_us"]
    _require(
        observed <= now_boottime_us < expires and 0 < expires - observed <= MAX_AGE_US,
        "stale/future/unbounded observation freshness",
    )
    _require(
        value["observation_sha256"]
        == digest({key: item for key, item in value.items() if key != "observation_sha256"}),
        "observation hash mismatch",
    )
    return value
