"""Typed candidate contracts, corrected from spec Appendix C and review addendum."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


@dataclass(frozen=True, slots=True)
class FitContext:
    """Immutable, label-free settings passed to candidate fitting."""

    seed: int
    signals: tuple[str, ...]
    regime_signals: tuple[str, ...]
    sampling_s: int | None
    time_budget_s: float

    def __post_init__(self) -> None:
        if self.sampling_s is not None and (
            isinstance(self.sampling_s, bool)
            or not isinstance(self.sampling_s, int)
            or self.sampling_s < 1
        ):
            raise ValueError("sampling_s must be a positive integer or None")


@dataclass(frozen=True, slots=True)
class AlarmPolicy:
    """Finite alarm thresholds and a positive consecutive-sample dwell count."""

    threshold: float
    release: float
    dwell: int

    def __post_init__(self) -> None:
        if isinstance(self.dwell, bool) or not isinstance(self.dwell, int):
            raise ValueError("dwell pozitif bir integer olmalı")
        if not np.isfinite(self.threshold) or not np.isfinite(self.release):
            raise ValueError("alarm eşikleri sonlu olmalı")
        if self.dwell < 1 or self.release > self.threshold:
            raise ValueError("dwell >= 1 ve release <= threshold olmalı")


@runtime_checkable
class ADPipeline(Protocol):
    """Candidate anomaly detection interface."""

    def fit(self, train: pd.DataFrame, ctx: FitContext) -> None:
        """Fit only from the supplied training partition."""

    def score(self, data: pd.DataFrame) -> np.ndarray:
        """Return one causal score per input row."""

    def alarm_policy(self, train_scores: np.ndarray) -> AlarmPolicy:
        """Choose an alarm policy from training scores only."""


@runtime_checkable
class ModePipeline(Protocol):
    """Candidate operating mode assignment interface."""

    def fit(self, train: pd.DataFrame, ctx: FitContext) -> None:
        """Fit only from the supplied training partition."""

    def assign(self, data: pd.DataFrame) -> np.ndarray:
        """Return one mode id per input row; -1 denotes noise."""

    def describe(self) -> list[dict[str, object]]:
        """Describe modes in domain units without exposing labels."""


@dataclass(frozen=True, slots=True)
class Decision:
    """Referee verdict with nullable measurements for strict JSON rejection records."""

    verdict: str
    delta: float | None
    ci_low: float | None
    reason: str


class ReplayManifest(BaseModel):
    """Strict, extra-forbid JSON contract containing every Referee input."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    parent: tuple[float, ...]
    child: tuple[float, ...]
    weights: tuple[float, ...]
    eps: float
    noise_sd: float
    simpler: bool
    guards_ok: bool
    max_task_drop: float = 0.5
    best_suite: float | None = None
    n_boot: int = Field(default=4000, ge=1)
    seed: int = 0

    @field_validator("parent", "child", "weights", mode="before")
    @classmethod
    def normalize_json_vectors(cls, values: object) -> object:
        """Accept JSON arrays while leaving elements subject to strict float checks."""
        if isinstance(values, list):
            return tuple(values)
        return values

    @field_validator("parent", "child", "weights")
    @classmethod
    def validate_vectors(cls, values: tuple[float, ...]) -> tuple[float, ...]:
        """Require finite values before Referee arithmetic."""
        if not values or not all(np.isfinite(value) for value in values):
            raise ValueError("replay vectors must be non-empty and finite")
        return values

    @model_validator(mode="after")
    def validate_relationships(self) -> ReplayManifest:
        """Validate score domain, vector shape, weights and every scalar input."""
        if len(self.parent) != len(self.child) or len(self.parent) != len(self.weights):
            raise ValueError("replay vectors must have equal lengths")
        if any(score < -1.0 or score > 3.0 for score in (*self.parent, *self.child)):
            raise ValueError("normalized scores must be within [-1, 3]")
        if any(weight <= 0.0 for weight in self.weights):
            raise ValueError("weights must be positive")
        if not all(np.isfinite(value) for value in (self.eps, self.noise_sd, self.max_task_drop)):
            raise ValueError("decision parameters must be finite")
        if self.eps < 0 or self.noise_sd < 0 or self.max_task_drop < 0:
            raise ValueError("decision bounds cannot be negative")
        if self.best_suite is not None and not -1.0 <= self.best_suite <= 3.0:
            raise ValueError("best_suite must be within [-1, 3]")
        return self
