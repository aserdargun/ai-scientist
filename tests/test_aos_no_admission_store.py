"""CPU fixtures only: UNADMITTED tombstones, never real runtime closure."""

from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from threading import Event

import pytest
from test_aos_control_store import BUDGET, CONFIG, DEPLOYMENT, PEER, PROFILE, SCHEMA, Rig

from lab.llm.aos_gpu_control_store import ControlStoreError, canonical, digest
from lab.llm.aos_no_admission_observation import ObservationError, schema_hash
from lab.llm.aos_no_admission_store import (
    REQUEST_SURFACES,
    TABLE,
    NoAdmissionObservationStore,
    ObserverAuthority,
    PhysicalEvidence,
    assert_not_observed,
)
from lab.llm.native_runtime import OwnedVllmRuntime


def encoded(value):
    return canonical(value).encode()


class ObservationRig:
    def __init__(self, tmp_path, monkeypatch):
        self.base = Rig(tmp_path, monkeypatch)
        self.control = self.base.store
        self.calls = {"observer": 0, "original": 0, "physical": 0}
        self.hooks = {}
        self.monotonic = time.monotonic
        # Isolated provenance query fixtures. No process/executor is created.
        with closing(self.control._connect()) as connection:
            for table in (
                "aos_gpu_child_bindings",
                "aos_gpu_output_bindings",
                "aos_gpu_turn_results",
            ):
                connection.execute(f"CREATE TABLE {table} (request_id TEXT PRIMARY KEY)")
        # Use production DDL, without constructing a runtime, scheduler or GPU
        # observer. This is the tenth table in the real shared canonical DB.
        runtime = OwnedVllmRuntime.__new__(OwnedVllmRuntime)
        runtime._database = self.control.database
        runtime._initialize_bindings()
        binding = json.loads(self.base.admission.binding_json)
        request = {
            "version": 1,
            "op": "infer",
            "request_id": "a" * 32,
            "profile_id": PROFILE,
            "deployment_digest": DEPLOYMENT,
            "payload": {"synthetic": True},
        }
        self.target = {
            "request_id": request["request_id"],
            "request_sha256": digest(request),
            "original_caller_generation_sha256": digest(PEER),
        }
        intent = {
            "session_id": "fixture-session",
            "runtime_id": "fixture-runtime",
            "lease_id": "fixture-lease",
            "generation": 0,
            "owner": "AGENT",
            "authorization_context_sha256": "1" * 64,
        }
        capture = {
            "admission_binding": binding,
            "capability_sha256": "2" * 64,
            "capability_freshness": {
                "boot_id": self.base.boot,
                "issued_boottime": 10,
                "expires_boottime": 30,
            },
        }
        record = {
            **capture,
            "schema_version": "2.0",
            "request_id": request["request_id"],
            "request_sha256": digest(request),
            "session_id": intent["session_id"],
            "intent_binding_sha256": digest(intent),
            "admission_binding_sha256": digest(binding),
            "captured_boottime": 20,
        }
        original_store = {"path_sha256": "3" * 64, "device": 1, "inode": 2, "uid": 1000}
        self.original = {
            "request_canonical": canonical(request),
            "admission_record": record,
            "admission_record_sha256": digest(record),
            "admission_binding_sha256": digest(binding),
            "admission_capture_sha256": digest(capture),
            "intent_binding": intent,
            "intent_binding_sha256": digest(intent),
            "broker_generation": binding["server_generation"],
            "broker_generation_sha256": digest(binding["server_generation"]),
            "store": original_store,
        }
        generation = {
            **binding["server_generation"],
            "pid": 500,
            "start_ticks": 400,
            "unit": "fixture-observer.service",
            "invocation_id": "7" * 32,
            "control_group": "/fixture/observer",
        }
        capability = {
            "feature": "no-admission-observation.v1",
            "version": 1,
            "schema_sha256": schema_hash(),
            "generation_sha256": digest(generation),
            "source_sha256": "5" * 64,
            "config_sha256": "6" * 64,
            "purpose": "observe_no_admission",
        }
        self.observer = {
            "generation": generation,
            "generation_sha256": digest(generation),
            "source_sha256": capability["source_sha256"],
            "config_sha256": capability["config_sha256"],
            "capability": capability,
            "capability_sha256": digest(capability),
            "purpose": "observe_no_admission",
        }
        self.cleanup = {
            "store": original_store,
            "session_id": intent["session_id"],
            "runtime_id": intent["runtime_id"],
            "lease_id": "current-cleanup-lease",
            "original_generation": 0,
            "current_generation": 1,
            "current_status": "stopped",
            "current_owner": "PAUSED",
            "authorization_context_sha256": "8" * 64,
            "purpose": "observe_no_admission",
        }
        self.physical = {
            "target": self.target,
            "caller_generation": PEER,
            "caller_generation_sha256": digest(PEER),
            "broker_generation": binding["server_generation"],
            "broker_generation_sha256": digest(binding["server_generation"]),
            "children": [],
            "caller_absent": True,
            "broker_absent": True,
            "original_cgroups_absent": True,
            "children_absent": True,
            "provenance_complete": True,
            "late_dispatch_fenced": True,
        }
        self.physical["proof_sha256"] = digest(self.physical)
        self.dispatch = {
            "target": self.target,
            "source_fingerprints": binding["source_fingerprints"],
            "caller_generation_sha256": digest(PEER),
            "broker_generation_sha256": digest(binding["server_generation"]),
            "provenance_complete": True,
            "deferred_execution": [],
            "quarantined_allocations": [],
        }
        self.store = self.reopen()

    def reopen(self, **overrides):
        options = {
            "observer_authority": self.authority,
            "original_reader": self.original_reader,
            "physical_reader": self.physical_reader,
            "monotonic": lambda: self.monotonic(),
        }
        return NoAdmissionObservationStore(self.control, **(options | overrides))

    def run_hook(self, name):
        self.calls[name] += 1
        if name in self.hooks:
            self.hooks[name](self.calls[name])

    def authority(self, target):
        self.run_hook("observer")
        if target != self.target:
            raise ControlStoreError("unauthorized")
        return ObserverAuthority(encoded(self.observer), encoded(self.cleanup))

    def original_reader(self, target):
        self.run_hook("original")
        assert target == self.target
        return encoded(self.original)

    def physical_reader(self, target, original):
        self.run_hook("physical")
        assert target == self.target and original == encoded(self.original)
        return PhysicalEvidence(encoded(self.physical), encoded(self.dispatch))

    def observe(self, **overrides):
        return self.store.observe(
            overrides.get("target", self.target),
            deadline_monotonic=overrides.get("deadline", self.monotonic() + 4),
        )

    def register(self):
        return self.control.register_intent(
            PEER,
            self.target["request_id"],
            self.target["request_sha256"],
            PROFILE,
            DEPLOYMENT,
            CONFIG,
            SCHEMA,
            self.base.now + 60,
            BUDGET,
            admission=self.base.admission,
        )

    def rows(self, table=TABLE):
        with closing(self.control._connect()) as connection:
            return [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]

    def runtime_binding(self, *, owner="aos", state="prepared", token=1, request_id=None):
        with closing(self.control._connect()) as connection:
            connection.execute(
                "INSERT INTO gpu_runtime_bindings(owner,request_id,fencing_token,unit,nonce,"
                "uds_directory,model_sha256,expected_description,created_boottime,"
                "runtime_max_seconds,launch_state) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    owner,
                    request_id or self.target["request_id"],
                    token,
                    f"fixture-{owner}-{token}.service",
                    "fixture-nonce",
                    "/fixture/uds",
                    "1" * 64,
                    "fixture runtime",
                    10,
                    60,
                    state,
                ),
            )


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return ObservationRig(tmp_path, monkeypatch)


