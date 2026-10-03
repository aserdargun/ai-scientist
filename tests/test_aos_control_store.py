"""CPU fixture tests for candidate control transactions; no real GPU evidence.

Source prepared without running tests. The coordinated root owns execution.
"""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, replace
from threading import Barrier

import pytest
from aos_admission_fixture import BOOT, grant_for

from lab.llm.aos_gpu_control_store import (
    ControlCanceled,
    ControlStore,
    ControlStoreError,
    canonical,
    control_deadline,
    digest,
)
from lab.llm.gpu_scheduler import (
    LeaseConflict,
    PrincipalReceipt,
    ProcessIdentity,
    SharedGpuScheduler,
)

REQUEST = "a" * 32
PROFILE = "aos.decider.turn.v1"
DEPLOYMENT = "b" * 64
CONFIG = "c" * 64
SCHEMA = "d" * 64
PAYLOAD = b'{"fixture":"canonical original frame bytes"}'
REQUEST_HASH = hashlib.sha256(PAYLOAD).hexdigest()
BUDGET = {
    "activation_seconds": 10,
    "inference_seconds": 5,
    "total_seconds": 15,
    "queue_seconds": 300,
    "max_tokens": 16,
}
PEER = {
    "uid": 1000,
    "pid": 123,
    "start_ticks": 55,
    "boot_id": BOOT,
    "unit": "swapp-aos-gpu-fixture.service",
    "invocation_id": "e" * 32,
    "control_group": "/fixture/aos",
    "parent_pid": 122,
    "parent_start_ticks": 54,
}
TARGET = {
    "request_id": REQUEST,
    "request_sha256": REQUEST_HASH,
    "original_peer_generation_sha256": digest(PEER),
}


class FixtureResolver:
    def resolve(self, owner):
        return PrincipalReceipt(
            owner,
            ProcessIdentity(123, 55, BOOT),
            PEER["unit"] if owner == "aos" else "swapp-lab-gpu-fixture.service",
            PEER["invocation_id"],
        )

    def verify(self, receipt):
        return receipt == self.resolve(receipt.owner)


class Rig:
    def __init__(self, tmp_path, monkeypatch):
        self.now = 100.0
        self.boot = BOOT
        self.admission = grant_for(PEER, PROFILE, DEPLOYMENT, CONFIG, SCHEMA)
        self.prove_drain = True
        self.database = tmp_path / "arbiter.sqlite3"
        monkeypatch.setattr("lab.llm.gpu_scheduler._boot_id", lambda: self.boot)
        monkeypatch.setattr("lab.llm.gpu_scheduler._process_identity_alive", lambda _identity: True)
        self.store = ControlStore(
            self.database,
            clock=lambda: self.now,
            boot_id=lambda: self.boot,
            peer_verifier=lambda peer: peer == PEER,
        )
        self.scheduler = SharedGpuScheduler(
            self.database,
            principal_resolver=FixtureResolver(),
            drain_verifier=self.drain,
            max_activation_seconds=10,
            max_inference_seconds=5,
            max_total_seconds=15,
            queue_timeout_seconds=300,
            clock=lambda: self.now,
            control_store=self.store,
        )

    def register(self, deadline=200.0):
        return self.store.register_intent(
            PEER,
            REQUEST,
            REQUEST_HASH,
            PROFILE,
            DEPLOYMENT,
            CONFIG,
            SCHEMA,
            deadline,
            BUDGET,
            admission=self.admission,
        )

    def submit(self):
        return self.scheduler.submit("aos", REQUEST, PAYLOAD)

    def status(self):
        return self.store.lookup(PEER, TARGET, PROFILE, DEPLOYMENT)

    def cancel(self):
        return self.store.cancel(
            PEER,
            TARGET,
            PROFILE,
            DEPLOYMENT,
            profile_config_sha256=CONFIG,
            response_schema_sha256=SCHEMA,
            budget=BUDGET,
        )

    def acquire(self):
        self.register()
        self.submit()
        lease = self.scheduler.try_acquire("aos", REQUEST)
        assert lease is not None
        return lease

    def drain(self, lease):
        if self.prove_drain:
            self.store.record_drain(
                asdict(lease),
                {
                    "fixture_only": True,
                    "child_generation": None,
                    "late_start_fenced": True,
                    "cgroup_empty": True,
                    "gpu_absent": True,
                    "no_child_intent": True,
                },
            )
        return True


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


def test_unknown_does_not_prove_no_admission(rig):
    status = rig.status()
    assert status["state"] == "unknown"
    assert status["terminal_receipt"] is None
    assert status["cancel_requested"] is False


