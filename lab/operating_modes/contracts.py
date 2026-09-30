"""Versioned, bounded contracts for normal operating modes and range-based OMR."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Finite = Annotated[float, Field(allow_inf_nan=False)]
Method = Literal["lsh", "optics", "som"]
SCENARIOS = (
    "healthy_single",
    "healthy_multiple",
    "healthy_load",
    "step",
    "drift",
    "variance",
    "correlation_break",
    "oscillation_lag",
    "unseen_mode",
    "sensor_quality",
)
CONTRACT_VERSION: Literal["operating-modes.omr.v1"] = "operating-modes.omr.v1"


class StrictModel(BaseModel):
    """Immutable JSON-safe records; unknown configuration fields are errors."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, allow_inf_nan=False)


class ModeConfig(StrictModel):
    """All fitting, mode admission, neighbor, and alarm parameters are independent."""

    method: Method = "lsh"
    seed: int = Field(default=0, ge=0, le=2**32 - 1)
    min_support: int = Field(default=4, ge=2, le=512)
    tolerance_quantile: Finite = Field(default=0.99, gt=0, le=1)
    tolerance_multiplier: Finite = Field(default=1.25, gt=0, le=20)
    k: int = Field(default=3, ge=1, le=128)
    weighting: Literal["uniform", "distance"] = "distance"
    metric: Literal["euclidean", "manhattan"] = "euclidean"
    calibration: Literal["leave_one_out", "chronological"] = "leave_one_out"
    calibration_fraction: Finite = Field(default=0.2, gt=0, lt=0.5)
    alarm_quantile: Finite = Field(default=0.99, gt=0, le=1)
    release_ratio: Finite = Field(default=0.8, ge=0, le=1)
    dwell: int = Field(default=1, ge=1, le=1000)
    lsh_tables: int = Field(default=4, ge=1, le=8)
    lsh_projections: int = Field(default=3, ge=1, le=8)
    lsh_width: Finite = Field(default=0.35, gt=0, le=10)
    lsh_merge_tables: int = Field(default=2, ge=1, le=8)
    optics_min_samples: int = Field(default=8, ge=2, le=512)
    optics_max_eps: Finite = Field(default=2.0, gt=0, le=100)
    optics_xi: Finite = Field(default=0.05, gt=0, lt=1)
    optics_min_cluster_size: int = Field(default=8, ge=2, le=512)
    som_rows: int = Field(default=3, ge=1, le=12)
    som_columns: int = Field(default=3, ge=1, le=12)
    som_iterations: int = Field(default=400, ge=1, le=10000)
    som_learning_rate: Finite = Field(default=0.3, gt=0, le=1)
    som_sigma: Finite = Field(default=1.5, gt=0, le=20)

    @model_validator(mode="after")
    def coherent_lsh(self) -> ModeConfig:
        """A merge cannot require more independent tables than are fitted."""
        if self.lsh_merge_tables > self.lsh_tables:
            raise ValueError("lsh_merge_tables cannot exceed lsh_tables")
        return self


class AlarmState(StrictModel):
    """Explicit streaming state; a new evaluation must start with a fresh state."""

    active: bool = False
    pending: int = Field(default=0, ge=0)


class SensorResidual(StrictModel):
    sensor: str
    actual: Finite | None
    predicted: Finite | None
    signed_difference: Finite | None
    absolute_difference: Finite | None
    training_range: Finite
    relative_deviation: Finite | None
    contribution: Finite | None
    excluded_reason: str | None = None
    constant_changed: bool = False


class PredictionRow(StrictModel):
    index: int
    state: Literal["known", "out_of_mode", "invalid_input", "unavailable"]
    mode_id: int | None
    reference_mode_id: int | None
    mode_distance: Finite | None
    mode_tolerance: Finite | None
    omr_percent: Finite | None
    reason: str | None
    residuals: tuple[SensorResidual, ...]
    alarm: bool | None
    som_bmu: int | None = None
    som_distance: Finite | None = None


class PredictionBatch(StrictModel):
    contract_version: Literal["operating-modes.omr.v1"] = CONTRACT_VERSION
    model_sha256: str
    rows: tuple[PredictionRow, ...]
    alarm_state: AlarmState

    def to_dict(self) -> dict[str, object]:
        """Return strict JSON-compatible Python values, including explicit nulls."""
        return self.model_dump(mode="json")
