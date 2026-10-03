"""Atomic retained budget/evidence fixtures; no AOS or physical GPU acceptance."""

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict

import pytest
import test_aos_terminal_publication as publication_cases
from test_aos_terminal_evidence import _request

from lab.llm.aos_gpu_control_store import ControlAuthority, ControlStoreError, canonical, digest

retained_fixture = publication_cases.publication


def _read(p, *, peer=None, target=None, authority=None, validator=None):
    return p.store.read_retained_terminal_evidence(
        asdict(p.peer) if peer is None else peer,
        p.target if target is None else target,
        p.profile.profile_id,
        p.profile.deployment_digest,
        authority=authority or ControlAuthority(p.admission.binding_json, None, lambda: None),
        control_request=_request(p),
        evidence_validator=validator,
    )


def _snapshot(p):
    with closing(p.store._connect()) as connection:
        return (
            [tuple(row) for row in connection.execute("SELECT * FROM aos_control_requests")],
            [tuple(row) for row in connection.execute("SELECT * FROM aos_control_reservations")],
            [tuple(row) for row in connection.execute("SELECT * FROM aos_control_idempotency")],
            [tuple(row) for row in connection.execute("SELECT * FROM aos_cleanup_grants")],
            [tuple(row) for row in connection.execute("SELECT * FROM gpu_turn_state")],
        )


@pytest.mark.parametrize("canceled", [False, True])
def test_atomic_export_preserves_preimages_and_exact_retry(retained_fixture, canceled):
    p = retained_fixture
    publication_cases._released_result_ready(p, canceled=canceled)
    calls = []
    result = _read(p, validator=lambda value: calls.append(canonical(value)))
    assert set(result) == {"evidence", "original_budget_witness"}
    assert calls == [canonical(result)]
    evidence, witness = result["evidence"], result["original_budget_witness"]
    terminal = json.loads(evidence["terminal_canonical"])
    assert canonical(terminal["original_budget"]) == witness["budget_canonical"]
    assert digest(terminal["original_budget"]) == witness["budget_sha256"]
    assert (evidence["result_canonical"] is None) == canceled
    assert evidence == p.store.read_terminal_evidence(
        asdict(p.peer),
        p.target,
        p.profile.profile_id,
        p.profile.deployment_digest,
        authority=ControlAuthority(p.admission.binding_json, None, lambda: None),
        control_request=_request(p),
    )
    assert witness == p.store.read_original_budget(
        asdict(p.peer),
        p.target,
        p.profile.profile_id,
        p.profile.deployment_digest,
        authority=ControlAuthority(p.admission.binding_json, None, lambda: None),
    )
    after = _snapshot(p)
    p.store._now = lambda: 10000.0
    assert _read(p) == result
    assert _snapshot(p) == after


def test_validator_observes_one_locked_snapshot_before_id_and_quota_write(retained_fixture):
    p = retained_fixture
    p.register()
    p.cancel()
    before = _snapshot(p)

    def validate(value):
        assert set(value) == {"evidence", "original_budget_witness"}
        assert _snapshot(p) == before
        # A competing writer cannot move retained columns between the two exports.
        with closing(sqlite3.connect(p.store.database, timeout=0)) as connection:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                connection.execute("UPDATE aos_control_requests SET original_deadline=9999")

    result = _read(p, validator=validate)
    assert (
        json.loads(result["original_budget_witness"]["budget_canonical"])["envelope_deadline"]
        == 160.0
    )
    after = _snapshot(p)
    assert after[0] == before[0]
    assert len(after[2]) == len(before[2]) + 1


@pytest.mark.parametrize("failure", ["validator", "final_revoke"])
def test_failed_export_rolls_back_control_id_and_reservation(retained_fixture, failure):
    p = retained_fixture
    publication_cases._released_result_ready(p, canceled=False)
    before = _snapshot(p)
    calls = 0

    def current():
        nonlocal calls
        calls += 1
        if calls == 2 and failure == "final_revoke":
            raise ControlStoreError("unauthorized")

    def validate(value):
        assert set(value) == {"evidence", "original_budget_witness"}
        if failure == "validator":
            raise ValueError("consumer rejected full export")

    with pytest.raises((ControlStoreError, ValueError)):
        _read(
            p,
            authority=ControlAuthority(p.admission.binding_json, None, current),
            validator=validate,
        )
    assert calls == (2 if failure == "final_revoke" else 1)
    assert _snapshot(p) == before
    assert _read(p)["evidence"]["result_canonical"] is not None


@pytest.mark.parametrize(
    "failure", ["principal", "request_id", "request_sha256", "original_peer_generation_sha256"]
)
def test_original_owner_and_target_are_required(retained_fixture, failure):
    p = retained_fixture
    p.register()
    p.cancel()
    before = _snapshot(p)
    kwargs = (
        {"peer": {**asdict(p.peer), "uid": p.peer.uid + 1}}
        if failure == "principal"
        else {"target": {**p.target, failure: "0" * len(p.target[failure])}}
    )
    with pytest.raises(ControlStoreError):
        _read(p, **kwargs)
    assert _snapshot(p) == before


