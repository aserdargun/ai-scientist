"""CPU-only immutable chronological stream input boundary tests."""

from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from lab.operating_modes import SCENARIOS, stream


def request(**kwargs):
    return stream.SyntheticStreamRequest.model_validate(
        {
            "scenario": "healthy_single",
            "seed": 7,
            "train_rows": 32,
            "evaluation_rows": 130,
            "chunk_rows": 64,
            **kwargs,
        }
    )


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_single_process_is_reproducible_and_independent_of_chunk_boundaries(tmp_path, scenario):
    left = stream.create_synthetic_input(tmp_path, request(scenario=scenario))
    right = stream.create_synthetic_input(tmp_path, request(scenario=scenario, chunk_rows=13))
    assert stream.create_synthetic_input(tmp_path, left.request) == left
    pd.testing.assert_frame_equal(
        stream.load_train(tmp_path, left), stream.load_train(tmp_path, right)
    )
    left_values = pd.concat(
        [frame for _, frame in stream.iter_chunks(tmp_path, left)], ignore_index=True
    )
    right_values = pd.concat(
        [frame for _, frame in stream.iter_chunks(tmp_path, right)], ignore_index=True
    )
    pd.testing.assert_frame_equal(left_values, right_values)
    assert left.request_sha256 != right.request_sha256
    assert left.scoring_available is False
    assert left.source_identity["scoring_available"] is False
    assert [frame.row_offset for frame in left.chunks] == [0, 64, 128]
    for earlier, later in zip((left.train, *left.chunks), left.chunks, strict=False):
        assert pd.Timestamp(later.start_utc) - pd.Timestamp(earlier.end_utc) == pd.Timedelta(
            seconds=1
        )
    # Evaluation is one realization, not repeated calls that restart the same seed.
    assert not np.allclose(left_values.iloc[:64], left_values.iloc[64:128], equal_nan=True)


@pytest.mark.parametrize(
    "change",
    [
        {"train_rows": 4097},
        {"train_rows": 15},
        {"evaluation_rows": 65537},
        {"evaluation_rows": 0},
        {"chunk_rows": 65},
        {"chunk_rows": 0},
        {"seed": True},
        {"scenario": "unknown"},
        {"evaluation_rows": 1025, "chunk_rows": 1},
    ],
)
def test_request_bounds_fail_before_generation(change):
    with pytest.raises(ValueError):
        request(**change)


def test_maximum_evaluation_is_finite_and_every_frame_stays_bounded(tmp_path):
    manifest = stream.create_synthetic_input(
        tmp_path, request(evaluation_rows=65536, train_rows=16)
    )
    assert len(manifest.chunks) == 1024
    assert sum(ref.rows for ref in manifest.chunks) == 65536
    assert max(ref.rows for ref in manifest.chunks) == 64
    assert manifest.chunks[-1].row_offset == 65472
    assert stream.read_chunk(tmp_path, manifest, 1023).shape == (64, 4)


def test_reuse_verifies_every_frame_and_per_step_reads_only_required_frame(tmp_path, monkeypatch):
    manifest = stream.create_synthetic_input(tmp_path, request())
    seen = []
    original = stream._frame

    def record(directory, current, ref):
        seen.append(ref.name)
        return original(directory, current, ref)

    monkeypatch.setattr(stream, "_frame", record)
    stream.read_chunk(tmp_path, manifest, 1)
    assert seen == [manifest.chunks[1].name]
    seen.clear()
    stream.load_input(tmp_path, manifest.sha256)
    assert seen == [manifest.train.name, *(ref.name for ref in manifest.chunks)]
    broken = tmp_path / manifest.sha256 / manifest.chunks[-1].name
    broken.chmod(0o600)
    payload = broken.read_bytes().replace(b"sensor_a", b"sensor_z")
    broken.write_bytes(payload)
    with pytest.raises(ValueError, match="hash"):
        stream.load_input(tmp_path, manifest.sha256)
    with pytest.raises(ValueError, match="hash"):
        stream.create_synthetic_input(tmp_path, manifest.request)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "oversize", "fifo"])
def test_actual_file_reads_reject_unsafe_type_or_size(tmp_path, kind):
    import os

    manifest = stream.create_synthetic_input(tmp_path, request())
    path = tmp_path / manifest.sha256 / manifest.train.name
    original = path.read_bytes()
    path.unlink()
    external = tmp_path / "external"
    external.write_bytes(original)
    if kind == "symlink":
        path.symlink_to(external)
    elif kind == "hardlink":
        os.link(external, path)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        with path.open("wb") as handle:
            handle.truncate(stream.MAX_TRAIN_BYTES + 1)
    with pytest.raises((ValueError, OSError)):
        stream.load_train(tmp_path, manifest)


def test_rehashed_overlapping_manifest_is_rejected(tmp_path):
    manifest = stream.create_synthetic_input(tmp_path, request())
    value = manifest.to_dict()
    value["chunks"][1]["start_utc"] = value["chunks"][0]["start_utc"]
    value.pop("sha256")
    value["sha256"] = hashlib.sha256(stream.canonical_json(value)).hexdigest()
    with pytest.raises(ValueError, match="overlap"):
        stream.StreamManifest.model_validate_json(json.dumps(value))


def test_manifest_change_is_checked_before_step(tmp_path):
    manifest = stream.create_synthetic_input(tmp_path, request())
    path = tmp_path / manifest.sha256 / "manifest.json"
    path.chmod(0o600)
    value = json.loads(path.read_bytes())
    value["entity"] = "other-process"
    path.write_bytes(stream.canonical_json(value))
    with pytest.raises(ValueError):
        stream.read_chunk(tmp_path, manifest, 0)


def test_sensor_quality_values_remain_null_without_labels(tmp_path):
    manifest = stream.create_synthetic_input(tmp_path, request(scenario="sensor_quality"))
    assert np.isfinite(stream.load_train(tmp_path, manifest).to_numpy()).all()
    frames = list(stream.iter_chunks(tmp_path, manifest))
    assert any(frame.isna().any().any() for _, frame in frames)
    for ref, frame in frames:
        assert tuple(frame.columns) == manifest.sensors
        raw = json.loads((tmp_path / manifest.sha256 / ref.name).read_bytes())
        assert "labels" not in raw and "event_labels" not in raw