def test_only_tombstone_changes_and_retry_preserves_bytes(rig):
    with closing(rig.control._connect()) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert tables - {"sqlite_sequence", TABLE} == {
        "aos_control_requests",
        "aos_control_reservations",
        "aos_cleanup_grants",
        "aos_control_idempotency",
        "gpu_turn_requests",
        "gpu_turn_state",
        "aos_gpu_child_bindings",
        "aos_gpu_output_bindings",
        "aos_gpu_turn_results",
        "gpu_runtime_bindings",
    }
    before = {table: rig.rows(table) for table in REQUEST_SURFACES}
    original_bytes = encoded(rig.original)
    observation = rig.observe()
    assert observation["admission_budget"] is None
    assert observation["outcome"] == "never_received"
    assert len(rig.rows()) == 1
    rig.store = rig.reopen()
    assert rig.observe() == observation
    assert rig.calls == {"observer": 4, "original": 4, "physical": 4}
    assert encoded(rig.original) == original_bytes
    assert {table: rig.rows(table) for table in REQUEST_SURFACES} == before


@pytest.mark.parametrize("provider", ["observer_authority", "original_reader", "physical_reader"])
def test_missing_provider_is_default_deny(rig, provider):
    rig.store = rig.reopen(**{provider: None})
    with pytest.raises(ControlStoreError, match="unauthorized"):
        rig.observe()
    assert not rig.rows()


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("cleanup", "current_owner", "AGENT"),
        ("cleanup", "current_generation", 0),
        ("cleanup", "session_id", "wrong-session"),
        ("cleanup", "runtime_id", "wrong-runtime"),
        ("cleanup", "current_status", "running"),
        ("cleanup", "purpose", "infer"),
        ("original", "admission_capture_sha256", "0" * 64),
        ("original", "admission_binding_sha256", "0" * 64),
        ("observer", "source_sha256", "0" * 64),
        ("observer", "capability_sha256", "0" * 64),
        ("physical", "caller_absent", False),
        ("physical", "original_cgroups_absent", False),
        ("physical", "children_absent", False),
        ("physical", "provenance_complete", False),
        ("physical", "late_dispatch_fenced", False),
        ("physical", "proof_sha256", "0" * 64),
    ],
)
def test_wrong_authority_capture_or_physical_evidence_denies(rig, section, field, value):
    getattr(rig, section)[field] = value
    with pytest.raises(ObservationError):
        rig.observe()
    assert not rig.rows()


