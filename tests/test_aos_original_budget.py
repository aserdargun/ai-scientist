"""CPU-only independent retained-column budget capture; no AOS/GPU acceptance."""

import json
from contextlib import closing
from dataclasses import asdict

import pytest
import test_aos_terminal_publication as publication_cases

from lab.llm.aos_gpu_control_store import ControlAuthority, ControlStoreError, canonical, digest

budget_fixture = publication_cases.publication


def _read(p, *, authority=None, target=None):
    return p.store.read_original_budget(
        asdict(p.peer),
        p.target if target is None else target,
        p.profile.profile_id,
        p.profile.deployment_digest,
        authority=authority or ControlAuthority(p.admission.binding_json, None, lambda: None),
    )


def _rows(p):
    with closing(p.store._connect()) as connection:
        return [tuple(row) for row in connection.execute("SELECT * FROM aos_control_requests")]


@pytest.mark.parametrize("canceled", [False, True])
def test_witness_uses_original_columns_not_terminal_json(budget_fixture, canceled):
    p = budget_fixture
    publication_cases._released_result_ready(p, canceled=canceled)
    before = _rows(p)
    witness = _read(p)
    assert _rows(p) == before
    assert witness["budget_sha256"] == digest(json.loads(witness["budget_canonical"]))
    original_budget = json.loads(witness["budget_canonical"])
    assert original_budget["admitted_boottime"] == 100.0
    assert original_budget["context_tokens"] == 256
    with closing(p.store._connect()) as connection:
        row = connection.execute("SELECT * FROM aos_control_requests").fetchone()
        receipt = json.loads(row["receipt_json"])
        assert original_budget == receipt["original_budget"]
        receipt["original_budget"]["queue_deadline"] = 9999.0
        connection.execute("UPDATE aos_control_requests SET receipt_json=?", (canonical(receipt),))
        connection.commit()
    assert _read(p) == witness
    p.store._now = lambda: 10000.0
    assert _read(p) == witness


def test_final_readiness_deadline_overrides_initial_allocation(budget_fixture):
    p = budget_fixture
    publication_cases._released_result_ready(p, canceled=False)
    with closing(p.store._connect()) as connection:
        row = connection.execute("SELECT * FROM aos_control_requests").fetchone()
        lease = json.loads(row["allocation_json"])["lease"]
        deadline = lease["total_deadline"] - 1.0
        connection.execute(
            "UPDATE aos_control_requests SET ready_deadlines_json=?",
            (
                canonical(
                    {
                        "activation_deadline": lease["activation_deadline"],
                        "inference_deadline": deadline,
                        "total_deadline": lease["total_deadline"],
                    }
                ),
            ),
        )
        connection.commit()
    budget = json.loads(_read(p)["budget_canonical"])
    assert budget["inference_deadline"] == deadline
    assert budget["inference_deadline"] != lease["inference_deadline"]


def test_never_admitted_witness_does_not_invent_phase_deadlines(budget_fixture):
    p = budget_fixture
    p.register()
    p.cancel()
    witness = _read(p)
    budget = json.loads(witness["budget_canonical"])
    assert witness["allocation_binding_sha256"] is None
    assert all(
        budget[key] is None
        for key in ("activation_deadline", "inference_deadline", "total_deadline")
    )
    assert budget["envelope_deadline"] == 160.0
    assert budget["admitted_boottime"] == 100.0


def test_inflight_budget_cannot_be_frozen_before_readiness(budget_fixture):
    p = budget_fixture
    p.register()
    before = _rows(p)
    with pytest.raises(ControlStoreError, match="request_conflict"):
        _read(p)
    assert _rows(p) == before


def test_wrong_target_and_revoked_authority_cannot_read_budget(budget_fixture):
    p = budget_fixture
    p.register()
    p.cancel()
    with pytest.raises(ControlStoreError):
        _read(p, target={**p.target, "request_sha256": "0" * 64})
    calls = 0

    def revoke():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ControlStoreError("unauthorized")

    before = _rows(p)
    with pytest.raises(ControlStoreError, match="unauthorized"):
        _read(p, authority=ControlAuthority(p.admission.binding_json, None, revoke))
    assert calls == 2
    assert _rows(p) == before