def test_cancel_before_intent_is_durable_and_prevents_late_submit(rig):
    receipt = rig.cancel()["terminal_receipt"]
    assert receipt["release_outcome"] == "never_admitted"
    assert receipt["allocation_binding_sha256"] is None
    assert receipt["original_budget"]["envelope_deadline"] is None
    assert receipt["profile_config_sha256"] == CONFIG
    assert receipt["no_admission_evidence_sha256"] is not None
    with pytest.raises(ControlCanceled):
        rig.register()
    with pytest.raises(ControlCanceled):
        rig.submit()
    reopened = ControlStore(rig.database, clock=lambda: rig.now, boot_id=lambda: rig.boot)
    assert reopened.lookup(PEER, TARGET, PROFILE, DEPLOYMENT)["terminal_receipt"] == receipt


def test_reconnect_preserves_original_deadline_and_queue_budget(rig):
    assert rig.register(130.0) == 130.0
    rig.now = 101.0
    assert rig.register(300.0) == 130.0
    ticket = rig.submit()
    assert ticket.queue_deadline == 130.0
    rig.now = 102.0
    assert rig.submit().queue_deadline == 130.0
    with pytest.raises(ControlStoreError, match="request_conflict"):
        rig.store.register_intent(
            PEER,
            REQUEST,
            REQUEST_HASH,
            PROFILE,
            DEPLOYMENT,
            CONFIG,
            SCHEMA,
            300.0,
            {**BUDGET, "max_tokens": 32},
            admission=rig.admission,
        )


def test_first_cancel_is_immutable_and_queued_never_allocated(rig):
    rig.register()
    rig.submit()
    first = rig.cancel()
    rig.now += 1
    assert rig.cancel() == first
    assert rig.scheduler.try_acquire("aos", REQUEST) is None
    assert first["state"] == "canceled"
    assert first["terminal_receipt"]["release_outcome"] == "never_admitted"


def test_cancel_acquire_race_has_no_false_no_admission_receipt(rig):
    rig.register()
    rig.submit()
    barrier = Barrier(2)

    def acquire():
        barrier.wait()
        return rig.scheduler.try_acquire("aos", REQUEST)

    def cancel():
        barrier.wait()
        return rig.cancel()

    with ThreadPoolExecutor(max_workers=2) as pool:
        acquired = pool.submit(acquire)
        canceled = pool.submit(cancel)
        lease, response = acquired.result(), canceled.result()
    if lease is None:
        assert response["terminal_receipt"]["release_outcome"] == "never_admitted"
    else:
        assert response["state"] == "cancel_pending"
        assert response["terminal_receipt"] is None
        rig.scheduler.release(lease)
        assert rig.status()["terminal_receipt"]["release_outcome"] == "released"


def test_allocated_cancel_ack_keeps_fence_until_trusted_drain(rig):
    lease = rig.acquire()
    response = rig.cancel()
    assert response["terminal_receipt"] is None
    assert response["state"] == "cancel_pending"
    with pytest.raises(ControlCanceled):
        rig.store.check_running(asdict(lease))
    rig.prove_drain = False
    with pytest.raises(ControlStoreError, match="internal_unavailable"):
        rig.scheduler.release(lease)
    with closing(rig.store._connect()) as connection:
        row = connection.execute("SELECT active_owner,active_token FROM gpu_turn_state").fetchone()
        assert tuple(row) == ("aos", lease.fencing_token)
    assert rig.status()["terminal_receipt"] is None
    rig.prove_drain = True
    rig.scheduler.release(lease)
    receipt = rig.status()["terminal_receipt"]
    assert receipt["terminal_state"] == "canceled"
    assert receipt["original_principal"] == PEER
    assert receipt["drain_evidence_sha256"] is not None
    assert receipt["receipt_sha256"] == digest(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )


