from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest

from harness import care_holdout as care


def _synthetic_rows(
    *,
    prediction_rows: int = 20,
    skip_prefix_index: int | None = 10,
    alter_eval_value: bool = False,
    alter_reference_value: bool = False,
    alter_reference_status: bool = False,
) -> tuple[list[care._SourceRow], dict[str, object]]:
    start = datetime(2024, 1, 1)
    reference_start = datetime(2024, 1, 13)
    prediction_start = reference_start + timedelta(days=7)
    rows: list[care._SourceRow] = []
    source_id = 0
    for index in range(600):
        values = tuple(float(np.sin(index * (column + 1) * 0.037) + column) for column in range(6))
        rows.append(
            care._SourceRow(
                start + timedelta(seconds=600 * index), source_id, "asset-1", "train", 0, values
            )
        )
        source_id += 1
    for index in range(1008):
        if index == skip_prefix_index:
            continue
        values = tuple(float(column + index / 100.0) for column in range(6))
        if alter_reference_value:
            values = tuple(value + 1000.0 for value in values)
        if index == 11:
            values = (values[0], float("nan"), *values[2:])
        status = 4 if index == 30 or (alter_reference_status and index == 13) else 0
        if index == 12:
            values = (0.0, *values[1:])
        rows.append(
            care._SourceRow(
                reference_start + timedelta(seconds=600 * index),
                source_id,
                "asset-1",
                "train",
                status,
                values,
            )
        )
        source_id += 1
    for index in range(prediction_rows):
        values = tuple(float(column + index) for column in range(6))
        if alter_eval_value:
            values = tuple(value + 100.0 for value in values)
        rows.append(
            care._SourceRow(
                prediction_start + timedelta(seconds=600 * index),
                source_id,
                "asset-1",
                "prediction",
                0,
                values,
            )
        )
        source_id += 1
    event = {
        "asset_id": "asset-1",
        "event_label": "anomaly",
        "event_start": prediction_start,
        "event_start_id": rows[-prediction_rows].source_index,
        "event_end": prediction_start + timedelta(seconds=600 * (prediction_rows - 1)),
        "event_end_id": rows[-1].source_index,
    }
    return rows, event


def _materialize(rows: list[care._SourceRow], event: dict[str, object]) -> care.FarmBCareTask:
    return care._materialize_task(
        event_id=53,
        event=event,
        rows=rows,
        source_member=care.MEMBER_PINS[53][0],
        source_member_sha256=care.MEMBER_PINS[53][1],
        source_archive_sha256=care.FARM_B_ARCHIVE_SHA256,
        manifest_sha256="a" * 64,
    )


def _small_zip(csv_text: str, *, member: str = "farm-b.csv") -> zipfile.ZipFile:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, csv_text.encode())
    data.seek(0)
    return zipfile.ZipFile(data, "r")


def _csv_rows(*, unknown_status: str = "0", off_grid: bool = False) -> str:
    header = ";".join(care.CSV_COLUMNS)
    lines = [header]
    start = datetime(2024, 2, 1)
    for index in range(8):
        timestamp = start + timedelta(seconds=600 * index)
        if off_grid and index == 6:
            timestamp += timedelta(seconds=300)
        split = "train" if index < 4 else "prediction"
        status = unknown_status if index == 5 else "0"
        values = [str(float(index + column)) for column in range(6)]
        lines.append(
            ";".join(
                [
                    timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                    "asset-1",
                    str(index),
                    split,
                    status,
                    *values,
                ]
            )
        )
    return "\n".join(lines) + "\n"


def _row_event() -> dict[str, object]:
    return {
        "asset_id": "asset-1",
        "event_label": "anomaly",
        "event_start": datetime(2024, 2, 1, 0, 40),
        "event_start_id": 4,
        "event_end": datetime(2024, 2, 1, 1, 10),
        "event_end_id": 7,
    }


