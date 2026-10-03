"""CPU draft evidence only: no real authority, service, process or GPU claim."""

from __future__ import annotations

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from lab.llm.gpu_scheduler import SharedGpuScheduler
from lab.llm.shared_launch_ledger import (
    DurableLaunchIntent,
    LaunchBinding,
    LaunchLedgerError,
    SharedLaunchLedger,
)

BOOT = "00000000-0000-0000-0000-000000000001"


def binding(**updates):
    process = {"uid": os.getuid(), "pid": 123, "start_ticks": 45, "boot_id": BOOT}
    service = {
        **process,
        "unit": "fixture.service",
        "invocation_id": "a" * 32,
        "control_group": "/fixture.service",
    }
    value = {
        "schema": "aos-scientist.shared-launch.v1-proposal2.draft",
        "request_id": "1" * 32,
        "review_id": "2" * 32,
        "operation": "launch_shared_desktop",
        "purpose": "reviewed_shared_desktop",
        "issuer": service,
        "broker": {**service, "pid": 124},
        "manager": process,
        "unit": "swapp-aos-gpu-shared-desktop-default.service",
        "uid": os.getuid(),
        "boot_id": BOOT,
        "app_session": "app-" + "3" * 32,
        "workspace": "/fixture/workspace",
        "workspace_device": 1,
        "workspace_inode": 2,
        "workspace_uid": os.getuid(),
        "plan_sha256": "4" * 64,
        "provision_sha256": "5" * 64,
        "predecessor_identity_sha256": None,
        "pins": {
            "aos_root": "/fixture/aos",
            "scientist_root": "/fixture/scientist",
            **dict.fromkeys(
                (
                    "aos_import_closure_sha256",
                    "scientist_import_closure_sha256",
                    "python_identity_sha256",
                    "launcher_sha256",
                    "config_map_sha256",
                    "model_profile_sha256",
                    "reviewed_launch_input_sha256",
                    "policy_revision_sha256",
                    "contract_sha256",
                ),
                "6" * 64,
            ),
        },
        "drain_seal_sha256": "7" * 64,
        "native_maintenance_sha256": "8" * 64,
        "prelaunch_exclusion_sha256": "9" * 64,
        "prerequisite_deadline_boottime": 1000.0,
        "clock": "CLOCK_BOOTTIME",
        "issued_boottime": 100.0,
        "expires_boottime": 200.0,
    }
    return LaunchBinding.model_validate({**value, **updates})


def intent(grant):
    return DurableLaunchIntent(
        request_id=grant.request_id,
        binding_sha256=grant.sha256(),
        intent_sha256="b" * 64,
        marker="/fixture/durable-intent.json",
    )


class Rig:
    def __init__(self, tmp_path: Path):
        self.now = 100.0
        self.boot = BOOT
        self.policy_open = True
        self.durable = True
        self.generation_current = True
        self.source_current = True
        self.suspend_in_claim = False
        self.observed = []
        self.database = tmp_path / "arbiter.sqlite3"
        # Use the actual existing allocator schema, never a launch-owned allocator.
        SharedGpuScheduler(
            self.database,
            principal_resolver=self,
            drain_verifier=lambda _: False,
        )
        self.database.chmod(0o600)
        self.ledger = self.reopen()
        self.ledger.initialize_draft_schema()
        self.grant = binding()

    def reopen(self):
        return SharedLaunchLedger(
            self.database,
            draft_enabled=True,
            verifier=self.check,
            clock=lambda: self.now,
            boot_id=lambda: self.boot,
        )

    def check(self, connection, grant, operation, marker):
        assert connection.in_transaction
        self.observed.append(operation)
        if operation in {"status", "revoke"}:
            return  # Separate fixture original-target read/control authorization.
        if not self.policy_open or not self.generation_current or not self.source_current:
            raise LaunchLedgerError("fixture_current_denied")
        if operation == "claim":
            assert marker is not None and marker.binding_sha256 == grant.sha256()
            if not self.durable:
                raise LaunchLedgerError("fixture_intent_not_durable")
            if self.suspend_in_claim:
                self.now = grant.expires_boottime

    def rows(self):
        with sqlite3.connect(self.database) as connection:
            return connection.execute("SELECT * FROM aos_shared_launch_draft_v1").fetchall()


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_disabled_construction_and_initialization_are_inert(tmp_path):
    database = tmp_path / "absent.sqlite3"
    ledger = SharedLaunchLedger(database)
    with pytest.raises(LaunchLedgerError, match="draft_disabled"):
        ledger.initialize_draft_schema()
    assert not database.exists()


@pytest.mark.parametrize(
    "updates",
    [
        {"expires_boottime": 1001.0},
        {"issued_boottime": float("nan")},
        {"workspace": "/fixture/../escape"},
        {"uid": True},
        {"unexpected": "extension"},
        {"expires_boottime": 0.0},
        {"prerequisite_deadline_boottime": 150.0},
    ],
)
def test_strict_closed_original_binding(updates):
    with pytest.raises(ValidationError):
        binding(**updates)


