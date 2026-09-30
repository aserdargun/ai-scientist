"""Strict public report contract for standalone baseline calibration runs."""

from __future__ import annotations

import json
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator


class BaselineDocumentReceipt(BaseModel):
    """Scorer-checked terminal pair that proves a baseline used no model."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    experiment_id: StrictStr = Field(min_length=1, max_length=128)
    baseline_name: Literal["robust_z", "iforest", "ecod_train_frozen"]
    status: Literal["scored", "abandoned", "crashed", "rejected"]
    experiment_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    trajectory_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")


class ReceiptExecutionPair(BaseModel):
    """Original task admission identity retained after a validated restart."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    admitted_generation: StrictInt = Field(ge=1)
    execution_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")


class BaselineReport(BaseModel):
    """Scorer-published, hash-bound baseline outcome with no research claims."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_name: Literal["lab.baseline-report.v1"] = Field(alias="schema")
    run_id: UUID
    purpose: Literal["baseline"]
    status: Literal["completed", "failed", "stopped"]
    calibration_complete: bool
    budget_verified: bool
    request_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    suite_id: StrictStr = Field(min_length=1, max_length=128)
    suite_version: StrictInt | None
    suite_manifest_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    harness_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    image_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    task_plan_sha256: StrictStr | None = Field(pattern=r"^[0-9a-f]{64}$")
    task_plan_count: StrictInt | None = Field(ge=0)
    admitted_generation: StrictInt | None = Field(default=None, ge=1)
    execution_sha256: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    receipt_execution_pairs: tuple[ReceiptExecutionPair, ...] | None = None
    calibration_sha256: StrictStr | None
    algorithms: tuple[Literal["robust_z", "iforest", "ecod_train_frozen"], ...]
    seeds: tuple[Literal[0, 1, 2], ...]
    task_count: StrictInt | None
    score_count: StrictInt = Field(ge=0)
    budget_receipt: dict[str, Any]
    model_usage: dict[Literal["input_tokens", "output_tokens", "calls"], StrictInt]
    baseline_records: tuple[dict[str, Any], ...]
    experiment_records: tuple[BaselineDocumentReceipt, ...]
    task_terminal_outcomes: tuple[dict[str, Any], ...]

    @model_validator(mode="after")
    def validate_state_contract(self) -> BaselineReport:
        if self.algorithms != ("robust_z", "iforest", "ecod_train_frozen"):
            raise ValueError("baseline report must retain the fixed three-algorithm matrix")
        if self.seeds != (0, 1, 2):
            raise ValueError("baseline report must retain seeds 0, 1 and 2")
        if (self.admitted_generation is None) != (self.execution_sha256 is None):
            raise ValueError("baseline report execution generation must be a complete pair")
        if self.receipt_execution_pairs is not None:
            if (
                not self.receipt_execution_pairs
                or self.admitted_generation is None
                or any(
                    pair.execution_sha256 != self.execution_sha256
                    or pair.admitted_generation > self.admitted_generation
                    for pair in self.receipt_execution_pairs
                )
            ):
                raise ValueError("baseline report receipt ancestry differs from writer execution")
            pairs = [
                (pair.admitted_generation, pair.execution_sha256)
                for pair in self.receipt_execution_pairs
            ]
            if pairs != sorted(set(pairs)):
                raise ValueError("baseline report receipt pairs must be unique and ordered")
        if any(self.model_usage.values()):
            raise ValueError("baseline report cannot contain model usage")
        if set(self.model_usage) != {"input_tokens", "output_tokens", "calls"}:
            raise ValueError("baseline report must include all zero model-usage counters")
        if self.calibration_complete != (self.status == "completed"):
            raise ValueError("only a completed baseline report can claim frozen calibration")
        if self.status == "completed":
            if (
                not self.budget_verified
                or self.task_plan_sha256 is None
                or self.task_plan_count is None
                or self.calibration_sha256 is None
                or self.suite_version is None
                or self.task_count is None
                or self.score_count != self.task_count * 9
                or self.task_plan_count != self.score_count
                or len(self.experiment_records) != 3
                or {item.baseline_name for item in self.experiment_records}
                != {"robust_z", "iforest", "ecod_train_frozen"}
                or any(item.status != "scored" for item in self.experiment_records)
            ):
                raise ValueError("completed baseline report is missing its verified matrix receipt")
        else:
            if self.task_plan_sha256 is None or self.task_plan_count is None:
                raise ValueError("incomplete baseline report requires a sealed task plan")
            if self.budget_verified:
                raise ValueError(
                    "incomplete baseline reports cannot claim a verified completion budget"
                )
            if self.calibration_sha256 is not None:
                raise ValueError("incomplete baseline reports cannot carry a frozen calibration")
            if self.task_plan_count is not None and self.score_count > self.task_plan_count:
                raise ValueError("incomplete baseline score count exceeds its sealed plan")
            if self.task_plan_count is not None and (
                self.score_count + len(self.task_terminal_outcomes) != self.task_plan_count
            ):
                raise ValueError("incomplete baseline report does not resolve its sealed plan")
        return self


def validate_baseline_report(value: dict[str, Any]) -> dict[str, Any]:
    """Validate JSON-friendly source data and return its canonical report object."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    model = BaselineReport.model_validate_json(encoded, strict=True)
    return model.model_dump(mode="json", by_alias=True, exclude_unset=True)
