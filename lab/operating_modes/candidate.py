"""Adapter into the existing candidate fit/score and Scorer/Director boundary."""

from __future__ import annotations

from typing import cast

import numpy as np
import pandas as pd

from harness.contracts import AlarmPolicy, FitContext

from .contracts import ModeConfig
from .model import OperatingModeModel, fit_model


class OperatingModeCandidate:
    """Normal-reference residual candidate; unavailable scores remain NaN."""

    def __init__(self, config: ModeConfig):
        self.config = config
        self.model: OperatingModeModel | None = None

    def fit(self, train: pd.DataFrame, ctx: FitContext) -> None:
        # The proposal seed and trusted repetition seed both affect the fit.
        effective_seed = (self.config.seed + ctx.seed) % (2**32)
        self.model = fit_model(train, self.config.model_copy(update={"seed": effective_seed}))

    def score(self, data: pd.DataFrame) -> np.ndarray:
        if self.model is None:
            raise RuntimeError("candidate not fitted")
        return np.array(
            [
                row.omr_percent if row.omr_percent is not None else np.nan
                for row in self.model.predict(data).rows
            ],
            dtype=float,
        )

    def alarm_policy(self, train_scores: np.ndarray) -> AlarmPolicy:
        if self.model is None or self.model.threshold is None:
            raise ValueError("normal-reference alarm calibration unavailable")
        return AlarmPolicy(
            self.model.threshold,
            self.model.threshold * self.config.release_ratio,
            self.config.dwell,
        )

    def assign(self, data: pd.DataFrame) -> np.ndarray:
        if self.model is None:
            raise RuntimeError("candidate not fitted")
        return np.array(
            [
                row.mode_id if row.mode_id is not None else -1
                for row in self.model.predict(data).rows
            ]
        )

    def describe(self) -> list[dict[str, object]]:
        if self.model is None:
            raise RuntimeError("candidate not fitted")
        return cast(list[dict[str, object]], self.model.summary()["modes"])


def candidate_source(config: ModeConfig) -> bytes:
    """Deterministic source proposal; execution still requires normal sandbox gates."""
    return (
        "from lab.operating_modes import ModeConfig, OperatingModeCandidate\n\n"
        "def build_candidate():\n"
        "    return OperatingModeCandidate(ModeConfig.model_validate_json(\n"
        f"        {config.model_dump_json()!r}))\n"
    ).encode()