@pytest.mark.parametrize(
    "field", ["request_id", "request_sha256", "original_caller_generation_sha256"]
)
def test_wrong_exact_target_denies(rig, field):
    target = {**rig.target, field: "0" * len(rig.target[field])}
    with pytest.raises(ControlStoreError, match="unauthorized"):
        rig.observe(target=target)
    assert not rig.rows()


def test_old_broker_cannot_be_observer_even_with_consistent_hashes(rig):
    rig.observer["generation"] = rig.original["broker_generation"]
    rig.observer["generation_sha256"] = digest(rig.observer["generation"])
    rig.observer["capability"]["generation_sha256"] = rig.observer["generation_sha256"]
    rig.observer["capability_sha256"] = digest(rig.observer["capability"])
    with pytest.raises(ObservationError, match="original process"):
        rig.observe()
    assert not rig.rows()


@pytest.mark.parametrize("table", list(REQUEST_SURFACES))
def test_any_canonical_provenance_denies(rig, table):
    request_id = rig.target["request_id"]
    with closing(rig.control._connect()) as connection:
        if table in {"aos_control_requests", "aos_control_reservations"}:
            rig.register()
            if table == "aos_control_reservations":
                connection.execute("DELETE FROM aos_control_requests")
        elif table == "gpu_turn_requests":
            rig.register()
            rig.base.scheduler.submit("aos", request_id, rig.original["request_canonical"].encode())
            connection.execute("DELETE FROM aos_control_requests")
            connection.execute("DELETE FROM aos_control_reservations")
        elif table == "gpu_turn_state":
            connection.execute(
                "UPDATE gpu_turn_state SET active_owner='aos',active_request_id=?", (request_id,)
            )
        elif table == "aos_cleanup_grants":
            connection.execute("INSERT INTO aos_cleanup_grants VALUES(?,?,'{}')", ("x", request_id))
        elif table == "aos_control_idempotency":
            connection.execute(
                "INSERT INTO aos_control_idempotency(principal_sha256,control_id,"
                "request_sha256,target_request_id) VALUES('x','y','z',?)",
                (request_id,),
            )
        elif table == "gpu_runtime_bindings":
            rig.runtime_binding()
        else:
            connection.execute(f"INSERT INTO {table}(request_id) VALUES(?)", (request_id,))
    with pytest.raises(ControlStoreError, match="request_conflict"):
        rig.observe()
    assert not rig.rows()


@pytest.mark.parametrize("owner", ["aos", "lab"])
@pytest.mark.parametrize("state", ["prepared", "created", "uncertain"])
@pytest.mark.parametrize("token", [1, 97])
def test_runtime_binding_any_owner_state_or_token_denies_without_mutation(rig, owner, state, token):
    rig.runtime_binding(owner=owner, state=state, token=token)
    before = {table: rig.rows(table) for table in (*REQUEST_SURFACES, "gpu_runtime_bindings")}
    with pytest.raises(ControlStoreError, match="request_conflict"):
        rig.observe()
    assert not rig.rows()
    assert {table: rig.rows(table) for table in before} == before


def test_unrelated_runtime_binding_is_preserved_and_does_not_deny_exact_target(rig):
    rig.runtime_binding(owner="lab", state="uncertain", request_id="b" * 32)
    before = rig.rows("gpu_runtime_bindings")
    assert rig.observe()["target"] == rig.target
    assert rig.rows("gpu_runtime_bindings") == before


