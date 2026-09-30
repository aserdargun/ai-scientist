"""Durable Director proposal boundary and restart-safe orchestration primitives."""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import math
import platform
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID, uuid5

import numpy as np
from sqlalchemy import Engine, text

from harness.baselines import champion_noise_sd as compute_champion_noise
from harness.baselines import normalize_task_score
from harness.contracts import Decision
from harness.referee import decide as referee_decide
from lab.director.artifacts import read_director_artifact, store_director_artifact
from lab.director.baselines import FrozenCalibrationDocument, calibration_sha256
from lab.director.budget import (
    MAX_EPISODE_CONTEXT_TOKENS,
    MAX_EPISODE_OUTPUT_TOKENS,
    MAX_EPISODE_WALL_SECONDS,
    MAX_SEED_WALL_SECONDS,
    EpisodeReservation,
    RunBudget,
)
from lab.director.contracts import (
    CandidateProposal,
    ExperimentDecision,
    ExperimentDocument,
    PerTaskResult,
    ReplayManifestDocument,
    TerminalReplayDisposition,
    TrajectoryDocument,
    replay_configuration_sha256,
)
from lab.director.executor import (
    CandidateExecutionRejected,
    evaluate_and_score_seed,
    verify_execution_identity,
)
from lab.director.fake_llm import (
    AgentContext,
    ProposalProvider,
    ProposalTurn,
    ProviderReceipt,
    prompt_context_sha256,
    prompt_messages_sha256,
)
from lab.director.journal import DirectorRunLease, canonical_bytes
from lab.director.ledger import (
    canonical_json_bytes,
    commit_experiment_record,
    register_experiment,
    transition_experiment,
)
from lab.director.parameter_grid import ParameterGridProvider
from lab.director.suite import SuiteTask
from lab.director.suite_guards import (
    SuitePositionBiasCheck,
    check_complete_suite_position_bias,
)
from lab.director.task_plan import (
    RunTaskAssignment,
    plan_run_tasks,
    record_planner_terminal_outcome,
)
from lab.llm.gpu_scheduler import boottime
from lab.sandbox.docker_runner import LocalDockerRunner
from lab.scorer.jobs import DEFAULT_ARTIFACT_ROOT, read_artifact_bytes
from lab.scorer.service import parse_candidate_score


class ExploreFamilyMismatch(ValueError):
    """Typed candidate abandonment before experiment/task/measurement admission."""

    reason_code = "explore_family_mismatch"
    response_text: str | None = None
    attempt_transcripts: tuple[Any, ...] = ()

    def __init__(self, turn: ProposalTurn):
        super().__init__(self.reason_code)
        self.provider_receipt = turn.provider_receipt


class ProposalCompletionUnverified(RuntimeError):
    """Stored proposal timing cannot authorize execution after interruption."""

    reason_code = "proposal_completion_unverified"


class ProposalCompletionUnsupported(ProposalCompletionUnverified):
    """Legacy proposal lacks the immutable completion proof needed for replay."""

    reason_code = "proposal_completion_unsupported"


@dataclass(frozen=True, slots=True)
class _ProposalCompletionClock:
    """Host-only episode identity; never supplied to a proposal provider."""

    run_id: UUID
    ordinal: int
    reservation: EpisodeReservation
    boot_id: str
    started_boottime: float
    current_boot_id: str

    def _binding(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": "director-proposal-completion.v1",
            "run_id": str(self.run_id),
            "ordinal": self.ordinal,
            "reservation_id": str(self.reservation.reservation_id),
            "wall_seconds": self.reservation.wall_seconds,
            "model_tokens": self.reservation.model_tokens,
            "boot_id": self.boot_id,
            "started_boottime": self.started_boottime,
            **{key: payload[key] for key in (
                "candidate_sha256", "inputs_sha256", "deterministic_provider_id",
                "provider_config_sha256", "provider_registry_entry_sha256",
            )},
        }

    def capture(self, payload: dict[str, Any]) -> dict[str, Any]:
        completed = boottime()
        return {
            **self._binding(payload),
            "completed_boottime": completed,
            "measured_wall_seconds": completed - self.started_boottime,
        }

    def validate(self, payload: dict[str, Any]) -> float:
        receipt = payload.get("completion_receipt")
        if "completion_receipt" not in payload:
            raise ProposalCompletionUnsupported("proposal_completion_missing: unsupported replay")
        if not isinstance(receipt, dict):
            raise ProposalCompletionUnverified("proposal_completion_receipt_malformed")
        binding = self._binding(payload)
        if set(receipt) != {*binding, "completed_boottime", "measured_wall_seconds"} or any(
            type(receipt[key]) is not type(value) or receipt[key] != value
            for key, value in binding.items()
        ):
            raise ProposalCompletionUnverified("proposal_completion_identity_mismatch")
        completed = receipt["completed_boottime"]
        elapsed = receipt["measured_wall_seconds"]
        if (
            any(isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value)
                for value in (self.started_boottime, completed, elapsed))
            or self.started_boottime < 0
            or completed < self.started_boottime
            or elapsed != completed - self.started_boottime
            or not 0 <= elapsed <= self.reservation.wall_seconds
            or (self.boot_id == self.current_boot_id and completed > boottime())
        ):
            raise ProposalCompletionUnverified("proposal_completion_timing_invalid")
        return float(elapsed)


_EXPERIMENT_NAMESPACE = UUID("0fac846e-d8f8-4c99-8998-d70967764392")
_MAX_MESSAGE_BYTES = 256 * 1024
POSITION_BIAS_THRESHOLD = 0.8
_CANDIDATE_TERMINAL_CODES = frozenset(
    {
        "invalid_input_frame",
        "unapproved_sensor_columns",
        "forbidden_input_column",
        "non_numeric_input",
        "non_finite_input",
        "input_too_large",
        "invalid_alarm_policy",
        "invalid_score_artifact",
        "score_length_or_index_mismatch",
        "non_finite_scores",
        "degenerate_constant_scores",
        "timeout",
        "candidate_crash",
        "harness_hash_mismatch",
        "invalid_fit_context",
        "fit_artifact_set_mismatch",
        "empty_fit_artifact",
        "score_artifact_set_mismatch",
        "guard_vector_mismatch",
        "non_finite_guard_vector",
        "invalid_causality_cut",
        "invalid_position_bias_vector",
        "invalid_source",
        "hardcoding_reject",
        "determinism_reject",
        "causality_reject",
        "position_bias_reject",
    }
)


@dataclass(frozen=True, slots=True)
class RegisteredProposal:
    """Proposal bytes and trusted identity committed before any candidate run."""

    experiment_id: str
    experiment_number: int
    proposal: CandidateProposal
    candidate_sha256: str
    candidate_blob_sha256: str
    inputs_sha256: str
    messages_blob_sha256: str
    calibration_sha256: str
    parent_experiment_id: str
    parent_tree_sha256: str
    harness_sha256: str
    image_sha256: str
    suite_id: str
    suite_version: int
    system: Literal["S1", "S2"]
    input_tokens: int
    output_tokens: int
    ledger_sequence: int = 0
    provider_receipt: ProviderReceipt | None = None
    deterministic_provider_id: str | None = None
    completion_wall_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class PrimaryEvaluationResult:
    """Measurements or a typed no-score terminal outcome for the requested seed."""

    experiment_id: str
    status: str
    measurements: tuple[dict[str, Any], ...]
    terminal_code: str | None
    wall_seconds: float = 0.0
    evaluation_kind: Literal["primary", "confirmation"] = "primary"
    seed: int = 0


@dataclass(frozen=True, slots=True)
class ProposalRefereeResult:
    """Trusted normalized comparison and typed output records for one proposal."""

    decision: ExperimentDecision
    per_task: tuple[PerTaskResult, ...]
    suite_score: float | None
    guards: dict[str, Literal["pass", "fail", "not_run"]]
    simpler: bool
    parent_scores: tuple[float, ...] | None
    child_scores: tuple[float, ...] | None
    weights: tuple[float, ...]


def _experiment_id(run_id: UUID, ordinal: int) -> str:
    return f"exp_{uuid5(_EXPERIMENT_NAMESPACE, f'{run_id}:{ordinal}').hex}"


def _candidate_git_tree(source: bytes) -> str:
    """Return the Git tree object ID for the fixed one-file candidate workspace."""
    blob_object = b"blob " + str(len(source)).encode("ascii") + b"\0" + source
    blob_oid = hashlib.sha1(blob_object).digest()  # nosec B324 -- Git object identifier
    entry = b"100644 pipeline.py\0" + blob_oid
    tree_object = b"tree " + str(len(entry)).encode("ascii") + b"\0" + entry
    return hashlib.sha1(tree_object).hexdigest()  # nosec B324 -- Git tree object identifier


def _stored_turn(
    *, run_id: UUID, ordinal: int, lease: DirectorRunLease, artifact_root: Any
) -> tuple[ProposalTurn, dict[str, Any]] | None:
    stored = lease.read_checkpoint(key=f"proposal:{ordinal}", artifact_root=artifact_root)
    if stored is None:
        return None
    payload = stored["payload"]
    if not isinstance(payload, dict) or payload.get("run_id") != str(run_id):
        raise ValueError("proposal checkpoint is bound to another run")
    if payload.get("experiment_number") != ordinal:
        raise ValueError("proposal checkpoint ordinal is invalid")
    source_digest = payload.get("candidate_blob_sha256")
    messages_digest = payload.get("messages_blob_sha256")
    context_digest = payload.get("context_blob_sha256")
    if not all(isinstance(item, str) for item in (source_digest, messages_digest, context_digest)):
        raise ValueError("proposal checkpoint has missing artifact receipts")
    source = read_director_artifact(cast(str, source_digest), artifact_root=artifact_root)
    messages = read_director_artifact(cast(str, messages_digest), artifact_root=artifact_root)
    context = read_director_artifact(cast(str, context_digest), artifact_root=artifact_root)
    if hashlib.sha256(source).hexdigest() != source_digest:
        raise ValueError("proposal source blob differs from its checkpoint")
    if hashlib.sha256(messages).hexdigest() != messages_digest:
        raise ValueError("proposal message blob differs from its checkpoint")
    if hashlib.sha256(context).hexdigest() != payload.get("inputs_sha256"):
        raise ValueError("proposal context blob differs from its checkpoint")
    messages_value = json.loads(messages)
    if not isinstance(messages_value, dict) or not isinstance(messages_value.get("messages"), list):
        raise ValueError("proposal transcript blob has an invalid envelope")
    turn_value = {
        "proposal": payload.get("proposal"),
        "messages": tuple(messages_value["messages"]),
        "input_tokens": payload.get("input_tokens"),
        "output_tokens": payload.get("output_tokens"),
        "provider_receipt": payload.get("provider_receipt"),
        "provider_attempts": messages_value.get("provider_attempts", []),
    }
    turn = ProposalTurn.model_validate_json(
        json.dumps(turn_value, ensure_ascii=False, separators=(",", ":")), strict=True
    )
    if turn.proposal.candidate_source.encode("utf-8") != source:
        raise ValueError("proposal candidate source differs from its immutable blob")
    if context != payload.get("context_canonical_json", "").encode("utf-8"):
        raise ValueError("proposal context bytes differ from the durable checkpoint")
    return turn, payload


