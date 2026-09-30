"""Deterministic proposal-only provider used by the Director integration run."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StrictStr,
    model_serializer,
    model_validator,
)

from lab.director.contracts import CandidateProposal, MoveType
from lab.director.explore import ExploreIntent, family_directive
from lab.director.journal import canonical_bytes


class AgentContext(BaseModel):
    """Bounded metadata-only prompt context; raw series and labels are excluded."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    phase: StrictStr = Field(min_length=1, max_length=32)
    experiment_number: int = Field(ge=1, le=35)
    system: Literal["S1", "S2"] = "S1"
    move_type: MoveType | None = None
    task_cards: tuple[StrictStr, ...] = Field(min_length=1, max_length=64)
    champion_source: StrictStr = Field(min_length=1, max_length=65_536)
    recent_feedback: tuple[StrictStr, ...] = Field(max_length=30)
    explore_intent: ExploreIntent | None = None
    explore_directive: StrictStr | None = None

    @model_validator(mode="after")
    def verify_explore_directive(self) -> AgentContext:
        if self.explore_intent is not None:
            if (
                self.phase != "explore"
                or self.system != "S2"
                or self.explore_directive != family_directive(self.explore_intent)
                or not self.explore_intent.start_ordinal
                <= self.experiment_number
                < self.explore_intent.start_ordinal + 10
            ):
                raise ValueError("EXPLORE context differs from its exact S2 family intent")
        elif self.explore_directive is not None:
            raise ValueError("family directive requires an immutable EXPLORE intent")
        return self

    @model_serializer(mode="wrap")
    def preserve_context_bytes(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        value = cast(dict[str, Any], handler(self))
        if self.explore_intent is None:
            value.pop("explore_intent", None)
            value.pop("explore_directive", None)
        return value


class ProviderRuntimeReceipt(BaseModel):
    """Host-observed model process details for one bounded provider turn."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    unit: StrictStr | None = Field(default=None, min_length=1, max_length=255)
    invocation_id: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    control_group: StrictStr | None = Field(default=None, min_length=1, max_length=512)
    main_pid: int | None = Field(default=None, gt=0)
    main_start_ticks: int | None = Field(default=None, gt=0)
    profile_id: StrictStr = Field(min_length=1, max_length=128)
    attempt_kind: Literal["proposal", "repair"]
    sampling_top_k: int | None = Field(default=None, ge=1, le=128)
    prompt_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    response_schema_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    response_sha256: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    prompt_utf8_bytes: int = Field(ge=1, le=128 * 1024)
    preflight_prompt_tokens: int | None = Field(default=None, ge=1, le=16_384)
    output_token_limit: int = Field(ge=1, le=8_192)
    outcome: Literal["completed", "output_budget_exhausted", "failed"]
    failure_type: StrictStr | None = Field(default=None, max_length=64)
    wall_seconds: float = Field(ge=0.0, allow_inf_nan=False)
    prompt_tokens: int | None = Field(default=None, ge=0, le=16_384)
    completion_tokens: int | None = Field(default=None, ge=0, le=8_192)
    startup_seconds: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    inference_seconds: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    drain_seconds: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    peak_host_memory_bytes: int | None = Field(default=None, ge=0)
    peak_gpu_memory_mib: int | None = Field(default=None, ge=0)
    cpu_usage_usec: int | None = Field(default=None, ge=0)


class ProviderPromptMessage(BaseModel):
    """One exact role/content message sent to the trusted local model."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    role: Literal["system", "user", "assistant"]
    content: StrictStr = Field(max_length=262_144)


class ProviderAttemptTranscript(BaseModel):
    """Raw final content and exact prompts for one model request, without reasoning."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    attempt_kind: Literal["proposal", "repair"]
    profile_id: StrictStr = Field(min_length=1, max_length=128)
    prompt_messages: tuple[ProviderPromptMessage, ...] = Field(min_length=2, max_length=4)
    prompt_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    response_schema_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    response_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    response_text: StrictStr = Field(max_length=262_144)
    preflight_prompt_tokens: int = Field(ge=1, le=16_384)


class ProviderReceipt(BaseModel):
    """Trusted configuration and observed usage; never supplied by the model."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, validate_by_name=True)

    schema_version: Literal["local-qwen-provider-receipt.v1"] = Field(alias="schema")
    provider_id: Literal["local-qwen.v1"]
    model_id: StrictStr = Field(min_length=1, max_length=256)
    model_revision: StrictStr = Field(min_length=1, max_length=128)
    model_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    profile_id: StrictStr = Field(min_length=1, max_length=128)
    profile_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    requested_system: Literal["S1", "S2"]
    actual_system: Literal["S1", "S2"]
    fallback_from: Literal["S2"] | None
    failure_profile_id: StrictStr | None = Field(default=None, max_length=128)
    failure_type: StrictStr | None = Field(default=None, max_length=64)
    context_template: Literal[
        "director.candidate-contract.metadata-only.v2",
        "director.candidate-contract.metadata-only.v3",
        "director.candidate-contract.metadata-only.v4",
        "director.candidate-contract.metadata-only.v5",
    ]
    context_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    input_tokens: int = Field(ge=0, le=49_152)
    output_tokens: int = Field(ge=0, le=16_384)
    attempts: tuple[ProviderRuntimeReceipt, ...] = Field(min_length=1, max_length=3)
    attempted_profile_ids: tuple[StrictStr, ...] = Field(min_length=1, max_length=3)
    provider_config_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    provider_registry_entry_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    sampling_temperature: float = Field(ge=0.0, le=2.0, allow_inf_nan=False)
    sampling_top_p: float = Field(gt=0.0, le=1.0, allow_inf_nan=False)
    sampling_top_k: int | None = Field(default=None, ge=1, le=128)
    enable_thinking: bool
    thinking_token_budget: int | None


class ProposalTurn(BaseModel):
    """Proposal and transcript plus optional trusted local-runtime evidence."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    proposal: CandidateProposal
    messages: tuple[StrictStr, ...] = Field(min_length=1, max_length=32)
    input_tokens: int = Field(ge=0, le=49_152)
    output_tokens: int = Field(ge=0, le=16_384)
    provider_receipt: ProviderReceipt | None = None
    provider_attempts: tuple[ProviderAttemptTranscript, ...] = Field(default=(), max_length=3)


class ProposalProvider(Protocol):
    """Proposal source; implementations cannot return scores or verdicts."""

    def propose(self, context: AgentContext) -> ProposalTurn:
        """Create one bounded proposal from non-sensitive task cards."""


class FakeLLM:
    """Replayable fake LLM that only yields strict source/hypothesis proposals."""

    def __init__(self, turns: Sequence[ProposalTurn]) -> None:
        if not turns or len(turns) > 35:
            raise ValueError("fake provider requires 1..35 proposal turns")
        self._turns = tuple(turns)

    def propose(self, context: AgentContext) -> ProposalTurn:
        index = context.experiment_number - 1
        if index >= len(self._turns):
            raise RuntimeError("fake_proposal_sequence_exhausted")
        return self._turns[index]

    def __len__(self) -> int:
        """Expose only the immutable proposal count for CLI budget validation."""
        return len(self._turns)


def prompt_context_sha256(context: AgentContext) -> str:
    """Hash the canonical metadata-only agent context, never raw series bytes."""
    payload = context.model_dump(mode="json")
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def prompt_messages_sha256(messages: Sequence[Mapping[str, str]]) -> str:
    """Hash only the exact role/content messages sent to the model runtime."""
    normalized = [{"role": message["role"], "content": message["content"]} for message in messages]
    return hashlib.sha256(canonical_bytes({"messages": normalized})).hexdigest()
