"""Strict Director proposal and immutable experiment/trajectory records."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_serializer,
    model_validator,
)

MoveType = Literal[
    "hparam",
    "preprocess",
    "features",
    "regime",
    "detector",
    "fusion",
    "alarm_policy",
    "simplify",
    "skill_reuse",
]
SystemRole = Literal["S1", "S2"]
Verdict = Literal["KEEP", "KEEP_SIMPLER", "DISCARD", "REJECT"]
ExperimentStatus = Literal["scored", "crashed", "abandoned", "rejected"]
BaselineName = Literal["robust_z", "iforest", "ecod_train_frozen"]
ExperimentKind = Literal["baseline", "proposal"]


def replay_configuration_sha256(payload: dict[str, object]) -> str:
    """Hash canonical manifest fields except the self-referential digest field."""
    fields = {key: value for key, value in payload.items() if key != "configuration_sha256"}
    return hashlib.sha256(
        json.dumps(
            fields, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str
        ).encode("utf-8")
    ).hexdigest()


class CandidateProposal(BaseModel):
    """Untrusted fake/real LLM output; it carries a hypothesis and source only."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    hypothesis: Annotated[StrictStr, Field(min_length=1, max_length=2_000)]
    move_type: MoveType
    candidate_source: Annotated[StrictStr, Field(min_length=1, max_length=65_536)]
    predicted_delta: Annotated[StrictFloat, Field(allow_inf_nan=False, ge=-4.0, le=4.0)]


class PerTaskResult(BaseModel):
    """Scorer-derived measurements for one trusted task identity."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    task_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    task_family: Literal["EVT", "PDM", "NRM"] = "EVT"
    score_norm: Annotated[StrictFloat, Field(allow_inf_nan=False, ge=-1.0, le=3.0)] | None
    vus_pr: Annotated[StrictFloat, Field(allow_inf_nan=False, ge=0.0, le=1.0)] | None = None
    vus_roc: Annotated[StrictFloat, Field(allow_inf_nan=False, ge=0.0, le=1.0)] | None = None
    task_score: Annotated[StrictFloat, Field(allow_inf_nan=False)] | None = None
    fa_per_day: Annotated[StrictFloat, Field(allow_inf_nan=False, ge=0.0)] | None = None
    duty_fraction: Annotated[StrictFloat, Field(allow_inf_nan=False, ge=0.0, le=1.0)] | None = None
    event_f1: Annotated[StrictFloat, Field(allow_inf_nan=False, ge=0.0, le=1.0)] | None
    fit_seconds: Annotated[StrictFloat, Field(allow_inf_nan=False, ge=0.0)]
    score_seconds: Annotated[StrictFloat, Field(allow_inf_nan=False, ge=0.0)]


class ExperimentDecision(BaseModel):
    """Referee result with every measured field explicit and finite."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    verdict: Verdict
    delta: Annotated[StrictFloat, Field(allow_inf_nan=False)] | None
    ci_low: Annotated[StrictFloat, Field(allow_inf_nan=False)] | None
    noise_sd: Annotated[StrictFloat, Field(allow_inf_nan=False, ge=0.0)]
    reason: Annotated[StrictStr, Field(min_length=1, max_length=128)]


