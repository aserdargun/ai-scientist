"""CPU synthetic metadata only; no admission, physical observation or closure."""

import json
from copy import deepcopy

import pytest

from lab.llm import aos_no_admission_observation as contract

BOOT = "00000000-0000-0000-0000-000000000001"


def generation(pid=300, *, caller=False):
    value = {
        "uid": 1000,
        "pid": pid,
        "start_ticks": pid * 10,
        "boot_id": BOOT,
        "unit": f"fixture-{pid}.service",
        "invocation_id": f"{pid:032x}",
        "control_group": f"/fixture/fixture-{pid}.service",
    }
    if caller:
        value.update(parent_pid=2, parent_start_ticks=10)
    return value


def expectations(value):
    """Synthetic separately retained fixture sections, never live authority."""
    return contract.ObservationExpectations(
        schema_sha256=contract.schema_hash(),
        **{
            field: contract.canonical(value[field])
            for field in ("original", "observer", "canonical", "physical", "cleanup_scope")
        },
    )


def sign(value):
    value["observation_sha256"] = contract.digest(
        {key: item for key, item in value.items() if key != "observation_sha256"}
    )
    return contract.canonical(value)


def valid_observation():
    """Editable synthetic fixture available for other proposed-contract tests."""
    sha = contract.digest
    caller, broker, observer_generation = (
        generation(200, caller=True),
        generation(300),
        generation(400),
    )
    pin = {
        key: "1" * 64
        for key in (
            "deployment_digest",
            "manifest_sha256",
            "config_sha256",
            "response_schema_sha256",
        )
    }
    pin["output_contract"] = {
        "name": "aos-scientist-profile-output.v2",
        "version": 2,
        "bundle_sha256": "2" * 64,
    }
    binding = {
        "caller_generation": caller,
        "server_generation": broker,
        "policy_sha256": "3" * 64,
        "source_fingerprints": {"scientist": "4" * 64, "aos": "5" * 64},
        "profile_id": "aos.decider.turn.v1",
        "profile_pin": pin,
    }
    for field, name in (
        ("infer_schema", "runtime"),
        ("control_schema", "control"),
        ("terminal_schema", "terminal"),
    ):
        binding[field] = {"name": f"aos-scientist-{name}.v1", "version": 1, "sha256": "6" * 64}
    request = {
        "version": 1,
        "op": "infer",
        "request_id": "a" * 32,
        "profile_id": binding["profile_id"],
        "deployment_digest": pin["deployment_digest"],
        "payload": {"messages": [{"role": "user", "content": "Türkçe fixture; kanıt değil"}]},
    }
    intent = {
        "session_id": "fixture-session",
        "runtime_id": "fixture-runtime",
        "lease_id": "fixture-lease",
        "generation": 0,
        "owner": "AGENT",
        "authorization_context_sha256": "7" * 64,
    }
    capture = {
        "admission_binding": binding,
        "capability_sha256": "8" * 64,
        "capability_freshness": {
            "boot_id": BOOT,
            "issued_boottime": 1.25,
            "expires_boottime": 61.25,
        },
    }
    record = {
        **capture,
        "schema_version": "2.0",
        "request_id": request["request_id"],
        "request_sha256": sha(request),
        "session_id": intent["session_id"],
        "intent_binding_sha256": sha(intent),
        "admission_binding_sha256": sha(binding),
        "captured_boottime": 2.75,
    }
    original_store = {"path_sha256": "9" * 64, "device": 1, "inode": 10, "uid": 1000}
    canonical_store = {"path_sha256": "b" * 64, "device": 1, "inode": 20, "uid": 1000}
    original = {
        "request_canonical": contract.canonical(request).decode(),
        "admission_record": record,
        "admission_record_sha256": sha(record),
        "admission_binding_sha256": sha(binding),
        "admission_capture_sha256": sha(capture),
        "intent_binding": intent,
        "intent_binding_sha256": sha(intent),
        "broker_generation": broker,
        "broker_generation_sha256": sha(broker),
        "store": original_store,
    }
    capability = {
        "feature": contract.FEATURE,
        "version": 1,
        "schema_sha256": contract.schema_hash(),
        "generation_sha256": sha(observer_generation),
        "source_sha256": "c" * 64,
        "config_sha256": "d" * 64,
        "purpose": contract.PURPOSE,
    }
    observer = {
        "generation": observer_generation,
        "generation_sha256": sha(observer_generation),
        "source_sha256": capability["source_sha256"],
        "config_sha256": capability["config_sha256"],
        "capability": capability,
        "capability_sha256": sha(capability),
        "purpose": contract.PURPOSE,
    }
    target = {
        "request_id": request["request_id"],
        "request_sha256": sha(request),
        "original_caller_generation_sha256": sha(caller),
    }
    freshness = {
        "clock": "CLOCK_BOOTTIME",
        "unit": "microseconds",
        "boot_id": BOOT,
        "observed_boottime_us": 20_000_000,
        "expires_boottime_us": 80_000_000,
        "max_age_us": 60_000_000,
    }
    physical = {
        "target": target,
        "caller_generation": caller,
        "caller_generation_sha256": sha(caller),
        "broker_generation": broker,
        "broker_generation_sha256": sha(broker),
        "children": [],
        **{
            key: True
            for key in (
                "caller_absent",
                "broker_absent",
                "original_cgroups_absent",
                "children_absent",
                "provenance_complete",
                "late_dispatch_fenced",
            )
        },
    }
    physical["proof_sha256"] = sha(physical)
    tombstone = {
        "id": "e" * 32,
        "target": target,
        "store": canonical_store,
        "admission_record_sha256": sha(record),
        "intent_binding_sha256": sha(intent),
        "observer_generation_sha256": sha(observer_generation),
        "physical_proof_sha256": physical["proof_sha256"],
        "freshness": freshness,
        "late_admission_fenced": True,
    }
    canonical = {
        "target": target,
        "store": canonical_store,
        "tombstone": tombstone,
        "tombstone_sha256": sha(tombstone),
        **{
            key: True
            for key in (
                "admission_absent",
                "gpu_queue_absent",
                "allocation_absent",
                "child_binding_absent",
                "deferred_execution_absent",
                "quarantined_allocation_absent",
            )
        },
    }
    cleanup = {
        "store": original_store,
        "session_id": intent["session_id"],
        "runtime_id": intent["runtime_id"],
        "lease_id": "fixture-current-lease",
        "original_generation": 0,
        "current_generation": 1,
        "current_status": "stopped",
        "current_owner": "PAUSED",
        "authorization_context_sha256": "f" * 64,
        "purpose": contract.PURPOSE,
    }
    value = {
        "schema": contract.NAME,
        "version": 1,
        "target": target,
        "original": original,
        "observer": observer,
        "canonical": canonical,
        "physical": physical,
        "cleanup_scope": cleanup,
        "outcome": "never_received",
        "admission_budget": None,
        "freshness": freshness,
    }
    # Avoid Python aliasing between repeated wire sections when constructing adversarial cases.
    value = deepcopy(json.loads(contract.canonical(value)))
    sign(value)
    return value


