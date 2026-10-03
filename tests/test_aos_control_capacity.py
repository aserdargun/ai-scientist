"""Small synthetic ledger limits; no runtime/GPU capacity claims."""

import json
from contextlib import closing

import pytest
from test_aos_cleanup_authority import cleanup_rig as cleanup_rig
from test_aos_control_store import DEPLOYMENT, PEER, PROFILE, TARGET

from lab.llm import aos_gpu_control_store as storage
from lab.llm.aos_gpu_control import SCHEMA, ControlError, LabAOSControl, digest
from lab.llm.aos_gpu_control_store import AdmissionGrant, ControlStoreError


def _snapshot(f):
    with closing(f.rig.store._connect()) as connection:
        return (
            connection.execute("SELECT COUNT(*) FROM aos_control_idempotency").fetchone()[0],
            [tuple(row) for row in connection.execute("SELECT * FROM aos_control_reservations")],
            connection.execute("SELECT COUNT(*) FROM aos_cleanup_grants").fetchone()[0],
        )


def _cap_request(control_id):
    return {
        "schema": SCHEMA,
        "version": 1,
        "op": "capability",
        "control_id": control_id,
        "expected_capability_sha256": None,
        "profile_id": PROFILE,
        "deployment_digest": DEPLOYMENT,
        "target": dict(TARGET),
    }


@pytest.mark.parametrize("retired", [False, True])
@pytest.mark.parametrize("operation", ["capability", "status", "cancel", "reconcile"])
def test_retained_target_controls_survive_unreserved_ledger_saturation(
    cleanup_rig, monkeypatch, retired, operation
):
    f = cleanup_rig
    monkeypatch.setattr(storage, "MAX_CONTROL_IDS", 40)
    monkeypatch.setattr(storage, "MAX_CLEANUP_GRANTS", 4)
    monkeypatch.setattr(storage, "CLEANUP_GRANT_RESERVE", 2, raising=False)
    f.rig.register(130.0)
    control, capability = f.first, f.capability
    if retired:
        original = f.rig.store.cleanup_original(PEER, TARGET, PROFILE, DEPLOYMENT)
        control = f.make(entries=[f.entry(original)], enabled=False, rotation=1)
        if operation != "capability":
            capability = f.call(control, target=TARGET)
    saturated = False
    for number in range(100, 150):
        try:
            f.rig.store.remember_control(PEER, f"{number:032x}", "8" * 64)
        except ControlStoreError as exc:
            assert exc.code == "busy"
            saturated = True
            break
    assert saturated, "fixture must exhaust the unreserved ledger allowance"
    response = f.call(
        control,
        operation,
        target=TARGET,
        capability=None if operation == "capability" else capability["capability_sha256"],
    )
    assert response["ok"] is True
    assert f.rig.store.cleanup_original(PEER, TARGET, PROFILE, DEPLOYMENT)[
        "original_admission_binding"
    ] == json.loads(f.rig.admission.binding_json)


def test_persisted_capability_retry_after_same_generation_reconstruction_without_renewal(
    cleanup_rig,
):
    f = cleanup_rig
    f.rig.register(130.0)
    request = _cap_request("f" * 32)
    first = f.first.handle(f.peer, request)
    before = _snapshot(f)
    f.rig.now += 61
    restarted = LabAOSControl(
        policy=f.first.policy,
        authenticator=f.first.authenticator,
        store=f.rig.store,
        server_generation=dict(f.first.server_generation),
        clock=lambda: f.rig.now,
    )
    assert restarted.handle(f.peer, request) == first
    assert _snapshot(f) == before
    with pytest.raises(ControlError, match="capability_expired"):
        f.call(restarted, "status", target=TARGET, capability=first["capability_sha256"])
    assert _snapshot(f) == before
    refreshed = restarted.handle(f.peer, _cap_request("e" * 32))
    assert (
        refreshed["data"]["capability"]["expires_boottime"]
        > first["data"]["capability"]["expires_boottime"]
    )
    with closing(f.rig.store._connect()) as connection:
        assert (
            connection.execute("SELECT original_deadline FROM aos_control_requests").fetchone()[0]
            == 130.0
        )


