"""Bounded model parameters compiled through the trusted operating-mode adapter."""

from __future__ import annotations

import hashlib
import inspect
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictStr

from lab.director.contracts import CandidateProposal, MoveType
from lab.director.journal import canonical_bytes
from lab.operating_modes.candidate import candidate_source
from lab.operating_modes.contracts import ModeConfig

ProposalContract = Literal["candidate-python.v1", "operating-mode-config.v1"]
MODE_PROMPT_TEMPLATE = "director.operating-mode-contract.metadata-only.v1"


class OperatingModeProposal(BaseModel):
    """An unmeasured hypothesis; the model cannot supply code, scores or a verdict."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, validate_by_name=True)

    schema_version: Literal["operating-mode-proposal.v1"] = Field(alias="schema")
    hypothesis: Annotated[StrictStr, Field(min_length=1, max_length=384)]
    move_type: MoveType
    configuration: ModeConfig
    predicted_delta: Annotated[StrictFloat, Field(allow_inf_nan=False, ge=-4.0, le=4.0)]


def compile_mode_proposal(
    proposal: OperatingModeProposal, expected_move: str | None = None
) -> CandidateProposal:
    """Produce sandbox input using only the existing deterministic trusted compiler."""
    if expected_move is not None and proposal.move_type != expected_move:
        raise ValueError("model proposal changed the trusted selected move intent")
    return CandidateProposal(
        hypothesis=proposal.hypothesis,
        move_type=proposal.move_type,
        candidate_source=candidate_source(proposal.configuration).decode("utf-8"),
        predicted_delta=proposal.predicted_delta,
    )


MODE_PROPOSAL_SCHEMA: dict[str, object] = OperatingModeProposal.model_json_schema()
MODE_SCHEMA_SHA256 = hashlib.sha256(canonical_bytes(MODE_PROPOSAL_SCHEMA)).hexdigest()
MODE_SCHEMA_NAME = "operating-mode-proposal.v1"
MODE_COMPILER_SHA256 = hashlib.sha256(inspect.getsource(candidate_source).encode()).hexdigest()
MODE_SYSTEM_PROMPT = (
    "Propose normal operating-mode parameters only. Return exactly one compact JSON object "
    "with schema=operating-mode-proposal.v1, hypothesis (1..384 characters), move_type "
    "(the trusted preselected move), configuration (ModeConfig), and predicted_delta "
    "(finite number in [-4,4], a prediction only). No Python, scores, verdicts, labels or "
    "raw series. The host compiles these parameters and Docker/Scorer measure them; "
    "Referee decides. configuration.method is lsh, optics or som. All settings obey the "
    "response schema. Omitted settings use its explicit defaults. Mode fitting, tolerance "
    "and distance, nearest-neighbor normal-value prediction, continuous sensor residual/OMR "
    "and alarm calibration are separate stages. SOM distance is not OMR. Unavailable OMR "
    "remains unavailable and is never zero. Fit and calibration use healthy training only; "
    "evaluation values never calibrate the model. Use trusted metadata, measured development "
    "feedback and the champion to propose a testable change. Preserve the selected move. "
    "S1 makes a small improvement; S2 may change method or combine parameter changes. "
    "The champion source is host-compiled context, not a request for model-written code."
)


def validate_proposal_contract(value: ProposalContract) -> ProposalContract:
    """Fail closed even when a caller bypasses static type checking."""
    if value not in ("candidate-python.v1", "operating-mode-config.v1"):
        raise ValueError("unknown trusted proposal contract")
    return value