def test_runtime_binding_inserted_after_first_absence_check_rolls_back_observation(
    rig, monkeypatch
):
    rig.runtime_binding(owner="lab", state="uncertain", request_id="b" * 32)
    before = {table: rig.rows(table) for table in REQUEST_SURFACES}
    check = rig.store._assert_absent
    checks = []

    def insert_after_initial_check(connection, request_id):
        checks.append(request_id)
        check(connection, request_id)
        if len(checks) == 1:
            # Inject into the observation's own write transaction. An external
            # connection cannot cross BEGIN IMMEDIATE; the final check must
            # also reject a dispatch row introduced inside that transaction.
            connection.execute(
                "INSERT INTO gpu_runtime_bindings(owner,request_id,fencing_token,unit,nonce,"
                "uds_directory,model_sha256,expected_description,created_boottime,"
                "runtime_max_seconds,launch_state) "
                "SELECT owner,?,fencing_token,'late-fixture.service',nonce,uds_directory,"
                "model_sha256,expected_description,created_boottime,runtime_max_seconds,"
                "launch_state FROM gpu_runtime_bindings",
                (request_id,),
            )

    monkeypatch.setattr(rig.store, "_assert_absent", insert_after_initial_check)
    with pytest.raises(ControlStoreError, match="request_conflict"):
        rig.observe()
    assert checks == [rig.target["request_id"]] * 2
    assert not rig.rows()
    assert {table: rig.rows(table) for table in REQUEST_SURFACES} == before


@pytest.mark.parametrize("change", ["missing", "missing_runtime", "unknown_deferred"])
def test_incomplete_or_unreviewed_provenance_denies(rig, change):
    with closing(rig.control._connect()) as connection:
        if change == "missing":
            connection.execute("DROP TABLE aos_gpu_child_bindings")
        elif change == "missing_runtime":
            connection.execute("DROP TABLE gpu_runtime_bindings")
        else:
            connection.execute("CREATE TABLE deferred_dispatch(request_id TEXT)")
    with pytest.raises(ControlStoreError, match="incomplete_provenance"):
        rig.observe()
    assert not rig.rows()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("deferred_execution", [{"pending": "unknown-owner"}]),
        ("quarantined_allocations", [{"token": 1}]),
        ("provenance_complete", False),
        ("caller_generation_sha256", "0" * 64),
        ("broker_generation_sha256", "0" * 64),
        ("source_fingerprints", {"aos": "0" * 64, "scientist": "0" * 64}),
    ],
)
def test_independent_dispatch_inventory_must_prove_exact_absence(rig, field, value):
    rig.dispatch[field] = value
    with pytest.raises(ControlStoreError, match="incomplete_provenance"):
        rig.observe()
    assert not rig.rows()


def test_missing_or_replaced_immutability_trigger_is_not_repaired(rig):
    with closing(rig.control._connect()) as connection:
        connection.execute("DROP TRIGGER aos_observations_no_delete")
    with pytest.raises(ControlStoreError, match="incomplete_provenance"):
        rig.reopen()
    with pytest.raises(ControlStoreError, match="incomplete_provenance"):
        rig.observe()
    assert not rig.rows()


def test_admission_fence_rejects_view_impersonating_tombstone_table(rig):
    with closing(rig.control._connect()) as connection:
        connection.execute(f"DROP TABLE {TABLE}")
        connection.execute(f"CREATE VIEW {TABLE} AS SELECT request_id FROM aos_control_requests")
    with pytest.raises(ControlStoreError, match="incomplete_provenance"):
        rig.register()
    assert not rig.rows("aos_control_requests")
    assert not rig.rows("aos_control_reservations")


@pytest.mark.parametrize("reader", ["observer", "original", "physical"])
@pytest.mark.parametrize("call", [1, 2])
def test_callback_failure_or_postinsert_revocation_rolls_back(rig, reader, call):
    def revoke(count):
        if count == call:
            raise ControlStoreError("revoked_or_cleanup_failed")

    rig.hooks[reader] = revoke
    with pytest.raises(ControlStoreError, match="revoked_or_cleanup_failed"):
        rig.observe()
    assert not rig.rows()


@pytest.mark.parametrize("reader", ["observer", "original", "physical"])
def test_changed_retained_evidence_after_insert_rolls_back(rig, reader):
    def change(count):
        if count == 2:
            section = {"observer": rig.cleanup, "original": rig.original, "physical": rig.physical}
            section[reader]["fixture_change"] = True

    rig.hooks[reader] = change
    with pytest.raises(ControlStoreError, match="request_conflict"):
        rig.observe()
    assert not rig.rows()