def test_cancel_prevents_plan_and_go_and_start_is_not_replayed(rig):
    lease = rig.acquire()
    with closing(rig.store._connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        rig.store.launch_handoff(connection, asdict(lease), "plan")
        connection.execute("""CREATE TABLE aos_gpu_child_bindings (
            owner TEXT, request_id TEXT, fencing_token INTEGER, profile_id TEXT,
            deployment_digest TEXT, unit TEXT, nonce TEXT, created_boottime REAL,
            total_seconds INTEGER, invocation_id TEXT, main_pid INTEGER,
            main_start_ticks INTEGER, boot_id TEXT, control_group TEXT)""")
        connection.execute(
            "INSERT INTO aos_gpu_child_bindings(owner,request_id,fencing_token,profile_id,"
            "deployment_digest,unit,nonce,created_boottime,total_seconds) VALUES(?,?,?,?,?,?,?,?,"
            "?)",
            (
                "aos",
                REQUEST,
                lease.fencing_token,
                PROFILE,
                DEPLOYMENT,
                "fixture-child.service",
                "f" * 64,
                100.0,
                15,
            ),
        )
        rig.store.launch_handoff(connection, asdict(lease), "start")
        connection.commit()
    with closing(rig.store._connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(ControlStoreError, match="request_conflict"):
            rig.store.launch_handoff(connection, asdict(lease), "start")
        connection.rollback()
    rig.cancel()
    with closing(rig.store._connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(ControlCanceled):
            rig.store.launch_handoff(connection, asdict(lease), "go")
        connection.rollback()
    with pytest.raises(ControlStoreError, match="internal_unavailable"):
        rig.drain(lease)  # The intent gap cannot be asserted to mean no child.
    rig.now = 146.0  # Even after created + total + drain, launch issuer may resume.
    assert rig.store.requires_bound_child(asdict(lease))
    with pytest.raises(ControlStoreError, match="internal_unavailable"):
        rig.store.record_drain(
            asdict(lease),
            {
                "fixture_only": True,
                "child_generation": None,
                "late_start_fenced": True,
                "cgroup_empty": True,
                "gpu_absent": True,
                "never_started": True,
                "child_intent": {
                    "unit": "fixture-child.service",
                    "nonce": "f" * 64,
                    "created_boottime": 100.0,
                    "total_seconds": 15,
                },
            },
        )
    # An old time-only proof already persisted by r1/r2 cannot bypass the new
    # finish guard through either normal release or quarantined recovery.
    with closing(rig.store._connect()) as connection:
        allocation_hash = connection.execute(
            "SELECT allocation_sha256 FROM aos_control_requests WHERE request_id=?",
            (REQUEST,),
        ).fetchone()[0]
        connection.execute(
            "UPDATE aos_control_requests SET drain_json=? WHERE request_id=?",
            (
                canonical(
                    {
                        "child_generation": None,
                        "handoff_stage": "start",
                        "allocation_binding_sha256": allocation_hash,
                    }
                ),
                REQUEST,
            ),
        )
    rig.prove_drain = False
    with pytest.raises(ControlStoreError, match="internal_unavailable"):
        rig.scheduler.release(lease)
    with pytest.raises(ControlStoreError, match="internal_unavailable"):
        rig.scheduler.recover_controlled_turn()
    assert rig.status()["state"] == "quarantined"
    assert rig.status()["terminal_receipt"] is None
    with closing(rig.store._connect()) as connection:
        active = connection.execute(
            "SELECT active_request_id,active_token FROM gpu_turn_state"
        ).fetchone()
        assert tuple(active) == (REQUEST, lease.fencing_token)


def test_result_cancel_order_decides_success_at_release_commit(rig):
    lease = rig.acquire()
    rig.store.record_result(REQUEST, {"text": "fixture"}, {"tokens": 1}, {})
    assert rig.status()["state"] == "result_ready"
    assert rig.status()["result"] is None
    rig.cancel()
    rig.scheduler.release(lease)
    assert rig.status()["state"] == "canceled"
    assert rig.status()["result"] is None
    assert rig.status()["terminal_receipt"]["result_sha256"] is None


def test_completed_commit_wins_later_cancel_and_survives_lost_ack(rig):
    lease = rig.acquire()
    rig.store.record_result(REQUEST, {"text": "fixture"}, {"tokens": 1}, {})
    rig.scheduler.release(lease)
    completed = rig.status()
    assert completed["state"] == "completed"
    assert completed["result"]["response"] == {"text": "fixture"}
    assert rig.cancel() == completed
    reopened = ControlStore(rig.database, clock=lambda: rig.now, boot_id=lambda: rig.boot)
    assert reopened.lookup(PEER, TARGET, PROFILE, DEPLOYMENT) == completed


def test_failed_release_cas_does_not_create_terminal_receipt(rig):
    lease = rig.acquire()
    rig.drain(lease)
    with pytest.raises(LeaseConflict):
        rig.scheduler.release(replace(lease, fencing_token=lease.fencing_token + 1))
    assert rig.status()["terminal_receipt"] is None


def test_restart_recovery_drains_cancel_without_new_allocation(rig):
    lease = rig.acquire()
    rig.cancel()
    assert rig.scheduler.recover_controlled_turn()
    receipt = rig.status()["terminal_receipt"]
    assert receipt["release_outcome"] == "recovered_released"
    assert receipt["terminal_state"] == "canceled"
    with closing(rig.store._connect()) as connection:
        row = connection.execute(
            "SELECT allocation_json FROM aos_control_requests WHERE request_id=?", (REQUEST,)
        ).fetchone()
        assert str(lease.fencing_token) in row[0]
    assert not rig.scheduler.recover_controlled_turn()


def test_expired_queue_has_receipt_and_boot_change_never_renews_budget(rig):
    rig.register(105.0)
    rig.submit()
    rig.now = 106.0
    assert rig.scheduler.try_acquire("aos", REQUEST) is None
    assert rig.status()["terminal_receipt"]["reason_code"] == "queue_timeout"
    rig.boot = "other-boot"
    with pytest.raises(ControlStoreError, match="stale_generation"):
        rig.register(200.0)


def test_control_id_and_full_original_generation_are_immutable(rig):
    rig.register()
    rig.store.remember_control(PEER, "1" * 32, "2" * 64)
    rig.store.remember_control(PEER, "1" * 32, "2" * 64)
    with pytest.raises(ControlStoreError, match="request_conflict"):
        rig.store.remember_control(PEER, "1" * 32, "3" * 64)
    with pytest.raises(ControlStoreError, match="request_conflict"):
        rig.store.lookup(PEER, {**TARGET, "request_sha256": "4" * 64}, PROFILE, DEPLOYMENT)
    with pytest.raises(ControlStoreError, match="history_denied"):
        rig.store.lookup(
            PEER, {**TARGET, "original_peer_generation_sha256": "4" * 64}, PROFILE, DEPLOYMENT
        )


def test_explicit_history_projection_has_no_principal_output_or_mutation(rig):
    rig.cancel()
    new_peer = {**PEER, "pid": 456, "start_ticks": 99, "invocation_id": "9" * 32}
    # Internal trusted API: service has already checked the explicit history ACL.
    reader = ControlStore(
        rig.database,
        clock=lambda: rig.now,
        boot_id=lambda: rig.boot,
        peer_verifier=lambda peer: peer == new_peer,
    )
    projection = reader.lookup(new_peer, TARGET, PROFILE, DEPLOYMENT, history=True)
    assert set(projection) == {
        "target",
        "state",
        "original_peer_generation_sha256",
        "terminal_receipt_sha256",
        "release_outcome",
    }
    with pytest.raises(ControlStoreError, match="history_denied"):
        reader.cancel(new_peer, TARGET, PROFILE, DEPLOYMENT, history=True)


def test_control_budget_expiry_is_checked_before_database_work(rig):
    token = control_deadline.set(0.0)
    try:
        with pytest.raises(ControlStoreError, match="deadline_exceeded"):
            rig.status()
    finally:
        control_deadline.reset(token)


def test_recorded_no_child_proof_blocks_later_plan(rig):
    lease = rig.acquire()
    rig.drain(lease)
    with closing(rig.store._connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(ControlStoreError, match="request_conflict"):
            rig.store.launch_handoff(connection, asdict(lease), "plan")
        connection.rollback()
    with pytest.raises(ControlStoreError, match="request_conflict"):
        rig.store.check_running(asdict(lease))


def test_quarantine_blocks_launch_even_without_cancel_flag(rig):
    lease = rig.acquire()
    with closing(rig.store._connect()) as connection:
        connection.execute("UPDATE gpu_turn_state SET phase='quarantined'")
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(ControlStoreError, match="request_conflict"):
            rig.store.launch_handoff(connection, asdict(lease), "plan")
        connection.rollback()


def test_terminal_and_allocator_release_rollback_together(rig, monkeypatch):
    lease = rig.acquire()
    original_finish = rig.scheduler._finish

    def crash_before_commit(connection, current, timestamp, **kwargs):
        original_finish(connection, current, timestamp, **kwargs)
        raise RuntimeError("fixture crash before SQLite commit")

    monkeypatch.setattr(rig.scheduler, "_finish", crash_before_commit)
    with pytest.raises(RuntimeError, match="fixture crash"):
        rig.scheduler.release(lease)
    assert rig.status()["terminal_receipt"] is None
    with closing(rig.store._connect()) as connection:
        assert (
            connection.execute("SELECT active_request_id FROM gpu_turn_state").fetchone()[0]
            == REQUEST
        )


def test_final_inference_deadline_is_retained_after_live_fields_clear(rig):
    lease = rig.acquire()
    rig.now = 101.0
    ready = rig.scheduler.mark_ready(lease)
    assert ready is not None
    assert ready.inference_deadline == 106.0
    rig.store.record_result(REQUEST, {"text": "fixture"}, {"tokens": 1}, {})
    rig.scheduler.release(ready)
    budget = rig.status()["terminal_receipt"]["original_budget"]
    assert budget["activation_deadline"] == 110.0
    assert budget["inference_deadline"] == 106.0
    assert budget["total_deadline"] == 115.0


def test_record_capacity_preserves_existing_target_cancel_and_lookup(rig, monkeypatch):
    rig.register()
    monkeypatch.setattr("lab.llm.aos_gpu_control_store.MAX_REQUESTS", 0)
    assert rig.cancel()["state"] == "canceled"
    assert rig.status()["state"] == "canceled"
    with pytest.raises(ControlStoreError, match="busy"):
        rig.store.register_intent(
            PEER,
            "9" * 32,
            REQUEST_HASH,
            PROFILE,
            DEPLOYMENT,
            CONFIG,
            SCHEMA,
            200.0,
            BUDGET,
            admission=rig.admission,
        )


def test_control_id_capacity_preserves_exact_retries(rig, monkeypatch):
    monkeypatch.setattr("lab.llm.aos_gpu_control_store.MAX_CONTROL_IDS", 1)
    rig.store.remember_control(PEER, "1" * 32, "2" * 64)
    rig.store.remember_control(PEER, "1" * 32, "2" * 64)
    with pytest.raises(ControlStoreError, match="busy"):
        rig.store.remember_control(PEER, "3" * 32, "2" * 64)


@pytest.mark.parametrize("queued", [False, True])
def test_restart_scan_closes_expired_unallocated_intent_or_queue(rig, queued):
    rig.register(105.0)
    if queued:
        rig.submit()
    rig.now = 106.0
    assert rig.scheduler.recover_controlled_turn()
    receipt = rig.status()["terminal_receipt"]
    assert receipt["terminal_state"] == "expired"
    assert receipt["release_outcome"] == "never_admitted"
    assert receipt["original_principal"] == PEER
    assert receipt["original_budget"]["envelope_deadline"] == 105.0
    assert receipt["allocation_binding_sha256"] is None
    assert receipt["child_generation"] is None
    with closing(rig.store._connect()) as connection:
        assert connection.execute(
            "SELECT active_owner,active_token FROM gpu_turn_state"
        ).fetchone()[:] == (None, 0)
    with pytest.raises(ControlCanceled):
        rig.submit()
    assert not rig.scheduler.recover_controlled_turn()


@pytest.mark.parametrize("cause", ["stale_peer", "changed_boot"])
def test_restart_scan_uses_original_generation_without_new_caller_adoption(rig, cause):
    rig.register()
    rig.submit()
    if cause == "stale_peer":
        rig.store._peer_verifier = lambda _peer: False
    else:
        rig.boot = "new-boot"
    assert rig.scheduler.recover_controlled_turn()
    reader = ControlStore(rig.database, clock=lambda: rig.now, boot_id=lambda: rig.boot)
    receipt = reader.lookup(PEER, TARGET, PROFILE, DEPLOYMENT)["terminal_receipt"]
    assert receipt["reason_code"] == "generation_lost"
    assert receipt["release_outcome"] == "never_admitted"
    assert receipt["original_principal"] == PEER
    assert receipt["original_budget"]["envelope_deadline"] == 200.0


def test_orphan_scan_is_bounded_and_no_admission_refuses_launch_evidence(rig):
    for number in range(9):
        rig.store.register_intent(
            PEER,
            f"{number:032x}",
            REQUEST_HASH,
            PROFILE,
            DEPLOYMENT,
            CONFIG,
            SCHEMA,
            105.0,
            BUDGET,
            admission=rig.admission,
        )
    rig.now = 106.0
    assert rig.scheduler.recover_controlled_turn()
    with closing(rig.store._connect()) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM aos_control_requests WHERE receipt_json IS NOT NULL"
            ).fetchone()[0]
            == 8
        )
    assert rig.scheduler.recover_controlled_turn()
    with closing(rig.store._connect()) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM aos_control_requests WHERE receipt_json IS NOT NULL"
            ).fetchone()[0]
            == 9
        )
    rig.now = 100.0
    rig.register()
    with closing(rig.store._connect()) as connection:
        connection.execute(
            "UPDATE aos_control_requests SET handoff_stage='start' WHERE request_id=?", (REQUEST,)
        )
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(ControlStoreError, match="internal_unavailable"):
            rig.store.no_admission(connection, REQUEST, "expired", "generation_lost")
        connection.rollback()
    assert rig.status()["terminal_receipt"] is None
