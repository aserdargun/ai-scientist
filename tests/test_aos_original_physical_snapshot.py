"""CPU fixtures for original bindings; these do not observe any physical GPU."""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import asdict

import pytest
from test_aos_terminal_publication import _released_result_ready, publication  # noqa: F401

from lab.llm.aos_gpu_control_store import ControlAuthority, ControlStoreError, canonical, digest
from lab.llm.aos_gpu_executor import authenticated_peer


def _read(p, *, authority=None):
    return p.store.read_original_physical_snapshot(
        asdict(p.peer),
        p.target,
        p.profile.profile_id,
        p.profile.deployment_digest,
        authority=authority or ControlAuthority(p.admission.binding_json, None, lambda: None),
    )


def _rows(p):
    with closing(p.store._connect()) as connection:
        return list(connection.iterdump())


def _execute(p, sql, parameters=()):
    with closing(p.store._connect()) as connection:
        connection.execute(sql, parameters)


@pytest.fixture
def released(publication, monkeypatch):  # noqa: F811
    p = publication
    # The shared fixture replaces this with b'fixture'; use the real wire
    # canonicalization here so a full-frame/payload hash confusion is detected.
    monkeypatch.setattr(
        "lab.llm.aos_gpu_executor._profile_payload",
        lambda _profile, payload: canonical(payload).encode("utf-8"),
    )
    _released_result_ready(p)
    with closing(p.store._connect()) as connection:
        row = connection.execute("SELECT * FROM aos_control_requests").fetchone()
        child = connection.execute("SELECT * FROM aos_gpu_child_bindings").fetchone()
        proof = json.loads(row["drain_json"])
        proof.pop("fixture_only")
        proof.update(
            {
                "kind": "physical_drain",
                "boot_id": p.peer.boot_id,
                "observed_boottime": 101.0,
                "never_started": False,
                "observed_gpu_pids": [p.result.main_pid],
                "remaining_owned_gpu_pids": [],
                "remaining_foreign_gpu_pids": [],
                "late_start_fence": child["created_boottime"] + child["total_seconds"] + 30,
                "final_unit_state": "inactive",
                "final_main_pid": 0,
            }
        )
        receipt = json.loads(row["receipt_json"])
        receipt["drain_evidence_sha256"] = digest(proof)
        receipt["receipt_sha256"] = digest(
            {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        )
        connection.execute(
            "UPDATE aos_control_requests SET drain_json=?,receipt_json=?",
            (canonical(proof), canonical(receipt)),
        )
        connection.execute(
            "UPDATE aos_gpu_child_bindings SET observed_gpu_pids_json=?",
            (canonical([p.result.main_pid]),),
        )
    return p


@pytest.mark.parametrize("queued", [False, True])
def test_never_admitted_original_snapshot(publication, queued):  # noqa: F811
    p = publication
    p.register()
    if queued:
        with authenticated_peer(p.peer):
            p.executor.scheduler.submit(
                "aos",
                p.request_id,
                p.frame,
                activation_seconds=10,
                inference_seconds=5,
                total_seconds=15,
                queue_timeout_seconds=60,
            )
    p.cancel()
    before = _rows(p)
    snapshot = _read(p)
    assert snapshot["child"] is None
    assert snapshot["handoff_stage"] is None
    assert (snapshot["ticket"] is not None) is queued
    assert snapshot["original_budget"]["envelope_deadline"] == 160.0
    assert (
        json.loads(snapshot["evidence"]["terminal_canonical"])["release_outcome"]
        == "never_admitted"
    )
    assert _rows(p) == before


def test_released_snapshot_is_one_read_transaction_and_has_no_payload(released, monkeypatch):
    p = released
    before = _rows(p)
    calls = []
    statements = []
    original_connect = p.store._connect

    def connect():
        connection = original_connect()
        calls.append(connection)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(p.store, "_connect", connect)
    snapshot = _read(p)
    assert len(calls) == 1
    assert statements.count("BEGIN") == 1
    assert "PRAGMA query_only=ON" in statements
    assert not any(sql.startswith(("UPDATE", "INSERT", "DELETE", "CREATE")) for sql in statements)
    assert snapshot["schema"] == "aos-scientist-physical-snapshot.v1"
    assert snapshot["child"]["observed_gpu_pids"] == [p.result.main_pid]
    assert snapshot["child"]["request_sha256"] == digest(p.payload)
    assert snapshot["ticket"]["payload_sha256"] == p.request_hash
    assert snapshot["handoff_stage"] == "go"
    assert "workdir" not in snapshot["child"]
    assert "synthetic state" not in canonical(snapshot)
    assert _rows(p) == before


@pytest.mark.parametrize("owner", ["lab", "aos"])
def test_other_active_job_requires_newer_fence(released, owner):
    p = released
    _execute(
        p,
        "UPDATE gpu_turn_state SET active_owner=?,active_request_id=?,"
        "active_token=active_token+1,phase='inference'",
        (owner, "7" * 32),
    )
    assert _read(p)["arbiter"]["active_owner"] == owner
    _execute(p, "UPDATE gpu_turn_state SET active_token=active_token-1")
    with pytest.raises(ControlStoreError):
        _read(p)


@pytest.mark.parametrize(
    "sql,parameters",
    [
        ("UPDATE gpu_turn_state SET active_token=0", ()),
        ("UPDATE gpu_turn_requests SET payload_sha256=?", ("0" * 64,)),
        ("UPDATE gpu_turn_requests SET owner_start_ticks=owner_start_ticks+1", ()),
        ("UPDATE gpu_turn_requests SET owner_invocation_id=?", ("0" * 32,)),
        ("UPDATE gpu_turn_requests SET activation_seconds=activation_seconds+1", ()),
        ("UPDATE gpu_turn_requests SET inference_seconds=inference_seconds+1", ()),
        ("UPDATE gpu_turn_requests SET total_seconds=total_seconds+1", ()),
        ("UPDATE gpu_turn_requests SET queue_deadline=queue_deadline+1", ()),
        ("UPDATE gpu_turn_requests SET state='active'", ()),
        ("DELETE FROM gpu_turn_requests", ()),
        ("UPDATE aos_gpu_child_bindings SET fencing_token=fencing_token+1", ()),
        ("UPDATE aos_gpu_child_bindings SET main_start_ticks=main_start_ticks+1", ()),
        ("UPDATE aos_gpu_child_bindings SET request_sha256=?", ("0" * 64,)),
        ("UPDATE aos_gpu_child_bindings SET observed_gpu_pids_json='[]'", ()),
        ("UPDATE aos_gpu_child_bindings SET nonce=?", ("0" * 64,)),
        ("UPDATE aos_gpu_output_bindings SET request_json='{}'", ()),
        ("UPDATE aos_gpu_output_bindings SET principal_json='{}'", ()),
        ("UPDATE aos_gpu_output_bindings SET profile_config_sha256=?", ("0" * 64,)),
        ("UPDATE aos_control_requests SET handoff_stage='plan'", ()),
        ("UPDATE aos_control_requests SET state='quarantined'", ()),
    ],
)
def test_mismatched_original_rows_fail_closed(released, sql, parameters):
    _execute(released, sql, parameters)
    before = _rows(released)
    with pytest.raises(ControlStoreError):
        _read(released)
    assert _rows(released) == before


@pytest.mark.parametrize("phase", ["inference", "quarantined"])
def test_original_still_active_is_rejected(released, phase):
    _execute(
        released,
        "UPDATE gpu_turn_state SET active_owner='aos',active_request_id=?,phase=?",
        (released.request_id, phase),
    )
    with pytest.raises(ControlStoreError):
        _read(released)


@pytest.mark.parametrize(
    "table",
    [
        "aos_control_requests",
        "gpu_turn_requests",
        "gpu_turn_state",
        "aos_gpu_child_bindings",
        "aos_gpu_output_bindings",
    ],
)
def test_missing_schema_is_not_created(released, table):
    _execute(released, f"DROP TABLE {table}")
    before = _rows(released)
    with pytest.raises(ControlStoreError):
        _read(released)
    assert _rows(released) == before


def test_authority_rechecked_before_return(released):
    count = 0

    def verify():
        nonlocal count
        count += 1
        if count == 2:
            raise ControlStoreError("unauthorized")

    before = _rows(released)
    with pytest.raises(ControlStoreError, match="unauthorized"):
        _read(released, authority=ControlAuthority(released.admission.binding_json, None, verify))
    assert count == 2
    assert _rows(released) == before


@pytest.mark.parametrize(
    "key,value",
    [
        ("kind", "fixture"),
        ("never_started", True),
        ("late_start_fence", 146.0),
        ("late_start_fenced", 1),
        ("gpu_absent", False),
        ("cgroup_empty", False),
        ("remaining_owned_gpu_pids", [456]),
        ("remaining_foreign_gpu_pids", [999]),
    ],
)
def test_rehashed_but_inconsistent_drain_is_rejected(released, key, value):
    p = released
    with closing(p.store._connect()) as connection:
        row = connection.execute(
            "SELECT drain_json,receipt_json FROM aos_control_requests"
        ).fetchone()
        proof, receipt = json.loads(row[0]), json.loads(row[1])
        proof[key] = value
        receipt["drain_evidence_sha256"] = digest(proof)
        receipt["receipt_sha256"] = digest(
            {name: item for name, item in receipt.items() if name != "receipt_sha256"}
        )
        connection.execute(
            "UPDATE aos_control_requests SET drain_json=?,receipt_json=?",
            (canonical(proof), canonical(receipt)),
        )
    with pytest.raises(ControlStoreError):
        _read(p)
