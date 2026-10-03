"""Synthetic v3 transport/budget correlation, no DB or physical-release proof."""

from copy import deepcopy
from pathlib import Path

import pytest
from test_aos_evidence_transport import BOOT, TARGET, target_data
from test_aos_evidence_transport import request as v2_request

from lab.llm import aos_control_contract_v2 as metadata
from lab.llm import aos_evidence_transport as v2
from lab.llm import aos_retained_evidence_transport as v3


def request(operation="reconcile", capability=None):
    value = v2_request(operation)
    value.update(
        schema=v3.SCHEMA, version=3, transport_schema_sha256=v3.EVIDENCE_TRANSPORT_SCHEMA_HASH
    )
    if capability is not None:
        value["expected_capability_sha256"] = metadata.digest(capability)
    return value


@pytest.fixture
def retained():
    capability = target_data()["capability"]
    binding = capability["control_binding"]
    # Independently supplied original configured/assigned fixture, never read
    # from a terminal JSON value when the witness is constructed.
    original_budget = {
        "activation_seconds": 10,
        "inference_seconds": 5,
        "total_seconds": 15,
        "queue_seconds": 45,
        "max_output_tokens": 16,
        "context_tokens": 1536,
        "activation_deadline": None,
        "inference_deadline": None,
        "total_deadline": None,
        "queue_deadline": 130.0,
        "envelope_deadline": 130.0,
        "admitted_boottime": 100.0,
        "boot_id": BOOT,
    }
    witness = {
        "schema": "aos-scientist-original-budget-witness.v1",
        "version": 1,
        "target": deepcopy(TARGET),
        "profile_id": binding["profile_id"],
        "deployment_digest": binding["profile_pin"]["deployment_digest"],
        "profile_config_sha256": binding["profile_pin"]["config_sha256"],
        "response_schema_sha256": binding["profile_pin"]["response_schema_sha256"],
        "original_admission_binding_sha256": metadata.digest(binding),
        "original_cleanup_authorization_sha256": None,
        "allocation_binding_sha256": None,
        "budget_canonical": metadata.canonical(original_budget).decode(),
        "budget_sha256": metadata.digest(original_budget),
    }
    proof = {
        "request_id": TARGET["request_id"],
        "request_sha256": TARGET["request_sha256"],
        "principal_sha256": TARGET["original_peer_generation_sha256"],
        "cancel_before_intent": False,
        "never_allocated": True,
        "reason": "caller_cancel",
    }
    terminal = {
        "schema": "aos-scientist-terminal.v1",
        "request_id": TARGET["request_id"],
        "request_sha256": TARGET["request_sha256"],
        "original_principal": binding["caller_generation"],
        "profile_id": witness["profile_id"],
        "deployment_digest": witness["deployment_digest"],
        "profile_config_sha256": witness["profile_config_sha256"],
        "response_schema_sha256": witness["response_schema_sha256"],
        "admission_binding": binding,
        "admission_binding_sha256": metadata.digest(binding),
        "original_budget": deepcopy(original_budget),
        "allocation_binding_sha256": None,
        "child_generation": None,
        "drain_evidence_sha256": None,
        "no_admission_evidence_sha256": metadata.digest(proof),
        "release_outcome": "never_admitted",
        "terminal_state": "canceled",
        "reason_code": "caller_cancel",
        "result_sha256": None,
        "recorded_boot_id": BOOT,
        "recorded_boottime": 120.5,
    }
    terminal["receipt_sha256"] = metadata.digest(terminal)
    evidence = {
        "schema": "aos-scientist-terminal-evidence.v2",
        "version": 2,
        "target": deepcopy(TARGET),
        "terminal_canonical": metadata.canonical(terminal).decode(),
        "allocation_canonical": None,
        "drain_canonical": None,
        "no_admission_canonical": metadata.canonical(proof).decode(),
        "result_canonical": None,
    }
    return capability, {"evidence": evidence, "original_budget_witness": witness}


def test_public_full_schema_matches_export_and_v2_remains_separate():
    path = (
        Path(__file__).resolve().parents[1]
        / "docs/ai-scientist/contracts/retained-evidence-transport-v3.schema.json"
    )
    schema = metadata.decode(path.read_bytes(), limit=metadata.SCHEMA_LIMIT, exact=False)
    assert metadata.digest(schema) == v3.EVIDENCE_TRANSPORT_SCHEMA_HASH
    assert v3.EVIDENCE_SCHEMA_SHA256 == v2.EVIDENCE_SCHEMA_SHA256
    assert v3.EVIDENCE_TRANSPORT_SCHEMA_HASH != v2.EVIDENCE_TRANSPORT_SCHEMA_HASH
    assert v2.decode_request(metadata.canonical(v2_request())) == v2_request()
    with pytest.raises(v3.TransportError):
        v3.decode_request(metadata.canonical(v2_request()))
    with pytest.raises(v2.TransportError):
        v2.decode_request(metadata.canonical(request("capability")))


def test_capability_payload_hash_and_historical_float_clocks_unchanged():
    data = target_data()
    original = metadata.canonical(data)
    reply = v3.success_response(request("capability"), data, metadata.digest(data["capability"]))
    assert metadata.canonical(data) == original
    assert reply["data"]["capability"] == data["capability"]
    assert reply["data"]["transport_schema_sha256"] == v3.EVIDENCE_TRANSPORT_SCHEMA_HASH
    assert reply["capability_sha256"] == metadata.digest(data["capability"])


