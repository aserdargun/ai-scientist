"""CPU source-only service composition; native/systemd observations are fixtures."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from test_shared_launch_authority import Rig as AuthorityRig
from test_shared_launch_authority import write

from lab.llm.aos_gpu_control import LabAOSControl
from lab.llm.aos_gpu_control_store import ControlStore, ControlStoreError, control_deadline, digest
from lab.llm.aos_gpu_service import SHARED_DESKTOP_UNIT, _verify_current_admission
from lab.llm.gpu_scheduler import PrincipalReceipt, ProcessIdentity, SharedGpuScheduler
from lab.llm.shared_launch_authority import SharedLaunchPolicy
from lab.llm.shared_launch_ledger import LaunchBinding, LaunchLedgerError
from lab.llm.shared_launch_transport import WIRE_SCHEMA_SHA256


@pytest.fixture
def rig(tmp_path):
    source = AuthorityRig(tmp_path, contract_sha256=WIRE_SCHEMA_SHA256)
    source.control.config["caller_unit"] = source.grant.unit
    source.control.config["history_reconcile"] = False
    source.control.sha256 = write(source.control_path, source.control.config)
    data = source.grant.model_dump(mode="json", by_alias=True)
    data["pins"]["policy_revision_sha256"] = source.control.sha256
    source.grant = LaunchBinding.model_validate(data)
    source.policy_document["entries"][0]["binding"] = data
    source.policy = SharedLaunchPolicy(
        source.policy_path, expected_raw_sha256=write(source.policy_path, source.policy_document)
    )
    source.authority.policy = source.policy
    authenticator = source.authenticator(SHARED_DESKTOP_UNIT)
    peer = authenticator.authenticate(source.process.pid, source.process.uid)
    store = ControlStore(
        source.database,
        boot_id=lambda: source.process.boot_id,
        peer_verifier=lambda value: authenticator.still_current(type(peer)(**value)),
        current_admission_verifier=lambda connection, binding: _verify_current_admission(
            connection, binding, control=control, shared_launch_authority=source.authority
        ),
    )
    control = LabAOSControl(
        policy=source.control,
        authenticator=authenticator,
        store=store,
        server_generation=source.grant.broker.model_dump(mode="json"),
        server_current=lambda: source.broker_live,
        clock=lambda: time.clock_gettime(time.CLOCK_BOOTTIME),
    )
    control._mint(peer, source.profile)
    admission = control.admit_infer(
        peer, source.profile.profile_id, source.profile.deployment_digest
    )
    return SimpleNamespace(
        source=source,
        control=control,
        store=store,
        peer=peer,
        binding=source.admission(),
        admission=admission,
    )


def verify(rig, *, authority=None):
    with closing(rig.store._connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _verify_current_admission(
            connection, rig.binding, control=rig.control, shared_launch_authority=authority
        )
        assert connection.in_transaction


def test_shared_default_deny_and_missing_entry_then_real_entered_authority(rig):
    with pytest.raises(ControlStoreError, match="unauthorized"):
        verify(rig)
    with pytest.raises(ControlStoreError, match="unauthorized"):
        verify(rig, authority=rig.source.authority)
    sha = rig.source.consume()
    rig.source.call("enter", intent_sha256=sha)
    verify(rig, authority=rig.source.authority)
    assert "verify_runtime" in rig.source.phases


def test_current_control_runs_first_and_both_verifiers_share_the_transaction(rig, monkeypatch):
    sha = rig.source.consume()
    rig.source.call("enter", intent_sha256=sha)
    calls = []
    current = rig.control.verify_saved_admission
    runtime = rig.source.authority.verify_runtime

    def checked_control(connection, binding):
        calls.append(("control", connection))
        current(connection, binding)

    def checked_runtime(connection, binding, *, deadline):
        calls.append(("runtime", connection))
        runtime(connection, binding, deadline=deadline)

    monkeypatch.setattr(rig.control, "verify_saved_admission", checked_control)
    monkeypatch.setattr(rig.source.authority, "verify_runtime", checked_runtime)
    monkeypatch.setattr(
        rig.source.authority.ledger, "_transaction", lambda **_: pytest.fail("Nested transaction")
    )
    verify(rig, authority=rig.source.authority)
    assert [name for name, _ in calls] == ["control", "runtime"]
    assert calls[0][1] is calls[1][1]


def test_authority_on_another_database_denies_before_runtime_call(rig, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    authority = AuthorityRig(other).authority
    monkeypatch.setattr(
        authority, "verify_runtime", lambda *_, **__: pytest.fail("Wrong DB authority")
    )
    with pytest.raises(ControlStoreError, match="unauthorized"):
        verify(rig, authority=authority)


def test_old_authority_contract_denies_before_entry_verification(rig, monkeypatch):
    rig.source.authority.contract_sha256 = "0" * 64
    monkeypatch.setattr(
        rig.source.authority,
        "verify_runtime",
        lambda *_, **__: pytest.fail("Old contract entry work"),
    )
    with pytest.raises(ControlStoreError, match="unauthorized"):
        verify(rig, authority=rig.source.authority)


@pytest.mark.parametrize("remaining", [None, 0.2, 10.0, -0.1])
def test_runtime_boottime_deadline_uses_remaining_inherited_budget(rig, monkeypatch, remaining):
    observed = []
    monkeypatch.setattr(rig.control, "verify_saved_admission", lambda *_: None)
    monkeypatch.setattr("lab.llm.aos_gpu_service.time.monotonic", lambda: 100.0)
    monkeypatch.setattr("lab.llm.aos_gpu_service.boottime", lambda: 1000.0)
    monkeypatch.setattr(
        rig.source.authority,
        "verify_runtime",
        lambda connection, binding, *, deadline: observed.append((connection, deadline)),
    )
    outer = None if remaining is None else 100.0 + remaining
    token = control_deadline.set(outer)
    try:
        if remaining is not None and remaining <= 0:
            with pytest.raises(ControlStoreError, match="deadline_exceeded"):
                verify(rig, authority=rig.source.authority)
            assert observed == []
        else:
            verify(rig, authority=rig.source.authority)
            assert observed[0][1] == pytest.approx(1000.0 + min(3.0, remaining or 3.0))
        assert control_deadline.get() == outer
    finally:
        control_deadline.reset(token)


@pytest.mark.parametrize("inherited", [float("nan"), float("inf"), True])
def test_invalid_inherited_deadline_denies_before_conversion_or_runtime(
    rig, monkeypatch, inherited
):
    monkeypatch.setattr(rig.control, "verify_saved_admission", lambda *_: None)
    monkeypatch.setattr(
        "lab.llm.aos_gpu_service.boottime", lambda: pytest.fail("Invalid deadline conversion")
    )
    monkeypatch.setattr(
        rig.source.authority,
        "verify_runtime",
        lambda *_, **__: pytest.fail("Invalid deadline entry work"),
    )
    token = control_deadline.set(inherited)
    try:
        with pytest.raises(ControlStoreError, match="deadline_exceeded"):
            verify(rig, authority=rig.source.authority)
    finally:
        control_deadline.reset(token)


@pytest.mark.parametrize(
    "error, expected",
    [
        (LaunchLedgerError("entry_required"), "unauthorized"),
        (LaunchLedgerError("entry_admission_closed"), "unauthorized"),
        (LaunchLedgerError("policy_changed"), "unauthorized"),
        (LaunchLedgerError("prerequisite_verifier_required"), "unauthorized"),
        (LaunchLedgerError("entered_generation_conflict"), "stale_generation"),
        (LaunchLedgerError("stale_broker"), "stale_generation"),
        (LaunchLedgerError("deadline_exceeded"), "deadline_exceeded"),
        (LaunchLedgerError("ledger_unavailable"), "internal_unavailable"),
        (OSError("observer unavailable"), "internal_unavailable"),
        (sqlite3.OperationalError("database unavailable"), "internal_unavailable"),
    ],
)
def test_explicit_entry_denials_are_distinct_from_observer_or_database_uncertainty(
    rig, monkeypatch, error, expected
):
    def denied(*_, **__):
        raise error

    monkeypatch.setattr(rig.source.authority, "verify_runtime", denied)
    with pytest.raises(ControlStoreError, match=expected):
        verify(rig, authority=rig.source.authority)


def test_wrapped_missing_entry_database_error_remains_unavailable(rig, monkeypatch):
    def unavailable(*_, **__):
        try:
            raise sqlite3.OperationalError("database unavailable")
        except sqlite3.Error as error:
            raise LaunchLedgerError("entry_required") from error

    monkeypatch.setattr(rig.source.authority, "verify_runtime", unavailable)
    with pytest.raises(ControlStoreError, match="internal_unavailable"):
        verify(rig, authority=rig.source.authority)


def test_legacy_caller_keeps_current_control_without_shared_authority(rig):
    unit = "swapp-aos-gpu-joint-acceptance.service"
    rig.source.control.config["caller_unit"] = unit
    rig.source.control.sha256 = write(rig.source.control_path, rig.source.control.config)
    authenticator = rig.source.authenticator(unit)
    peer = authenticator.authenticate(rig.source.process.pid, rig.source.process.uid)
    control = LabAOSControl(
        policy=rig.source.control,
        authenticator=authenticator,
        store=rig.store,
        server_generation=rig.control.server_generation,
        clock=rig.control.clock,
    )
    binding = control._binding(peer, rig.source.profile)
    with closing(rig.store._connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _verify_current_admission(connection, binding, control=control)


def test_real_launch_policy_revoke_retires_queue_and_allows_lab_progress(rig):
    sha = rig.source.consume()
    rig.source.call("enter", intent_sha256=sha)
    source = rig.source

    def principal(owner):
        return PrincipalReceipt(
            owner,
            ProcessIdentity(source.process.pid, source.process.start_ticks, source.process.boot_id),
            source.grant.unit if owner == "aos" else "swapp-lab-gpu-fixture.service",
            "e" * 32,
        )

    scheduler = SharedGpuScheduler(
        source.database,
        principal_resolver=SimpleNamespace(
            resolve=principal, verify=lambda value: value == principal(value.owner)
        ),
        drain_verifier=lambda _: pytest.fail("No physical cleanup or GPU operations"),
        max_activation_seconds=10,
        max_inference_seconds=5,
        max_total_seconds=15,
        control_store=rig.store,
    )
    payload = b'{"source_only":"CPU queued policy revoke"}'
    request = "a" * 32
    request_hash = hashlib.sha256(payload).hexdigest()
    profile = source.profile
    rig.store.register_intent(
        asdict(rig.peer),
        request,
        request_hash,
        profile.profile_id,
        profile.deployment_digest,
        profile.config_sha256,
        profile.response_schema_sha256,
        time.clock_gettime(time.CLOCK_BOOTTIME) + 60,
        {
            "activation_seconds": 10,
            "inference_seconds": 5,
            "total_seconds": 15,
            "queue_seconds": 300,
            "max_tokens": 16,
        },
        admission=rig.admission,
    )
    scheduler.submit("aos", request, payload)
    scheduler.submit("lab", "lab-request", b"CPU lab fixture")
    write(source.policy_path, {**source.policy_document, "enabled": False})
    lease = scheduler.try_acquire("lab", "lab-request")
    assert lease is not None and lease.owner == "lab" and lease.fencing_token == 1
    target = {
        "request_id": request,
        "request_sha256": request_hash,
        "original_peer_generation_sha256": digest(asdict(rig.peer)),
    }
    terminal = rig.store.lookup(
        asdict(rig.peer), target, profile.profile_id, profile.deployment_digest
    )["terminal_receipt"]
    assert terminal["release_outcome"] == "never_admitted"
    assert terminal["allocation_binding_sha256"] is None
