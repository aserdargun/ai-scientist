"""Synthetic metadata checks only; no real authority, process or DB closure."""

import hashlib
import json
from copy import deepcopy
from dataclasses import replace

import pytest

from lab.llm import aos_no_admission_observation as old
from lab.llm import aos_no_admission_recovery as recovery
from tests.test_aos_no_admission_observation import (
    BOOT,
    expectations,
    generation,
    sign,
    valid_observation,
)

NOW = 100_000_000


def fixture():
    historical = valid_observation()
    raw = old.canonical(historical)
    principal_generation = generation(500)
    principal = {
        "generation": principal_generation,
        "generation_sha256": old.digest(principal_generation),
        "source_sha256": "6" * 64,
        "config_sha256": "7" * 64,
    }
    value = {
        "schema": recovery.NAME,
        "version": 1,
        "purpose": recovery.PURPOSE,
        "recovery_request_id": "8" * 32,
        "target": deepcopy(historical["target"]),
        "retained": {
            "observation_raw_sha256": hashlib.sha256(raw).hexdigest(),
            "observation_sha256": historical["observation_sha256"],
            "observation_schema_sha256": old.schema_hash(),
            "canonical_store": deepcopy(historical["canonical"]["store"]),
            "canonical_schema_sha256": "9" * 64,
            "cleanup_scope_sha256": old.digest(historical["cleanup_scope"]),
            "observer_generation_sha256": historical["observer"]["generation_sha256"],
            "producer_source_sha256": historical["observer"]["source_sha256"],
            "producer_config_sha256": historical["observer"]["config_sha256"],
        },
        "principal": principal,
        "cleanup_scope": deepcopy(historical["cleanup_scope"]),
        "freshness": {
            "clock": "CLOCK_BOOTTIME",
            "unit": "microseconds",
            "boot_id": BOOT,
            "issued_boottime_us": NOW - 1,
            "expires_boottime_us": NOW + 59_999_999,
            "max_age_us": 60_000_000,
        },
        "inference_allowed": False,
        "gpu_release_allowed": False,
        "observation_reissue_allowed": False,
    }
    expected = recovery.RecoveryExpectations(
        schema_sha256=recovery.schema_hash(),
        authority_sha256=old.digest(value),
        authority_raw_sha256=hashlib.sha256(old.canonical(value)).hexdigest(),
        observation_raw_sha256=hashlib.sha256(raw).hexdigest(),
        observation=expectations(historical),
        canonical_schema_sha256=value["retained"]["canonical_schema_sha256"],
        principal=old.canonical(principal),
    )
    return value, raw, expected


def check(value, raw, expected, *, repin=False, now=NOW, boot=BOOT):
    """Re-pinning exercises semantic rejection despite matching outer hashes."""
    wire = old.canonical(value)
    if repin:
        expected = replace(
            expected,
            authority_sha256=old.digest(value),
            authority_raw_sha256=hashlib.sha256(wire).hexdigest(),
        )
    return recovery.validate_recovery(
        wire,
        retained_observation=raw,
        expected=expected,
        now_boottime_us=now,
        current_boot_id=boot,
    )


def test_expired_retained_proof_validates_as_metadata_without_fake_clock(monkeypatch):
    value, raw, expected = fixture()
    before = bytes(raw)
    historical = old.decode(raw)
    with pytest.raises(old.ObservationError, match="freshness"):
        old.validate_observation(
            raw, expected=expected.observation, now_boottime_us=NOW, current_boot_id=BOOT
        )

    def forbidden(*_args, **_kwargs):
        pytest.fail("normal freshness validator must never receive a synthetic clock")

    monkeypatch.setattr(old, "validate_observation", forbidden)
    assert check(value, raw, expected) == value
    assert raw == before
    assert historical["freshness"]["expires_boottime_us"] < NOW
    assert historical["admission_budget"] is None


@pytest.mark.parametrize(
    "field", ["inference_allowed", "gpu_release_allowed", "observation_reissue_allowed"]
)
@pytest.mark.parametrize("bad", [True, 0, None])
def test_capability_escalation_rejected(field, bad):
    value, raw, expected = fixture()
    value[field] = bad
    with pytest.raises(old.ObservationError):
        check(value, raw, expected, repin=True)