def test_status_churn_cannot_spend_cancel_or_reconcile_reserve(cleanup_rig):
    f = cleanup_rig
    f.rig.register()
    with closing(f.rig.store._connect()) as connection:
        quota = json.loads(
            connection.execute("SELECT quota_json FROM aos_control_reservations").fetchone()[0]
        )
    for _ in range(quota["status"]):
        f.call(f.first, "status", target=TARGET, capability=f.capability["capability_sha256"])
    before = _snapshot(f)
    with pytest.raises(ControlStoreError, match="busy"):
        f.call(f.first, "status", target=TARGET, capability=f.capability["capability_sha256"])
    assert _snapshot(f) == before
    canceled = f.call(
        f.first, "cancel", target=TARGET, capability=f.capability["capability_sha256"]
    )
    terminal = canceled["data"]["terminal_receipt"]
    reconciled = f.call(
        f.first, "reconcile", target=TARGET, capability=f.capability["capability_sha256"]
    )
    assert reconciled["data"]["terminal_receipt"] == terminal
    assert _snapshot(f)[1], "terminal retains unused reservations without recycling audit capacity"


def test_denied_target_and_revoked_policy_do_not_consume_any_slots(cleanup_rig):
    f = cleanup_rig
    f.rig.register()
    before = _snapshot(f)
    with pytest.raises(ControlStoreError, match="request_conflict"):
        f.call(
            f.first,
            "cancel",
            target={**TARGET, "request_sha256": "9" * 64},
            capability=f.capability["capability_sha256"],
        )
    assert _snapshot(f) == before
    with pytest.raises(ControlError, match="invalid_frame"):
        f.call(
            f.first,
            "status",
            target={**TARGET, "unexpected": True},
            capability=f.capability["capability_sha256"],
        )
    assert _snapshot(f) == before
    f.make(rotation=1)
    with pytest.raises(ControlError):
        f.call(f.first, "status", target=TARGET, capability=f.capability["capability_sha256"])
    assert _snapshot(f) == before


@pytest.mark.parametrize("operation", ["status", "infer"])
def test_policy_revoke_during_last_peer_observation_rolls_back(cleanup_rig, monkeypatch, operation):
    f = cleanup_rig
    if operation == "status":
        f.rig.register()
    before = _snapshot(f)
    phase = {"written": False, "revoke_next_peer": False, "revoked": False}
    mutation_name = "_remember_frame" if operation == "status" else "_reserve_controls"
    original_mutation = getattr(f.rig.store, mutation_name)

    def after_sql_mutation(*args, **kwargs):
        result = original_mutation(*args, **kwargs)
        phase["written"] = True
        return result

    original_profile = f.first.policy.bound_profile

    def after_final_policy_verify(*args, **kwargs):
        result = original_profile(*args, **kwargs)
        if phase["written"]:
            phase["revoke_next_peer"] = True
        return result

    original_peer = f.first.authenticator.still_current

    def revoke_during_last_peer(peer):
        if phase["revoke_next_peer"]:
            config = json.loads(f.policy_path.read_text())
            config["enabled"] = False
            f.policy_path.write_text(json.dumps(config))
            phase["revoke_next_peer"] = False
            phase["revoked"] = True
        return original_peer(peer)

    monkeypatch.setattr(f.rig.store, mutation_name, after_sql_mutation)
    monkeypatch.setattr(f.first.policy, "bound_profile", after_final_policy_verify)
    monkeypatch.setattr(f.first.authenticator, "still_current", revoke_during_last_peer)
    with pytest.raises(ControlError, match="capability_mismatch"):
        if operation == "status":
            f.call(f.first, "status", target=TARGET, capability=f.capability["capability_sha256"])
        else:
            f.rig.register()
    assert phase["revoked"], "revoke must occur after the transaction's SQL mutation"
    assert _snapshot(f) == before
    with closing(f.rig.store._connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM aos_control_requests").fetchone()[0] == (
            1 if operation == "status" else 0
        )


@pytest.mark.parametrize("resource", ["ids", "grants", "lifecycle"])
def test_infer_denied_before_intent_when_full_reservation_cannot_fit(
    cleanup_rig, monkeypatch, resource
):
    f = cleanup_rig
    monkeypatch.setattr(
        storage,
        {
            "ids": "MAX_CONTROL_IDS",
            "grants": "MAX_CLEANUP_GRANTS",
            "lifecycle": "MAX_TARGET_REFRESHES",
        }[resource],
        1,
    )
    before = _snapshot(f)
    with pytest.raises(ControlStoreError, match="busy"):
        f.rig.register()
    assert _snapshot(f) == before
    with closing(f.rig.store._connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM aos_control_requests").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM gpu_turn_requests").fetchone()[0] == 0


