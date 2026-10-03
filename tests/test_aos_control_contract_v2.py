"""Synthetic metadata only; no resolver authority, process, SQLite or GPU proof."""

import hashlib
import json
from copy import deepcopy
from fractions import Fraction
from pathlib import Path

import pytest
from aos_admission_fixture import BOOT, binding_for
from test_aos_profile_output import GENERATION, decider

from lab.llm import aos_control_contract_v2 as c
from lab.llm import aos_profile_output as output

DIRECTORY = Path(__file__).resolve().parents[1] / "lab/llm/contracts/control_v2"
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
CONFIGURED = {
    "activation_seconds": 10,
    "inference_seconds": 5,
    "total_seconds": 15,
    "queue_seconds": 45,
    "max_output_tokens": 16,
    "context_tokens": 1536,
}
ASSIGNED = {
    "activation_deadline": 110.0,
    "inference_deadline": 115.0,
    "total_deadline": 115.0,
    "envelope_deadline": 130.0,
    "queue_deadline": 130.0,
    "admitted_boottime": 100.0,
}


@pytest.fixture
def contract():
    bundle = json.loads((DIRECTORY / "bundle.json").read_bytes())
    return c.load_contract(DIRECTORY, c.digest(bundle))


@pytest.fixture
def completed(contract):
    raw, response = decider()
    request = json.loads(raw)
    output_dir = DIRECTORY.parent / "profile_output_v2"
    out_bundle = json.loads((output_dir / "bundle.json").read_bytes())
    out_contract = output.load_contract(output_dir, c.digest(out_bundle))
    binding = binding_for(
        PEER, request["profile_id"], request["deployment_digest"], "c" * 64, "d" * 64
    )
    binding["control_schema"] = {
        "name": c.CONTROL,
        "version": 2,
        "sha256": contract.schema_hash("response.template.schema.json"),
    }
    binding["terminal_schema"] = {
        "name": c.TERMINAL,
        "version": 2,
        "sha256": contract.schema_hash("terminal.schema.json"),
    }
    result = {
        "response": response,
        "usage": deepcopy(response["metrics"]),
        "generation": GENERATION,
    }
    child = {key: value for key, value in GENERATION.items() if key != "main_pid"}
    child.update(pid=GENERATION["main_pid"], start_ticks=77, boot_id=BOOT)
    terminal = {
        "schema": c.TERMINAL,
        "version": 2,
        "request_id": request["request_id"],
        "request_sha256": hashlib.sha256(raw).hexdigest(),
        "original_principal": PEER,
        "profile_id": request["profile_id"],
        "deployment_digest": request["deployment_digest"],
        "profile_config_sha256": "c" * 64,
        "response_schema_sha256": "d" * 64,
        "admission_binding": binding,
        "admission_binding_sha256": c.digest(binding),
        "original_budget": c.project_budget(CONFIGURED, ASSIGNED, BOOT),
        "allocation_binding_sha256": "e" * 64,
        "child_generation": child,
        "drain_evidence_sha256": "f" * 64,
        "no_admission_evidence_sha256": None,
        "release_outcome": "released",
        "terminal_state": "completed",
        "reason_code": "success",
        "result_sha256": c.digest(result),
        "recorded_clock": {"name": "CLOCK_BOOTTIME", "unit": "microseconds", "boot_id": BOOT},
        "recorded_boottime_us": 120_000_000,
    }
    terminal["receipt_sha256"] = c.digest(terminal)
    target = {
        "request_id": terminal["request_id"],
        "request_sha256": terminal["request_sha256"],
        "original_peer_generation_sha256": c.digest(PEER),
    }
    control_request = {
        "schema": c.CONTROL,
        "version": 2,
        "op": "reconcile",
        "control_id": "a" * 32,
        "expected_capability_sha256": "b" * 64,
        "target": target,
        "profile_id": request["profile_id"],
        "deployment_digest": request["deployment_digest"],
    }
    reply = {
        "schema": c.CONTROL,
        "version": 2,
        "op": "reconcile",
        "control_id": "a" * 32,
        "ok": True,
        "capability_sha256": "b" * 64,
        "error": None,
        "data": {
            "target": target,
            "state": "completed",
            "cancel_requested": False,
            "cancel_clock": None,
            "first_cancel_boottime_us": None,
            "terminal_receipt": terminal,
            "result": result,
        },
    }
    return raw, out_contract, control_request, reply