def register_proposal_before_execution(
    director_engine: Engine,
    *,
    run_id: UUID,
    ordinal: int,
    parent_experiment_id: str,
    parent_tree_sha256: str,
    suite_id: str,
    suite_version: int,
    calibration_sha256: str,
    harness_sha256: str,
    image_sha256: str,
    system: str,
    context: AgentContext,
    provider: ProposalProvider,
    lease: DirectorRunLease,
    artifact_root: Path,
    remaining_wall_seconds: float | None = None,
    remaining_model_tokens: int | None = None,
    completion_clock: _ProposalCompletionClock | None = None,
) -> RegisteredProposal:
    """Durably checkpoint and register a proposal before candidate execution.

    The checkpoint is the recovery boundary: a fresh Director reads it instead of
    asking the provider for the same ordinal a second time. Only proposal source,
    hypothesis, predicted delta and bounded transcript are accepted from provider.
    """
    lease.require_run_active()
    if context.experiment_number != ordinal or not 1 <= ordinal <= 35:
        raise ValueError("proposal context ordinal differs from the durable run ordinal")
    if len(calibration_sha256) != 64 or len(harness_sha256) != 64 or len(image_sha256) != 64:
        raise ValueError("suite execution digests must be SHA-256 values")
    for digest in (calibration_sha256, harness_sha256, image_sha256, parent_tree_sha256):
        if any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("suite and parent tree digests must use lowercase hexadecimal")
    if not 40 <= len(parent_tree_sha256) <= 64:
        raise ValueError("parent tree identity must be a full Git tree SHA")
    if system not in {"S1", "S2"}:
        raise ValueError("proposal system must be S1 or S2")
    if getattr(provider, "provider_id", None) == "local-qwen.v1" and context.system != system:
        raise ValueError("local provider context system differs from the routed model")
    existing = _stored_turn(
        run_id=run_id, ordinal=ordinal, lease=lease, artifact_root=artifact_root
    )
    if existing is None:
        if remaining_wall_seconds is not None and (
            not math.isfinite(remaining_wall_seconds) or remaining_wall_seconds <= 0
        ):
            raise RuntimeError("proposal episode has no remaining provider time")
        # API stop commits on the run row independently of the Director owner
        # advisory lock. Do not enter the external provider after that fence.
        lease.require_run_active()
        bounded_propose = getattr(provider, "propose_bounded", None)
        if callable(bounded_propose):
            if remaining_wall_seconds is None:
                raise ValueError("bounded provider call has no durable episode time budget")

            def load_provider_attempt(index: int) -> dict[str, object] | None:
                for phase in ("completed", "started"):
                    stored_attempt = lease.read_checkpoint(
                        key=f"provider-attempt:{ordinal}:{index}:{phase}",
                        artifact_root=artifact_root,
                    )
                    if stored_attempt is None:
                        continue
                    attempt_payload = stored_attempt.get("payload")
                    if not isinstance(attempt_payload, dict):
                        raise ValueError("provider attempt checkpoint payload is malformed")
                    if (
                        attempt_payload.get("run_id") != str(run_id)
                        or attempt_payload.get("ordinal") != ordinal
                    ):
                        raise ValueError("provider attempt checkpoint belongs to another episode")
                    response_digest = attempt_payload.get("response_blob_sha256")
                    if response_digest is not None:
                        if not isinstance(response_digest, str):
                            raise ValueError("provider response blob receipt is malformed")
                        response_bytes = read_director_artifact(
                            response_digest, artifact_root=artifact_root
                        )
                        if hashlib.sha256(response_bytes).hexdigest() != response_digest:
                            raise ValueError("provider response blob failed hash verification")
                        attempt_payload = dict(attempt_payload)
                        attempt_payload["response_text"] = response_bytes.decode("utf-8")
                    return attempt_payload
                return None

            def save_provider_attempt(
                index: int, phase: str, attempt_payload: dict[str, object]
            ) -> None:
                if phase not in {"started", "completed"}:
                    raise ValueError("provider attempt phase is invalid")
                lease.require_run_active()
                durable = dict(attempt_payload)
                response = durable.pop("response_text", None)
                if response is not None:
                    if not isinstance(response, str) or len(response.encode("utf-8")) > 256 * 1024:
                        raise ValueError("provider final response exceeds its durable blob bound")
                    response_bytes = response.encode("utf-8")
                    durable["response_blob_sha256"] = store_director_artifact(
                        response_bytes, artifact_root=artifact_root
                    )
                durable.update(
                    {
                        "run_id": str(run_id),
                        "ordinal": ordinal,
                        "attempt_index": index,
                    }
                )
                _append_next_checkpoint(
                    lease,
                    director_engine,
                    run_id=run_id,
                    key=f"provider-attempt:{ordinal}:{index}:{phase}",
                    phase="provider_attempt",
                    payload=durable,
                    artifact_root=artifact_root,
                )

            turn = bounded_propose(
                context,
                remaining_wall_seconds=remaining_wall_seconds,
                remaining_model_tokens=(
                    int(remaining_model_tokens) if remaining_model_tokens is not None else None
                ),
                attempt_load=load_provider_attempt,
                attempt_save=save_provider_attempt,
            )
        else:
            turn = provider.propose(context)
        lease.require_run_active()
        # Revalidate provider output at the trust boundary, even for typed adapters.
        turn = ProposalTurn.model_validate_json(turn.model_dump_json(), strict=True)
        if turn.proposal.move_type != context.move_type and context.move_type is not None:
            raise ValueError("provider changed the preselected move intent")
        if (
            turn.provider_receipt is not None
            and turn.provider_receipt.context_sha256 != prompt_context_sha256(context)
        ):
            raise ValueError("provider receipt is bound to different metadata context")
        if turn.provider_receipt is not None:
            if len(turn.messages) < 3:
                raise ValueError("local provider transcript is missing its request messages")
            request_messages = [
                {"role": "system", "content": turn.messages[0]},
                {"role": "user", "content": turn.messages[1]},
            ]
            prompt_digest = prompt_messages_sha256(request_messages)
            if (
                prompt_digest != turn.provider_receipt.prompt_sha256
                or prompt_digest != turn.provider_receipt.attempts[-1].prompt_sha256
            ):
                raise ValueError("provider receipt prompt hash differs from its transcript")
        source = turn.proposal.candidate_source.encode("utf-8")
        messages_bytes = canonical_bytes(
            {
                "messages": list(turn.messages),
                "provider_attempts": [
                    item.model_dump(mode="json") for item in turn.provider_attempts
                ],
            }
        )
        context_bytes = canonical_bytes(context.model_dump(mode="json"))
        if len(source) > 1_048_576 or len(messages_bytes) > _MAX_MESSAGE_BYTES:
            raise ValueError("proposal source or transcript exceeds its bounded size")
        candidate_digest = hashlib.sha256(source).hexdigest()
        if prompt_context_sha256(context) != hashlib.sha256(context_bytes).hexdigest():
            raise ValueError("canonical prompt context hash is inconsistent")
        candidate_blob = store_director_artifact(source, artifact_root=artifact_root)
        messages_blob = store_director_artifact(messages_bytes, artifact_root=artifact_root)
        context_blob = store_director_artifact(context_bytes, artifact_root=artifact_root)
        if (candidate_blob, messages_blob, context_blob) != (
            candidate_digest,
            hashlib.sha256(messages_bytes).hexdigest(),
            hashlib.sha256(context_bytes).hexdigest(),
        ):
            raise RuntimeError("proposal artifact store returned an unexpected digest")
        with director_engine.connect() as connection:
            prior_sequence = connection.execute(
                text("SELECT max(sequence) FROM lab.experiments WHERE run_id = :run_id"),
                {"run_id": run_id},
            ).scalar_one()
        sequence = 0 if prior_sequence is None else int(prior_sequence) + 1
        payload: dict[str, Any] = {
            "schema": "director-proposal-checkpoint.v1",
            "run_id": str(run_id),
            "experiment_number": ordinal,
            "experiment_id": _experiment_id(run_id, ordinal),
            "sequence": sequence,
            "proposal": turn.proposal.model_dump(mode="json"),
            "deterministic_provider_id": (
                "operating-mode-grid.v1" if isinstance(provider, ParameterGridProvider) else None
            ),
            "provider_receipt": (
                turn.provider_receipt.model_dump(mode="json")
                if turn.provider_receipt is not None
                else None
            ),
            "provider_attempts": [item.model_dump(mode="json") for item in turn.provider_attempts],
            "input_tokens": turn.input_tokens,
            "output_tokens": turn.output_tokens,
            "inputs_sha256": hashlib.sha256(context_bytes).hexdigest(),
            "context_canonical_json": context_bytes.decode("utf-8"),
            "candidate_sha256": candidate_digest,
            "candidate_blob_sha256": candidate_blob,
            "messages_blob_sha256": messages_blob,
            "context_blob_sha256": context_blob,
            "calibration_sha256": calibration_sha256,
            "harness_sha256": harness_sha256,
            "image_sha256": image_sha256,
            "suite_id": suite_id,
            "suite_version": suite_version,
            "parent_experiment_id": parent_experiment_id,
            "parent_tree_sha256": parent_tree_sha256,
            "system": system,
            "provider_config_sha256": (
                turn.provider_receipt.provider_config_sha256
                if turn.provider_receipt is not None
                else getattr(provider, "configuration_sha256", None)
            ),
            "provider_registry_entry_sha256": (
                turn.provider_receipt.provider_registry_entry_sha256
                if turn.provider_receipt is not None
                else getattr(provider, "registry_entry_sha256", None)
            ),
        }
        with director_engine.connect() as connection:
            previous = connection.execute(
                text(
                    "SELECT max((event_json->>'sequence')::integer) FROM lab.run_events "
                    "WHERE run_id = :run_id AND event_type = 'director.checkpoint'"
                ),
                {"run_id": run_id},
            ).scalar_one()
        sequence = 0 if previous is None else int(previous) + 1
        if completion_clock is not None:
            payload["completion_receipt"] = completion_clock.capture(payload)
        lease.append_checkpoint(
            sequence=sequence,
            key=f"proposal:{ordinal}",
            phase="proposal_registered",
            payload=payload,
            artifact_root=artifact_root,
        )
    else:
        turn, payload = existing
        candidate_digest = str(payload["candidate_sha256"])
        candidate_blob = str(payload["candidate_blob_sha256"])
        messages_blob = str(payload["messages_blob_sha256"])
        context_hash = str(payload["inputs_sha256"])
        if any(
            payload.get(key) != value
            for key, value in (
                ("calibration_sha256", calibration_sha256),
                ("harness_sha256", harness_sha256),
                ("image_sha256", image_sha256),
                ("suite_id", suite_id),
                ("suite_version", suite_version),
                ("parent_experiment_id", parent_experiment_id),
                ("parent_tree_sha256", parent_tree_sha256),
                ("system", system),
                (
                    "provider_config_sha256",
                    getattr(provider, "configuration_sha256", None),
                ),
                (
                    "provider_registry_entry_sha256",
                    getattr(provider, "registry_entry_sha256", None),
                ),
            )
        ):
            raise RuntimeError("proposal checkpoint execution identity changed on resume")
        if prompt_context_sha256(context) != context_hash:
            raise RuntimeError("proposal context changed on resume")

    deterministic_id = (
        "operating-mode-grid.v1" if isinstance(provider, ParameterGridProvider) else None
    )
    if payload.get("deterministic_provider_id") != deterministic_id:
        raise ValueError("proposal checkpoint provider identity changed")
    if deterministic_id is not None and (turn.input_tokens or turn.output_tokens):
        raise ValueError("deterministic grid provider must consume zero model tokens")
    local_provider = getattr(provider, "provider_id", None) == "local-qwen.v1"
    if local_provider and turn.provider_receipt is None:
        raise ValueError("local provider proposal is missing its host runtime receipt")
    if not local_provider and turn.provider_receipt is not None:
        raise ValueError("non-local provider cannot attach a local runtime receipt")
    if turn.provider_receipt is not None:
        receipt = turn.provider_receipt
        if (
            receipt.provider_config_sha256 != getattr(provider, "configuration_sha256", None)
            or receipt.provider_registry_entry_sha256
            != getattr(provider, "registry_entry_sha256", None)
            or receipt.context_sha256 != prompt_context_sha256(context)
            or len(turn.messages) < 3
        ):
            raise ValueError("local provider receipt differs from the active registered context")
        prompt_messages = [
            {"role": "system", "content": turn.messages[0]},
            {"role": "user", "content": turn.messages[1]},
        ]
        prompt_digest = prompt_messages_sha256(prompt_messages)
        if (
            prompt_digest != receipt.prompt_sha256
            or prompt_digest != receipt.attempts[-1].prompt_sha256
        ):
            raise ValueError("local provider receipt differs from its transcript")
        if not turn.provider_attempts:
            raise ValueError("local provider result is missing durable attempt transcripts")
        transcript = turn.provider_attempts[-1]
        if (
            transcript.prompt_sha256 != prompt_digest
            or transcript.response_sha256
            != hashlib.sha256(turn.messages[-1].encode("utf-8")).hexdigest()
            or transcript.response_schema_sha256 != receipt.attempts[-1].response_schema_sha256
            or transcript.response_sha256 != receipt.attempts[-1].response_sha256
        ):
            raise ValueError("local provider final attempt differs from its receipt")
        for item in turn.provider_attempts:
            matching = [
                attempt
                for attempt in receipt.attempts
                if attempt.prompt_sha256 == item.prompt_sha256
                and attempt.response_sha256 == item.response_sha256
            ]
            if len(matching) != 1 or matching[0].response_schema_sha256 != (
                item.response_schema_sha256
            ):
                raise ValueError("local provider attempt transcript lacks a matching host receipt")

    if context.explore_intent is not None:
        from lab.director.explore import require_target_family

        if (
            system != "S2"
            or context.system != "S2"
            or (turn.provider_receipt is not None and turn.provider_receipt.actual_system != "S2")
        ):
            raise ValueError("EXPLORE proposal must use S2")
        try:
            require_target_family(
                turn.proposal.candidate_source.encode("utf-8"), context.explore_intent
            )
        except ValueError as exc:
            raise ExploreFamilyMismatch(turn) from exc

    experiment_id = str(payload["experiment_id"])
    candidate_digest = str(payload["candidate_sha256"])
    candidate_blob = str(payload["candidate_blob_sha256"])
    context_hash = str(payload["inputs_sha256"])
    proposal_json = {
        **turn.proposal.model_dump(mode="json"),
        "calibration_sha256": calibration_sha256,
        "harness_sha256": harness_sha256,
        "image_sha256": image_sha256,
        "suite_id": suite_id,
        "suite_version": suite_version,
        "candidate_tree_sha256": _candidate_git_tree(
            turn.proposal.candidate_source.encode("utf-8")
        ),
        "inputs_sha256": context_hash,
        "provider_receipt_sha256": (
            hashlib.sha256(
                canonical_bytes(turn.provider_receipt.model_dump(mode="json"))
            ).hexdigest()
            if turn.provider_receipt is not None
            else None
        ),
        "provider_registry_entry_sha256": payload.get("provider_registry_entry_sha256"),
        "provider_config_sha256": payload.get("provider_config_sha256"),
        "deterministic_provider_id": deterministic_id,
    }
    sequence_value = payload.get("sequence")
    if (
        isinstance(sequence_value, bool)
        or not isinstance(sequence_value, int)
        or sequence_value < 0
    ):
        raise ValueError("proposal checkpoint has an invalid immutable ledger sequence")
    completion_wall_seconds = (
        completion_clock.validate(payload) if completion_clock is not None else None
    )
    register_experiment(
        director_engine,
        experiment_id=experiment_id,
        run_id=str(run_id),
        sequence=sequence_value,
        experiment_number=ordinal,
        kind="proposal",
        baseline_name=None,
        parent_experiment_id=parent_experiment_id,
        candidate_sha256=candidate_digest,
        candidate_blob_sha256=candidate_blob,
        inputs_sha256=context_hash,
        move_type=turn.proposal.move_type,
        system=cast(Literal["S1", "S2"], system),
        hypothesis=turn.proposal.hypothesis,
        predicted_delta=turn.proposal.predicted_delta,
        proposal=proposal_json,
    )
    return RegisteredProposal(
        experiment_id=experiment_id,
        experiment_number=ordinal,
        proposal=turn.proposal,
        candidate_sha256=candidate_digest,
        candidate_blob_sha256=candidate_blob,
        inputs_sha256=context_hash,
        messages_blob_sha256=str(payload["messages_blob_sha256"]),
        calibration_sha256=calibration_sha256,
        parent_experiment_id=parent_experiment_id,
        parent_tree_sha256=parent_tree_sha256,
        harness_sha256=harness_sha256,
        image_sha256=image_sha256,
        suite_id=suite_id,
        suite_version=suite_version,
        system=cast(Literal["S1", "S2"], system),
        input_tokens=turn.input_tokens,
        output_tokens=turn.output_tokens,
        ledger_sequence=sequence_value,
        provider_receipt=turn.provider_receipt,
        deterministic_provider_id=deterministic_id,
        completion_wall_seconds=completion_wall_seconds,
    )


