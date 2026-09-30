"""Open, versioned operating modes and training-range residuals; no product parity claim."""

from .candidate import OperatingModeCandidate, candidate_source
from .contracts import SCENARIOS, AlarmState, ModeConfig, PredictionBatch
from .model import OperatingModeModel, fit_model
from .snapshots import (
    SourceSelection,
    SourceSnapshot,
    SyntheticCase,
    read_postgres_snapshot,
    snapshot_from_frame,
    synthetic_snapshot,
)

__all__ = [
    "SCENARIOS",
    "AlarmState",
    "ModeConfig",
    "PredictionBatch",
    "OperatingModeModel",
    "OperatingModeCandidate",
    "candidate_source",
    "fit_model",
    "SourceSelection",
    "SourceSnapshot",
    "SyntheticCase",
    "read_postgres_snapshot",
    "snapshot_from_frame",
    "synthetic_snapshot",
]
