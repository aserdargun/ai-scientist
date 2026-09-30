"""Hash-verified public sessions and versioned short-benchmark splitting."""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field


class SourceFileRecord(BaseModel):
    """Typed subset of a source discovery manifest entry."""

    model_config = ConfigDict(strict=True, extra="allow", frozen=True)

    path: str
    bytes: int = Field(ge=0)
    sha256: str
    rows: int | None = None
    columns: list[str] | None = None
    sensor_columns: list[str] | None = None
    timezone_in_source: str | None = None


class SourceManifest(BaseModel):
    """Immutable source revision and per-file provenance."""

    model_config = ConfigDict(strict=True, extra="allow", frozen=True)

    manifest_schema: Literal["source-discovery.v1"] = Field(alias="schema")
    dataset: str
    revision: str
    repository_license: str | None = None
    source_timezone: str | None = None
    files: tuple[SourceFileRecord, ...]


class SMDSourceFileRecord(BaseModel):
    """Hash-pinned file and upstream role for one SMD machine."""

    model_config = ConfigDict(strict=True, extra="allow", frozen=True)

    path: str
    bytes: int = Field(ge=0)
    sha256: str
    entity: str | None = None
    source_split: Literal["train", "test", "test_label", "interpretation_label"] | None = None
    rows: int | None = None
    columns: int | None = None
    sensor_columns: list[str] | None = None


class SMDSourceManifest(BaseModel):
    """SMD source provenance; timestamps and missing train labels stay explicit."""

    model_config = ConfigDict(strict=True, extra="allow", frozen=True)

    manifest_schema: Literal["smd-source-manifest.v1"] = Field(alias="schema")
    revision: str
    license: str
    usage_profile: Literal["noncommercial_research"]
    source_split_policy: str
    timestamp_in_source: Literal[False]
    physical_sampling_seconds: float
    sampling_source: str
    sampling_source_section: str
    sampling_evidence_sha256: str
    time_policy_status: str
    train_labels_status: str
    source_files: tuple[SMDSourceFileRecord, ...]


@dataclass(frozen=True, slots=True)
class PublicSeries:
    """Trusted label-bearing session; only `values` is candidate-facing."""

    session_id: str
    values: pd.DataFrame
    labels: np.ndarray
    timestamps_original: tuple[str, ...]
    sampling_period_s: float
    source_revision: str
    source_license: str | None
    usage_profile: Literal["noncommercial_research"] = "noncommercial_research"


@dataclass(frozen=True, slots=True)
class PublicTimeGrid:
    """A regularized sequence with source-clock provenance and explicit gaps.

    `values` may contain bounded forward fills for candidate input. `masked` remains
    true for every point that was not observed or could not be filled within the
    training-derived limit. Labels are copied onto exact grid points only.
    """

    values: pd.DataFrame
    labels: np.ndarray
    timestamps_original: tuple[str, ...]
    source_indices: tuple[int | None, ...]
    masked: np.ndarray
    sampling_period_s: int
    max_forward_fill_steps: int
    source_revision: str
    source_license: str | None


@dataclass(frozen=True, slots=True)
class CAREPublicTask:
    """One CARE Farm A dev prediction segment, with scorer-only labels/masks."""

    task_id: str
    dataset_id: str
    split_id: Literal["Adev"]
    session_id: str
    family: Literal["PDM", "NRM"]
    train_values: pd.DataFrame
    train_source_indices: tuple[int, ...]
    train_times: tuple[str, ...]
    embargo_seconds: int
    evaluation_values: pd.DataFrame
    evaluation_times: tuple[str, ...]
    masked_samples: tuple[bool, ...]
    failure_windows: tuple[tuple[int, int], ...]
    labels: tuple[bool, ...]
    sampling_period_s: int
    sliding_window: int
    source_revision: str
    source_license: str
    source_manifest_sha256: str
    attribution: str
    access_terms: str
    usage_profile: Literal["noncommercial_research"] = "noncommercial_research"


@dataclass(frozen=True, slots=True)
class PublicBenchmarkSplit:
    """Contiguous development split with a train-derived, same-session embargo."""

    session_id: str
    train_values: pd.DataFrame
    eval_values: pd.DataFrame
    eval_labels: np.ndarray
    train_timestamps_original: tuple[str, ...]
    eval_timestamps_original: tuple[str, ...]
    train_indices: tuple[int, ...]
    eval_indices: tuple[int, ...]
    embargo_samples: int
    embargo_seconds: float
    dropped_train_anomaly_rows: int
    dropped_embargo_rows: int
    dropped_embargo_events: int
    policy: str
    usage_profile: Literal["noncommercial_research"]


@dataclass(frozen=True, slots=True)
class SMDPublicSplit:
    """Official per-machine train/test split with scorer-only test labels."""

    session_id: str
    train_values: pd.DataFrame
    eval_values: pd.DataFrame
    eval_labels: np.ndarray
    train_indices: tuple[int, ...]
    eval_indices: tuple[int, ...]
    train_source_indices: tuple[int, ...]
    eval_source_indices: tuple[int, ...]
    sample_period_s: float
    sliding_window: int
    time_axis_policy: str
    source_revision: str
    source_license: str
    usage_profile: Literal["noncommercial_research"]


@dataclass(frozen=True, slots=True)
class TSBCuratedSeries:
    """One hash-bound TSB-AD-M row-order series with labels kept out of features."""

    member: str
    values: pd.DataFrame
    labels: np.ndarray
    masked: np.ndarray
    curated_train_stop: int
    source_revision: str
    archive_sha256: str
    member_sha256: str
    member_crc32: str
    member_bytes: int
    source_family: str
    signal_type: str
    sampling_period_s: int | None
    source_row_offset: int
    time_axis_policy: str


@dataclass(frozen=True, slots=True)
class TSBOrderSplit:
    """Fixed curated-train-prefix split; the boundary never consults labels."""

    train_values: pd.DataFrame
    evaluation_values: pd.DataFrame
    evaluation_labels: np.ndarray
    train_indices: tuple[int, ...]
    evaluation_indices: tuple[int, ...]
    embargo_samples: int
    sliding_window: int
    evaluation_instants: tuple[int, ...]
    masked_samples: tuple[bool, ...]


