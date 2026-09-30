"""TSB boundary selection is frozen by the curated train prefix, never by labels."""

from __future__ import annotations

import numpy as np
import pandas as pd

from harness.public_data import TSBCuratedSeries, split_tsb_curated_prefix


def _series(labels: np.ndarray) -> TSBCuratedSeries:
    rows = np.arange(160, dtype=np.float64)
    values = pd.DataFrame({"sensor": np.sin(rows / 5.0), "sensor2": np.cos(rows / 8.0)})
    return TSBCuratedSeries(
        member="TSB-AD-M/001_Genesis_id_1_Sensor_tr_100_1st_120.csv",
        values=values,
        labels=labels,
        masked=np.zeros(len(values), dtype=np.bool_),
        curated_train_stop=100,
        source_revision="revision",
        archive_sha256="a" * 64,
        member_sha256="b" * 64,
        member_crc32="12345678",
        member_bytes=100,
        source_family="industrial-process",
        signal_type="sensors",
        sampling_period_s=None,
        source_row_offset=0,
        time_axis_policy="ordered-row-index-with-unresolved-cadence.v1",
    )


def test_tsb_prefix_split_is_invariant_to_all_label_values() -> None:
    quiet = split_tsb_curated_prefix(_series(np.zeros(160, dtype=np.int8)))
    anomaly = np.zeros(160, dtype=np.int8)
    anomaly[:100] = 1
    anomaly[quiet.evaluation_indices[-1]] = 1
    labeled = split_tsb_curated_prefix(_series(anomaly))
    assert quiet.train_indices == labeled.train_indices == tuple(range(100))
    assert quiet.evaluation_indices == labeled.evaluation_indices
    assert quiet.embargo_samples == labeled.embargo_samples


def test_tsb_grid_mask_preserves_gap_and_uses_contiguous_train_segment() -> None:
    mask = np.zeros(160, dtype=np.bool_)
    mask[20:23] = True
    mask[135] = True
    split = split_tsb_curated_prefix(
        _series(np.zeros(160, dtype=np.int8)), masked=tuple(bool(value) for value in mask)
    )
    assert split.train_indices == tuple(range(23, 100))
    assert split.masked_samples[135 - split.evaluation_indices[0]] is True
    assert len(split.evaluation_labels) == len(split.evaluation_values)