def verify(value, *, expected=None, now=30_000_000, boot=BOOT):
    return contract.validate_observation(
        sign(value),
        expected=expected or expectations(value),
        now_boottime_us=now,
        current_boot_id=boot,
    )


def test_valid_fixture_is_only_metadata_preserves_original_and_unicode():
    value = valid_observation()
    before = contract.canonical(value)
    assert verify(value) == value
    assert contract.canonical(value) == before
    assert b"\\u00fc" not in before
    assert value["admission_budget"] is None
    assert value["original"]["admission_record"]["captured_boottime"] == 2.75
    assert (
        value["original"]["admission_record"]["admission_binding"]["control_schema"]["version"] == 1
    )


def test_full_schema_recursively_closed_and_legacy_definition_preserved():
    document = contract.schema_document()

    def walk(value):
        if type(value) is dict:
            if value.get("type") == "object":
                assert value["additionalProperties"] is False
                assert set(value["required"]) == set(value["properties"])
            for item in value.values():
                walk(item)
        elif type(value) is list:
            for item in value:
                walk(item)

    walk(document)
    assert len(contract.schema_hash()) == len(contract.source_hash()) == 64
    # All legacy definitions are fingerprinted independently; no runtime AOS import.
    assert (
        document["$defs"]["ScientistAdmissionRecordV2"]["properties"]["schema_version"]["const"]
        == "2.0"
    )


