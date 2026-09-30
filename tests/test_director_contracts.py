"""Strict schemas for model proposals and trusted research records."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from lab.director.contracts import CandidateProposal, ExperimentDocument


def test_candidate_proposal_carries_only_untrusted_research_intent() -> None:
    proposal = CandidateProposal.model_validate(
        {
            "hypothesis": "Train-only robust scale improves event separation.",
            "move_type": "preprocess",
            "candidate_source": "def build_candidate():\n    return Candidate()\n",
            "predicted_delta": 0.02,
        },
        strict=True,
    )

    assert proposal.move_type == "preprocess"
    assert proposal.predicted_delta == 0.02


@pytest.mark.parametrize(
    "payload",
    [
        {
            "hypothesis": "candidate source",
            "move_type": "preprocess",
            "candidate_source": "pass",
            "predicted_delta": float("nan"),
        },
        {
            "hypothesis": "candidate source",
            "move_type": "preprocess",
            "candidate_source": "pass",
            "predicted_delta": "0.0",
        },
        {
            "hypothesis": "candidate source",
            "move_type": "preprocess",
            "candidate_source": "pass",
            "simpler": False,
            "predicted_delta": 0.0,
            "verdict": "KEEP",
        },
    ],
)
def test_candidate_cannot_supply_invalid_values_or_a_verdict(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CandidateProposal.model_validate(payload, strict=True)


def test_final_experiment_record_is_typed_and_rejects_unknown_fields() -> None:
    payload = {
        "schema": "experiment.v1",
        "experiment_id": f"exp_{uuid4().hex}",
        "run_id": str(uuid4()),
        "ordinal": 1,
        "kind": "proposal",
        "experiment_number": 1,
        "baseline_name": None,
        "calibration_sha256": "1" * 64,
        "agent_version": "fake-llm.v1",
        "parent_experiment_id": None,
        "candidate_sha256": "a" * 64,
        "candidate_blob_sha256": "b" * 64,
        "move_type": "detector",
        "system": "S1",
        "hypothesis": "A robust train-only scale separates anomalies.",
        "predicted_delta": 0.1,
        "inputs_sha256": "c" * 64,
        "parent_tree": "d" * 40,
        "child_tree": None,
        "harness_sha256": "e" * 64,
        "image_sha256": "f" * 64,
        "suite_id": "synthetic-four-family",
        "suite_version": 1,
        "per_task": [],
        "suite_score": None,
        "guards": {"causality": "not_run"},
        "decision": {
            "verdict": "REJECT",
            "delta": None,
            "ci_low": None,
            "noise_sd": 0.0,
            "reason": "causality",
        },
        "status": "rejected",
        "fit_seconds": None,
        "score_seconds": None,
        "llm_input_tokens": 0,
        "llm_output_tokens": 0,
        "wall_seconds": 0.0,
    }
    record = ExperimentDocument.model_validate_json(json.dumps(payload), strict=True)
    assert record.schema_version == "experiment.v1"
    assert record.decision.verdict == "REJECT"

    final_payload = record.model_dump(mode="json", by_alias=True)
    final_payload["trusted_verdict"] = "KEEP"
    with pytest.raises(ValidationError):
        ExperimentDocument.model_validate_json(json.dumps(final_payload), strict=True)


def test_baseline_experiment_record_has_no_fabricated_referee_or_delta() -> None:
    payload = {
        "schema": "experiment.v1",
        "experiment_id": f"exp_{uuid4().hex}",
        "run_id": str(uuid4()),
        "ordinal": 1,
        "kind": "baseline",
        "experiment_number": None,
        "baseline_name": "robust_z",
        "calibration_sha256": None,
        "agent_version": "baseline.v1",
        "parent_experiment_id": None,
        "candidate_sha256": "a" * 64,
        "candidate_blob_sha256": "b" * 64,
        "move_type": "detector",
        "system": "S1",
        "hypothesis": "Measured robust-z baseline.",
        "predicted_delta": None,
        "inputs_sha256": "c" * 64,
        "parent_tree": "d" * 40,
        "child_tree": None,
        "harness_sha256": "e" * 64,
        "image_sha256": "f" * 64,
        "suite_id": "synthetic-four-family",
        "suite_version": 1,
        "per_task": [],
        "suite_score": None,
        "guards": {"determinism": "pass", "causality": "pass"},
        "decision": None,
        "status": "scored",
        "fit_seconds": 1.0,
        "score_seconds": 1.0,
        "llm_input_tokens": 0,
        "llm_output_tokens": 0,
        "wall_seconds": 2.0,
    }
    record = ExperimentDocument.model_validate_json(json.dumps(payload), strict=True)
    assert record.kind == "baseline"
    assert record.predicted_delta is None
    assert record.decision is None
    with pytest.raises(ValidationError):
        ExperimentDocument.model_validate({**payload, "predicted_delta": 0.0}, strict=True)
