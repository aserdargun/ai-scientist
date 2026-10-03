"""Synthetic evidence codec bytes, no socket/DB/authority or GPU execution."""

from copy import deepcopy

import pytest
from aos_admission_fixture import BOOT, binding_for

from lab.llm import aos_control_contract_v2 as metadata
from lab.llm import aos_evidence_transport as codec

PEER = {
    "uid": 1000,
    "pid": 123,
    "start_ticks": 20,
    "boot_id": BOOT,
    "unit": "swapp-aos-gpu-fixture.service",
    "invocation_id": "1" * 32,
    "control_group": "/fixture/aos",
    "parent_pid": 122,
    "parent_start_ticks": 19,
}
PROFILE = "aos.decider.turn.v1"
DEPLOYMENT = "b" * 64
TARGET = {
    "request_id": "a" * 32,
    "request_sha256": "c" * 64,
    "original_peer_generation_sha256": metadata.digest(PEER),
}


def request(operation="capability"):
    return {
        "schema": codec.SCHEMA,
        "version": 2,
        "op": operation,
        "control_id": "d" * 32,
        "profile_id": PROFILE,
        "deployment_digest": DEPLOYMENT,
        "target": deepcopy(TARGET),
        "expected_capability_sha256": None if operation == "capability" else "e" * 64,
        "evidence_schema_sha256": codec.EVIDENCE_SCHEMA_SHA256,
        "transport_schema_sha256": codec.EVIDENCE_TRANSPORT_SCHEMA_HASH,
    }


def target_data():
    return {
        "capability": {
            "target": deepcopy(TARGET),
            "control_binding": binding_for(PEER, PROFILE, DEPLOYMENT, "c" * 64, "d" * 64),
            "operations": ["cancel", "reconcile", "status"],
            "boot_id": BOOT,
            "issued_boottime": 100.125,
            "expires_boottime": 160.125,
        },
        "admission": "denied",
        "reason_code": "retained_target_only",
    }


@pytest.mark.parametrize(
    "change",
    [
        "missing_pin",
        "wrong_pin",
        "target_null",
        "version_float",
        "extra",
        "wrong_op",
        "expected_hash",
        "legacy_namespace",
    ],
)
def test_explicit_request_shape_and_pins_fail_closed(change):
    value = request()
    if change == "missing_pin":
        value.pop("evidence_schema_sha256")
    elif change == "wrong_pin":
        value["transport_schema_sha256"] = "f" * 64
    elif change == "target_null":
        value["target"] = None
    elif change == "version_float":
        value["version"] = 2.0
    elif change == "extra":
        value["caller_drained"] = True
    elif change == "wrong_op":
        value["op"] = "cancel"
    elif change == "expected_hash":
        value["expected_capability_sha256"] = "f" * 64
    else:
        value["schema"] = "aos-scientist-control.v1"
    with pytest.raises(codec.TransportError) as error:
        codec.decode_request(metadata.canonical(value))
    assert error.value.code in metadata.ERRORS


def test_complete_schema_hash_and_copy_cannot_change_validator():
    schema = codec.schema_document()
    assert metadata.digest(schema) == codec.EVIDENCE_TRANSPORT_SCHEMA_HASH
    assert "legacy_terminal" in schema["$defs"]
    schema["$defs"].clear()
    assert codec.decode_request(metadata.canonical(request())) == request()


def test_original_v1_target_capability_is_preserved_with_explicit_wrapper_pins():
    data = target_data()
    before = metadata.canonical(data)
    fingerprint = metadata.digest(data["capability"])
    value = codec.success_response(request(), data, fingerprint)
    assert metadata.canonical(data) == before
    assert metadata.canonical(value["data"]["capability"]) == metadata.canonical(data["capability"])
    assert value["capability_sha256"] == fingerprint
    assert value["data"]["transport_schema_sha256"] == codec.EVIDENCE_TRANSPORT_SCHEMA_HASH
    assert set(value) == {
        "schema",
        "version",
        "op",
        "control_id",
        "ok",
        "capability_sha256",
        "data",
        "error",
    }
    assert codec.validate_response(codec.encode_response(value, request()), request()) == value
    # There is no current-clock conversion or refresh: expired exact replies
    # remain byte-identical; trusted runtime authority checks use-time freshness.
    assert value["data"]["capability"]["expires_boottime"] == 160.125


def test_infer_capability_and_unknown_nested_pin_cannot_be_exported():
    data = target_data()
    data["capability"]["admission_binding"] = data["capability"].pop("control_binding")
    with pytest.raises(codec.TransportError):
        codec.success_response(request(), data, metadata.digest(data["capability"]))
    data = target_data()
    data["capability"]["control_binding"]["profile_pin"]["output_contract"]["unknown"] = True
    with pytest.raises(codec.TransportError):
        codec.success_response(request(), data, metadata.digest(data["capability"]))


def test_reconcile_preserves_canonical_preimage_strings_and_denies_foreign_target():
    req = request("reconcile")
    data = {
        "schema": "aos-scientist-terminal-evidence.v2",
        "version": 2,
        "target": deepcopy(TARGET),
        "terminal_canonical": '{"synthetic":1.0}',
        "allocation_canonical": None,
        "drain_canonical": None,
        "no_admission_canonical": None,
        "result_canonical": None,
    }
    # Codec treats JSON preimages as immutable transport, not validated physical
    # evidence. metadata.validate_terminal_evidence independently rejects this stub.
    reply = codec.success_response(req, data, req["expected_capability_sha256"])
    assert reply["data"]["terminal_canonical"] == data["terminal_canonical"]
    data["target"]["request_sha256"] = "f" * 64
    with pytest.raises(codec.TransportError):
        codec.success_response(req, data, req["expected_capability_sha256"])


def test_duplicate_noncanonical_overflow_and_error_shapes():
    for raw in (
        b'{"version":2,"version":2}',
        metadata.canonical(request()) + b"\n",
        b" " * codec.REQUEST_LIMIT,
    ):
        with pytest.raises(codec.TransportError):
            codec.decode_request(raw)
    req = request("reconcile")
    error = {
        "schema": codec.SCHEMA,
        "version": 2,
        "op": "reconcile",
        "control_id": req["control_id"],
        "ok": False,
        "capability_sha256": None,
        "data": None,
        "error": {"code": "unauthorized", "retryable": False},
    }
    assert codec.validate_response(metadata.canonical(error), req) == error
    error["error"]["retryable"] = True
    with pytest.raises(codec.TransportError):
        codec.encode_response(error, req)
