"""Strict, bounded API payloads for the local Lab control surface."""

from __future__ import annotations

from typing import Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer

from lab.director.field_context import FieldIntent
from lab.director.history_context import PriorExperienceSelection


class RunBudget(BaseModel):
    """Run-wide ceilings, separate from per-episode LLM limits."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    # The plateau rule needs 25 non-KEEP proposals before 10 EXPLORE proposals.
    # Baselines and confirmation seeds are separately budgeted by the Director.
    experiments: int = Field(ge=1, le=35)
    wall_seconds: int = Field(ge=1, le=14_400)
    model_tokens: int = Field(ge=0, le=350_000)


class StartRunRequest(BaseModel):
    """Request a durable run; identity is bound by the authenticated service principal."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    idempotency_key: str = Field(min_length=16, max_length=128)
    track: Literal["anomaly", "mode"]
    suite: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$")
    budget: RunBudget
    program_version: str = Field(min_length=1, max_length=64)
    external_task_id: str | None = Field(
        default=None, pattern=r"^task-[0-9a-f]{32}$", max_length=37
    )
    external_run_id: str | None = Field(default=None, pattern=r"^run-[0-9a-f]{32}$", max_length=36)
    external_action_id: str | None = Field(
        default=None, pattern=r"^action-[0-9a-f]{32}$", max_length=39
    )
    prior_experience: PriorExperienceSelection | None = None
    field_intent: FieldIntent | None = None

    @model_serializer(mode="wrap")
    def preserve_request_bytes(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Absent opt-in history must preserve legacy payload/idempotency bytes."""
        value = cast(dict[str, Any], handler(self))
        if self.field_intent is None:
            value.pop("field_intent", None)
        if self.prior_experience is None:
            value.pop("prior_experience", None)
        return value


class BaselineBudget(BaseModel):
    """Baseline-only budget: measured wall time and explicitly no research work."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    experiments: Literal[0]
    wall_seconds: int = Field(ge=1, le=14_400)
    model_tokens: Literal[0]


class StartBaselineRequest(BaseModel):
    """Request one registered, independent baseline calibration operation."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    idempotency_key: str = Field(min_length=16, max_length=128)
    suite: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$")
    budget: BaselineBudget
    program_version: str = Field(min_length=1, max_length=64)


class StartRunResponse(BaseModel):
    """Durable run identity and current state."""

    run_id: UUID
    state: str
    reused: bool


class RunStatusResponse(BaseModel):
    """Owner-visible run status without scorer-only labels or candidate output."""

    run_id: UUID
    origin: str
    purpose: Literal["baseline", "research", "mode-grid", "mode-stream"] | None = None
    state: str
    created_at: str
    updated_at: str
    stop_requested: bool
    report_sha256: str | None = None