class ReplayManifestDocument(BaseModel):
    """All trusted inputs needed to recompute one Referee decision exactly."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal["referee-replay.v1"] = Field(alias="schema")
    decision_stage: Literal["primary", "confirmed"]
    experiment_id: Annotated[StrictStr, Field(pattern=r"^exp_[0-9a-f]{32}$")]
    run_id: UUID
    parent_experiment_id: Annotated[StrictStr, Field(pattern=r"^exp_[0-9a-f]{32}$")]
    parent_tree: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{40,64}$")]
    child_tree: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{40,64}$")]
    candidate_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    parent_source_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    child_source_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    calibration_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    harness_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    image_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    suite_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    suite_version: Annotated[StrictInt, Field(ge=1)]
    referee_source_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    python_version: Annotated[StrictStr, Field(min_length=1, max_length=64)]
    numpy_version: Annotated[StrictStr, Field(min_length=1, max_length=64)]
    task_ids: tuple[Annotated[StrictStr, Field(min_length=1, max_length=128)], ...]
    expected_decision: ExperimentDecision
    task_families: tuple[Literal["EVT", "PDM", "NRM"], ...]
    profile_sha256: tuple[Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")], ...]
    normalization_base: tuple[StrictFloat, ...]
    normalization_reference: tuple[StrictFloat, ...]
    parent: tuple[StrictFloat, ...]
    child: tuple[StrictFloat, ...]
    weights: tuple[StrictFloat, ...]
    parent_raw_scores: tuple[tuple[StrictFloat, ...], ...]
    child_raw_scores: tuple[tuple[StrictFloat, ...], ...]
    parent_noise_raw_scores: tuple[tuple[StrictFloat, ...], ...]
    parent_noise_seed_ids: tuple[StrictInt, ...]
    parent_noise_evaluation_kinds: tuple[Literal["baseline", "primary", "confirmation"], ...]
    parent_noise_output_sha256: tuple[tuple[StrictStr, ...], ...]
    parent_noise_score_sha256: tuple[StrictStr, ...]
    parent_seed_ids: tuple[StrictInt, ...]
    parent_seed_evaluation_kinds: tuple[Literal["baseline", "primary", "confirmation"], ...]
    decision_seeds: tuple[StrictInt, ...]
    parent_seed_output_sha256: tuple[tuple[StrictStr, ...], ...]
    parent_seed_score_sha256: tuple[StrictStr, ...]
    child_seed_output_sha256: tuple[tuple[StrictStr, ...], ...]
    child_seed_score_sha256: tuple[StrictStr, ...]
    eps: StrictFloat
    noise_sd: StrictFloat
    simpler: StrictBool
    guards_ok: StrictBool
    guard_results: dict[StrictStr, Literal["pass", "fail", "not_run"]]
    position_bias_threshold: StrictFloat = Field(ge=0.0, le=1.0)
    max_task_drop: StrictFloat = 0.5
    best_suite: StrictFloat
    n_boot: StrictInt = 4000
    bootstrap_seed: StrictInt = 0
    configuration_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]

    @model_validator(mode="after")
    def validate_manifest(self) -> ReplayManifestDocument:
        if not self.task_ids or len(set(self.task_ids)) != len(self.task_ids):
            raise ValueError("replay task order must be non-empty and unique")
        task_lengths = (
            len(self.task_families),
            len(self.profile_sha256),
            len(self.normalization_base),
            len(self.normalization_reference),
            len(self.parent),
            len(self.child),
            len(self.weights),
        )
        if any(length != len(self.task_ids) for length in task_lengths):
            raise ValueError("replay vectors must match the ordered task list")
        if len(self.decision_seeds) not in {1, 2} or self.decision_seeds[0] != 0:
            raise ValueError(
                "decision seeds must be primary seed zero, optionally confirmation one"
            )
        if self.decision_seeds not in {(0,), (0, 1)}:
            raise ValueError("unsupported Referee seed set")
        valid_parent_sources: dict[
            str,
            set[
                tuple[
                    tuple[int, ...],
                    tuple[Literal["baseline", "primary", "confirmation"], ...],
                ]
            ],
        ] = {
            "primary": {
                ((0,), ("baseline",)),
                ((0,), ("primary",)),
            },
            "confirmed": {
                ((0, 1), ("baseline", "baseline")),
                ((0, 1), ("primary", "confirmation")),
            },
        }
        if (self.parent_seed_ids, self.parent_seed_evaluation_kinds) not in valid_parent_sources[
            self.decision_stage
        ]:
            raise ValueError("parent decision seeds have invalid evaluation-kind provenance")
        if self.parent_noise_seed_ids != (0, 1, 2):
            raise ValueError("champion noise replay requires seeds zero through two")
        if self.parent_noise_evaluation_kinds not in {
            ("baseline", "baseline", "baseline"),
            ("primary", "confirmation", "confirmation"),
        }:
            raise ValueError("champion noise seeds have invalid evaluation-kind provenance")
        score_rows = (
            *self.parent_raw_scores,
            *self.child_raw_scores,
            *self.parent_noise_raw_scores,
        )
        output_rows = (
            *self.parent_seed_output_sha256,
            *self.child_seed_output_sha256,
            *self.parent_noise_output_sha256,
        )
        if any(len(values) != len(self.task_ids) for values in (*score_rows, *output_rows)):
            raise ValueError("replay per-seed rows must match the ordered task list")
        if (
            len(self.parent_raw_scores) != len(self.parent_seed_ids)
            or len(self.parent_seed_evaluation_kinds) != len(self.parent_seed_ids)
            or len(self.child_raw_scores) != len(self.decision_seeds)
            or len(self.parent_seed_output_sha256) != len(self.parent_seed_ids)
            or len(self.child_seed_output_sha256) != len(self.decision_seeds)
            or len(self.parent_seed_score_sha256) != len(self.parent_seed_ids)
            or len(self.child_seed_score_sha256) != len(self.decision_seeds)
            or len(self.parent_noise_raw_scores) != len(self.parent_noise_seed_ids)
            or len(self.parent_noise_evaluation_kinds) != len(self.parent_noise_seed_ids)
            or len(self.parent_noise_output_sha256) != len(self.parent_noise_seed_ids)
            or len(self.parent_noise_score_sha256) != len(self.parent_noise_seed_ids)
        ):
            raise ValueError("replay seed arrays and receipts have inconsistent lengths")
        all_digests = (
            *self.profile_sha256,
            *self.parent_seed_score_sha256,
            *self.child_seed_score_sha256,
            *self.parent_noise_score_sha256,
            *(digest for row in output_rows for digest in row),
        )
        if any(
            len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value)
            for value in all_digests
        ):
            raise ValueError("replay source receipts must be SHA-256 digests")
        config = self.model_dump(mode="json", by_alias=True)
        expected_config = replay_configuration_sha256(config)
        if self.configuration_sha256 != expected_config:
            raise ValueError("replay configuration fingerprint mismatch")
        return self


class TerminalReplayDisposition(BaseModel):
    """Receipt for an unmeasured terminal rejection, with no invented scores."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal["terminal-replay.v1"] = Field(alias="schema")
    experiment_id: Annotated[StrictStr, Field(pattern=r"^exp_[0-9a-f]{32}$")]
    run_id: UUID
    evaluation_status: Annotated[StrictStr, Field(min_length=1, max_length=64)]
    terminal_code: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    candidate_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    parent_experiment_id: Annotated[StrictStr, Field(pattern=r"^exp_[0-9a-f]{32}$")]
    parent_tree: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{40,64}$")]
    calibration_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    harness_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    image_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    decision: ExperimentDecision