def regularize_public_series(
    series: PublicSeries,
    *,
    train_stop_index: int,
    sampling_period_s: int | None = None,
    max_forward_fill_steps: int = 3,
) -> PublicTimeGrid:
    """Create a train-derived regular grid while retaining timestamp gaps as masks.

    The output clock is a naive, source-relative clock. This function never invents
    a timezone. The cadence defaults to the rounded median interval in the training
    prefix only. Source rows map to their nearest grid point only within one quarter
    of the chosen cadence; duplicate/ambiguous mappings are rejected.
    """
    count = len(series.values)
    if not 2 <= train_stop_index <= count - 1:
        raise ValueError("train_stop_index must leave non-empty train and evaluation spans")
    if isinstance(max_forward_fill_steps, bool) or not isinstance(max_forward_fill_steps, int):
        raise ValueError("max_forward_fill_steps must be an integer")
    if max_forward_fill_steps < 0 or max_forward_fill_steps > 100:
        raise ValueError("max_forward_fill_steps is outside the bounded fill policy")
    timestamps = pd.to_datetime(series.timestamps_original, errors="raise", utc=False)
    seconds = timestamps.astype("int64").to_numpy(dtype=np.int64) // 1_000_000_000
    intervals = np.diff(seconds[:train_stop_index])
    if intervals.size == 0 or np.any(intervals <= 0):
        raise ValueError("training prefix timestamps must be strictly increasing")
    cadence = (
        int(round(float(np.median(intervals)))) if sampling_period_s is None else sampling_period_s
    )
    if isinstance(cadence, bool) or not isinstance(cadence, int) or cadence < 1:
        raise ValueError("sampling_period_s must be a positive integer")
    grid_seconds = np.arange(seconds[0], seconds[-1] + 1, cadence, dtype=np.int64)
    if grid_seconds.size < 2:
        raise ValueError("regularized sequence is too short")
    grid = pd.DataFrame(np.nan, index=np.arange(grid_seconds.size), columns=series.values.columns)
    labels = np.zeros(grid_seconds.size, dtype=np.int8)
    source_indices: list[int | None] = [None] * grid_seconds.size
    tolerance = cadence / 4.0
    for source_index, source_time in enumerate(seconds):
        offset = int(source_time - seconds[0])
        grid_index = int(round(offset / cadence))
        if (
            grid_index >= len(grid_seconds)
            or abs(int(grid_seconds[grid_index]) - int(source_time)) > tolerance
        ):
            continue
        if source_indices[grid_index] is not None:
            raise ValueError("multiple source observations map to one regular-grid point")
        source_indices[grid_index] = source_index
        grid.iloc[grid_index] = series.values.iloc[source_index].to_numpy(dtype=np.float64)
        labels[grid_index] = int(series.labels[source_index])
    observed = np.asarray([index is not None for index in source_indices], dtype=np.bool_)
    # Filled values remain marked if the source gap exceeds the allowed short fill.
    values = grid.ffill(limit=max_forward_fill_steps)
    masked = ~observed
    values = values.fillna(0.0)
    grid_times = tuple(
        pd.Timestamp(int(value), unit="s").strftime("%Y-%m-%d %H:%M:%S") for value in grid_seconds
    )
    return PublicTimeGrid(
        values=values.astype(np.float64),
        labels=labels,
        timestamps_original=grid_times,
        source_indices=tuple(source_indices),
        masked=masked,
        sampling_period_s=cadence,
        max_forward_fill_steps=max_forward_fill_steps,
        source_revision=series.source_revision,
        source_license=series.source_license,
    )


def load_source_manifest(manifest_path: Path) -> SourceManifest:
    """Parse a source manifest without accepting non-JSON coercions."""
    return SourceManifest.model_validate_json(manifest_path.read_bytes(), strict=True)


def load_tsb_curated_series(
    *,
    manifest_path: Path,
    repository_root: Path,
    member: str,
    source_family: str,
    signal_type: str,
    sampling_period_s: int | None,
    source_row_offset: int = 0,
    time_axis_policy: str,
) -> TSBCuratedSeries:
    """Read one pinned TSB member without extracting or selecting on its labels.

    The `_tr_N_` token is the upstream curated train prefix boundary. The `_1st_`
    token and every label value are deliberately ignored by this loader. Unknown
    source cadence is represented as ``None`` and a row-index axis.
    """
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("TSB acquisition manifest is missing or unsafe")
    manifest_raw = manifest_path.read_bytes()
    manifest = json.loads(manifest_raw)
    if (
        manifest.get("schema") != "source-discovery.v1"
        or manifest.get("dataset") != "TSB-AD-M"
        or manifest.get("sha256")
        != "7de86ac27f30eeb48d833bb061055670e3f3de07defd995cf2bd5db10ccc9a0d"
        or manifest.get("archive_bytes") != 540_383_983
    ):
        raise ValueError("TSB archive identity differs from pinned discovery evidence")
    relative_archive = manifest.get("archive")
    if not isinstance(relative_archive, str):
        raise ValueError("TSB archive path is invalid")
    archive_path = _safe_source_path(repository_root, relative_archive)
    if archive_path.stat().st_size != manifest["archive_bytes"]:
        raise ValueError("TSB archive byte count differs from its manifest")
    with archive_path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != manifest["sha256"]:
            raise ValueError("TSB archive hash differs from its pinned manifest")
    records = manifest.get("members")
    record = next(
        (item for item in records if isinstance(item, dict) and item.get("path") == member),
        None,
    ) if isinstance(records, list) else None
    if record is None or not member.startswith("TSB-AD-M/"):
        raise ValueError("requested TSB member is absent from its pinned archive manifest")
    if (
        not isinstance(source_family, str)
        or not source_family
        or not isinstance(signal_type, str)
        or not signal_type
        or isinstance(sampling_period_s, bool)
        or (sampling_period_s is not None and sampling_period_s < 1)
        or isinstance(source_row_offset, bool)
        or not isinstance(source_row_offset, int)
        or source_row_offset < 0
    ):
        raise ValueError("TSB source metadata is invalid")
    archive = zipfile.ZipFile(archive_path)
    try:
        try:
            info = archive.getinfo(member)
        except KeyError as exc:
            raise ValueError("requested TSB member is missing from archive") from exc
        if (
            info.file_size != record.get("uncompressed_bytes")
            or f"{info.CRC:08x}" != record.get("crc32")
        ):
            raise ValueError("TSB member size or CRC differs from its frozen archive manifest")
        raw = archive.read(member)
    finally:
        archive.close()
    frame = pd.read_csv(io.BytesIO(raw))
    if frame.empty:
        raise ValueError("TSB member has no data rows")
    train_match = re.search(r"_tr_(\d+)_", Path(member).name)
    if train_match is None:
        raise ValueError("TSB source member lacks its upstream train-prefix token")
    train_stop = int(train_match.group(1))
    if "Label" not in frame.columns or frame.columns[-1] != "Label":
        raise ValueError("TSB source member must store Label as its final column")
    labels_raw = pd.to_numeric(frame.pop("Label"), errors="raise").to_numpy(dtype=np.float64)
    values = frame.apply(pd.to_numeric, errors="raise")
    matrix = values.to_numpy(dtype=np.float64)
    if (
        not 2 <= train_stop < len(values)
        or not np.isfinite(labels_raw).all()
        or not np.isin(labels_raw, (0.0, 1.0)).all()
        or not np.isfinite(matrix).all()
        or matrix.shape[1] < 1
    ):
        raise ValueError("TSB curated member has invalid rows, labels, or sensor values")
    return TSBCuratedSeries(
        member=member,
        values=values.astype(np.float64),
        labels=labels_raw.astype(np.int8),
        masked=np.zeros(len(values), dtype=np.bool_),
        curated_train_stop=train_stop,
        source_revision=manifest["sha256"],
        archive_sha256=manifest["sha256"],
        member_sha256=hashlib.sha256(raw).hexdigest(),
        member_crc32=str(record["crc32"]),
        member_bytes=int(record["uncompressed_bytes"]),
        source_family=source_family,
        signal_type=signal_type,
        sampling_period_s=sampling_period_s,
        source_row_offset=source_row_offset,
        time_axis_policy=time_axis_policy,
    )


