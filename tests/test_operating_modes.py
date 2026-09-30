"""Numerical contracts and label-free synthetic operating-mode acceptance fixtures."""

import json
from dataclasses import FrozenInstanceError

import numpy as np
import pandas as pd
import pytest

from harness.contracts import FitContext
from lab.operating_modes import (
    SCENARIOS,
    ModeConfig,
    SourceSelection,
    candidate_source,
    fit_model,
    snapshot_from_frame,
    synthetic_snapshot,
)


def config(method="lsh", **kwargs):
    return ModeConfig(
        method=method,
        min_support=2,
        lsh_width=10.0,
        som_rows=2,
        som_columns=2,
        som_iterations=60,
        **kwargs,
    )


def test_exact_range_rms_contributions_and_no_clipping():
    train = pd.DataFrame(
        {"a": [0.0, 2.0, 4.0, 6.0], "b": [0.0, 4.0, 8.0, 12.0], "constant": [7.0] * 4}
    )
    model = fit_model(train, config(k=1))
    row = model.predict(pd.DataFrame({"a": [18.0], "b": [24.0], "constant": [9.0]})).rows[0]
    assert row.state == "out_of_mode"
    assert row.mode_id is None and row.reference_mode_id == 0
    assert row.omr_percent == pytest.approx(100 * np.sqrt((2**2 + 1**2) / 2))
    assert [r.contribution for r in row.residuals] == [0.8, 0.2, None]
    assert row.residuals[2].constant_changed
    assert row.residuals[2].excluded_reason == "zero_training_range"
    assert model.threshold > 0  # excludes actual reference row during calibration
    json.dumps(model.summary(), allow_nan=False)
    with pytest.raises(FrozenInstanceError):
        model.threshold = 0


def test_zero_residual_and_all_constant():
    data = pd.DataFrame({"a": [0.0, 1.0, 2.0, 3.0]})
    row = fit_model(data, config()).predict(data.iloc[:1]).rows[0]
    assert row.omr_percent == 0 and row.residuals[0].contribution == 0
    constants = pd.DataFrame({"a": [4.0] * 4})
    row = fit_model(constants, config()).predict(constants.iloc[:1]).rows[0]
    assert row.omr_percent is None and row.reason == "all_sensors_constant"


@pytest.mark.parametrize("method", ["lsh", "optics", "som"])
def test_frozen_train_only_and_stream_invariance(method):
    case = synthetic_snapshot("step", 3, train_rows=64, evaluation_rows=32)
    model = fit_model(case.snapshot.frame("train"), config(method, dwell=3))
    data = case.snapshot.frame("evaluation")
    before = model.model_sha256
    batch = model.predict(data)
    rows, state = [], None
    for start in range(0, len(data), 7):
        result = model.predict(data.iloc[start : start + 7], alarm_state=state, row_offset=start)
        rows.extend(result.rows)
        state = result.alarm_state
    assert tuple(rows) == batch.rows
    assert state == batch.alarm_state
    assert model.model_sha256 == before
    assert fit_model(case.snapshot.frame("train"), config(method, dwell=3)).model_sha256 == before
    if method == "som":
        diagnostics = model.summary()["diagnostics"]
        assert len(diagnostics["occupancy"]) == 4
        assert diagnostics["history"] and "topological_error" in diagnostics
    json.dumps(batch.to_dict(), allow_nan=False)


def test_chronological_calibration_does_not_change_scale():
    train = pd.DataFrame({"a": [0.0, 1.0, 2.0, 3.0, 100.0]})
    model = fit_model(train, config(calibration="chronological", calibration_fraction=0.2))
    assert model.ranges == (3.0,)
    assert model.calibration_count == 1
    assert model.threshold > 100


def test_unknown_noise_missing_order_and_bounds():
    train = pd.DataFrame({"a": [0.0, 1.0, 2.0, 3.0]})
    model = fit_model(train, config("optics", optics_min_samples=8))
    row = model.predict(train.iloc[:1]).rows[0]
    assert row.mode_id is None and row.omr_percent is None and row.reason == "no_supported_modes"
    model = fit_model(train, config())
    row = model.predict(pd.DataFrame({"a": [np.nan]})).rows[0]
    assert row.state == "invalid_input" and row.alarm is None
    with pytest.raises(ValueError):
        model.predict(pd.DataFrame({"different": [0.0]}))
    with pytest.raises(ValueError):
        fit_model(pd.DataFrame({"a": np.zeros(4097)}))


def test_snapshots_are_reproducible_utc_hashed_and_labels_separate():
    case = synthetic_snapshot("sensor_quality", 5)
    assert case.snapshot == synthetic_snapshot("sensor_quality", 5).snapshot
    assert case.snapshot.sha256 != synthetic_snapshot("sensor_quality", 6).snapshot.sha256
    assert not any(case.event_labels) and any(case.quality_labels)
    assert "labels" not in json.dumps(case.snapshot.to_dict())
    with pytest.raises(ValueError, match="UTC"):
        snapshot_from_frame(
            pd.DataFrame({"x": [1.0, 2.0, 3.0]}),
            pd.date_range("2024", periods=3),
            SourceSelection(source_id="test"),
            train_rows=2,
        )


