"""Explicit frozen historical dev findings; no training or promotion authority."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictStr, model_validator

from lab.director.ledger import canonical_json_bytes
from lab.operating_modes import ModeConfig

Digest = Annotated[StrictStr, Field(pattern=r"^[a-f0-9]{64}$")]
RunId = Annotated[StrictStr, Field(pattern=r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")]
ExperimentId = Annotated[StrictStr, Field(pattern=r"^exp_[a-f0-9]{32}$")]
MAX_PRIOR_FINDINGS = 8
MAX_PRIOR_BYTES = 16 * 1024


class PriorRecordRef(BaseModel):
    """A client can select identities, never supply scores or findings."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    experiment_id: ExperimentId
    experiment_sha256: Digest
    trajectory_sha256: Digest


class PriorExperienceSelection(BaseModel):
    """One explicitly chosen source run and at most eight unique records."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    source_run_id: RunId
    source_report_sha256: Digest
    records: list[PriorRecordRef] = Field(min_length=1, max_length=MAX_PRIOR_FINDINGS)

    @model_validator(mode="after")
    def unique_records(self) -> PriorExperienceSelection:
        """A repeated reference cannot inflate the apparent evidence count."""
        if len({item.experiment_id for item in self.records}) != len(self.records):
            raise ValueError("duplicate prior experiment selection")
        return self


class PriorDevFinding(BaseModel):
    """Allowlisted numeric observation in its own historical measurement namespace."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    experiment_id: ExperimentId
    experiment_sha256: Digest
    trajectory_sha256: Digest
    candidate_sha256: Digest
    configuration_sha256: Digest
    configuration: ModeConfig
    method: Literal["lsh", "optics", "som"]
    dev_suite_score: Annotated[StrictFloat, Field(ge=-1, le=3, allow_inf_nan=False)]
    decision: Literal["KEEP", "KEEP_SIMPLER", "DISCARD"]
    source_harness_sha256: Digest
    source_image_sha256: Digest
    dev_task_scope_sha256: Digest
    provenance_sha256: Digest
    provenance_manifest_sha256s: tuple[Digest, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def configuration_identity(self) -> PriorDevFinding:
        """The method label must match the exact normalized configuration digest."""
        raw = canonical_json_bytes(self.configuration.model_dump(mode="json"))
        if (
            self.method != self.configuration.method
            or hashlib.sha256(raw).hexdigest() != self.configuration_sha256
        ):
            raise ValueError("historical mode configuration identity differs")
        return self


class PriorFindingsSnapshot(BaseModel):
    """Server-created admission evidence, retained identically through resume."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    schema_version: Literal["prior-dev-findings.v1"] = Field(alias="schema")
    source_run_id: RunId
    source_report_sha256: Digest
    source_request_sha256: Digest
    source_suite_manifest_sha256: Digest
    scope: Literal["historical-advisory-only"]
    records: tuple[PriorDevFinding, ...] = Field(min_length=1, max_length=MAX_PRIOR_FINDINGS)

    @model_validator(mode="after")
    def unique_records(self) -> PriorFindingsSnapshot:
        """Keep the snapshot count consistent with the explicitly selected references."""
        if len({item.experiment_id for item in self.records}) != len(self.records):
            raise ValueError("duplicate frozen prior experiment")
        return self


def snapshot_bytes(snapshot: PriorFindingsSnapshot) -> bytes:
    """Canonical bounded admission bytes; no timestamp or mutable runtime state."""
    raw = canonical_json_bytes(snapshot.model_dump(mode="json", by_alias=True))
    if len(raw) > MAX_PRIOR_BYTES:
        raise ValueError("historical findings byte bound exceeded")
    return raw


def load_frozen_prior_findings(request: dict[str, Any]) -> PriorFindingsSnapshot | None:
    """Validate stored bytes, digest and client reference binding without history queries."""
    selection = request.get("prior_experience")
    payload = request.get("prior_findings")
    expected_sha = request.get("prior_findings_sha256")
    if selection is None:
        if payload is not None or expected_sha is not None:
            raise ValueError("frozen findings have no explicit selection")
        return None
    refs = PriorExperienceSelection.model_validate_json(json.dumps(selection), strict=True)
    if not isinstance(payload, dict):
        raise ValueError("selected history has no frozen findings")
    raw = canonical_json_bytes(payload)
    if len(raw) > MAX_PRIOR_BYTES or hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError("frozen findings digest differs")
    snapshot = PriorFindingsSnapshot.model_validate_json(raw, strict=True)
    if snapshot_bytes(snapshot) != raw:
        raise ValueError("frozen findings are not normalized canonical bytes")
    selected = {(r.experiment_id, r.experiment_sha256, r.trajectory_sha256) for r in refs.records}
    frozen = {(r.experiment_id, r.experiment_sha256, r.trajectory_sha256) for r in snapshot.records}
    if (
        snapshot.source_run_id != refs.source_run_id
        or snapshot.source_report_sha256 != refs.source_report_sha256
        or frozen != selected
    ):
        raise ValueError("frozen findings differ from explicit source references")
    return snapshot