class SourceProvenance(BaseModel):
    """Trusted dataset source identity and licensing supplied outside the LLM."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    dataset_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    split_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    session_id: Annotated[StrictStr, Field(min_length=1, max_length=256)]
    source_manifest_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    source_revision: Annotated[StrictStr, Field(min_length=1, max_length=256)]
    license_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    attribution: Annotated[StrictStr, Field(min_length=1, max_length=1_000)]
    access_terms: Annotated[StrictStr, Field(min_length=1, max_length=1_000)]
    usage_profile: Literal["noncommercial_research"]


class InfrastructureStopReceipt(BaseModel):
    """Hash-bound stop of a registered proposal before any candidate admission."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    reason: Literal["stopped_before_candidate_admission"]
    recovery_id: UUID
    run_id: UUID
    experiment_id: Annotated[StrictStr, Field(pattern=r"^exp_[0-9a-f]{32}$")]
    generation: Annotated[StrictInt, Field(ge=1)]
    execution_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    proposal_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    reservation_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    reconciled_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")] | None
    calibration_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]


class AttemptedProposalStopReceipt(BaseModel):
    """Exact admitted and terminal inventories for an interrupted primary evaluation."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    reason: Literal["stopped_during_primary_evaluation"]
    recovery_id: UUID
    run_id: UUID
    experiment_id: Annotated[StrictStr, Field(pattern=r"^exp_[0-9a-f]{32}$")]
    generation: Annotated[StrictInt, Field(ge=1)]
    execution_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    proposal_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    reservation_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    reconciled_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")] | None
    calibration_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    admitted_inventory_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    terminal_inventory_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]


class ExperimentDocument(BaseModel):
    """Final immutable experiment.v1 record, derived by trusted Director code."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal["experiment.v1"] = Field(alias="schema")
    experiment_id: Annotated[StrictStr, Field(pattern=r"^exp_[0-9a-f]{32}$")]
    run_id: UUID
    ordinal: Annotated[StrictInt, Field(ge=1, le=1_000)]
    kind: ExperimentKind
    experiment_number: Annotated[StrictInt, Field(ge=1)] | None
    baseline_name: BaselineName | None
    calibration_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")] | None
    agent_version: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    parent_experiment_id: Annotated[StrictStr, Field(pattern=r"^exp_[0-9a-f]{32}$")] | None
    candidate_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    candidate_blob_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    move_type: MoveType
    system: SystemRole
    hypothesis: Annotated[StrictStr, Field(min_length=1, max_length=2_000)]
    predicted_delta: Annotated[StrictFloat, Field(allow_inf_nan=False, ge=-4.0, le=4.0)] | None
    inputs_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    parent_tree: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{40,64}$")]
    child_tree: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{40,64}$")] | None
    harness_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    image_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    suite_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    suite_version: Annotated[StrictInt, Field(ge=1)]
    per_task: tuple[PerTaskResult, ...]
    suite_score: Annotated[StrictFloat, Field(allow_inf_nan=False, ge=-1.0, le=3.0)] | None
    guards: dict[StrictStr, Literal["pass", "fail", "not_run"]]
    decision: ExperimentDecision | None
    status: ExperimentStatus
    fit_seconds: Annotated[StrictFloat, Field(allow_inf_nan=False, ge=0.0)] | None
    score_seconds: Annotated[StrictFloat, Field(allow_inf_nan=False, ge=0.0)] | None
    llm_input_tokens: Annotated[StrictInt, Field(ge=0)]
    llm_output_tokens: Annotated[StrictInt, Field(ge=0)]
    wall_seconds: Annotated[StrictFloat, Field(allow_inf_nan=False, ge=0.0)] | None
    provider_receipt_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")] | None = None

    infrastructure_stop: InfrastructureStopReceipt | AttemptedProposalStopReceipt | None = None

    @model_serializer(mode="wrap")
    def preserve_historical_bytes(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        value = cast(dict[str, Any], handler(self))
        if self.infrastructure_stop is None:
            value.pop("infrastructure_stop", None)
        return value

    @model_validator(mode="after")
    def validate_kind_identity(self) -> ExperimentDocument:
        if self.infrastructure_stop is not None and not (
            self.kind == "proposal"
            and self.status == "abandoned"
            and not self.per_task
            and self.suite_score is None
            and self.decision is None
            and self.fit_seconds is None
            and self.score_seconds is None
            and self.wall_seconds is None
            and all(v == "not_run" for v in self.guards.values())
            and self.infrastructure_stop.run_id == self.run_id
            and self.infrastructure_stop.experiment_id == self.experiment_id
            and self.infrastructure_stop.calibration_sha256 == self.calibration_sha256
        ):
            raise ValueError("infrastructure stop requires an unmeasured abandoned proposal")
        if self.wall_seconds is None and not (
            (self.kind == "baseline" or self.infrastructure_stop is not None)
            and self.status == "abandoned"
            and not self.per_task
            and self.suite_score is None
            and self.fit_seconds is None
            and self.score_seconds is None
        ):
            raise ValueError(
                "unknown wall time requires unmeasured abandoned baseline or bound proposal stop"
            )
        if self.kind == "baseline":
            if (
                self.experiment_number is not None
                or self.baseline_name is None
                or self.calibration_sha256 is not None
                or self.predicted_delta is not None
                or self.decision is not None
            ):
                raise ValueError("baseline experiment cannot carry proposal or Referee fields")
        elif (
            self.experiment_number is None
            or self.baseline_name is not None
            or self.calibration_sha256 is None
            or self.predicted_delta is None
            or (self.decision is None and self.infrastructure_stop is None)
        ):
            raise ValueError("proposal experiment requires its registered identity and decision")
        return self


class TrajectoryDocument(BaseModel):
    """Final immutable trajectory.v1 record with digest-linked message blob."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal["trajectory.v1"] = Field(alias="schema")
    trajectory_id: Annotated[StrictStr, Field(pattern=r"^trj_[0-9a-f]{32}$")]
    run_id: UUID
    experiment_id: Annotated[StrictStr, Field(pattern=r"^exp_[0-9a-f]{32}$")]
    kind: ExperimentKind
    experiment_number: Annotated[StrictInt, Field(ge=1)] | None
    baseline_name: BaselineName | None
    calibration_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")] | None
    agent_version: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    model_id: Annotated[StrictStr, Field(min_length=1, max_length=256)]
    usage_profile: Literal["noncommercial_research"]
    source_provenance: tuple[SourceProvenance, ...]
    quantization: Annotated[StrictStr, Field(min_length=1, max_length=64)]
    adapter: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    system: SystemRole
    thinking: StrictBool
    temperature: Annotated[StrictFloat, Field(allow_inf_nan=False, ge=0.0, le=2.0)]
    top_p: Annotated[StrictFloat, Field(allow_inf_nan=False, gt=0.0, le=1.0)]
    context_template: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    inputs_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    messages_blob_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    tool_calls: Annotated[StrictInt, Field(ge=0, le=1_000)]
    outcome: ExperimentDecision | None
    quality_tier: Literal["bronze", "silver", "gold"]
    secrets_scrubbed: StrictBool
    people_scrubbed: StrictBool
    raw_values_scrubbed: StrictBool
    exclusions: tuple[Annotated[StrictStr, Field(min_length=1, max_length=256)], ...]
    provider_receipt: dict[StrictStr, object] | None = None
    replay_manifests: tuple[ReplayManifestDocument, ...] = ()
    terminal_replay: TerminalReplayDisposition | None = None

    infrastructure_stop: InfrastructureStopReceipt | AttemptedProposalStopReceipt | None = None

    @model_serializer(mode="wrap")
    def preserve_historical_bytes(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        value = cast(dict[str, Any], handler(self))
        if self.infrastructure_stop is None:
            value.pop("infrastructure_stop", None)
        return value

    @model_validator(mode="after")
    def validate_kind_identity(self) -> TrajectoryDocument:
        if self.infrastructure_stop is not None and not (
            self.kind == "proposal"
            and self.outcome is None
            and not self.replay_manifests
            and self.terminal_replay is None
            and self.infrastructure_stop.run_id == self.run_id
            and self.infrastructure_stop.experiment_id == self.experiment_id
            and self.infrastructure_stop.calibration_sha256 == self.calibration_sha256
        ):
            raise ValueError("infrastructure stop trajectory cannot invent evaluation evidence")
        if self.kind == "baseline":
            if (
                self.experiment_number is not None
                or self.baseline_name is None
                or self.calibration_sha256 is not None
                or self.outcome is not None
            ):
                raise ValueError("baseline trajectory cannot carry proposal or Referee identity")
        elif (
            self.experiment_number is None
            or self.baseline_name is not None
            or self.calibration_sha256 is None
            or (self.outcome is None and self.infrastructure_stop is None)
        ):
            raise ValueError("proposal trajectory requires its registered identity and outcome")
        if self.kind == "baseline" and self.replay_manifests:
            raise ValueError("baseline trajectory cannot carry Referee replay inputs")
        if self.kind == "baseline" and self.terminal_replay is not None:
            raise ValueError("baseline trajectory cannot carry a terminal decision disposition")
        if any(
            item.run_id != self.run_id or item.experiment_id != self.experiment_id
            for item in self.replay_manifests
        ) or len({item.decision_stage for item in self.replay_manifests}) != len(
            self.replay_manifests
        ):
            raise ValueError("trajectory replay manifest identity differs")
        if self.terminal_replay is not None and (
            self.terminal_replay.run_id != self.run_id
            or self.terminal_replay.experiment_id != self.experiment_id
        ):
            raise ValueError("trajectory terminal disposition identity differs")
        return self