def test_materializer_adds_fixed_prefix_and_preserves_full_prediction_grid() -> None:
    rows, event = _synthetic_rows()
    task = _materialize(rows, event)

    assert len(task.evaluation_values) == 1008 + 20
    assert len(task.evaluation_times) == len(task.masked_samples) == len(task.labels)
    assert task.evaluation_times[0] == "2024-01-13 00:00:00"
    assert task.evaluation_times[1008] == "2024-01-20 00:00:00"
    assert task.evaluation_source_indices[10] is None
    assert task.masked_samples[10] is True
    assert task.masked_samples[11] is True
    assert task.masked_samples[30] is True
    assert task.masked_samples[12] is False
    assert task.evaluation_values.iloc[12, 0] == 0.0
    assert task.failure_windows == ((1008, 1027),)
    assert task.labels[1008] is True and task.labels[1027] is True
    assert task.labels[10] is False
    assert task.sampling_period_s == 600
    assert task.source_license == "CC-BY-SA-4.0"
    assert task.usage_profile == "noncommercial_research"


def test_fit_is_contiguous_train_only_embargoed_and_earliest_on_tie() -> None:
    rows, event = _synthetic_rows()
    # A same-length eligible segment after a large raw-time gap must not win ties.
    first = rows[:600]
    last_source_index = first[-1].source_index
    second_start = first[-1].timestamp + timedelta(days=1)
    second = [
        care._SourceRow(
            second_start + timedelta(seconds=600 * index),
            last_source_index + index + 1,
            "asset-1",
            "train",
            0,
            row.values,
        )
        for index, row in enumerate(first)
    ]
    boundary_id = second[-1].source_index + 1
    reference_start = datetime(2024, 1, 13)
    # Keep the original prefix and prediction; shift their source IDs to remain ordered.
    tail = [row for row in rows if row.timestamp >= reference_start]
    shifted_tail = [
        care._SourceRow(
            row.timestamp, boundary_id + index, row.asset_id, row.split, row.status, row.values
        )
        for index, row in enumerate(tail)
    ]
    event = dict(event)
    event["event_start_id"] = shifted_tail[-20].source_index
    event["event_end_id"] = shifted_tail[-1].source_index
    all_rows = first + second + shifted_tail
    fit = care._longest_fit_segment(all_rows, cutoff=datetime(2024, 1, 12))

    assert len(fit) == len(first)
    assert fit[0].timestamp == first[0].timestamp
    assert fit[-1].timestamp == first[-1].timestamp
    task = _materialize(all_rows, event)
    assert task.train_source_indices[0] == first[0].source_index
    assert task.train_times[-1] <= (
        datetime.fromisoformat(task.reference_start) - timedelta(days=1)
    ).strftime("%Y-%m-%d %H:%M:%S")


def test_label_or_prediction_change_cannot_change_fit_or_window() -> None:
    rows, event = _synthetic_rows()
    original = _materialize(rows, event)
    changed_rows, changed_event = _synthetic_rows(alter_eval_value=True)
    changed_event["event_label"] = "normal"
    changed = _materialize(changed_rows, changed_event)

    pd.testing.assert_frame_equal(original.train_values, changed.train_values)
    assert original.train_source_indices == changed.train_source_indices
    assert original.sliding_window == changed.sliding_window
    assert original.evaluation_times == changed.evaluation_times
    assert original.masked_samples == changed.masked_samples
    assert original.evaluation_values.iloc[:1008].equals(changed.evaluation_values.iloc[:1008])
    assert (
        original.evaluation_values.iloc[1008:].iloc[0, 0]
        != changed.evaluation_values.iloc[1008:].iloc[0, 0]
    )
    assert original.family == "PDM" and changed.family == "NRM"
    assert original.failure_windows and changed.failure_windows == ()
    assert not any(changed.labels)


def test_reference_values_and_status_cannot_change_fit_or_window() -> None:
    rows, event = _synthetic_rows()
    original = _materialize(rows, event)
    changed_rows, changed_event = _synthetic_rows(
        alter_reference_value=True,
        alter_reference_status=True,
    )
    changed = _materialize(changed_rows, changed_event)

    pd.testing.assert_frame_equal(original.train_values, changed.train_values)
    assert original.train_source_indices == changed.train_source_indices
    assert original.train_times == changed.train_times
    assert original.train_cutoff == changed.train_cutoff
    assert original.sliding_window == changed.sliding_window
    assert original.evaluation_values.iloc[:1008].to_numpy().tolist() != (
        changed.evaluation_values.iloc[:1008].to_numpy().tolist()
    )
    assert original.masked_samples[13] is False
    assert changed.masked_samples[13] is True
    assert original.labels == changed.labels
    assert original.failure_windows == changed.failure_windows


