"""Public source provenance and split protocol regressions."""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from harness.public_data import (
    SMDSourceFileRecord,
    SMDSourceManifest,
    SourceFileRecord,
    SourceManifest,
    _safe_source_path,
    load_care_farm_a_task,
    load_skab_session,
    load_smd_machine,
    load_smd_manifest,
    split_public_benchmark,
)


def test_care_farm_a_ignores_status_for_eval_but_masks_gaps_and_missing_features(
    tmp_path: Path,
) -> None:
    member = "CARE_To_Compare/Wind Farm A/datasets/0.csv"
    timestamps = list(pd.date_range("2023-01-01", periods=16, freq="10min"))
    timestamps.extend(pd.date_range("2023-01-03", periods=8, freq="10min"))
    timestamps[20:] = [value + pd.Timedelta(minutes=10) for value in timestamps[20:]]
    frame = pd.DataFrame(
        {
            "time_stamp": timestamps,
            "asset_id": ["1"] * 24,
            "id": list(range(24)),
            "train_test": ["train"] * 16 + ["prediction"] * 8,
            "status_type_id": [0] * 16 + [1, 3, 4, 1, 0, 0, 0, 0],
            "wind_speed_3_avg": [float(i) for i in range(24)],
            "sensor_0_avg": [float(i + 1) for i in range(24)],
        }
    )
    frame.loc[20, "sensor_0_avg"] = np.nan
    raw = frame.to_csv(index=False, sep=";").encode()
    archive_path = tmp_path / "care.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, raw)
    start_time = timestamps[18].strftime("%Y-%m-%d %H:%M:%S")
    end_time = timestamps[21].strftime("%Y-%m-%d %H:%M:%S")
    event_info = (
        "asset;event_id;event_label;event_start;event_start_id;event_end;event_end_id\n"
        f"1;0;anomaly;{start_time};18;{end_time};21\n"
    ).encode()
    task = load_care_farm_a_task(
        archive_path,
        member=member,
        event_info_csv=event_info,
        expected_member_sha256=hashlib.sha256(raw).hexdigest(),
        expected_member_bytes=len(raw),
        source_revision="zenodo:15846963:v6",
        source_license="CC-BY-SA-4.0",
        source_manifest_sha256="a" * 64,
        attribution="CARE source attribution",
        access_terms="noncommercial research",
        sliding_window=2,
        max_forward_fill_steps=0,
    )
    # Farm A's retrospective status codes do not mask prediction-time ground
    # truth; source gaps and observed NaNs remain masked after finite fill.
    assert not task.masked_samples[0]
    assert not task.masked_samples[1]
    assert task.labels[2]
    assert not task.masked_samples[2]
    assert not task.masked_samples[3]
    assert task.labels[3]
    assert task.masked_samples[4]
    missing_feature_index = int(
        (timestamps[20] - timestamps[16]).total_seconds() // 600
    )
    assert task.masked_samples[missing_feature_index]
    assert np.isfinite(task.evaluation_values.to_numpy()).all()
    assert task.train_source_indices == tuple(range(16))
    assert task.train_values.shape[0] == 16
    assert task.embargo_seconds == 86_400
    assert task.session_id.endswith("farm-A-status-eval-ignored.v3:event-0")


def _skab_source(root: Path, *, sensors: list[str] | None = None) -> tuple[SourceManifest, str]:
    relative = "data/public/skab/rev/data/valve1/0.csv"
    path = root / relative
    path.parent.mkdir(parents=True)
    frame = pd.DataFrame(
        {
            "datetime": pd.date_range("2020-01-01", periods=10, freq="s").strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "Accelerometer1RMS": np.arange(10, dtype=np.float64),
            "anomaly": [0.0] * 10,
            "changepoint": [0] * 10,
        }
    )
    raw = frame.to_csv(index=False, sep=";").encode()
    path.write_bytes(raw)
    record = SourceFileRecord(
        path=relative,
        bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        rows=len(frame),
        columns=list(frame.columns),
        sensor_columns=sensors or ["Accelerometer1RMS"],
        timezone_in_source=None,
    )
    manifest = SourceManifest(
        schema="source-discovery.v1",
        dataset="SKAB",
        revision="rev",
        repository_license="GPL-3.0",
        source_timezone="unspecified",
        files=(record,),
    )
    return manifest, relative


def test_skab_loader_hashes_and_parses_the_same_semicolon_bytes(tmp_path: Path) -> None:
    manifest, relative = _skab_source(tmp_path)
    loaded = load_skab_session(manifest, relative, tmp_path)
    assert loaded.session_id == f"rev:{relative}"
    assert loaded.values.columns.tolist() == ["Accelerometer1RMS"]
    assert loaded.labels.tolist() == [0] * 10
    assert loaded.timestamps_original[0] == "2020-01-01 00:00:00"


@pytest.mark.parametrize("sensors", [["anomaly"], ["changepoint"], ["datetime"]])
def test_skab_loader_rejects_label_or_time_as_sensor(tmp_path: Path, sensors: list[str]) -> None:
    manifest, relative = _skab_source(tmp_path, sensors=sensors)
    with pytest.raises(ValueError, match="sensor columns"):
        load_skab_session(manifest, relative, tmp_path)


def test_public_source_path_rejects_traversal_and_symlink_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="normalized"):
        _safe_source_path(tmp_path, "../escape.csv")
    real_root = tmp_path / "real"
    real_root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real_root, target_is_directory=True)
    with pytest.raises(ValueError, match="root cannot be a symlink"):
        _safe_source_path(alias, "data.csv")