def test_retained_budget_and_evidence_are_validated_without_mutation(retained):
    capability, data = retained
    before = metadata.canonical(data)
    req = request(capability=capability)
    reply = v3.success_response(
        req, data, metadata.digest(capability), expected_capability=capability
    )
    assert metadata.canonical(data) == before
    assert reply["data"] == data
    raw = v3.encode_response(reply, req, expected_capability=capability)
    assert v3.validate_response(raw, req, expected_capability=capability) == reply
    with pytest.raises(v3.TransportError):
        v3.validate_response(raw, req)


@pytest.mark.parametrize(
    "mutation",
    [
        "target",
        "profile",
        "config",
        "response",
        "admission",
        "cleanup",
        "allocation",
        "budget_hash",
        "budget_value",
        "extra",
    ],
)
def test_changed_witness_is_not_adopted(retained, mutation):
    capability, data = retained
    witness = data["original_budget_witness"]
    if mutation == "target":
        witness["target"]["request_sha256"] = "f" * 64
    elif mutation == "profile":
        witness["profile_id"] = "aos.bonsai.recovery.v1"
    elif mutation in {"config", "response", "admission", "cleanup", "allocation", "budget_hash"}:
        key = {
            "config": "profile_config_sha256",
            "response": "response_schema_sha256",
            "admission": "original_admission_binding_sha256",
            "cleanup": "original_cleanup_authorization_sha256",
            "allocation": "allocation_binding_sha256",
            "budget_hash": "budget_sha256",
        }[mutation]
        witness[key] = "f" * 64
    elif mutation == "budget_value":
        budget = metadata.decode(witness["budget_canonical"].encode())
        budget["envelope_deadline"] = 140.0
        witness["budget_canonical"] = metadata.canonical(budget).decode()
        witness["budget_sha256"] = metadata.digest(budget)
    else:
        witness["extra"] = True
    with pytest.raises(v3.TransportError):
        v3.success_response(
            request(capability=capability),
            data,
            metadata.digest(capability),
            expected_capability=capability,
        )


def test_tombstone_cleanup_authority_hash_is_bound_to_retained_capability(retained):
    capability, data = retained
    witness = data["original_budget_witness"]
    terminal = metadata.decode(data["evidence"]["terminal_canonical"].encode())
    original_cleanup = {
        "target": TARGET,
        "binding": capability["control_binding"],
        "operations": ["cancel"],
    }
    budget = metadata.decode(witness["budget_canonical"].encode())
    for key in ("admitted_boottime", "envelope_deadline", "queue_deadline"):
        budget[key] = None
    witness.update(
        original_admission_binding_sha256=None,
        original_cleanup_authorization_sha256=metadata.digest(original_cleanup),
        budget_canonical=metadata.canonical(budget).decode(),
        budget_sha256=metadata.digest(budget),
    )
    terminal.update(admission_binding=None, admission_binding_sha256=None, original_budget=budget)
    proof = metadata.decode(data["evidence"]["no_admission_canonical"].encode())
    proof["cancel_before_intent"] = True
    data["evidence"]["no_admission_canonical"] = metadata.canonical(proof).decode()
    terminal["no_admission_evidence_sha256"] = metadata.digest(proof)
    terminal["receipt_sha256"] = metadata.digest(
        {key: value for key, value in terminal.items() if key != "receipt_sha256"}
    )
    data["evidence"]["terminal_canonical"] = metadata.canonical(terminal).decode()
    assert v3.success_response(
        request(capability=capability),
        data,
        metadata.digest(capability),
        expected_capability=capability,
    )["ok"]
    witness["original_cleanup_authorization_sha256"] = "f" * 64
    with pytest.raises(v3.TransportError):
        v3.success_response(
            request(capability=capability),
            data,
            metadata.digest(capability),
            expected_capability=capability,
        )


def test_wrong_pin_float_version_and_oversized_response_fail(retained):
    capability, data = retained
    for bad in (
        {**request(), "version": 3.0},
        {**request(), "transport_schema_sha256": v2.EVIDENCE_TRANSPORT_SCHEMA_HASH},
    ):
        with pytest.raises(v3.TransportError):
            v3.decode_request(metadata.canonical(bad))
    data["evidence"]["terminal_canonical"] = " " * v3.RESPONSE_LIMIT
    with pytest.raises(v3.TransportError):
        v3.success_response(
            request(capability=capability),
            data,
            metadata.digest(capability),
            expected_capability=capability,
        )


def test_null_budget_variant_and_wrong_protocol_are_explicitly_unsupported(retained):
    capability, data = retained
    witness = data["original_budget_witness"]
    witness.update(budget_canonical="null", budget_sha256=metadata.digest(None))
    with pytest.raises(v3.TransportError) as error:
        v3.success_response(
            request(capability=capability),
            data,
            metadata.digest(capability),
            expected_capability=capability,
        )
    assert error.value.code == "unsupported_schema"
    for value, code in (
        (v2_request(), "unsupported_schema"),
        ({**request(), "version": 3.0}, "unsupported_version"),
    ):
        with pytest.raises(v3.TransportError) as error:
            v3.decode_request(metadata.canonical(value))
        assert error.value.code == code