def test_materialization_receipt_reports_mask_vector_grid_length_not_mask_presence() -> None:
    from runpy import run_path

    rows, event = _synthetic_rows()
    task = _materialize(rows, event)
    report_module = run_path("docs/ai-scientist/review-evidence/care-b-prefix-materialization.py")
    task_report = report_module["_task_report"](task)

    assert task_report["evaluation_mask_vector_rows"] == len(task.evaluation_values)
    assert task_report["prediction_grid_rows"] == task_report["prediction_endpoint_grid_rows"] == 20
    assert task_report["prediction_rows_masked"] == sum(task.masked_samples[1008:])


def test_entire_event_prediction_still_has_seven_day_healthy_reference() -> None:
    rows, event = _synthetic_rows(prediction_rows=20)
    task = _materialize(rows, event)

    assert task.family == "PDM"
    assert all(task.labels[index] for index in range(1008, len(task.labels)))
    assert any(not task.masked_samples[index] for index in range(1008))
    assert any(not task.labels[index] and not task.masked_samples[index] for index in range(1008))


def test_unknown_status_and_off_grid_source_rows_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = "CARE_To_Compare/Wind Farm B/datasets/53.csv"
    monkeypatch.setattr(care, "MEMBER_PINS", {53: (member, "", 0)})
    for text in (_csv_rows(unknown_status="9"), _csv_rows(off_grid=True)):
        encoded = text.encode()
        digest = hashlib.sha256(encoded).hexdigest()
        monkeypatch.setattr(care, "MEMBER_PINS", {53: (member, digest, len(encoded))})
        with _small_zip(text, member=member) as archive:
            with pytest.raises(ValueError):
                care._read_source_rows(archive, event_id=53, event=_row_event())


def test_member_hash_mismatch_and_non_farm_b_path_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = _csv_rows()
    encoded = text.encode()
    member = "CARE_To_Compare/Wind Farm B/datasets/53.csv"
    monkeypatch.setattr(care, "MEMBER_PINS", {53: (member, "0" * 64, len(encoded))})
    with _small_zip(text, member=member) as archive:
        with pytest.raises(ValueError, match="member hash"):
            care._read_source_rows(archive, event_id=53, event=_row_event())
    with pytest.raises(ValueError, match="non-pinned Farm B"):
        care._validate_farm_b_member(53, "CARE_To_Compare/Wind Farm C/datasets/53.csv")


def test_short_or_unhealthy_fit_and_pdm_without_healthy_exposure_fail_closed() -> None:
    rows, event = _synthetic_rows()
    too_short = [row for row in rows if row.split != "train" or row.source_index >= 580]
    with pytest.raises(ValueError):
        _materialize(too_short, event)

    # Mark the fixed reference and prediction as non-normal: there is no healthy
    # exposure outside the event window, so the task cannot enter the suite.
    unhealthy = [
        care._SourceRow(
            row.timestamp,
            row.source_index,
            row.asset_id,
            row.split,
            4 if datetime(2024, 1, 13) <= row.timestamp < datetime(2024, 1, 20) else row.status,
            row.values,
        )
        for row in rows
    ]
    with pytest.raises(ValueError, match="healthy reference/eval exposure"):
        _materialize(unhealthy, event)


def test_event_metadata_digest_fails_closed() -> None:
    with pytest.raises(ValueError, match="event metadata hash"):
        care._parse_event_info(b"event_id;event_label\n53;anomaly\n")


def test_task_semantics_digest_binds_family_times_masks_and_failure_window() -> None:
    rows, event = _synthetic_rows()
    task = _materialize(rows, event)
    assert len(task.semantics_sha256) == 64
    tampered = dict(task.semantics)
    tampered["masked_samples"] = [True, *task.masked_samples[1:]]
    payload = {"schema": "public-task-semantics.v1", **tampered}
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    ).hexdigest()
    assert digest != task.semantics_sha256