def test_candidate_adapter_uses_calibration_and_normal_contract():
    namespace = {}
    exec(compile(candidate_source(config()), "candidate.py", "exec"), namespace)
    candidate = namespace["build_candidate"]()
    train = pd.DataFrame({"a": [0.0, 1.0, 2.0, 3.0]})
    candidate.fit(
        train,
        FitContext(seed=0, signals=("a",), regime_signals=(), sampling_s=1, time_budget_s=10.0),
    )
    scores = candidate.score(train)
    assert (
        candidate.alarm_policy(scores).threshold > 0
    )  # self-score zero does not calibrate threshold
    assert candidate.assign(train).shape == (4,)
    assert candidate.describe()


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("method", ["lsh", "optics", "som"])
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_synthetic_condition_matrix(scenario, method, seed):
    case = synthetic_snapshot(scenario, seed, train_rows=48, evaluation_rows=24)
    model = fit_model(case.snapshot.frame("train"), config(method))
    result = model.predict(case.snapshot.frame("evaluation"))
    assert len(result.rows) == 24
    assert model.calibration_count > 0
    assert model.summary()["modes"]
    assert all(row.omr_percent is None or row.omr_percent >= 0 for row in result.rows)
    if scenario == "sensor_quality":
        assert any(row.state == "invalid_input" for row in result.rows)
    json.dumps(result.to_dict(), allow_nan=False)


def test_snapshot_rejects_tampered_content():
    from lab.operating_modes import SourceSnapshot

    snapshot = synthetic_snapshot().snapshot
    payload = snapshot.to_dict()
    payload["values"][0][0] += 1
    with pytest.raises(ValueError, match="hash"):
        SourceSnapshot.model_validate_json(json.dumps(payload))


def test_database_selection_uses_readonly_bounded_parameters():
    from unittest.mock import MagicMock

    from sqlalchemy.dialects.postgresql import dialect

    from lab.operating_modes import read_postgres_snapshot

    engine = MagicMock()
    engine.dialect = dialect()
    connection = engine.connect.return_value.__enter__.return_value
    stamps = pd.date_range("2024", periods=3, tz="UTC")
    connection.execute.return_value.fetchall.return_value = list(
        zip(stamps, [1.0, 2.0, 3.0], strict=True)
    )
    selection = SourceSelection(
        source_id="test",
        start_utc=stamps[0].isoformat(),
        end_utc="2024-01-04T00:00:00+00:00",
        entity_column="asset",
        entity="a' OR true",
    )
    result = read_postgres_snapshot(engine, selection, ("value",), train_rows=2)
    calls = connection.execute.call_args_list
    assert str(calls[0].args[0]) == "SET TRANSACTION READ ONLY"
    assert "statement_timeout" in str(calls[1].args[0])
    assert "a' OR true" not in str(calls[2].args[0])
    assert calls[2].args[1]["entity"] == "a' OR true"
    assert calls[2].args[1]["limit"] == 4097
    assert result.train_rows == 2
    with pytest.raises(ValueError, match="identifier"):
        read_postgres_snapshot(engine, selection, ("x; DROP TABLE x",), train_rows=2)


def test_quality_metrics_do_not_inflate_process_success():
    from lab.operating_modes.evaluation import summarize_synthetic

    case = synthetic_snapshot("sensor_quality", 0, train_rows=48, evaluation_rows=24)
    model = fit_model(case.snapshot.frame("train"), config())
    result = summarize_synthetic(case, model.predict(case.snapshot.frame("evaluation")))
    assert result["quality_rows"] > 0
    assert result["process_point_recall"] is None
    assert result["quality_scored_as_process_success"] is False


def test_uniform_neighbors_ties_and_candidate_seed_composition():
    from lab.operating_modes import OperatingModeCandidate

    train = pd.DataFrame({"a": [0.0, 2.0, 4.0, 6.0]})
    model = fit_model(train, config(k=2, weighting="uniform"))
    row = model.predict(pd.DataFrame({"a": [3.0]})).rows[0]
    assert row.residuals[0].predicted == 3.0
    assert row.omr_percent == 0.0
    tied = fit_model(train, config(k=1)).predict(pd.DataFrame({"a": [3.0]})).rows[0]
    assert tied.residuals[0].predicted == 2.0  # original row order breaks ties
    candidate = OperatingModeCandidate(config(seed=7))
    candidate.fit(
        train,
        FitContext(seed=2, signals=("a",), regime_signals=(), sampling_s=1, time_budget_s=10.0),
    )
    assert candidate.model.config.seed == 9


def test_lsh_forms_separate_reference_modes():
    train = pd.DataFrame(
        {
            "a": [
                0.0,
                0.001,
                0.002,
                0.003,
                0.004,
                0.005,
                10.0,
                10.001,
                10.002,
                10.003,
                10.004,
                10.005,
            ]
        }
    )
    model = fit_model(train, ModeConfig(min_support=2, lsh_width=0.05))
    assert len(model.centers) == 2
    assert sum(model.labels.count(mode) for mode in range(2)) == len(train)
    assert model.summary()["diagnostics"]["semantics"] == "collision-count-connected-components.v1"