@pytest.mark.parametrize("state", ["intent", "quarantined"])
def test_nonterminal_assignment_cannot_be_exported(retained_fixture, state):
    p = retained_fixture
    p.register()
    if state == "quarantined":
        with closing(p.store._connect()) as connection:
            connection.execute("UPDATE aos_control_requests SET state='quarantined'")
            connection.commit()
    before = _snapshot(p)
    with pytest.raises(ControlStoreError, match="request_conflict"):
        _read(p)
    assert _snapshot(p) == before


def test_rehashed_receipt_cannot_supply_expected_budget(retained_fixture):
    p = retained_fixture
    publication_cases._released_result_ready(p, canceled=False)
    with closing(p.store._connect()) as connection:
        receipt = json.loads(
            connection.execute("SELECT receipt_json FROM aos_control_requests").fetchone()[0]
        )
        receipt["original_budget"]["inference_deadline"] += 1.0
        receipt["receipt_sha256"] = digest(
            {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        )
        connection.execute("UPDATE aos_control_requests SET receipt_json=?", (canonical(receipt),))
        connection.commit()
    before = _snapshot(p)
    with pytest.raises(ControlStoreError, match="request_conflict"):
        _read(p)
    assert _snapshot(p) == before


def test_null_budget_tombstone_is_not_upgraded_into_admitted_witness(retained_fixture):
    p = retained_fixture
    authority = ControlAuthority(p.admission.binding_json, None, lambda: None)
    p.store.cancel(
        asdict(p.peer),
        p.target,
        p.profile.profile_id,
        p.profile.deployment_digest,
        authority=authority,
    )
    before = _snapshot(p)
    with pytest.raises(ControlStoreError, match="request_conflict"):
        _read(p, authority=authority)
    assert _snapshot(p) == before


def test_allocated_release_without_bound_child_stays_unsupported(retained_fixture):
    p = retained_fixture
    publication_cases._released_result_ready(p, canceled=True)
    with closing(p.store._connect()) as connection:
        row = connection.execute(
            "SELECT receipt_json,drain_json FROM aos_control_requests"
        ).fetchone()
        receipt, drain = json.loads(row[0]), json.loads(row[1])
        drain["child_generation"] = None
        receipt["child_generation"] = None
        receipt["drain_evidence_sha256"] = digest(drain)
        receipt["receipt_sha256"] = digest(
            {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        )
        connection.execute(
            "UPDATE aos_control_requests SET receipt_json=?,drain_json=?,child_json='null'",
            (canonical(receipt), canonical(drain)),
        )
        connection.commit()
    before = _snapshot(p)
    with pytest.raises(ControlStoreError):
        _read(p)
    assert _snapshot(p) == before


def test_full_export_bound_without_callback_rolls_back_id_and_quota(retained_fixture):
    p = retained_fixture
    publication_cases._released_result_ready(p, canceled=False)
    authority = ControlAuthority(p.admission.binding_json, None, lambda: None)
    evidence = p.store.read_terminal_evidence(
        asdict(p.peer),
        p.target,
        p.profile.profile_id,
        p.profile.deployment_digest,
        authority=authority,
        control_request={**_request(p), "control_id": "f" * 32},
    )
    witness = p.store.read_original_budget(
        asdict(p.peer),
        p.target,
        p.profile.profile_id,
        p.profile.deployment_digest,
        authority=authority,
    )
    result = json.loads(evidence["result_canonical"])
    terminal = json.loads(evidence["terminal_canonical"])
    # Use an unescaped field to place the serialized combined export exactly
    # one byte over its bound while the legacy evidence container still fits.
    result["response"]["padding"] = ""
    evidence["result_canonical"] = canonical(result)
    combined = {"evidence": evidence, "original_budget_witness": witness}
    padding = 128 * 1024 - len(canonical(combined).encode("utf-8"))
    assert padding > 0
    result["response"]["padding"] = "x" * padding
    terminal["result_sha256"] = digest(result)
    terminal["receipt_sha256"] = digest(
        {key: value for key, value in terminal.items() if key != "receipt_sha256"}
    )
    evidence["result_canonical"] = canonical(result)
    evidence["terminal_canonical"] = canonical(terminal)
    assert len(canonical(evidence).encode("utf-8")) <= 128 * 1024 - 1
    assert len(canonical(combined).encode("utf-8")) == 128 * 1024
    with closing(p.store._connect()) as connection:
        connection.execute(
            "UPDATE aos_control_requests SET result_json=?,receipt_json=?",
            (canonical(result), canonical(terminal)),
        )
        connection.commit()
    before = _snapshot(p)
    with pytest.raises(ControlStoreError, match="internal_unavailable"):
        _read(p)  # No external validator: the store itself must enforce the bound.
    assert _snapshot(p) == before