def test_deadline_before_and_after_insert_denies(rig):
    with pytest.raises(ControlStoreError, match="deadline_exceeded"):
        rig.observe(deadline=time.monotonic() - 1)
    real_now = time.monotonic()
    rig.monotonic = lambda: real_now

    def expire(count):
        if count == 2:
            rig.monotonic = lambda: real_now + 10

    rig.hooks["physical"] = expire
    with pytest.raises(ControlStoreError, match="deadline_exceeded"):
        rig.observe(deadline=real_now + 2)
    assert not rig.rows()


def test_same_id_changed_proof_cannot_overwrite(rig):
    first = rig.observe()
    rig.cleanup["authorization_context_sha256"] = "9" * 64
    with pytest.raises(ControlStoreError, match="request_conflict"):
        rig.observe()
    assert json.loads(rig.rows()[0][-1]) == first


def test_stale_retry_cannot_refresh_tombstone(rig):
    first = rig.observe()
    rig.base.now += 61
    with pytest.raises(ObservationError, match="freshness"):
        rig.observe()
    assert json.loads(rig.rows()[0][-1]) == first


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "REPLACE"])
def test_tombstone_is_append_only_even_with_replace(rig, operation):
    rig.observe()
    before = rig.rows()
    with closing(sqlite3.connect(rig.control.database)) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable observation"):
            if operation == "UPDATE":
                connection.execute(f"UPDATE {TABLE} SET proof_sha256='changed'")
            elif operation == "DELETE":
                connection.execute(f"DELETE FROM {TABLE}")
            else:
                connection.execute(f"INSERT OR REPLACE INTO {TABLE} SELECT * FROM {TABLE}")
    assert rig.rows() == before


def test_fence_requires_transaction_and_denies_late_admission(rig):
    with closing(rig.control._connect()) as connection:
        with pytest.raises(ControlStoreError, match="internal_unavailable"):
            assert_not_observed(connection, rig.target["request_id"])
        connection.execute("BEGIN IMMEDIATE")
        assert_not_observed(connection, rig.target["request_id"])
        connection.commit()
    rig.observe()
    with pytest.raises(ControlStoreError, match="request_conflict"):
        rig.register()
    assert not rig.rows("aos_control_requests")
    assert not rig.rows("aos_control_reservations")


@pytest.mark.parametrize("winner", ["observation", "admission"])
def test_admission_and_observation_share_one_serialized_transaction(rig, winner):
    entered, release, loser_started = Event(), Event(), Event()

    def block(_count=None):
        entered.set()
        assert release.wait(3)

    if winner == "observation":
        rig.hooks["observer"] = lambda count: block() if count == 1 else None
        first, second = rig.observe, rig.register
    else:
        rig.base.admission = type(rig.base.admission)(rig.base.admission.binding_json, block)
        first, second = rig.register, rig.observe

    def loser():
        loser_started.set()
        return second()

    with ThreadPoolExecutor(max_workers=2) as pool:
        winning = pool.submit(first)
        assert entered.wait(2)
        losing = pool.submit(loser)
        assert loser_started.wait(2)
        release.set()
        winning.result(timeout=5)
        with pytest.raises(ControlStoreError, match="request_conflict"):
            losing.result(timeout=5)
    assert (len(rig.rows()), len(rig.rows("aos_control_requests"))) == (
        (1, 0) if winner == "observation" else (0, 1)
    )


@pytest.mark.parametrize("after_commit", [False, True])
def test_process_crash_before_or_after_commit_is_atomic(rig, after_commit):
    def child():
        if not after_commit:
            rig.hooks["physical"] = lambda count: os._exit(73) if count == 2 else None
        rig.observe()
        os._exit(73)

    process = multiprocessing.get_context("fork").Process(target=child)
    process.start()
    process.join(timeout=6)
    if process.is_alive():
        process.terminate()
        process.join()
        pytest.fail("isolated crash fixture did not stop")
    assert process.exitcode == 73
    assert len(rig.rows()) == int(after_commit)
    # Recovery repeats the same observation once, without fabricating admission.
    recovered = rig.observe()
    assert len(rig.rows()) == 1
    assert json.loads(rig.rows()[0][-1]) == recovered
    assert not rig.rows("aos_control_requests")


def test_provider_cannot_mutate_requested_target(rig):
    expected = deepcopy(rig.target)
    authority = rig.store._observer_authority

    def mutating_provider(target):
        answer = authority(target)
        target["request_id"] = "0" * 32
        return answer

    rig.store = rig.reopen(observer_authority=mutating_provider)
    assert rig.observe()["target"] == expected
    assert rig.target == expected
