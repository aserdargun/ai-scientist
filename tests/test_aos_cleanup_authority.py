"""Synthetic CPU evidence for explicit retired-target control authority only."""

import hashlib
import json
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace

import pytest
from aos_admission_fixture import output_pin
from test_aos_control_store import CONFIG, DEPLOYMENT, PEER, PROFILE, SCHEMA, TARGET, Rig

from lab.llm.aos_gpu_broker import PeerGeneration
from lab.llm.aos_gpu_control import (
    CONTROL_SCHEMA_HASH,
    INFER_SCHEMA_HASH,
    POLICY_SCHEMA,
    ControlError,
    ControlPolicy,
    LabAOSControl,
    canonical,
    decode_request,
    digest,
)
from lab.llm.aos_gpu_control import (
    SCHEMA as CONTROL_SCHEMA,
)
from lab.llm.aos_gpu_control_store import ControlStoreError
from lab.llm.aos_gpu_executor import TurnBudgets


@pytest.fixture
def cleanup_rig(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    peer = PeerGeneration(**PEER)
    root = tmp_path / "sources"
    root.mkdir()
    policy_path = tmp_path / "policy.json"
    paths = {
        "scientist": [
            "lab/llm/" + name + ".py"
            for name in (
                "aos_gpu_broker",
                "aos_gpu_service",
                "aos_gpu_executor",
                "gpu_scheduler",
                "aos_gpu_control",
                "aos_gpu_control_store",
            )
        ],
        "aos": [
            "services/decider/broker_worker.py",
            "services/bonsai/broker_worker.py",
            "src/aos/scientist_transport.py",
            "src/aos/scientist_protocol.py",
            "src/aos/scientist_intents.py",
        ],
    }
    server = json.loads(rig.admission.binding_json)["server_generation"]
    sequence = []

    def make_control(*, entries=None, enabled=True, rotation=0, deployment=DEPLOYMENT):
        sources = {}
        for name, files in paths.items():
            sources[name] = {}
            for relative in files:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"synthetic source revision {rotation}\n")
                sources[name][relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        profiles = {
            name: SimpleNamespace(
                profile_id=name,
                deployment_digest=deployment,
                manifest_sha256="4" * 64,
                config_sha256=CONFIG,
                response_schema_sha256=SCHEMA,
                output_contract=output_pin(),
                source_root=root,
                budgets=TurnBudgets(10, 5, 15, 300),
                max_output_tokens=16,
                context_tokens=256,
            )
            for name in ("aos.decider.turn.v1", "aos.bonsai.recovery.v1", "aos.bonsai.vision.v1")
        }

        def get(profile_id, requested):
            assert requested == deployment, "retired profile must not be loaded"
            return profiles[profile_id]

        config = {
            "schema": POLICY_SCHEMA,
            "enabled": enabled,
            "caller_unit": peer.unit,
            "control_socket": "/fixture/control.sock",
            "history_reconcile": False,
            "source_files": sources,
            "profile_pins": {key: ControlPolicy._pin(value) for key, value in profiles.items()},
            "infer_schema_sha256": INFER_SCHEMA_HASH,
            "control_schema_sha256": CONTROL_SCHEMA_HASH,
        }
        if entries is not None:
            config["cleanup_targets"] = entries
        policy_path.write_bytes(canonical(config))
        policy_path.chmod(0o600)
        policy = ControlPolicy(policy_path, profiles=SimpleNamespace(get=get), source_root=root)
        control = LabAOSControl(
            policy=policy,
            authenticator=SimpleNamespace(still_current=lambda actual: actual == peer),
            store=rig.store,
            server_generation=dict(server),
            clock=lambda: rig.now,
        )
        return control

    def call(control, operation="capability", *, target=None, capability=None, caller=peer):
        sequence.append(True)
        request = {
            "schema": CONTROL_SCHEMA,
            "version": 1,
            "op": operation,
            "control_id": f"{len(sequence):032x}",
            "expected_capability_sha256": capability,
            "profile_id": PROFILE,
            "deployment_digest": DEPLOYMENT,
            "target": target,
        }
        return control.handle(caller, decode_request(canonical(request)))

    def entry(original, operations=None):
        return {
            "target": dict(TARGET),
            "profile_id": PROFILE,
            "deployment_digest": DEPLOYMENT,
            "original_admission_binding_sha256": original["original_admission_binding_sha256"],
            "original_cleanup_authorization_sha256": original[
                "original_cleanup_authorization_sha256"
            ],
            "operations": sorted(operations or ["status", "cancel", "reconcile"]),
        }

    first = make_control()
    capability = call(first)
    rig.admission = first.admit_infer(peer, PROFILE, DEPLOYMENT)
    return SimpleNamespace(
        rig=rig,
        peer=peer,
        first=first,
        capability=capability,
        make=make_control,
        call=call,
        entry=entry,
        policy_path=policy_path,
    )


def test_explicit_cleanup_survives_rotation_disabled_admission_and_retired_profile(cleanup_rig):
    f = cleanup_rig
    f.rig.register(130.0)
    f.rig.submit()
    original = f.rig.store.cleanup_original(PEER, TARGET, PROFILE, DEPLOYMENT)
    control = f.make(entries=[f.entry(original)], enabled=False, rotation=1, deployment="9" * 64)
    control.server_generation["invocation_id"] = "7" * 32
    capability = f.call(control, target=TARGET)
    assert capability["data"]["admission"] == "denied"
    grant = capability["data"]["capability"]["cleanup_grant"]
    assert grant["original_admission_binding"] == json.loads(f.rig.admission.binding_json)
    assert grant["resolver_generation"] == control.server_generation
    for operation in ("status", "cancel", "reconcile"):
        response = f.call(
            control, operation, target=TARGET, capability=capability["capability_sha256"]
        )
        assert response["data"]["cleanup_grant_sha256"] == digest(grant)
    terminal = response["data"]["observation"]["terminal_receipt"]
    assert terminal["original_budget"]["envelope_deadline"] == 130.0
    assert terminal["admission_binding"] == grant["original_admission_binding"]
    f.rig.now += 1
    refreshed = f.call(control, target=TARGET)
    assert refreshed["capability_sha256"] != capability["capability_sha256"]
    assert refreshed["data"]["capability"]["cleanup_grant_sha256"] == digest(grant)
    again = f.call(control, "reconcile", target=TARGET, capability=refreshed["capability_sha256"])
    assert again["data"]["observation"]["terminal_receipt"] == terminal
    assert control._capabilities == {}
    with closing(f.rig.store._connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM aos_cleanup_grants").fetchone()[0] == 1
        assert connection.execute(
            "SELECT active_owner,active_token FROM gpu_turn_state"
        ).fetchone()[:] == (None, 0)


def test_current_capability_cannot_bypass_missing_retired_cleanup_acl(cleanup_rig):
    f = cleanup_rig
    f.rig.register()
    control = f.make(rotation=1)
    current = f.call(control)
    for operation in ("status", "cancel", "reconcile"):
        with pytest.raises(ControlStoreError, match="unauthorized"):
            f.call(control, operation, target=TARGET, capability=current["capability_sha256"])
    with pytest.raises(ControlError, match="unauthorized"):
        f.call(control, target=TARGET)
    with pytest.raises(ControlError):
        f.first.admit_infer(f.peer, PROFILE, DEPLOYMENT)


def test_tombstone_keeps_null_admission_and_separate_original_cleanup_authority(cleanup_rig):
    f = cleanup_rig
    first = f.call(f.first, "cancel", target=TARGET, capability=f.capability["capability_sha256"])
    terminal = first["data"]["terminal_receipt"]
    assert terminal["admission_binding"] is None
    original = f.rig.store.cleanup_original(PEER, TARGET, PROFILE, DEPLOYMENT)
    assert original["original_admission_binding"] is None
    authorization = original["original_cleanup_authorization"]
    assert authorization["operations"] == ["cancel"]
    assert authorization["target"] == TARGET
    assert original["original_cleanup_authorization_sha256"] == digest(authorization)
    control = f.make(entries=[f.entry(original)], enabled=False, rotation=1)
    capability = f.call(control, target=TARGET)
    observed = f.call(
        control, "reconcile", target=TARGET, capability=capability["capability_sha256"]
    )
    assert observed["data"]["observation"]["terminal_receipt"] == terminal


@pytest.mark.parametrize(
    "denial",
    [
        "wrong_pin",
        "new_caller",
        "wrong_target",
        "cancel_not_granted",
        "expired",
        "policy_replaced",
        "source_replaced",
        "resolver_changed",
    ],
)
def test_cleanup_grant_cannot_expand_target_generation_or_rights(cleanup_rig, denial):
    f = cleanup_rig
    f.rig.register()
    original = f.rig.store.cleanup_original(PEER, TARGET, PROFILE, DEPLOYMENT)
    entry = f.entry(original, ["status"])
    if denial == "wrong_pin":
        entry["original_admission_binding_sha256"] = "9" * 64
    control = f.make(entries=[entry], enabled=False, rotation=1)
    if denial == "wrong_pin":
        with pytest.raises(ControlError, match="unauthorized"):
            f.call(control, target=TARGET)
        return
    capability = f.call(control, target=TARGET)
    target, caller, operation = TARGET, f.peer, "status"
    if denial == "new_caller":
        caller = replace(f.peer, invocation_id="9" * 32)
        control.authenticator.still_current = lambda actual: actual == caller
    elif denial == "wrong_target":
        target = {**TARGET, "request_id": "8" * 32}
    elif denial == "cancel_not_granted":
        operation = "cancel"
    elif denial == "expired":
        f.rig.now += 61
    elif denial == "source_replaced":
        (control.policy.source_root / "lab/llm/aos_gpu_control.py").write_text("unapproved source")
    elif denial == "resolver_changed":
        control.server_generation["start_ticks"] += 1
    else:
        f.policy_path.write_bytes(
            f.policy_path.read_bytes().replace(b'"enabled":false', b'"enabled":true')
        )
    with pytest.raises((ControlError, ControlStoreError)):
        f.call(
            control,
            operation,
            target=target,
            caller=caller,
            capability=capability["capability_sha256"],
        )
    assert f.rig.status()["cancel_requested"] is False


def test_cleanup_capability_cannot_authorize_infer_and_old_null_evidence_is_denied(cleanup_rig):
    f = cleanup_rig
    f.rig.register()
    original = f.rig.store.cleanup_original(PEER, TARGET, PROFILE, DEPLOYMENT)
    control = f.make(entries=[f.entry(original)], rotation=1)
    f.call(control, target=TARGET)
    with pytest.raises(ControlError, match="capability_mismatch"):
        control.admit_infer(f.peer, PROFILE, DEPLOYMENT)
    with closing(f.rig.store._connect()) as connection:
        connection.execute(
            "UPDATE aos_control_requests SET admission_binding_json=NULL,"
            "admission_binding_sha256=NULL"
        )
        connection.commit()
    with pytest.raises(ControlStoreError, match="unauthorized"):
        f.call(control, target=TARGET)


def test_cleanup_grant_capacity_preserves_existing_grant(cleanup_rig, monkeypatch):
    f = cleanup_rig
    f.rig.register()
    original = f.rig.store.cleanup_original(PEER, TARGET, PROFILE, DEPLOYMENT)
    control = f.make(entries=[f.entry(original)], enabled=False, rotation=1)
    first = f.call(control, target=TARGET)
    monkeypatch.setattr("lab.llm.aos_gpu_control_store.MAX_TARGET_CLEANUP_GRANTS", 1)
    f.rig.now += 1
    assert (
        f.call(control, target=TARGET)["data"]["capability"]["cleanup_grant_sha256"]
        == first["data"]["capability"]["cleanup_grant_sha256"]
    )
    rotated = f.make(entries=[f.entry(original)], enabled=False, rotation=2)
    with pytest.raises(ControlStoreError, match="busy"):
        f.call(rotated, target=TARGET)


def test_policy_cleanup_acl_rejects_inference_rights_and_unknown_fields(cleanup_rig):
    f = cleanup_rig
    f.rig.register()
    original = f.rig.store.cleanup_original(PEER, TARGET, PROFILE, DEPLOYMENT)
    entry = f.entry(original)
    entry["operations"] = ["infer"]
    with pytest.raises(ControlError, match="invalid_frame"):
        f.make(entries=[entry], rotation=1)
    entry = {**f.entry(original), "allow_same_uid": True}
    with pytest.raises(ControlError, match="invalid_frame"):
        f.make(entries=[entry], rotation=1)


def test_retired_four_field_binding_is_cleanup_only(cleanup_rig):
    f = cleanup_rig
    f.rig.register()
    binding = json.loads(f.rig.admission.binding_json)
    del binding["profile_pin"]["output_contract"]
    # Synthetic migration evidence: retain an original pre-v2 four-field pin.
    with closing(f.rig.store._connect()) as connection:
        connection.execute(
            "UPDATE aos_control_requests SET admission_binding_json=?, admission_binding_sha256=?",
            (canonical(binding).decode(), digest(binding)),
        )
        connection.commit()
    with pytest.raises(ControlStoreError, match="unauthorized"):
        f.rig.submit()
    original = f.rig.store.cleanup_original(PEER, TARGET, PROFILE, DEPLOYMENT)
    control = f.make(entries=[f.entry(original)], enabled=False, rotation=1)
    capability = f.call(control, target=TARGET)
    observed = f.call(control, "cancel", target=TARGET, capability=capability["capability_sha256"])
    assert observed["data"]["cleanup_grant"]["original_admission_binding"] == binding
    assert observed["data"]["observation"]["terminal_receipt"]["admission_binding"] == binding
