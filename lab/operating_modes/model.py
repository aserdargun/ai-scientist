"""Immutable training-only modes, deterministic neighbors, and continuous OMR."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields, replace
from itertools import combinations
from typing import Any, Literal

import numpy as np
import pandas as pd
from sklearn.cluster import OPTICS

from .contracts import AlarmState, ModeConfig, PredictionBatch, PredictionRow, SensorResidual

MAX_ROWS = 4096
MAX_SENSORS = 64


def _distance(points: np.ndarray, point: np.ndarray, metric: str) -> np.ndarray:
    delta = np.abs(points - point)
    return np.sqrt(np.sum(delta * delta, axis=1)) if metric == "euclidean" else delta.sum(axis=1)


def _frame(frame: pd.DataFrame, sensors: tuple[str, ...] | None = None) -> np.ndarray:
    if len(frame) > MAX_ROWS or not 1 <= len(frame.columns) <= MAX_SENSORS:
        raise ValueError("frame exceeds 4096 rows or 1..64 sensors")
    if not frame.columns.is_unique or not all(isinstance(c, str) for c in frame.columns):
        raise ValueError("sensor names must be unique strings")
    if sensors is not None and tuple(frame.columns) != sensors:
        raise ValueError("prediction sensor order must match training")
    return frame.to_numpy(dtype=np.float64, copy=True)


def _lsh(data: np.ndarray, config: ModeConfig) -> tuple[np.ndarray, dict[str, Any]]:
    """Union points sharing a bucket in any required combination of tables.

    Group by combined signatures rather than materializing all pairwise edges.
    This exactly implements collision-count connected components with O(n) memory.
    """
    rng = np.random.default_rng(config.seed)
    signatures = []
    for _ in range(config.lsh_tables):
        projection = rng.normal(size=(data.shape[1], config.lsh_projections))
        offset = rng.uniform(0, config.lsh_width, size=config.lsh_projections)
        signatures.append(
            np.floor((data @ projection + offset) / config.lsh_width).astype(np.int64)
        )
    parent = list(range(len(data)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for tables in combinations(range(config.lsh_tables), config.lsh_merge_tables):
        first: dict[tuple[int, ...], int] = {}
        for index in range(len(data)):
            key = tuple(int(v) for table in tables for v in signatures[table][index])
            previous = first.setdefault(key, index)
            left, right = root(previous), root(index)
            parent[max(left, right)] = min(left, right)
    labels = np.array([root(i) for i in range(len(data))])
    return labels, {"semantics": "collision-count-connected-components.v1"}


def _som(data: np.ndarray, config: ModeConfig) -> tuple[np.ndarray, dict[str, Any]]:
    rng = np.random.default_rng(config.seed)
    grid = np.array([(r, c) for r in range(config.som_rows) for c in range(config.som_columns)])
    weights = data[rng.integers(len(data), size=len(grid))].copy()
    history = []
    for iteration in range(config.som_iterations):
        point = data[int(rng.integers(len(data)))]
        bmu = int(np.argmin(_distance(weights, point, config.metric)))
        fraction = 1 - iteration / config.som_iterations
        sigma = max(config.som_sigma * fraction, 0.05)
        influence = np.exp(-np.sum((grid - grid[bmu]) ** 2, axis=1) / (2 * sigma**2))
        weights += config.som_learning_rate * fraction * influence[:, None] * (point - weights)
        if (
            iteration % max(1, config.som_iterations // 20) == 0
            or iteration == config.som_iterations - 1
        ):
            errors = [float(np.min(_distance(weights, row, config.metric))) for row in data]
            history.append(
                {"iteration": iteration + 1, "quantization_error": float(np.mean(errors))}
            )
    labels, distances, topology = [], [], []
    for row in data:
        dist = _distance(weights, row, config.metric)
        order = np.argsort(dist, kind="stable")
        labels.append(int(order[0]))
        distances.append(float(dist[order[0]]))
        if len(order) > 1:
            topology.append(float(np.abs(grid[order[0]] - grid[order[1]]).sum() != 1))
    return np.array(labels), {
        "semantics": "occupied-unit-modes.v1",
        "grid": grid.tolist(),
        "weights": weights.tolist(),
        "occupancy": np.bincount(labels, minlength=len(grid)).tolist(),
        "quantization_error": float(np.mean(distances)),
        "topological_error": float(np.mean(topology)) if topology else None,
        "history": history,
    }


@dataclass(frozen=True, slots=True)
class OperatingModeModel:
    """Tuple-backed fitted state; prediction cannot update training or calibration."""

    config: ModeConfig
    sensors: tuple[str, ...]
    references: tuple[tuple[float, ...], ...]
    minimum: tuple[float, ...]
    ranges: tuple[float, ...]
    labels: tuple[int, ...]
    centers: tuple[tuple[float, ...], ...]
    tolerances: tuple[float, ...]
    diagnostics_json: str
    threshold: float | None
    calibration_count: int
    training_rows: int
    dropped_rows: int

    @property
    def model_sha256(self) -> str:
        return hashlib.sha256(self._serialized().encode()).hexdigest()

    def _serialized(self) -> str:
        return json.dumps(
            {
                **{field.name: getattr(self, field.name) for field in fields(self)},
                "config": self.config.model_dump(),
            },
            sort_keys=True,
            allow_nan=False,
        )

    def summary(self) -> dict[str, Any]:
        """JSON-safe model provenance and separate mode/SOM diagnostics."""
        return {
            "contract_version": "operating-modes.omr.v1",
            "model_sha256": self.model_sha256,
            "config": self.config.model_dump(mode="json"),
            "sensors": list(self.sensors),
            "training_rows": self.training_rows,
            "reference_rows": len(self.references),
            "dropped_rows": self.dropped_rows,
            "training_minimum": list(self.minimum),
            "training_ranges": list(self.ranges),
            "excluded_sensors": [
                s for s, r in zip(self.sensors, self.ranges, strict=True) if r == 0
            ],
            "noise_rows": self.labels.count(-1),
            "alarm_threshold": self.threshold,
            "alarm_release": self.threshold * self.config.release_ratio
            if self.threshold is not None
            else None,
            "calibration_count": self.calibration_count,
            "modes": [
                {
                    "mode_id": i,
                    "support": self.labels.count(i),
                    "center": list(center),
                    "tolerance": self.tolerances[i],
                }
                for i, center in enumerate(self.centers)
            ],
            "diagnostics": json.loads(self.diagnostics_json),
        }

    def _row(self, point: np.ndarray, index: int, exclude: int | None = None) -> PredictionRow:
        ranges, minimum = np.array(self.ranges), np.array(self.minimum)
        active = ranges > 0
        finite = np.isfinite(point).all()
        reason = None if finite else "non_finite_input"
        mode = reference_mode = None
        distance = tolerance = omr = None
        bmu = som_distance = None
        predicted = np.full(len(point), np.nan)
        state: Literal["known", "out_of_mode", "invalid_input", "unavailable"] = (
            "invalid_input" if not finite else "unavailable"
        )
        if finite and active.any() and self.centers:
            normalized = (point[active] - minimum[active]) / ranges[active]
            centers = (np.array(self.centers)[:, active] - minimum[active]) / ranges[active]
            distances = _distance(centers, normalized, self.config.metric)
            accepted = np.flatnonzero(distances <= np.array(self.tolerances))
            reference_mode = (
                int(accepted[np.argmin(distances[accepted])])
                if len(accepted)
                else int(np.argmin(distances))
            )
            mode = reference_mode if len(accepted) else None
            distance, tolerance = float(distances[reference_mode]), self.tolerances[reference_mode]
            state = "known" if mode is not None else "out_of_mode"
            references = np.array(self.references)
            eligible = np.flatnonzero(np.array(self.labels) == reference_mode)
            if exclude is not None:
                eligible = eligible[eligible != exclude]
            if len(eligible):
                relative_offsets = (references[eligible][:, active] - point[active]) / ranges[
                    active
                ]
                neighbor_dist = _distance(
                    relative_offsets, np.zeros(int(active.sum())), self.config.metric
                )
                order = np.argsort(neighbor_dist, kind="stable")[: self.config.k]
                selected, distances_k = references[eligible[order]], neighbor_dist[order]
                if self.config.weighting == "uniform":
                    predicted = selected.mean(axis=0)
                elif np.any(distances_k == 0):
                    predicted = selected[distances_k == 0].mean(axis=0)
                else:
                    weights = distances_k.min() / distances_k
                    predicted = np.average(selected, axis=0, weights=weights)
                relative = np.abs(point[active] - predicted[active]) / ranges[active]
                omr = float(100 * np.sqrt(np.mean(relative**2)))
            else:
                reason = "no_reference_after_exclusion"
            if self.config.method == "som":
                weights = np.array(json.loads(self.diagnostics_json)["weights"])
                distances_som = _distance(weights, normalized, self.config.metric)
                bmu = int(np.argmin(distances_som))
                som_distance = float(distances_som[bmu])
        elif finite:
            reason = "all_sensors_constant" if not active.any() else "no_supported_modes"
        residuals = []
        squares = np.zeros(len(point))
        usable = active & np.isfinite(point) & np.isfinite(predicted)
        squares[usable] = ((point[usable] - predicted[usable]) / ranges[usable]) ** 2
        total = float(squares.sum())
        for j, sensor in enumerate(self.sensors):
            actual = float(point[j]) if np.isfinite(point[j]) else None
            estimate = float(predicted[j]) if np.isfinite(predicted[j]) else None
            difference = actual - estimate if actual is not None and estimate is not None else None
            residuals.append(
                SensorResidual(
                    sensor=sensor,
                    actual=actual,
                    predicted=estimate,
                    signed_difference=difference,
                    absolute_difference=abs(difference) if difference is not None else None,
                    training_range=float(ranges[j]),
                    relative_deviation=float(np.sqrt(squares[j])) if usable[j] else None,
                    contribution=float(squares[j] / total)
                    if usable[j] and total
                    else (0.0 if usable[j] else None),
                    excluded_reason="zero_training_range"
                    if not active[j]
                    else ("non_finite_input" if actual is None else None),
                    constant_changed=bool(
                        not active[j] and actual is not None and actual != minimum[j]
                    ),
                )
            )
        return PredictionRow(
            index=index,
            state=state,
            mode_id=mode,
            reference_mode_id=reference_mode,
            mode_distance=distance,
            mode_tolerance=tolerance,
            omr_percent=omr,
            reason=reason,
            residuals=tuple(residuals),
            alarm=None,
            som_bmu=bmu,
            som_distance=som_distance,
        )

    def predict(
        self, frame: pd.DataFrame, *, alarm_state: AlarmState | None = None, row_offset: int = 0
    ) -> PredictionBatch:
        """Causal rows; pass returned alarm_state and row_offset between chunks."""
        data = _frame(frame, self.sensors)
        state = alarm_state or AlarmState()
        rows = []
        for i, point in enumerate(data):
            row = self._row(point, row_offset + i)
            alarm = None
            if row.omr_percent is None or self.threshold is None:
                state = AlarmState(active=state.active, pending=0)
            elif state.active:
                state = AlarmState(
                    active=row.omr_percent > self.threshold * self.config.release_ratio
                )
                alarm = state.active
            else:
                pending = state.pending + 1 if row.omr_percent > self.threshold else 0
                state = AlarmState(
                    active=pending >= self.config.dwell,
                    pending=0 if pending >= self.config.dwell else pending,
                )
                alarm = state.active
            rows.append(row.model_copy(update={"alarm": alarm}))
        return PredictionBatch(model_sha256=self.model_sha256, rows=tuple(rows), alarm_state=state)


def fit_model(train: pd.DataFrame, config: ModeConfig | None = None) -> OperatingModeModel:
    """Fit healthy references only; no labels or evaluation rows are accepted."""
    config = config or ModeConfig()
    raw = _frame(train)
    split = (
        int(len(raw) * (1 - config.calibration_fraction))
        if config.calibration == "chronological"
        else len(raw)
    )
    prefix = raw[:split]
    finite = np.isfinite(prefix).all(axis=1)
    data = prefix[finite]
    if len(data) < 2:
        raise ValueError("at least two finite training rows are required")
    minimum, ranges = data.min(axis=0), np.ptp(data, axis=0)
    if not np.isfinite(ranges).all():
        raise ValueError("training range exceeds finite arithmetic")
    active = ranges > 0
    normalized = (data[:, active] - minimum[active]) / ranges[active]
    diagnostics: dict[str, Any] = {}
    if not active.any():
        labels = np.full(len(data), -1)
    elif config.method == "lsh":
        labels, diagnostics = _lsh(normalized, config)
    elif config.method == "som":
        labels, diagnostics = _som(normalized, config)
    elif len(data) < max(config.optics_min_samples, config.optics_min_cluster_size):
        labels = np.full(len(data), -1)
    else:
        optics = OPTICS(
            min_samples=config.optics_min_samples,
            max_eps=config.optics_max_eps,
            xi=config.optics_xi,
            min_cluster_size=config.optics_min_cluster_size,
            metric=config.metric,
            n_jobs=1,
        )
        labels = optics.fit_predict(normalized)
        diagnostics = {
            "semantics": "optics-xi-training-clusters.v1",
            "ordering": optics.ordering_.tolist(),
            "reachability": [float(v) if np.isfinite(v) else None for v in optics.reachability_],
        }
    valid = [
        int(label)
        for label in sorted(set(labels))
        if label >= 0 and np.sum(labels == label) >= config.min_support
    ]
    mapped = np.full(len(data), -1)
    centers, tolerances = [], []
    for mode, label in enumerate(valid):
        members = data[labels == label]
        center = members.mean(axis=0)
        distances = _distance(
            (members[:, active] - minimum[active]) / ranges[active],
            (center[active] - minimum[active]) / ranges[active],
            config.metric,
        )
        mapped[labels == label] = mode
        centers.append(tuple(float(v) for v in center))
        tolerances.append(
            float(np.quantile(distances, config.tolerance_quantile) * config.tolerance_multiplier)
        )
    diagnostics["original_mode_labels"] = valid
    model = OperatingModeModel(
        config=config,
        sensors=tuple(train.columns),
        references=tuple(tuple(float(v) for v in row) for row in data),
        minimum=tuple(float(v) for v in minimum),
        ranges=tuple(float(v) for v in ranges),
        labels=tuple(int(v) for v in mapped),
        centers=tuple(centers),
        tolerances=tuple(tolerances),
        diagnostics_json=json.dumps(diagnostics, sort_keys=True, allow_nan=False),
        threshold=None,
        calibration_count=0,
        training_rows=len(raw),
        dropped_rows=int((~finite).sum()),
    )
    calibration = raw[split:] if config.calibration == "chronological" else data
    scores = [
        model._row(row, i, exclude=i if config.calibration == "leave_one_out" else None).omr_percent
        for i, row in enumerate(calibration)
    ]
    valid_scores = [score for score in scores if score is not None]
    return replace(
        model,
        threshold=float(np.quantile(valid_scores, config.alarm_quantile)) if valid_scores else None,
        calibration_count=len(valid_scores),
    )
