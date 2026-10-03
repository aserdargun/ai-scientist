"""Separately opted-in evidence-v3 codec; immutable v1 proof/budget bytes stay intact.

This is transport validation, not current authorization, physical proof or a
budget provider. The trusted store creates both exports from one transaction.
The codec never constructs the budget witness from the terminal it checks.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any

from lab.llm import aos_control_contract_v2 as metadata
from lab.llm import aos_evidence_transport as v2

SCHEMA = "aos-scientist-control-evidence.v3"
VERSION = 3
REQUEST_LIMIT = v2.REQUEST_LIMIT
RESPONSE_LIMIT = v2.RESPONSE_LIMIT
EVIDENCE_SCHEMA_SHA256 = v2.EVIDENCE_SCHEMA_SHA256
TransportError = v2.TransportError


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise TransportError(reason)


def _ref(name: str) -> dict[str, str]:
    return {"$ref": "#/$defs/" + name}


def _nullable(value: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [value, {"type": "null"}]}


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _transport_schema() -> dict[str, Any]:
    schema = v2.schema_document()
    definitions = schema["$defs"]
    witness = {
        "schema": {"type": "string", "const": "aos-scientist-original-budget-witness.v1"},
        "version": {"type": "integer", "const": 1},
        "target": _ref("target"),
        "profile_id": _ref("profile"),
        "deployment_digest": _ref("hash"),
        "profile_config_sha256": _nullable(_ref("hash")),
        "response_schema_sha256": _nullable(_ref("hash")),
        "original_admission_binding_sha256": _nullable(_ref("hash")),
        "original_cleanup_authorization_sha256": _nullable(_ref("hash")),
        "allocation_binding_sha256": _nullable(_ref("hash")),
        "budget_canonical": {
            "type": "string",
            "minLength": 2,
            "maxLength": REQUEST_LIMIT,
            "contentMediaType": "application/json",
            "contentSchema": _ref("legacy_budget"),
        },
        "budget_sha256": _ref("hash"),
    }
    definitions["original_budget_witness"] = _object(witness)
    definitions["retained_evidence"] = _object(
        {
            "evidence": _ref("terminal_evidence"),
            "original_budget_witness": _ref("original_budget_witness"),
        }
    )
    for root in ("evidence_request", "evidence_response"):
        for variant in definitions[root]["oneOf"]:
            properties = variant["properties"]
            properties["schema"] = {"type": "string", "const": SCHEMA}
            properties["version"] = {"type": "integer", "const": VERSION}
            if root == "evidence_response" and properties["op"].get("const") == "reconcile":
                properties["data"] = _ref("retained_evidence")
    return schema


_SCHEMA_BYTES = metadata.canonical(_transport_schema())
EVIDENCE_TRANSPORT_SCHEMA_HASH = hashlib.sha256(_SCHEMA_BYTES).hexdigest()


def schema_document() -> dict[str, Any]:
    """Complete self-contained schema copy; no mutable global validation state."""
    return metadata.decode(_SCHEMA_BYTES, limit=metadata.SCHEMA_LIMIT)


def _check(value: dict[str, Any], root: str) -> None:
    schema = schema_document()
    try:
        metadata._check(value, _ref(root), schema["$defs"])
    except metadata.ContractError as exc:
        raise TransportError(str(exc)) from exc


def _pins(value: dict[str, Any]) -> None:
    _require(
        value["evidence_schema_sha256"] == EVIDENCE_SCHEMA_SHA256
        and value["transport_schema_sha256"] == EVIDENCE_TRANSPORT_SCHEMA_HASH,
        "capability_mismatch",
    )


def decode_request(raw: bytes) -> dict[str, Any]:
    """Exact v3 request/pins; never infer a protocol version or accept v2 pins."""
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
        raise TransportError(str(exc)) from exc


def _v2_request(request: dict[str, Any]) -> dict[str, Any]:
    """Internal shape-validation projection AFTER authentic v3 pins were checked."""
    return {
        **deepcopy(request),
        "schema": v2.SCHEMA,
        "version": 2,
        "transport_schema_sha256": v2.EVIDENCE_TRANSPORT_SCHEMA_HASH,
    }


def _validate_original_capability(request: dict[str, Any], capability: dict[str, Any]) -> None:
    _require(
        metadata.digest(capability) == request["expected_capability_sha256"],
        "original retained capability hash",
    )
    data = {
        "capability": capability,
        "admission": "denied",
        "reason_code": "cleanup_only" if "cleanup_grant" in capability else "retained_target_only",
    }
    projected = _v2_request(request)
    projected.update(op="capability", expected_capability_sha256=None)
    # v2 checks original target-capability shape and identity only. It grants no
    # authority, and its schema hash is never accepted as a v3 wire pin.
    v2.success_response(projected, data, metadata.digest(capability))


def _budget_links(
    data: dict[str, Any], request: dict[str, Any], capability: dict[str, Any]
) -> None:
    witness, evidence = data["original_budget_witness"], data["evidence"]
    _validate_original_capability(request, capability)
    _require(
        witness["target"] == evidence["target"] == request["target"]
        and witness["profile_id"] == request["profile_id"]
        and witness["deployment_digest"] == request["deployment_digest"],
        "witness target/profile",
    )
    definitions = schema_document()["$defs"]
    terminal = metadata.decode(evidence["terminal_canonical"].encode("utf-8"))
    metadata._check(terminal, _ref("legacy_terminal"), definitions)
    _require(
        terminal["request_id"] == request["target"]["request_id"]
        and terminal["request_sha256"] == request["target"]["request_sha256"]
        and metadata.digest(terminal["original_principal"])
        == request["target"]["original_peer_generation_sha256"]
        and metadata.digest(
            {key: value for key, value in terminal.items() if key != "receipt_sha256"}
        )
        == terminal["receipt_sha256"],
        "terminal original identity/hash",
    )
    for key in (
        "profile_id",
        "deployment_digest",
        "profile_config_sha256",
        "response_schema_sha256",
        "allocation_binding_sha256",
    ):
        _require(witness[key] == terminal[key], "witness terminal pins")
    budget_raw = witness["budget_canonical"].encode("utf-8")
    _require(len(budget_raw) <= REQUEST_LIMIT, "budget byte bound")
    if budget_raw == b"null":
        raise TransportError("unsupported_schema")
    budget = metadata.decode(budget_raw, limit=REQUEST_LIMIT)
    metadata._check(budget, _ref("legacy_budget"), definitions)
    _require(
        metadata.digest(budget) == witness["budget_sha256"]
        and metadata.canonical(budget) == metadata.canonical(terminal["original_budget"]),
        "independently supplied budget witness mismatch",
    )
    if budget is not None:
        metadata._budget(budget, legacy=True)
    original_admission = witness["original_admission_binding_sha256"]
    original_cleanup = witness["original_cleanup_authorization_sha256"]
    _require(
        (original_admission is None) != (original_cleanup is None)
        and original_admission == terminal["admission_binding_sha256"],
        "original authority variant",
    )
    if "cleanup_grant" in capability:
        grant = capability["cleanup_grant"]
        _require(
            grant["original_admission_binding_sha256"] == original_admission
            and grant["original_cleanup_authorization_sha256"] == original_cleanup,
            "cleanup grant original authority",
        )
        binding = grant["original_admission_binding"]
        if binding is None:
            binding = grant["original_cleanup_authorization"]["binding"]
    else:
        binding = capability["control_binding"]
        if original_admission is None:
            authorization = {
                "target": request["target"],
                "binding": binding,
                "operations": ["cancel"],
            }
            _require(metadata.digest(authorization) == original_cleanup, "tombstone authority hash")
    _require(binding["caller_generation"] == terminal["original_principal"], "original caller")
    if original_admission is not None:
        _require(
            metadata.digest(binding) == original_admission
            and binding == terminal["admission_binding"],
            "original admission binding",
        )
    else:
        _require(
            terminal["admission_binding"] is None
            and terminal["release_outcome"] == "never_admitted"
            and terminal["terminal_state"] == "canceled",
            "tombstone terminal variant",
        )
    for outer, inner in (
        ("profile_config_sha256", "config_sha256"),
        ("response_schema_sha256", "response_schema_sha256"),
    ):
        _require(witness[outer] == binding["profile_pin"][inner], "original profile pins")
    if budget is not None:
        _require(
            (budget["admitted_boottime"] is not None) == (original_admission is not None),
            "original budget admission variant",
        )
    else:
        _require(original_admission is None, "admitted budget missing")
    allocation_raw = evidence["allocation_canonical"]
    if allocation_raw is None:
        _require(witness["allocation_binding_sha256"] is None, "missing allocation preimage")
    else:
        allocation = metadata.decode(allocation_raw.encode("utf-8"))
        metadata._check(allocation, _ref("legacy_allocation"), definitions)
        _require(
            metadata.digest(allocation) == witness["allocation_binding_sha256"]
            and allocation["admission_binding_sha256"] == original_admission
            and allocation["request_sha256"] == request["target"]["request_sha256"]
            and allocation["lease"]["request_id"] == request["target"]["request_id"]
            and allocation["original_principal"] == terminal["original_principal"]
            and budget is not None
            and allocation["original_deadline"] == budget["envelope_deadline"]
            and allocation["original_budget"]
            == {key: budget[key] for key in allocation["original_budget"]},
            "allocation budget witness binding",
        )


def validate_response(
    raw: bytes, request: dict[str, Any], *, expected_capability: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Validate v3 envelope and original budget links, never authorize resolution.

    Successful reconcile needs the already retained original target capability
    to bind cleanup hashes absent from the terminal. Current authority/freshness
    and trusted physical proof remain mandatory external checks.
    """
    request = decode_request(metadata.canonical(request))
    try:
        response = metadata.decode(raw, limit=RESPONSE_LIMIT - 1)
        _check(response, "evidence_response")
        _require(
            response["control_id"] == request["control_id"] and response["op"] == request["op"],
            "response correlation",
        )
        projected = deepcopy(response)
        projected.update(schema=v2.SCHEMA, version=2)
        if response["ok"] and request["op"] == "capability":
            _pins(response["data"])
            projected["data"]["transport_schema_sha256"] = v2.EVIDENCE_TRANSPORT_SCHEMA_HASH
        elif response["ok"]:
            if expected_capability is None:
                raise TransportError("retained target capability required")
            _budget_links(response["data"], request, expected_capability)
            projected["data"] = deepcopy(response["data"]["evidence"])
        v2.validate_response(metadata.canonical(projected), _v2_request(request))
        return response
    except metadata.ContractError as exc:
        raise TransportError(str(exc)) from exc


def success_response(
    request: dict[str, Any],
    data: dict[str, Any],
    capability_sha256: str,
    *,
    expected_capability: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
    return validate_response(
        metadata.canonical(response), request, expected_capability=expected_capability
    )


def encode_response(
    response: dict[str, Any],
    request: dict[str, Any],
    *,
    expected_capability: dict[str, Any] | None = None,
) -> bytes:
    raw = metadata.canonical(response)
    validate_response(raw, request, expected_capability=expected_capability)
    return raw
