"""CPU fixtures only: original proof export grants no physical GPU authority."""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

import pytest
import test_aos_terminal_publication as publication_cases
from test_aos_terminal_publication import _released_result_ready

from lab.llm import aos_control_contract_v2 as contract_v2
from lab.llm.aos_gpu_control_store import ControlAuthority, ControlStoreError, canonical, digest

evidence_fixture = publication_cases.publication


def _request(p):
    return {
        "schema": "aos-scientist-control.v1",
        "version": 1,
        "op": "reconcile",
        "control_id": "d" * 32,
        "expected_capability_sha256": "e" * 64,
        "target": p.target,
        "profile_id": p.profile.profile_id,
        "deployment_digest": p.profile.deployment_digest,
    }


def _read(p, *, authority=None, target=None):
    return p.store.read_terminal_evidence(
        asdict(p.peer),
        p.target if target is None else target,
        p.profile.profile_id,
        p.profile.deployment_digest,
        authority=authority or ControlAuthority(p.admission.binding_json, None, lambda: None),
        control_request=_request(p),
    )


def _count(p):
    with closing(p.store._connect()) as connection:
        return connection.execute("SELECT COUNT(*) FROM aos_control_idempotency").fetchone()[0]


@pytest.mark.parametrize("canceled", [False, True])
def test_original_terminal_preimages_and_exact_retry(evidence_fixture, canceled):
    p = evidence_fixture
    _released_result_ready(p, canceled=canceled)
    before = _count(p)
    evidence = _read(p)
    assert evidence["version"] == 2
    assert evidence["target"] == p.target
    with closing(p.store._connect()) as connection:
        row = connection.execute("SELECT * FROM aos_control_requests").fetchone()
        for exported, stored in (
            ("terminal_canonical", "receipt_json"),
            ("allocation_canonical", "allocation_json"),
            ("drain_canonical", "drain_json"),
        ):
            assert evidence[exported] == row[stored]
        assert evidence["result_canonical"] == (None if canceled else row["result_json"])
    assert evidence["no_admission_canonical"] is None
    assert _count(p) == before + 1
    assert _read(p) == evidence
    assert _count(p) == before + 1


def test_never_admitted_has_only_original_nonallocation_proof(evidence_fixture):
    p = evidence_fixture
    p.register()
    p.cancel()
    evidence = _read(p)
    receipt = json.loads(evidence["terminal_canonical"])
    assert receipt["release_outcome"] == "never_admitted"
    assert evidence["allocation_canonical"] is None
    assert evidence["drain_canonical"] is None
    assert evidence["result_canonical"] is None
    proof = json.loads(evidence["no_admission_canonical"])
    assert digest(proof) == receipt["no_admission_evidence_sha256"]
    directory = Path(__file__).resolve().parents[1] / "lab/llm/contracts/control_v2"
    contract = contract_v2.load_contract(
        directory, digest(json.loads((directory / "bundle.json").read_text()))
    )
    assert (
        contract_v2.validate_terminal_evidence(
            canonical(evidence).encode(), contract, expected_target=p.target
        )
        == evidence
    )


def test_inflight_is_not_cleanup_proof_and_spends_no_control_id(evidence_fixture):
    p = evidence_fixture
    p.register()
    before = _count(p)
    with pytest.raises(ControlStoreError, match="request_conflict"):
        _read(p)
    assert _count(p) == before


@pytest.mark.parametrize("key", ["request_id", "request_sha256", "original_peer_generation_sha256"])
def test_wrong_original_target_is_rejected(evidence_fixture, key):
    p = evidence_fixture
    _released_result_ready(p, canceled=False)
    before = _count(p)
    with pytest.raises(ControlStoreError):
        _read(p, target={**p.target, key: "0" * len(p.target[key])})
    assert _count(p) == before


@pytest.mark.parametrize("field", ["original_budget", "child_generation"])
def test_rehashed_receipt_cannot_change_retained_budget_or_child(evidence_fixture, field):
    p = evidence_fixture
    _released_result_ready(p, canceled=True)
    with closing(p.store._connect()) as connection:
        receipt = json.loads(
            connection.execute("SELECT receipt_json FROM aos_control_requests").fetchone()[0]
        )
        if field == "original_budget":
            receipt[field]["queue_deadline"] += 1
        else:
            receipt[field]["pid"] += 1
        receipt["receipt_sha256"] = digest(
            {k: v for k, v in receipt.items() if k != "receipt_sha256"}
        )
        connection.execute("UPDATE aos_control_requests SET receipt_json=?", (canonical(receipt),))
        connection.commit()
    before = _count(p)
    with pytest.raises(ControlStoreError, match="request_conflict"):
        _read(p)
    assert _count(p) == before


def test_late_authority_revocation_rolls_back_control_id(evidence_fixture):
    p = evidence_fixture
    _released_result_ready(p, canceled=False)
    before = _count(p)
    calls = 0

    def verify():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ControlStoreError("unauthorized")

    with pytest.raises(ControlStoreError, match="unauthorized"):
        _read(p, authority=ControlAuthority(p.admission.binding_json, None, verify))
    assert calls == 2
    assert _count(p) == before
    assert _read(p)["result_canonical"] is not None
