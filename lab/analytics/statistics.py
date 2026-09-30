"""Bounded descriptive statistics; never fit a model or fill missing observations."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

import numpy as np
import pandas as pd
from numpy.typing import NDArray
from pandas.api.types import is_bool_dtype, is_complex_dtype, is_numeric_dtype
from pydantic import BaseModel, ConfigDict


class Statistic(BaseModel):
    """A finite measurement, or an explicit reason why it is unavailable."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    value: float | None
    reason: str | None = None


class SensorSummary(BaseModel):
    """Statistics for one explicitly selected numeric sensor."""

    name: str
    rows: int
    valid: int
    missing: int
    nonfinite: int
    missing_fraction: float
    longest_constant_run: int
    statistics: dict[str, Statistic]
    histogram_counts: list[int]
    histogram_edges: list[float]
    histogram_reason: str | None = None
    autocorrelation: dict[int, Statistic]


class PairSummary(BaseModel):
    """Pairwise complete observations; no imputation or causal claim."""

    first: str
    second: str
    valid_pairs: int
    pearson: Statistic
    spearman: Statistic


class TimeSummary(BaseModel):
    """UTC timeline quality, retained in the original input order."""

    column: str | None
    valid: int
    missing: int
    invalid: int
    naive: int
    duplicate_count: int
    backward_steps: int
    regular: bool
    interval_seconds: Statistic
    maximum_gap_seconds: Statistic
    gap_count: int
    reason: str | None


class DatasetSummary(BaseModel):
    """JSON-safe report shared by the dataset preview and experiment UI."""

    schema_version: Literal["descriptive-statistics.v1"] = "descriptive-statistics.v1"
    rows: int
    sensors: list[SensorSummary]
    pairs: list[PairSummary]
    timeline: TimeSummary
    notes: tuple[str, ...] = (
        "Descriptive only; no model fitting, interpolation or missing-value imputation.",
        "Sample standard deviation/variance use ddof=1; kurtosis is unbiased excess kurtosis.",
        "Correlations use pairwise finite values and do not establish causality.",
        "Autocorrelation requires an ordered regular UTC grid; missing sensor pairs stay missing.",
    )


def _number(value: float, reason: str = "numerical_overflow") -> Statistic:
    return (
        Statistic(value=float(value))
        if np.isfinite(value)
        else Statistic(value=None, reason=reason)
    )


def _unavailable(reason: str) -> Statistic:
    return Statistic(value=None, reason=reason)


def _timeline(
    frame: pd.DataFrame,
    column: str | None,
) -> tuple[TimeSummary, NDArray[np.float64]]:
    seconds = np.full(len(frame), np.nan)
    missing = invalid = naive = 0
    if column is not None:
        origin: pd.Timestamp | None = None
        for offset, raw in enumerate(frame[column]):
            if pd.isna(raw):
                missing += 1
                continue
            try:
                stamp = pd.Timestamp(raw)
                if stamp.tzinfo is None:
                    naive += 1
                    continue
                stamp = stamp.tz_convert("UTC")
                origin = stamp if origin is None else origin
                seconds[offset] = (stamp - origin).total_seconds()
            except (TypeError, ValueError, OverflowError):
                invalid += 1
    valid = seconds[np.isfinite(seconds)]
    delta = np.diff(valid)
    positive = delta[delta > 0]
    duplicate = len(valid) - len(np.unique(valid))
    backward = int(np.count_nonzero(delta < 0))
    median = float(np.median(positive)) if len(positive) else np.nan
    regular = bool(
        len(valid) == len(frame)
        and len(valid) >= 3
        and not duplicate
        and not backward
        and np.allclose(delta, median, rtol=1e-9, atol=1e-6)
    )
    reason = None
    if not regular:
        reason = "timestamp_not_selected" if column is None else "irregular_or_invalid_timeline"
    return TimeSummary(
        column=column,
        valid=len(valid),
        missing=missing,
        invalid=invalid,
        naive=naive,
        duplicate_count=duplicate,
        backward_steps=backward,
        regular=regular,
        interval_seconds=_number(median, "insufficient_positive_intervals"),
        maximum_gap_seconds=_number(
            float(np.max(positive)) if len(positive) else np.nan,
            "insufficient_positive_intervals",
        ),
        gap_count=int(np.count_nonzero(positive > 1.5 * median)),
        reason=reason,
    ), seconds


