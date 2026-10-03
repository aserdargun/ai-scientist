"""Bounded user-declared field intent; no entity, scoring or operational authority."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from lab.director.journal import canonical_bytes


class FieldIntent(BaseModel):
    """Plain advisory metadata, explicitly outside the trusted dataset/task identity."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    asset_id: StrictStr = Field(min_length=1, max_length=128)
    goal_kind: Literal["digital_twin", "predictive_maintenance"]
    objective: StrictStr = Field(min_length=1, max_length=600)

    @field_validator("asset_id", "objective", mode="before")
    @classmethod
    def plain_text(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("field intent requires plain text")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("field intent cannot contain control characters")
        value = value.strip()
        if not value:
            raise ValueError("field intent must be nonempty plain text without control characters")
        return value


class FieldContext(BaseModel):
    """Server-frozen input binding; the asset reference remains a user declaration."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["field-study-context.v1"] = Field(alias="schema")
    intent: FieldIntent
    snapshot_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    asset_identity: Literal["user_supplied"] = "user_supplied"
    use: Literal["advisory-only"] = "advisory-only"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(
            canonical_bytes(self.model_dump(mode="json", by_alias=True))
        ).hexdigest()


def load_frozen_field_context(
    request: Mapping[str, Any], *, expected_snapshot_sha256: str | None = None
) -> FieldContext | None:
    """Fresh and resume read the immutable request only; no UI/history reconstruction."""
    intent = request.get("field_intent")
    frozen, sha = request.get("field_context"), request.get("field_context_sha256")
    if intent is None:
        if frozen is not None or sha is not None:
            raise ValueError("field context lacks its admitted user intent")
        return None
    if (
        request.get("track") != "mode"
        or request.get("provider") not in {"mode-grid", "local-qwen"}
        or frozen is None
        or sha is None
    ):
        raise ValueError("field intent requires its verified mode snapshot admission")
    context = FieldContext.model_validate(frozen, strict=True)
    if (
        FieldIntent.model_validate(intent, strict=True) != context.intent
        or context.sha256 != sha
        or (
            expected_snapshot_sha256 is not None
            and context.snapshot_sha256 != expected_snapshot_sha256
        )
    ):
        raise ValueError("frozen field context differs from immutable admission")
    return context
