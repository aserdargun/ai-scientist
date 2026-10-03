"""CPU-only synthetic admission identity and transaction regressions."""

import json
from contextlib import closing
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_aos_control_store import (
    BUDGET,
    CONFIG,
    DEPLOYMENT,
    PEER,
    PROFILE,
    REQUEST,
    REQUEST_HASH,
    SCHEMA,
    Rig,
)

from lab.llm.aos_gpu_broker import PeerGeneration
from lab.llm.aos_gpu_control import ControlError, LabAOSControl
from lab.llm.aos_gpu_control_store import AdmissionGrant, ControlStoreError, canonical, digest


@pytest.fixture
def admission_rig(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    original = json.loads(rig.admission.binding_json)
    peer = PeerGeneration(**PEER)
    profile = SimpleNamespace(profile_id=PROFILE, deployment_digest=DEPLOYMENT)
    policy = SimpleNamespace(
        config={
            "enabled": True,
            "history_reconcile": False,
            "source_files": {"scientist": {"fixture": "2" * 64}, "aos": {"fixture": "3" * 64}},
        },
        sha256=original["policy_sha256"],
        bound_profile=lambda *_args: profile,
        verify_policy_hash=lambda: None,
        _pin=lambda _profile: dict(original["profile_pin"]),
    )
    control = LabAOSControl(
        policy=policy,
        authenticator=SimpleNamespace(still_current=lambda p: p == peer),
        store=rig.store,
        server_generation=dict(original["server_generation"]),
        clock=lambda: rig.now,
    )
    control._mint(peer, profile)
    rig.admission = control.admit_infer(peer, PROFILE, DEPLOYMENT)
    return rig, control, peer, profile


def test_capability_refresh_preserves_stable_binding_and_original_deadline(admission_rig):
    rig, control, peer, profile = admission_rig
    old = control._mint(peer, profile)["capability"]
    assert rig.register(130.0) == 130.0
    rig.now += 1
    new = control._mint(peer, profile)["capability"]
    assert digest(old) != digest(new)
    assert old["admission_binding"] == new["admission_binding"]
    assert old["admission_binding_sha256"] == new["admission_binding_sha256"]
    rig.admission = control.admit_infer(peer, PROFILE, DEPLOYMENT)
    assert rig.register(300.0) == 130.0


@pytest.mark.parametrize(
    "changed", ["policy", "source", "manifest", "server", "caller", "schema", "output_bundle"]
)
def test_durable_intent_rejects_changed_stable_authority(admission_rig, changed):
    rig, _control, _peer, _profile = admission_rig
    rig.register()
    binding = json.loads(rig.admission.binding_json)
    if changed == "policy":
        binding["policy_sha256"] = "9" * 64
    elif changed == "source":
        binding["source_fingerprints"]["scientist"] = "9" * 64
    elif changed == "manifest":
        binding["profile_pin"]["manifest_sha256"] = "9" * 64
    elif changed == "server":
        binding["server_generation"]["invocation_id"] = "9" * 32
    elif changed == "caller":
        binding["caller_generation"]["parent_start_ticks"] += 1
    elif changed == "output_bundle":
        binding["profile_pin"]["output_contract"]["bundle_sha256"] = "9" * 64
    else:
        binding["control_schema"]["sha256"] = "9" * 64
    rig.admission = AdmissionGrant(canonical(binding), lambda: None)
    with pytest.raises(ControlStoreError, match="request_conflict"):
        rig.register()


@pytest.mark.parametrize("changed", ["policy", "server", "expired"])
def test_grant_revalidates_at_transaction_boundary(admission_rig, changed):
    rig, control, _peer, _profile = admission_rig
    if changed == "policy":
        control.policy.sha256 = "9" * 64
    elif changed == "server":
        control.server_generation["start_ticks"] += 1
    else:
        rig.now += 61
    with pytest.raises(ControlError, match="capability_mismatch"):
        rig.register()
    with closing(rig.store._connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM aos_control_requests").fetchone()[0] == 0


def test_final_authority_check_rolls_back_new_intent(admission_rig):
    rig, _control, _peer, _profile = admission_rig
    checks = []

    def verify():
        checks.append(True)
        if len(checks) == 2:
            raise ControlError("stale_generation")

    rig.admission = AdmissionGrant(rig.admission.binding_json, verify)
    with pytest.raises(ControlError, match="stale_generation"):
        rig.register()
    with closing(rig.store._connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM aos_control_requests").fetchone()[0] == 0


def test_capability_expiring_during_final_peer_check_rolls_back_intent(admission_rig):
    rig, control, peer, _profile = admission_rig
    checks = []

    def delayed_final_peer_check(actual):
        checks.append(actual)
        # register_intent verifies twice; each grant check observes the peer
        # before and after policy work. The last observation consumes freshness.
        if len(checks) == 4:
            rig.now = 161.0  # capability was issued at 100 and expires at 160
        return actual == peer

    control.authenticator.still_current = delayed_final_peer_check
    with pytest.raises(ControlError, match="capability_mismatch"):
        rig.register()
    assert len(checks) == 4
    with closing(rig.store._connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM aos_control_requests").fetchone()[0] == 0


def test_missing_grant_and_legacy_null_binding_cannot_be_adopted(admission_rig):
    rig, _control, _peer, _profile = admission_rig
    with pytest.raises(ControlStoreError, match="unauthorized"):
        rig.store.register_intent(
            PEER, REQUEST, REQUEST_HASH, PROFILE, DEPLOYMENT, CONFIG, SCHEMA, 200.0, BUDGET
        )
    rig.register()
    with closing(rig.store._connect()) as connection:
        connection.execute(
            "UPDATE aos_control_requests SET admission_binding_json=NULL, "
            "admission_binding_sha256=NULL"
        )
        connection.commit()
    with pytest.raises(ControlStoreError, match="request_conflict"):
        rig.register()
    with pytest.raises(ControlStoreError, match="request_conflict"):
        rig.submit()


def test_terminal_keeps_original_pins_and_tombstone_has_no_admission(admission_rig):
    rig, control, _peer, _profile = admission_rig
    binding = json.loads(rig.admission.binding_json)
    rig.register()
    control.server_generation["invocation_id"] = "9" * 32
    control.policy.sha256 = "9" * 64
    receipt = rig.cancel()["terminal_receipt"]
    assert receipt["admission_binding"] == binding
    assert receipt["admission_binding_sha256"] == digest(binding)
    tombstone = rig.store.cancel(
        PEER,
        {
            "request_id": "8" * 32,
            "request_sha256": REQUEST_HASH,
            "original_peer_generation_sha256": digest(PEER),
        },
        PROFILE,
        DEPLOYMENT,
        profile_config_sha256=CONFIG,
        response_schema_sha256=SCHEMA,
        budget=BUDGET,
    )["terminal_receipt"]
    assert tombstone["admission_binding"] is None
    assert tombstone["admission_binding_sha256"] is None


def test_draft_binding_fixture_matches_runtime_closed_shape():
    from pathlib import Path

    from lab.llm.aos_gpu_control_store import validate_admission_binding

    draft = Path(__file__).resolve().parents[1] / "docs/ai-scientist/contracts/control-v1-draft"
    value = json.loads((draft / "admission_binding-positive.jsonl").read_text())
    assert validate_admission_binding(value) == value
    invalid = deepcopy(value)
    invalid["issued_boottime"] = 100.0
    with pytest.raises(ControlStoreError, match="invalid_frame"):
        validate_admission_binding(invalid)


def test_legacy_profile_pin_is_cleanup_evidence_but_cannot_admit_infer(admission_rig):
    rig, control, peer, profile = admission_rig
    binding = json.loads(rig.admission.binding_json)
    del binding["profile_pin"]["output_contract"]
    control.policy._pin = lambda _profile: dict(binding["profile_pin"])
    control._mint(peer, profile)
    with pytest.raises(ControlStoreError, match="unauthorized"):
        control.admit_infer(peer, PROFILE, DEPLOYMENT)
    rig.admission = AdmissionGrant(canonical(binding), lambda: None)
    with pytest.raises(ControlStoreError, match="unauthorized"):
        rig.register()


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        {"name": "aos-scientist-profile-output.v2", "version": True, "bundle_sha256": "9" * 64},
        {
            "name": "aos-scientist-profile-output.v2",
            "version": 2,
            "bundle_sha256": "9" * 64,
            "allow_unknown": True,
        },
    ],
)
def test_output_contract_pin_is_closed_and_integer_versioned(admission_rig, invalid):
    rig, _control, _peer, _profile = admission_rig
    binding = json.loads(rig.admission.binding_json)
    binding["profile_pin"]["output_contract"] = invalid
    rig.admission = AdmissionGrant(canonical(binding), lambda: None)
    with pytest.raises(ControlStoreError, match="invalid_frame"):
        rig.register()