def _correlation(first: NDArray[np.float64], second: NDArray[np.float64]) -> Statistic:
    if len(first) < 3:
        return _unavailable("fewer_than_three_pairs")
    if np.ptp(first) == 0 or np.ptp(second) == 0:
        return _unavailable("constant_series")
    with np.errstate(all="ignore"):
        return _number(float(np.corrcoef(first, second)[0, 1]))


def _longest_run(values: NDArray[np.float64]) -> int:
    longest = current = 0
    previous = np.nan
    for value in values:
        if not np.isfinite(value):
            current = 0
        else:
            current = current + 1 if value == previous else 1
            longest = max(longest, current)
        previous = value
    return longest


def _distribution(values: NDArray[np.float64]) -> dict[str, Statistic]:
    names = (
        "minimum",
        "maximum",
        "mean",
        "median",
        "std",
        "variance",
        "mad",
        "iqr",
        "p01",
        "p05",
        "p25",
        "p75",
        "p95",
        "p99",
        "skewness",
        "excess_kurtosis",
        "tukey_outlier_count",
    )
    if not len(values):
        return {name: _unavailable("no_finite_observations") for name in names}
    series = pd.Series(values)
    with np.errstate(all="ignore"):
        quantiles = np.quantile(values, [0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99])
        q01, q05, q25, median, q75, q95, q99 = quantiles
        iqr = q75 - q25
        results = {
            "minimum": float(values.min()),
            "maximum": float(values.max()),
            "mean": float(values.mean()),
            "median": float(median),
            "mad": float(np.median(np.abs(values - median))),
            "iqr": float(iqr),
            "p01": float(q01),
            "p05": float(q05),
            "p25": float(q25),
            "p75": float(q75),
            "p95": float(q95),
            "p99": float(q99),
            "tukey_outlier_count": float(
                np.count_nonzero(
                    (values < q25 - 1.5 * iqr) | (values > q75 + 1.5 * iqr),
                )
            ),
        }
        stats = {key: _number(value) for key, value in results.items()}
        for key, method in (("std", series.std), ("variance", series.var)):
            stats[key] = (
                _number(float(method(ddof=1)))
                if len(values) >= 2
                else _unavailable("fewer_than_two_observations")
            )
        for key, minimum, shape_method in (
            ("skewness", 3, series.skew),
            ("excess_kurtosis", 4, series.kurt),
        ):
            if len(values) < minimum:
                stats[key] = _unavailable(f"fewer_than_{minimum}_observations")
            elif np.ptp(values) == 0:
                stats[key] = _unavailable("constant_series")
            else:
                stats[key] = _number(float(np.asarray(shape_method(), dtype=np.float64)))
    return stats


def _trend(values: NDArray[np.float64], seconds: NDArray[np.float64]) -> Statistic:
    keep = np.isfinite(values) & np.isfinite(seconds)
    x_values, y_values = seconds[keep], values[keep]
    if len(x_values) < 3 or np.ptp(x_values) == 0:
        return _unavailable("insufficient_timed_observations")
    with np.errstate(all="ignore"):
        centered = x_values - x_values.mean()
        slope = np.dot(centered, y_values - y_values.mean()) / np.dot(centered, centered)
    return _number(float(slope))