@pytest.mark.parametrize(
    "path,replacement",
    [
        (("version",), True),
        (("version",), 1.0),
        (("version",), 2),
        (("schema",), "aos-scientist-terminal.v1"),
        (("outcome",), "released"),
        (("admission_budget",), {"admitted_boottime": 1}),
        (("target", "original_caller_generation_sha256"), "0" * 64),
        (("original", "admission_record_sha256"), "0" * 64),
        (("original", "admission_binding_sha256"), "0" * 64),
        (("original", "admission_capture_sha256"), "0" * 64),
        (("original", "intent_binding_sha256"), "0" * 64),
        (("original", "admission_record", "captured_boottime"), True),
        (("original", "admission_record", "captured_boottime"), -1),
        (("original", "admission_record", "capability_freshness", "issued_boottime"), -1),
        (("original", "admission_record", "admission_binding", "control_schema", "version"), 2),
        (("observer", "generation", "pid"), True),
        (("observer", "capability", "schema_sha256"), "0" * 64),
        (("observer", "capability", "source_sha256"), "0" * 64),
        (("observer", "capability", "config_sha256"), "0" * 64),
        (("observer", "capability", "feature"), "no-admission-observation.v2"),
        (("observer", "purpose"), "infer"),
        (("canonical", "store", "inode"), True),
        (("canonical", "gpu_queue_absent"), False),
        (("canonical", "allocation_absent"), False),
        (("canonical", "tombstone", "id"), "f" * 32),
        (("canonical", "tombstone_sha256"), "0" * 64),
        (("canonical", "tombstone", "late_admission_fenced"), False),
        (("physical", "late_dispatch_fenced"), False),
        (("physical", "provenance_complete"), False),
        (("physical", "proof_sha256"), "0" * 64),
        (("cleanup_scope", "current_generation"), 0),
        (("cleanup_scope", "original_generation"), 1),
        (("cleanup_scope", "runtime_id"), "other-runtime"),
        (("cleanup_scope", "current_status"), "running"),
        (("cleanup_scope", "current_owner"), "AGENT"),
        (("freshness", "observed_boottime_us"), 20_000_000.0),
        (("freshness", "expires_boottime_us"), True),
        (("freshness", "boot_id"), "00000000-0000-0000-0000-000000000002"),
    ],
)
def test_tampered_shape_or_cross_binding_rejected_even_with_resigned_root(path, replacement):
    value = valid_observation()
    current = value
    for key in path[:-1]:
        current = current[key]
    current[path[-1]] = replacement
    with pytest.raises(contract.ObservationError):
        verify(value)


@pytest.mark.parametrize(
    "section", ["original", "observer", "canonical", "physical", "cleanup_scope"]
)
def test_independent_retained_preimages_reject_rehashed_substitution(section):
    value = valid_observation()
    retained = expectations(value)
    changed = deepcopy(value)
    if section == "original":
        changed[section]["admission_record"]["capability_sha256"] = "0" * 64
    elif section == "observer":
        changed[section]["source_sha256"] = "0" * 64
    elif section == "canonical":
        changed[section]["store"]["inode"] += 1
    elif section == "physical":
        changed[section]["broker_generation"]["start_ticks"] += 1
    else:
        changed[section]["authorization_context_sha256"] = "0" * 64
    with pytest.raises(contract.ObservationError, match="independent .* preimage mismatch"):
        verify(changed, expected=retained)


@pytest.mark.parametrize(
    "section", [None, "original", "observer", "canonical", "physical", "cleanup_scope"]
)
def test_extra_fields_rejected_at_every_section(section):
    value = valid_observation()
    (value if section is None else value[section])["GPUrelease"] = True
    with pytest.raises(contract.ObservationError, match="object closure"):
        verify(value)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"version":1,"version":1}',
        b'{"number":NaN}',
        b'{"number":Infinity}',
        b'{"number":1e999}',
        b"\xff",
        b"[]",
    ],
)
def test_duplicate_nonfinite_utf8_nonobject_rejected(raw):
    with pytest.raises(contract.ObservationError):
        contract.decode(raw)


@pytest.mark.parametrize("now", [19_999_999, 80_000_000, True, 30_000_000.0, -1, 2**53])
def test_stale_future_and_invalid_clock_rejected(now):
    with pytest.raises(contract.ObservationError):
        verify(valid_observation(), now=now)


def test_long_freshness_interval_rejected_without_rebasing_original_capture():
    value = valid_observation()
    value["freshness"]["expires_boottime_us"] += 1
    value["canonical"]["tombstone"]["freshness"] = deepcopy(value["freshness"])
    value["canonical"]["tombstone_sha256"] = contract.digest(value["canonical"]["tombstone"])
    with pytest.raises(contract.ObservationError, match="freshness"):
        verify(value)


def test_altered_canonical_request_and_duplicate_embedded_request_rejected():
    for encoded in ('{"version":1,"version":1}', "{}", '{"payload":NaN}'):
        value = valid_observation()
        value["original"]["request_canonical"] = encoded
        with pytest.raises(contract.ObservationError):
            verify(value)