def execute_candidate_seed(
    director_engine: Engine,
    planner_engine: Engine,
    *,
    lease: DirectorRunLease,
    runner: LocalDockerRunner,
    run_id: UUID,
    proposal: RegisteredProposal,
    tasks: tuple[SuiteTask, ...],
    harness_sha256: str,
    image_sha256: str,
    budget: RunBudget,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
    evaluation_kind: Literal["primary", "confirmation"] = "primary",
    seed: int = 0,
    seed_wall_seconds: int = MAX_SEED_WALL_SECONDS,
    infrastructure_retries: int = 1,
) -> PrimaryEvaluationResult:
    """Measure one whole suite seed through fresh Docker and Scorer paths."""
    if not tasks or len(tasks) > 256:
        raise ValueError("suite seed must contain 1..256 trusted tasks")
    if (evaluation_kind == "primary" and seed != 0) or (
        evaluation_kind == "confirmation" and seed not in {1, 2}
    ):
        raise ValueError("proposal seed must be primary 0 or confirmation 1/2")
    if not 0 <= infrastructure_retries <= 1:
        raise ValueError("only one bounded infrastructure retry is allowed")
    if (
        isinstance(seed_wall_seconds, bool)
        or not isinstance(seed_wall_seconds, int)
        or not 1 <= seed_wall_seconds <= MAX_SEED_WALL_SECONDS
    ):
        raise ValueError("seed wall allowance must be between one and 600 seconds")
    lease.require_run_active()
    try:
        verify_execution_identity(runner, harness_sha256=harness_sha256, image_sha256=image_sha256)
    except CandidateExecutionRejected as exc:
        return PrimaryEvaluationResult(
            proposal.experiment_id,
            "rejected",
            (),
            exc.code,
            0.0,
            evaluation_kind,
            seed,
        )
    completed = _read_completed_seed(
        director_engine,
        planner_engine,
        lease=lease,
        run_id=run_id,
        proposal=proposal,
        tasks=tasks,
        evaluation_kind=evaluation_kind,
        seed=seed,
        harness_sha256=harness_sha256,
        budget=budget,
        artifact_root=artifact_root,
    )
    if completed is not None:
        try:
            verify_execution_identity(
                runner, harness_sha256=harness_sha256, image_sha256=image_sha256
            )
        except CandidateExecutionRejected as exc:
            return PrimaryEvaluationResult(
                proposal.experiment_id,
                "rejected",
                (),
                exc.code,
                completed.wall_seconds,
                evaluation_kind,
                seed,
            )
        return completed
    lease.require_run_active()
    assignments = tuple(
        RunTaskAssignment(
            experiment_id=proposal.experiment_id,
            evaluation_kind=evaluation_kind,
            task_id=task.task_id,
            seed=seed,
            candidate_sha256=proposal.candidate_sha256,
            dataset_id=task.dataset_id,
            split_id=task.split_id,
            session_id=task.session_id,
        )
        for task in tasks
    )
    plan_run_tasks(planner_engine, run_id=run_id, assignments=assignments)
    if evaluation_kind == "primary":
        lease.require_run_active()
        _advance_experiment_if_needed(
            director_engine, experiment_id=proposal.experiment_id, status="primary_running"
        )
    measurements: list[dict[str, Any]] = []
    reservation_key = f"seed-reservation:{proposal.experiment_id}:{evaluation_kind}:{seed}"
    reservation_event = lease.read_checkpoint(key=reservation_key, artifact_root=artifact_root)
    if reservation_event is None:
        reserve_seconds = min(seed_wall_seconds, int(budget.remaining_wall_seconds))
        if reserve_seconds < 1:
            raise RuntimeError("run_wall_budget_exhausted")
        reservation = budget.reserve_work(wall_seconds=reserve_seconds, model_tokens=0)
        started_at = datetime.now(UTC)
        reservation_payload = {
            "experiment_id": proposal.experiment_id,
            "evaluation_kind": evaluation_kind,
            "seed": seed,
            "seed_wall_seconds": seed_wall_seconds,
            "reservation_id": str(reservation.reservation_id),
            "wall_seconds": reservation.wall_seconds,
            "model_tokens": reservation.model_tokens,
            "started_at": started_at.isoformat(),
        }
        _append_next_checkpoint(
            lease,
            director_engine,
            run_id=run_id,
            key=reservation_key,
            phase="seed_reserved",
            payload=reservation_payload,
            artifact_root=artifact_root,
        )
    else:
        reservation_payload = reservation_event["payload"]
        try:
            reservation = EpisodeReservation(
                reservation_id=UUID(str(reservation_payload["reservation_id"])),
                wall_seconds=int(reservation_payload["wall_seconds"]),
                model_tokens=int(reservation_payload["model_tokens"]),
            )
            started_at = datetime.fromisoformat(str(reservation_payload["started_at"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("durable seed reservation is malformed") from exc
        if (
            started_at.tzinfo is None
            or reservation_payload.get("experiment_id") != proposal.experiment_id
        ):
            raise ValueError("durable seed reservation has invalid run identity or clock")
        if (
            reservation_payload.get("seed_wall_seconds", reservation.wall_seconds)
            != seed_wall_seconds
        ):
            raise RuntimeError("resumed seed limit differs from its immutable reservation")
        budget.adopt_reservation(reservation)
    from lab.director.evaluation_recovery import recover_abandoned_experiment

    if recover_abandoned_experiment(
        planner_engine,
        run_id=run_id,
        experiment_id=proposal.experiment_id,
        evidence_engine=director_engine,
        lease=lease,
        artifact_root=artifact_root,
    ):
        # Consume the existing reservation in full; downtime never creates retry credit.
        budget.reconcile_proposal(
            reservation,
            measured_wall_seconds=reservation.wall_seconds,
            measured_model_tokens=reservation.model_tokens,
        )
        _append_next_checkpoint(
            lease,
            director_engine,
            run_id=run_id,
            key=f"seed-complete:{proposal.experiment_id}:{evaluation_kind}:{seed}",
            phase="seed_complete",
            payload={
                "experiment_id": proposal.experiment_id,
                "evaluation_kind": evaluation_kind,
                "seed": seed,
                "elapsed_wall_seconds": float(reservation.wall_seconds),
                "reservation_id": str(reservation.reservation_id),
                "terminal_code": "infrastructure_abandoned",
                "budget": _budget_json(budget),
            },
            artifact_root=artifact_root,
        )
        return PrimaryEvaluationResult(
            proposal.experiment_id,
            "abandoned",
            (),
            "infrastructure_abandoned",
            float(reservation.wall_seconds),
            evaluation_kind,
            seed,
        )
    overall_deadline = time.monotonic() + max(
        0, reservation.wall_seconds - (datetime.now(UTC) - started_at).total_seconds()
    )
    candidate_rejection: CandidateExecutionRejected | None = None
    for index, task in enumerate(tasks):
        lease.require_run_active()
        cached = _read_measurement(
            director_engine,
            lease=lease,
            run_id=run_id,
            experiment_id=proposal.experiment_id,
            evaluation_kind=evaluation_kind,
            seed=seed,
            task=task,
            candidate_sha256=proposal.candidate_sha256,
            harness_sha256=harness_sha256,
            experiment_number=proposal.experiment_number,
            artifact_root=artifact_root,
        )
        if cached is not None:
            measurements.append(cached)
            continue
        if _task_has_terminal_outcome(
            planner_engine,
            run_id=run_id,
            experiment_id=proposal.experiment_id,
            task=task,
            evaluation_kind=evaluation_kind,
            seed=seed,
        ):
            raise RuntimeError("primary task already has a durable terminal outcome")
        attempts = infrastructure_retries + 1
        last_error: Exception | None = None
        measured_result: dict[str, Any] | None = None
        for attempt in range(attempts):
            remaining = min(
                seed_wall_seconds,
                120 if attempt else seed_wall_seconds,
                int(overall_deadline - time.monotonic()),
                int(budget.remaining_wall_seconds + reservation.wall_seconds),
            )
            if remaining < 1:
                raise RuntimeError("whole_suite_seed_budget_exhausted")
            try:
                measured_result = evaluate_and_score_seed(
                    director_engine,
                    planner_engine,
                    runner,
                    run_id=run_id,
                    experiment_id=proposal.experiment_id,
                    evaluation_kind=evaluation_kind,
                    task=task,
                    candidate_source=proposal.proposal.candidate_source.encode("utf-8"),
                    candidate_sha256=proposal.candidate_sha256,
                    seed=seed,
                    harness_sha256=harness_sha256,
                    image_sha256=image_sha256,
                    remaining_seconds=remaining,
                    lease=lease,
                    artifact_root=artifact_root,
                )
                last_error = None
                break
            except CandidateExecutionRejected as exc:
                candidate_rejection = exc
                outcome: Literal["candidate_timeout", "candidate_crash", "guard_rejected"] = (
                    "candidate_timeout"
                    if exc.code == "timeout"
                    else ("candidate_crash" if exc.code == "candidate_crash" else "guard_rejected")
                )
                for pending_index, pending in enumerate(tasks[index:]):
                    lease.require_run_active()
                    record_planner_terminal_outcome(
                        planner_engine,
                        run_id=run_id,
                        experiment_id=proposal.experiment_id,
                        evaluation_kind=evaluation_kind,
                        task_id=pending.task_id,
                        seed=seed,
                        candidate_sha256=proposal.candidate_sha256,
                        outcome_code=outcome if pending_index == 0 else "guard_rejected",
                    )
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                if attempt + 1 == attempts:
                    break
                from lab.director.evaluation_recovery import reserve_infrastructure_retry

                if not reserve_infrastructure_retry(
                    director_engine,
                    lease,
                    experiment_id=proposal.experiment_id,
                    artifact_root=artifact_root,
                    remaining_seconds=min(120, int(overall_deadline - time.monotonic())),
                ):
                    break
                lease.require_run_active()
        if last_error is not None:
            elapsed = min(
                reservation.wall_seconds,
                max(0.0, (datetime.now(UTC) - started_at).total_seconds()),
            )
            budget.reconcile_proposal(
                reservation,
                measured_wall_seconds=elapsed,
                measured_model_tokens=0,
            )
            raise last_error
        if measured_result is not None:
            measurements.append(measured_result)
            _append_next_checkpoint(
                lease,
                director_engine,
                run_id=run_id,
                key=(
                    f"measurement:{proposal.experiment_number}:{task.task_id}:"
                    f"{evaluation_kind}:{seed}"
                ),
                phase=f"{evaluation_kind}_measured",
                payload={
                    "experiment_id": proposal.experiment_id,
                    "task_id": task.task_id,
                    "evaluation_kind": evaluation_kind,
                    "seed": seed,
                    "measurement": measured_result,
                },
                artifact_root=artifact_root,
            )
        if candidate_rejection is not None:
            break
    elapsed = max(0.0, (datetime.now(UTC) - started_at).total_seconds())
    if elapsed > reservation.wall_seconds:
        raise RuntimeError("whole_suite_seed_exceeded_reserved_wall_budget")
    budget.reconcile_proposal(reservation, measured_wall_seconds=elapsed, measured_model_tokens=0)
    _append_next_checkpoint(
        lease,
        director_engine,
        run_id=run_id,
        key=f"seed-complete:{proposal.experiment_id}:{evaluation_kind}:{seed}",
        phase="seed_complete",
        payload={
            "experiment_id": proposal.experiment_id,
            "evaluation_kind": evaluation_kind,
            "seed": seed,
            "reservation_id": str(reservation.reservation_id),
            "elapsed_wall_seconds": elapsed,
            "terminal_code": candidate_rejection.code if candidate_rejection else None,
            "budget": _budget_json(budget),
        },
        artifact_root=artifact_root,
    )
    if candidate_rejection is not None:
        status = "crashed" if candidate_rejection.code == "candidate_crash" else "rejected"
        return PrimaryEvaluationResult(
            proposal.experiment_id,
            status,
            tuple(measurements),
            candidate_rejection.code,
            elapsed,
            evaluation_kind,
            seed,
        )
    return PrimaryEvaluationResult(
        proposal.experiment_id,
        "primary_measured",
        tuple(measurements),
        None,
        elapsed,
        evaluation_kind,
        seed,
    )


def _read_completed_seed(
    director_engine: Engine,
    planner_engine: Engine,
    *,
    lease: DirectorRunLease,
    run_id: UUID,
    proposal: RegisteredProposal,
    tasks: tuple[SuiteTask, ...],
    evaluation_kind: Literal["primary", "confirmation"],
    seed: int,
    harness_sha256: str,
    budget: RunBudget,
    artifact_root: Path,
) -> PrimaryEvaluationResult | None:
    """Rebuild a completed seed from immutable checkpoint receipts without rerunning it."""
    key = f"seed-complete:{proposal.experiment_id}:{evaluation_kind}:{seed}"
    completed = lease.read_checkpoint(key=key, artifact_root=artifact_root)
    if completed is None:
        return None
    payload = completed["payload"]
    if not isinstance(payload, dict) or any(
        payload.get(name) != expected
        for name, expected in (
            ("experiment_id", proposal.experiment_id),
            ("evaluation_kind", evaluation_kind),
            ("seed", seed),
        )
    ):
        raise RuntimeError("completed seed receipt differs from the requested execution")
    elapsed_value = payload.get("elapsed_wall_seconds")
    terminal_code = payload.get("terminal_code")
    reservation_digest = payload.get("reservation_id")
    durable_budget = payload.get("budget")
    if (
        isinstance(elapsed_value, bool)
        or not isinstance(elapsed_value, (int, float))
        or not math.isfinite(elapsed_value)
        or elapsed_value < 0
        or elapsed_value > MAX_SEED_WALL_SECONDS
        or (terminal_code is not None and not isinstance(terminal_code, str))
        or not isinstance(reservation_digest, str)
        or not isinstance(durable_budget, dict)
    ):
        raise RuntimeError("completed seed receipt has malformed timing, outcome, or budget")
    reservation_event = lease.read_checkpoint(
        key=f"seed-reservation:{proposal.experiment_id}:{evaluation_kind}:{seed}",
        artifact_root=artifact_root,
    )
    if reservation_event is None:
        raise RuntimeError("completed seed has no durable pre-dispatch reservation")
    reservation_payload = reservation_event["payload"]
    if (
        not isinstance(reservation_payload, dict)
        or reservation_payload.get("experiment_id") != proposal.experiment_id
        or reservation_payload.get("evaluation_kind") != evaluation_kind
        or reservation_payload.get("seed") != seed
        or reservation_payload.get("reservation_id") != reservation_digest
    ):
        raise RuntimeError("completed seed reservation identity does not match its receipt")
    reservation = EpisodeReservation(
        reservation_id=UUID(reservation_digest),
        wall_seconds=reservation_payload["wall_seconds"],
        model_tokens=reservation_payload["model_tokens"],
    )
    snapshot = budget.snapshot()
    active = {item.reservation_id: item for item in snapshot.reservations}
    if reservation.reservation_id in active:
        budget.reconcile_proposal(
            active[reservation.reservation_id],
            measured_wall_seconds=float(elapsed_value),
            measured_model_tokens=0,
        )
    if not _budget_totals_at_least(_budget_totals(budget), _payload_budget_totals(durable_budget)):
        raise RuntimeError("resumed budget predates the completed seed checkpoint boundary")

    measurements: list[dict[str, Any]] = []
    measurement_ids: set[str] = set()
    for task in tasks:
        row = _read_measurement(
            director_engine,
            lease=lease,
            run_id=run_id,
            experiment_id=proposal.experiment_id,
            evaluation_kind=evaluation_kind,
            seed=seed,
            task=task,
            candidate_sha256=proposal.candidate_sha256,
            harness_sha256=harness_sha256,
            experiment_number=proposal.experiment_number,
            artifact_root=artifact_root,
        )
        if row is not None:
            measurements.append(row)
            measurement_ids.add(task.task_id)
        elif terminal_code is None or not _task_has_terminal_outcome(
            planner_engine,
            run_id=run_id,
            experiment_id=proposal.experiment_id,
            task=task,
            evaluation_kind=evaluation_kind,
            seed=seed,
        ):
            raise RuntimeError("completed seed has an unmeasured task without a terminal receipt")
    if terminal_code is None and measurement_ids != {task.task_id for task in tasks}:
        raise RuntimeError("completed successful seed does not contain the complete suite")
    if terminal_code is not None:
        if terminal_code not in _CANDIDATE_TERMINAL_CODES | {"infrastructure_abandoned"}:
            raise RuntimeError("completed candidate terminal code is not a known guard outcome")
        status = (
            "abandoned"
            if terminal_code == "infrastructure_abandoned"
            else "crashed"
            if terminal_code == "candidate_crash"
            else "rejected"
        )
    else:
        status = "primary_measured"
    return PrimaryEvaluationResult(
        proposal.experiment_id,
        status,
        tuple(measurements),
        terminal_code,
        float(elapsed_value),
        evaluation_kind,
        seed,
    )


def _budget_totals(budget: RunBudget) -> tuple[int, float, int, float, int]:
    snapshot = budget.snapshot()
    return (
        snapshot.proposal_count,
        snapshot.wall_seconds,
        snapshot.model_tokens,
        snapshot.reserved_wall_seconds,
        snapshot.reserved_model_tokens,
    )


def _payload_budget_totals(payload: dict[str, Any]) -> tuple[int, float, int, float, int]:
    try:
        values = (
            payload["proposal_count"],
            payload["wall_seconds"],
            payload["model_tokens"],
            payload["reserved_wall_seconds"],
            payload["reserved_model_tokens"],
        )
    except KeyError as exc:
        raise RuntimeError("durable seed budget receipt is incomplete") from exc
    if (
        isinstance(values[0], bool)
        or not isinstance(values[0], int)
        or isinstance(values[2], bool)
        or not isinstance(values[2], int)
        or isinstance(values[4], bool)
        or not isinstance(values[4], int)
        or isinstance(values[1], bool)
        or not isinstance(values[1], (float, int))
        or isinstance(values[3], bool)
        or not isinstance(values[3], (float, int))
        or not math.isfinite(values[1])
        or not math.isfinite(values[3])
    ):
        raise RuntimeError("durable seed budget counters are malformed")
    return (values[0], float(values[1]), values[2], float(values[3]), values[4])


def _budget_totals_at_least(
    current: tuple[int, float, int, float, int],
    checkpoint: tuple[int, float, int, float, int],
) -> bool:
    """Accept a later durable budget without rewinding to a historical seed."""
    return all(now >= then for now, then in zip(current, checkpoint, strict=True))


def execute_primary_seed(
    director_engine: Engine,
    planner_engine: Engine,
    *,
    lease: DirectorRunLease,
    runner: LocalDockerRunner,
    run_id: UUID,
    proposal: RegisteredProposal,
    tasks: tuple[SuiteTask, ...],
    harness_sha256: str,
    image_sha256: str,
    budget: RunBudget,
    infrastructure_retries: int = 1,
    seed_wall_seconds: int = MAX_SEED_WALL_SECONDS,
) -> PrimaryEvaluationResult:
    """Compatibility entry point for the proposal's primary seed zero."""
    return execute_candidate_seed(
        director_engine,
        planner_engine,
        lease=lease,
        runner=runner,
        run_id=run_id,
        proposal=proposal,
        tasks=tasks,
        harness_sha256=harness_sha256,
        image_sha256=image_sha256,
        budget=budget,
        evaluation_kind="primary",
        seed=0,
        infrastructure_retries=infrastructure_retries,
        seed_wall_seconds=seed_wall_seconds,
    )


def _append_next_checkpoint(
    lease: DirectorRunLease,
    engine: Engine,
    *,
    run_id: UUID,
    key: str,
    phase: str,
    payload: dict[str, Any],
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
) -> None:
    with engine.connect() as connection:
        previous = connection.execute(
            text(
                "SELECT max((event_json->>'sequence')::integer) FROM lab.run_events "
                "WHERE run_id=:run_id AND event_type='director.checkpoint'"
            ),
            {"run_id": run_id},
        ).scalar_one()
    lease.append_checkpoint(
        sequence=0 if previous is None else int(previous) + 1,
        key=key,
        phase=phase,
        payload=payload,
        artifact_root=artifact_root,
    )


def _budget_json(budget: RunBudget) -> dict[str, Any]:
    snapshot = budget.snapshot()
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


def _read_measurement(
    engine: Engine,
    *,
    lease: DirectorRunLease,
    run_id: UUID,
    experiment_id: str,
    evaluation_kind: Literal["primary", "confirmation"],
    seed: int,
    task: SuiteTask,
    candidate_sha256: str,
    harness_sha256: str,
    experiment_number: int,
    artifact_root: Path,
) -> dict[str, Any] | None:
    del engine, run_id  # Only a complete checkpoint, not a bare score row, is resumable.
    checkpoint = lease.read_checkpoint(
        key=f"measurement:{experiment_number}:{task.task_id}:{evaluation_kind}:{seed}",
        artifact_root=artifact_root,
    )
    if checkpoint is None:
        return None
    payload = checkpoint["payload"]
    if not isinstance(payload, dict) or payload.get("experiment_id") != experiment_id:
        raise RuntimeError("resumed measurement checkpoint belongs to another experiment")
    row = payload.get("measurement")
    if not isinstance(row, dict):
        raise RuntimeError("resumed measurement checkpoint has no typed measurement")
    if any(
        row[key] != expected
        for key, expected in (
            ("candidate_sha256", candidate_sha256),
            ("dataset_id", task.dataset_id),
            ("split_id", task.split_id),
            ("session_id", task.session_id),
            ("profile_sha256", task.profile_sha256),
            ("harness_sha256", harness_sha256),
        )
    ):
        raise RuntimeError("resumed primary score differs from its preregistered identity")
    family = task.family
    recorded_family = row.get("task_family")
    if recorded_family is None:
        # Checkpoints written before family-aware scoring remain readable only for EVT.
        if family != "EVT" or "task_family" in row:
            raise RuntimeError("resumed measurement task family differs from its trusted task")
    elif recorded_family != family:
        raise RuntimeError("resumed measurement task family differs from its trusted task")

    family_metrics: list[float]
    try:
        if family == "EVT":
            family_metrics = [float(row["vus_pr"]), float(row["vus_roc"])]
        elif family in {"PDM", "NRM"}:
            family_metrics = [
                float(row["task_score"]),
                float(row["fa_per_day"]),
                float(row["duty_fraction"]),
            ]
            if family == "NRM":
                family_metrics.append(float(row["position_bias"]))
        else:
            raise RuntimeError("resumed measurement has an unsupported trusted task family")
        fit_seconds = float(row["fit_seconds"])
        score_seconds = float(row["score_seconds"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError("resumed measurement lacks complete family score provenance") from exc

    if (
        not all(math.isfinite(value) for value in (*family_metrics, fit_seconds, score_seconds))
        or fit_seconds < 0
        or score_seconds < 0
        or (family in {"PDM", "NRM"} and (family_metrics[1] < 0 or not 0 <= family_metrics[2] <= 1))
        or (family == "NRM" and not 0 <= family_metrics[3] <= 1)
        or not isinstance(row.get("guards"), dict)
        or not row.get("candidate_output_sha256")
    ):
        raise RuntimeError("resumed measurement lacks complete guard and timing provenance")
    return dict(row)


def _suite_position_bias_checks(
    tasks: tuple[SuiteTask, ...],
    measurements: Mapping[str, dict[str, Any]],
    *,
    seed_count: int,
) -> tuple[SuitePositionBiasCheck, ...]:
    """Verify each measured NRM output blob and aggregate complete suite guards."""
    families = {task.task_id: str(task.family) for task in tasks}
    checks = []
    for seed_index in range(seed_count):
        vectors: dict[str, tuple[float, ...]] = {}
        reported_bias: dict[str, float] = {}
        for task in tasks:
            if str(task.family) != "NRM":
                continue
            row = measurements.get(task.task_id)
            if row is None:
                continue
            seed_artifacts = row.get("seed_artifacts_sha256")
            if seed_artifacts is None:
                digests = (row.get("candidate_output_sha256"),)
            elif isinstance(seed_artifacts, list) and all(
                isinstance(item, str) for item in seed_artifacts
            ):
                digests = tuple(seed_artifacts)
            else:
                raise ValueError("NRM measurement has malformed seed artifact receipts")
            if seed_index >= len(digests):
                continue
            digest = digests[seed_index]
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
            ):
                raise ValueError("NRM score output lacks a valid immutable Scorer digest")
            payload = read_artifact_bytes(digest)
            if hashlib.sha256(payload).hexdigest() != digest:
                raise ValueError("NRM score output artifact failed its digest")
            artifact = parse_candidate_score(payload)
            if artifact.sample_indices != list(range(len(artifact.scores))):
                raise ValueError("NRM score output does not preserve the ordered evaluation grid")
            vectors[task.task_id] = tuple(float(value) for value in artifact.scores)

            reported_values = row.get("seed_position_bias")
            if reported_values is None:
                reported_values = (row.get("position_bias"),)
            if not isinstance(reported_values, (tuple, list)) or seed_index >= len(reported_values):
                raise ValueError("NRM measurement omits its per-seed Scorer position-bias value")
            value = reported_values[seed_index]
            if isinstance(value, bool):
                raise ValueError("Scorer position-bias value must be numeric")
            try:
                reported = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("Scorer position-bias value is malformed") from exc
            if not math.isfinite(reported) or not 0.0 <= reported <= 1.0:
                raise ValueError("Scorer position-bias value is outside [0,1]")
            reported_bias[task.task_id] = reported

        check = check_complete_suite_position_bias(
            vectors, families, threshold=POSITION_BIAS_THRESHOLD
        )
        for task_id, measured_bias in check.task_bias.items():
            stored_bias = reported_bias.get(task_id)
            if stored_bias is None or not math.isclose(
                stored_bias, measured_bias, rel_tol=0.0, abs_tol=1e-12
            ):
                raise ValueError("Scorer position bias differs from its immutable score blob")
        checks.append(check)
    return tuple(checks)


def _task_has_terminal_outcome(
    planner_engine: Engine,
    *,
    run_id: UUID,
    experiment_id: str,
    task: SuiteTask,
    evaluation_kind: Literal["primary", "confirmation"],
    seed: int,
) -> bool:
    with planner_engine.connect() as connection:
        return bool(
            connection.execute(
                text(
                    "SELECT 1 FROM scorer.task_terminal_outcomes WHERE run_id=:run_id "
                    "AND experiment_id=:experiment_id AND evaluation_kind=:evaluation_kind "
                    "AND task_id=:task_id AND seed=:seed"
                ),
                {
                    "run_id": run_id,
                    "experiment_id": experiment_id,
                    "evaluation_kind": evaluation_kind,
                    "task_id": task.task_id,
                    "seed": seed,
                },
            ).first()
        )


def decide_proposal_from_measurements(
    *,
    proposal: RegisteredProposal,
    result: PrimaryEvaluationResult,
    tasks: tuple[SuiteTask, ...],
    calibration: FrozenCalibrationDocument,
    parent_source: bytes,
    champion_scores_by_task: Mapping[str, float] | None = None,
    champion_noise_sd: float | None = None,
    best_suite: float | None,
    epsilon: float = 0.01,
    bootstrap_seed: int = 0,
) -> ProposalRefereeResult:
    """Run Referee only on exact trusted task order and measured Scorer values.

    A missing score is a typed no-score rejection; the helper never pads an
    unmeasured task with zero. CandidateProposal fields such as predicted_delta
    and any LLM simplicity claim do not participate in the decision.
    """
    if proposal.calibration_sha256 != calibration_sha256(calibration):
        raise ValueError("proposal has no frozen calibration identity")
    if (
        calibration.suite_id != proposal.suite_id
        or calibration.suite_version != proposal.suite_version
        or calibration.harness_sha256 != proposal.harness_sha256
        or calibration.image_sha256 != proposal.image_sha256
    ):
        raise ValueError("proposal and frozen calibration execution identities differ")
    task_by_id = {task.task_id: task for task in tasks}
    calibration_by_id = {task.task_id: task for task in calibration.tasks}
    if set(task_by_id) != set(calibration_by_id) or len(task_by_id) != len(tasks):
        raise ValueError("proposal task set differs from its exact frozen calibration")
    if _candidate_git_tree(parent_source) != proposal.parent_tree_sha256:
        raise ValueError("parent source differs from the registered Git tree")
    ordered = tuple(
        sorted(
            calibration.tasks,
            key=lambda item: (item.dataset_id, item.split_id, item.session_id, item.task_id),
        )
    )
    measurement_by_id: dict[str, dict[str, Any]] = {}
    for measurement in result.measurements:
        task_id = measurement.get("task_id")
        if not isinstance(task_id, str) or task_id in measurement_by_id:
            raise ValueError("measured result contains duplicate or malformed task identity")
        measurement_by_id[task_id] = measurement
    if any(task_id not in task_by_id for task_id in measurement_by_id):
        raise ValueError("measured result contains a task outside the frozen suite")

    task_results: list[PerTaskResult] = []
    parent_values: list[float] = []
    child_values: list[float] = []
    weights: list[float] = []
    all_measured = result.status == "primary_measured" and set(measurement_by_id) == set(task_by_id)
    all_guards_pass = all_measured
    aggregate_guards: dict[str, Literal["pass", "fail", "not_run"]] = {
        "hardcoding": "not_run",
        "determinism": "not_run",
        "causality": "not_run",
        # Position bias is a suite-wide NRM guard. This slice is EVT-only.
        "position_bias": "pass",
    }
    for task_calibration in ordered:
        task = task_by_id[task_calibration.task_id]
        if (
            task.dataset_id != task_calibration.dataset_id
            or task.split_id != task_calibration.split_id
            or task.session_id != task_calibration.session_id
            or task.profile_sha256 != task_calibration.profile_sha256
            or task.family != task_calibration.family
        ):
            raise ValueError("suite task differs from its frozen profile identity")
        measured_row = measurement_by_id.get(task.task_id)
        if measured_row is None:
            all_guards_pass = False
            continue
        if any(
            measured_row.get(key) != expected
            for key, expected in (
                ("candidate_sha256", proposal.candidate_sha256),
                ("dataset_id", task.dataset_id),
                ("split_id", task.split_id),
                ("session_id", task.session_id),
                ("profile_sha256", task.profile_sha256),
            )
        ):
            raise ValueError("measured candidate identity differs from trusted task")
        task_family = str(measured_row.get("task_family", task.family))
        if task_family != str(task.family) or task_family != str(task_calibration.family):
            raise ValueError("Scorer task family differs from the trusted suite calibration")
        raw_metric_value = (
            measured_row.get("vus_pr") if task_family == "EVT" else measured_row.get("task_score")
        )
        if isinstance(raw_metric_value, bool) or not isinstance(raw_metric_value, (float, int)):
            raise ValueError("Scorer result omits its family-specific decision metric")
        raw_metric = float(raw_metric_value)
        raw_pr_value = measured_row.get("vus_pr") if task_family == "EVT" else None
        raw_roc_value = measured_row.get("vus_roc") if task_family == "EVT" else None
        raw_pr = float(raw_pr_value) if raw_pr_value is not None else None
        raw_roc = float(raw_roc_value) if raw_roc_value is not None else None
        fit_seconds = float(measured_row.get("fit_seconds", 0.0))
        score_seconds = float(measured_row.get("score_seconds", 0.0))
        finite_values = [raw_metric, fit_seconds, score_seconds]
        finite_values.extend(value for value in (raw_pr, raw_roc) if value is not None)
        if not all(math.isfinite(value) for value in finite_values):
            raise ValueError("candidate result contains a non-finite Scorer value")
        normalized = normalize_task_score(
            raw_metric,
            task_calibration.base_score,
            task_calibration.reference_score,
            family=task_calibration.family,
        )
        task_results.append(
            PerTaskResult(
                task_id=task.task_id,
                task_family=cast(Literal["EVT", "PDM", "NRM"], task_family),
                score_norm=float(normalized),
                vus_pr=raw_pr,
                vus_roc=raw_roc,
                task_score=raw_metric if task_family != "EVT" else None,
                fa_per_day=(float(measured_row["fa_per_day"]) if task_family == "PDM" else None),
                duty_fraction=(
                    float(measured_row["duty_fraction"]) if task_family == "NRM" else None
                ),
                event_f1=None,
                fit_seconds=fit_seconds,
                score_seconds=score_seconds,
            )
        )
        baseline_seeds = next(
            row for row in task_calibration.seed_scores if row.name == "robust_z"
        ).scores
        parent_raw = (
            champion_scores_by_task[task.task_id]
            if champion_scores_by_task is not None
            else (baseline_seeds[0] + baseline_seeds[1]) / 2.0
        )
        parent_values.append(
            float(
                normalize_task_score(
                    float(parent_raw),
                    task_calibration.base_score,
                    task_calibration.reference_score,
                    family=task_calibration.family,
                )
            )
        )
        child_values.append(float(normalized))
        weights.append(float(task_calibration.task_weight))
        task_guards = measured_row.get("guards", {})
        if not isinstance(task_guards, dict):
            raise ValueError("guard results have an invalid typed envelope")
        for key in ("hardcoding", "determinism", "causality"):
            code = task_guards.get(key)
            if code is None:
                all_guards_pass = False
                continue
            passed = code.endswith("_pass") or code == "position_bias_not_applicable"
            if not passed:
                all_guards_pass = False
                aggregate_guards[key] = "fail"
            elif aggregate_guards[key] != "fail":
                aggregate_guards[key] = "pass"

    if len(task_results) != len(measurement_by_id):
        raise ValueError("not every measured Scorer row was represented in the proposal record")
    seed_artifacts = next(iter(measurement_by_id.values()), {}).get("seed_artifacts_sha256")
    seed_count = len(seed_artifacts) if isinstance(seed_artifacts, list) else 1
    if seed_count not in {1, 2}:
        raise ValueError("decision NRM guard has an unsupported seed artifact count")
    position_checks = _suite_position_bias_checks(tasks, measurement_by_id, seed_count=seed_count)
    position_passed = bool(position_checks) and all(check.passed for check in position_checks)
    aggregate_guards["position_bias"] = "pass" if position_passed else "fail"
    if not position_passed:
        all_guards_pass = False
    noise = champion_noise_sd
    if noise is None:
        noise = calibration.champion_noise_sd
    if noise is None or not math.isfinite(noise) or noise < 0:
        raise ValueError("current champion noise must be a measured finite suite value")
    simpler = _source_is_measurably_simpler(
        proposal.proposal.candidate_source.encode("utf-8"), parent_source
    )
    suite_score: float | None = None
    if all_measured and len(child_values) == len(ordered):
        if best_suite is None or not math.isfinite(best_suite):
            raise ValueError("Referee requires the fixed measured champion best_suite")
        denominator = sum(weights)
        suite_score = (
            sum(value * weight for value, weight in zip(child_values, weights, strict=True))
            / denominator
        )
        decision = referee_decide(
            parent_values,
            child_values,
            weights,
            eps=epsilon,
            noise_sd=noise,
            simpler=simpler,
            guards_ok=all_guards_pass,
            best_suite=best_suite,
            seed=0,
        )
    else:
        decision = Decision("REJECT", None, None, result.terminal_code or "incomplete_measurements")
    typed_decision = ExperimentDecision(
        verdict=cast(Any, decision.verdict),
        delta=decision.delta,
        ci_low=decision.ci_low,
        noise_sd=float(noise),
        reason=str(decision.reason)[:128] or "referee_reject",
    )
    return ProposalRefereeResult(
        decision=typed_decision,
        per_task=tuple(task_results),
        suite_score=suite_score,
        guards=aggregate_guards,
        simpler=simpler,
        parent_scores=tuple(parent_values) if len(parent_values) == len(ordered) else None,
        child_scores=tuple(child_values) if len(child_values) == len(ordered) else None,
        weights=tuple(weights),
    )


def _source_is_measurably_simpler(candidate: bytes, parent: bytes) -> bool:
    """Require a 5% occupied-AST-line reduction and no new imports."""
    try:
        child_tree = ast.parse(candidate.decode("utf-8"))
        parent_tree = ast.parse(parent.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError):
        return False

    def occupied_lines(tree: ast.AST) -> set[int]:
        return {
            line
            for node in ast.walk(tree)
            if isinstance(node, ast.stmt)
            for line in range(node.lineno, (node.end_lineno or node.lineno) + 1)
        }

    def imports(tree: ast.AST) -> set[str]:
        values: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                values.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                values.add(node.module or "")
        return values

    child_lines = occupied_lines(child_tree)
    parent_lines = occupied_lines(parent_tree)
    if not parent_lines:
        return False
    return len(child_lines) <= math.floor(len(parent_lines) * 0.95) and imports(
        child_tree
    ) <= imports(parent_tree)


def _merge_confirmation_measurements(
    primary: PrimaryEvaluationResult, confirmation: PrimaryEvaluationResult
) -> PrimaryEvaluationResult:
    """Use the arithmetic mean of actual seeds zero and one for Referee input."""
    left = {item["task_id"]: item for item in primary.measurements}
    right = {item["task_id"]: item for item in confirmation.measurements}
    if not left or set(left) != set(right) or len(left) != len(primary.measurements):
        raise ValueError("primary and confirmation measurements do not cover the same tasks")
    merged: list[dict[str, Any]] = []
    for task_id in sorted(left):
        first, second = left[task_id], right[task_id]
        for key in ("candidate_sha256", "dataset_id", "split_id", "session_id", "profile_sha256"):
            if first.get(key) != second.get(key):
                raise ValueError("primary and confirmation task identities differ")
        if not isinstance(first.get("guards"), dict) or not isinstance(second.get("guards"), dict):
            raise ValueError("both decision seeds require trusted guard records")
        merged_guards = {
            key: first["guards"].get(key)
            if first["guards"].get(key) == second["guards"].get(key)
            else "guard_seed_disagreement"
            for key in ("hardcoding", "determinism", "causality")
        }
        merged_metrics: dict[str, float | None] = {}
        for key in ("vus_pr", "vus_roc", "task_score", "fa_per_day", "duty_fraction"):
            first_value, second_value = first.get(key), second.get(key)
            if first_value is None and second_value is None:
                merged_metrics[key] = None
            elif (
                isinstance(first_value, bool)
                or not isinstance(first_value, (int, float))
                or isinstance(second_value, bool)
                or not isinstance(second_value, (int, float))
            ):
                raise ValueError("primary and confirmation family metrics are incomplete")
            else:
                merged_metrics[key] = (float(first_value) + float(second_value)) / 2.0
        position_bias = [first.get("position_bias"), second.get("position_bias")]
        if all(value is None for value in position_bias):
            merged_position_bias: list[float] = []
        elif any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in position_bias
        ):
            raise ValueError("confirmation position-bias receipts are incomplete")
        else:
            merged_position_bias = [
                float(value) for value in cast(list[int | float], position_bias)
            ]
        merged.append(
            {
                **first,
                **merged_metrics,
                "fit_seconds": float(first["fit_seconds"]) + float(second["fit_seconds"]),
                "score_seconds": float(first["score_seconds"]) + float(second["score_seconds"]),
                "guards": merged_guards,
                "seed_artifacts_sha256": [
                    first["candidate_output_sha256"],
                    second["candidate_output_sha256"],
                ],
                "seed_position_bias": merged_position_bias,
            }
        )
    return PrimaryEvaluationResult(
        primary.experiment_id,
        "primary_measured",
        tuple(merged),
        None,
        primary.wall_seconds + confirmation.wall_seconds,
    )


def _build_replay_manifest(
    *,
    director_engine: Engine,
    run_id: UUID,
    proposal: RegisteredProposal,
    calibration: FrozenCalibrationDocument,
    parent_source: bytes,
    referee: ProposalRefereeResult,
    primary: PrimaryEvaluationResult,
    confirmation: PrimaryEvaluationResult | None,
    artifact_root: Path,
    best_suite: float | None,
    epsilon: float,
    bootstrap_seed: int,
    decision_stage: Literal["primary", "confirmed"],
) -> ReplayManifestDocument | None:
    """Bind exact Referee inputs and seed output receipts into the trajectory."""
    if referee.parent_scores is None or referee.child_scores is None or best_suite is None:
        return None
    decision_seeds = (0, 1) if confirmation is not None else (0,)
    task_refs = tuple(
        sorted(
            calibration.tasks,
            key=lambda item: (item.dataset_id, item.split_id, item.session_id, item.task_id),
        )
    )
    task_ids = tuple(item.task_id for item in task_refs)
    if task_ids != tuple(item.task_id for item in referee.per_task):
        raise ValueError("replay task order differs from the frozen calibration")
    with director_engine.connect() as connection:
        parent_row = (
            connection.execute(
                text(
                    "SELECT candidate_sha256, kind, baseline_name FROM lab.experiments "
                    "WHERE experiment_id = :id"
                ),
                {"id": proposal.parent_experiment_id},
            )
            .mappings()
            .one_or_none()
        )
    if parent_row is None:
        raise ValueError("replay parent experiment is absent from trusted ledger")
    parent_candidate_sha256 = parent_row["candidate_sha256"]
    if not isinstance(parent_candidate_sha256, str) or hashlib.sha256(
        parent_source
    ).hexdigest() != (parent_candidate_sha256):
        raise ValueError("replay parent source differs from its registered candidate hash")

    def seed_rows(
        experiment_id: str,
        candidate_sha256: str,
        seed_kinds: tuple[tuple[int, Literal["baseline", "primary", "confirmation"]], ...],
    ) -> tuple[tuple[tuple[float, ...], ...], tuple[tuple[str, ...], ...], tuple[str, ...]]:
        with director_engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        """SELECT * FROM lab.dev_task_results
                         WHERE run_id = :run_id AND experiment_id = :experiment_id"""
                    ),
                    {"run_id": run_id, "experiment_id": experiment_id},
                )
                .mappings()
                .all()
            )
        indexed: dict[tuple[str, int, str], Any] = {}
        for row in rows:
            if row.get("candidate_sha256") != candidate_sha256:
                raise ValueError("replay Scorer row candidate hash differs from ledger")
            seed_value = row.get("seed")
            task_value = row.get("task_id")
            kind_value = row.get("evaluation_kind")
            if (
                isinstance(seed_value, bool)
                or not isinstance(seed_value, int)
                or not isinstance(task_value, str)
                or kind_value not in {"baseline", "primary", "confirmation"}
            ):
                raise ValueError("replay Scorer row has invalid seed/task identity")
            key = (kind_value, seed_value, task_value)
            if key in indexed:
                raise ValueError("replay Scorer rows contain duplicate seed/task identities")
            indexed[key] = row
        raw_vectors: list[tuple[float, ...]] = []
        output_vectors: list[tuple[str, ...]] = []
        receipts: list[str] = []
        for seed, evaluation_kind in seed_kinds:
            raw_values: list[float] = []
            output_digests: list[str] = []
            for task_ref in task_refs:
                scorer_row = indexed.get((evaluation_kind, seed, task_ref.task_id))
                if scorer_row is None:
                    raise ValueError("replay Scorer evidence is missing a decision task/seed")
                if (
                    scorer_row.get("dataset_id") != task_ref.dataset_id
                    or scorer_row.get("split_id") != task_ref.split_id
                    or scorer_row.get("session_id") != task_ref.session_id
                    or scorer_row.get("profile_sha256") != task_ref.profile_sha256
                    or scorer_row.get("task_family", task_ref.family) != task_ref.family
                ):
                    raise ValueError("replay Scorer task identity differs from calibration")
                raw_value = scorer_row.get("task_score", scorer_row.get("vus_pr"))
                if isinstance(raw_value, bool) or not isinstance(raw_value, (float, int, str)):
                    raise ValueError("replay Scorer row lacks its family score")
                try:
                    raw_score = float(raw_value)
                except (TypeError, ValueError) as exc:
                    raise ValueError("replay Scorer score is malformed") from exc
                if not math.isfinite(raw_score):
                    raise ValueError("replay Scorer score is non-finite")
                digest = scorer_row.get("candidate_output_sha256")
                if not isinstance(digest, str) or len(digest) != 64:
                    raise ValueError("replay Scorer row omits its output artifact receipt")
                output = read_artifact_bytes(digest)
                if hashlib.sha256(output).hexdigest() != digest:
                    raise ValueError("replay Scorer output artifact failed its digest")
                raw_values.append(raw_score)
                output_digests.append(digest)
            raw_vector = tuple(raw_values)
            output_vector = tuple(output_digests)
            raw_vectors.append(raw_vector)
            output_vectors.append(output_vector)
            receipts.append(
                hashlib.sha256(
                    canonical_bytes(
                        {
                            "experiment_id": experiment_id,
                            "seed": seed,
                            "evaluation_kind": evaluation_kind,
                            "task_ids": task_ids,
                            "raw_scores": raw_vector,
                            "output_sha256": output_vector,
                        }
                    )
                ).hexdigest()
            )
        return tuple(raw_vectors), tuple(output_vectors), tuple(receipts)

    if parent_row["kind"] == "baseline":
        parent_seed_kinds: tuple[
            tuple[int, Literal["baseline", "primary", "confirmation"]], ...
        ] = (
            ((0, "baseline"),)
            if decision_stage == "primary"
            else (
                (0, "baseline"),
                (1, "baseline"),
            )
        )
        parent_noise_seed_kinds: tuple[
            tuple[int, Literal["baseline", "primary", "confirmation"]], ...
        ] = tuple((seed, "baseline") for seed in (0, 1, 2))
    elif parent_row["kind"] == "proposal":
        parent_seed_kinds = (
            ((0, "primary"),)
            if decision_stage == "primary"
            else (
                (0, "primary"),
                (1, "confirmation"),
            )
        )
        parent_noise_seed_kinds = (
            (0, "primary"),
            (1, "confirmation"),
            (2, "confirmation"),
        )
    else:
        raise ValueError("replay parent ledger kind is unsupported")
    parent_seed_ids = tuple(seed for seed, _ in parent_seed_kinds)
    parent_raw_scores, parent_output_refs, parent_score_refs = seed_rows(
        proposal.parent_experiment_id, parent_candidate_sha256, parent_seed_kinds
    )

    def child_kind(seed: int) -> Literal["baseline", "primary", "confirmation"]:
        return "primary" if seed == 0 else "confirmation"

    child_seed_kinds: tuple[tuple[int, Literal["baseline", "primary", "confirmation"]], ...] = (
        tuple((seed, child_kind(seed)) for seed in decision_seeds)
    )
    child_raw_scores, child_output_refs, child_score_refs = seed_rows(
        proposal.experiment_id,
        proposal.candidate_sha256,
        child_seed_kinds,
    )
    parent_noise_seed_ids = tuple(seed for seed, _ in parent_noise_seed_kinds)
    parent_noise_raw, parent_noise_outputs, parent_noise_score_refs = seed_rows(
        proposal.parent_experiment_id, parent_candidate_sha256, parent_noise_seed_kinds
    )

    def normalized_mean(raw_rows: tuple[tuple[float, ...], ...]) -> tuple[float, ...]:
        means = [
            sum(row[index] for row in raw_rows) / len(raw_rows) for index in range(len(task_refs))
        ]
        return tuple(
            float(
                normalize_task_score(
                    value,
                    task_ref.base_score,
                    task_ref.reference_score,
                    family=task_ref.family,
                )
            )
            for value, task_ref in zip(means, task_refs, strict=True)
        )

    parent_vector = normalized_mean(parent_raw_scores)
    child_vector = normalized_mean(child_raw_scores)
    if any(
        left.hex() != right.hex()
        for values, expected in (
            (parent_vector, referee.parent_scores),
            (child_vector, referee.child_scores),
        )
        for left, right in zip(values, expected, strict=True)
    ):
        raise ValueError("Referee vectors cannot be reproduced from Scorer rows and calibration")
    weights = tuple(float(task.task_weight) for task in task_refs)
    if any(left.hex() != right.hex() for left, right in zip(weights, referee.weights, strict=True)):
        raise ValueError("Referee weights differ from frozen calibration")
    noise_suite_scores: dict[int, float] = {}
    for seed, raw in zip(parent_noise_seed_ids, parent_noise_raw, strict=True):
        normalized = tuple(
            float(
                normalize_task_score(
                    value,
                    task_ref.base_score,
                    task_ref.reference_score,
                    family=task_ref.family,
                )
            )
            for value, task_ref in zip(raw, task_refs, strict=True)
        )
        noise_suite_scores[seed] = sum(
            value * weight for value, weight in zip(normalized, weights, strict=True)
        ) / sum(weights)
    verified_noise = compute_champion_noise(noise_suite_scores)
    if verified_noise.hex() != referee.decision.noise_sd.hex():
        raise ValueError("Referee noise differs from parent Scorer seed evidence")
    referee_source = inspect.getsourcefile(referee_decide)
    if referee_source is None:
        raise RuntimeError("cannot identify Referee source for immutable replay")
    child_source = proposal.proposal.candidate_source.encode("utf-8")
    fields: dict[str, Any] = {
        "schema": "referee-replay.v1",
        "decision_stage": decision_stage,
        "experiment_id": proposal.experiment_id,
        "run_id": run_id,
        "parent_experiment_id": proposal.parent_experiment_id,
        "parent_tree": proposal.parent_tree_sha256,
        "child_tree": _candidate_git_tree(child_source),
        "candidate_sha256": proposal.candidate_sha256,
        "parent_source_sha256": hashlib.sha256(parent_source).hexdigest(),
        "child_source_sha256": hashlib.sha256(child_source).hexdigest(),
        "calibration_sha256": proposal.calibration_sha256,
        "harness_sha256": proposal.harness_sha256,
        "image_sha256": proposal.image_sha256,
        "suite_id": proposal.suite_id,
        "suite_version": proposal.suite_version,
        "referee_source_sha256": hashlib.sha256(Path(referee_source).read_bytes()).hexdigest(),
        "python_version": platform.python_version(),
        "numpy_version": np.__version__,
        "task_ids": task_ids,
        "expected_decision": referee.decision.model_dump(mode="json"),
        "task_families": tuple(task.family for task in task_refs),
        "profile_sha256": tuple(task.profile_sha256 for task in task_refs),
        "normalization_base": tuple(float(task.base_score) for task in task_refs),
        "normalization_reference": tuple(float(task.reference_score) for task in task_refs),
        "parent": referee.parent_scores,
        "child": referee.child_scores,
        "weights": referee.weights,
        "parent_raw_scores": parent_raw_scores,
        "child_raw_scores": child_raw_scores,
        "parent_noise_raw_scores": parent_noise_raw,
        "parent_noise_seed_ids": parent_noise_seed_ids,
        "parent_noise_evaluation_kinds": tuple(kind for _, kind in parent_noise_seed_kinds),
        "parent_noise_output_sha256": parent_noise_outputs,
        "parent_noise_score_sha256": parent_noise_score_refs,
        "parent_seed_ids": parent_seed_ids,
        "parent_seed_evaluation_kinds": tuple(kind for _, kind in parent_seed_kinds),
        "decision_seeds": decision_seeds,
        "parent_seed_output_sha256": parent_output_refs,
        "parent_seed_score_sha256": parent_score_refs,
        "child_seed_output_sha256": child_output_refs,
        "child_seed_score_sha256": child_score_refs,
        "eps": epsilon,
        "noise_sd": referee.decision.noise_sd,
        "simpler": referee.simpler,
        "guards_ok": all(value == "pass" for value in referee.guards.values()),
        "guard_results": referee.guards,
        "position_bias_threshold": POSITION_BIAS_THRESHOLD,
        "max_task_drop": 0.5,
        "best_suite": best_suite,
        "n_boot": 4000,
        "bootstrap_seed": bootstrap_seed,
    }
    fields["configuration_sha256"] = replay_configuration_sha256(fields)
    return ReplayManifestDocument.model_validate(fields, strict=True)