def expand_gecco_tsb_source_grid(
    series: TSBCuratedSeries,
    *,
    repository_root: Path,
    review_path: Path,
) -> TSBCuratedSeries:
    """Map GECCO's finite curated rows back onto the original source-row grid.

    Rows removed upstream for non-finite sensors are restored as masked, bounded
    filled feature rows. The source's one local-clock reset is masked, while the
    documented 60-second nominal row cadence is kept separate from wall time.
    """
    if series.member != "TSB-AD-M/173_GECCO_id_1_Sensor_tr_16165_1st_16265.csv":
        raise ValueError("GECCO grid expansion accepts only the pinned curated task")
    if review_path.is_symlink() or not review_path.is_file():
        raise ValueError("GECCO source-grid review evidence is missing or unsafe")
    review_raw = review_path.read_bytes()
    review = json.loads(review_raw)
    relative_path = review.get("source_path")
    if (
        review.get("dataset") != "GECCO"
        or review.get("source_sha256")
        != "c41221f3045696b75b79374f4c41c63f6c988e1df03b1dedcdbf910d44ea71cd"
        or review.get("source_rows") != 139566
        or review.get("curated_member") != series.member
        or review.get("source_valid_sensor_rows") != len(series.values)
        or review.get("timestamp_timezone") != "unspecified in original source"
        or not isinstance(relative_path, str)
    ):
        raise ValueError("GECCO source-grid evidence differs from the pinned review")
    source_path = _safe_source_path(repository_root, relative_path)
    source_raw = source_path.read_bytes()
    if hashlib.sha256(source_raw).hexdigest() != review["source_sha256"]:
        raise ValueError("GECCO original source hash differs from review evidence")
    source = pd.read_csv(io.BytesIO(source_raw))
    sensors = tuple(str(column) for column in series.values.columns)
    if (
        len(source) != review["source_rows"]
        or "Time" not in source
        or not set(sensors).issubset(source.columns)
        or tuple(source.loc[:, sensors].columns) != sensors
    ):
        raise ValueError("GECCO original source sensor schema or row count changed")
    timestamps = pd.to_datetime(source["Time"], errors="raise", utc=False)
    differences = timestamps.diff().dt.total_seconds().to_numpy(dtype=np.float64)[1:]
    if (
        not np.isfinite(differences).all()
        or int(np.count_nonzero(differences == -3540.0)) != 1
        or int(np.count_nonzero(differences == 60.0)) != len(source) - 2
    ):
        raise ValueError("GECCO source clock no longer matches documented row cadence/reset")
    raw_values = source.loc[:, sensors].apply(pd.to_numeric, errors="coerce")
    matrix = raw_values.to_numpy(dtype=np.float64)
    finite = np.isfinite(matrix).all(axis=1)
    source_indices = np.flatnonzero(finite)
    curated_values = series.values.to_numpy(dtype=np.float64)
    if len(source_indices) != len(curated_values) or not np.allclose(
        matrix[source_indices], curated_values, rtol=0.0, atol=2e-8
    ):
        raise ValueError("GECCO curated sensors do not align to finite source rows in order")
    labels = np.zeros(len(source), dtype=np.int8)
    labels[source_indices] = series.labels
    masked = ~finite
    reset_rows = np.flatnonzero(differences != 60.0) + 1
    masked[reset_rows] = True
    filled = raw_values.ffill(limit=3).fillna(0.0).astype(np.float64)
    train_stop = int(source_indices[series.curated_train_stop - 1]) + 1
    return TSBCuratedSeries(
        member=series.member,
        values=filled,
        labels=labels,
        masked=masked,
        curated_train_stop=train_stop,
        source_revision=series.source_revision,
        archive_sha256=series.archive_sha256,
        member_sha256=series.member_sha256,
        member_crc32=series.member_crc32,
        member_bytes=series.member_bytes,
        source_family=series.source_family,
        signal_type=series.signal_type,
        sampling_period_s=60,
        source_row_offset=0,
        time_axis_policy=(
            "gecco-original-row-order-60s-nominal-gap-mask-reset-not-utc.v1;"
            f" review_sha256={hashlib.sha256(review_raw).hexdigest()}"
        ),
    )


