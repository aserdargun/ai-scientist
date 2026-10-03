"""Distinct after-admission receipts preserve old stop records and proof hashes."""

from uuid import UUID

import pytest
from pydantic import ValidationError

from lab.director.contracts import InfrastructureStopReceipt


def receipt_fields():
    return {
        "reason": "stopped_during_primary_evaluation",
        "recovery_id": UUID(int=1),
        "run_id": UUID(int=2),
        "experiment_id": "exp_" + "a" * 32,
        "generation": 2,
        "execution_sha256": "a" * 64,
        "proposal_sha256": "b" * 64,
        "reservation_sha256": "c" * 64,
        "reconciled_sha256": None,
        "calibration_sha256": "d" * 64,
        "admitted_inventory_sha256": "e" * 64,
        "terminal_inventory_sha256": "f" * 64,
    }


def test_after_admission_receipt_requires_distinct_reason_and_both_inventory_hashes():
    from lab.director.contracts import AttemptedProposalStopReceipt

    receipt = AttemptedProposalStopReceipt.model_validate(receipt_fields(), strict=True)
    assert AttemptedProposalStopReceipt.model_validate_json(receipt.model_dump_json()) == receipt
    with pytest.raises(ValidationError):
        InfrastructureStopReceipt.model_validate(receipt_fields(), strict=True)


@pytest.mark.parametrize("field", ["admitted_inventory_sha256", "terminal_inventory_sha256"])
def test_after_admission_receipt_rejects_missing_or_invalid_inventory_proof(field):
    from lab.director.contracts import AttemptedProposalStopReceipt

    payload = receipt_fields()
    payload.pop(field)
    with pytest.raises(ValidationError):
        AttemptedProposalStopReceipt.model_validate(payload, strict=True)
    payload[field] = "not-a-trusted-proof"
    with pytest.raises(ValidationError):
        AttemptedProposalStopReceipt.model_validate(payload, strict=True)


def test_zero_admission_receipt_byte_shape_does_not_gain_inventory_fields():
    payload = receipt_fields()
    payload["reason"] = "stopped_before_candidate_admission"
    payload.pop("admitted_inventory_sha256")
    payload.pop("terminal_inventory_sha256")
    receipt = InfrastructureStopReceipt.model_validate(payload, strict=True)
    assert set(receipt.model_dump()) == set(payload)
    assert "inventory" not in receipt.model_dump_json()