def commit_primary_terminal_record(
    director_engine: Engine,
    *,
    run_id: UUID,
    proposal: RegisteredProposal,
    tasks: tuple[SuiteTask, ...],
    result: PrimaryEvaluationResult,
    calibration: FrozenCalibrationDocument,
    parent_source: bytes,
    artifact_root: Path,
    confirmation_result: PrimaryEvaluationResult | None = None,
    champion_noise_result: PrimaryEvaluationResult | None = None,
    best_suite: float | None = None,
    champion_scores_by_task: Mapping[str, float] | None = None,
    champion_noise_sd: float | None = None,
    precomputed_referee: ProposalRefereeResult | None = None,
    epsilon: float = 0.01,
    bootstrap_seed: int = 0,
    replay_decisions: tuple[
        tuple[
            Literal["primary", "confirmed"],
            ProposalRefereeResult,
            PrimaryEvaluationResult,
            PrimaryEvaluationResult | None,
        ],
        ...,
    ] = (),
) -> dict[str, Any]:
    """Persist canonical experiment.v1 + trajectory.v1 after trusted scoring."""
    if calibration.run_id != run_id:
        raise ValueError("terminal records and frozen calibration belong to different runs")
    if result.experiment_id != proposal.experiment_id or any(
        item is not None and item.experiment_id != proposal.experiment_id
        for item in (confirmation_result, champion_noise_result)
    ):
        raise ValueError("terminal measurements belong to a different registered experiment")
    if result.status == "primary_measured":
        if result.evaluation_kind != "primary" or result.seed != 0:
            raise ValueError("proposal decision requires primary seed zero")
        if confirmation_result is None:
            decision_input = result
        else:
            if (
                confirmation_result.status != "primary_measured"
                or confirmation_result.evaluation_kind != "confirmation"
                or confirmation_result.seed != 1
            ):
                raise ValueError("proposal confirmation must be measured at seed one")
            decision_input = _merge_confirmation_measurements(result, confirmation_result)
    else:
        decision_input = result
    referee = precomputed_referee or decide_proposal_from_measurements(
        proposal=proposal,
        result=decision_input,
        tasks=tasks,
        calibration=calibration,
        parent_source=parent_source,
        champion_scores_by_task=champion_scores_by_task,
        champion_noise_sd=champion_noise_sd,
        best_suite=best_suite,
        epsilon=epsilon,
        bootstrap_seed=bootstrap_seed,
    )
    if referee.decision.verdict in {"KEEP", "KEEP_SIMPLER"}:
        if confirmation_result is None:
            raise ValueError("promotable proposal requires confirmation seed one")
        if (
            champion_noise_result is None
            or champion_noise_result.status != "primary_measured"
            or champion_noise_result.evaluation_kind != "confirmation"
            or champion_noise_result.seed != 2
            or set(item["task_id"] for item in champion_noise_result.measurements)
            != {task.task_id for task in tasks}
        ):
            raise ValueError(
                "proposal promotion requires the measured seed-two champion-noise repeat"
            )
        _weighted_suite_seed_scores(
            result,
            tasks,
            calibration,
            candidate_sha256=proposal.candidate_sha256,
            evaluation_kind="primary",
            seed=0,
        )
        _weighted_suite_seed_scores(
            confirmation_result,
            tasks,
            calibration,
            candidate_sha256=proposal.candidate_sha256,
            evaluation_kind="confirmation",
            seed=1,
        )
        _weighted_suite_seed_scores(
            champion_noise_result,
            tasks,
            calibration,
            candidate_sha256=proposal.candidate_sha256,
            evaluation_kind="confirmation",
            seed=2,
        )
    status: Literal["scored", "crashed", "abandoned", "rejected"] = (
        "abandoned"
        if result.terminal_code == "infrastructure_abandoned"
        else "scored"
        if result.status == "primary_measured" and referee.decision.verdict != "REJECT"
        else "crashed"
        if result.status == "crashed"
        else "rejected"
    )
    experiment = ExperimentDocument(
        schema="experiment.v1",
        experiment_id=proposal.experiment_id,
        run_id=run_id,
        ordinal=proposal.ledger_sequence + 1,
        kind="proposal",
        experiment_number=proposal.experiment_number,
        baseline_name=None,
        calibration_sha256=proposal.calibration_sha256,
        agent_version=(
            "director.v0.17.0" if proposal.provider_receipt is not None else "director.v0.11.0"
        ),
        parent_experiment_id=proposal.parent_experiment_id,
        candidate_sha256=proposal.candidate_sha256,
        candidate_blob_sha256=proposal.candidate_blob_sha256,
        move_type=proposal.proposal.move_type,
        system=proposal.system,
        hypothesis=proposal.proposal.hypothesis,
        predicted_delta=proposal.proposal.predicted_delta,
        inputs_sha256=proposal.inputs_sha256,
        parent_tree=proposal.parent_tree_sha256,
        child_tree=_candidate_git_tree(proposal.proposal.candidate_source.encode("utf-8")),
        harness_sha256=proposal.harness_sha256,
        image_sha256=proposal.image_sha256,
        suite_id=proposal.suite_id,
        suite_version=proposal.suite_version,
        per_task=referee.per_task,
        suite_score=referee.suite_score,
        guards=referee.guards,
        decision=referee.decision,
        status=status,
        fit_seconds=sum(item.fit_seconds for item in referee.per_task),
        score_seconds=sum(item.score_seconds for item in referee.per_task),
        llm_input_tokens=proposal.input_tokens,
        llm_output_tokens=proposal.output_tokens,
        wall_seconds=max(
            0.0,
            result.wall_seconds
            + (confirmation_result.wall_seconds if confirmation_result else 0.0)
            + (champion_noise_result.wall_seconds if champion_noise_result else 0.0),
        ),
        provider_receipt_sha256=(
            hashlib.sha256(
                canonical_bytes(proposal.provider_receipt.model_dump(mode="json"))
            ).hexdigest()
            if proposal.provider_receipt is not None
            else None
        ),
    )
    provenances = tuple(dict.fromkeys(task.provenance for task in tasks))
    exclusions: tuple[str, ...] = (
        "labels and raw feature values were not supplied to the proposal provider",
    )
    if result.terminal_code == "harness_hash_mismatch":
        exclusions += ("execution identity rejection; not a candidate quality label",)
    replay_inputs = (
        replay_decisions
        or (
            (
                ("confirmed" if confirmation_result is not None else "primary"),
                referee,
                result,
                confirmation_result,
            ),
        )
        if result.status == "primary_measured"
        else replay_decisions
    )
    replay_manifests = tuple(
        manifest
        for stage, decision_result, primary_result, confirmation_input in replay_inputs
        if (
            manifest := _build_replay_manifest(
                director_engine=director_engine,
                run_id=run_id,
                proposal=proposal,
                calibration=calibration,
                parent_source=parent_source,
                referee=decision_result,
                primary=primary_result,
                confirmation=confirmation_input,
                artifact_root=artifact_root,
                best_suite=best_suite,
                epsilon=epsilon,
                bootstrap_seed=bootstrap_seed,
                decision_stage=stage,
            )
        )
        is not None
    )
    if len(replay_manifests) != len(replay_inputs):
        raise ValueError("proposal cannot be committed without every required exact replay input")
    terminal_replay = (
        TerminalReplayDisposition.model_validate(
            {
                "schema": "terminal-replay.v1",
                "experiment_id": proposal.experiment_id,
                "run_id": run_id,
                "evaluation_status": result.status,
                "terminal_code": result.terminal_code or referee.decision.reason,
                "candidate_sha256": proposal.candidate_sha256,
                "parent_experiment_id": proposal.parent_experiment_id,
                "parent_tree": proposal.parent_tree_sha256,
                "calibration_sha256": proposal.calibration_sha256,
                "harness_sha256": proposal.harness_sha256,
                "image_sha256": proposal.image_sha256,
                "decision": referee.decision.model_dump(mode="json"),
            },
            strict=True,
        )
        if result.status != "primary_measured"
        else None
    )
    trajectory_id = uuid5(_EXPERIMENT_NAMESPACE, f"trajectory:{proposal.experiment_id}").hex
    trajectory = TrajectoryDocument(
        schema="trajectory.v1",
        trajectory_id=f"trj_{trajectory_id}",
        run_id=run_id,
        experiment_id=proposal.experiment_id,
        kind="proposal",
        experiment_number=proposal.experiment_number,
        baseline_name=None,
        calibration_sha256=proposal.calibration_sha256,
        agent_version=(
            "director.v0.17.0" if proposal.provider_receipt is not None else "director.v0.11.0"
        ),
        model_id=(
            proposal.provider_receipt.model_id
            if proposal.provider_receipt is not None
            else proposal.deterministic_provider_id or "fake_llm.deterministic.v1"
        ),
        usage_profile="noncommercial_research",
        source_provenance=provenances,
        quantization=("fp8_per_tensor" if proposal.provider_receipt is not None else "none"),
        adapter=(
            "vllm-local"
            if proposal.provider_receipt is not None
            else "parameter-grid"
            if proposal.deterministic_provider_id is not None
            else "fake-provider"
        ),
        system=proposal.system,
        thinking=(
            proposal.provider_receipt.enable_thinking
            if proposal.provider_receipt is not None
            else False
        ),
        temperature=(
            proposal.provider_receipt.sampling_temperature
            if proposal.provider_receipt is not None
            else 0.0
        ),
        top_p=(
            proposal.provider_receipt.sampling_top_p
            if proposal.provider_receipt is not None
            else 1.0
        ),
        context_template=(
            proposal.provider_receipt.context_template
            if proposal.provider_receipt is not None
            else "director.metadata-only.v1"
        ),
        inputs_sha256=proposal.inputs_sha256,
        messages_blob_sha256=proposal.messages_blob_sha256,
        tool_calls=0,
        outcome=referee.decision,
        quality_tier="bronze",
        secrets_scrubbed=False,
        people_scrubbed=False,
        raw_values_scrubbed=False,
        exclusions=exclusions,
        provider_receipt=(
            proposal.provider_receipt.model_dump(mode="json")
            if proposal.provider_receipt is not None
            else None
        ),
        replay_manifests=replay_manifests,
        terminal_replay=terminal_replay,
    )
    transcript_bytes = read_director_artifact(
        proposal.messages_blob_sha256, artifact_root=artifact_root
    )
    if hashlib.sha256(transcript_bytes).hexdigest() != proposal.messages_blob_sha256:
        raise RuntimeError("proposal transcript no longer matches its registered digest")
    experiment_bytes = canonical_json_bytes(experiment)
    trajectory_bytes = canonical_json_bytes(trajectory)
    experiment_blob = store_director_artifact(experiment_bytes, artifact_root=artifact_root)
    trajectory_blob = store_director_artifact(trajectory_bytes, artifact_root=artifact_root)
    if read_director_artifact(experiment_blob, artifact_root=artifact_root) != experiment_bytes:
        raise RuntimeError("experiment document blob failed hash-bound readback")
    if read_director_artifact(trajectory_blob, artifact_root=artifact_root) != trajectory_bytes:
        raise RuntimeError("trajectory document blob failed hash-bound readback")
    return commit_experiment_record(
        director_engine,
        experiment=experiment,
        trajectory=trajectory,
        experiment_blob_sha256=experiment_blob,
        trajectory_blob_sha256=trajectory_blob,
    )