def summarize_dataset(
    frame: pd.DataFrame,
    *,
    feature_columns: Sequence[str],
    timestamp_column: str | None = None,
    max_lag: int = 12,
    histogram_bins: int = 12,
) -> DatasetSummary:
    """Summarize explicitly selected sensors without changing the input data.

    Call separately for each period/asset/mode; this function never selects labels
    or uses another split. Bounds cap preview work, not the underlying dataset.
    """
    features = list(feature_columns)
    if (
        not features
        or len(features) > 50
        or not all(isinstance(name, str) for name in features)
        or len(features) != len(set(features))
    ):
        raise ValueError("select 1 to 50 unique sensor columns")
    if len(frame) > 100_000 or frame.columns.has_duplicates:
        raise ValueError("preview requires at most 100000 rows and unique columns")
    if not 0 <= max_lag <= 48 or not 1 <= histogram_bins <= 100:
        raise ValueError("invalid preview lag or histogram bound")
    if timestamp_column is not None and (
        timestamp_column not in frame.columns or timestamp_column in features
    ):
        raise ValueError("timestamp must be a separate existing column")
    for name in features:
        if (
            name not in frame
            or not is_numeric_dtype(frame[name])
            or is_bool_dtype(frame[name])
            or is_complex_dtype(frame[name])
        ):
            raise ValueError("selected sensors must be real numeric columns, excluding booleans")
    timeline, seconds = _timeline(frame, timestamp_column)
    arrays = {
        name: frame[name].to_numpy(dtype=np.float64, na_value=np.nan, copy=True)
        for name in features
    }
    summaries: list[SensorSummary] = []
    for name, array in arrays.items():
        valid = array[np.isfinite(array)]
        stats = _distribution(valid)
        stats["trend_per_second"] = _trend(array, seconds)
        counts: NDArray[np.int64] = np.array([], dtype=np.int64)
        edges: NDArray[np.float64] = np.array([], dtype=np.float64)
        histogram_reason = "no_finite_observations" if not len(valid) else None
        if len(valid):
            with np.errstate(all="ignore"):
                try:
                    counts, edges = np.histogram(valid, bins=histogram_bins)
                    if not np.isfinite(edges).all():
                        raise ValueError("histogram edge overflow")
                except (ValueError, IndexError, OverflowError):
                    counts = np.array([], dtype=np.int64)
                    edges = np.array([], dtype=np.float64)
                    histogram_reason = "numerical_overflow"
        autocorrelation = {}
        for lag in range(1, max_lag + 1):
            if not timeline.regular:
                autocorrelation[lag] = _unavailable(timeline.reason or "irregular_timeline")
            elif lag >= len(array):
                autocorrelation[lag] = _unavailable("insufficient_lagged_observations")
            else:
                first, second = array[:-lag], array[lag:]
                keep = np.isfinite(first) & np.isfinite(second)
                autocorrelation[lag] = _correlation(first[keep], second[keep])
        missing = int(np.count_nonzero(np.isnan(array)))
        summaries.append(
            SensorSummary(
                name=name,
                rows=len(array),
                valid=len(valid),
                missing=missing,
                nonfinite=int(np.count_nonzero(np.isinf(array))),
                missing_fraction=missing / len(array) if len(array) else 0.0,
                longest_constant_run=_longest_run(array),
                statistics=stats,
                histogram_counts=counts.tolist(),
                histogram_edges=edges.tolist(),
                histogram_reason=histogram_reason,
                autocorrelation=autocorrelation,
            )
        )
    pairs: list[PairSummary] = []
    for index, name in enumerate(features):
        for other in features[index + 1 :]:
            first, second = arrays[name], arrays[other]
            keep = np.isfinite(first) & np.isfinite(second)
            first, second = first[keep], second[keep]
            pairs.append(
                PairSummary(
                    first=name,
                    second=other,
                    valid_pairs=len(first),
                    pearson=_correlation(first, second),
                    spearman=_correlation(
                        np.asarray(pd.Series(first).rank(), dtype=np.float64),
                        np.asarray(pd.Series(second).rank(), dtype=np.float64),
                    ),
                )
            )
    return DatasetSummary(rows=len(frame), sensors=summaries, pairs=pairs, timeline=timeline)
