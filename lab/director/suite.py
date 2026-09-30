"""Trusted, label-free suite task inputs held as immutable numeric snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

from harness.contracts import FitContext
from lab.director.baselines import TrustedCalibrationTask
from lab.director.contracts import SourceProvenance


@dataclass(frozen=True, slots=True)
class SuiteTask:
    """A single feature-only task; labels remain exclusively in Scorer storage."""

    task_id: str
    dataset_id: str
    split_id: str
    session_id: str
    profile_sha256: str
    family: Literal["EVT", "PDM", "NRM"]
    task_weight: float
    independent_family: str
    label_tier: Literal["gold", "silver", "bronze"]
    provenance: SourceProvenance
    context: FitContext
    _columns: tuple[str, ...]
    _train: np.ndarray
    _evaluation: np.ndarray
    task_ids_for_guard: frozenset[str]
    evaluation_instants: tuple[str | int | float, ...]

    @classmethod
    def from_frames(
        cls,
        *,
        task_id: str,
        dataset_id: str,
        split_id: str,
        session_id: str,
        profile_sha256: str,
        family: Literal["EVT", "PDM", "NRM"],
        task_weight: float,
        independent_family: str | None = None,
        label_tier: Literal["gold", "silver", "bronze"] = "gold",
        provenance: SourceProvenance,
        context: FitContext,
        train: pd.DataFrame,
        evaluation: pd.DataFrame,
        task_ids_for_guard: frozenset[str] = frozenset(),
        evaluation_instants: tuple[str | int | float, ...] = (),
    ) -> SuiteTask:
        """Copy validated finite sensor matrices; reject label columns and metadata."""
        if train.empty or evaluation.empty:
            raise ValueError("suite train and evaluation features must be non-empty")
        columns = tuple(str(column) for column in train.columns)
        if (
            not columns
            or len(columns) != len(set(columns))
            or columns != tuple(str(column) for column in evaluation.columns)
            or columns != context.signals
        ):
            raise ValueError("suite feature columns must exactly match trusted sensor signals")
        forbidden = {"label", "anomaly", "is_anomaly", "changepoint", "datetime", "timestamp"}
        if any(column.lower() in forbidden for column in columns):
            raise ValueError("suite feature matrix contains a label or time metadata column")
        if not np.isfinite(task_weight) or task_weight <= 0:
            raise ValueError("suite task weight must be positive and finite")
        if context.sampling_s is None:
            if family != "EVT":
                raise ValueError("unknown physical cadence is supported only for EVT tasks")
        elif (
            isinstance(context.sampling_s, bool)
            or not isinstance(context.sampling_s, int)
            or context.sampling_s < 1
        ):
            raise ValueError("known sampling_s must be a positive integer")
        train_values = train.to_numpy(dtype=np.float64, copy=True)
        eval_values = evaluation.to_numpy(dtype=np.float64, copy=True)
        if (
            train_values.ndim != 2
            or eval_values.ndim != 2
            or not np.isfinite(train_values).all()
            or not np.isfinite(eval_values).all()
        ):
            raise ValueError("suite task values must be finite two-dimensional sensor matrices")
        if train_values.nbytes + eval_values.nbytes > 128 * 1024**2:
            raise ValueError("one trusted suite task exceeds the 128 MiB input bound")
        train_values.setflags(write=False)
        eval_values.setflags(write=False)
        return cls(
            task_id=task_id,
            dataset_id=dataset_id,
            split_id=split_id,
            session_id=session_id,
            profile_sha256=profile_sha256,
            family=family,
            task_weight=task_weight,
            independent_family=(
                independent_family
                if independent_family is not None
                else f"{dataset_id}:unclassified:{family}"
            ),
            label_tier=label_tier,
            provenance=provenance,
            context=context,
            _columns=columns,
            _train=train_values,
            _evaluation=eval_values,
            task_ids_for_guard=task_ids_for_guard,
            evaluation_instants=evaluation_instants,
        )

    @property
    def calibration_identity(self) -> TrustedCalibrationTask:
        """Return the trusted identity and normalization weight for calibration."""
        return TrustedCalibrationTask(
            task_id=self.task_id,
            dataset_id=self.dataset_id,
            split_id=self.split_id,
            session_id=self.session_id,
            profile_sha256=self.profile_sha256,
            family=self.family,
            weight=self.task_weight,
        )

    def frames(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Rebuild fresh frames so candidate execution cannot mutate trusted storage."""
        return (
            pd.DataFrame(self._train.copy(), columns=self._columns),
            pd.DataFrame(self._evaluation.copy(), columns=self._columns),
        )