def split_tsb_curated_prefix(
    series: TSBCuratedSeries,
    *,
    masked: Sequence[bool] | None = None,
    evaluation_rows: int | None = None,
) -> TSBOrderSplit:
    """Freeze train window then embargo the official curated train boundary.

    This split is wholly independent of labels. Selection is by fixed member and
    source-published `_tr_` prefix; exactly one train-derived window of source
    samples is excluded after it. No positive-event search or retry is performed.
    """
    count = len(series.values)
    if series.labels.shape != (count,) or not 2 <= series.curated_train_stop < count:
        raise ValueError("TSB values, labels and curated train boundary do not align")
    if masked is None:
        mask = np.asarray(series.masked, dtype=np.bool_)
    else:
        mask = np.asarray(masked, dtype=np.bool_)
        if mask.shape != (count,):
            raise ValueError("TSB mask must cover the full source-order grid")
    if mask.shape != (count,) or not np.isfinite(series.values.to_numpy(dtype=np.float64)).all():
        raise ValueError("TSB source-grid features must be finite and align with the full mask")
    eligible = np.flatnonzero(~mask[: series.curated_train_stop])
    runs: list[tuple[int, int]] = []
    if eligible.size:
        start = previous = int(eligible[0])
        for raw_index in eligible[1:]:
            current = int(raw_index)
            if current != previous + 1:
                runs.append((start, previous + 1))
                start = current
            previous = current
        runs.append((start, previous + 1))
    train_start, train_stop = max(runs, key=lambda run: (run[1] - run[0], -run[0]))
    train_indices = np.arange(train_start, train_stop, dtype=np.int64)
    if train_indices.size < 2:
        raise ValueError("TSB source train prefix has no contiguous observed feature segment")
    # Training uses one contiguous finite source segment, so rolling operations do
    # not silently join observations across a source-data gap.
    train_frame = series.values.iloc[train_indices].reset_index(drop=True)
    window = derive_train_sliding_window(train_frame.to_numpy(dtype=np.float64))
    eval_start = series.curated_train_stop + window
    if count - eval_start < window:
        raise ValueError("TSB train-derived embargo leaves too little evaluation data")
    eval_indices = np.arange(eval_start, count, dtype=np.int64)
    if evaluation_rows is not None:
        if (
            isinstance(evaluation_rows, bool)
            or not isinstance(evaluation_rows, int)
            or evaluation_rows < 1
        ):
            raise ValueError("TSB evaluation_rows must be a positive integer when bounded")
        eval_indices = eval_indices[-evaluation_rows:]
    selected_train_indices = train_indices
    # This fixed suffix cap is a suite serialization budget policy. It is applied
    # after the label-free split is frozen and never inspected/revised by labels.
    return TSBOrderSplit(
        train_values=train_frame,
        evaluation_values=series.values.iloc[eval_indices].reset_index(drop=True),
        evaluation_labels=series.labels[eval_indices].copy(),
        train_indices=tuple(int(index) for index in selected_train_indices),
        evaluation_indices=tuple(int(index) for index in eval_indices),
        embargo_samples=window,
        sliding_window=window,
        evaluation_instants=tuple(
            (series.source_row_offset + int(index)) * (series.sampling_period_s or 1)
            for index in eval_indices
        ),
        masked_samples=tuple(bool(value) for value in mask[eval_indices]),
    )


def load_smd_manifest(manifest_path: Path) -> SMDSourceManifest:
    """Parse the frozen SMD acquisition manifest with strict scalar types."""
    return SMDSourceManifest.model_validate_json(manifest_path.read_bytes(), strict=True)


def load_smd_machine(
    manifest: SMDSourceManifest,
    repository_root: Path,
    entity: str,
    *,
    sliding_window: int,
) -> SMDPublicSplit:
    """Load one SMD official train/test machine, keeping all labels scorer-side.

    The source has no timestamp column. A 60-second sampling period is required
    from the pinned source manifest; sample indices remain ordered and are never
    presented as calendar timestamps.
    """
    if not entity or len(entity) > 64 or not entity.startswith("machine-"):
        raise ValueError("SMD entity must be a source machine ID")
    if isinstance(sliding_window, bool) or not isinstance(sliding_window, int):
        raise ValueError("SMD sliding_window must be an integer")
    if sliding_window < 1:
        raise ValueError("SMD sliding_window must be positive")
    if (
        manifest.license != "MIT"
        or manifest.usage_profile != "noncommercial_research"
        or manifest.timestamp_in_source is not False
        or not math.isfinite(manifest.physical_sampling_seconds)
        or manifest.physical_sampling_seconds != 60.0
        or manifest.sampling_source
        != "https://netman.aiops.org/wp-content/uploads/2019/08/OmniAnomaly_camera-ready.pdf"
        or manifest.sampling_source_section != "Appendix A: DATASETS"
        or manifest.sampling_evidence_sha256
        != "e09319f9f62505d6b49497484c9c2dd599e94d7bd484704d867c6bdb8f8652b3"
        or manifest.time_policy_status
        != (
            "source paper establishes 60 seconds; preserve relative sample axis, "
            "absolute time origin unknown"
        )
        or "train" not in manifest.source_split_policy.lower()
        or "test" not in manifest.source_split_policy.lower()
        or "separate" not in manifest.source_split_policy.lower()
        or "no train labels" not in manifest.train_labels_status.lower()
    ):
        raise ValueError("SMD manifest lacks the verified official split/time policy")
    entity_records = [record for record in manifest.source_files if record.entity == entity]
    records_by_split = {record.source_split: record for record in entity_records}
    if len(records_by_split) != len(entity_records) or set(records_by_split) != {
        "train",
        "test",
        "test_label",
        "interpretation_label",
    }:
        raise ValueError("SMD machine manifest must contain exactly four distinct source roles")
    train_record, test_record, labels_record = (
        records_by_split["train"],
        records_by_split["test"],
        records_by_split["test_label"],
    )
    sensors = train_record.sensor_columns
    if (
        train_record.columns != 38
        or test_record.columns != 38
        or labels_record.columns != 1
        or not sensors
        or len(sensors) != 38
        or len(set(sensors)) != 38
        or test_record.sensor_columns != sensors
    ):
        raise ValueError("SMD machine must expose the same 38 ordered sensors in train and test")

    def verified_bytes(record: SMDSourceFileRecord) -> bytes:
        path = _safe_source_path(repository_root, record.path)
        raw = path.read_bytes()
        if len(raw) != record.bytes or hashlib.sha256(raw).hexdigest() != record.sha256:
            raise ValueError("SMD source file hash mismatch")
        return raw

    train_raw = verified_bytes(train_record)
    test_raw = verified_bytes(test_record)
    labels_raw = verified_bytes(labels_record)
    if sliding_window > min(train_record.rows or 0, test_record.rows or 0):
        raise ValueError("SMD sliding_window exceeds a source split length")
    train_frame = pd.read_csv(io.BytesIO(train_raw), header=None)
    test_frame = pd.read_csv(io.BytesIO(test_raw), header=None)
    label_frame = pd.read_csv(io.BytesIO(labels_raw), header=None)
    for frame, record in (
        (train_frame, train_record),
        (test_frame, test_record),
        (label_frame, labels_record),
    ):
        if record.rows is not None and len(frame) != record.rows:
            raise ValueError("SMD source row count differs from manifest")
    if train_frame.shape[1] != 38 or test_frame.shape[1] != 38 or label_frame.shape[1] != 1:
        raise ValueError("SMD source machine has an unexpected number of columns")
    train_values = train_frame.apply(pd.to_numeric, errors="raise")
    test_values = test_frame.apply(pd.to_numeric, errors="raise")
    train_values.columns = sensors
    test_values.columns = sensors
    train_numeric = train_values.to_numpy(dtype=np.float64)
    test_numeric = test_values.to_numpy(dtype=np.float64)
    raw_labels = label_frame.iloc[:, 0].to_numpy(dtype=np.float64)
    if (
        not np.isfinite(train_numeric).all()
        or not np.isfinite(test_numeric).all()
        or not np.isfinite(raw_labels).all()
        or not np.isin(raw_labels, (0.0, 1.0)).all()
        or len(test_values) != len(raw_labels)
        or not np.any(raw_labels == 1.0)
    ):
        raise ValueError("SMD values/labels must be finite, aligned and binary with anomalies")
    session_id = f"{manifest.revision}:{entity}"
    return SMDPublicSplit(
        session_id=session_id,
        train_values=train_values,
        eval_values=test_values,
        eval_labels=raw_labels.astype(np.int8),
        train_indices=tuple(range(len(train_values))),
        eval_indices=tuple(range(len(test_values))),
        train_source_indices=tuple(range(len(train_values))),
        eval_source_indices=tuple(range(len(train_values), len(train_values) + len(test_values))),
        sample_period_s=float(manifest.physical_sampling_seconds),
        sliding_window=sliding_window,
        time_axis_policy="ordered_index_no_source_timestamps.v1",
        source_revision=manifest.revision,
        source_license=manifest.license,
        usage_profile=manifest.usage_profile,
    )