def run_one_proposal(
    director_engine: Engine,
    planner_engine: Engine,
    *,
    lease: DirectorRunLease,
    runner: LocalDockerRunner,
    run_id: UUID,
    ordinal: int,
    parent_experiment_id: str,
    parent_tree_sha256: str,
    parent_source: bytes,
    suite_id: str,
    suite_version: int,
    calibration: FrozenCalibrationDocument,
    harness_sha256: str,
    image_sha256: str,
    system: Literal["S1", "S2"],
    context: AgentContext,
    provider: ProposalProvider,
    tasks: tuple[SuiteTask, ...],
    budget: RunBudget,
    artifact_root: Path,
    best_suite: float,
    screen_champion_scores_by_task: Mapping[str, float] | None = None,
    champion_scores_by_task: Mapping[str, float] | None = None,
    champion_noise: float | None = None,
    infrastructure_retries: int = 1,
    seed_wall_seconds: int = MAX_SEED_WALL_SECONDS,
) -> dict[str, Any]:
    """Run one durable proposal through decision seeds and terminal artifacts.

    This is the smallest callable Director vertical path: provider output is
    registered first, then guarded Docker/Scorer measurements for primary zero,
    conditional confirmation one, and (only for a provisional KEEP) champion
    noise seed two run before the final typed records can commit.
    """
    if not math.isfinite(best_suite):
        raise ValueError("proposal loop requires a fixed measured best_suite")
    lease.require_run_active()
    proposal_checkpoint = lease.read_checkpoint(
        key=f"proposal:{ordinal}", artifact_root=artifact_root
    )
    provider_reservation: EpisodeReservation | None = None
    reservation_key = f"proposal-budget-reservation:{run_id}:{ordinal}"
    reconciled_key = f"proposal-budget-reconciled:{run_id}:{ordinal}"
    reconciled = lease.read_checkpoint(key=reconciled_key, artifact_root=artifact_root)
    reservation_checkpoint = lease.read_checkpoint(key=reservation_key, artifact_root=artifact_root)
    if reconciled is not None:
        reconciled_payload = reconciled.get("payload")
        if isinstance(reconciled_payload, dict) and reconciled_payload.get("status") in {
            "provider_error",
            "provider_error_episode_overrun",
            "provider_error_run_budget_exhausted",
            "episode_budget_overrun",
            "run_budget_exhausted",
        }:
            raise RuntimeError("proposal episode has a durable terminal provider failure")
    if reconciled is not None and proposal_checkpoint is None:
        raise RuntimeError("proposal budget receipt exists without its immutable proposal")
    started_at = datetime.now(UTC)
    started_boottime = boottime()
    current_boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    reservation_boot_id = current_boot_id
    if reconciled is None:
        if reservation_checkpoint is None:
            if proposal_checkpoint is not None:
                raise RuntimeError("proposal checkpoint has no durable budget reconciliation")
            deterministic_grid = isinstance(provider, ParameterGridProvider)
            provider_reservation = budget.reserve_proposal(
                wall_seconds=(
                    min(10, int(budget.remaining_wall_seconds))
                    if deterministic_grid
                    else MAX_EPISODE_WALL_SECONDS[system]
                ),
                model_tokens=(
                    0
                    if deterministic_grid
                    else MAX_EPISODE_CONTEXT_TOKENS + MAX_EPISODE_OUTPUT_TOKENS[system]
                ),
            )
            reservation_payload = {
                "run_id": str(run_id),
                "ordinal": ordinal,
                "reservation_id": str(provider_reservation.reservation_id),
                "wall_seconds": provider_reservation.wall_seconds,
                "model_tokens": provider_reservation.model_tokens,
                "started_at": started_at.isoformat(),
                "started_boottime": started_boottime,
                "boot_id": current_boot_id,
            }
            _append_next_checkpoint(
                lease,
                director_engine,
                run_id=run_id,
                key=reservation_key,
                phase="proposal_budget_reserved",
                payload=reservation_payload,
                artifact_root=artifact_root,
            )
        else:
            reservation_payload = reservation_checkpoint["payload"]
            if (
                not isinstance(reservation_payload, dict)
                or reservation_payload.get("run_id") != str(run_id)
                or reservation_payload.get("ordinal") != ordinal
            ):
                raise ValueError("proposal reservation belongs to a different run or ordinal")
            wall_seconds_value = reservation_payload.get("wall_seconds")
            token_value = reservation_payload.get("model_tokens")
            started_value = reservation_payload.get("started_at")
            started_boottime_value = reservation_payload.get("started_boottime")
            boot_id_value = reservation_payload.get("boot_id")
            if (
                isinstance(wall_seconds_value, bool)
                or not isinstance(wall_seconds_value, int)
                or isinstance(token_value, bool)
                or not isinstance(token_value, int)
                or not isinstance(started_value, str)
                or isinstance(started_boottime_value, bool)
                or not isinstance(started_boottime_value, (int, float))
                or not isinstance(boot_id_value, str)
            ):
                raise ValueError("proposal reservation has invalid bounded counters")
            provider_reservation = EpisodeReservation(
                reservation_id=UUID(str(reservation_payload["reservation_id"])),
                wall_seconds=wall_seconds_value,
                model_tokens=token_value,
            )
            started_at = datetime.fromisoformat(started_value)
            if started_at.tzinfo is None:
                raise ValueError("proposal reservation timestamp must be timezone-aware")
            budget.adopt_proposal_reservation(provider_reservation)
            started_boottime = float(started_boottime_value)
            reservation_boot_id = boot_id_value

    def provider_elapsed_seconds() -> float:
        if reservation_boot_id != current_boot_id:
            return float(provider_reservation.wall_seconds if provider_reservation else 0)
        return max(0.0, boottime() - started_boottime)

    try:
        proposal = register_proposal_before_execution(
            director_engine,
            run_id=run_id,
            ordinal=ordinal,
            parent_experiment_id=parent_experiment_id,
            parent_tree_sha256=parent_tree_sha256,
            suite_id=suite_id,
            suite_version=suite_version,
            calibration_sha256=calibration_sha256(calibration),
            harness_sha256=harness_sha256,
            image_sha256=image_sha256,
            system=system,
            context=context,
            provider=provider,
            lease=lease,
            artifact_root=artifact_root,
            remaining_wall_seconds=(
                max(0.0, provider_reservation.wall_seconds - provider_elapsed_seconds())
                if provider_reservation is not None
                else None
            ),
            remaining_model_tokens=(
                provider_reservation.model_tokens if provider_reservation is not None else None
            ),
            completion_clock=(
                _ProposalCompletionClock(
                    run_id, ordinal, provider_reservation, reservation_boot_id,
                    started_boottime, current_boot_id,
                )
                if provider_reservation is not None else None
            ),
        )
    except Exception as provider_error:
        if proposal_checkpoint is not None and isinstance(
            provider_error, ProposalCompletionUnverified
        ):
            # No timing estimate or new charge can repair an unverified durable proposal.
            raise
        if provider_reservation is not None:
            elapsed = provider_elapsed_seconds()
            provider_receipt = getattr(provider_error, "provider_receipt", None)
            if not isinstance(provider_receipt, ProviderReceipt):
                provider_receipt = None
            used_tokens = (
                provider_receipt.input_tokens + provider_receipt.output_tokens
                if provider_receipt is not None
                else provider_reservation.model_tokens
            )
            usage_incomplete = provider_receipt is None or any(
                attempt.outcome == "failed" for attempt in provider_receipt.attempts
            )
            if usage_incomplete:
                used_tokens = provider_reservation.model_tokens
            response_digest = None
            response_text = getattr(provider_error, "response_text", None)
            if (
                isinstance(response_text, str)
                and len(response_text.encode("utf-8")) <= _MAX_MESSAGE_BYTES
            ):
                response_bytes = response_text.encode("utf-8")
                response_digest = store_director_artifact(
                    response_bytes, artifact_root=artifact_root
                )
            provider_attempts = getattr(provider_error, "attempt_transcripts", ())
            attempts_blob_digest = None
            if provider_attempts:
                attempts_bytes = canonical_bytes(
                    {
                        "provider_attempts": [
                            item.model_dump(mode="json") for item in provider_attempts
                        ]
                    }
                )
                attempts_blob_digest = store_director_artifact(
                    attempts_bytes, artifact_root=artifact_root
                )
            status = "provider_error"
            try:
                budget.reconcile_proposal(
                    provider_reservation,
                    measured_wall_seconds=elapsed,
                    measured_model_tokens=used_tokens,
                )
            except RuntimeError:
                budget.reconcile_proposal_overrun(
                    provider_reservation,
                    measured_wall_seconds=elapsed,
                    measured_model_tokens=used_tokens,
                )
                status = (
                    "provider_error_episode_overrun"
                    if elapsed > provider_reservation.wall_seconds
                    or used_tokens > provider_reservation.model_tokens
                    else "provider_error_run_budget_exhausted"
                )
            _append_next_checkpoint(
                lease,
                director_engine,
                run_id=run_id,
                key=reconciled_key,
                phase="proposal_budget_reconciled",
                payload={
                    "ordinal": ordinal,
                    "status": status,
                    "provider_receipt": (
                        provider_receipt.model_dump(mode="json")
                        if provider_receipt is not None
                        else None
                    ),
                    "response_blob_sha256": response_digest,
                    "error_type": type(provider_error).__name__,
                    "reason_code": getattr(provider_error, "reason_code", "provider_error"),
                    "measured_wall_seconds": elapsed,
                    "charged_model_tokens": used_tokens,
                    "usage_incomplete": usage_incomplete,
                    "attempt_transcripts_blob_sha256": attempts_blob_digest,
                    "budget": _budget_json(budget),
                },
                artifact_root=artifact_root,
            )
        raise
    if provider_reservation is not None:
        if proposal.completion_wall_seconds is None:
            raise ProposalCompletionUnsupported("proposal_completion_missing: unsupported replay")
        elapsed = proposal.completion_wall_seconds
        measured_tokens = proposal.input_tokens + proposal.output_tokens
        try:
            budget.reconcile_proposal(
                provider_reservation,
                measured_wall_seconds=elapsed,
                measured_model_tokens=measured_tokens,
            )
        except RuntimeError as budget_error:
            budget.reconcile_proposal_overrun(
                provider_reservation,
                measured_wall_seconds=elapsed,
                measured_model_tokens=measured_tokens,
            )
            status = (
                "episode_budget_overrun"
                if elapsed > provider_reservation.wall_seconds
                or measured_tokens > provider_reservation.model_tokens
                else "run_budget_exhausted"
            )
            _append_next_checkpoint(
                lease,
                director_engine,
                run_id=run_id,
                key=reconciled_key,
                phase="proposal_budget_reconciled",
                payload={
                    "ordinal": ordinal,
                    "status": status,
                    "experiment_id": proposal.experiment_id,
                    "measured_wall_seconds": elapsed,
                    "charged_model_tokens": measured_tokens,
                    "budget": _budget_json(budget),
                },
                artifact_root=artifact_root,
            )
            raise RuntimeError("proposal episode exceeded its durable budget") from budget_error
        _append_next_checkpoint(
            lease,
            director_engine,
            run_id=run_id,
            key=reconciled_key,
            phase="proposal_budget_reconciled",
            payload={
                "experiment_id": proposal.experiment_id,
                "ordinal": ordinal,
                "budget": _budget_json(budget),
            },
            artifact_root=artifact_root,
        )

    lease.require_run_active()
    primary = execute_candidate_seed(
        director_engine,
        planner_engine,
        lease=lease,
        runner=runner,
        run_id=run_id,
        proposal=proposal,
        tasks=tasks,
        harness_sha256=harness_sha256,
        image_sha256=image_sha256,
        budget=budget,
        evaluation_kind="primary",
        seed=0,
        artifact_root=artifact_root,
        infrastructure_retries=infrastructure_retries,
        seed_wall_seconds=seed_wall_seconds,
    )
    if primary.status != "primary_measured":
        return commit_primary_terminal_record(
            director_engine,
            run_id=run_id,
            proposal=proposal,
            tasks=tasks,
            result=primary,
            calibration=calibration,
            parent_source=parent_source,
            artifact_root=artifact_root,
            best_suite=best_suite,
            champion_scores_by_task=champion_scores_by_task,
            champion_noise_sd=champion_noise,
        )

    primary_preview = decide_proposal_from_measurements(
        proposal=proposal,
        result=primary,
        tasks=tasks,
        calibration=calibration,
        parent_source=parent_source,
        champion_scores_by_task=(
            screen_champion_scores_by_task
            if screen_champion_scores_by_task is not None
            else champion_scores_by_task
        ),
        champion_noise_sd=champion_noise,
        best_suite=best_suite,
    )
    if primary_preview.decision.verdict not in {"KEEP", "KEEP_SIMPLER"}:
        return commit_primary_terminal_record(
            director_engine,
            run_id=run_id,
            proposal=proposal,
            tasks=tasks,
            result=primary,
            calibration=calibration,
            parent_source=parent_source,
            artifact_root=artifact_root,
            best_suite=best_suite,
            champion_scores_by_task=(
                screen_champion_scores_by_task
                if screen_champion_scores_by_task is not None
                else champion_scores_by_task
            ),
            champion_noise_sd=champion_noise,
            precomputed_referee=primary_preview,
            replay_decisions=(("primary", primary_preview, primary, None),),
        )

    lease.require_run_active()
    _advance_experiment_if_needed(
        director_engine, experiment_id=proposal.experiment_id, status="awaiting_confirmation"
    )
    _advance_experiment_if_needed(
        director_engine, experiment_id=proposal.experiment_id, status="confirmation_running"
    )
    lease.require_run_active()
    confirmation = execute_candidate_seed(
        director_engine,
        planner_engine,
        lease=lease,
        runner=runner,
        run_id=run_id,
        proposal=proposal,
        tasks=tasks,
        harness_sha256=harness_sha256,
        image_sha256=image_sha256,
        budget=budget,
        artifact_root=artifact_root,
        evaluation_kind="confirmation",
        seed=1,
        infrastructure_retries=infrastructure_retries,
        seed_wall_seconds=seed_wall_seconds,
    )
    if confirmation.status != "primary_measured":
        failed = PrimaryEvaluationResult(
            proposal.experiment_id,
            confirmation.status,
            primary.measurements,
            confirmation.terminal_code,
            primary.wall_seconds + confirmation.wall_seconds,
        )
        return commit_primary_terminal_record(
            director_engine,
            run_id=run_id,
            proposal=proposal,
            tasks=tasks,
            result=failed,
            calibration=calibration,
            parent_source=parent_source,
            artifact_root=artifact_root,
            best_suite=best_suite,
            champion_scores_by_task=champion_scores_by_task,
            champion_noise_sd=champion_noise,
            replay_decisions=(("primary", primary_preview, primary, None),),
        )

    merged = _merge_confirmation_measurements(primary, confirmation)
    referee_preview = decide_proposal_from_measurements(
        proposal=proposal,
        result=merged,
        tasks=tasks,
        calibration=calibration,
        parent_source=parent_source,
        champion_scores_by_task=champion_scores_by_task,
        champion_noise_sd=champion_noise,
        best_suite=best_suite,
    )
    noise_repeat: PrimaryEvaluationResult | None = None
    if referee_preview.decision.verdict in {"KEEP", "KEEP_SIMPLER"}:
        lease.require_run_active()
        noise_repeat = execute_candidate_seed(
            director_engine,
            planner_engine,
            lease=lease,
            runner=runner,
            run_id=run_id,
            proposal=proposal,
            tasks=tasks,
            harness_sha256=harness_sha256,
            image_sha256=image_sha256,
            budget=budget,
            evaluation_kind="confirmation",
            seed=2,
            artifact_root=artifact_root,
            infrastructure_retries=infrastructure_retries,
            seed_wall_seconds=seed_wall_seconds,
        )
        if noise_repeat.status != "primary_measured":
            failed = PrimaryEvaluationResult(
                proposal.experiment_id,
                noise_repeat.status,
                primary.measurements,
                noise_repeat.terminal_code,
                primary.wall_seconds + confirmation.wall_seconds + noise_repeat.wall_seconds,
            )
            return commit_primary_terminal_record(
                director_engine,
                run_id=run_id,
                proposal=proposal,
                tasks=tasks,
                result=failed,
                calibration=calibration,
                parent_source=parent_source,
                artifact_root=artifact_root,
                best_suite=best_suite,
                champion_scores_by_task=champion_scores_by_task,
                champion_noise_sd=champion_noise,
                replay_decisions=(
                    ("primary", primary_preview, primary, None),
                    ("confirmed", referee_preview, primary, confirmation),
                ),
            )
        primary_score = _weighted_suite_seed_scores(
            primary,
            tasks,
            calibration,
            candidate_sha256=proposal.candidate_sha256,
            evaluation_kind="primary",
            seed=0,
        )
        confirmation_score = _weighted_suite_seed_scores(
            confirmation,
            tasks,
            calibration,
            candidate_sha256=proposal.candidate_sha256,
            evaluation_kind="confirmation",
            seed=1,
        )
        noise_values = _weighted_suite_seed_scores(
            noise_repeat,
            tasks,
            calibration,
            candidate_sha256=proposal.candidate_sha256,
            evaluation_kind="confirmation",
            seed=2,
        )
        noise_document = {
            "experiment_id": proposal.experiment_id,
            "evaluation_kind": "confirmation",
            "purpose": "champion_noise",
            "seed_set": [0, 1, 2],
            "suite_seed_scores": {
                "0": primary_score,
                "1": confirmation_score,
                "2": noise_values,
            },
            "noise_sd": compute_champion_noise(
                {
                    0: primary_score,
                    1: confirmation_score,
                    2: noise_values,
                }
            ),
        }
        _append_next_checkpoint(
            lease,
            director_engine,
            run_id=run_id,
            key=f"champion-noise:{proposal.experiment_id}",
            phase="champion_noise_measured",
            payload=noise_document,
            artifact_root=artifact_root,
        )
    return commit_primary_terminal_record(
        director_engine,
        run_id=run_id,
        proposal=proposal,
        tasks=tasks,
        result=primary,
        confirmation_result=confirmation,
        champion_noise_result=noise_repeat,
        calibration=calibration,
        parent_source=parent_source,
        artifact_root=artifact_root,
        best_suite=best_suite,
        champion_scores_by_task=champion_scores_by_task,
        champion_noise_sd=champion_noise,
        precomputed_referee=referee_preview,
        replay_decisions=(
            ("primary", primary_preview, primary, None),
            ("confirmed", referee_preview, primary, confirmation),
        ),
    )


