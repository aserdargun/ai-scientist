"""Label-free, whole-task SKAB development snapshot; original source identity is preserved."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from lab.director.contracts import SourceProvenance
from lab.director.suite_manifest import SuiteTaskManifest
from lab.suite_limits import MAX_SUITE_MANIFEST_BYTES

if TYPE_CHECKING:
    from lab.director.public_suite import ScorerTaskRegistration

SKAB_TASK_ID = "valve1-0-fixed-source-session"
PUBLIC_SUITE_SHA256 = "a183c276742b35593866d99d743f83dec37f0f59079a70fa72e3627c48e0053c"


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


class PublicTaskBinding(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["mode-public-task-binding.v1"] = Field(alias="schema")
    source_suite_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    original_task_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    original_task: SuiteTaskManifest
    semantics_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    train_matrix_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    evaluation_matrix_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_train_start_index: int = Field(ge=0)
    source_train_end_index: int = Field(gt=0)
    source_evaluation_start_index: int = Field(ge=0)
    source_evaluation_end_index: int = Field(gt=0)
    time_axis: Literal["source_order_index"] = "source_order_index"
    source_time_kind: Literal["naive"] = "naive"
    fit_policy: Literal["label_blind_source_order;normal_fit_not_guaranteed"] = (
        "label_blind_source_order;normal_fit_not_guaranteed"
    )

    @model_validator(mode="after")
    def verify(self) -> PublicTaskBinding:
        task = self.original_task
        if (
            self.source_suite_manifest_sha256 != PUBLIC_SUITE_SHA256
            or task.dataset_id != "SKAB"
            or task.task_id != SKAB_TASK_ID
            or task.split_id != "public-benchmark-fixed-source-prefix.v1"
            or task.provenance.usage_profile != "noncommercial_research"
            or task.provenance.dataset_id != task.dataset_id
            or task.provenance.split_id != task.split_id
            or task.provenance.session_id != task.session_id
            or len(task.train) != 26
            or len(task.evaluation) != 470
            or len(task.train) + len(task.evaluation) > 4096
            or self.source_train_end_index - self.source_train_start_index != len(task.train)
            or self.source_evaluation_end_index - self.source_evaluation_start_index
            != len(task.evaluation)
            or self.source_train_end_index > self.source_evaluation_start_index
            or self.original_task_sha256 != digest(task.model_dump(mode="json"))
            or self.train_matrix_sha256 != digest(task.train)
            or self.evaluation_matrix_sha256 != digest(task.evaluation)
        ):
            raise ValueError("public task binding differs from the exact development source task")
        return self

    @property
    def sha256(self) -> str:
        return digest(self.model_dump(mode="json", by_alias=True))

    def verify_study_task(self, task: SuiteTaskManifest) -> None:
        if task != self.original_task.model_copy(update={"task_weight": 1.0}):
            raise ValueError("public study must preserve the original task except its study weight")


class PublicTaskSnapshot(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["public-task-snapshot.v1"] = Field(alias="schema")
    source_kind: Literal["public_dev"] = "public_dev"
    binding: PublicTaskBinding

    @property
    def sha256(self) -> str:
        return digest(self.model_dump(mode="json", by_alias=True))

    @property
    def sensors(self) -> tuple[str, ...]:
        return self.binding.original_task.columns

    @property
    def provenance(self) -> SourceProvenance:
        return self.binding.original_task.provenance

    def frame(self, partition: Literal["all", "train", "evaluation"] = "all") -> pd.DataFrame:
        task = self.binding.original_task
        values = (
            task.train
            if partition == "train"
            else task.evaluation
            if partition == "evaluation"
            else task.train + task.evaluation
        )
        return pd.DataFrame(values, columns=self.sensors, dtype=float)

    def describe(self) -> dict[str, Any]:
        task, binding = self.binding.original_task, self.binding
        return dict(
            source_kind="public_dev",
            snapshot_sha256=self.sha256,
            source=dict(
                source_id=f"{task.dataset_id}:{task.task_id}",
                dataset_id=task.dataset_id,
                task_id=task.task_id,
                source_version=task.provenance.source_revision,
                license_id=task.provenance.license_id,
                usage_profile=task.provenance.usage_profile,
                source_manifest_sha256=task.provenance.source_manifest_sha256,
            ),
            provenance=task.provenance.model_dump(mode="json"),
            source_suite_manifest_sha256=binding.source_suite_manifest_sha256,
            original_task_sha256=binding.original_task_sha256,
            profile_sha256=task.profile_sha256,
            semantics_sha256=binding.semantics_sha256,
            binding_sha256=binding.sha256,
            sensors=list(self.sensors),
            rows=496,
            train_rows=26,
            evaluation_rows=470,
            first_utc=None,
            last_utc=None,
            time_axis=binding.time_axis,
            source_time_kind=binding.source_time_kind,
            source_ranges=dict(
                train=[binding.source_train_start_index, binding.source_train_end_index],
                evaluation=[
                    binding.source_evaluation_start_index,
                    binding.source_evaluation_end_index,
                ],
            ),
            fit_policy=binding.fit_policy,
            scoring_available=True,
        )


def materialize_public_snapshot(
    repository_root: Path, source_suite: Path, expected_sha256: str
) -> tuple[PublicTaskSnapshot, ScorerTaskRegistration]:
    """Trusted operator only: revalidate source matrices and keep labels outside all artifacts."""
    from lab.director.public_suite import materialize_skab_development

    if (
        expected_sha256 != PUBLIC_SUITE_SHA256
        or source_suite.is_symlink()
        or source_suite.stat().st_size > MAX_SUITE_MANIFEST_BYTES
    ):
        raise ValueError("public source suite identity is not the reviewed development suite")
    raw = source_suite.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("public source suite hash differs")
    document = json.loads(raw)
    if document.get("schema") != "director-suite.v1":
        raise ValueError("invalid original public suite")
    selected = [task for task in document["tasks"] if task["task_id"] == SKAB_TASK_ID]
    if len(selected) != 1:
        raise ValueError("exactly one original SKAB development task required")
    original = SuiteTaskManifest.model_validate(selected[0], strict=True)
    (materialized,) = materialize_skab_development(repository_root=repository_root)
    task, registration = materialized.task, materialized.scorer_registration
    rebuilt = SuiteTaskManifest(
        task_id=task.task_id,
        dataset_id=task.dataset_id,
        split_id=task.split_id,
        session_id=task.session_id,
        profile_sha256=task.profile_sha256,
        family=task.family,
        task_weight=original.task_weight,
        independent_family=task.independent_family,
        label_tier=task.label_tier,
        provenance=task.provenance,
        context=dict(
            seed=task.context.seed,
            signals=list(task.context.signals),
            regime_signals=list(task.context.regime_signals),
            sampling_s=task.context.sampling_s,
            time_budget_s=task.context.time_budget_s,
        ),
        columns=task._columns,
        train=tuple(tuple(float(v) for v in row) for row in task._train),
        evaluation=tuple(tuple(float(v) for v in row) for row in task._evaluation),
    )
    if (
        rebuilt != original
        or registration.visibility != "dev"
        or registration.semantics_sha256 is None
    ):
        raise ValueError("materialized SKAB task differs from original public development task")
    indices = (
        materialized.source_train_start_index,
        materialized.source_train_end_index,
        materialized.source_evaluation_start_index,
        materialized.source_evaluation_end_index,
    )
    if any(type(value) is not int for value in indices):
        raise ValueError("public source ranges must be explicit unchanged source indices")
    binding = PublicTaskBinding(
        schema="mode-public-task-binding.v1",
        source_suite_manifest_sha256=expected_sha256,
        original_task_sha256=digest(original.model_dump(mode="json")),
        original_task=original,
        semantics_sha256=registration.semantics_sha256,
        train_matrix_sha256=digest(original.train),
        evaluation_matrix_sha256=digest(original.evaluation),
        source_train_start_index=cast(int, materialized.source_train_start_index),
        source_train_end_index=cast(int, materialized.source_train_end_index),
        source_evaluation_start_index=cast(int, materialized.source_evaluation_start_index),
        source_evaluation_end_index=cast(int, materialized.source_evaluation_end_index),
    )
    return PublicTaskSnapshot(schema="public-task-snapshot.v1", binding=binding), registration