def test_verify_is_repeatable_read_only_and_claim_single_use(rig):
    initial = rig.ledger.issue(rig.grant)
    original_rows = rig.rows()
    assert not initial.consumed
    for _ in range(3):
        assert rig.ledger.verify(rig.grant) == initial
    assert rig.rows() == original_rows
    claimed = rig.ledger.claim(rig.grant, intent(rig.grant))
    assert claimed.consumed_now and claimed.status.cleanup_required
    consumed_rows = rig.rows()
    assert rig.ledger.verify(rig.grant).consumed
    assert not rig.reopen().claim(rig.grant, intent(rig.grant)).consumed_now
    assert rig.ledger.issue(rig.grant).consumed
    assert rig.rows() == consumed_rows
    with sqlite3.connect(rig.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM gpu_turn_requests").fetchone()[0] == 0
        assert connection.execute("SELECT active_token FROM gpu_turn_state").fetchone()[0] == 0


def test_concurrent_claims_and_lost_ack_never_replay(rig):
    rig.ledger.issue(rig.grant)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: rig.reopen().claim(rig.grant, intent(rig.grant)),
                range(2),
            )
        )
    assert sum(result.consumed_now for result in results) == 1
    # Simulate the successful reply being lost and a replacement process reading it.
    assert not rig.reopen().claim(rig.grant, intent(rig.grant)).consumed_now
    assert rig.ledger.status(rig.grant).cleanup_required


def test_changed_digest_and_reused_target_scope_denied(rig):
    rig.ledger.issue(rig.grant)
    with pytest.raises(LaunchLedgerError, match="request_conflict"):
        rig.ledger.issue(binding(plan_sha256="f" * 64))
    with pytest.raises(LaunchLedgerError, match="target_scope_reused"):
        rig.ledger.issue(binding(request_id="d" * 32))
    assert len(rig.rows()) == 1


@pytest.mark.parametrize("update", [{"intent_sha256": "f" * 64}, {"marker": "/other/marker"}])
def test_consumed_intent_is_immutable(rig, update):
    rig.ledger.issue(rig.grant)
    original = intent(rig.grant)
    rig.ledger.claim(rig.grant, original)
    changed = DurableLaunchIntent.model_validate({**original.model_dump(), **update})
    with pytest.raises(LaunchLedgerError, match="intent_conflict"):
        rig.reopen().claim(rig.grant, changed)


@pytest.mark.parametrize("cause", ["policy", "generation", "source", "boot", "expiry", "suspend"])
def test_fresh_checks_after_durable_intent_before_consumption(rig, cause):
    rig.ledger.issue(rig.grant)
    if cause == "policy":
        rig.policy_open = False
    elif cause == "generation":
        rig.generation_current = False
    elif cause == "source":
        rig.source_current = False
    elif cause == "boot":
        rig.boot = "00000000-0000-0000-0000-000000000002"
    elif cause == "expiry":
        rig.now = rig.grant.expires_boottime
    else:
        rig.suspend_in_claim = True
    with pytest.raises(LaunchLedgerError):
        rig.ledger.claim(rig.grant, intent(rig.grant))
    assert not rig.ledger.status(rig.grant).consumed


def test_missing_durable_intent_rolls_back(rig):
    rig.ledger.issue(rig.grant)
    rig.durable = False
    with pytest.raises(LaunchLedgerError, match="intent_not_durable"):
        rig.ledger.claim(rig.grant, intent(rig.grant))
    assert not rig.ledger.status(rig.grant).consumed


def test_revoke_keeps_original_uncertain_target_and_cleanup_obligation(rig):
    rig.ledger.issue(rig.grant)
    rig.ledger.claim(rig.grant, intent(rig.grant))
    rig.policy_open = False
    rig.now = 250.0
    revoked = rig.ledger.revoke(rig.grant)
    assert revoked.consumed and revoked.cleanup_required and revoked.expired
    assert rig.ledger.revoke(rig.grant) == revoked
    with pytest.raises(LaunchLedgerError, match="admission_closed"):
        rig.ledger.verify(rig.grant)
    assert not rig.ledger.claim(rig.grant, intent(rig.grant)).consumed_now
    assert json.loads(rig.rows()[0][6])["intent_sha256"] == "b" * 64
    rig.policy_open = True
    rig.now = 150.0
    next_grant = binding(request_id="e" * 32, app_session="app-" + "f" * 32)
    rig.ledger.issue(next_grant)
    with pytest.raises(LaunchLedgerError, match="original_cleanup_unresolved"):
        rig.ledger.claim(next_grant, intent(next_grant))


def test_verify_cannot_mutate_even_through_callback(rig):
    rig.ledger.issue(rig.grant)
    before = rig.rows()

    def mutating_verifier(connection, *_):
        connection.execute("DELETE FROM aos_shared_launch_draft_v1")

    ledger = SharedLaunchLedger(
        rig.database,
        draft_enabled=True,
        verifier=mutating_verifier,
        clock=lambda: rig.now,
        boot_id=lambda: rig.boot,
    )
    with pytest.raises(LaunchLedgerError, match="ledger_unavailable"):
        ledger.verify(rig.grant)
    assert rig.rows() == before