def test_scorer_binding_requires_exact_tasks_without_importing_holdout_runtime() -> None:
    from lab.scorer.care_holdout import CareFarmBTaskBinding, validate_care_farm_b_bindings

    rows, event = _synthetic_rows()
    task = _materialize(rows, event)
    suite = care.FarmBCareSuite(
        tasks=(task,),
        manifest_sha256="b" * 64,
        source_archive_sha256=care.FARM_B_ARCHIVE_SHA256,
        source_event_info_sha256=care.FARM_B_EVENT_INFO_SHA256,
        source_feature_description_sha256=care.FARM_B_FEATURE_DESCRIPTION_SHA256,
    )
    with pytest.raises(ValueError, match="cover every suite task exactly"):
        validate_care_farm_b_bindings(suite, bindings={})
    binding = CareFarmBTaskBinding(
        profile_sha256="c" * 64,
        task_weight=1.0,
        base_score=0.1,
        reference_score=0.5,
    )
    checked = validate_care_farm_b_bindings(suite, bindings={task.task_id: binding})
    assert checked == ((task, binding),)
    with pytest.raises(ValueError, match="binding is invalid"):
        validate_care_farm_b_bindings(
            suite,
            bindings={
                task.task_id: CareFarmBTaskBinding(
                    profile_sha256="not-a-digest",
                    task_weight=1.0,
                    base_score=0.1,
                    reference_score=0.5,
                )
            },
        )


def test_registration_builder_binds_labels_semantics_and_arrow_matrices() -> None:
    from harness.contracts import FitContext
    from lab.scorer.care_holdout import (
        CareFarmBTaskBinding,
        build_care_farm_b_registration_records,
    )
    from lab.scorer.holdout import HoldoutTaskSpec, _frame_bytes

    rows, event = _synthetic_rows()
    task = _materialize(rows, event)
    suite = care.FarmBCareSuite(
        tasks=(task,),
        manifest_sha256="b" * 64,
        source_archive_sha256=care.FARM_B_ARCHIVE_SHA256,
        source_event_info_sha256=care.FARM_B_EVENT_INFO_SHA256,
        source_feature_description_sha256=care.FARM_B_FEATURE_DESCRIPTION_SHA256,
    )
    binding = CareFarmBTaskBinding(
        profile_sha256="c" * 64,
        task_weight=1.0,
        base_score=0.1,
        reference_score=0.5,
    )
    records = build_care_farm_b_registration_records(
        suite,
        bindings={task.task_id: binding},
    )

    assert len(records) == 1
    record = records[0]
    spec = record.task_spec
    assert isinstance(spec, HoldoutTaskSpec)
    assert spec.context == FitContext(
        seed=0,
        signals=tuple(care.FEATURE_COLUMNS),
        regime_signals=(),
        sampling_s=600,
        time_budget_s=60.0,
    )
    assert spec.train.equals(task.train_values)
    assert spec.evaluation.equals(task.evaluation_values)
    assert len(record.labels) == len(record.semantics["masked_samples"]) == len(
        record.semantics["evaluation_times"]
    ) == len(spec.evaluation)
    assert record.labels == task.labels
    assert record.semantics == task.semantics
    assert record.semantics_sha256 == task.semantics_sha256
    assert record.source_identity == task.source_identity
    assert record.usage_profile == "noncommercial_research"

    for frame in (spec.train, spec.evaluation):
        payload = _frame_bytes(frame, columns=tuple(spec.context.signals))
        table = ipc.open_stream(pa.py_buffer(payload)).read_all()
        roundtrip = table.to_pandas()
        pd.testing.assert_frame_equal(frame.reset_index(drop=True), roundtrip)
        assert not any(name in frame.columns for name in ("label", "timestamp", "status"))

    with pytest.raises(ValueError, match="cover every suite task exactly"):
        build_care_farm_b_registration_records(suite, bindings={})