def _weighted_suite_seed_scores(
    result: PrimaryEvaluationResult,
    tasks: tuple[SuiteTask, ...],
    calibration: FrozenCalibrationDocument,
    *,
    candidate_sha256: str,
    evaluation_kind: Literal["primary", "confirmation"],
    seed: int,
) -> float:
    """Compute one measured seed's weighted normalized suite score."""
    measurements = {item["task_id"]: item for item in result.measurements}
    calibrations = {item.task_id: item for item in calibration.tasks}
    if (
        result.experiment_id == ""
        or result.status != "primary_measured"
        or result.evaluation_kind != evaluation_kind
        or result.seed != seed
        or set(measurements) != {task.task_id for task in tasks}
        or len(measurements) != len(result.measurements)
    ):
        raise ValueError("suite seed score is incomplete")
    total_weight = 0.0
    weighted_score = 0.0
    for task in tasks:
        measured = measurements[task.task_id]
        reference = calibrations[task.task_id]
        if any(
            measured.get(key) != expected
            for key, expected in (
                ("candidate_sha256", candidate_sha256),
                ("task_id", task.task_id),
                ("dataset_id", task.dataset_id),
                ("split_id", task.split_id),
                ("session_id", task.session_id),
                ("profile_sha256", task.profile_sha256),
            )
        ):
            raise ValueError("suite seed measurement has an invalid identity")
        output_digest = measured.get("candidate_output_sha256")
        if not isinstance(output_digest, str) or len(output_digest) != 64:
            raise ValueError("suite seed measurement has no Scorer artifact receipt")
        guards = measured.get("guards")
        if not isinstance(guards, dict) or any(
            not isinstance(guards.get(name), str) or not guards[name].endswith("_pass")
            for name in ("hardcoding", "determinism", "causality")
        ):
            raise ValueError("suite seed measurement has incomplete guard evidence")
        primary_score = (
            measured.get("vus_pr") if reference.family == "EVT" else measured.get("task_score")
        )
        if isinstance(primary_score, bool) or not isinstance(primary_score, (int, float)):
            raise ValueError("suite seed is missing its family primary score")
        normalized = normalize_task_score(
            float(primary_score),
            reference.base_score,
            reference.reference_score,
            family=reference.family,
        )
        weighted_score += float(normalized) * reference.task_weight
        total_weight += reference.task_weight
    score = weighted_score / total_weight
    if not math.isfinite(score):
        raise ValueError("suite seed score is non-finite")
    return score


def _advance_experiment_if_needed(engine: Engine, *, experiment_id: str, status: str) -> None:
    """Make a nonterminal phase transition idempotent across Director restarts."""
    if not hasattr(engine, "connect"):
        transition_experiment(engine, experiment_id=experiment_id, status=status)
        return
    with engine.connect() as connection:
        current = connection.execute(
            text("SELECT status FROM lab.experiments WHERE experiment_id=:experiment_id"),
            {"experiment_id": experiment_id},
        ).scalar_one_or_none()
    if current is None:
        raise ValueError("proposal experiment is not registered")
    order = {
        "proposed": 0,
        "primary_running": 1,
        "awaiting_confirmation": 2,
        "confirmation_running": 3,
    }
    current_rank = order.get(str(current))
    requested_rank = order.get(status)
    if current_rank is None or requested_rank is None:
        raise RuntimeError("proposal has a terminal or unsupported durable phase")
    if requested_rank <= current_rank:
        return
    if requested_rank != current_rank + 1:
        raise RuntimeError("proposal cannot resume from its durable experiment phase")
    transition_experiment(engine, experiment_id=experiment_id, status=status)
