"""Numerical and data-quality contracts for the dataset preview."""

import json

import numpy as np
import pandas as pd
import pytest

from lab.analytics.statistics import summarize_dataset


def test_known_statistics_and_timed_trend_are_correct_without_mutation() -> None:
    frame = pd.DataFrame(
        {
            "time": pd.date_range("2026-01-01", periods=5, freq="2s", tz="UTC"),
            "x": [1.0, 2.0, 3.0, 4.0, 5.0],
            "y": [10.0, 8.0, 6.0, 4.0, 2.0],
        }
    )
    before = frame.copy(deep=True)
    result = summarize_dataset(frame, feature_columns=["x", "y"], timestamp_column="time")
    pd.testing.assert_frame_equal(frame, before)
    sensor = result.sensors[0]
    for key, expected in {
        "mean": 3,
        "median": 3,
        "variance": 2.5,
        "mad": 1,
        "iqr": 2,
        "trend_per_second": 0.5,
    }.items():
        assert sensor.statistics[key].value == pytest.approx(expected)
    assert sensor.statistics["std"].value == pytest.approx(np.sqrt(2.5))
    assert result.pairs[0].pearson.value == pytest.approx(-1)
    assert result.pairs[0].spearman.value == pytest.approx(-1)
    assert sensor.autocorrelation[1].value == pytest.approx(1)
    assert result.timeline.regular and result.timeline.interval_seconds.value == 2
    assert sum(sensor.histogram_counts) == 5
    json.dumps(result.model_dump(), allow_nan=False)


def test_missing_nonfinite_and_constant_inputs_have_explicit_reasons() -> None:
    frame = pd.DataFrame({"x": [1.0, np.nan, 3.0, np.inf, 5.0], "flat": [7.0] * 5})
    result = summarize_dataset(frame, feature_columns=["x", "flat"])
    sensor = result.sensors[0]
    assert (sensor.valid, sensor.missing, sensor.nonfinite) == (3, 1, 1)
    assert sensor.statistics["mean"].value == 3
    assert result.pairs[0].valid_pairs == 3
    assert result.pairs[0].pearson.reason == "constant_series"
    assert result.sensors[1].statistics["skewness"].reason == "constant_series"
    assert result.sensors[1].longest_constant_run == 5
    assert sensor.autocorrelation[1].reason == "timestamp_not_selected"
    json.dumps(result.model_dump(), allow_nan=False)


def test_irregular_timestamps_do_not_silently_produce_regular_acf() -> None:
    frame = pd.DataFrame(
        {
            "t": [
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:01Z",
                "2026-01-01T00:00:02Z",
                "2026-01-01T00:00:10Z",
            ],
            "x": [0.0, 1.0, 2.0, 10.0],
        }
    )
    result = summarize_dataset(frame, feature_columns=["x"], timestamp_column="t")
    assert not result.timeline.regular
    assert result.timeline.gap_count == 1
    assert result.timeline.maximum_gap_seconds.value == 8
    assert result.sensors[0].statistics["trend_per_second"].value == pytest.approx(1)
    assert result.sensors[0].autocorrelation[1].value is None


def test_bad_naive_duplicate_and_out_of_order_timestamps_are_reported() -> None:
    frame = pd.DataFrame(
        {
            "t": [
                "2026-01-01T00:00:02Z",
                "2026-01-01T00:00:02Z",
                "2026-01-01T00:00:01Z",
                "2026-01-01",
                "bad",
                None,
            ],
            "x": [1.0] * 6,
        }
    )
    timeline = summarize_dataset(frame, feature_columns=["x"], timestamp_column="t").timeline
    assert (timeline.duplicate_count, timeline.backward_steps) == (1, 1)
    assert (timeline.naive, timeline.invalid, timeline.missing) == (1, 1, 1)


def test_pairwise_counts_and_rank_ties_use_only_paired_values() -> None:
    frame = pd.DataFrame({"a": [1.0, 1.0, 2.0, 4.0, np.nan], "b": [4.0, 4.0, 3.0, 1.0, 100.0]})
    pair = summarize_dataset(frame, feature_columns=["a", "b"]).pairs[0]
    assert pair.valid_pairs == 4
    assert pair.pearson.value == pytest.approx(-1)
    assert pair.spearman.value == pytest.approx(-1)


def test_empty_nullable_and_tiny_samples_serialize_without_nan() -> None:
    for values in ([], [None], [1.0]):
        frame = pd.DataFrame({"x": pd.Series(values, dtype="Float64")})
        result = summarize_dataset(frame, feature_columns=["x"])
        assert result.sensors[0].statistics["std"].value is None
        json.dumps(result.model_dump(), allow_nan=False)


@pytest.mark.parametrize("features", [["missing"], ["x", "x"], [], ["text"], ["flag"], ["complex"]])
def test_rejects_ambiguous_or_nonnumeric_features(features: list[str]) -> None:
    frame = pd.DataFrame({"x": [1.0], "text": ["secret"], "flag": [True], "complex": [1 + 2j]})
    with pytest.raises(ValueError):
        summarize_dataset(frame, feature_columns=features)