@pytest.mark.parametrize("seconds", [0, 0.000001, 0.1, 12.3456789, 9007199254.0])
def test_exact_floor_timestamp_ceil_duration(seconds):
    exact = Fraction(seconds) * 1_000_000
    assert c.boottime_us(seconds) == exact.numerator // exact.denominator
    assert c.duration_us(seconds) == -(-exact.numerator // exact.denominator)


@pytest.mark.parametrize("seconds", [True, -1, float("nan"), float("inf"), 2**53])
def test_unsafe_clock_inputs_deny(seconds):
    with pytest.raises(c.ContractError):
        c.boottime_us(seconds)
    with pytest.raises(c.ContractError):
        c.duration_us(seconds)


def test_budget_projection_preserves_none_and_copies_inputs():
    original = deepcopy(CONFIGURED)
    assigned = dict.fromkeys(c.TIMESTAMPS)
    result = c.project_budget(CONFIGURED, assigned, BOOT)
    assert all(result[key + "_us"] is None for key in c.TIMESTAMPS)
    assert CONFIGURED == original and assigned == dict.fromkeys(c.TIMESTAMPS)
    with pytest.raises(c.ContractError):
        c.project_budget({**CONFIGURED, "max_output_tokens": 16.0}, assigned, BOOT)


def test_all_packaged_object_schemas_are_closed_and_full_hash_pinned(contract):
    def walk(value):
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert value["additionalProperties"] is False
                assert set(value["required"]) <= set(value["properties"])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for filename, schema in contract.schemas.items():
        walk(schema)
        assert contract.schema_hash(filename) == c.digest(schema)
    assert contract.schemas["response.template.schema.json"]["$defs"]["result"] is False


def test_completed_response_requires_original_result_specialization(contract, completed):
    infer, output_contract, request, reply = completed
    with pytest.raises(c.ContractError):
        c.validate_response(
            c.canonical(reply), c.canonical(request), contract, boot_id=BOOT, now_us=120_000_000
        )
    assert (
        c.validate_response(
            c.canonical(reply),
            c.canonical(request),
            contract,
            boot_id=BOOT,
            now_us=120_000_000,
            original_infer_bytes=infer,
            output_contract=output_contract,
        )
        == reply
    )
    reply["data"]["result"]["response"]["prediction"]["probabilities"]["foreign"] = 0
    with pytest.raises(c.ContractError):
        c.validate_response(
            c.canonical(reply),
            c.canonical(request),
            contract,
            boot_id=BOOT,
            now_us=120_000_000,
            original_infer_bytes=infer,
            output_contract=output_contract,
        )


@pytest.mark.parametrize(
    "mutation", ["unknown", "version_float", "timestamp_float", "wrong_hash", "wrong_state"]
)
def test_terminal_nested_and_outcome_denials(contract, completed, mutation):
    terminal = deepcopy(completed[3]["data"]["terminal_receipt"])
    if mutation == "unknown":
        terminal["admission_binding"]["profile_pin"]["output_contract"]["extra"] = True
    elif mutation == "version_float":
        terminal["version"] = 2.0
    elif mutation == "timestamp_float":
        terminal["recorded_boottime_us"] = 120_000_000.0
    elif mutation == "wrong_hash":
        terminal["result_sha256"] = "0" * 64
    else:
        terminal["terminal_state"] = "canceled"
    if mutation != "wrong_hash":
        terminal["receipt_sha256"] = c.digest(
            {k: v for k, v in terminal.items() if k != "receipt_sha256"}
        )
    with pytest.raises(c.ContractError):
        c.validate_terminal(c.canonical(terminal), contract)


def test_legacy_preimage_evidence_preserves_bytes_and_never_upgrades(contract, completed):
    terminal = deepcopy(completed[3]["data"]["terminal_receipt"])
    terminal.pop("version")
    terminal.pop("recorded_clock")
    terminal.pop("recorded_boottime_us")
    terminal.update(
        schema="aos-scientist-terminal.v1",
        recorded_boot_id=BOOT,
        recorded_boottime=120.0,
        admission_binding=None,
        admission_binding_sha256=None,
        original_budget=None,
        allocation_binding_sha256=None,
        child_generation=None,
        drain_evidence_sha256=None,
        result_sha256=None,
        terminal_state="canceled",
        release_outcome="never_admitted",
        reason_code="caller_cancel",
    )
    target = completed[2]["target"]
    proof = {
        "request_id": target["request_id"],
        "request_sha256": target["request_sha256"],
        "principal_sha256": target["original_peer_generation_sha256"],
        "cancel_before_intent": True,
        "never_allocated": True,
        "reason": "caller_cancel",
    }
    terminal["no_admission_evidence_sha256"] = c.digest(proof)
    terminal["receipt_sha256"] = c.digest(
        {k: v for k, v in terminal.items() if k != "receipt_sha256"}
    )
    raw = c.canonical(terminal)
    legacy = c.read_legacy_terminal(raw, contract)
    assert legacy.canonical_bytes == raw and legacy.cleanup_only is True
    with pytest.raises(c.ContractError):
        c.validate_terminal(raw, contract)
    envelope = {
        "schema": "aos-scientist-terminal-evidence.v2",
        "version": 2,
        "target": target,
        "terminal_canonical": raw.decode(),
        "allocation_canonical": None,
        "drain_canonical": None,
        "no_admission_canonical": c.canonical(proof).decode(),
        "result_canonical": None,
    }
    assert (
        c.validate_terminal_evidence(c.canonical(envelope), contract, expected_target=target)
        == envelope
    )
    envelope["no_admission_canonical"] = c.canonical({**proof, "never_allocated": False}).decode()
    with pytest.raises(c.ContractError):
        c.validate_terminal_evidence(c.canonical(envelope), contract, expected_target=target)


def test_duplicate_noncanonical_and_oversized_frames_fail(contract, completed):
    raw = c.canonical(completed[2])
    for invalid in (b'{"version":2,"version":2}', raw + b"\n", b" " * c.REQUEST_LIMIT):
        with pytest.raises(c.ContractError):
            c.validate_request(invalid, contract)


def test_retained_capability_freshness_and_foreign_boot_fail_closed(contract, completed):
    binding = completed[3]["data"]["terminal_receipt"]["admission_binding"]
    capability = {
        "target": completed[2]["target"],
        "control_binding": binding,
        "operations": c.CLEANUP_OPS,
        "clock": {"name": "CLOCK_BOOTTIME", "unit": "microseconds", "boot_id": BOOT},
        "issued_boottime_us": 100_000_000,
        "expires_boottime_us": 160_000_000,
    }
    c.validate_capability(capability, contract, boot_id=BOOT, now_us=120_000_000)
    for boot_id, now in (
        (BOOT, 160_000_000),
        (BOOT, True),
        ("00000000-0000-0000-0000-000000000002", 120_000_000),
    ):
        with pytest.raises(c.ContractError):
            c.validate_capability(capability, contract, boot_id=boot_id, now_us=now)
    changed = deepcopy(capability)
    changed["control_binding"]["caller_generation"]["parent_pid"] += 1
    with pytest.raises(c.ContractError):
        c.validate_capability(changed, contract, boot_id=BOOT, now_us=120_000_000)


def test_unknown_fields_at_every_nested_terminal_object_are_denied(contract, completed):
    original = completed[3]["data"]["terminal_receipt"]

    def object_paths(value, path=()):
        if isinstance(value, dict):
            yield path
            for key, child in value.items():
                yield from object_paths(child, (*path, key))

    for path in object_paths(original):
        terminal = deepcopy(original)
        nested = terminal
        for key in path:
            nested = nested[key]
        nested["unreviewed"] = "deny"
        terminal["receipt_sha256"] = c.digest(
            {key: value for key, value in terminal.items() if key != "receipt_sha256"}
        )
        with pytest.raises(c.ContractError):
            c.validate_terminal(c.canonical(terminal), contract)
