"""Output-driven unit checks for suite and source guard decisions."""

from __future__ import annotations

import json
from io import BytesIO

import numpy as np
import pandas as pd
import pyarrow.ipc as ipc
import pytest

from lab.sandbox.evaluation import (
    CandidateGuardReject,
    _arrow_bytes,
    _relative_difference,
    _score_artifact,
    _strict_policy,
    check_complete_suite_position_bias,
    check_hardcoding,
    check_position_bias,
)


def test_constant_nan_and_wrong_length_outputs_are_rejected() -> None:
    for scores in ([1.0] * 4, [0.0, float("nan"), 2.0, 3.0], [1.0, 2.0, 3.0]):
        payload = json.dumps(
            {
                "schema": "candidate-scores.v1",
                "sample_indices": list(range(len(scores))),
                "scores": scores,
            },
            allow_nan=True,
        ).encode()
        with pytest.raises((CandidateGuardReject, ValueError)):
            _score_artifact(payload, 4)


def test_position_bias_uses_only_nrm_and_rejects_suite_half_threshold() -> None:
    ramp = tuple(float(index) for index in range(20))
    valid = tuple(float(index % 3) for index in range(20))
    assert (
        check_position_bias(((False, ramp), (False, ramp))).code == "position_bias_not_applicable"
    )
    assert check_position_bias(((True, valid), (False, ramp))).passed
    result = check_position_bias(((True, ramp), (True, valid)))
    assert not result.passed and result.code == "position_bias"
    assert result.value is not None and result.value > 0.8
    assert (
        check_complete_suite_position_bias(((True, ramp),), expected_nrm_tasks=2).code
        == "position_bias_incomplete_suite"
    )
    assert check_complete_suite_position_bias(
        ((True, ramp), (True, valid)), expected_nrm_tasks=2
    ).code == "position_bias"


def test_hardcoding_guard_matches_task_ids_utc_instants_and_numeric_sequences() -> None:
    task_ids = frozenset({"task-secret-1"})
    instant = "2025-01-02T03:04:05Z"
    assert check_hardcoding(
        b"WINDOW=4\n", task_ids=task_ids, evaluation_instants=frozenset({instant})
    ).passed
    assert (
        check_hardcoding(
            b'TASK="task-secret-1"\n', task_ids=task_ids, evaluation_instants=frozenset()
        ).code
        == "hardcoding_task_id"
    )
    assert (
        check_hardcoding(
            b'TIME="2025-01-02T03:04:05+00:00"\n',
            task_ids=frozenset(),
            evaluation_instants=frozenset({instant}),
        ).code
        == "hardcoding_timestamp"
    )
    literal_vector = (
        "VALUES = [" + ",".join(str(index / 2) for index in range(64)) + "]\n"
    ).encode()
    assert (
        check_hardcoding(literal_vector, task_ids=frozenset(), evaluation_instants=frozenset()).code
        == "hardcoding_numeric_sequence"
    )
    negative_vector = (
        "VALUES = [" + ",".join(f"-{index + 0.5}" for index in range(64)) + "]\n"
    ).encode()
    assert (
        check_hardcoding(
            negative_vector, task_ids=frozenset(), evaluation_instants=frozenset()
        ).code
        == "hardcoding_numeric_sequence"
    )


def test_hardcoding_timestamp_requires_timezone_and_parses_real_instants() -> None:
    assert check_hardcoding(
        b'TIME="2025-01-02T03:04:05"\n',
        task_ids=frozenset(),
        evaluation_instants=frozenset({"2025-01-02T03:04:05Z"}),
    ).passed
    assert (
        check_hardcoding(
            b'TIME="2025-01-02T10:15:00Z"\n',
            task_ids=frozenset(),
            evaluation_instants=frozenset(),
            evaluation_intervals=(("2025-01-02T10:00:00Z", "2025-01-02T11:00:00Z"),),
        ).code
        == "hardcoding_timestamp"
    )


def test_full_guard_contract_uses_one_hundred_nanorelative_tolerance() -> None:
    from lab.sandbox.evaluation import CAUSALITY_REL_TOLERANCE, DETERMINISM_REL_TOLERANCE

    assert DETERMINISM_REL_TOLERANCE == pytest.approx(1e-7)
    assert CAUSALITY_REL_TOLERANCE == pytest.approx(1e-7)
    with pytest.raises(ValueError):
        _score_artifact(
            b'{"schema":"candidate-scores.v1","sample_indices":[0,1],"scores":[1.0,Infinity]}',
            2,
        )


def test_relative_guard_rejects_small_magnitude_full_scale_change() -> None:
    assert _relative_difference(np.asarray([1e-12]), np.asarray([2e-12])) == pytest.approx(1.0)


def test_arrow_transport_drops_dataframe_attributes_and_schema_metadata() -> None:
    frame = pd.DataFrame({"sensor": [1.0, 2.0]})
    frame.attrs["eval_labels"] = [0, 1]
    payload = _arrow_bytes(frame, allowed_sensor_columns=("sensor",))
    table = ipc.open_stream(BytesIO(payload)).read_all()
    assert table.column_names == ["sensor"]
    assert table.schema.metadata is None
    assert frame.attrs["eval_labels"] == [0, 1]


def test_arrow_transport_drops_unused_categorical_values() -> None:
    frame = pd.DataFrame(
        {
            "sensor": pd.Categorical(
                ["1", "2"], categories=["1", "2", "private-unused-category-canary"]
            )
        }
    )
    payload = _arrow_bytes(frame, allowed_sensor_columns=("sensor",))
    table = ipc.open_stream(BytesIO(payload)).read_all()
    observed = table.to_pandas()
    assert observed["sensor"].tolist() == [1.0, 2.0]
    assert observed["sensor"].dtype == np.float64
    assert b"private-unused-category-canary" not in payload


def test_extreme_literals_and_duplicate_policy_keys_fail_closed() -> None:
    huge_integer = str(10**1000).encode()
    assert check_hardcoding(
        b"VALUE=" + huge_integer,
        task_ids=frozenset(),
        evaluation_instants=frozenset(),
    ).passed
    with pytest.raises(CandidateGuardReject, match="invalid_alarm_policy"):
        _strict_policy(
            b'{"threshold":' + huge_integer + b',"release":0,"dwell":1}'
        )
    with pytest.raises(CandidateGuardReject, match="invalid_alarm_policy"):
        _strict_policy(
            b'{"threshold":2,"threshold":3,"release":0,"dwell":1}'
        )
    assert check_hardcoding(
        b'TIME="9999-12-31T23:59:59-23:00"',
        task_ids=frozenset(),
        evaluation_instants=frozenset(),
    ).passed