def derive_train_sliding_window(
    train_values: np.ndarray,
    *,
    minimum: int = 10,
    maximum: int = 100,
) -> int:
    """Freeze a bounded VUS window from training-only sensor decorrelation.

    For each non-constant feature, select the first lag up to `maximum` at which
    absolute Pearson autocorrelation falls below 1/e. The median feature lag is
    rounded and clipped to [minimum, maximum]. No labels/evaluation data enter.
    """
    values = np.asarray(train_values, dtype=np.float64)
    if values.ndim != 2 or values.shape[0] < 2 or values.shape[1] < 1:
        raise ValueError("train-derived window needs a non-empty numeric matrix")
    if not np.isfinite(values).all():
        raise ValueError("train-derived window features must be finite")
    if (
        isinstance(minimum, bool)
        or isinstance(maximum, bool)
        or not isinstance(minimum, int)
        or not isinstance(maximum, int)
        or minimum < 1
        or maximum < minimum
    ):
        raise ValueError("train-derived window bounds are invalid")
    lag_limit = min(maximum, max(1, values.shape[0] // 4))
    selected: list[int] = []
    for column in range(values.shape[1]):
        series = values[:, column]
        if np.std(series) == 0.0:
            continue
        centered = series - float(np.mean(series))
        variance = float(np.dot(centered, centered))
        for lag in range(1, lag_limit + 1):
            correlation = float(np.dot(centered[:-lag], centered[lag:]) / variance)
            if abs(correlation) <= 1.0 / np.e:
                selected.append(lag)
                break
        else:
            selected.append(lag_limit)
    if not selected:
        selected.append(minimum)
    return min(maximum, max(minimum, int(round(float(np.median(selected))))))


def load_care_farm_a_task(
    archive_path: Path,
    *,
    member: str,
    event_info_csv: bytes,
    expected_member_sha256: str,
    expected_member_bytes: int,
    source_revision: str,
    source_license: str,
    source_manifest_sha256: str,
    attribution: str,
    access_terms: str,
    sliding_window: int | None = None,
    feature_columns: Sequence[str] | None = None,
    max_forward_fill_steps: int = 3,
) -> CAREPublicTask:
    """Load one CARE Farm A dev task from the sealed source ZIP.

    The member must be in Farm A. Farm B and C are rejected by path before archive
    access. Event metadata is trusted Scorer input. Candidate features are limited
    to `_avg` sensor columns; status, event metadata, timestamps and IDs stay out.
    CARE's source clock is a naive 600-second grid. Missing timestamps remain masked
    on that grid; candidate training receives only finite status-0/2 training rows.
    """
    expected_prefix = "CARE_To_Compare/Wind Farm A/datasets/"
    if not member.startswith(expected_prefix) or not member.endswith(".csv"):
        raise ValueError("only CARE Farm A development dataset members are eligible")
    if (
        len(expected_member_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_member_sha256)
        or expected_member_bytes < 1
        or source_license != "CC-BY-SA-4.0"
        or not source_revision.startswith("zenodo:15846963")
        or not source_manifest_sha256
        or not attribution
        or not access_terms
    ):
        raise ValueError("CARE provenance, source hash, or noncommercial access terms are missing")
    if sliding_window is not None and (
        isinstance(sliding_window, bool)
        or not isinstance(sliding_window, int)
        or sliding_window < 1
    ):
        raise ValueError("CARE sliding_window override must be a positive integer")
    if (
        isinstance(max_forward_fill_steps, bool)
        or not isinstance(max_forward_fill_steps, int)
        or not 0 <= max_forward_fill_steps <= 100
    ):
        raise ValueError("CARE forward-fill limit is outside the bounded policy")
    if archive_path.is_symlink() or not archive_path.is_file():
        raise ValueError("CARE source archive must be a regular, non-symlink file")
    with zipfile.ZipFile(archive_path) as archive:
        try:
            info = archive.getinfo(member)
            raw = archive.read(info)
        except KeyError as exc:
            raise ValueError("CARE Farm A member is absent from the pinned archive") from exc
    if (
        len(raw) != expected_member_bytes
        or hashlib.sha256(raw).hexdigest() != expected_member_sha256
    ):
        raise ValueError("CARE dataset member hash or size differs from the frozen manifest")
    frame = pd.read_csv(io.BytesIO(raw), sep=";")
    required = {"time_stamp", "id", "train_test", "status_type_id"}
    if not required.issubset(frame.columns):
        raise ValueError("CARE dataset member has an unexpected source schema")
    available_sensor_columns = tuple(column for column in frame.columns if column.endswith("_avg"))
    if feature_columns is None:
        sensor_columns = available_sensor_columns
    else:
        requested = tuple(feature_columns)
        if not requested or len(requested) != len(set(requested)):
            raise ValueError("CARE feature selection must be a non-empty unique column list")
        if any(column not in available_sensor_columns for column in requested):
            raise ValueError(
                "CARE feature selection includes a missing or non-average source field"
            )
        sensor_columns = requested
    if (
        not sensor_columns
        or any(not pd.api.types.is_numeric_dtype(frame[column]) for column in sensor_columns)
        or "status_type" in frame.columns
    ):
        raise ValueError("CARE feature policy requires numeric *_avg columns only")
    times = pd.to_datetime(frame["time_stamp"], errors="raise", utc=False)
    if times.dt.tz is not None or not times.is_monotonic_increasing or times.duplicated().any():
        raise ValueError("CARE source timestamps must be ordered naive timestamps")
    if not set(frame["train_test"].unique()).issubset({"train", "prediction"}):
        raise ValueError("CARE split has unexpected train_test values")
    train_mask = frame["train_test"].eq("train").to_numpy(dtype=np.bool_)
    prediction_mask = frame["train_test"].eq("prediction").to_numpy(dtype=np.bool_)
    train_count = int(train_mask.sum())
    if (
        train_count < 2
        or not prediction_mask.any()
        or not np.array_equal(np.flatnonzero(train_mask), np.arange(train_count))
        or not np.array_equal(np.flatnonzero(prediction_mask), np.arange(train_count, len(frame)))
    ):
        raise ValueError("CARE Farm A requires one train prefix and one prediction suffix")
    timestamps = times.dt.strftime("%Y-%m-%d %H:%M:%S")
    timestamp_seconds = times.astype("int64").to_numpy(dtype=np.int64) // 1_000_000_000
    if np.any(np.diff(timestamp_seconds) <= 0):
        raise ValueError("CARE timestamps must be strictly increasing")
    if np.any(np.diff(timestamp_seconds) % 600 != 0):
        raise ValueError("CARE source timestamps must retain the 600-second source grid")

    # Farm A status ids are useful for filtering training data, but the v6
    # release explicitly says to ignore them for prediction-time evaluation:
    # unlike Farms B/C, Farm A statuses were derived from the failure logbook.
    # Keep validation here so an unknown upstream code cannot silently enter a
    # new source revision; status codes do not create evaluation masks.
    statuses = pd.to_numeric(frame["status_type_id"], errors="raise").to_numpy(dtype=np.int64)
    if not set(np.unique(statuses)).issubset({0, 1, 2, 3, 4, 5}):
        raise ValueError("CARE status_type_id contains an undocumented status")
    numeric = frame.loc[:, sensor_columns].apply(pd.to_numeric, errors="coerce")
    feature_missing = numeric.isna().any(axis=1).to_numpy(dtype=np.bool_)
    train_healthy = train_mask & np.isin(statuses, (0, 2)) & ~feature_missing
    prediction_start = int(timestamp_seconds[np.flatnonzero(prediction_mask)[0]])
    selected_window = sliding_window or derive_train_sliding_window(
        numeric.loc[train_healthy].to_numpy(dtype=np.float64)
    )
    embargo_seconds = max(selected_window * 600, 86_400)
    train_healthy &= timestamp_seconds <= prediction_start - embargo_seconds
    eligible = np.flatnonzero(train_healthy)
    runs: list[tuple[int, int]] = []
    if eligible.size:
        run_start = previous = int(eligible[0])
        for index in eligible[1:]:
            current = int(index)
            if (
                current != previous + 1
                or timestamp_seconds[current] - timestamp_seconds[previous] != 600
            ):
                runs.append((run_start, previous + 1))
                run_start = current
            previous = current
        runs.append((run_start, previous + 1))
    train_start, train_stop = max(runs, key=lambda run: run[1] - run[0]) if runs else (0, 0)
    train_indices = tuple(range(train_start, train_stop))
    train_values = numeric.iloc[list(train_indices)].reset_index(drop=True)
    if (
        len(train_values) < max(3 * selected_window, selected_window + 1)
        or not np.isfinite(train_values.to_numpy(dtype=np.float64)).all()
    ):
        raise ValueError("CARE Farm A has insufficient healthy finite training data")

    prediction_indices = np.flatnonzero(prediction_mask)
    prediction_times = timestamp_seconds[prediction_indices]
    grid_times = np.arange(prediction_times[0], prediction_times[-1] + 1, 600, dtype=np.int64)
    if len(grid_times) < selected_window:
        raise ValueError("CARE Farm A prediction segment is too short")
    eval_frame = pd.DataFrame(np.nan, index=np.arange(len(grid_times)), columns=sensor_columns)
    eval_source_indices = np.full(len(grid_times), -1, dtype=np.int64)
    for source_index in prediction_indices:
        offset = int(timestamp_seconds[source_index] - grid_times[0])
        if offset % 600 != 0:
            raise ValueError("CARE prediction timestamp is off the fixed 600-second grid")
        grid_index = offset // 600
        eval_source_indices[grid_index] = int(source_index)
        eval_frame.iloc[grid_index] = numeric.iloc[source_index].to_numpy(dtype=np.float64)
    observed = eval_source_indices >= 0
    feature_missing_grid = eval_frame.isna().any(axis=1).to_numpy(dtype=np.bool_)
    # Farm A status ids are retrospective fault-logbook labels, not a valid
    # prediction-time mask. Only unobserved source-grid points and missing
    # selected sensor values are excluded from evaluation.
    masked = (~observed) | feature_missing_grid
    if max_forward_fill_steps:
        eval_frame = eval_frame.ffill(limit=max_forward_fill_steps)
    eval_frame = eval_frame.fillna(0.0)
    eval_numeric = eval_frame.to_numpy(dtype=np.float64)
    if not np.isfinite(eval_numeric).all():
        raise ValueError("CARE prediction grid contains non-finite values after bounded fill")
    event_info = pd.read_csv(io.BytesIO(event_info_csv), sep=";")
    event_id = int(Path(member).stem)
    id_column = "asset" if "asset" in event_info.columns else "asset_id"
    event_rows = event_info.loc[event_info["event_id"].astype(int).eq(event_id)]
    if len(event_rows) != 1 or id_column not in event_info.columns:
        raise ValueError("CARE Farm A event metadata must contain exactly one matching event")
    event = event_rows.iloc[0]
    event_label = str(event["event_label"]).strip().lower()
    if event_label not in {"anomaly", "normal"}:
        raise ValueError("CARE event label must be anomaly or normal")
    member_asset = str(frame["asset_id"].iloc[0]) if "asset_id" in frame.columns else ""
    if not member_asset or str(event[id_column]) != member_asset:
        raise ValueError("CARE event metadata asset identity differs from dataset rows")
    for id_name, time_name in (
        ("event_start_id", "event_start"),
        ("event_end_id", "event_end"),
    ):
        matched_times = frame.loc[
            pd.to_numeric(frame["id"], errors="raise").eq(int(event[id_name])), "time_stamp"
        ]
        if len(matched_times) != 1 or pd.Timestamp(matched_times.iloc[0]) != pd.Timestamp(
            event[time_name]
        ):
            raise ValueError("CARE event metadata IDs and source timestamps do not agree")
    start_timestamp = pd.Timestamp(event["event_start"])
    end_timestamp = pd.Timestamp(event["event_end"])
    start = int(np.searchsorted(grid_times, start_timestamp.value // 1_000_000_000))
    end = int(np.searchsorted(grid_times, end_timestamp.value // 1_000_000_000))
    if (
        start >= len(grid_times)
        or end >= len(grid_times)
        or grid_times[start] * 1_000_000_000 != start_timestamp.value
        or grid_times[end] * 1_000_000_000 != end_timestamp.value
        or end <= start
    ):
        raise ValueError("CARE event window does not align to the original prediction grid")
    family: Literal["PDM", "NRM"] = "PDM" if event_label == "anomaly" else "NRM"
    labels = np.zeros(len(grid_times), dtype=np.bool_)
    failures: tuple[tuple[int, int], ...] = ()
    if family == "PDM":
        labels[start : end + 1] = ~masked[start : end + 1]
        failures = ((start, end),)
    eval_times = tuple(
        pd.Timestamp(int(value), unit="s").strftime("%Y-%m-%d %H:%M:%S") for value in grid_times
    )
    safe_task = f"care-a-{event_id:03d}"
    return CAREPublicTask(
        task_id=safe_task,
        dataset_id="CARE-A",
        split_id="Adev",
        session_id=(
            f"{source_revision}:farm-A-status-eval-ignored.v3:event-{event_id}"
        ),
        family=family,
        train_values=train_values.astype(np.float64),
        train_source_indices=train_indices,
        train_times=tuple(str(timestamps.iloc[index]) for index in train_indices),
        embargo_seconds=embargo_seconds,
        evaluation_values=pd.DataFrame(eval_numeric, columns=sensor_columns),
        evaluation_times=eval_times,
        masked_samples=tuple(bool(value) for value in masked),
        failure_windows=failures,
        labels=tuple(bool(value) for value in labels),
        sampling_period_s=600,
        sliding_window=selected_window,
        source_revision=source_revision,
        source_license=source_license,
        source_manifest_sha256=source_manifest_sha256,
        attribution=attribution,
        access_terms=access_terms,
    )


def _safe_source_path(repository_root: Path, relative_path: str) -> Path:
    """Resolve an in-repository source path while rejecting traversal/symlinks."""
    if repository_root.is_symlink():
        raise ValueError("public source repository root cannot be a symlink")
    if "\\" in relative_path or any(part in ("", ".", "..") for part in relative_path.split("/")):
        raise ValueError("source path must be a normalized repository-relative path")
    relative = Path(relative_path)
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise ValueError("source path must be a normalized repository-relative path")
    root = repository_root.resolve(strict=True)
    path = root / relative
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"symlink in public source path: {relative_path}")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError("public source path escapes repository or is not a file")
    return resolved


def load_skab_session(
    manifest: SourceManifest,
    relative_path: str,
    repository_root: Path,
) -> PublicSeries:
    """Load one SKAB session, verify its bytes, and keep labels out of values."""
    if manifest.dataset != "SKAB":
        raise ValueError("manifest is not a SKAB source")
    record = next((item for item in manifest.files if item.path == relative_path), None)
    if record is None or not relative_path.lower().endswith(".csv"):
        raise ValueError("requested session is absent from manifest")
    path = _safe_source_path(repository_root, relative_path)
    raw = path.read_bytes()
    if len(raw) != record.bytes or hashlib.sha256(raw).hexdigest() != record.sha256:
        raise ValueError("public source file hash mismatch")
    # Parse the exact bytes whose digest was checked; reopening the path would
    # leave a hash/parse race if a source file changed between those operations.
    frame = pd.read_csv(io.BytesIO(raw), sep=";")
    if record.rows is not None and len(frame) != record.rows:
        raise ValueError("public source row count differs from manifest")
    if "datetime" not in frame.columns:
        raise ValueError("SKAB source session has no datetime column")
    if record.columns is not None and list(frame.columns) != record.columns:
        raise ValueError("public source columns differ from manifest")
    if record.timezone_in_source is not None:
        raise ValueError("this SKAB adapter supports only source timestamps without timezone")
    timestamps = pd.to_datetime(frame["datetime"], errors="raise", utc=False)
    if timestamps.dt.tz is not None or not timestamps.is_monotonic_increasing:
        raise ValueError("SKAB naive timestamp order is invalid; no timezone is inferred")
    differences = timestamps.diff().dt.total_seconds().dropna().to_numpy(dtype=np.float64)
    if differences.size == 0 or not np.isfinite(differences).all() or np.any(differences <= 0):
        raise ValueError("SKAB session needs positive timestamp intervals")
    sampling_period_s = float(np.median(differences))
    label_name = "anomaly" if "anomaly" in frame.columns else None
    if label_name is None:
        if "anomaly-free" not in relative_path:
            raise ValueError("labeled SKAB session has no anomaly column")
        labels = np.zeros(len(frame), dtype=np.int8)
    else:
        raw_labels = frame[label_name].to_numpy(dtype=np.float64)
        if not np.isfinite(raw_labels).all() or not np.isin(raw_labels, (0.0, 1.0)).all():
            raise ValueError("SKAB labels must be finite binary values")
        labels = raw_labels.astype(np.int8)
    sensors = record.sensor_columns
    forbidden = {"datetime", "anomaly", "changepoint"}
    if (
        not sensors
        or len(set(sensors)) != len(sensors)
        or forbidden.intersection(sensors)
        or any(column not in frame.columns for column in sensors)
    ):
        raise ValueError("manifest sensor columns do not match source CSV")
    values = frame.loc[:, sensors].apply(pd.to_numeric, errors="raise")
    numeric = values.to_numpy(dtype=np.float64)
    if not np.isfinite(numeric).all():
        raise ValueError("SKAB sensor values must be finite")
    session_id = f"{manifest.revision}:{relative_path}"
    return PublicSeries(
        session_id=session_id,
        values=values.reset_index(drop=True),
        labels=labels,
        timestamps_original=tuple(timestamps.dt.strftime("%Y-%m-%d %H:%M:%S")),
        sampling_period_s=sampling_period_s,
        source_revision=manifest.revision,
        source_license=manifest.repository_license,
        usage_profile="noncommercial_research",
    )


def _events(labels: np.ndarray) -> tuple[tuple[int, int], ...]:
    """Return inclusive contiguous positive intervals."""
    padded = np.concatenate((np.array([False]), labels.astype(bool), np.array([False])))
    changes = np.flatnonzero(padded[1:] != padded[:-1])
    return tuple(
        (int(start), int(stop - 1)) for start, stop in zip(changes[::2], changes[1::2], strict=True)
    )


def split_public_benchmark(
    series: PublicSeries,
    *,
    sliding_window: int,
    train_fraction: float = 0.6,
) -> PublicBenchmarkSplit:
    """Split one session on source order with an embargo derived from train cadence.

    `public_benchmark.v1` intentionally has no one-day floor: these source sessions are
    13–166 minutes. It never joins sessions or rewrites their naive timestamps as UTC.
    """
    if isinstance(sliding_window, bool) or not isinstance(sliding_window, int):
        raise ValueError("sliding_window must be an integer")
    if sliding_window < 1 or not 0.4 <= train_fraction <= 0.75:
        raise ValueError("invalid public benchmark split parameters")
    size = len(series.values)
    if series.labels.shape != (size,) or size < 8 * sliding_window:
        raise ValueError("session is too short for train/eval plus embargo")
    target = int(size * train_fraction)
    radius = max(sliding_window * 2, int(size * 0.12))
    lower = max(3 * sliding_window, target - radius)
    upper = min(size - 3 * sliding_window, target + radius)
    event_spans = _events(series.labels)
    candidates = [
        boundary
        for boundary in range(lower, upper + 1)
        if not any(start < boundary <= end for start, end in event_spans)
    ]
    if not candidates:
        raise ValueError("no event-safe boundary exists for public_benchmark.v1")
    boundary = min(candidates, key=lambda value: (abs(value - target), value))
    # Embargo cadence is estimated exclusively from timestamps available before
    # the selected boundary; evaluation timestamps cannot influence the split.
    train_timestamps = pd.to_datetime(
        series.timestamps_original[:boundary], errors="raise", utc=False
    )
    train_differences = (
        train_timestamps.to_series().diff().dt.total_seconds().dropna().to_numpy(dtype=np.float64)
    )
    if (
        train_differences.size == 0
        or not np.isfinite(train_differences).all()
        or np.any(train_differences <= 0)
    ):
        raise ValueError("training prefix has invalid timestamp cadence")
    train_sample_period = float(np.median(train_differences))
    embargo_seconds = sliding_window * train_sample_period
    embargo_samples = max(sliding_window, math.ceil(embargo_seconds / train_sample_period))
    train_end = boundary - embargo_samples
    eval_start = boundary + embargo_samples
    if train_end < sliding_window or size - eval_start < sliding_window:
        raise ValueError("embargo leaves insufficient train/eval samples")
    train_labels = series.labels[:train_end]
    healthy_runs = []
    cursor = 0
    for start, end in _events(train_labels):
        if cursor < start:
            healthy_runs.append((cursor, start))
        cursor = end + 1
    if cursor < train_end:
        healthy_runs.append((cursor, train_end))
    eligible_runs = [run for run in healthy_runs if run[1] - run[0] >= sliding_window]
    if not eligible_runs:
        raise ValueError("public split has no contiguous healthy training segment")
    train_start, selected_train_end = max(eligible_runs, key=lambda run: (run[1] - run[0], -run[0]))

    # Do not score partial events at the evaluation edges. Move the evaluation
    # start past a crossing event and trim a crossing terminal event.
    for start, end in event_spans:
        if start < eval_start <= end:
            eval_start = end + 1
    eval_end = size
    for start, end in event_spans:
        if start < eval_end <= end:
            eval_end = start
            break
    eval_indices_array = np.arange(eval_start, eval_end, dtype=np.int64)
    if eval_end - eval_start < sliding_window:
        raise ValueError("public split has insufficient contiguous eval samples")
    eval_labels = series.labels[eval_indices_array].copy()
    train_indices = np.arange(train_start, selected_train_end, dtype=np.int64)
    dropped_events = sum(1 for start, end in event_spans if start < eval_start and end >= train_end)
    if not np.any(eval_labels == 1):
        raise ValueError("public split requires healthy train rows and eval anomaly rows")
    return PublicBenchmarkSplit(
        session_id=series.session_id,
        train_values=series.values.iloc[train_indices].copy(),
        eval_values=series.values.iloc[eval_indices_array].copy(),
        eval_labels=eval_labels,
        train_timestamps_original=tuple(
            series.timestamps_original[index] for index in train_indices
        ),
        eval_timestamps_original=tuple(
            series.timestamps_original[index] for index in eval_indices_array
        ),
        train_indices=tuple(int(index) for index in train_indices),
        eval_indices=tuple(int(index) for index in eval_indices_array),
        embargo_samples=embargo_samples,
        embargo_seconds=embargo_seconds,
        dropped_train_anomaly_rows=int((train_labels == 1).sum()),
        dropped_embargo_rows=eval_start - train_end + (size - eval_end),
        dropped_embargo_events=dropped_events,
        policy="public_benchmark.v1",
        usage_profile=series.usage_profile,
    )
