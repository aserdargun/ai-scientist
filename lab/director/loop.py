"""Resumable proposal loop over frozen measured calibration and terminal receipts."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_serializer,
)
from sqlalchemy import Engine, text

from harness.baselines import champion_noise_sd
from lab.director.artifacts import read_director_artifact, read_registered_calibration
from lab.director.baselines import FrozenCalibrationDocument, calibration_sha256
from lab.director.budget import (
    MAX_PROPOSALS,
    MAX_SEED_WALL_SECONDS,
    BudgetSnapshot,
    EpisodeReservation,
    RunBudget,
)
from lab.director.contracts import CandidateProposal, ExperimentDocument, MoveType
from lab.director.explore import ExploreEpisode, ExploreResolution, begin_explore, family_directive
from lab.director.fake_llm import AgentContext, FakeLLM, ProposalProvider, ProposalTurn
from lab.director.journal import DirectorRunLease, canonical_bytes
from lab.director.local_llm import ProviderOutputError
from lab.director.runner import (
    ExploreFamilyMismatch,
    PrimaryEvaluationResult,
    ProposalCompletionUnverified,
    RegisteredProposal,
    _candidate_git_tree,
    _read_measurement,
    _weighted_suite_seed_scores,
)
from lab.director.runner import (
    run_one_proposal as _run_proposal,
)
from lab.director.strategy import (
    StrategyOutcome,
    StrategySelection,
    StrategyState,
    initial_strategy_state,
    parse_strategy_state_json,
    resolve_strategy_selection,
    select_strategy_move,
    strategy_state_json,
    strategy_state_sha256,
    verify_strategy_state_sha256,
)
from lab.director.suite import SuiteTask
from lab.llm.router import route_system
from lab.sandbox.docker_runner import DEFAULT_SANDBOX_IMAGE, LocalDockerRunner


def _strategy_seed_for_run(run_id: UUID) -> int:
    """Pin the Thompson RNG seed to this immutable run identity."""
    digest = hashlib.sha256(b"swapp-director-thompson-seed-v1:" + run_id.bytes).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=False)


def _strategy_checkpoint_fields(strategy: StrategyState) -> dict[str, object]:
    """Serialize strategy state canonically and bind its policy and seed."""
    return {
        "schema": "director-loop-state.v2",
        "strategy_seed": strategy.seed,
        "strategy_policy_version": strategy.policy_version,
        "strategy_state_json": strategy_state_json(strategy).decode("utf-8"),
        "strategy_state_sha256": strategy_state_sha256(strategy),
    }


class DirectorLoopState(BaseModel):
    """Complete, hash-bound loop state stored after every terminal proposal."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal["director-loop-state.v1", "director-loop-state.v2"] = Field(
        alias="schema", serialization_alias="schema"
    )
    run_id: UUID
    suite_manifest_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    suite_id: StrictStr
    suite_version: StrictInt
    calibration_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    harness_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    image_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    proposal_limit: Annotated[StrictInt, Field(ge=1, le=MAX_PROPOSALS)]
    completed_proposals: Annotated[StrictInt, Field(ge=0, le=MAX_PROPOSALS)]
    next_ordinal: Annotated[StrictInt, Field(ge=1, le=MAX_PROPOSALS + 1)]
    champion_experiment_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    champion_source_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    champion_tree_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{40,64}$")]
    champion_source_blob_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    champion_seed0_by_task: dict[StrictStr, StrictFloat]
    champion_seed1_by_task: dict[StrictStr, StrictFloat]
    champion_suite_seed_scores: tuple[StrictFloat, StrictFloat, StrictFloat]
    champion_noise_sd: Annotated[StrictFloat, Field(ge=0, allow_inf_nan=False)]
    best_suite: StrictFloat
    consecutive_non_keep: Annotated[StrictInt, Field(ge=0)]
    explore_proposals: Annotated[StrictInt, Field(ge=0, le=10)]
    explore_family: StrictStr | None
    explore_episode: ExploreEpisode | None = None
    termination_reason: Literal["explore_exhausted"] | None = None
    loop_phase: Literal["HOLDOUT_CHECK"] | None = None

    @model_serializer(mode="wrap")
    def preserve_historical_state_bytes(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        value = cast(dict[str, Any], handler(self))
        if self.explore_episode is None:
            value.pop("explore_episode", None)
        if self.termination_reason is None:
            value.pop("termination_reason", None)
        if self.loop_phase is None:
            value.pop("loop_phase", None)
        return value

    consecutive_candidate_crashes: Annotated[StrictInt, Field(ge=0)]
    discard_streak: Annotated[StrictInt, Field(ge=0)] = 0
    previous_move_type: StrictStr | None
    recent_feedback: tuple[StrictStr, ...] = Field(max_length=30)
    budget: dict[StrictStr, object]
    holdout_approved_snapshot: HoldoutApprovedSnapshot | None = None
    holdout_applied_keep_count: Annotated[StrictInt, Field(ge=0)] = 0
    holdout_quota_exhausted: bool = False
    holdout_checks_passed: Annotated[StrictInt, Field(ge=0)] = 0
    holdout_last_status: Literal["not_run", "passed", "reverted", "quota_exhausted", "failed"] = (
        "not_run"
    )
    strategy_seed: StrictInt | None = None
    strategy_policy_version: Literal["beta-thompson-coverage-v1"] | None = None
    strategy_state_json: Annotated[StrictStr, Field(max_length=65_536)] | None = None
    strategy_state_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")] | None = None

    def verify_consistency(self) -> DirectorLoopState:
        if self.next_ordinal != self.completed_proposals + 1:
            raise ValueError("Director loop ordinal does not follow its durable proposal count")
        episode = self.explore_episode
        if episode is not None:
            if (
                episode.intent.run_id != self.run_id
                or self.next_ordinal != episode.intent.start_ordinal + len(episode.resolutions)
                or self.explore_proposals != len(episode.resolutions)
                or self.explore_family != episode.intent.target_family
                or episode.phase == "LOOP"
            ):
                raise ValueError("EXPLORE episode differs from its durable loop prefix")
        exhausted = episode is not None and episode.phase == "HOLDOUT_CHECK"
        if (self.termination_reason == "explore_exhausted") != exhausted or (
            self.loop_phase == "HOLDOUT_CHECK"
        ) != exhausted:
            raise ValueError("EXPLORE exhaustion must share the terminal loop state")
        if len(self.champion_seed0_by_task) == 0 or set(self.champion_seed0_by_task) != set(
            self.champion_seed1_by_task
        ):
            raise ValueError("champion raw seed-zero and seed-one vectors differ")
        values = (
            *self.champion_seed0_by_task.values(),
            *self.champion_seed1_by_task.values(),
            *self.champion_suite_seed_scores,
            self.champion_noise_sd,
            self.best_suite,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Director loop state contains non-finite measured values")
        strategy_fields = (
            self.strategy_seed,
            self.strategy_policy_version,
            self.strategy_state_json,
            self.strategy_state_sha256,
        )
        if self.schema_version == "director-loop-state.v1":
            if any(value is not None for value in strategy_fields):
                raise ValueError("legacy Director state cannot carry partial strategy fields")
        else:
            if any(value is None for value in strategy_fields):
                raise ValueError("Director state v2 requires a complete pinned strategy")
            strategy = self.load_strategy_state()
            if (
                strategy.seed != self.strategy_seed
                or strategy.policy_version != self.strategy_policy_version
                or self.strategy_seed != _strategy_seed_for_run(self.run_id)
                or strategy.completed_experiments != self.completed_proposals
                or strategy.selection_count
                != self.completed_proposals + int(strategy.pending is not None)
                or (strategy.pending is not None and strategy.pending.ordinal != self.next_ordinal)
            ):
                raise ValueError("Director strategy counters differ from the loop checkpoint")
        return self

    def load_strategy_state(self) -> StrategyState:
        if self.strategy_state_json is None or self.strategy_state_sha256 is None:
            raise ValueError("Director checkpoint has no Thompson strategy state")
        strategy = parse_strategy_state_json(self.strategy_state_json)
        return verify_strategy_state_sha256(strategy, self.strategy_state_sha256)


class HoldoutApprovedSnapshot(BaseModel):
    """Exact champion state last approved by the private holdout gate."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    experiment_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    source_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    tree_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{40,64}$")]
    source_blob_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    seed0_by_task: dict[StrictStr, StrictFloat]
    seed1_by_task: dict[StrictStr, StrictFloat]
    suite_seed_scores: tuple[StrictFloat, StrictFloat, StrictFloat]
    noise_sd: Annotated[StrictFloat, Field(ge=0, allow_inf_nan=False)]
    best_suite: StrictFloat
    periodic_keep_watermark: Annotated[StrictInt, Field(ge=0)]

    @classmethod
    def from_state(cls, state: DirectorLoopState, *, keep_count: int) -> HoldoutApprovedSnapshot:
        return cls(
            experiment_id=state.champion_experiment_id,
            source_sha256=state.champion_source_sha256,
            tree_sha256=state.champion_tree_sha256,
            source_blob_sha256=state.champion_source_blob_sha256,
            seed0_by_task=state.champion_seed0_by_task,
            seed1_by_task=state.champion_seed1_by_task,
            suite_seed_scores=state.champion_suite_seed_scores,
            noise_sd=state.champion_noise_sd,
            best_suite=state.best_suite,
            periodic_keep_watermark=keep_count,
        )

    def restore(self, state: DirectorLoopState, *, keep_count: int) -> DirectorLoopState:
        return state.model_copy(
            update={
                "champion_experiment_id": self.experiment_id,
                "champion_source_sha256": self.source_sha256,
                "champion_tree_sha256": self.tree_sha256,
                "champion_source_blob_sha256": self.source_blob_sha256,
                "champion_seed0_by_task": self.seed0_by_task,
                "champion_seed1_by_task": self.seed1_by_task,
                "champion_suite_seed_scores": self.suite_seed_scores,
                "champion_noise_sd": self.noise_sd,
                "best_suite": self.best_suite,
                "holdout_applied_keep_count": keep_count,
            }
        ).verify_consistency()


DirectorLoopState.model_rebuild(
    _types_namespace={"HoldoutApprovedSnapshot": HoldoutApprovedSnapshot}
)


class DirectorLoopResult(BaseModel):
    """Small CLI-facing summary; measurements remain in verified ledger records."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    run_id: UUID
    status: Literal[
        "proposal_limit_reached",
        "paused_candidate_crashes",
        "budget_exhausted",
        "explore_family_unverified",
    ]
    completed_proposals: StrictInt
    next_ordinal: StrictInt
    champion_experiment_id: StrictStr
    best_suite: StrictFloat
    checkpoint_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    termination_reason: Literal["explore_exhausted"] | None = None

    @model_serializer(mode="wrap")
    def preserve_historical_result_bytes(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        value = cast(dict[str, Any], handler(self))
        if self.termination_reason is None:
            value.pop("termination_reason", None)
        return value


def load_fake_provider(path: Path) -> FakeLLM:
    """Load proposal-only turns from a private strict JSON scenario file."""
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("fake provider scenario must be a private regular file")
    if path.stat().st_size > 32 * 1024**2:
        raise ValueError("fake provider scenario exceeds 32 MiB")
    document = json.loads(path.read_bytes())
    if (
        not isinstance(document, dict)
        or document.get("schema") != "review-fake-provider-input.v1"
        or not isinstance(document.get("proposals"), list)
    ):
        raise ValueError("fake provider scenario has an unsupported envelope")
    turns = tuple(
        ProposalTurn.model_validate_json(json.dumps(item, separators=(",", ":")), strict=True)
        for item in document["proposals"]
    )
    return FakeLLM(turns)


class DirectorLoop:
    """Run a bounded proposal sequence under the existing per-run owner lease."""

    def __init__(
        self,
        director_engine: Engine,
        planner_engine: Engine,
        *,
        lease: DirectorRunLease,
        runner: LocalDockerRunner,
        run_id: UUID,
        tasks: tuple[SuiteTask, ...],
        suite_manifest_sha256: str,
        provider: ProposalProvider,
        budget: RunBudget,
        artifact_root: Path,
        proposal_limit: int,
        seed_wall_seconds: int = MAX_SEED_WALL_SECONDS,
        holdout_evaluator: Callable[[str, Literal["keep_interval", "run_end"], int, int], object]
        | None = None,
        holdout_enabled: bool = False,
    ) -> None:
        if not 1 <= proposal_limit <= MAX_PROPOSALS:
            raise ValueError("proposal limit must be in 1..35")
        if not 1 <= seed_wall_seconds <= MAX_SEED_WALL_SECONDS:
            raise ValueError("seed deadline must be in 1..600")
        if len(suite_manifest_sha256) != 64:
            raise ValueError("suite manifest digest must be SHA-256")
        self.director_engine = director_engine
        self.planner_engine = planner_engine
        self.lease = lease
        self.runner = runner
        self.run_id = run_id
        self.tasks = tasks
        self.suite_manifest_sha256 = suite_manifest_sha256
        self.provider = provider
        self.budget = budget
        self.artifact_root = artifact_root
        self.proposal_limit = proposal_limit
        self.seed_wall_seconds = seed_wall_seconds
        self.holdout_evaluator = holdout_evaluator
        self.holdout_enabled = holdout_enabled or holdout_evaluator is not None

    def run(self) -> DirectorLoopResult:
        self.lease.heartbeat()
        calibration = read_registered_calibration(
            self.director_engine, run_id=self.run_id, artifact_root=self.artifact_root
        )
        self._verify_execution_identity(calibration)
        from lab.director.holdout_replay import recover_observed_terminal_holdouts

        recover_observed_terminal_holdouts(
            self.director_engine, self.lease, artifact_root=self.artifact_root
        )
        state, state_checkpoint = self._load_or_initialize(calibration)
        terminal_run_end = self._pending_run_end_terminal_result(state, state_checkpoint)
        if terminal_run_end is not None:
            return terminal_run_end
        self._recover_unresolved_holdout_budget()
        # Reconcile an already committed periodic receipt before checking the
        # proposal limit. A restart at KEEP 10/20/30 must apply its result even
        # when the configured limit has been reached.
        state, state_checkpoint = self._reconcile_periodic_holdout(state, state_checkpoint)
        while state.completed_proposals < self.proposal_limit:
            self.lease.heartbeat()
            ordinal_reconciled = self.lease.read_checkpoint(
                key=f"proposal-budget-reconciled:{self.run_id}:{state.next_ordinal}",
                artifact_root=self.artifact_root,
            )
            if ordinal_reconciled is not None:
                reconciled_payload = ordinal_reconciled.get("payload")
                abandonment_key = f"proposal-abandoned:{state.next_ordinal}"
                reason_code = (
                    reconciled_payload.get("reason_code")
                    if isinstance(reconciled_payload, dict)
                    else None
                )
                if (
                    isinstance(reconciled_payload, dict)
                    and reason_code in {"model_request", "budget_exhausted", "provider_error"}
                    and self.lease.read_checkpoint(
                        key=abandonment_key, artifact_root=self.artifact_root
                    )
                    is None
                ):
                    state, state_checkpoint = self._preserve_pending_strategy_after_failure(
                        state, ordinal=state.next_ordinal
                    )
                    raise RuntimeError(
                        "provider infrastructure failed with a durable pending strategy intent"
                    )
                if (
                    isinstance(reconciled_payload, dict)
                    and reason_code in {"tool_parse", "explore_family_mismatch"}
                    and self.lease.read_checkpoint(
                        key=abandonment_key, artifact_root=self.artifact_root
                    )
                    is None
                ):
                    intent = self.lease.read_checkpoint(
                        key=f"proposal-intent:{state.next_ordinal}",
                        artifact_root=self.artifact_root,
                    )
                    intent_payload = intent.get("payload") if intent is not None else None
                    if not isinstance(intent_payload, dict):
                        raise RuntimeError("tool-parse recovery has no immutable proposal intent")
                    recovered_move = intent_payload.get("move_type")
                    recovered_explore = self._is_explore(state)
                    self.lease.append_checkpoint(
                        sequence=self._next_checkpoint_sequence(),
                        key=abandonment_key,
                        phase="proposal_abandoned",
                        payload={
                            "run_id": str(self.run_id),
                            "ordinal": state.next_ordinal,
                            "reason": reconciled_payload.get("reason_code"),
                            "move_type": recovered_move,
                            "is_explore": recovered_explore,
                            "provider_receipt": reconciled_payload.get("provider_receipt"),
                            "response_blob_sha256": reconciled_payload.get("response_blob_sha256"),
                            "attempt_transcripts_blob_sha256": reconciled_payload.get(
                                "attempt_transcripts_blob_sha256"
                            ),
                            "error_type": reconciled_payload.get("error_type"),
                            "budget": reconciled_payload.get("budget"),
                        },
                        artifact_root=self.artifact_root,
                    )
            recovered_abandonment = self.lease.read_checkpoint(
                key=f"proposal-abandoned:{state.next_ordinal}",
                artifact_root=self.artifact_root,
            )
            if recovered_abandonment is not None:
                payload = recovered_abandonment.get("payload")
                if (
                    not isinstance(payload, dict)
                    or payload.get("run_id") != str(self.run_id)
                    or payload.get("ordinal") != state.next_ordinal
                    or payload.get("reason") not in {"tool_parse", "explore_family_mismatch"}
                    or not isinstance(payload.get("is_explore"), bool)
                ):
                    raise RuntimeError("durable abandoned episode identity is invalid")
                recovered_move = payload.get("move_type")
                state = self._advance_tool_parse_abandonment(
                    state,
                    ordinal=state.next_ordinal,
                    move_type=recovered_move if isinstance(recovered_move, str) else None,
                    is_explore=payload["is_explore"],
                    reason=str(payload["reason"]),
                )
                state_checkpoint = self._persist_state(state)
                continue
            recovered_terminal = self._recover_committed_terminal(
                state, state_checkpoint, calibration
            )
            if recovered_terminal is not None:
                state, state_checkpoint, promoted = recovered_terminal
                if promoted:
                    state, state_checkpoint = self._apply_periodic_holdout(state, state_checkpoint)
                continue
            state, state_checkpoint = self._reconcile_periodic_holdout(state, state_checkpoint)
            if state.consecutive_candidate_crashes >= 5:
                return self._result(state, state_checkpoint, "paused_candidate_crashes")
            from lab.director.parameter_grid import ParameterGridProvider

            if self.budget.remaining_wall_seconds < 1 or (
                self.budget.remaining_model_tokens < 1
                and not isinstance(self.provider, ParameterGridProvider)
            ):
                return self._result(state, state_checkpoint, "budget_exhausted")
            ordinal = state.next_ordinal
            is_explore = self._is_explore(state)
            if is_explore and self.proposal_limit < 25 + state.explore_proposals + 1:
                # A deliberately short smoke run is not allowed to claim EXPLORE.
                break
            if is_explore:
                try:
                    state, state_checkpoint = self._prepare_explore_episode(state, state_checkpoint)
                except ValueError as error:
                    if str(error) != "explore_family_unverified":
                        raise
                    return self._result(state, state_checkpoint, "explore_family_unverified")
            state, state_checkpoint, selection, system = self._prepare_strategy_intent(
                state, ordinal=ordinal, is_explore=is_explore
            )
            move_intent = selection.move_type
            cards = self._task_cards(state, calibration)
            context = AgentContext(
                phase="explore" if is_explore else "proposal",
                experiment_number=ordinal,
                system=system,
                move_type=move_intent,
                task_cards=cards,
                champion_source=self._champion_source(state).decode("utf-8"),
                recent_feedback=state.recent_feedback,
                explore_intent=state.explore_episode.intent
                if is_explore and state.explore_episode
                else None,
                explore_directive=family_directive(state.explore_episode.intent)
                if is_explore and state.explore_episode
                else None,
            )
            try:
                proposal = self._run_one(
                    state=state,
                    calibration=calibration,
                    ordinal=ordinal,
                    system=system,
                    context=context,
                )
            except (ProviderOutputError, ExploreFamilyMismatch) as error:
                self.lease.require_run_active()
                if error.reason_code not in {"tool_parse", "explore_family_mismatch"}:
                    state, state_checkpoint = self._preserve_pending_strategy_after_failure(
                        state, ordinal=ordinal
                    )
                    raise RuntimeError(
                        f"provider infrastructure failed ({error.reason_code}); "
                        "durable Thompson intent remains unresolved"
                    ) from error
                from lab.director.artifacts import store_director_artifact

                response_blob = None
                if error.response_text:
                    response_blob = store_director_artifact(
                        error.response_text.encode("utf-8"),
                        artifact_root=self.artifact_root,
                    )
                transcripts_blob = store_director_artifact(
                    json.dumps(
                        [item.model_dump(mode="json") for item in error.attempt_transcripts],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8"),
                    artifact_root=self.artifact_root,
                )
                self.lease.append_checkpoint(
                    sequence=self._next_checkpoint_sequence(),
                    key=f"proposal-abandoned:{ordinal}",
                    phase="proposal_abandoned",
                    payload={
                        "run_id": str(self.run_id),
                        "ordinal": ordinal,
                        "reason": error.reason_code,
                        "move_type": move_intent,
                        "is_explore": is_explore,
                        "provider_receipt": error.provider_receipt.model_dump(mode="json")
                        if error.provider_receipt is not None
                        else None,
                        "response_blob_sha256": response_blob,
                        "attempt_transcripts_blob_sha256": transcripts_blob,
                        "error_type": type(error).__name__,
                        "budget": _budget_to_json(self.budget.snapshot()),
                    },
                    artifact_root=self.artifact_root,
                )
                state = self._advance_tool_parse_abandonment(
                    state,
                    ordinal=ordinal,
                    move_type=move_intent,
                    is_explore=is_explore,
                    reason=error.reason_code,
                )
                state_checkpoint = self._persist_state(state)
                continue
            except Exception as error:
                # The provider or proposal adapter did not produce a durable
                # candidate outcome. Preserve the pending move and consumed
                # budget so recovery cannot award a posterior update or resample.
                self._preserve_pending_strategy_after_failure(state, ordinal=ordinal)
                if isinstance(error, ProposalCompletionUnverified):
                    raise
                raise RuntimeError(
                    "proposal infrastructure failed with unresolved Thompson intent"
                ) from error
            receipt = self._read_terminal_document(proposal.experiment_id)
            updated = self._advance_state(state, proposal, receipt, calibration, is_explore)
            promoted = (
                receipt.decision is not None
                and receipt.decision.verdict in {"KEEP", "KEEP_SIMPLER"}
                and receipt.status == "scored"
            )
            state_checkpoint = self._persist_state(updated)
            state = updated
            if promoted:
                state, state_checkpoint = self._apply_periodic_holdout(state, state_checkpoint)
        return self._result(state, state_checkpoint, "proposal_limit_reached")

    def apply_run_end_holdout(
        self, result: DirectorLoopResult, receipt: object
    ) -> DirectorLoopResult:
        """Durably apply the final one-bit result before sealing/finalization."""
        from lab.director.holdout import HoldoutReceipt

        if not isinstance(receipt, HoldoutReceipt):
            raise RuntimeError("run-end holdout did not produce a terminal result")
        state_key = self._state_key_for_digest(result.checkpoint_sha256)
        stored = self.lease.read_checkpoint(key=state_key, artifact_root=self.artifact_root)
        if stored is None:
            raise RuntimeError("run-end state checkpoint disappeared")
        state = DirectorLoopState.model_validate_json(
            json.dumps(stored["payload"], separators=(",", ":")), strict=True
        ).verify_consistency()
        snapshot = state.holdout_approved_snapshot
        if snapshot is None:
            raise RuntimeError("run-end state has no approved champion snapshot")
        if receipt.state not in {"passed", "reverted", "exhausted", "failed"}:
            raise RuntimeError("run-end holdout did not produce a terminal result")
        next_state, checkpoint = self._record_holdout_application(
            state,
            stored["receipt"],
            receipt,
            trigger_kind="run_end",
            trigger_index=1,
            candidate_id=state.champion_experiment_id,
        )
        digest = checkpoint.get("payload_sha256")
        if not isinstance(digest, str):
            raise RuntimeError("final holdout state checkpoint has no digest")
        return result.model_copy(
            update={
                "champion_experiment_id": next_state.champion_experiment_id,
                "best_suite": next_state.best_suite,
                "checkpoint_sha256": digest,
            }
        )

    def apply_run_end_admission_failure(
        self, result: DirectorLoopResult, failure: object
    ) -> DirectorLoopResult:
        """Apply the durable bitless fence when run-end admission cannot be proven."""
        from lab.director.holdout import RunEndAdmissionFailure
        from lab.director.ownership import active_execution_owner

        if not isinstance(failure, RunEndAdmissionFailure):
            raise TypeError("run-end failure application requires its typed durable fence")
        if failure.run_id != self.run_id:
            raise ValueError("run-end admission failure belongs to another run")
        owner = active_execution_owner()
        if (
            owner is None
            or owner.run_id != self.run_id
            or failure.admitted_generation != owner.generation
            or failure.execution_sha256 != owner.execution_sha256
        ):
            raise RuntimeError("run-end failure differs from the captured execution owner")
        state_key = self._state_key_for_digest(result.checkpoint_sha256)
        checkpoint = self.lease.read_checkpoint(key=state_key, artifact_root=self.artifact_root)
        if checkpoint is None or not isinstance(checkpoint.get("payload"), dict):
            raise RuntimeError("run-end failure prior state checkpoint is unavailable")
        state = DirectorLoopState.model_validate_json(
            json.dumps(
                checkpoint["payload"],
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
            strict=True,
        ).verify_consistency()
        state_receipt = checkpoint.get("receipt")
        if not isinstance(state_receipt, dict):
            raise RuntimeError("run-end failure prior state has no checkpoint receipt")
        next_state, next_checkpoint = self._record_run_end_admission_failure(
            state, state_receipt, failure
        )
        digest = next_checkpoint.get("payload_sha256")
        if not isinstance(digest, str):
            raise RuntimeError("run-end failure state has no durable digest")
        return result.model_copy(
            update={
                "champion_experiment_id": next_state.champion_experiment_id,
                "best_suite": next_state.best_suite,
                "checkpoint_sha256": digest,
            }
        )

    def check_run_end_holdout(self, result: DirectorLoopResult) -> DirectorLoopResult:
        """Reserve budget, evaluate, reconcile, and apply the immutable final check."""
        from lab.director.holdout import (
            HoldoutReceipt,
            check_at_run_end,
            fence_missing_run_end_admission,
            fence_run_end_unavailable,
            read_holdout_request,
            read_run_end_admission_failure,
            read_run_end_admission_registration,
            read_run_end_unavailable,
            recover_existing_holdout_request,
        )

        restored = self.restore_run_end_holdout(result)
        if restored is not None:
            return restored
        existing_unavailable = read_run_end_unavailable(self.director_engine, run_id=self.run_id)
        if existing_unavailable is not None:
            return self.apply_run_end_unavailable(result, existing_unavailable)
        existing_failure = read_run_end_admission_failure(self.director_engine, run_id=self.run_id)
        if existing_failure is not None:
            return self.apply_run_end_admission_failure(result, existing_failure)
        intent = self.lease.read_checkpoint(
            key="director-holdout-run-end-intent", artifact_root=self.artifact_root
        )
        existing = read_holdout_request(
            self.director_engine,
            run_id=self.run_id,
            candidate_experiment_id=result.champion_experiment_id,
            trigger_kind="run_end",
            trigger_index=1,
        )
        if existing is not None:
            if existing.state not in {"passed", "reverted", "failed", "exhausted"}:
                if existing.state not in {"reserved", "running"}:
                    raise RuntimeError(
                        f"run-end holdout has an unsupported operational state: {existing.state}"
                    )
                self._recover_unresolved_holdout_budget()
                existing = recover_existing_holdout_request(
                    self.director_engine,
                    run_id=self.run_id,
                    candidate_experiment_id=result.champion_experiment_id,
                    trigger_kind="run_end",
                    trigger_index=1,
                    remaining_seconds=30,
                )
                if existing.state not in {"passed", "reverted", "failed", "exhausted"}:
                    raise RuntimeError(
                        f"run-end holdout recovery remains pending: {existing.state}"
                    )
            self._recover_unresolved_holdout_budget()
            return self.apply_run_end_holdout(result, existing)
        reservation_checkpoint = self._run_end_reservation_checkpoint()
        if intent is None and reservation_checkpoint is not None:
            reservation_payload = reservation_checkpoint.get("payload")
            reservation_receipt = reservation_checkpoint.get("receipt")
            reservation_id = (
                reservation_payload.get("reservation_id")
                if isinstance(reservation_payload, dict)
                else None
            )
            if (
                not isinstance(reservation_id, str)
                or not isinstance(reservation_receipt, dict)
                or cast(dict[str, object], reservation_payload).get("candidate_experiment_id")
                != result.champion_experiment_id
            ):
                raise RuntimeError("run-end reservation does not match the terminal candidate")
            # A durable budget reservation means this exact check was already
            # admitted locally. Reconcile it conservatively; never reserve again.
            reconciliation_key = f"holdout-budget-reconciled:{reservation_id}"
            reconciliation = self.lease.read_checkpoint(
                key=reconciliation_key, artifact_root=self.artifact_root
            )
            for _ in range(32):
                if reconciliation is not None:
                    break
                self._recover_unresolved_holdout_budget()
                reconciliation = self.lease.read_checkpoint(
                    key=reconciliation_key, artifact_root=self.artifact_root
                )
            if reconciliation is None:
                raise RuntimeError("run-end reservation budget could not be reconciled")
            reconciliation_payload = reconciliation.get("payload")
            if (
                not isinstance(reconciliation_payload, dict)
                or cast(dict[str, object], reconciliation_payload).get("trigger_kind") != "run_end"
                or cast(dict[str, object], reconciliation_payload).get("trigger_index") != 1
                or cast(dict[str, object], reconciliation_payload).get("candidate_experiment_id")
                != result.champion_experiment_id
                or cast(dict[str, object], reconciliation_payload).get("reservation_id")
                != reservation_id
            ):
                raise RuntimeError("run-end reservation reconciliation identity is invalid")
            budget_snapshot = reconciliation_payload.get("budget")
            if not isinstance(budget_snapshot, dict):
                raise RuntimeError("run-end reservation reconciliation has no budget snapshot")
            self.budget.restore(_budget_from_json(budget_snapshot))
            failure = self._fence_run_end_unavailable(
                result,
                reason="reservation_checkpoint_without_sql_intent",
                remaining_wall_seconds=float(self.budget.remaining_wall_seconds),
                original_intent=None,
                fence=fence_run_end_unavailable,
                reservation_checkpoint=reservation_checkpoint,
                reconciliation_checkpoint=reconciliation,
            )
            return self.apply_run_end_unavailable(result, failure)
        if intent is not None:
            # The intent forbids a fresh query. If its reservation never reached
            # Scorer, conservatively charge any unresolved wall bound and persist
            # a durable bitless failure fence for deterministic restart handling.
            self._recover_unresolved_holdout_budget()
            intent_receipt = intent.get("receipt")
            intent_payload = intent.get("payload")
            if (
                not isinstance(intent_receipt, dict)
                or not isinstance(intent_payload, dict)
                or intent_receipt.get("phase") != "holdout_run_end_intent"
                or intent_payload.get("run_id") != str(self.run_id)
                or intent_payload.get("candidate_experiment_id") != result.champion_experiment_id
            ):
                raise RuntimeError("run-end intent identity cannot authorize a failure fence")
            intent_sha = intent_receipt.get("payload_sha256")
            intent_sequence = intent_receipt.get("sequence")
            if (
                not isinstance(intent_sha, str)
                or isinstance(intent_sequence, bool)
                or not isinstance(intent_sequence, int)
            ):
                raise RuntimeError("run-end intent receipt has no durable identity")
            intent_registered, reservation_exists = read_run_end_admission_registration(
                self.director_engine, run_id=self.run_id
            )
            if reservation_exists:
                raise RuntimeError("run-end reservation exists without an authoritative receipt")
            if not intent_registered:
                failure = self._fence_run_end_unavailable(
                    result,
                    reason="intent_checkpoint_without_sql_registration",
                    remaining_wall_seconds=float(self.budget.remaining_wall_seconds),
                    original_intent=intent,
                    fence=fence_run_end_unavailable,
                )
                return self.apply_run_end_unavailable(result, failure)
            budget_checkpoint = self._latest_reconciled_run_end_budget()
            if budget_checkpoint is None:
                raise RuntimeError("run-end intent has no conservatively reconciled budget")
            budget_receipt = budget_checkpoint.get("receipt")
            budget_payload = budget_checkpoint.get("payload")
            if not isinstance(budget_receipt, dict) or not isinstance(budget_payload, dict):
                raise RuntimeError("run-end charged budget checkpoint is malformed")
            budget_key = budget_receipt.get("key")
            budget_sha = budget_receipt.get("payload_sha256")
            if not isinstance(budget_key, str) or not isinstance(budget_sha, str):
                raise RuntimeError("run-end charged budget checkpoint has no verified receipt")
            failure = fence_missing_run_end_admission(
                self.director_engine,
                run_id=self.run_id,
                intent_checkpoint_sha256=intent_sha,
                intent_checkpoint_sequence=intent_sequence,
                budget_checkpoint_key=budget_key,
                budget_checkpoint_sha256=budget_sha,
            )
            if (
                failure.candidate_experiment_id != result.champion_experiment_id
                or failure.intent_checkpoint_sha256 != intent_sha
                or failure.intent_checkpoint_sequence != intent_sequence
                or failure.budget_checkpoint_key != budget_key
                or failure.budget_checkpoint_sha256 != budget_sha
            ):
                raise RuntimeError("run-end failure fence differs from its local receipts")
            return self.apply_run_end_admission_failure(result, failure)
        remaining = int(self.budget.remaining_wall_seconds)
        if remaining < 1:
            failure = self._fence_run_end_unavailable(
                result,
                reason="fresh_wall_budget_unavailable",
                remaining_wall_seconds=float(self.budget.remaining_wall_seconds),
                original_intent=None,
                fence=fence_run_end_unavailable,
            )
            return self.apply_run_end_unavailable(result, failure)
        reservation = self.budget.reserve_work(wall_seconds=min(600, remaining), model_tokens=0)
        from lab.director.ownership import active_execution_owner

        owner = active_execution_owner()
        if owner is None or owner.run_id != self.run_id:
            raise RuntimeError("run-end budget reservation lacks the captured execution owner")
        self.lease.append_checkpoint(
            sequence=self._next_checkpoint_sequence(),
            key=f"holdout-budget-reserved:{reservation.reservation_id}",
            phase="holdout_budget_reserved",
            payload={
                "run_id": str(self.run_id),
                "trigger_kind": "run_end",
                "trigger_index": 1,
                "candidate_experiment_id": result.champion_experiment_id,
                "reservation_id": str(reservation.reservation_id),
                "wall_seconds": reservation.wall_seconds,
                "model_tokens": reservation.model_tokens,
                "admitted_generation": owner.generation,
                "execution_sha256": owner.execution_sha256,
                "budget": _budget_to_json(self.budget.snapshot()),
            },
            artifact_root=self.artifact_root,
        )
        state_key = self._state_key_for_digest(result.checkpoint_sha256)
        state_checkpoint = self.lease.read_checkpoint(
            key=state_key, artifact_root=self.artifact_root
        )
        if state_checkpoint is None or not isinstance(state_checkpoint.get("payload"), dict):
            raise RuntimeError("run-end holdout state checkpoint is unavailable")
        state_payload = state_checkpoint["payload"]
        state = DirectorLoopState.model_validate_json(
            json.dumps(state_payload, sort_keys=True, separators=(",", ":"), allow_nan=False),
            strict=True,
        ).verify_consistency()
        state = state.model_copy(update={"budget": _budget_to_json(self.budget.snapshot())})
        reserved_state_checkpoint = self._persist_state(
            state, checkpoint_tag="run_end_budget_reserved"
        )
        prepared_result = self._result(state, reserved_state_checkpoint, result.status)
        started = time.monotonic()
        try:
            receipt = check_at_run_end(
                self.director_engine,
                run_id=self.run_id,
                lease=self.lease,
                loop_result=prepared_result,
                artifact_root=self.artifact_root,
                remaining_seconds=reservation.wall_seconds,
            )
        except BaseException:
            # Preserve the unresolved reservation for conservative restart recovery.
            raise
        elapsed = max(0.0, time.monotonic() - started)
        self._reconcile_holdout_budget(
            reservation, elapsed, trigger_kind="run_end", trigger_index=1
        )
        reconciled_state = state.model_copy(
            update={"budget": _budget_to_json(self.budget.snapshot())}
        )
        reconciled_checkpoint = self._persist_state(
            reconciled_state, checkpoint_tag="run_end_budget_reconciled"
        )
        reconciled_result = self._result(reconciled_state, reconciled_checkpoint, result.status)
        if not isinstance(receipt, HoldoutReceipt):
            raise RuntimeError("run-end holdout did not produce a typed terminal receipt")
        return self.apply_run_end_holdout(reconciled_result, receipt)

    def _fence_run_end_unavailable(
        self,
        result: DirectorLoopResult,
        *,
        reason: str,
        remaining_wall_seconds: float,
        original_intent: dict[str, object] | None,
        fence: Callable[..., object],
        reservation_checkpoint: dict[str, object] | None = None,
        reconciliation_checkpoint: dict[str, object] | None = None,
    ) -> object:
        """Freeze exact prior state/budget identity, then install the SQL fence."""
        from lab.director.ownership import active_execution_owner

        captured_owner = active_execution_owner()
        if captured_owner is None or captured_owner.run_id != self.run_id:
            raise RuntimeError("run-end unavailable fence lacks the captured execution owner")
        state_key = self._state_key_for_digest(result.checkpoint_sha256)
        state_checkpoint = self.lease.read_checkpoint(
            key=state_key, artifact_root=self.artifact_root
        )
        if state_checkpoint is None or not isinstance(state_checkpoint.get("payload"), dict):
            raise RuntimeError("run-end unavailable prior state checkpoint is missing")
        state_receipt = state_checkpoint.get("receipt")
        if not isinstance(state_receipt, dict):
            raise RuntimeError("run-end unavailable prior state receipt is malformed")
        state = DirectorLoopState.model_validate_json(
            json.dumps(state_checkpoint["payload"], sort_keys=True, separators=(",", ":")),
            strict=True,
        ).verify_consistency()
        prior_sha = state_receipt.get("payload_sha256")
        prior_sequence = state_receipt.get("sequence")
        if (
            state.run_id != self.run_id
            or state.champion_experiment_id != result.champion_experiment_id
            or not isinstance(prior_sha, str)
            or prior_sha != result.checkpoint_sha256
            or isinstance(prior_sequence, bool)
            or not isinstance(prior_sequence, int)
        ):
            raise RuntimeError("run-end unavailable prior state differs from terminal result")
        budget_snapshot = _budget_to_json(self.budget.snapshot())
        if reconciliation_checkpoint is not None:
            charged_payload = reconciliation_checkpoint.get("payload")
            charged_budget = (
                charged_payload.get("budget") if isinstance(charged_payload, dict) else None
            )
            if not isinstance(charged_budget, dict):
                raise RuntimeError("run-end reconciliation has no immutable budget snapshot")
            budget_snapshot = charged_budget
        budget_sha = self._budget_snapshot_sha256(budget_snapshot)
        original_sha: str | None = None
        original_sequence: int | None = None
        if original_intent is not None:
            intent_receipt = original_intent.get("receipt")
            intent_payload = original_intent.get("payload")
            if (
                not isinstance(intent_receipt, dict)
                or not isinstance(intent_payload, dict)
                or intent_receipt.get("phase") != "holdout_run_end_intent"
                or intent_payload.get("run_id") != str(self.run_id)
                or intent_payload.get("candidate_experiment_id") != state.champion_experiment_id
                or intent_payload.get("admitted_generation") != captured_owner.generation
                or intent_payload.get("execution_sha256") != captured_owner.execution_sha256
            ):
                raise RuntimeError("original run-end intent does not bind the prior state")
            original_sha = intent_receipt.get("payload_sha256")
            original_sequence = intent_receipt.get("sequence")
            if (
                not isinstance(original_sha, str)
                or isinstance(original_sequence, bool)
                or not isinstance(original_sequence, int)
            ):
                raise RuntimeError("original run-end intent receipt is malformed")
        frozen_key = "director-holdout-run-end-unavailable-intent:1"
        frozen = self.lease.read_checkpoint(key=frozen_key, artifact_root=self.artifact_root)
        identity: dict[str, object] = {
            "schema": "director-run-end-unavailable-intent.v1",
            "run_id": str(self.run_id),
            "candidate_experiment_id": state.champion_experiment_id,
            "reason": reason,
            "terminal_status": result.status,
            "prior_state_key": state_key,
            "prior_state_sha256": prior_sha,
            "prior_state_sequence": prior_sequence,
            "budget_snapshot": budget_snapshot,
            "budget_snapshot_sha256": budget_sha,
            "remaining_wall_seconds": remaining_wall_seconds,
            "original_intent_checkpoint_sha256": original_sha,
            "original_intent_checkpoint_sequence": original_sequence,
            "admitted_generation": captured_owner.generation,
            "execution_sha256": captured_owner.execution_sha256,
        }
        reservation_receipt: object = None
        reconciliation_receipt: object = None
        if reason == "reservation_checkpoint_without_sql_intent":
            if reservation_checkpoint is None or reconciliation_checkpoint is None:
                raise RuntimeError("reservation-only closure lacks durable budget receipts")
            reservation_receipt = reservation_checkpoint.get("receipt")
            reservation_payload = reservation_checkpoint.get("payload")
            reconciliation_receipt = reconciliation_checkpoint.get("receipt")
            reconciliation_payload = reconciliation_checkpoint.get("payload")
            reservation_key = (
                reservation_receipt.get("key") if isinstance(reservation_receipt, dict) else None
            )
            reservation_sha = (
                reservation_receipt.get("payload_sha256")
                if isinstance(reservation_receipt, dict)
                else None
            )
            reservation_sequence = (
                reservation_receipt.get("sequence")
                if isinstance(reservation_receipt, dict)
                else None
            )
            reconciliation_key = (
                reconciliation_receipt.get("key")
                if isinstance(reconciliation_receipt, dict)
                else None
            )
            reconciliation_sha = (
                reconciliation_receipt.get("payload_sha256")
                if isinstance(reconciliation_receipt, dict)
                else None
            )
            reconciliation_sequence = (
                reconciliation_receipt.get("sequence")
                if isinstance(reconciliation_receipt, dict)
                else None
            )
            reservation_id = (
                reservation_payload.get("reservation_id")
                if isinstance(reservation_payload, dict)
                else None
            )
            if (
                not isinstance(reservation_key, str)
                or not isinstance(reservation_sha, str)
                or isinstance(reservation_sequence, bool)
                or not isinstance(reservation_sequence, int)
                or not isinstance(reconciliation_key, str)
                or not isinstance(reconciliation_sha, str)
                or isinstance(reconciliation_sequence, bool)
                or not isinstance(reconciliation_sequence, int)
                or not isinstance(reservation_id, str)
                or reservation_key != f"holdout-budget-reserved:{reservation_id}"
                or reconciliation_key != f"holdout-budget-reconciled:{reservation_id}"
                or cast(dict[str, object], reservation_payload).get("candidate_experiment_id")
                != state.champion_experiment_id
                or cast(dict[str, object], reservation_payload).get("admitted_generation")
                != captured_owner.generation
                or cast(dict[str, object], reservation_payload).get("execution_sha256")
                != captured_owner.execution_sha256
                or cast(dict[str, object], reconciliation_payload).get("candidate_experiment_id")
                != state.champion_experiment_id
            ):
                raise RuntimeError(
                    "reservation-only closure receipts do not bind the current run end"
                )
            identity.update(
                {
                    "reservation_checkpoint_key": reservation_key,
                    "reservation_checkpoint_sha256": reservation_sha,
                    "reservation_checkpoint_sequence": reservation_sequence,
                    "reconciliation_checkpoint_key": reconciliation_key,
                    "reconciliation_checkpoint_sha256": reconciliation_sha,
                    "reconciliation_checkpoint_sequence": reconciliation_sequence,
                }
            )
        if frozen is None:
            frozen_receipt = self.lease.append_holdout_closure_checkpoint(
                sequence=self._next_checkpoint_sequence(),
                key=frozen_key,
                phase="holdout_run_end_unavailable_intent",
                payload=identity,
                artifact_root=self.artifact_root,
            )
        else:
            frozen_payload = frozen.get("payload")
            existing_frozen_receipt = frozen.get("receipt")
            if (
                not isinstance(frozen_payload, dict)
                or not isinstance(existing_frozen_receipt, dict)
                or existing_frozen_receipt.get("phase") != "holdout_run_end_unavailable_intent"
                or any(
                    frozen_payload.get(name) != identity[name]
                    for name in (
                        "schema",
                        "run_id",
                        "candidate_experiment_id",
                        "reason",
                        "terminal_status",
                        "prior_state_key",
                        "prior_state_sha256",
                        "prior_state_sequence",
                        "original_intent_checkpoint_sha256",
                        "original_intent_checkpoint_sequence",
                        "admitted_generation",
                        "execution_sha256",
                        "reservation_checkpoint_key",
                        "reservation_checkpoint_sha256",
                        "reservation_checkpoint_sequence",
                        "reconciliation_checkpoint_key",
                        "reconciliation_checkpoint_sha256",
                        "reconciliation_checkpoint_sequence",
                    )
                )
                or not isinstance(frozen_payload.get("budget_snapshot"), dict)
                or self._budget_snapshot_sha256(frozen_payload["budget_snapshot"])
                != frozen_payload.get("budget_snapshot_sha256")
            ):
                raise RuntimeError("run-end unavailable intent conflicts with its frozen receipt")
            # A crash after freeze cannot recalculate or enlarge its budget.
            identity = frozen_payload
            frozen_receipt = existing_frozen_receipt
            budget_snapshot = frozen_payload["budget_snapshot"]
            budget_sha = str(frozen_payload["budget_snapshot_sha256"])
            remaining_wall_seconds = float(frozen_payload["remaining_wall_seconds"])
            original_sha = frozen_payload.get("original_intent_checkpoint_sha256")
            original_sequence = frozen_payload.get("original_intent_checkpoint_sequence")
        checkpoint_sha = frozen_receipt.get("payload_sha256")
        checkpoint_sequence = frozen_receipt.get("sequence")
        if (
            not isinstance(checkpoint_sha, str)
            or isinstance(checkpoint_sequence, bool)
            or not isinstance(checkpoint_sequence, int)
        ):
            raise RuntimeError("run-end unavailable intent lacks a durable checkpoint identity")
        if reason == "reservation_checkpoint_without_sql_intent":
            from lab.director.holdout import fence_run_end_reserved_budget_without_intent

            if reservation_receipt is None or reconciliation_receipt is None:
                # Re-read immutable receipts after a retry through an already frozen checkpoint.
                frozen_payload = identity
                reservation_checkpoint = self.lease.read_checkpoint(
                    key=str(frozen_payload["reservation_checkpoint_key"]),
                    artifact_root=self.artifact_root,
                )
                reconciliation_checkpoint = self.lease.read_checkpoint(
                    key=str(frozen_payload["reconciliation_checkpoint_key"]),
                    artifact_root=self.artifact_root,
                )
                reservation_receipt = (
                    reservation_checkpoint.get("receipt") if reservation_checkpoint else None
                )
                reconciliation_receipt = (
                    reconciliation_checkpoint.get("receipt") if reconciliation_checkpoint else None
                )
            if not isinstance(reservation_receipt, dict) or not isinstance(
                reconciliation_receipt, dict
            ):
                raise RuntimeError("reservation-only closure receipts disappeared")
            failure: object = fence_run_end_reserved_budget_without_intent(
                self.director_engine,
                run_id=self.run_id,
                candidate_experiment_id=state.champion_experiment_id,
                prior_state_key=state_key,
                prior_state_sha256=prior_sha,
                prior_state_sequence=prior_sequence,
                budget_snapshot_sha256=budget_sha,
                unavailable_checkpoint_sha256=checkpoint_sha,
                unavailable_checkpoint_sequence=checkpoint_sequence,
                reservation_checkpoint_key=str(reservation_receipt["key"]),
                reservation_checkpoint_sha256=str(reservation_receipt["payload_sha256"]),
                reservation_checkpoint_sequence=int(cast(int, reservation_receipt["sequence"])),
                reconciliation_checkpoint_key=str(reconciliation_receipt["key"]),
                reconciliation_checkpoint_sha256=str(reconciliation_receipt["payload_sha256"]),
                reconciliation_checkpoint_sequence=int(
                    cast(int, reconciliation_receipt["sequence"])
                ),
            )
        else:
            failure = fence(
                self.director_engine,
                run_id=self.run_id,
                candidate_experiment_id=state.champion_experiment_id,
                reason=reason,
                prior_state_key=state_key,
                prior_state_sha256=prior_sha,
                prior_state_sequence=prior_sequence,
                budget_snapshot_sha256=budget_sha,
                unavailable_checkpoint_sha256=checkpoint_sha,
                unavailable_checkpoint_sequence=checkpoint_sequence,
                remaining_wall_seconds=remaining_wall_seconds,
                original_intent_checkpoint_sha256=original_sha,
                original_intent_checkpoint_sequence=original_sequence,
            )
        if (
            getattr(failure, "admitted_generation", None) != captured_owner.generation
            or getattr(failure, "execution_sha256", None) != captured_owner.execution_sha256
        ):
            raise RuntimeError("run-end unavailable SQL fence differs from captured owner")
        return failure

    def apply_run_end_unavailable(
        self, result: DirectorLoopResult, failure: object
    ) -> DirectorLoopResult:
        """Apply the durable bitless no-admission outcome and restore approved state."""
        from lab.director.holdout import RunEndUnavailable
        from lab.director.ownership import active_execution_owner

        if not isinstance(failure, RunEndUnavailable) or failure.run_id != self.run_id:
            raise TypeError("run-end unavailable reducer requires its typed durable fence")
        owner = active_execution_owner()
        if (
            owner is None
            or owner.run_id != self.run_id
            or failure.admitted_generation != owner.generation
            or failure.execution_sha256 != owner.execution_sha256
        ):
            raise RuntimeError("run-end unavailable fence differs from the captured owner")
        frozen_key = "director-holdout-run-end-unavailable-intent:1"
        frozen = self.lease.read_checkpoint(key=frozen_key, artifact_root=self.artifact_root)
        prior = self.lease.read_checkpoint(
            key=failure.prior_state_key, artifact_root=self.artifact_root
        )
        frozen_payload = frozen.get("payload") if frozen is not None else None
        prior_receipt = prior.get("receipt") if prior is not None else None
        if (
            result.run_id != self.run_id
            or frozen is None
            or prior is None
            or not isinstance(frozen_payload, dict)
            or not isinstance(prior.get("payload"), dict)
            or not isinstance(prior_receipt, dict)
            or prior_receipt.get("key") != failure.prior_state_key
            or prior_receipt.get("phase") != "director_loop_state"
            or frozen.get("receipt", {}).get("payload_sha256")
            != failure.unavailable_checkpoint_sha256
            or frozen.get("receipt", {}).get("sequence") != failure.unavailable_checkpoint_sequence
            or frozen.get("receipt", {}).get("phase") != "holdout_run_end_unavailable_intent"
            or prior.get("receipt", {}).get("payload_sha256") != failure.prior_state_sha256
            or prior.get("receipt", {}).get("sequence") != failure.prior_state_sequence
            or frozen_payload.get("run_id") != str(self.run_id)
            or frozen_payload.get("candidate_experiment_id") != failure.candidate_experiment_id
            or frozen_payload.get("reason") != failure.reason
            or frozen_payload.get("prior_state_key") != failure.prior_state_key
            or frozen_payload.get("budget_snapshot_sha256") != failure.budget_snapshot_sha256
            or frozen_payload.get("prior_state_sha256") != failure.prior_state_sha256
            or frozen_payload.get("prior_state_sequence") != failure.prior_state_sequence
            or frozen_payload.get("original_intent_checkpoint_sha256")
            != failure.original_intent_checkpoint_sha256
            or frozen_payload.get("original_intent_checkpoint_sequence")
            != failure.original_intent_checkpoint_sequence
            or frozen_payload.get("admitted_generation") != failure.admitted_generation
            or frozen_payload.get("execution_sha256") != failure.execution_sha256
            or frozen_payload.get("reservation_checkpoint_key")
            != failure.reservation_checkpoint_key
            or frozen_payload.get("reservation_checkpoint_sha256")
            != failure.reservation_checkpoint_sha256
            or frozen_payload.get("reservation_checkpoint_sequence")
            != failure.reservation_checkpoint_sequence
            or (
                failure.reason == "fresh_wall_budget_unavailable"
                and (
                    isinstance(frozen_payload.get("remaining_wall_seconds"), bool)
                    or not isinstance(frozen_payload.get("remaining_wall_seconds"), (int, float))
                    or not math.isfinite(float(frozen_payload["remaining_wall_seconds"]))
                    or frozen_payload["remaining_wall_seconds"] >= 1
                )
            )
        ):
            raise RuntimeError("run-end unavailable receipt differs from frozen local checkpoints")
        if failure.original_intent_checkpoint_sha256 is not None:
            original_intent = self.lease.read_checkpoint(
                key="director-holdout-run-end-intent", artifact_root=self.artifact_root
            )
            if (
                original_intent is None
                or original_intent.get("receipt", {}).get("payload_sha256")
                != failure.original_intent_checkpoint_sha256
                or original_intent.get("receipt", {}).get("sequence")
                != failure.original_intent_checkpoint_sequence
                or original_intent.get("payload", {}).get("run_id") != str(self.run_id)
                or original_intent.get("payload", {}).get("candidate_experiment_id")
                != failure.candidate_experiment_id
                or original_intent.get("payload", {}).get("admitted_generation")
                != failure.admitted_generation
                or original_intent.get("payload", {}).get("execution_sha256")
                != failure.execution_sha256
            ):
                raise RuntimeError("run-end unavailable original intent is not hash-bound")
        budget_snapshot = frozen_payload.get("budget_snapshot")
        if (
            not isinstance(budget_snapshot, dict)
            or self._budget_snapshot_sha256(budget_snapshot) != failure.budget_snapshot_sha256
        ):
            raise RuntimeError("run-end unavailable budget snapshot failed hash verification")
        prior_state = DirectorLoopState.model_validate_json(
            json.dumps(prior["payload"], sort_keys=True, separators=(",", ":")), strict=True
        ).verify_consistency()
        if (
            prior_state.run_id != self.run_id
            or prior_state.champion_experiment_id != failure.candidate_experiment_id
        ):
            raise RuntimeError("run-end unavailable candidate is not bound to the prior state")
        application_key = "holdout-run-end-unavailable:1"
        application = self.lease.read_checkpoint(
            key=application_key, artifact_root=self.artifact_root
        )
        next_state = self._run_end_unavailable_state(prior_state, budget_snapshot)
        app_payload = {
            "schema": "director-run-end-unavailable-application.v1",
            "run_id": str(self.run_id),
            "candidate_experiment_id": failure.candidate_experiment_id,
            "reason": failure.reason,
            "expected_state_sha256": hashlib.sha256(
                canonical_bytes(next_state.model_dump(mode="json", by_alias=True))
            ).hexdigest(),
            "unavailable_checkpoint_sha256": failure.unavailable_checkpoint_sha256,
            "unavailable_checkpoint_sequence": failure.unavailable_checkpoint_sequence,
            "prior_state_key": failure.prior_state_key,
            "prior_state_sha256": failure.prior_state_sha256,
            "budget_snapshot": budget_snapshot,
            "budget_snapshot_sha256": failure.budget_snapshot_sha256,
            "admitted_generation": failure.admitted_generation,
            "execution_sha256": failure.execution_sha256,
            "reservation_checkpoint_key": failure.reservation_checkpoint_key,
            "reservation_checkpoint_sha256": failure.reservation_checkpoint_sha256,
            "reservation_checkpoint_sequence": failure.reservation_checkpoint_sequence,
            "state": "failed",
            "bit": None,
        }
        if application is None:
            app_receipt = self.lease.append_holdout_closure_checkpoint(
                sequence=self._next_checkpoint_sequence(),
                key=application_key,
                phase="holdout_run_end_unavailable",
                payload=app_payload,
                artifact_root=self.artifact_root,
            )
        else:
            if (
                application.get("payload") != app_payload
                or application.get("receipt", {}).get("phase") != "holdout_run_end_unavailable"
            ):
                raise RuntimeError("run-end unavailable application conflicts on restart")
            app_receipt = application["receipt"]
        next_checkpoint = self._persist_state(
            next_state, checkpoint_tag="run_end", holdout_closure=True
        )
        next_digest = next_checkpoint.get("payload_sha256")
        if not isinstance(next_digest, str):
            raise RuntimeError("run-end unavailable state checkpoint has no digest")
        marker_payload = {
            "schema": "director-holdout-state-application.v1",
            "run_id": str(self.run_id),
            "application_sha256": app_receipt.get("payload_sha256"),
            "state_sha256": next_digest,
        }
        marker_key = "holdout-state-application:run_end:1"
        marker = self.lease.read_checkpoint(key=marker_key, artifact_root=self.artifact_root)
        if marker is None:
            self.lease.append_holdout_closure_checkpoint(
                sequence=self._next_checkpoint_sequence(),
                key=marker_key,
                phase="holdout_state_application",
                payload=marker_payload,
                artifact_root=self.artifact_root,
            )
        elif marker.get("payload") != marker_payload:
            raise RuntimeError("run-end unavailable application marker changed")
        return result.model_copy(
            update={
                "champion_experiment_id": next_state.champion_experiment_id,
                "best_suite": next_state.best_suite,
                "checkpoint_sha256": next_digest,
            }
        )

    @staticmethod
    def _run_end_unavailable_state(
        state: DirectorLoopState, budget_snapshot: dict[str, object]
    ) -> DirectorLoopState:
        approved = state.holdout_approved_snapshot
        if approved is None:
            raise RuntimeError("run-end unavailable state has no approved champion snapshot")
        _budget_from_json(budget_snapshot)
        return (
            approved.restore(state, keep_count=state.holdout_applied_keep_count)
            .model_copy(
                update={
                    "holdout_approved_snapshot": approved,
                    "holdout_last_status": "failed",
                    "budget": budget_snapshot,
                    "recent_feedback": (
                        *state.recent_feedback,
                        "run-end holdout was unavailable; restored last approved champion and "
                        "requires human review",
                    )[-30:],
                }
            )
            .verify_consistency()
        )

    def _latest_reconciled_run_end_budget(self) -> dict[str, object] | None:
        with self.director_engine.connect() as connection:
            events = (
                connection.execute(
                    text(
                        "SELECT event_json->>'key' AS key FROM lab.run_events "
                        "WHERE run_id=:run_id AND event_type='director.checkpoint' "
                        "AND event_json->>'phase'='holdout_budget_reconciled' "
                        "ORDER BY (event_json->>'sequence')::integer DESC LIMIT 32"
                    ),
                    {"run_id": self.run_id},
                )
                .mappings()
                .all()
            )
        for event in events:
            key = event.get("key")
            if not isinstance(key, str):
                continue
            checkpoint = self.lease.read_checkpoint(key=key, artifact_root=self.artifact_root)
            payload = checkpoint.get("payload") if checkpoint is not None else None
            receipt = checkpoint.get("receipt") if checkpoint is not None else None
            if (
                not isinstance(payload, dict)
                or not isinstance(receipt, dict)
                or receipt.get("phase") != "holdout_budget_reconciled"
                or payload.get("run_id") != str(self.run_id)
                or payload.get("trigger_kind") != "run_end"
                or payload.get("trigger_index") != 1
                or not isinstance(payload.get("budget"), dict)
            ):
                continue
            return checkpoint
        return None

    def _record_run_end_admission_failure(
        self,
        state: DirectorLoopState,
        state_checkpoint: dict[str, object],
        failure: object,
    ) -> tuple[DirectorLoopState, dict[str, object]]:
        from lab.director.holdout import RunEndAdmissionFailure

        if not isinstance(failure, RunEndAdmissionFailure):
            raise TypeError("run-end failure reducer requires its typed fence")
        if failure.admitted_generation is None or failure.execution_sha256 is None:
            raise RuntimeError("run-end failure fence lacks its admitted owner pair")
        prior_digest = state_checkpoint.get("payload_sha256")
        if not isinstance(prior_digest, str) or failure.run_id != self.run_id:
            raise ValueError("run-end failure is not bound to the current prior state")
        if state.champion_experiment_id != failure.candidate_experiment_id:
            raise ValueError("run-end failure candidate differs from the prior champion")
        intent_checkpoint = self.lease.read_checkpoint(
            key="director-holdout-run-end-intent", artifact_root=self.artifact_root
        )
        budget_checkpoint = self.lease.read_checkpoint(
            key=failure.budget_checkpoint_key, artifact_root=self.artifact_root
        )
        if (
            intent_checkpoint is None
            or budget_checkpoint is None
            or intent_checkpoint.get("receipt", {}).get("payload_sha256")
            != failure.intent_checkpoint_sha256
            or intent_checkpoint.get("receipt", {}).get("sequence")
            != failure.intent_checkpoint_sequence
            or intent_checkpoint.get("payload", {}).get("run_id") != str(self.run_id)
            or intent_checkpoint.get("payload", {}).get("candidate_experiment_id")
            != failure.candidate_experiment_id
            or intent_checkpoint.get("payload", {}).get("admitted_generation")
            != failure.admitted_generation
            or intent_checkpoint.get("payload", {}).get("execution_sha256")
            != failure.execution_sha256
            or budget_checkpoint.get("receipt", {}).get("payload_sha256")
            != failure.budget_checkpoint_sha256
            or budget_checkpoint.get("receipt", {}).get("phase") != "holdout_budget_reconciled"
        ):
            raise RuntimeError("run-end failure fence does not match hash-verified local receipts")
        budget_payload = budget_checkpoint.get("payload")
        if (
            not isinstance(budget_payload, dict)
            or budget_payload.get("run_id") != str(self.run_id)
            or budget_payload.get("trigger_kind") != "run_end"
            or budget_payload.get("trigger_index") != 1
            or not isinstance(budget_payload.get("budget"), dict)
        ):
            raise RuntimeError("run-end failure budget checkpoint has the wrong identity")
        budget_snapshot = budget_payload["budget"]
        _budget_from_json(budget_snapshot)
        next_state, _ = self._record_failure_expected_state(
            state,
            failure,
            {
                "budget_snapshot": budget_snapshot,
                "budget_snapshot_sha256": self._budget_snapshot_sha256(budget_snapshot),
            },
        )
        application_key = "holdout-run-end-admission-failure:1"
        identity = {
            "schema": "director-run-end-admission-failure.v1",
            "expected_state_sha256": hashlib.sha256(
                canonical_bytes(next_state.model_dump(mode="json", by_alias=True))
            ).hexdigest(),
            "run_id": str(self.run_id),
            "candidate_experiment_id": failure.candidate_experiment_id,
            "intent_checkpoint_sha256": failure.intent_checkpoint_sha256,
            "intent_checkpoint_sequence": failure.intent_checkpoint_sequence,
            "budget_checkpoint_key": failure.budget_checkpoint_key,
            "budget_checkpoint_sha256": failure.budget_checkpoint_sha256,
            "failure_kind": failure.failure_kind,
            "admitted_generation": failure.admitted_generation,
            "execution_sha256": failure.execution_sha256,
            "state": "failed",
            "bit": None,
            "prior_state_sha256": prior_digest,
        }
        application = self.lease.read_checkpoint(
            key=application_key, artifact_root=self.artifact_root
        )
        if application is None:
            application_payload = {
                **identity,
                "budget_snapshot": budget_snapshot,
                "budget_snapshot_sha256": self._budget_snapshot_sha256(budget_snapshot),
            }
            application_receipt = self.lease.append_holdout_closure_checkpoint(
                sequence=self._next_checkpoint_sequence(),
                key=application_key,
                phase="holdout_run_end_admission_failure",
                payload=application_payload,
                artifact_root=self.artifact_root,
            )
        else:
            app_payload = application.get("payload")
            if (
                not isinstance(app_payload, dict)
                or any(app_payload.get(name) != value for name, value in identity.items())
                or application.get("receipt", {}).get("phase")
                != "holdout_run_end_admission_failure"
                or self._validated_application_budget(app_payload) != budget_snapshot
            ):
                raise RuntimeError("run-end admission failure application conflicts on resume")
            application_receipt = application["receipt"]
        approved = state.holdout_approved_snapshot
        if approved is None:
            raise RuntimeError("run-end admission failure has no approved champion snapshot")
        next_state = (
            approved.restore(state, keep_count=state.holdout_applied_keep_count)
            .model_copy(
                update={
                    "holdout_approved_snapshot": approved,
                    "holdout_last_status": "failed",
                    "budget": budget_snapshot,
                    "recent_feedback": (
                        *state.recent_feedback,
                        "run-end holdout admission could not be proven; restored last approved "
                        "champion and requires human review",
                    )[-30:],
                }
            )
            .verify_consistency()
        )
        next_checkpoint = self._persist_state(
            next_state, checkpoint_tag="run_end", holdout_closure=True
        )
        next_digest = next_checkpoint.get("payload_sha256")
        if not isinstance(next_digest, str):
            raise RuntimeError("run-end failure state checkpoint has no digest")
        marker_key = "holdout-state-application:run_end:1"
        marker_payload = {
            "schema": "director-holdout-state-application.v1",
            "run_id": str(self.run_id),
            "application_sha256": application_receipt.get("payload_sha256"),
            "state_sha256": next_digest,
        }
        old_marker = self.lease.read_checkpoint(key=marker_key, artifact_root=self.artifact_root)
        if old_marker is None:
            self.lease.append_holdout_closure_checkpoint(
                sequence=self._next_checkpoint_sequence(),
                key=marker_key,
                phase="holdout_state_application",
                payload=marker_payload,
                artifact_root=self.artifact_root,
            )
        elif old_marker.get("payload") != marker_payload:
            raise RuntimeError("run-end failure state marker changed on resume")
        return next_state, next_checkpoint

    def _restore_run_end_admission_failure(
        self, result: DirectorLoopResult
    ) -> DirectorLoopResult | None:
        application = self.lease.read_checkpoint(
            key="holdout-run-end-admission-failure:1", artifact_root=self.artifact_root
        )
        if application is None:
            return None
        app_payload = application.get("payload")
        app_receipt = application.get("receipt")
        if (
            not isinstance(app_payload, dict)
            or not isinstance(app_receipt, dict)
            or app_receipt.get("phase") != "holdout_run_end_admission_failure"
        ):
            raise RuntimeError("run-end admission failure application is malformed")
        from lab.director.holdout import RunEndAdmissionFailure
        from lab.director.ownership import active_execution_owner

        sequence = app_payload.get("intent_checkpoint_sequence")
        failure_kind = app_payload.get("failure_kind")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or failure_kind != "missing_reservation_unverifiable_budget"
        ):
            raise RuntimeError("run-end admission failure application has invalid typed fields")
        failure = RunEndAdmissionFailure(
            run_id=UUID(str(app_payload.get("run_id"))),
            candidate_experiment_id=str(app_payload.get("candidate_experiment_id")),
            intent_checkpoint_sha256=str(app_payload.get("intent_checkpoint_sha256")),
            intent_checkpoint_sequence=sequence,
            budget_checkpoint_key=str(app_payload.get("budget_checkpoint_key")),
            budget_checkpoint_sha256=str(app_payload.get("budget_checkpoint_sha256")),
            failure_kind="missing_reservation_unverifiable_budget",
            admitted_generation=app_payload.get("admitted_generation"),
            execution_sha256=app_payload.get("execution_sha256"),
        )
        if failure.run_id != self.run_id:
            raise RuntimeError("run-end admission failure application identity changed")
        owner = active_execution_owner()
        if (
            owner is None
            or owner.run_id != self.run_id
            or failure.admitted_generation != owner.generation
            or failure.execution_sha256 != owner.execution_sha256
        ):
            raise RuntimeError("run-end failure checkpoint differs from captured execution owner")
        intent_checkpoint = self.lease.read_checkpoint(
            key="director-holdout-run-end-intent", artifact_root=self.artifact_root
        )
        if (
            intent_checkpoint is None
            or intent_checkpoint.get("receipt", {}).get("payload_sha256")
            != failure.intent_checkpoint_sha256
            or intent_checkpoint.get("receipt", {}).get("sequence")
            != failure.intent_checkpoint_sequence
            or intent_checkpoint.get("payload", {}).get("admitted_generation")
            != failure.admitted_generation
            or intent_checkpoint.get("payload", {}).get("execution_sha256")
            != failure.execution_sha256
        ):
            raise RuntimeError("run-end failure owner differs from its durable intent")
        prior_key = self._state_key_for_digest(str(app_payload.get("prior_state_sha256")))
        prior_checkpoint = self.lease.read_checkpoint(
            key=prior_key, artifact_root=self.artifact_root
        )
        if (
            prior_checkpoint is None
            or not isinstance(prior_checkpoint.get("payload"), dict)
            or prior_checkpoint.get("receipt", {}).get("payload_sha256")
            != app_payload.get("prior_state_sha256")
        ):
            raise RuntimeError("run-end failure prior state receipt is unavailable")
        prior_state = DirectorLoopState.model_validate_json(
            json.dumps(prior_checkpoint["payload"], sort_keys=True, separators=(",", ":")),
            strict=True,
        ).verify_consistency()
        if prior_state.champion_experiment_id != failure.candidate_experiment_id:
            raise RuntimeError("run-end failure candidate differs from its hash-bound prior state")
        marker = self.lease.read_checkpoint(
            key="holdout-state-application:run_end:1", artifact_root=self.artifact_root
        )
        state_key = self._latest_state_key()
        state_checkpoint = self.lease.read_checkpoint(
            key=state_key, artifact_root=self.artifact_root
        )
        if state_checkpoint is None or not isinstance(state_checkpoint.get("payload"), dict):
            raise RuntimeError("run-end admission failure state is unavailable")
        state = DirectorLoopState.model_validate_json(
            json.dumps(
                state_checkpoint["payload"],
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
            strict=True,
        ).verify_consistency()
        state_digest = state_checkpoint.get("payload_sha256")
        if not isinstance(state_digest, str):
            state_receipt = state_checkpoint.get("receipt")
            state_digest = (
                state_receipt.get("payload_sha256") if isinstance(state_receipt, dict) else None
            )
        if marker is None:
            if state_digest == app_payload.get("prior_state_sha256"):
                state_receipt = state_checkpoint.get("receipt")
                if not isinstance(state_receipt, dict):
                    raise RuntimeError("run-end failure prior state has no checkpoint receipt")
                state, state_checkpoint = self._record_run_end_admission_failure(
                    state, state_receipt, failure
                )
            else:
                expected_state, _ = self._record_failure_expected_state(
                    prior_state, failure, app_payload
                )
                expected_key = self._state_checkpoint_key(expected_state, checkpoint_tag="run_end")
                if state_key != expected_key or state.model_dump(
                    mode="json", by_alias=True
                ) != expected_state.model_dump(mode="json", by_alias=True):
                    raise RuntimeError("latest state is not the exact run-end failure transition")
                app_sha = app_receipt.get("payload_sha256")
                self.lease.append_holdout_closure_checkpoint(
                    sequence=self._next_checkpoint_sequence(),
                    key="holdout-state-application:run_end:1",
                    phase="holdout_state_application",
                    payload={
                        "schema": "director-holdout-state-application.v1",
                        "run_id": str(self.run_id),
                        "application_sha256": app_sha,
                        "state_sha256": state_digest,
                    },
                    artifact_root=self.artifact_root,
                )
        elif marker.get("payload") != {
            "schema": "director-holdout-state-application.v1",
            "run_id": str(self.run_id),
            "application_sha256": app_receipt.get("payload_sha256"),
            "state_sha256": state_digest,
        }:
            raise RuntimeError("run-end admission failure marker differs from durable state")
        digest = state_checkpoint.get("payload_sha256")
        if not isinstance(digest, str):
            state_receipt = state_checkpoint.get("receipt")
            digest = (
                state_receipt.get("payload_sha256") if isinstance(state_receipt, dict) else None
            )
        if not isinstance(digest, str):
            raise RuntimeError("run-end admission failure state has no digest")
        return result.model_copy(
            update={
                "champion_experiment_id": state.champion_experiment_id,
                "best_suite": state.best_suite,
                "checkpoint_sha256": digest,
            }
        )

    @classmethod
    def _record_failure_expected_state(
        cls,
        state: DirectorLoopState,
        failure: object,
        app_payload: dict[str, object],
    ) -> tuple[DirectorLoopState, dict[str, object]]:
        from lab.director.holdout import RunEndAdmissionFailure

        if not isinstance(failure, RunEndAdmissionFailure):
            raise TypeError("run-end failure expected-state derivation requires its typed fence")
        budget_snapshot = cls._validated_application_budget(app_payload)
        approved = state.holdout_approved_snapshot
        if approved is None:
            raise RuntimeError("run-end admission failure has no approved champion snapshot")
        expected = (
            approved.restore(state, keep_count=state.holdout_applied_keep_count)
            .model_copy(
                update={
                    "holdout_approved_snapshot": approved,
                    "holdout_last_status": "failed",
                    "budget": budget_snapshot,
                    "recent_feedback": (
                        *state.recent_feedback,
                        "run-end holdout admission could not be proven; restored last approved "
                        "champion and requires human review",
                    )[-30:],
                }
            )
            .verify_consistency()
        )
        return expected, budget_snapshot

    def restore_run_end_holdout(self, result: DirectorLoopResult) -> DirectorLoopResult | None:
        """Recover an already applied run-end bit without registering a new intent."""
        restored_failure = self._restore_run_end_admission_failure(result)
        if restored_failure is not None:
            return restored_failure
        restored_unavailable = self._restore_run_end_unavailable(result)
        if restored_unavailable is not None:
            return restored_unavailable
        marker = self.lease.read_checkpoint(
            key="holdout-state-application:run_end:1", artifact_root=self.artifact_root
        )
        application = self.lease.read_checkpoint(
            key="holdout-application:run_end:1", artifact_root=self.artifact_root
        )
        if marker is None and application is None:
            return None
        if application is None or not isinstance(application.get("payload"), dict):
            raise RuntimeError("run-end state marker has no holdout application receipt")
        app_payload = application["payload"]
        if (
            app_payload.get("trigger_kind") != "run_end"
            or app_payload.get("run_id") != str(self.run_id)
            or app_payload.get("state") not in {"passed", "reverted", "exhausted", "failed"}
        ):
            raise RuntimeError("stored run-end application identity is invalid")
        if (
            app_payload.get("trigger_index") != 1
            or not isinstance(app_payload.get("candidate_experiment_id"), str)
            or not app_payload.get("candidate_experiment_id")
            or (
                app_payload.get("state") in {"exhausted", "failed"}
                and app_payload.get("bit") is not None
            )
            or (
                app_payload.get("state") in {"passed", "reverted"}
                and (
                    type(app_payload.get("bit")) is not bool
                    or app_payload.get("bit") != (app_payload.get("state") == "passed")
                )
            )
            or not isinstance(app_payload.get("prior_state_sha256"), str)
            or len(app_payload["prior_state_sha256"]) != 64
        ):
            raise RuntimeError("stored run-end application binding is malformed")
        from lab.director.ownership import active_execution_owner

        owner = active_execution_owner()
        if (
            owner is None
            or owner.run_id != self.run_id
            or app_payload.get("execution_sha256") != owner.execution_sha256
        ):
            raise RuntimeError("stored run-end holdout differs from captured execution owner")
        if app_payload.get("admitted_generation") != owner.generation:
            from lab.director.resume import assert_historical_receipt

            assert_historical_receipt(
                self.director_engine,
                owner=owner,
                admitted_generation=app_payload["admitted_generation"],
                execution_sha256=app_payload["execution_sha256"],
            )
        state_key = self._latest_state_key()
        state_checkpoint = self.lease.read_checkpoint(
            key=state_key, artifact_root=self.artifact_root
        )
        if state_checkpoint is None:
            raise RuntimeError("applied run-end state checkpoint is missing")
        state = DirectorLoopState.model_validate_json(
            json.dumps(state_checkpoint["payload"], separators=(",", ":")), strict=True
        ).verify_consistency()
        state_digest = state_checkpoint["receipt"].get("payload_sha256")
        if not isinstance(state_digest, str):
            raise RuntimeError("current Director state receipt has no payload digest")
        if marker is None:
            if state_digest == app_payload.get("prior_state_sha256"):
                from lab.director.holdout import HoldoutReceipt

                recovered = HoldoutReceipt(
                    reservation_id=UUID(str(app_payload["reservation_id"])),
                    state=app_payload["state"],
                    bit=app_payload["bit"],
                    run_id=self.run_id,
                    candidate_experiment_id=str(app_payload["candidate_experiment_id"]),
                    trigger_kind="run_end",
                    trigger_index=1,
                    admitted_generation=app_payload.get("admitted_generation"),
                    execution_sha256=app_payload.get("execution_sha256"),
                )
                state, state_checkpoint = self._record_holdout_application(
                    state,
                    state_checkpoint["receipt"],
                    recovered,
                    trigger_kind="run_end",
                    trigger_index=1,
                    candidate_id=str(app_payload["candidate_experiment_id"]),
                )
                state_digest = state_checkpoint.get("payload_sha256")
                if not isinstance(state_digest, str):
                    raise RuntimeError("recovered run-end state has no digest")
            else:
                # Only bless the exact transition implied by the prior state and the
                # immutable application receipt. A later state checkpoint is not proof.
                prior_key = self._state_key_for_digest(str(app_payload.get("prior_state_sha256")))
                prior_checkpoint = self.lease.read_checkpoint(
                    key=prior_key, artifact_root=self.artifact_root
                )
                if prior_checkpoint is None:
                    raise RuntimeError("run-end prior state checkpoint is missing")
                prior_receipt = prior_checkpoint.get("receipt")
                if not isinstance(prior_receipt, dict) or prior_receipt.get(
                    "payload_sha256"
                ) != app_payload.get("prior_state_sha256"):
                    raise RuntimeError("run-end prior state receipt differs from application")
                prior_state = DirectorLoopState.model_validate_json(
                    json.dumps(prior_checkpoint["payload"], separators=(",", ":")), strict=True
                ).verify_consistency()
                if prior_state.champion_experiment_id != app_payload["candidate_experiment_id"]:
                    raise RuntimeError("run-end application candidate differs from prior champion")
                from lab.director.holdout import HoldoutReceipt

                recovered = HoldoutReceipt(
                    reservation_id=UUID(str(app_payload["reservation_id"])),
                    state=app_payload["state"],
                    bit=app_payload["bit"],
                    run_id=self.run_id,
                    candidate_experiment_id=str(app_payload["candidate_experiment_id"]),
                    trigger_kind="run_end",
                    trigger_index=1,
                    admitted_generation=app_payload.get("admitted_generation"),
                    execution_sha256=app_payload.get("execution_sha256"),
                )
                expected_state = self._state_after_holdout(
                    prior_state,
                    recovered,
                    trigger_kind="run_end",
                    trigger_index=1,
                    budget_snapshot=self._validated_application_budget(app_payload),
                )
                expected_key = self._state_checkpoint_key(expected_state, checkpoint_tag="run_end")
                if state_key != expected_key or state.model_dump(
                    mode="json", by_alias=True
                ) != expected_state.model_dump(mode="json", by_alias=True):
                    raise RuntimeError("latest state is not the exact run-end holdout transition")
                append = (
                    self.lease.append_holdout_closure_checkpoint
                    if recovered.state == "failed" and recovered.bit is None
                    else self.lease.append_checkpoint
                )
                append(
                    sequence=self._next_checkpoint_sequence(),
                    key="holdout-state-application:run_end:1",
                    phase="holdout_state_application",
                    payload={
                        "schema": "director-holdout-state-application.v1",
                        "run_id": str(self.run_id),
                        "application_sha256": application["receipt"].get("payload_sha256"),
                        "state_sha256": state_digest,
                    },
                    artifact_root=self.artifact_root,
                )
        elif not isinstance(marker.get("payload"), dict) or marker["payload"] != {
            "schema": "director-holdout-state-application.v1",
            "run_id": str(self.run_id),
            "application_sha256": application["receipt"].get("payload_sha256"),
            "state_sha256": state_digest,
        }:
            raise RuntimeError("run-end holdout state marker differs from durable state")
        return result.model_copy(
            update={
                "champion_experiment_id": state.champion_experiment_id,
                "best_suite": state.best_suite,
                "checkpoint_sha256": state_digest,
            }
        )

    def _restore_run_end_unavailable(self, result: DirectorLoopResult) -> DirectorLoopResult | None:
        application = self.lease.read_checkpoint(
            key="holdout-run-end-unavailable:1", artifact_root=self.artifact_root
        )
        if application is None:
            return None
        app_payload = application.get("payload")
        app_receipt = application.get("receipt")
        if (
            not isinstance(app_payload, dict)
            or not isinstance(app_receipt, dict)
            or app_receipt.get("phase") != "holdout_run_end_unavailable"
            or app_payload.get("schema") != "director-run-end-unavailable-application.v1"
            or app_payload.get("run_id") != str(self.run_id)
            or app_payload.get("state") != "failed"
            or app_payload.get("bit") is not None
        ):
            raise RuntimeError("run-end unavailable application receipt is malformed")
        from lab.director.holdout import read_run_end_unavailable

        failure = read_run_end_unavailable(self.director_engine, run_id=self.run_id)
        if failure is None:
            raise RuntimeError("run-end unavailable application has no SQL fence")
        if (
            app_payload.get("candidate_experiment_id") != failure.candidate_experiment_id
            or app_payload.get("reason") != failure.reason
            or app_payload.get("unavailable_checkpoint_sha256")
            != failure.unavailable_checkpoint_sha256
            or app_payload.get("unavailable_checkpoint_sequence")
            != failure.unavailable_checkpoint_sequence
            or app_payload.get("prior_state_key") != failure.prior_state_key
            or app_payload.get("prior_state_sha256") != failure.prior_state_sha256
            or app_payload.get("budget_snapshot_sha256") != failure.budget_snapshot_sha256
            or app_payload.get("admitted_generation") != failure.admitted_generation
            or app_payload.get("execution_sha256") != failure.execution_sha256
        ):
            raise RuntimeError("run-end unavailable application differs from its SQL fence")
        return self.apply_run_end_unavailable(result, failure)

    def _latest_state_key(self) -> str:
        with self.director_engine.connect() as connection:
            key = connection.execute(
                text(
                    "SELECT event_json->>'key' FROM lab.run_events WHERE run_id=:run_id "
                    "AND event_type='director.checkpoint' "
                    "AND event_json->>'phase'='director_loop_state' "
                    "ORDER BY (event_json->>'sequence')::integer DESC LIMIT 1"
                ),
                {"run_id": self.run_id},
            ).scalar_one_or_none()
        if not isinstance(key, str):
            raise RuntimeError("durable Director state checkpoint is missing")
        return key

    def _state_key_for_digest(self, digest: str) -> str:
        with self.director_engine.connect() as connection:
            key = connection.execute(
                text(
                    "SELECT event_json->>'key' FROM lab.run_events WHERE run_id=:run_id "
                    "AND event_type='director.checkpoint' "
                    "AND event_json->>'payload_sha256'=:digest "
                    "AND event_json->>'phase'='director_loop_state'"
                ),
                {"run_id": self.run_id, "digest": digest},
            ).scalar_one_or_none()
        if not isinstance(key, str):
            raise RuntimeError("loop result does not reference a durable state checkpoint")
        return key

    def _advance_tool_parse_abandonment(
        self,
        state: DirectorLoopState,
        *,
        ordinal: int,
        move_type: MoveType | str | None,
        is_explore: bool,
        reason: str,
    ) -> DirectorLoopState:
        if ordinal != state.next_ordinal:
            raise RuntimeError("abandoned proposal does not match the next durable ordinal")
        strategy = state.load_strategy_state()
        if (
            strategy.pending is None
            or strategy.pending.ordinal != ordinal
            or strategy.pending.move_type != move_type
        ):
            raise RuntimeError("abandoned proposal differs from its durable Thompson intent")
        resolved_strategy = resolve_strategy_selection(strategy, ordinal, "ABANDONED")
        feedback = f"experiment={ordinal}; verdict=ABANDONED; reason={reason}"
        episode = state.explore_episode
        if is_explore:
            terminal = self.lease.read_checkpoint(
                key=f"proposal-abandoned:{ordinal}", artifact_root=self.artifact_root
            )
            if episode is None or terminal is None or not isinstance(terminal.get("payload"), dict):
                raise RuntimeError("EXPLORE abandonment requires its committed receipt")
            episode = episode.resolve(
                ExploreResolution(
                    ordinal=ordinal,
                    terminal_sha256=hashlib.sha256(
                        canonical_bytes(terminal["payload"])
                    ).hexdigest(),
                    outcome="ABANDONED",
                )
            )

        payload = {
            **state.model_dump(mode="json", by_alias=True),
            **_strategy_checkpoint_fields(resolved_strategy),
            "completed_proposals": state.completed_proposals + 1,
            "next_ordinal": state.next_ordinal + 1,
            "consecutive_non_keep": state.consecutive_non_keep + 1,
            "explore_proposals": len(episode.resolutions)
            if episode is not None
            else state.explore_proposals,
            "explore_episode": episode.model_dump(mode="json", by_alias=True) if episode else None,
            "termination_reason": "explore_exhausted"
            if episode and episode.phase == "HOLDOUT_CHECK"
            else None,
            "loop_phase": "HOLDOUT_CHECK" if episode and episode.phase == "HOLDOUT_CHECK" else None,
            "previous_move_type": move_type,
            "recent_feedback": (*state.recent_feedback, feedback)[-30:],
            "budget": _budget_to_json(self.budget.snapshot()),
        }
        return DirectorLoopState.model_validate_json(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
            strict=True,
        ).verify_consistency()

    def _verify_execution_identity(self, calibration: FrozenCalibrationDocument) -> None:
        image_digest = DEFAULT_SANDBOX_IMAGE.rsplit("@sha256:", 1)[-1].removeprefix("sha256:")
        if (
            self.runner.image != DEFAULT_SANDBOX_IMAGE
            or image_digest != calibration.image_sha256
            or calibration.run_id != self.run_id
        ):
            raise RuntimeError("Director dispatch suite/harness/image identity mismatch")
        if self.suite_manifest_sha256 != self._manifest_digest_from_run():
            raise RuntimeError("suite manifest hash differs from the run's immutable request")
        task_identities = {
            (task.dataset_id, task.split_id, task.session_id, task.task_id, task.profile_sha256)
            for task in self.tasks
        }
        calibrated = {
            (task.dataset_id, task.split_id, task.session_id, task.task_id, task.profile_sha256)
            for task in calibration.tasks
        }
        if task_identities != calibrated:
            raise RuntimeError("Director tasks differ from the exact frozen calibration set")

    def _manifest_digest_from_run(self) -> str:
        with self.director_engine.connect() as connection:
            value = connection.execute(
                text(
                    "SELECT request_json->>'suite_manifest_sha256' FROM lab.runs WHERE run_id=:run"
                ),
                {"run": self.run_id},
            ).scalar_one_or_none()
        if not isinstance(value, str):
            raise RuntimeError("run request has no suite manifest identity")
        return value

    def _load_or_initialize(
        self, calibration: FrozenCalibrationDocument
    ) -> tuple[DirectorLoopState, dict[str, object]]:
        with self.director_engine.connect() as connection:
            key = connection.execute(
                text(
                    "SELECT event_json->>'key' FROM lab.run_events WHERE run_id=:run "
                    "AND event_type='director.checkpoint' "
                    "AND event_json->>'key' LIKE 'director-state:%' "
                    "ORDER BY (event_json->>'sequence')::integer DESC LIMIT 1"
                ),
                {"run": self.run_id},
            ).scalar_one_or_none()
        if key is not None:
            checkpoint = self.lease.read_checkpoint(key=str(key), artifact_root=self.artifact_root)
            if checkpoint is None:
                raise RuntimeError("Director loop state receipt disappeared")
            state = DirectorLoopState.model_validate_json(
                json.dumps(checkpoint["payload"], separators=(",", ":")), strict=True
            )
            state.verify_consistency()
            if state.schema_version != "director-loop-state.v2":
                raise RuntimeError(
                    "cannot resume legacy Director state without preregistered Thompson history"
                )
            if state.holdout_approved_snapshot is None:
                raise RuntimeError("resumed Director state has no approved holdout snapshot")
            if (
                state.run_id != self.run_id
                or state.suite_manifest_sha256 != self.suite_manifest_sha256
                or state.calibration_sha256 != calibration_sha256(calibration)
                or state.harness_sha256 != calibration.harness_sha256
                or state.image_sha256 != calibration.image_sha256
                or state.suite_id != calibration.suite_id
                or state.suite_version != calibration.suite_version
                or state.proposal_limit != self.proposal_limit
            ):
                raise RuntimeError("Director loop execution identity changed on resume")
            state_receipt = checkpoint.get("receipt")
            state_sequence = (
                state_receipt.get("sequence") if isinstance(state_receipt, dict) else None
            )
            if isinstance(state_sequence, bool) or not isinstance(state_sequence, int):
                raise RuntimeError("Director state receipt has no durable sequence")
            self._restore_budget(state.budget, after_sequence=state_sequence)
            return state, checkpoint["receipt"]
        state = self._initial_state(calibration)
        receipt = self._persist_state(state)
        return state, receipt

    def _initial_state(self, calibration: FrozenCalibrationDocument) -> DirectorLoopState:
        if calibration.champion_baseline_name != "robust_z" or not calibration.champion_seed_scores:
            raise RuntimeError("initial Director champion must be measured robust-z seeds 0–2")
        seed0: dict[str, float] = {}
        seed1: dict[str, float] = {}
        for task in calibration.tasks:
            scores = next(item for item in task.seed_scores if item.name == "robust_z").scores
            seed0[task.task_id] = scores[0]
            seed1[task.task_id] = scores[1]
        suite_scores = calibration.champion_seed_scores
        # Initial historical best is derived from measured baseline seed rows.
        measured_baseline_mean = sum(suite_scores) / 3.0
        from lab.director.artifacts import store_director_artifact
        from lab.director.baselines import baseline_candidate_source

        source = baseline_candidate_source("robust_z")
        blob = store_director_artifact(source, artifact_root=self.artifact_root)
        strategy = initial_strategy_state(_strategy_seed_for_run(self.run_id))
        return DirectorLoopState.model_validate(
            {
                **_strategy_checkpoint_fields(strategy),
                "run_id": self.run_id,
                "suite_manifest_sha256": self.suite_manifest_sha256,
                "suite_id": calibration.suite_id,
                "suite_version": calibration.suite_version,
                "calibration_sha256": calibration_sha256(calibration),
                "harness_sha256": calibration.harness_sha256,
                "image_sha256": calibration.image_sha256,
                "proposal_limit": self.proposal_limit,
                "completed_proposals": 0,
                "next_ordinal": 1,
                "champion_experiment_id": calibration.champion_experiment_id,
                "champion_source_sha256": hashlib.sha256(source).hexdigest(),
                "champion_tree_sha256": _candidate_git_tree(source),
                "champion_source_blob_sha256": blob,
                "champion_seed0_by_task": seed0,
                "champion_seed1_by_task": seed1,
                "champion_suite_seed_scores": suite_scores,
                "champion_noise_sd": float(calibration.champion_noise_sd or 0.0),
                "best_suite": measured_baseline_mean,
                "consecutive_non_keep": 0,
                "explore_proposals": 0,
                "explore_family": None,
                "consecutive_candidate_crashes": 0,
                "discard_streak": 0,
                "previous_move_type": None,
                "recent_feedback": (),
                "budget": _budget_to_json(self.budget.snapshot()),
                "holdout_approved_snapshot": {
                    "experiment_id": calibration.champion_experiment_id,
                    "source_sha256": hashlib.sha256(source).hexdigest(),
                    "tree_sha256": _candidate_git_tree(source),
                    "source_blob_sha256": blob,
                    "seed0_by_task": seed0,
                    "seed1_by_task": seed1,
                    "suite_seed_scores": suite_scores,
                    "noise_sd": float(calibration.champion_noise_sd or 0.0),
                    "best_suite": measured_baseline_mean,
                    "periodic_keep_watermark": 0,
                },
                "holdout_applied_keep_count": 0,
            },
            strict=True,
        ).verify_consistency()

    def _persist_state(
        self,
        state: DirectorLoopState,
        *,
        checkpoint_tag: str | None = None,
        holdout_closure: bool = False,
    ) -> dict[str, object]:
        state.verify_consistency()
        key = self._state_checkpoint_key(state, checkpoint_tag=checkpoint_tag)
        payload = state.model_dump(mode="json", by_alias=True)
        existing = self.lease.read_checkpoint(key=key, artifact_root=self.artifact_root)
        if existing is not None:
            receipt = existing.get("receipt")
            if (
                existing.get("payload") != payload
                or not isinstance(receipt, dict)
                or receipt.get("key") != key
                or receipt.get("phase") != "director_loop_state"
            ):
                raise RuntimeError("Director state checkpoint key conflicts with another state")
            return receipt
        append = (
            self.lease.append_holdout_closure_checkpoint
            if holdout_closure
            else self.lease.append_checkpoint
        )
        receipt = append(
            sequence=self._next_checkpoint_sequence(),
            key=key,
            phase="director_loop_state",
            payload=payload,
            artifact_root=self.artifact_root,
        )
        return receipt

    def _preserve_pending_strategy_after_failure(
        self, state: DirectorLoopState, *, ordinal: int
    ) -> tuple[DirectorLoopState, dict[str, object]]:
        """Persist budget consumption without resolving or replacing provider intent."""
        strategy = state.load_strategy_state()
        if strategy.pending is None or strategy.pending.ordinal != ordinal:
            raise RuntimeError("provider failure has no matching pending Thompson intent")
        unchanged_strategy = strategy_state_json(strategy)
        checkpoint_tag = f"provider-failure-{ordinal}"
        checkpoint_key = self._state_checkpoint_key(state, checkpoint_tag=checkpoint_tag)
        existing = self.lease.read_checkpoint(key=checkpoint_key, artifact_root=self.artifact_root)
        if existing is not None:
            payload = existing.get("payload")
            receipt = existing.get("receipt")
            if not isinstance(payload, dict) or not isinstance(receipt, dict):
                raise RuntimeError("provider failure checkpoint is malformed")
            restored = DirectorLoopState.model_validate_json(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                strict=True,
            ).verify_consistency()
            if (
                strategy_state_json(restored.load_strategy_state()) != unchanged_strategy
                or restored.next_ordinal != ordinal
                or receipt.get("key") != checkpoint_key
                or receipt.get("phase") != "director_loop_state"
            ):
                raise RuntimeError("provider failure checkpoint conflicts with pending intent")
            return restored, receipt
        updated = state.model_copy(
            update={"budget": _budget_to_json(self.budget.snapshot())}
        ).verify_consistency()
        if strategy_state_json(updated.load_strategy_state()) != unchanged_strategy:
            raise RuntimeError("provider failure changed the pending Thompson state")
        receipt = self._persist_state(updated, checkpoint_tag=checkpoint_tag)
        return updated, receipt

    def _with_strategy_state(
        self,
        state: DirectorLoopState,
        strategy: StrategyState,
        **updates: object,
    ) -> DirectorLoopState:
        payload = state.model_dump(mode="json", by_alias=True)
        payload.update(updates)
        payload.update(_strategy_checkpoint_fields(strategy))
        return DirectorLoopState.model_validate_json(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
            strict=True,
        ).verify_consistency()

    def _pending_run_end_terminal_result(
        self,
        state: DirectorLoopState,
        state_checkpoint: dict[str, object],
    ) -> DirectorLoopResult | None:
        """Stop proposal replay when any durable run-end action already began."""
        if not getattr(self, "holdout_enabled", False):
            return None
        local_keys = (
            "director-holdout-run-end-intent",
            "director-holdout-run-end-unavailable-intent:1",
            "holdout-run-end-unavailable:1",
            "holdout-run-end-admission-failure:1",
            "holdout-application:run_end:1",
            "holdout-state-application:run_end:1",
        )
        checkpoints: dict[str, dict[str, object]] = {}
        for key in local_keys:
            checkpoint = self.lease.read_checkpoint(key=key, artifact_root=self.artifact_root)
            if checkpoint is not None:
                checkpoints[key] = checkpoint
        # Reservation checkpoint creation precedes the state/intent checkpoints.
        # Discover it from the journal so a crash at that first boundary cannot
        # resume proposal replay or reserve a second run-end check.
        reservation_checkpoint = self._run_end_reservation_checkpoint()
        if reservation_checkpoint is not None:
            reservation_receipt = reservation_checkpoint.get("receipt")
            reservation_key = (
                reservation_receipt.get("key") if isinstance(reservation_receipt, dict) else None
            )
            if not isinstance(reservation_key, str):
                raise RuntimeError("run-end reservation checkpoint key is malformed")
            checkpoints[reservation_key] = reservation_checkpoint
        from lab.director.holdout import (
            read_run_end_admission_registration,
            read_run_end_unavailable,
        )

        unavailable = read_run_end_unavailable(self.director_engine, run_id=self.run_id)
        intent_registered, reservation_exists = read_run_end_admission_registration(
            self.director_engine, run_id=self.run_id
        )
        if unavailable is not None:
            if "director-holdout-run-end-unavailable-intent:1" not in checkpoints:
                raise RuntimeError("run-end unavailable SQL fence has no local freeze checkpoint")
        started = bool(checkpoints) or intent_registered or reservation_exists
        if not started:
            return None

        status: object = None
        freeze = checkpoints.get("director-holdout-run-end-unavailable-intent:1")
        freeze_payload = freeze.get("payload") if isinstance(freeze, dict) else None
        if isinstance(freeze_payload, dict):
            status = freeze_payload.get("terminal_status")
            if status is None and freeze_payload.get("reason") == "fresh_wall_budget_unavailable":
                status = "budget_exhausted"
        intent = checkpoints.get("director-holdout-run-end-intent")
        intent_payload = intent.get("payload") if isinstance(intent, dict) else None
        if status is None and isinstance(intent_payload, dict):
            status = intent_payload.get("terminal_status")
        if status is None:
            status = (
                "proposal_limit_reached"
                if state.completed_proposals >= state.proposal_limit
                else "budget_exhausted"
            )
        if status not in {"proposal_limit_reached", "budget_exhausted"}:
            raise RuntimeError("durable run-end action has an invalid terminal status")
        if status == "proposal_limit_reached" and state.completed_proposals != state.proposal_limit:
            raise RuntimeError("run-end proposal-limit status differs from durable loop state")
        # The load path already selected the newest durable budget receipt. A frozen
        # unavailable intent is newer authority and must replace a stale state budget.
        if isinstance(freeze_payload, dict):
            frozen_budget = freeze_payload.get("budget_snapshot")
            frozen_budget_sha = freeze_payload.get("budget_snapshot_sha256")
            if (
                not isinstance(frozen_budget, dict)
                or self._budget_snapshot_sha256(frozen_budget) != frozen_budget_sha
            ):
                raise RuntimeError("run-end unavailable frozen budget failed verification")
            self.budget.restore(_budget_from_json(frozen_budget))
        terminal_result = self._result(
            state,
            state_checkpoint,
            cast(
                Literal["proposal_limit_reached", "budget_exhausted"],
                status,
            ),
        )
        return self.check_run_end_holdout(terminal_result)

    def _run_end_reservation_checkpoint(self) -> dict[str, object] | None:
        """Return the one durable run-end reservation, including pre-intent prefixes."""
        with self.director_engine.connect() as connection:
            keys = (
                connection.execute(
                    text(
                        "SELECT event_json->>'key' FROM lab.run_events "
                        "WHERE run_id=:run AND event_type='director.checkpoint' "
                        "AND event_json->>'phase'='holdout_budget_reserved' "
                        "AND event_json->>'key' LIKE 'holdout-budget-reserved:%' "
                        "ORDER BY (event_json->>'sequence')::integer"
                    ),
                    {"run": self.run_id},
                )
                .scalars()
                .all()
            )
        matches: list[dict[str, object]] = []
        for key in keys:
            if not isinstance(key, str):
                raise RuntimeError("durable holdout reservation key is malformed")
            checkpoint = self.lease.read_checkpoint(key=key, artifact_root=self.artifact_root)
            payload = checkpoint.get("payload") if checkpoint is not None else None
            receipt = checkpoint.get("receipt") if checkpoint is not None else None
            if not isinstance(payload, dict) or not isinstance(receipt, dict):
                raise RuntimeError("durable holdout reservation checkpoint is unavailable")
            if payload.get("trigger_kind") == "run_end" and payload.get("trigger_index") == 1:
                if (
                    payload.get("run_id") != str(self.run_id)
                    or receipt.get("phase") != "holdout_budget_reserved"
                    or receipt.get("key") != key
                    or payload.get("reservation_id") != key.removeprefix("holdout-budget-reserved:")
                ):
                    raise RuntimeError("run-end reservation checkpoint identity is inconsistent")
                matches.append(cast(dict[str, object], checkpoint))
        if len(matches) > 1:
            raise RuntimeError("run-end has multiple durable reservation intents")
        return matches[0] if matches else None

    def _reconcile_periodic_holdout(
        self, state: DirectorLoopState, state_checkpoint: dict[str, object]
    ) -> tuple[DirectorLoopState, dict[str, object]]:
        if not self.holdout_enabled:
            return state, state_checkpoint
        keep_ids = self._verified_scored_keep_ids()
        keep_count = len(keep_ids)
        if state.holdout_quota_exhausted:
            return state, state_checkpoint
        due = state.holdout_applied_keep_count + 10
        if keep_count < due:
            return state, state_checkpoint
        if keep_count != due:
            raise RuntimeError("holdout interval was crossed without applying its prior gate")
        candidate_id = keep_ids[due - 1]
        if candidate_id != state.champion_experiment_id:
            raise RuntimeError("periodic holdout candidate is not the durable champion")
        return self._apply_periodic_holdout_receipt(state, state_checkpoint, candidate_id, due)

    def _apply_periodic_holdout(
        self, state: DirectorLoopState, state_checkpoint: dict[str, object]
    ) -> tuple[DirectorLoopState, dict[str, object]]:
        if not self.holdout_enabled:
            return state, state_checkpoint
        if state.holdout_quota_exhausted:
            return state, state_checkpoint
        if state.holdout_approved_snapshot is None:
            raise RuntimeError("Director state has no holdout-approved champion snapshot")
        keep_count = len(self._verified_scored_keep_ids())
        if keep_count <= state.holdout_applied_keep_count or keep_count % 10:
            return state, state_checkpoint
        return self._apply_periodic_holdout_receipt(
            state, state_checkpoint, state.champion_experiment_id, keep_count
        )

    def _verified_scored_keep_ids(self) -> list[str]:
        """Read verdicts only through the Director's typed terminal receipt RPC."""
        with self.director_engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT experiment_id,status,experiment_number FROM lab.experiments "
                        "WHERE run_id=:run_id AND kind='proposal' "
                        "ORDER BY experiment_number,experiment_id"
                    ),
                    {"run_id": self.run_id},
                )
                .mappings()
                .all()
            )
        keeps: list[tuple[int, str]] = []
        seen_ordinals: set[int] = set()
        for row in rows:
            if row["status"] != "scored":
                continue
            ordinal = row["experiment_number"]
            experiment_id = row["experiment_id"]
            if (
                isinstance(ordinal, bool)
                or not isinstance(ordinal, int)
                or ordinal < 1
                or not isinstance(experiment_id, str)
                or ordinal in seen_ordinals
            ):
                raise RuntimeError("scored proposal ledger identity is malformed")
            document = self._read_terminal_document(experiment_id)
            if document.status != row["status"] or document.experiment_number != ordinal:
                raise RuntimeError("scored proposal receipt differs from its ledger row")
            if document.decision is not None and document.decision.verdict in {
                "KEEP",
                "KEEP_SIMPLER",
            }:
                seen_ordinals.add(ordinal)
                keeps.append((ordinal, experiment_id))
        keeps.sort()
        return [experiment_id for _, experiment_id in keeps]

    def _apply_periodic_holdout_receipt(
        self,
        state: DirectorLoopState,
        state_checkpoint: dict[str, object],
        candidate_id: str,
        keep_count: int,
    ) -> tuple[DirectorLoopState, dict[str, object]]:
        from lab.director.holdout import (
            HoldoutReceipt,
            evaluate_holdout_check,
            read_holdout_request,
        )

        remaining = int(self.budget.remaining_wall_seconds)
        existing = (
            None
            if self.holdout_evaluator is not None
            else read_holdout_request(
                self.director_engine,
                run_id=self.run_id,
                candidate_experiment_id=candidate_id,
                trigger_kind="keep_interval",
                trigger_index=keep_count,
            )
        )
        reservation: EpisodeReservation | None = None
        receipt: object | None = None
        if existing is not None:
            if existing.state not in {"passed", "reverted", "failed", "exhausted"}:
                if existing.state not in {"reserved", "running"}:
                    raise RuntimeError(
                        f"holdout reservation has unsupported state: {existing.state}"
                    )
                receipt = evaluate_holdout_check(
                    self.director_engine,
                    run_id=self.run_id,
                    candidate_experiment_id=candidate_id,
                    trigger_kind="keep_interval",
                    trigger_index=keep_count,
                    remaining_seconds=30,
                )
                if receipt.state not in {"passed", "reverted", "failed", "exhausted"}:
                    raise RuntimeError(f"holdout recovery remains pending: {receipt.state}")
            else:
                receipt = existing
        else:
            if remaining < 1:
                raise RuntimeError("holdout check cannot start after the immutable run wall budget")
            reservation = self.budget.reserve_work(wall_seconds=min(600, remaining), model_tokens=0)
            reservation_key = f"holdout-budget-reserved:{reservation.reservation_id}"
            self.lease.append_checkpoint(
                sequence=self._next_checkpoint_sequence(),
                key=reservation_key,
                phase="holdout_budget_reserved",
                payload={
                    "run_id": str(self.run_id),
                    "trigger_kind": "keep_interval",
                    "trigger_index": keep_count,
                    "candidate_experiment_id": candidate_id,
                    "reservation_id": str(reservation.reservation_id),
                    "wall_seconds": reservation.wall_seconds,
                    "model_tokens": reservation.model_tokens,
                    "budget": _budget_to_json(self.budget.snapshot()),
                },
                artifact_root=self.artifact_root,
            )
        if receipt is None:
            if reservation is None:
                raise RuntimeError("periodic holdout has no durable work reservation")
            started = time.monotonic()
            try:
                if self.holdout_evaluator is None:
                    receipt = evaluate_holdout_check(
                        self.director_engine,
                        run_id=self.run_id,
                        candidate_experiment_id=candidate_id,
                        trigger_kind="keep_interval",
                        trigger_index=keep_count,
                        remaining_seconds=reservation.wall_seconds,
                    )
                else:
                    receipt = self.holdout_evaluator(
                        candidate_id,
                        "keep_interval",
                        keep_count,
                        reservation.wall_seconds,
                    )
            except BaseException:
                # Leave the durable reservation unresolved. A restarted Director
                # charges its full bound before it can dispatch further work.
                raise
            elapsed = max(0.0, time.monotonic() - started)
            self._reconcile_holdout_budget(
                reservation, elapsed, trigger_kind="keep_interval", trigger_index=keep_count
            )
        if not isinstance(receipt, HoldoutReceipt):
            state_name = getattr(receipt, "state", "invalid")
            raise RuntimeError(f"periodic holdout did not produce a terminal bit: {state_name}")
        if receipt.state == "exhausted" and receipt.bit is None:
            return self._record_holdout_application(
                state,
                state_checkpoint,
                receipt,
                trigger_kind="keep_interval",
                trigger_index=keep_count,
                candidate_id=candidate_id,
            )
        if receipt.state not in {"passed", "reverted", "failed"}:
            raise RuntimeError(f"periodic holdout did not produce a terminal bit: {receipt.state}")
        return self._record_holdout_application(
            state,
            state_checkpoint,
            receipt,
            trigger_kind="keep_interval",
            trigger_index=keep_count,
            candidate_id=candidate_id,
        )

    def _record_holdout_application(
        self,
        state: DirectorLoopState,
        state_checkpoint: dict[str, object],
        receipt: object,
        *,
        trigger_kind: Literal["keep_interval", "run_end"],
        trigger_index: int,
        candidate_id: str,
    ) -> tuple[DirectorLoopState, dict[str, object]]:
        from lab.director.holdout import HoldoutReceipt
        from lab.director.ownership import active_execution_owner

        if not isinstance(receipt, HoldoutReceipt) or receipt.state not in {
            "passed",
            "reverted",
            "exhausted",
            "failed",
        }:
            raise ValueError("holdout application requires a terminal receipt")
        if receipt.state in {"exhausted", "failed"} and receipt.bit is not None:
            raise ValueError("non-result holdout status cannot contain a result bit")
        if receipt.state not in {"exhausted", "failed"} and receipt.bit is None:
            raise ValueError("terminal holdout result has no boolean bit")
        if (
            receipt.run_id != self.run_id
            or receipt.candidate_experiment_id != candidate_id
            or receipt.trigger_kind != trigger_kind
            or receipt.trigger_index != trigger_index
        ):
            raise ValueError("holdout result identity differs from its requested application")
        owner = active_execution_owner()
        if owner is None or owner.run_id != self.run_id:
            raise ValueError("holdout application lacks its captured execution owner")
        if receipt.admitted_generation != owner.generation:
            from lab.director.holdout_replay import append_terminal_holdout_suffix

            return append_terminal_holdout_suffix(
                self.director_engine,
                self.lease,
                receipt=receipt,
                prior_state_sha256=str(state_checkpoint["payload_sha256"]),
                artifact_root=self.artifact_root,
            )
        if receipt.execution_sha256 != owner.execution_sha256:
            raise ValueError("holdout receipt execution changed")
        key = f"holdout-application:{trigger_kind}:{trigger_index}"
        identity_payload = {
            "schema": "director-holdout-application.v1",
            "run_id": str(self.run_id),
            "trigger_kind": trigger_kind,
            "trigger_index": trigger_index,
            "candidate_experiment_id": candidate_id,
            "reservation_id": str(receipt.reservation_id),
            "admitted_generation": receipt.admitted_generation,
            "execution_sha256": receipt.execution_sha256,
            "state": receipt.state,
            "bit": receipt.bit,
            "prior_state_sha256": state_checkpoint.get("payload_sha256"),
        }
        existing = self.lease.read_checkpoint(key=key, artifact_root=self.artifact_root)
        closure_application = (
            trigger_kind == "run_end" and receipt.state == "failed" and receipt.bit is None
        )
        if existing is None:
            budget_snapshot = _budget_to_json(self.budget.snapshot())
            budget_proof: dict[str, object] = {}
            if closure_application:
                charged = self._latest_reconciled_run_end_budget()
                charged_payload = charged.get("payload") if charged is not None else None
                charged_receipt = charged.get("receipt") if charged is not None else None
                if (
                    not isinstance(charged_payload, dict)
                    or not isinstance(charged_receipt, dict)
                    or charged_payload.get("candidate_experiment_id") != candidate_id
                    or not isinstance(charged_payload.get("budget"), dict)
                ):
                    raise RuntimeError("failed run-end application lacks its exact charged budget")
                budget_snapshot = charged_payload["budget"]
                budget_proof = {
                    "budget_checkpoint_key": charged_receipt["key"],
                    "budget_checkpoint_sha256": charged_receipt["payload_sha256"],
                }
            payload = {
                **budget_proof,
                **identity_payload,
                "budget_snapshot": budget_snapshot,
                "budget_snapshot_sha256": self._budget_snapshot_sha256(budget_snapshot),
            }
            if closure_application:
                expected_state = self._state_after_holdout(
                    state,
                    receipt,
                    trigger_kind=trigger_kind,
                    trigger_index=trigger_index,
                    budget_snapshot=budget_snapshot,
                )
                payload["expected_state_sha256"] = hashlib.sha256(
                    canonical_bytes(expected_state.model_dump(mode="json", by_alias=True))
                ).hexdigest()
            append = (
                self.lease.append_holdout_closure_checkpoint
                if closure_application
                else self.lease.append_checkpoint
            )
            application_receipt = append(
                sequence=self._next_checkpoint_sequence(),
                key=key,
                phase="holdout_application",
                payload=payload,
                artifact_root=self.artifact_root,
            )
        else:
            existing_payload = existing.get("payload")
            if (
                existing.get("receipt", {}).get("phase") != "holdout_application"
                or not isinstance(existing_payload, dict)
                or any(
                    existing_payload.get(name) != value for name, value in identity_payload.items()
                )
            ):
                raise RuntimeError("holdout application checkpoint conflicts with its receipt")
            budget_snapshot = self._validated_application_budget(existing_payload)
            payload = existing_payload
            application_receipt = existing["receipt"]
        next_state = self._state_after_holdout(
            state,
            receipt,
            trigger_kind=trigger_kind,
            trigger_index=trigger_index,
            budget_snapshot=budget_snapshot,
        )
        next_checkpoint = self._persist_state(
            next_state,
            checkpoint_tag=("run_end" if trigger_kind == "run_end" else "keep_interval"),
            holdout_closure=closure_application,
        )
        if next_checkpoint.get("payload_sha256") is None:
            raise RuntimeError("holdout state checkpoint has no digest")
        # Bind state application to the exact terminal holdout application receipt.
        state_key = f"holdout-state-application:{trigger_kind}:{trigger_index}"
        state_payload = {
            "schema": "director-holdout-state-application.v1",
            "run_id": str(self.run_id),
            "application_sha256": application_receipt.get("payload_sha256"),
            "state_sha256": next_checkpoint.get("payload_sha256"),
        }
        old = self.lease.read_checkpoint(key=state_key, artifact_root=self.artifact_root)
        if old is None:
            append = (
                self.lease.append_holdout_closure_checkpoint
                if closure_application
                else self.lease.append_checkpoint
            )
            append(
                sequence=self._next_checkpoint_sequence(),
                key=state_key,
                phase="holdout_state_application",
                payload=state_payload,
                artifact_root=self.artifact_root,
            )
        elif old.get("payload") != state_payload:
            raise RuntimeError("holdout state application changed on resume")
        return next_state, next_checkpoint

    @staticmethod
    def _state_after_holdout(
        state: DirectorLoopState,
        receipt: object,
        *,
        trigger_kind: Literal["keep_interval", "run_end"],
        trigger_index: int,
        budget_snapshot: dict[str, object],
    ) -> DirectorLoopState:
        from lab.director.holdout import HoldoutReceipt

        if not isinstance(receipt, HoldoutReceipt):
            raise ValueError("holdout state transition requires a terminal receipt")
        if receipt.state == "exhausted" and receipt.bit is None:
            return state.model_copy(
                update={
                    "budget": budget_snapshot,
                    "holdout_quota_exhausted": True,
                    "holdout_last_status": "quota_exhausted",
                    "holdout_applied_keep_count": (
                        trigger_index
                        if trigger_kind == "keep_interval"
                        else state.holdout_applied_keep_count
                    ),
                    "recent_feedback": (
                        *state.recent_feedback,
                        "holdout quota exhausted; candidate promotion requires human review",
                    )[-30:],
                }
            ).verify_consistency()
        if receipt.state == "failed" and receipt.bit is None:
            snapshot = state.holdout_approved_snapshot
            if snapshot is None:
                raise RuntimeError("failed holdout has no approved champion snapshot")
            safe_state = snapshot.restore(
                state,
                keep_count=(
                    trigger_index
                    if trigger_kind == "keep_interval"
                    else state.holdout_applied_keep_count
                ),
            ).model_copy(
                update={
                    "holdout_approved_snapshot": snapshot,
                    "holdout_last_status": "failed",
                    "recent_feedback": (
                        *state.recent_feedback,
                        "holdout operational failure; champion restored to last approved snapshot; "
                        "final promotion requires human review",
                    )[-30:],
                }
            )
            return safe_state.model_copy(update={"budget": budget_snapshot}).verify_consistency()
        if receipt.bit is None:
            raise ValueError("terminal holdout result has no boolean bit")
        if trigger_kind == "keep_interval":
            snapshot = state.holdout_approved_snapshot
            if snapshot is None:
                raise RuntimeError("periodic holdout has no prior approved snapshot")
            next_snapshot = (
                HoldoutApprovedSnapshot.from_state(state, keep_count=trigger_index)
                if receipt.bit
                else snapshot
            )
            next_state = (
                state.model_copy(
                    update={
                        "holdout_approved_snapshot": next_snapshot,
                        "holdout_applied_keep_count": trigger_index,
                    }
                )
                if receipt.bit
                else snapshot.restore(state, keep_count=trigger_index).model_copy(
                    update={"holdout_approved_snapshot": snapshot}
                )
            )
            next_state = next_state.model_copy(
                update={
                    "holdout_checks_passed": state.holdout_checks_passed
                    + (1 if receipt.bit else 0),
                    "holdout_last_status": "passed" if receipt.bit else "reverted",
                }
            )
            if not receipt.bit:
                next_state = next_state.model_copy(
                    update={
                        "recent_feedback": (
                            *next_state.recent_feedback,
                            f"holdout keep={trigger_index}; bit=reverted; champion restored to "
                            f"{snapshot.experiment_id}",
                        )[-30:]
                    }
                )
        else:
            snapshot = state.holdout_approved_snapshot
            if snapshot is None:
                raise RuntimeError("run-end holdout has no approved champion snapshot")
            if receipt.bit:
                next_state = state.model_copy(
                    update={
                        "holdout_approved_snapshot": HoldoutApprovedSnapshot.from_state(
                            state, keep_count=state.holdout_applied_keep_count
                        ),
                        "holdout_checks_passed": state.holdout_checks_passed + 1,
                        "holdout_last_status": "passed",
                    }
                )
            else:
                next_state = snapshot.restore(
                    state, keep_count=state.holdout_applied_keep_count
                ).model_copy(update={"holdout_approved_snapshot": snapshot})
                next_state = next_state.model_copy(
                    update={
                        "holdout_last_status": "reverted",
                        "recent_feedback": (
                            *next_state.recent_feedback,
                            "holdout run_end; bit=reverted; champion restored to "
                            f"{snapshot.experiment_id}",
                        )[-30:],
                    }
                )
        return next_state.model_copy(update={"budget": budget_snapshot}).verify_consistency()

    @staticmethod
    def _budget_snapshot_sha256(snapshot: dict[str, object]) -> str:
        encoded = json.dumps(
            snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _validated_application_budget(cls, payload: dict[str, object]) -> dict[str, object]:
        snapshot = payload.get("budget_snapshot")
        digest = payload.get("budget_snapshot_sha256")
        if (
            not isinstance(snapshot, dict)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or cls._budget_snapshot_sha256(snapshot) != digest
        ):
            raise RuntimeError("holdout application has no hash-bound budget snapshot")
        _budget_from_json(snapshot)
        return snapshot

    def _reconcile_holdout_budget(
        self,
        reservation: EpisodeReservation,
        elapsed_seconds: float,
        *,
        trigger_kind: Literal["keep_interval", "run_end"],
        trigger_index: int,
        closure: bool = False,
    ) -> None:
        if elapsed_seconds <= reservation.wall_seconds:
            self.budget.reconcile_proposal(
                reservation,
                measured_wall_seconds=elapsed_seconds,
                measured_model_tokens=0,
            )
        else:
            self.budget.reconcile_proposal_overrun(
                reservation,
                measured_wall_seconds=elapsed_seconds,
                measured_model_tokens=0,
            )
        key = f"holdout-budget-reconciled:{reservation.reservation_id}"
        from lab.director.ownership import active_execution_owner

        owner = active_execution_owner() if trigger_kind == "run_end" else None
        if trigger_kind == "run_end" and (owner is None or owner.run_id != self.run_id):
            raise RuntimeError("run-end budget reconciliation lacks the captured owner")
        reserved_checkpoint = self.lease.read_checkpoint(
            key=f"holdout-budget-reserved:{reservation.reservation_id}",
            artifact_root=self.artifact_root,
        )
        reserved_payload = (
            reserved_checkpoint.get("payload") if reserved_checkpoint is not None else None
        )
        candidate_id = (
            reserved_payload.get("candidate_experiment_id")
            if isinstance(reserved_payload, dict)
            else None
        )
        checkpoint: dict[str, Any] = {
            "sequence": self._next_checkpoint_sequence(),
            "key": key,
            "phase": "holdout_budget_reconciled",
            "payload": {
                "run_id": str(self.run_id),
                "trigger_kind": trigger_kind,
                "trigger_index": trigger_index,
                "candidate_experiment_id": candidate_id,
                "reservation_id": str(reservation.reservation_id),
                "reserved_wall_seconds": reservation.wall_seconds,
                "measured_wall_seconds": elapsed_seconds,
                **(
                    {
                        "admitted_generation": owner.generation,
                        "execution_sha256": owner.execution_sha256,
                    }
                    if owner is not None
                    else {}
                ),
                "budget": _budget_to_json(self.budget.snapshot()),
            },
            "artifact_root": self.artifact_root,
        }
        if trigger_kind == "run_end" and closure:
            self.lease.append_holdout_closure_checkpoint(**checkpoint)
        else:
            self.lease.append_checkpoint(**checkpoint)

    def _recover_unresolved_holdout_budget(self) -> None:
        """Conservatively charge a persisted reservation after process loss."""
        with self.director_engine.connect() as connection:
            events = (
                connection.execute(
                    text(
                        "SELECT event_json->>'key' AS key FROM lab.run_events "
                        "WHERE run_id=:run AND event_type='director.checkpoint' "
                        "AND event_json->>'phase'='holdout_budget_reserved' "
                        "ORDER BY (event_json->>'sequence')::integer DESC LIMIT 32"
                    ),
                    {"run": self.run_id},
                )
                .mappings()
                .all()
            )
        for event in events:
            key = event["key"]
            if not isinstance(key, str):
                continue
            checkpoint = self.lease.read_checkpoint(key=key, artifact_root=self.artifact_root)
            if checkpoint is None or not isinstance(checkpoint.get("payload"), dict):
                raise RuntimeError("durable holdout reservation checkpoint is missing")
            payload = checkpoint["payload"]
            application = self.lease.read_checkpoint(
                key=f"holdout-application:{payload.get('trigger_kind')}:{payload.get('trigger_index')}",
                artifact_root=self.artifact_root,
            )
            marker = self.lease.read_checkpoint(
                key=f"holdout-state-application:{payload.get('trigger_kind')}:{payload.get('trigger_index')}",
                artifact_root=self.artifact_root,
            )
            if application is not None and marker is not None:
                if (
                    marker["payload"].get("application_sha256")
                    != application["receipt"]["payload_sha256"]
                ):
                    raise RuntimeError("holdout application marker digest changed")
                applied_budget = self._validated_application_budget(application["payload"])
                if not any(
                    str(item.reservation_id) == payload.get("reservation_id")
                    for item in _budget_from_json(applied_budget).reservations
                ):
                    # The immutable application already consumed this reservation once.
                    continue
            reservation_id = payload.get("reservation_id")
            if not isinstance(reservation_id, str):
                raise RuntimeError("durable holdout reservation identity is malformed")
            if (
                self.lease.read_checkpoint(
                    key=f"holdout-budget-reconciled:{reservation_id}",
                    artifact_root=self.artifact_root,
                )
                is not None
            ):
                continue
            if payload.get("run_id") != str(self.run_id):
                raise RuntimeError("durable holdout reservation belongs to another run")
            wall_seconds = payload.get("wall_seconds")
            if (
                isinstance(wall_seconds, bool)
                or not isinstance(wall_seconds, int)
                or wall_seconds < 1
            ):
                raise RuntimeError("durable holdout reservation bound is malformed")
            reservation = EpisodeReservation(UUID(reservation_id), wall_seconds, 0)
            persisted_budget = payload.get("budget")
            if not isinstance(persisted_budget, dict):
                raise RuntimeError("holdout reservation has no durable budget snapshot")
            reserved_budget = _budget_from_json(persisted_budget)
            if reservation not in reserved_budget.reservations:
                raise RuntimeError("holdout reservation is absent from its persisted budget")
            self.budget.restore(reserved_budget)
            if payload.get("trigger_kind") == "run_end":
                self._observe_execution_elapsed()
            trigger_kind = payload.get("trigger_kind")
            trigger_index = payload.get("trigger_index")
            if trigger_kind not in {"keep_interval", "run_end"} or not isinstance(
                trigger_index, int
            ):
                raise RuntimeError("durable holdout reservation trigger is malformed")
            # Charge all time up to the persisted pre-dispatch checkpoint plus the
            # complete reservation. The episode's unobserved time cannot be inferred.
            additional_wall = float(wall_seconds)
            self._reconcile_holdout_budget(
                reservation,
                additional_wall,
                trigger_kind=cast(Literal["keep_interval", "run_end"], trigger_kind),
                trigger_index=trigger_index,
                closure=trigger_kind == "run_end",
            )
            return

    @staticmethod
    def _state_checkpoint_key(
        state: DirectorLoopState, *, checkpoint_tag: str | None = None
    ) -> str:
        key = f"director-state:{state.completed_proposals}"
        if state.holdout_applied_keep_count:
            key += f":holdout:{state.holdout_applied_keep_count}"
        if checkpoint_tag is not None:
            key += f":{checkpoint_tag}"
        return key

    def _persist_proposal_intent(
        self,
        state: DirectorLoopState,
        *,
        ordinal: int,
        system: Literal["S1", "S2"],
        move_type: str,
        is_explore: bool,
    ) -> None:
        """Freeze the selected move and system before invoking the model."""
        configuration_sha256 = getattr(self.provider, "configuration_sha256", None)
        registry_sha256 = getattr(self.provider, "registry_entry_sha256", None)
        expected = {
            "run_id": str(self.run_id),
            "ordinal": ordinal,
            "move_type": move_type,
            "system": system,
            "is_explore": is_explore,
            "strategy_seed": state.strategy_seed,
            "strategy_policy_version": state.strategy_policy_version,
            "strategy_state_sha256": state.strategy_state_sha256,
            "parent_experiment_id": state.champion_experiment_id,
            "parent_tree_sha256": state.champion_tree_sha256,
            "provider_config_sha256": configuration_sha256,
            "provider_registry_entry_sha256": registry_sha256,
        }
        if is_explore:
            if state.explore_episode is None:
                raise RuntimeError("EXPLORE selection requires its prior durable family intent")
            expected["explore_episode_id"] = state.explore_episode.intent.episode_id
        key = f"proposal-intent:{ordinal}"
        existing = self.lease.read_checkpoint(key=key, artifact_root=self.artifact_root)
        if existing is not None:
            if existing.get("payload") != expected:
                raise RuntimeError("proposal intent changed on resume")
            return
        self.lease.require_run_active()
        self.lease.append_checkpoint(
            sequence=self._next_checkpoint_sequence(),
            key=key,
            phase="proposal_intent",
            payload=expected,
            artifact_root=self.artifact_root,
        )
        persisted = self.lease.read_checkpoint(key=key, artifact_root=self.artifact_root)
        if persisted is None or persisted.get("payload") != expected:
            raise RuntimeError("proposal intent did not survive durable checkpoint reload")

    def _is_explore(self, state: DirectorLoopState) -> bool:
        from lab.director.parameter_grid import ParameterGridProvider

        return (
            not isinstance(self.provider, ParameterGridProvider)
            and state.consecutive_non_keep >= 25
            and state.explore_proposals < 10
        )

    def _prepare_explore_episode(
        self, state: DirectorLoopState, state_checkpoint: dict[str, object]
    ) -> tuple[DirectorLoopState, dict[str, object]]:
        new_episode = state.explore_episode is None
        if new_episode:
            intent = begin_explore(
                run_id=self.run_id,
                start_ordinal=state.next_ordinal,
                champion_experiment_id=state.champion_experiment_id,
                champion_source=self._champion_source(state),
            )
            state = state.model_copy(
                update={
                    "explore_episode": ExploreEpisode(intent=intent),
                    "explore_proposals": 0,
                    "explore_family": intent.target_family,
                }
            ).verify_consistency()
        episode = state.explore_episode
        if episode is None or episode.phase != "EXPLORE" or episode.intent.run_id != self.run_id:
            raise RuntimeError("EXPLORE intent is not pending for this run")
        if state.next_ordinal != episode.intent.start_ordinal + len(episode.resolutions):
            raise RuntimeError("EXPLORE ordinal differs from its terminal prefix")
        if new_episode:
            state_checkpoint = self._persist_state(
                state, checkpoint_tag=f"explore-intent-{episode.intent.start_ordinal}"
            )
        return state, state_checkpoint

    def _prepare_strategy_intent(
        self,
        state: DirectorLoopState,
        *,
        ordinal: int,
        is_explore: bool,
    ) -> tuple[
        DirectorLoopState,
        dict[str, object],
        StrategySelection,
        Literal["S1", "S2"],
    ]:
        """Persist and verify one immutable Thompson move before provider use."""
        if state.schema_version != "director-loop-state.v2":
            raise RuntimeError("Thompson strategy is unavailable in a legacy loop checkpoint")
        if ordinal != state.next_ordinal:
            raise RuntimeError("Thompson intent ordinal differs from the loop ordinal")
        strategy = state.load_strategy_state()
        selected_state, selection = select_strategy_move(strategy, ordinal)
        pending_state = self._with_strategy_state(state, selected_state)
        tag = f"strategy-intent-{ordinal}"
        receipt = self._persist_state(pending_state, checkpoint_tag=tag)
        key = self._state_checkpoint_key(pending_state, checkpoint_tag=tag)
        persisted = self.lease.read_checkpoint(key=key, artifact_root=self.artifact_root)
        expected_state = pending_state.model_dump(mode="json", by_alias=True)
        if persisted is None or persisted.get("payload") != expected_state:
            raise RuntimeError("Thompson intent failed durable checkpoint reload")
        payload = persisted.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeError("reloaded Thompson checkpoint payload is malformed")
        reloaded_state = DirectorLoopState.model_validate_json(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            strict=True,
        ).verify_consistency()
        reloaded_strategy = reloaded_state.load_strategy_state()
        if (
            reloaded_strategy.pending is None
            or reloaded_strategy.pending.model_dump(mode="json")
            != selection.model_dump(mode="json")
            or reloaded_state.strategy_seed != strategy.seed
            or reloaded_state.strategy_policy_version != strategy.policy_version
        ):
            raise RuntimeError("reloaded Thompson intent differs from selected strategy move")
        system = route_system(
            "experiment",
            selection.move_type,
            explore=is_explore,
            discard_streak=reloaded_state.discard_streak,
        )
        self._persist_proposal_intent(
            reloaded_state,
            ordinal=ordinal,
            system=system,
            move_type=selection.move_type,
            is_explore=is_explore,
        )
        intent = self.lease.read_checkpoint(
            key=f"proposal-intent:{ordinal}", artifact_root=self.artifact_root
        )
        intent_payload = intent.get("payload") if intent is not None else None
        if (
            not isinstance(intent_payload, dict)
            or intent_payload.get("strategy_state_sha256") != reloaded_state.strategy_state_sha256
            or intent_payload.get("strategy_seed") != strategy.seed
            or intent_payload.get("strategy_policy_version") != strategy.policy_version
            or intent_payload.get("move_type") != selection.move_type
            or intent_payload.get("system") != system
            or intent_payload.get("is_explore") is not is_explore
        ):
            raise RuntimeError("provider intent does not bind the durable Thompson selection")
        self.lease.require_run_active()
        return reloaded_state, receipt, selection, system

    def _next_checkpoint_sequence(self) -> int:
        with self.director_engine.connect() as connection:
            value = connection.execute(
                text(
                    "SELECT max((event_json->>'sequence')::integer) FROM lab.run_events "
                    "WHERE run_id=:run AND event_type='director.checkpoint'"
                ),
                {"run": self.run_id},
            ).scalar_one()
        return 0 if value is None else int(value) + 1

    def _observe_execution_elapsed(self) -> None:
        """Keep downtime against the original SQL start without changing durable receipts."""
        from lab.director.ownership import active_execution_owner

        owner = active_execution_owner()
        if owner is None or owner.run_id != self.run_id:
            raise RuntimeError("elapsed accounting requires the captured execution owner")
        with self.director_engine.connect() as connection:
            elapsed = connection.execute(
                text(
                    "SELECT greatest(0,extract(epoch FROM clock_timestamp()-started_at)) "
                    "FROM lab.director_execution_contracts "
                    "WHERE run_id=:run_id AND execution_sha256=:execution_sha256"
                ),
                {"run_id": self.run_id, "execution_sha256": owner.execution_sha256},
            ).scalar_one()
        self.budget.observe_elapsed(float(elapsed))

    def _restore_budget(self, payload: dict[str, object], *, after_sequence: int) -> None:
        restored = _budget_from_json(payload)
        self.budget.restore(self._latest_budget_checkpoint(restored, after_sequence=after_sequence))
        self._observe_execution_elapsed()

    def _latest_budget_checkpoint(
        self, baseline: BudgetSnapshot, *, after_sequence: int = -1
    ) -> BudgetSnapshot:
        with self.director_engine.connect() as connection:
            events = (
                connection.execute(
                    text(
                        "SELECT event_json->>'key' AS key, "
                        "(event_json->>'sequence')::integer AS sequence FROM lab.run_events "
                        "WHERE run_id=:run AND event_type='director.checkpoint' "
                        "ORDER BY (event_json->>'sequence')::integer DESC LIMIT 256"
                    ),
                    {"run": self.run_id},
                )
                .mappings()
                .all()
            )
        phases = {
            "proposal_budget_reconciled",
            "primary_measured",
            "confirmation_measured",
            "seed_complete",
            "baseline_measured",
            "holdout_budget_reserved",
            "holdout_budget_reconciled",
            "holdout_run_end_unavailable_intent",
        }
        for event in events:
            key = event["key"]
            sequence = event.get("sequence")
            if not isinstance(key, str) or (
                after_sequence >= 0
                and (
                    isinstance(sequence, bool)
                    or not isinstance(sequence, int)
                    or sequence <= after_sequence
                )
            ):
                continue
            checkpoint = self.lease.read_checkpoint(key=key, artifact_root=self.artifact_root)
            if checkpoint is None:
                continue
            payload = checkpoint["payload"]
            phase = checkpoint["receipt"].get("phase")
            if (
                isinstance(payload, dict)
                and phase in phases
                and (
                    phase != "holdout_run_end_unavailable_intent"
                    or payload.get("run_id") == str(self.run_id)
                )
            ):
                budget_key = (
                    "budget_snapshot" if phase == "holdout_run_end_unavailable_intent" else "budget"
                )
                snapshot = payload.get(budget_key)
                if isinstance(snapshot, dict):
                    return _budget_from_json(snapshot)
        return baseline

    def _champion_source(self, state: DirectorLoopState) -> bytes:
        source = read_director_artifact(
            state.champion_source_blob_sha256, artifact_root=self.artifact_root
        )
        if hashlib.sha256(source).hexdigest() != state.champion_source_sha256:
            raise RuntimeError("current champion source blob failed its state hash")
        return source

    def _task_cards(
        self, state: DirectorLoopState, calibration: FrozenCalibrationDocument
    ) -> tuple[str, ...]:
        entries = []
        calibration_by_id = {task.task_id: task for task in calibration.tasks}
        for task in self.tasks:
            reference = calibration_by_id[task.task_id]
            entries.append(
                f"task={task.task_id}; family={task.family}; weight={task.task_weight:.6g}; "
                f"allowed_signals={','.join(task.context.signals)}; "
                f"regime_signals={','.join(task.context.regime_signals)}; "
                f"sampling_s={task.context.sampling_s}; "
                f"fit_budget_s={task.context.time_budget_s:.6g}; "
                f"train_rows={task._train.shape[0]}; eval_rows={task._evaluation.shape[0]}; "
                f"champion_seed0={state.champion_seed0_by_task[task.task_id]:.8g}; "
                f"champion_seed1={state.champion_seed1_by_task[task.task_id]:.8g}; "
                f"base={reference.base_score:.8g}; reference={reference.reference_score:.8g}"
            )
        return tuple(entries)

    def _run_one(
        self,
        *,
        state: DirectorLoopState,
        calibration: FrozenCalibrationDocument,
        ordinal: int,
        system: Literal["S1", "S2"],
        context: AgentContext,
    ) -> RegisteredProposal:
        source = self._champion_source(state)
        parent_scores = {
            task_id: (state.champion_seed0_by_task[task_id] + state.champion_seed1_by_task[task_id])
            / 2.0
            for task_id in state.champion_seed0_by_task
        }
        tree = state.champion_tree_sha256
        _run_proposal(
            self.director_engine,
            self.planner_engine,
            lease=self.lease,
            runner=self.runner,
            run_id=self.run_id,
            ordinal=ordinal,
            parent_experiment_id=state.champion_experiment_id,
            parent_tree_sha256=tree,
            parent_source=source,
            suite_id=state.suite_id,
            suite_version=state.suite_version,
            calibration=calibration,
            harness_sha256=state.harness_sha256,
            image_sha256=state.image_sha256,
            system=system,
            context=context,
            provider=self.provider,
            tasks=self.tasks,
            budget=self.budget,
            artifact_root=self.artifact_root,
            best_suite=state.best_suite,
            screen_champion_scores_by_task=state.champion_seed0_by_task,
            champion_scores_by_task=parent_scores,
            champion_noise=state.champion_noise_sd,
            seed_wall_seconds=self.seed_wall_seconds,
        )
        stored = self.lease.read_checkpoint(
            key=f"proposal:{ordinal}", artifact_root=self.artifact_root
        )
        if stored is None or not isinstance(stored.get("payload"), dict):
            raise RuntimeError("proposal call has no immutable preregistration checkpoint")
        payload = stored["payload"]
        raw_proposal = payload.get("proposal")
        if not isinstance(raw_proposal, dict):
            raise RuntimeError("proposal checkpoint has no candidate proposal contract")
        candidate = CandidateProposal.model_validate(raw_proposal, strict=True)
        return RegisteredProposal(
            experiment_id=str(payload["experiment_id"]),
            experiment_number=ordinal,
            proposal=candidate,
            candidate_sha256=str(payload["candidate_sha256"]),
            candidate_blob_sha256=str(payload["candidate_blob_sha256"]),
            inputs_sha256=str(payload["inputs_sha256"]),
            messages_blob_sha256=str(payload["messages_blob_sha256"]),
            calibration_sha256=str(payload["calibration_sha256"]),
            parent_experiment_id=str(payload["parent_experiment_id"]),
            parent_tree_sha256=str(payload["parent_tree_sha256"]),
            harness_sha256=str(payload["harness_sha256"]),
            image_sha256=str(payload["image_sha256"]),
            suite_id=str(payload["suite_id"]),
            suite_version=int(payload["suite_version"]),
            system=system,
            input_tokens=int(payload["input_tokens"]),
            output_tokens=int(payload["output_tokens"]),
            ledger_sequence=int(payload["sequence"]),
        )

    def _read_terminal_document(self, experiment_id: str) -> ExperimentDocument:
        with self.director_engine.connect() as connection:
            receipt = connection.execute(
                text("SELECT lab.experiment_record_receipt(:id)"), {"id": experiment_id}
            ).scalar_one()
        if not isinstance(receipt, dict) or receipt.get("run_id") != str(self.run_id):
            raise RuntimeError("proposal has no valid immutable terminal receipt")
        payload = read_director_artifact(
            str(receipt["experiment_blob_sha256"]), artifact_root=self.artifact_root
        )
        if hashlib.sha256(payload).hexdigest() != receipt.get("experiment_sha256"):
            raise RuntimeError("proposal terminal blob does not match its receipt")
        document = ExperimentDocument.model_validate_json(payload, strict=True)
        if document.experiment_id != experiment_id or document.run_id != self.run_id:
            raise RuntimeError("proposal terminal document identity mismatch")
        return document

    def _recover_committed_terminal(
        self,
        state: DirectorLoopState,
        state_checkpoint: dict[str, object],
        calibration: FrozenCalibrationDocument,
    ) -> tuple[DirectorLoopState, dict[str, object], bool] | None:
        """Advance a terminal committed proposal without rerunning external work."""
        ordinal = state.next_ordinal
        with self.director_engine.connect() as connection:
            terminal_identity = (
                connection.execute(
                    text(
                        "SELECT experiment_id,status FROM lab.experiments WHERE run_id=:run_id "
                        "AND experiment_number=:ordinal AND kind='proposal'"
                    ),
                    {"run_id": self.run_id, "ordinal": ordinal},
                )
                .mappings()
                .one_or_none()
            )
        stored = self.lease.read_checkpoint(
            key=f"proposal:{ordinal}", artifact_root=self.artifact_root
        )
        if stored is None or not isinstance(stored.get("payload"), dict):
            if terminal_identity is not None and terminal_identity["status"] in {
                "scored",
                "crashed",
                "abandoned",
                "rejected",
            }:
                raise RuntimeError("terminal proposal record has no immutable proposal checkpoint")
            return None
        payload = stored["payload"]
        if payload.get("run_id") != str(self.run_id) or payload.get("experiment_number") != ordinal:
            raise RuntimeError("registered proposal checkpoint differs from next Director ordinal")
        experiment_id = payload.get("experiment_id")
        if not isinstance(experiment_id, str):
            raise RuntimeError("registered proposal checkpoint has no experiment identity")
        status = (
            terminal_identity["status"]
            if terminal_identity is not None and terminal_identity["experiment_id"] == experiment_id
            else None
        )
        if status is None or status not in {"scored", "crashed", "abandoned", "rejected"}:
            return None
        document = self._read_terminal_document(experiment_id)
        if (
            document.experiment_number != ordinal
            or document.status != status
            or document.candidate_sha256 != payload.get("candidate_sha256")
            or document.candidate_blob_sha256 != payload.get("candidate_blob_sha256")
            or document.parent_experiment_id != state.champion_experiment_id
            or document.parent_tree != state.champion_tree_sha256
            or document.calibration_sha256 != state.calibration_sha256
            or document.harness_sha256 != state.harness_sha256
            or document.image_sha256 != state.image_sha256
            or document.suite_id != state.suite_id
            or document.suite_version != state.suite_version
        ):
            raise RuntimeError(
                "committed terminal record differs from the pending proposal identity"
            )
        proposal = self._proposal_from_payload(payload, ordinal=ordinal)
        intent = self.lease.read_checkpoint(
            key=f"proposal-intent:{ordinal}", artifact_root=self.artifact_root
        )
        is_explore = self._is_explore(state)
        if intent is not None:
            intent_payload = intent.get("payload")
            if not isinstance(intent_payload, dict) or intent_payload.get("run_id") != str(
                self.run_id
            ):
                raise RuntimeError("committed terminal proposal has an invalid intent checkpoint")
            is_explore = intent_payload.get("is_explore", is_explore) is True
        updated = self._advance_state(state, proposal, document, calibration, is_explore)
        checkpoint = self._persist_state(updated)
        promoted = (
            document.status == "scored"
            and document.decision is not None
            and document.decision.verdict in {"KEEP", "KEEP_SIMPLER"}
        )
        return updated, checkpoint, promoted

    def _proposal_from_payload(
        self, payload: dict[str, object], *, ordinal: int
    ) -> RegisteredProposal:
        raw_proposal = payload.get("proposal")
        if not isinstance(raw_proposal, dict):
            raise RuntimeError("registered proposal checkpoint has no candidate contract")
        candidate = CandidateProposal.model_validate(raw_proposal, strict=True)
        source = candidate.candidate_source.encode("utf-8")
        digest = hashlib.sha256(source).hexdigest()
        if digest != payload.get("candidate_sha256") or digest != payload.get(
            "candidate_blob_sha256"
        ):
            raise RuntimeError("registered proposal source differs from its pinned hash")
        suite_version = payload.get("suite_version")
        input_tokens = payload.get("input_tokens")
        output_tokens = payload.get("output_tokens")
        sequence = payload.get("sequence")
        system = payload.get("system")
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (suite_version, input_tokens, output_tokens, sequence)
        ) or system not in {"S1", "S2"}:
            raise RuntimeError("registered proposal checkpoint counters are malformed")
        return RegisteredProposal(
            experiment_id=str(payload["experiment_id"]),
            experiment_number=ordinal,
            proposal=candidate,
            candidate_sha256=digest,
            candidate_blob_sha256=str(payload["candidate_blob_sha256"]),
            inputs_sha256=str(payload["inputs_sha256"]),
            messages_blob_sha256=str(payload["messages_blob_sha256"]),
            calibration_sha256=str(payload["calibration_sha256"]),
            parent_experiment_id=str(payload["parent_experiment_id"]),
            parent_tree_sha256=str(payload["parent_tree_sha256"]),
            harness_sha256=str(payload["harness_sha256"]),
            image_sha256=str(payload["image_sha256"]),
            suite_id=str(payload["suite_id"]),
            suite_version=cast(int, suite_version),
            system=cast(Literal["S1", "S2"], system),
            input_tokens=cast(int, input_tokens),
            output_tokens=cast(int, output_tokens),
            ledger_sequence=cast(int, sequence),
        )

    def _advance_state(
        self,
        state: DirectorLoopState,
        proposal: RegisteredProposal,
        document: ExperimentDocument,
        calibration: FrozenCalibrationDocument,
        is_explore: bool,
    ) -> DirectorLoopState:
        if document.experiment_number != state.next_ordinal:
            raise RuntimeError("terminal proposal ordinal differs from the durable loop state")
        if is_explore:
            from lab.director.explore import require_target_family

            if state.explore_episode is None or proposal.system != "S2":
                raise RuntimeError("EXPLORE terminal requires its exact S2 intent")
            require_target_family(
                proposal.proposal.candidate_source.encode("utf-8"), state.explore_episode.intent
            )
        strategy = state.load_strategy_state()
        if (
            strategy.pending is None
            or strategy.pending.ordinal != state.next_ordinal
            or strategy.pending.move_type != proposal.proposal.move_type
        ):
            raise RuntimeError("terminal proposal differs from its preregistered move intent")
        verdict = document.decision.verdict if document.decision is not None else "REJECT"
        if document.status == "abandoned":
            strategy_outcome: StrategyOutcome = "ABANDONED"
        elif verdict in {"KEEP", "KEEP_SIMPLER", "DISCARD"}:
            strategy_outcome = cast(StrategyOutcome, verdict)
        elif document.status == "crashed":
            strategy_outcome = "CANDIDATE_FAILURE"
        elif verdict == "REJECT":
            strategy_outcome = (
                "POLICY_REJECT"
                if document.decision is not None
                and document.decision.reason
                in {
                    "hardcoding_reject",
                    "determinism_reject",
                    "causality_reject",
                    "position_bias_reject",
                }
                else "CANDIDATE_REJECT"
            )
        else:
            raise RuntimeError("terminal proposal has no resolvable Thompson outcome")
        resolved_strategy = resolve_strategy_selection(
            strategy, state.next_ordinal, strategy_outcome
        )
        promoted = verdict in {"KEEP", "KEEP_SIMPLER"} and document.status == "scored"
        dev_metrics = json.dumps(
            [item.model_dump(mode="json") for item in document.per_task], sort_keys=True
        )
        feedback = (
            f"experiment={state.next_ordinal}; verdict={verdict}; reason="
            f"{document.decision.reason if document.decision else 'no_measurement'}; "
            "suite_score="
            f"{document.suite_score if document.suite_score is not None else 'unmeasured'}; "
            f"guards={json.dumps(document.guards, sort_keys=True)}; "
            f"dev_metrics={dev_metrics}"
        )[:2048]
        recent = (*state.recent_feedback, feedback)[-30:]
        if promoted and state.holdout_quota_exhausted:
            recent = (
                *recent,
                "dev KEEP; final promotion requires human review (holdout quota exhausted)",
            )[-30:]
        crashes = state.consecutive_candidate_crashes + 1 if document.status == "crashed" else 0
        if promoted:
            raw0 = self._seed_measurements(proposal, "primary", 0)
            raw1 = self._seed_measurements(proposal, "confirmation", 1)
            raw2 = self._seed_measurements(proposal, "confirmation", 2)
            seed_results: tuple[
                tuple[PrimaryEvaluationResult, Literal["primary", "confirmation"], int], ...
            ] = (
                (raw0, "primary", 0),
                (raw1, "confirmation", 1),
                (raw2, "confirmation", 2),
            )
            weighted = tuple(
                _weighted_suite_seed_scores(
                    result,
                    self.tasks,
                    calibration,
                    candidate_sha256=proposal.candidate_sha256,
                    evaluation_kind=kind,
                    seed=seed,
                )
                for result, kind, seed in seed_results
            )
            source = proposal.proposal.candidate_source.encode("utf-8")
            source_blob = hashlib.sha256(source).hexdigest()
            from lab.director.artifacts import store_director_artifact

            blob = store_director_artifact(source, artifact_root=self.artifact_root)
            if blob != source_blob:
                raise RuntimeError("promoted candidate source blob hash mismatch")
            champ_id = proposal.experiment_id
            seed0 = {
                str(item["task_id"]): _required_promoted_task_score(item)
                for item in raw0.measurements
            }
            seed1 = {
                str(item["task_id"]): _required_promoted_task_score(item)
                for item in raw1.measurements
            }
            noise = champion_noise_sd({0: weighted[0], 1: weighted[1], 2: weighted[2]})
            if document.suite_score is None or not math.isfinite(document.suite_score):
                raise RuntimeError("promoted proposal has no finite confirmed suite score")
            best = max(state.best_suite, document.suite_score)
            tree = _candidate_git_tree(source)
        else:
            source_blob = state.champion_source_blob_sha256
            champ_id = state.champion_experiment_id
            seed0 = state.champion_seed0_by_task
            seed1 = state.champion_seed1_by_task
            weighted = state.champion_suite_seed_scores
            noise = state.champion_noise_sd
            best = state.best_suite
            tree = state.champion_tree_sha256
            blob = source_blob
        non_keep = 0 if promoted else state.consecutive_non_keep + 1
        discard_streak = state.discard_streak + 1 if verdict == "DISCARD" else 0
        episode = state.explore_episode
        if is_explore:
            if episode is None:
                raise RuntimeError("EXPLORE terminal record has no durable episode")
            outcome = (
                "ABANDONED"
                if document.status == "abandoned"
                else "CANDIDATE_FAILURE"
                if document.status == "crashed"
                else verdict
                if verdict in {"KEEP", "KEEP_SIMPLER", "DISCARD"}
                else "REJECT"
            )
            episode = episode.resolve(
                ExploreResolution(
                    ordinal=state.next_ordinal,
                    terminal_sha256=hashlib.sha256(
                        canonical_bytes(document.model_dump(mode="json", by_alias=True))
                    ).hexdigest(),
                    outcome=cast(Any, outcome),
                )
            )
        if promoted:
            episode = None
        explore_count = len(episode.resolutions) if episode is not None else 0
        explore_family = episode.intent.target_family if episode is not None else None
        next_payload: dict[str, object] = {
            **_strategy_checkpoint_fields(resolved_strategy),
            "run_id": str(self.run_id),
            "suite_manifest_sha256": state.suite_manifest_sha256,
            "suite_id": state.suite_id,
            "suite_version": state.suite_version,
            "calibration_sha256": state.calibration_sha256,
            "harness_sha256": state.harness_sha256,
            "image_sha256": state.image_sha256,
            "proposal_limit": state.proposal_limit,
            "completed_proposals": state.completed_proposals + 1,
            "next_ordinal": state.next_ordinal + 1,
            "champion_experiment_id": champ_id,
            "champion_source_sha256": source_blob,
            "champion_tree_sha256": tree,
            "champion_source_blob_sha256": blob,
            "champion_seed0_by_task": seed0,
            "champion_seed1_by_task": seed1,
            "champion_suite_seed_scores": weighted,
            "champion_noise_sd": float(noise),
            "best_suite": best,
            "consecutive_non_keep": non_keep,
            "explore_proposals": explore_count,
            "explore_family": explore_family,
            "explore_episode": episode.model_dump(mode="json", by_alias=True) if episode else None,
            "termination_reason": "explore_exhausted"
            if episode and episode.phase == "HOLDOUT_CHECK"
            else None,
            "loop_phase": "HOLDOUT_CHECK" if episode and episode.phase == "HOLDOUT_CHECK" else None,
            "consecutive_candidate_crashes": crashes,
            "discard_streak": discard_streak,
            "previous_move_type": proposal.proposal.move_type,
            "recent_feedback": recent,
            "budget": _budget_to_json(self.budget.snapshot()),
            "holdout_approved_snapshot": (
                state.holdout_approved_snapshot.model_dump(mode="json")
                if state.holdout_approved_snapshot is not None
                else None
            ),
            "holdout_applied_keep_count": state.holdout_applied_keep_count,
            "holdout_quota_exhausted": state.holdout_quota_exhausted,
            "holdout_checks_passed": state.holdout_checks_passed,
            "holdout_last_status": state.holdout_last_status,
        }
        return DirectorLoopState.model_validate_json(
            json.dumps(
                next_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
            strict=True,
        ).verify_consistency()

    def _seed_measurements(
        self, proposal: RegisteredProposal, kind: Literal["primary", "confirmation"], seed: int
    ) -> PrimaryEvaluationResult:
        rows = []
        for task in self.tasks:
            row = _read_measurement(
                self.director_engine,
                lease=self.lease,
                run_id=self.run_id,
                experiment_id=proposal.experiment_id,
                evaluation_kind=kind,
                seed=seed,
                task=task,
                candidate_sha256=proposal.candidate_sha256,
                harness_sha256=proposal.harness_sha256,
                experiment_number=proposal.experiment_number,
                artifact_root=self.artifact_root,
            )
            if row is None:
                raise RuntimeError("promoted proposal lacks a complete seed checkpoint")
            rows.append(row)
        return PrimaryEvaluationResult(
            experiment_id=proposal.experiment_id,
            status="primary_measured",
            measurements=tuple(rows),
            terminal_code=None,
            wall_seconds=0.0,
            evaluation_kind=kind,
            seed=seed,
        )

    def _result(
        self,
        state: DirectorLoopState,
        receipt: dict[str, object],
        status: Literal[
            "proposal_limit_reached",
            "paused_candidate_crashes",
            "budget_exhausted",
            "explore_family_unverified",
        ],
    ) -> DirectorLoopResult:
        digest = receipt.get("payload_sha256")
        if not isinstance(digest, str):
            raise RuntimeError("final Director state has no hash receipt")
        return DirectorLoopResult(
            run_id=self.run_id,
            status=status,
            completed_proposals=state.completed_proposals,
            next_ordinal=state.next_ordinal,
            champion_experiment_id=state.champion_experiment_id,
            best_suite=state.best_suite,
            checkpoint_sha256=digest,
            termination_reason=(
                "explore_exhausted"
                if state.explore_episode is not None
                and state.explore_episode.phase == "HOLDOUT_CHECK"
                else None
            ),
        )


def _budget_to_json(snapshot: BudgetSnapshot) -> dict[str, object]:
    return {
        "proposal_count": snapshot.proposal_count,
        "wall_seconds": snapshot.wall_seconds,
        "model_tokens": snapshot.model_tokens,
        "reserved_wall_seconds": snapshot.reserved_wall_seconds,
        "reserved_model_tokens": snapshot.reserved_model_tokens,
        "elapsed_wall_seconds": snapshot.elapsed_wall_seconds,
        "reservations": [
            {
                "reservation_id": str(item.reservation_id),
                "wall_seconds": item.wall_seconds,
                "model_tokens": item.model_tokens,
            }
            for item in snapshot.reservations
        ],
    }


def _budget_from_json(payload: dict[str, object]) -> BudgetSnapshot:
    reservations_value = payload.get("reservations")
    if not isinstance(reservations_value, list):
        raise ValueError("Director budget checkpoint has no reservation list")
    reservations_list: list[EpisodeReservation] = []
    for item in reservations_value:
        if not isinstance(item, dict):
            raise ValueError("Director budget reservation is malformed")
        reservation_id = item.get("reservation_id")
        wall_seconds = item.get("wall_seconds")
        model_tokens = item.get("model_tokens")
        if (
            not isinstance(reservation_id, str)
            or isinstance(wall_seconds, bool)
            or not isinstance(wall_seconds, int)
            or isinstance(model_tokens, bool)
            or not isinstance(model_tokens, int)
        ):
            raise ValueError("Director budget reservation fields have invalid types")
        reservations_list.append(
            EpisodeReservation(
                reservation_id=UUID(reservation_id),
                wall_seconds=wall_seconds,
                model_tokens=model_tokens,
            )
        )
    reservations = tuple(reservations_list)

    def strict_int(name: str) -> int:
        value = payload.get(name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"Director budget {name} must be an integer")
        return value

    def finite_number(name: str) -> float:
        value = payload.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Director budget {name} must be numeric")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"Director budget {name} must be finite")
        return number

    return BudgetSnapshot(
        proposal_count=strict_int("proposal_count"),
        wall_seconds=finite_number("wall_seconds"),
        model_tokens=strict_int("model_tokens"),
        reserved_wall_seconds=finite_number("reserved_wall_seconds"),
        reserved_model_tokens=strict_int("reserved_model_tokens"),
        elapsed_wall_seconds=finite_number("elapsed_wall_seconds"),
        reservations=reservations,
    )


def _required_score(row: dict[str, object], key: str) -> float:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError(f"measured score {key} is missing or malformed")
    score = float(value)
    if not math.isfinite(score):
        raise RuntimeError(f"measured score {key} is non-finite")
    return score


def _required_promoted_task_score(row: dict[str, object]) -> float:
    """Persist the family-primary score for newly promoted candidates.

    Older EVT-only fixtures may predate the common ``task_score`` alias and
    contain only ``vus_pr``. New PDM/NRM rows require ``task_score`` and may
    never fall back to a VUS metric.
    """
    family = row.get("task_family")
    if family in {"PDM", "NRM"}:
        return _required_score(row, "task_score")
    if "task_score" in row:
        return _required_score(row, "task_score")
    return _required_score(row, "vus_pr")


def run_proposal_loop(
    director_engine: Engine,
    planner_engine: Engine,
    *,
    lease: DirectorRunLease,
    runner: LocalDockerRunner,
    run_id: UUID,
    tasks: tuple[SuiteTask, ...],
    suite_manifest_sha256: str,
    provider: ProposalProvider,
    budget: RunBudget,
    artifact_root: Path,
    proposal_limit: int,
    seed_wall_seconds: int = MAX_SEED_WALL_SECONDS,
    holdout_evaluator: Callable[[str, Literal["keep_interval", "run_end"], int, int], object]
    | None = None,
    holdout_enabled: bool = False,
) -> DirectorLoopResult:
    """Public functional API used by the production `lab run` command."""
    return DirectorLoop(
        director_engine,
        planner_engine,
        lease=lease,
        runner=runner,
        run_id=run_id,
        tasks=tasks,
        suite_manifest_sha256=suite_manifest_sha256,
        provider=provider,
        budget=budget,
        artifact_root=artifact_root,
        proposal_limit=proposal_limit,
        seed_wall_seconds=seed_wall_seconds,
        holdout_evaluator=holdout_evaluator,
        holdout_enabled=holdout_enabled,
    ).run()