def test_current_generation_cannot_reuse_original_broker_even_with_new_hashes():
    value = valid_observation()
    observer = value["observer"]
    observer["generation"] = deepcopy(value["original"]["broker_generation"])
    observer["generation_sha256"] = contract.digest(observer["generation"])
    observer["capability"]["generation_sha256"] = observer["generation_sha256"]
    observer["capability_sha256"] = contract.digest(observer["capability"])
    with pytest.raises(contract.ObservationError, match="current observer"):
        verify(value)


def test_noncanonical_wire_wrong_full_schema_and_observation_hash_rejected():
    value = valid_observation()
    args = {"expected": expectations(value), "now_boottime_us": 30_000_000, "current_boot_id": BOOT}
    with pytest.raises(contract.ObservationError, match="noncanonical"):
        contract.validate_observation(sign(value) + b"\n", **args)
    value["observation_sha256"] = "0" * 64
    with pytest.raises(contract.ObservationError, match="observation hash"):
        contract.validate_observation(contract.canonical(value), **args)
    from dataclasses import replace

    args["expected"] = replace(args["expected"], schema_sha256="0" * 64)
    with pytest.raises(contract.ObservationError, match="schema pin"):
        contract.validate_observation(sign(value), **args)


def test_original_record_nonfinite_seconds_and_escaped_group_rejected():
    value = valid_observation()
    value["original"]["admission_record"]["captured_boottime"] = float("nan")
    with pytest.raises(contract.ObservationError):
        sign(value)
    value = valid_observation()
    value["original"]["broker_generation"]["control_group"] = "/fixture/../escaped"
    with pytest.raises(contract.ObservationError):
        verify(value)


def test_expired_original_capture_does_not_restrict_fresh_observer_cleanup_window():
    value = valid_observation()
    original = contract.canonical(value["original"])
    value["freshness"]["observed_boottime_us"] = 100_000_000
    value["freshness"]["expires_boottime_us"] = 160_000_000
    tombstone = value["canonical"]["tombstone"]
    tombstone["freshness"] = deepcopy(value["freshness"])
    value["canonical"]["tombstone_sha256"] = contract.digest(tombstone)
    assert verify(value, now=110_000_000)["admission_budget"] is None
    assert contract.canonical(value["original"]) == original


def test_later_cleanup_generations_supported_without_rewriting_original():
    value = valid_observation()
    original = value["original"]
    original["intent_binding"]["generation"] = 3
    checksum = contract.digest(original["intent_binding"])
    original["intent_binding_sha256"] = checksum
    original["admission_record"]["intent_binding_sha256"] = checksum
    original["admission_record_sha256"] = contract.digest(original["admission_record"])
    value["cleanup_scope"]["original_generation"] = 3
    value["cleanup_scope"]["current_generation"] = 4
    tombstone = value["canonical"]["tombstone"]
    tombstone["intent_binding_sha256"] = checksum
    tombstone["admission_record_sha256"] = original["admission_record_sha256"]
    value["canonical"]["tombstone_sha256"] = contract.digest(tombstone)
    assert verify(value)["original"]["intent_binding"]["generation"] == 3
    value["cleanup_scope"]["current_generation"] = 3
    with pytest.raises(contract.ObservationError, match="cleanup original scope"):
        verify(value)


def test_current_cleanup_lease_requires_independently_retained_exact_value():
    value = valid_observation()
    retained = expectations(value)
    assert value["cleanup_scope"]["lease_id"] != value["original"]["intent_binding"]["lease_id"]
    value["cleanup_scope"]["lease_id"] = value["original"]["intent_binding"]["lease_id"]
    with pytest.raises(contract.ObservationError, match="independent cleanup_scope"):
        verify(value, expected=retained)


def test_children_require_retained_original_boot_hash_and_complete_absence():
    value = valid_observation()
    child = {
        "generation": generation(500),
        "generation_sha256": contract.digest(generation(500)),
        "process_absent": True,
        "cgroup_absent": True,
    }
    value["physical"]["children"] = [child]

    def rehash():
        value["physical"]["proof_sha256"] = contract.digest(
            {key: item for key, item in value["physical"].items() if key != "proof_sha256"}
        )
        value["canonical"]["tombstone"]["physical_proof_sha256"] = value["physical"]["proof_sha256"]
        value["canonical"]["tombstone_sha256"] = contract.digest(value["canonical"]["tombstone"])

    rehash()
    assert verify(value)["physical"]["children"] == [child]
    child["generation"]["boot_id"] = "00000000-0000-0000-0000-000000000002"
    child["generation_sha256"] = contract.digest(child["generation"])
    rehash()
    with pytest.raises(contract.ObservationError, match="child original boot"):
        verify(value)