@pytest.mark.parametrize(
    "path",
    [
        (),
        ("target",),
        ("retained",),
        ("principal",),
        ("principal", "generation"),
        ("cleanup_scope",),
        ("freshness",),
    ],
)
def test_every_context_object_is_closed(path):
    value, raw, expected = fixture()
    target = value
    for key in path:
        target = target[key]
    target["allow_expired"] = True
    with pytest.raises(old.ObservationError, match="closure"):
        check(value, raw, expected, repin=True)


@pytest.mark.parametrize(
    "field",
    [
        "schema_sha256",
        "authority_sha256",
        "authority_raw_sha256",
        "observation_raw_sha256",
        "canonical_schema_sha256",
    ],
)
def test_independent_pin_mismatch(field):
    value, raw, expected = fixture()
    with pytest.raises(old.ObservationError):
        check(value, raw, replace(expected, **{field: "0" * 64}))


@pytest.mark.parametrize(
    "field", ["original", "observer", "canonical", "physical", "cleanup_scope"]
)
def test_independent_historical_sections_required(field):
    value, raw, expected = fixture()
    old_expected = replace(expected.observation, **{field: b"{}"})
    with pytest.raises(old.ObservationError, match="preimage mismatch"):
        check(value, raw, replace(expected, observation=old_expected))


@pytest.mark.parametrize(
    "field",
    [
        "observation_raw_sha256",
        "observation_sha256",
        "observation_schema_sha256",
        "canonical_schema_sha256",
        "cleanup_scope_sha256",
        "observer_generation_sha256",
        "producer_source_sha256",
        "producer_config_sha256",
    ],
)
def test_retained_hash_rebinding_rejected(field):
    value, raw, expected = fixture()
    value["retained"][field] = "0" * 64
    with pytest.raises(old.ObservationError, match="retained recovery pins"):
        check(value, raw, expected, repin=True)


@pytest.mark.parametrize(
    "field,bad",
    [
        ("lease_id", "other-lease"),
        ("current_generation", 2),
        ("session_id", "other-session"),
        ("runtime_id", "other-runtime"),
        ("authorization_context_sha256", "0" * 64),
    ],
)
def test_scope_cannot_adopt_a_new_generation_or_authority(field, bad):
    value, raw, expected = fixture()
    value["cleanup_scope"][field] = bad
    with pytest.raises(old.ObservationError, match="cleanup scope"):
        check(value, raw, expected, repin=True)


@pytest.mark.parametrize("now", [True, -1, 2**53, float("nan"), NOW - 2, NOW + 59_999_999])
def test_bad_current_time_and_expiry_boundary(now):
    value, raw, expected = fixture()
    with pytest.raises(old.ObservationError):
        check(value, raw, expected, now=now)


@pytest.mark.parametrize(
    "issued,expires",
    [
        (NOW + 1, NOW + 2),
        (NOW, NOW),
        (NOW, NOW - 1),
        (NOW, NOW + 60_000_001),
        (19_999_999, NOW + 1),
    ],
)
def test_new_freshness_is_finite_current_and_after_historical_observation(issued, expires):
    value, raw, expected = fixture()
    value["freshness"].update(issued_boottime_us=issued, expires_boottime_us=expires)
    with pytest.raises(old.ObservationError, match="freshness"):
        check(value, raw, expected, repin=True)


def test_same_boot_required():
    value, raw, expected = fixture()
    with pytest.raises(old.ObservationError, match="boot"):
        check(value, raw, expected, boot="00000000-0000-0000-0000-000000000002")


@pytest.mark.parametrize(
    "generation_path", [("observer", "generation"), ("original", "broker_generation")]
)
def test_original_generations_cannot_be_recovery_principal(generation_path):
    value, raw, expected = fixture()
    historical = old.decode(raw)
    generation_value = historical
    for key in generation_path:
        generation_value = generation_value[key]
    value["principal"]["generation"] = generation_value
    value["principal"]["generation_sha256"] = old.digest(generation_value)
    expected = replace(expected, principal=old.canonical(value["principal"]))
    with pytest.raises(old.ObservationError, match="distinct recovery principal"):
        check(value, raw, expected, repin=True)