def test_crash_rollback_and_retry_preserve_original_quota_and_spending(cleanup_rig):
    f = cleanup_rig
    original = f.rig.admission
    checks = []

    def crash_before_commit():
        original.verify_current()
        checks.append(True)
        if len(checks) == 2:
            raise ControlError("stale_generation")

    f.rig.admission = AdmissionGrant(original.binding_json, crash_before_commit)
    before = _snapshot(f)
    with pytest.raises(ControlError, match="stale_generation"):
        f.rig.register(130.0)
    assert _snapshot(f) == before
    f.rig.admission = original
    assert f.rig.register(130.0) == 130.0
    f.call(f.first, "status", target=TARGET, capability=f.capability["capability_sha256"])
    after_status = _snapshot(f)
    assert f.rig.register(500.0) == 130.0
    assert _snapshot(f) == after_status
    with closing(f.rig.store._connect()) as connection:
        row = connection.execute("SELECT * FROM aos_control_reservations").fetchone()
        assert row["quota_sha256"] == digest(json.loads(row["quota_json"]))
        connection.execute("DELETE FROM aos_control_reservations")
        connection.commit()
    with pytest.raises(ControlStoreError, match="unauthorized"):
        f.rig.register(500.0)


def test_retired_grants_use_reserved_space_and_failure_rolls_back_cap_slot(
    cleanup_rig, monkeypatch
):
    f = cleanup_rig
    monkeypatch.setattr(storage, "MAX_CLEANUP_GRANTS", 4)
    monkeypatch.setattr(storage, "CLEANUP_GRANT_RESERVE", 2)
    f.rig.register()
    # Synthetic pre-existing historical audit rows occupy all unreserved grant space.
    with closing(f.rig.store._connect()) as connection:
        for value in ("1", "2"):
            connection.execute(
                "INSERT INTO aos_cleanup_grants VALUES(?,?,?)", (value * 64, value * 32, "{}")
            )
        connection.commit()
    original = f.rig.store.cleanup_original(PEER, TARGET, PROFILE, DEPLOYMENT)
    for rotation in (1, 2):
        control = f.make(entries=[f.entry(original)], enabled=False, rotation=rotation)
        assert f.call(control, target=TARGET)["ok"]
    control = f.make(entries=[f.entry(original)], enabled=False, rotation=3)
    before = _snapshot(f)
    with pytest.raises(ControlStoreError, match="busy"):
        f.call(control, target=TARGET)
    assert _snapshot(f) == before


def test_target_refresh_does_not_depend_on_full_infer_capability_cache(cleanup_rig, monkeypatch):
    f = cleanup_rig
    f.rig.register()
    monkeypatch.setattr("lab.llm.aos_gpu_control.MAX_CAPABILITIES", 0)
    assert f.call(f.first, target=TARGET)["ok"]


def test_resolver_generation_change_requires_new_explicit_cleanup_capability(cleanup_rig):
    f = cleanup_rig
    f.rig.register()
    first = f.call(f.first, target=TARGET)
    original = f.rig.store.cleanup_original(PEER, TARGET, PROFILE, DEPLOYMENT)
    replacement = f.make(entries=[f.entry(original)], enabled=False, rotation=1)
    replacement.server_generation["start_ticks"] += 1
    before = _snapshot(f)
    with pytest.raises(ControlError, match="capability_mismatch"):
        f.call(replacement, "status", target=TARGET, capability=first["capability_sha256"])
    assert _snapshot(f) == before
    current = f.call(replacement, target=TARGET)
    grant = current["data"]["capability"]["cleanup_grant"]
    assert grant["resolver_generation"] == replacement.server_generation
    assert grant["original_admission_binding"] == original["original_admission_binding"]


@pytest.mark.parametrize("column", ["status_remaining", "grant_remaining"])
def test_corrupt_negative_reservations_fail_closed_global_accounting(cleanup_rig, column):
    f = cleanup_rig
    f.rig.register()
    with closing(f.rig.store._connect()) as connection:
        with pytest.raises(ControlStoreError, match="internal_unavailable"):
            connection.execute(f"UPDATE aos_control_reservations SET {column}=-1")
        # Explicit synthetic corruption bypass; normal writes were rejected above.
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(f"UPDATE aos_control_reservations SET {column}=-1")
        connection.commit()
    before = _snapshot(f)
    with pytest.raises(ControlStoreError, match="internal_unavailable"):
        f.rig.store.remember_control(PEER, "f" * 32, "e" * 64)
    assert _snapshot(f) == before
