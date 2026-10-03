"""Explicit evidence transport v2 codec; original target authority stays v1.

No socket, policy, clock observation, capability minting or DB operation lives
here. An envelope version is not a new admission/terminal version. Reconcile
must be authorized from the persisted target-capability table by its caller.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path
from typing import Any

from lab.llm import aos_control_contract_v2 as metadata

SCHEMA = "aos-scientist-control-evidence.v2"
VERSION = 2
REQUEST_LIMIT = 8192
RESPONSE_LIMIT = 128 * 1024
_DIRECTORY = Path(__file__).with_name("contracts") / "control_v2"
_BUNDLE = metadata.decode((_DIRECTORY / "bundle.json").read_bytes(), exact=False)
_CONTRACT = metadata.load_contract(_DIRECTORY, metadata.digest(_BUNDLE))
_EVIDENCE_SCHEMA = _CONTRACT.schemas["terminal-evidence.schema.json"]
EVIDENCE_SCHEMA_SHA256 = metadata.digest(_EVIDENCE_SCHEMA)


class TransportError(ValueError):
    """The explicit transport contract was not met; no fallback is authorized."""

    def __init__(self, code: str) -> None:
        self.code = code if code in metadata.ERRORS else "invalid_frame"
        super().__init__(code)


EvidenceTransportError = TransportError


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise EvidenceTransportError(reason)


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _ref(name: str) -> dict[str, str]:
    return {"$ref": "#/$defs/" + name}


def _constant(value: Any) -> dict[str, Any]:
    kind = "boolean" if type(value) is bool else "integer" if type(value) is int else "string"
    return {"type": kind, "const": value}


def _transport_schema() -> dict[str, Any]:
    defs = deepcopy(_EVIDENCE_SCHEMA["$defs"])
    # Exact current stored target-capability variants, not new v2 authority.
    defs["cleanup_authorization"]["properties"]["binding"] = _ref("legacy_binding")
    grant = deepcopy(defs["cleanup_grant"])
    grant["properties"]["schema"] = _constant("aos-scientist-cleanup-grant.v1")
    grant["properties"]["version"] = _constant(1)
    grant["properties"]["original_admission_binding"] = {
        "anyOf": [_ref("legacy_binding"), {"type": "null"}]
    }
    defs["transport_cleanup_grant"] = grant
    seconds = {"type": "number", "minimum": 0, "maximum": metadata.SAFE_INTEGER}
    freshness = {"boot_id": _ref("boot"), "issued_boottime": seconds, "expires_boottime": seconds}
    operations = {
        "type": "array",
        "const": ["cancel", "reconcile", "status"],
        "items": {"type": "string", "enum": ["cancel", "reconcile", "status"]},
        "minItems": 3,
        "maxItems": 3,
    }
    defs["transport_target_capability"] = _object(
        {
            "target": _ref("target"),
            "control_binding": _ref("legacy_binding"),
            "operations": operations,
            **freshness,
        }
    )
    defs["transport_cleanup_capability"] = _object(
        {
            "cleanup_grant": _ref("transport_cleanup_grant"),
            "cleanup_grant_sha256": _ref("hash"),
            **freshness,
        }
    )
    # Pin syntax is schema-bound; exact values are enforced without a self-hash.
    pins = {"evidence_schema_sha256": _ref("hash"), "transport_schema_sha256": _ref("hash")}
    request = {
        "schema": _constant(SCHEMA),
        "version": _constant(2),
        "control_id": _ref("id"),
        "profile_id": _ref("profile"),
        "deployment_digest": _ref("hash"),
        "target": _ref("target"),
        **pins,
    }
    defs["evidence_request"] = {
        "oneOf": [
            _object(
                {
                    **request,
                    "op": _constant("capability"),
                    "expected_capability_sha256": {"type": "null"},
                }
            ),
            _object(
                {
                    **request,
                    "op": _constant("reconcile"),
                    "expected_capability_sha256": _ref("hash"),
                }
            ),
        ]
    }
    capability_data = {
        "oneOf": [
            _object(
                {
                    "capability": _ref(kind),
                    "admission": _constant("denied"),
                    "reason_code": _constant(reason),
                    **pins,
                }
            )
            for kind, reason in (
                ("transport_target_capability", "retained_target_only"),
                ("transport_cleanup_capability", "cleanup_only"),
            )
        ]
    }
    envelope = {
        "schema": _constant(SCHEMA),
        "version": _constant(2),
        "control_id": _ref("id"),
        "ok": _constant(True),
        "capability_sha256": _ref("hash"),
        "error": {"type": "null"},
    }
    defs["evidence_response"] = {
        "oneOf": [
            _object({**envelope, "op": _constant("capability"), "data": capability_data}),
            _object({**envelope, "op": _constant("reconcile"), "data": _ref("terminal_evidence")}),
            _object(
                {
                    **envelope,
                    "op": {"type": "string", "enum": ["capability", "reconcile"]},
                    "ok": _constant(False),
                    "capability_sha256": {"type": "null"},
                    "data": {"type": "null"},
                    "error": _object(
                        {
                            "code": {"type": "string", "enum": metadata.ERRORS},
                            "retryable": {"type": "boolean"},
                        }
                    ),
                }
            ),
        ]
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": defs,
        "oneOf": [_ref("evidence_request"), _ref("evidence_response")],
    }


_SCHEMA_BYTES = metadata.canonical(_transport_schema())
EVIDENCE_TRANSPORT_SCHEMA_HASH = hashlib.sha256(_SCHEMA_BYTES).hexdigest()


def schema_document() -> dict[str, Any]:
    """Return a copy of the complete pinned schema, never mutable validator state."""
    return metadata.decode(_SCHEMA_BYTES, limit=metadata.SCHEMA_LIMIT)


def _check(value: dict[str, Any], root: str) -> None:
    schema = schema_document()
    try:
        metadata._check(value, _ref(root), schema["$defs"])
    except metadata.ContractError as exc:
        raise EvidenceTransportError(str(exc)) from exc


def _pins(value: dict[str, Any]) -> None:
    if (
        value["evidence_schema_sha256"] != EVIDENCE_SCHEMA_SHA256
        or value["transport_schema_sha256"] != EVIDENCE_TRANSPORT_SCHEMA_HASH
    ):
        raise TransportError("unsupported_schema")


def decode_request(raw: bytes) -> dict[str, Any]:
    """Canonical JSON payload without LF; byte limit reserves the framing LF."""
    try:
        request = metadata.decode(raw, limit=REQUEST_LIMIT - 1)
        if request.get("schema") != SCHEMA:
            raise TransportError("unsupported_schema")
        if type(request.get("version")) is not int or request["version"] != VERSION:
            raise TransportError("unsupported_version")
        _check(request, "evidence_request")
        _pins(request)
        return request
    except metadata.ContractError as exc:
        raise EvidenceTransportError(str(exc)) from exc


def _capability_data(data: dict[str, Any], request: dict[str, Any], fingerprint: str) -> None:
    _pins(data)
    capability = data["capability"]
    _require(metadata.digest(capability) == fingerprint, "original target-capability hash")
    if "control_binding" in capability:
        binding = capability["control_binding"]
        target = capability["target"]
        boot = binding["server_generation"]["boot_id"]
    else:
        grant = capability["cleanup_grant"]
        _require(metadata.digest(grant) == capability["cleanup_grant_sha256"], "cleanup grant hash")
        binding = grant["original_admission_binding"]
        authorization = grant["original_cleanup_authorization"]
        if binding is None:
            _require(
                authorization is not None
                and grant["original_admission_binding_sha256"] is None
                and metadata.digest(authorization)
                == grant["original_cleanup_authorization_sha256"],
                "tombstone authority",
            )
            binding = authorization["binding"]
        else:
            _require(
                metadata.digest(binding) == grant["original_admission_binding_sha256"]
                and authorization is None
                and grant["original_cleanup_authorization_sha256"] is None,
                "original admission authority",
            )
        target, boot = grant["target"], grant["resolver_generation"]["boot_id"]
        _require(
            "reconcile" in grant["operations"]
            and grant["caller_generation"] == binding["caller_generation"]
            and grant["profile_id"] == request["profile_id"]
            and grant["deployment_digest"] == request["deployment_digest"],
            "cleanup scope",
        )
    _require(
        target == request["target"]
        and binding["profile_id"] == request["profile_id"]
        and binding["profile_pin"]["deployment_digest"] == request["deployment_digest"]
        and metadata.digest(binding["caller_generation"])
        == target["original_peer_generation_sha256"]
        and capability["boot_id"] == boot
        and capability["issued_boottime"] < capability["expires_boottime"],
        "target capability identity",
    )
    # Deliberately no current-clock check: exact retries preserve expired bytes.
    # The existing trusted authority callback decides freshness before use.


def validate_response(raw: bytes, request: dict[str, Any]) -> dict[str, Any]:
    """Validate transport/correlation, not physical truth or profile semantics.

    Evidence strings retain their full contentSchema annotations. The separate
    metadata.validate_terminal_evidence API verifies their nested preimages and
    request-derived result with original context; this codec does not grant proof.
    """
    request = decode_request(metadata.canonical(request))
    try:
        response = metadata.decode(raw, limit=RESPONSE_LIMIT - 1)
        _check(response, "evidence_response")
        _require(
            response["control_id"] == request["control_id"] and response["op"] == request["op"],
            "response correlation",
        )
        if not response["ok"]:
            error = response["error"]
            _require(
                error["retryable"]
                == (error["code"] in {"busy", "deadline_exceeded", "internal_unavailable"}),
                "error retryability",
            )
        elif request["op"] == "capability":
            _capability_data(response["data"], request, response["capability_sha256"])
        else:
            _require(
                response["capability_sha256"] == request["expected_capability_sha256"]
                and response["data"]["target"] == request["target"],
                "reconcile correlation",
            )
            for field in (
                "terminal_canonical",
                "allocation_canonical",
                "drain_canonical",
                "no_admission_canonical",
                "result_canonical",
            ):
                encoded = response["data"][field]
                if encoded is not None:
                    limit = 96 * 1024 if field == "result_canonical" else RESPONSE_LIMIT
                    metadata.decode(encoded.encode("utf-8"), limit=limit)
        return response
    except metadata.ContractError as exc:
        raise EvidenceTransportError(str(exc)) from exc


def success_response(
    request: dict[str, Any], data: dict[str, Any], capability_sha256: str
) -> dict[str, Any]:
    """Frame already-authorized data; capability payload is copied byte-for-byte semantically."""
    payload = deepcopy(data)
    if request["op"] == "capability":
        payload.update(
            evidence_schema_sha256=EVIDENCE_SCHEMA_SHA256,
            transport_schema_sha256=EVIDENCE_TRANSPORT_SCHEMA_HASH,
        )
    response = {
        "schema": SCHEMA,
        "version": VERSION,
        "control_id": request["control_id"],
        "op": request["op"],
        "ok": True,
        "capability_sha256": capability_sha256,
        "data": payload,
        "error": None,
    }
    return validate_response(metadata.canonical(response), request)


def encode_response(response: dict[str, Any], request: dict[str, Any]) -> bytes:
    """Return canonical payload without LF after validation and full byte bounds."""
    raw = metadata.canonical(response)
    validate_response(raw, request)
    return raw