def test_principal_must_match_independent_preimage():
    value, raw, expected = fixture()
    value["principal"]["source_sha256"] = "0" * 64
    with pytest.raises(old.ObservationError, match="principal"):
        check(value, raw, expected, repin=True)


def test_original_caller_cannot_hide_same_process_by_dropping_parent_fields():
    value, raw, expected = fixture()
    caller = old.decode(raw)["original"]["admission_record"]["admission_binding"][
        "caller_generation"
    ]
    principal = value["principal"]
    principal["generation"] = {
        key: item for key, item in caller.items() if key not in {"parent_pid", "parent_start_ticks"}
    }
    principal["generation_sha256"] = old.digest(principal["generation"])
    assert principal["generation_sha256"] != old.digest(caller)
    expected = replace(expected, principal=old.canonical(principal))
    with pytest.raises(old.ObservationError, match="historical process identity"):
        check(value, raw, expected, repin=True)


def test_target_and_canonical_store_cannot_be_rebound():
    for section, field, bad in (
        ("target", "request_id", "b" * 32),
        ("store", "inode", 999),
    ):
        value, raw, expected = fixture()
        target = value["target"] if section == "target" else value["retained"]["canonical_store"]
        target[field] = bad
        with pytest.raises(old.ObservationError):
            check(value, raw, expected, repin=True)


def test_historical_raw_bytes_cannot_be_reformatted():
    value, raw, expected = fixture()
    with pytest.raises(old.ObservationError, match="noncanonical"):
        check(value, json.dumps(old.decode(raw), indent=2).encode(), expected)


@pytest.mark.parametrize("bad_raw", [b"", b"{}\n", b'{"x":1,"x":2}', b'{"x":NaN}'])
def test_malformed_or_noncanonical_context(bad_raw):
    _, raw, expected = fixture()
    with pytest.raises(old.ObservationError):
        recovery.validate_recovery(
            bad_raw,
            retained_observation=raw,
            expected=expected,
            now_boottime_us=NOW,
            current_boot_id=BOOT,
        )


@pytest.mark.parametrize(
    "mutation", ["ttl", "observation_hash", "physical_hash", "original_hash", "observer_hash"]
)
def test_historical_internal_invariants_reject_even_rebound_expected_sections(mutation):
    value, raw, expected = fixture()
    historical = old.decode(raw)
    if mutation == "ttl":
        historical["freshness"]["expires_boottime_us"] += 1
        historical["canonical"]["tombstone"]["freshness"] = deepcopy(historical["freshness"])
        historical["canonical"]["tombstone_sha256"] = old.digest(
            historical["canonical"]["tombstone"]
        )
    elif mutation == "physical_hash":
        historical["physical"]["proof_sha256"] = "0" * 64
    elif mutation == "original_hash":
        historical["original"]["admission_capture_sha256"] = "0" * 64
    elif mutation == "observer_hash":
        historical["observer"]["capability_sha256"] = "0" * 64
    raw = sign(historical)
    if mutation == "observation_hash":
        historical["observation_sha256"] = "0" * 64
        raw = old.canonical(historical)
    expected = replace(
        expected,
        observation=expectations(historical),
        observation_raw_sha256=hashlib.sha256(raw).hexdigest(),
    )
    with pytest.raises(old.ObservationError):
        check(value, raw, expected, repin=True)


def test_schema_reuses_exact_original_definitions_and_pin():
    original = old.schema_document()
    document = recovery.schema_document()
    assert old.schema_hash() == recovery.OBSERVATION_SCHEMA_SHA256
    for name in ("ScientistServerGeneration", "hash", "id", "store", "target", "cleanup_scope"):
        assert document["$defs"][name] == original["$defs"][name]
    assert recovery.schema_hash() == old.digest(json.loads(recovery.SCHEMA_PATH.read_bytes()))


def test_original_request_id_cannot_be_recovery_request_id():
    value, raw, expected = fixture()
    value["recovery_request_id"] = value["target"]["request_id"]
    with pytest.raises(old.ObservationError, match="target"):
        check(value, raw, expected, repin=True)