def test_public_split_preserves_source_axis_and_never_cuts_eval_events() -> None:
    size = 320
    # Deliberately nonuniform cadence; the later evaluation half has 30-second
    # intervals and must not affect the training-derived embargo cadence.
    intervals = np.concatenate((np.ones(200), np.full(size - 200, 30.0)))
    times = pd.Timestamp("2020-01-01") + pd.to_timedelta(
        np.concatenate(([0.0], np.cumsum(intervals[:-1]))), unit="s"
    )
    labels = np.zeros(size, dtype=np.int8)
    labels[40:45] = 1
    labels[210:220] = 1
    labels[270:281] = 1
    values = pd.DataFrame({"sensor": np.arange(size, dtype=np.float64)})
    from harness.public_data import PublicSeries

    series = PublicSeries(
        session_id="fixture",
        values=values,
        labels=labels,
        timestamps_original=tuple(times.strftime("%Y-%m-%d %H:%M:%S")),
        sampling_period_s=1.0,
        source_revision="rev",
        source_license="GPL-3.0",
    )
    split = split_public_benchmark(series, sliding_window=5)
    assert split.policy == "public_benchmark.v1"
    assert split.embargo_seconds == 5.0
    assert split.train_indices == tuple(range(split.train_indices[0], split.train_indices[-1] + 1))
    assert split.train_indices[-1] < split.eval_indices[0]
    assert split.eval_timestamps_original[0] == times[split.eval_indices[0]].strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    assert 210 in split.eval_indices and 219 in split.eval_indices
    assert not any(
        label == 1 and (index == split.eval_indices[0] or index == split.eval_indices[-1])
        for index, label in zip(split.eval_indices, split.eval_labels, strict=True)
    )


def test_smd_loader_keeps_official_machine_split_and_test_labels_separate(
    tmp_path: Path,
) -> None:
    entity = "machine-1-1"
    revision = "source-rev"
    sensors = [f"sensor_{index:02d}" for index in range(38)]
    files: list[SMDSourceFileRecord] = []
    for source_split in ("train", "test", "test_label", "interpretation_label"):
        relative = f"data/public/smd/{revision}/{source_split}/{entity}.txt"
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if source_split in {"train", "test"}:
            frame = pd.DataFrame(np.arange(12 * 38, dtype=np.float64).reshape(12, 38))
            raw = frame.to_csv(index=False, header=False).encode("ascii")
            columns = 38
            sensor_columns = sensors
            rows = 12
        elif source_split == "test_label":
            raw = (
                pd.DataFrame([0, 0, 1, 1, 0, 0, 0, 0, 1, 0, 0, 0])
                .to_csv(index=False, header=False)
                .encode("ascii")
            )
            columns = 1
            sensor_columns = None
            rows = 12
        else:
            raw = b"0:2\n"
            columns = 1
            sensor_columns = None
            rows = 1
        path.write_bytes(raw)
        files.append(
            SMDSourceFileRecord(
                path=relative,
                bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
                entity=entity,
                source_split=source_split,
                rows=rows,
                columns=columns,
                sensor_columns=sensor_columns,
            )
        )
    manifest = SMDSourceManifest(
        schema="smd-source-manifest.v1",
        revision=revision,
        license="MIT",
        usage_profile="noncommercial_research",
        source_split_policy="upstream train/test; machines remain separate",
        timestamp_in_source=False,
        physical_sampling_seconds=60.0,
        sampling_source=(
            "https://netman.aiops.org/wp-content/uploads/2019/08/OmniAnomaly_camera-ready.pdf"
        ),
        sampling_source_section="Appendix A: DATASETS",
        sampling_evidence_sha256="e09319f9f62505d6b49497484c9c2dd599e94d7bd484704d867c6bdb8f8652b3",
        time_policy_status=(
            "source paper establishes 60 seconds; preserve relative sample axis, "
            "absolute time origin unknown"
        ),
        train_labels_status="no train labels provided; do not invent labels",
        source_files=tuple(files),
    )
    manifest_path = tmp_path / "smd-manifest.json"
    manifest_path.write_text(manifest.model_dump_json(by_alias=True), encoding="utf-8")
    loaded_manifest = load_smd_manifest(manifest_path)
    loaded = load_smd_machine(loaded_manifest, tmp_path, entity, sliding_window=4)
    assert loaded.session_id == f"{revision}:{entity}"
    assert loaded.train_values.shape == (12, 38)
    assert loaded.eval_values.shape == (12, 38)
    assert loaded.train_indices == tuple(range(12))
    assert loaded.eval_indices == tuple(range(12))
    assert loaded.train_source_indices == tuple(range(12))
    assert loaded.eval_source_indices == tuple(range(12, 24))
    assert loaded.eval_labels.tolist() == [0, 0, 1, 1, 0, 0, 0, 0, 1, 0, 0, 0]
    assert loaded.sample_period_s == 60.0
    assert loaded.time_axis_policy == "ordered_index_no_source_timestamps.v1"
    assert not hasattr(loaded, "train_labels")
    assert "interpretation_label" not in loaded.eval_values.columns

    with pytest.raises(ValueError, match="verified official split/time policy"):
        load_smd_machine(
            loaded_manifest.model_copy(update={"physical_sampling_seconds": 1.0}),
            tmp_path,
            entity,
            sliding_window=4,
        )
    with pytest.raises(ValueError, match="verified official split/time policy"):
        load_smd_machine(
            loaded_manifest.model_copy(update={"sampling_evidence_sha256": "0" * 64}),
            tmp_path,
            entity,
            sliding_window=4,
        )
    with pytest.raises(ValueError, match="exceeds a source split length"):
        load_smd_machine(loaded_manifest, tmp_path, entity, sliding_window=13)

    test_labels = tmp_path / files[2].path
    test_labels.write_bytes(b"1\n" * 12)
    with pytest.raises(ValueError, match="hash mismatch"):
        load_smd_machine(loaded_manifest, tmp_path, entity, sliding_window=4)
