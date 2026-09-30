"""Scorer-only CARE Farm B v1 private holdout materializer.

This is a local adaptation of CARE metadata and the parent-approved seven-day
healthy reference policy. It is not a reproduction of the published benchmark.
Never import this module from Director/candidate code: returned tasks contain
trusted labels, private identities, timestamp semantics, and source provenance.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import IO, Literal, cast

import numpy as np
import pandas as pd

from harness.public_data import derive_train_sliding_window

FARM_B_ARCHIVE_SHA256 = "ca61379e98956d891041ad45c885109bd8a14199fde0688d0184a11c2d4194f1"
FARM_B_EVENT_INFO_SHA256 = "e27b4de2fc9f87b7c73ca5bc5a0950cb56cf666a26d9749c52c4aff7c473e681"
FARM_B_FEATURE_DESCRIPTION_SHA256 = (
    "140b0446767c4d2a0399791e0086258596c3a58370b0c4ebfbe1d0f5edc0efed"
)
FARM_B_SOURCE_MANIFEST_SHA256 = "55433394759fee3782eb51c8350446561268acd837f2d44624d07a8b1505ec20"
FARM_B_RECORD_URL = "https://zenodo.org/records/15846963"
FARM_B_REVISION = "care-to-compare-zenodo-15846963-v6-farm-b-local-adaptation-v1"
FARM_B_LICENSE = "CC-BY-SA-4.0"
USAGE_PROFILE: Literal["noncommercial_research"] = "noncommercial_research"
SAMPLING_SECONDS = 600
REFERENCE_DAYS = 7
EMBARGO_SECONDS = 86_400
MIN_WINDOW = 10
MAX_WINDOW = 100
MIN_FIT_ROWS_MULTIPLE = 3
CSV_CHUNK_ROWS = 4096
DEFAULT_ARCHIVE_PATH = Path("data/public/care/15846963/CARE_To_Compare.zip")
EVENT_INFO_MEMBER = "CARE_To_Compare/Wind Farm B/event_info.csv"
FEATURE_DESCRIPTION_MEMBER = "CARE_To_Compare/Wind Farm B/feature_description.csv"
FEATURE_COLUMNS = (
    "reactive_power_11_avg",
    "power_58_avg",
    "wind_speed_59_avg",
    "wind_speed_60_avg",
    "wind_speed_61_avg",
    "power_62_avg",
)
NORMAL_STATUSES = frozenset({0, 2})
DOCUMENTED_STATUSES = frozenset({0, 1, 2, 3, 4, 5})
MASKED_STATUSES = frozenset({1, 3, 4, 5})
CSV_COLUMNS = ("time_stamp", "asset_id", "id", "train_test", "status_type_id", *FEATURE_COLUMNS)

# Each Farm B CSV member is independently pinned in addition to the archive and
# event/feature metadata digests. Farm C members are deliberately absent.
MEMBER_PINS: Mapping[int, tuple[str, str, int]] = {
    2: (
        "CARE_To_Compare/Wind Farm B/datasets/2.csv",
        "0d103687d779a4f9714a6dab80aeda526346ea6def85006541a24a6038b947c5",
        87149492,
    ),
    7: (
        "CARE_To_Compare/Wind Farm B/datasets/7.csv",
        "4a3c90b68f97079efac7f50db51ae066be89d07b616557b01da3911a7e3a1236",
        92094858,
    ),
    19: (
        "CARE_To_Compare/Wind Farm B/datasets/19.csv",
        "5a70079af0dcd4a8012235b68552a9d4b37c0680a664c8361f8aa364d69e1972",
        89680003,
    ),
    21: (
        "CARE_To_Compare/Wind Farm B/datasets/21.csv",
        "11d5a6221ea4e2c367a777e08528e0b295d8c51f2aa716d3e99ffff13047e663",
        85180339,
    ),
    23: (
        "CARE_To_Compare/Wind Farm B/datasets/23.csv",
        "daabd43cd9ad3d742aa5e4dfb752f607e22741429accb657a119b3bd759b747d",
        86384882,
    ),
    27: (
        "CARE_To_Compare/Wind Farm B/datasets/27.csv",
        "1d0778d613a5d7cb2e8d1f31c8d5b68559ca3b355b4724525fc8234878e8d889",
        98960870,
    ),
    34: (
        "CARE_To_Compare/Wind Farm B/datasets/34.csv",
        "2bea7a785a4ec6d170965bb57fb183c39b09c119e6d3a27047e8f35419474120",
        89434660,
    ),
    52: (
        "CARE_To_Compare/Wind Farm B/datasets/52.csv",
        "29250e477ad7b41626fb041329d88796cbd56ea9a7cc83584f167d708a0f7d1a",
        87397671,
    ),
    53: (
        "CARE_To_Compare/Wind Farm B/datasets/53.csv",
        "9a8fd31ce4efb75323edf1daef46af46b25a7f2a4baf39359112be469291b0c6",
        93271611,
    ),
    74: (
        "CARE_To_Compare/Wind Farm B/datasets/74.csv",
        "14c870e25b4eec0dc73f378e556e15197b020a7e131a27a10e0a6eb215276819",
        88646969,
    ),
    77: (
        "CARE_To_Compare/Wind Farm B/datasets/77.csv",
        "8ef8d082f0ff2f7c8c816aa15ba215e88cd2a2f407c521501ad9baafe56ccbd3",
        98677572,
    ),
    82: (
        "CARE_To_Compare/Wind Farm B/datasets/82.csv",
        "3f83609f4cac332834a3f1f08e8951d3fe0234da81ad85dcc02e979e3d6db781",
        87397445,
    ),
    83: (
        "CARE_To_Compare/Wind Farm B/datasets/83.csv",
        "a1df7aafd3792f66f515b245f5e09ea9d77e78ced3e84fede67e50cbf0219092",
        105007050,
    ),
    86: (
        "CARE_To_Compare/Wind Farm B/datasets/86.csv",
        "2ae869069d0ca3c3042c0310c2a85e48457aa3f6ae877ef76d08b5ab1dba7ccd",
        88573954,
    ),
    87: (
        "CARE_To_Compare/Wind Farm B/datasets/87.csv",
        "54c13391b96392f301dcf09f958df082a2c58d1b219aeafb82de67bda8674e1a",
        87964022,
    ),
}


@dataclass(frozen=True, slots=True)
class _SourceRow:
    timestamp: datetime
    source_index: int
    asset_id: str
    split: Literal["train", "prediction"]
    status: int
    values: tuple[float, ...]

    @property
    def finite_features(self) -> bool:
        return all(math.isfinite(value) for value in self.values)


@dataclass(frozen=True, slots=True)
class FarmBCareTask:
    """One private source task with separate fit/reference/evaluation roles."""

    task_id: str
    dataset_id: str
    split_id: str
    session_id: str
    family: Literal["PDM", "NRM"]
    train_values: pd.DataFrame
    train_source_indices: tuple[int, ...]
    train_times: tuple[str, ...]
    train_cutoff: str
    fit_start: str
    fit_end: str
    reference_start: str
    prediction_start: str
    prediction_end: str
    evaluation_values: pd.DataFrame
    evaluation_times: tuple[str, ...]
    evaluation_source_indices: tuple[int | None, ...]
    masked_samples: tuple[bool, ...]
    labels: tuple[bool, ...]
    failure_windows: tuple[tuple[int, int], ...]
    event_annotation_times: tuple[str, str]
    event_annotation_source_indices: tuple[int, int]
    sampling_period_s: int
    sliding_window: int
    source_member: str
    source_member_sha256: str
    source_archive_sha256: str
    source_manifest_sha256: str
    source_event_info_sha256: str
    source_feature_description_sha256: str
    source_revision: str = FARM_B_REVISION
    source_license: str = FARM_B_LICENSE
    attribution: str = (
        "CARE to Compare: A Real-World Benchmark for Early Fault Detection in Wind Turbine Data; "
        "Zenodo record 15846963 v6, DOI 10.5281/zenodo.15846963; CC-BY-SA-4.0."
    )
    access_terms: str = (
        "Noncommercial research only; retain attribution and CC-BY-SA-4.0 terms for adaptations."
    )
    usage_profile: Literal["noncommercial_research"] = USAGE_PROFILE

    @property
    def source_identity(self) -> str:
        """Opaque stable identity for Scorer profile/session registration."""
        return hashlib.sha256(
            f"{self.source_manifest_sha256}:{self.task_id}:{self.source_member_sha256}".encode()
        ).hexdigest()

    @property
    def semantics(self) -> dict[str, object]:
        return {
            "task_family": self.family,
            "sampling_s": self.sampling_period_s,
            "evaluation_times": list(self.evaluation_times),
            "masked_samples": list(self.masked_samples),
            "failure_windows": [list(value) for value in self.failure_windows],
        }

    @property
    def semantics_sha256(self) -> str:
        canonical = {
            "schema": "public-task-semantics.v1",
            **self.semantics,
        }
        return hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
                "ascii"
            )
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class FarmBCareSuite:
    """Full pinned 15-task Scorer-only Farm B bundle."""

    tasks: tuple[FarmBCareTask, ...]
    manifest_sha256: str
    source_archive_sha256: str
    source_event_info_sha256: str
    source_feature_description_sha256: str
    policy_revision: str = "care-farm-b-seven-day-reference-local-v1"
    usage_profile: Literal["noncommercial_research"] = USAGE_PROFILE

    @property
    def evaluation_rows(self) -> int:
        return sum(len(task.evaluation_values) for task in self.tasks)


def _hash_stream(source: IO[bytes]) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = source.read(1024 * 1024)
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise TypeError("hash source must yield bytes")
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _parse_naive_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("CARE Farm B timestamp is missing")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        raise ValueError("CARE Farm B timestamps must remain source-naive, not be assigned UTC")
    return parsed


def _parse_event_info(payload: bytes) -> dict[int, dict[str, object]]:
    if hashlib.sha256(payload).hexdigest() != FARM_B_EVENT_INFO_SHA256:
        raise ValueError("CARE Farm B event metadata hash differs from pinned source")
    text = payload.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    required = {
        "asset_id",
        "event_id",
        "event_label",
        "event_start",
        "event_start_id",
        "event_end",
        "event_end_id",
        "event_description",
    }
    if set(reader.fieldnames or ()) != required:
        raise ValueError("CARE Farm B event metadata schema is not the pinned source schema")
    result: dict[int, dict[str, object]] = {}
    for row in reader:
        event_id = int(row["event_id"])
        label = row["event_label"]
        if event_id in result or label not in {"anomaly", "normal"}:
            raise ValueError("CARE Farm B event metadata has duplicate or unknown event labels")
        start_time = _parse_naive_timestamp(row["event_start"])
        end_time = _parse_naive_timestamp(row["event_end"])
        start_id = int(row["event_start_id"])
        end_id = int(row["event_end_id"])
        if end_time < start_time or end_id < start_id:
            raise ValueError("CARE Farm B event metadata endpoints are reversed")
        result[event_id] = {
            "asset_id": row["asset_id"],
            "event_id": event_id,
            "event_label": label,
            "event_start": start_time,
            "event_start_id": start_id,
            "event_end": end_time,
            "event_end_id": end_id,
        }
    if set(result) != set(MEMBER_PINS):
        raise ValueError("CARE Farm B event metadata does not bind all 15 pinned tasks")
    return result


def _hash_member(archive: zipfile.ZipFile, member: str, expected_size: int) -> str:
    info = archive.getinfo(member)
    if info.file_size != expected_size:
        raise ValueError("CARE Farm B CSV member size differs from pinned source")
    with archive.open(info, "r") as stream:
        digest, actual_size = _hash_stream(stream)
    if actual_size != expected_size:
        raise ValueError("CARE Farm B CSV member decompressed size changed")
    return digest


def _validate_farm_b_member(event_id: int, member: str) -> None:
    """Reject paths outside the exact Farm B payload allowlist."""
    expected = MEMBER_PINS.get(event_id)
    if (
        expected is None
        or member != expected[0]
        or not member.startswith("CARE_To_Compare/Wind Farm B/datasets/")
    ):
        raise ValueError("CARE Farm B loader refuses a non-pinned Farm B member")


def _read_source_rows(
    archive: zipfile.ZipFile,
    *,
    event_id: int,
    event: Mapping[str, object],
) -> list[_SourceRow]:
    member, expected_sha, expected_size = MEMBER_PINS[event_id]
    _validate_farm_b_member(event_id, member)
    info = archive.getinfo(member)
    if info.file_size != expected_size:
        raise ValueError("CARE Farm B CSV member size differs from pinned source")
    if _hash_member(archive, member, expected_size) != expected_sha:
        raise ValueError("CARE Farm B CSV member hash differs from pinned source")

    rows: list[_SourceRow] = []
    train_ended = False
    previous_time: datetime | None = None
    previous_source_index = -1
    expected_asset = str(event["asset_id"])
    with archive.open(info, "r") as stream:
        for frame in pd.read_csv(
            stream,
            sep=";",
            usecols=list(CSV_COLUMNS),
            dtype={
                "time_stamp": "string",
                "asset_id": "string",
                "id": "int64",
                "train_test": "string",
                "status_type_id": "string",
                **{name: "float64" for name in FEATURE_COLUMNS},
            },
            chunksize=CSV_CHUNK_ROWS,
            keep_default_na=True,
        ):
            if tuple(frame.columns) != CSV_COLUMNS:
                frame = frame.loc[:, list(CSV_COLUMNS)]
            for row in frame.itertuples(index=False, name=None):
                timestamp = _parse_naive_timestamp(str(row[0]))
                asset = str(row[1])
                source_index = int(row[2])
                split_value = str(row[3])
                if split_value not in {"train", "prediction"}:
                    raise ValueError("CARE Farm B has an undocumented split value")
                if split_value == "prediction":
                    train_ended = True
                elif train_ended:
                    raise ValueError(
                        "CARE Farm B split is not a train prefix followed by prediction"
                    )
                if asset != expected_asset:
                    raise ValueError("CARE Farm B row asset differs from pinned event metadata")
                if source_index != previous_source_index + 1:
                    raise ValueError(
                        "CARE Farm B source row identifiers are not contiguous and ordered"
                    )
                if previous_time is not None:
                    delta = int((timestamp - previous_time).total_seconds())
                    if delta <= 0 or delta % SAMPLING_SECONDS:
                        raise ValueError(
                            "CARE Farm B source timestamps violate the 600-second grid"
                        )
                previous_time = timestamp
                previous_source_index = source_index
                status_text = row[4]
                try:
                    status = int(str(status_text))
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        "CARE Farm B has a missing or malformed operator status"
                    ) from exc
                if status not in DOCUMENTED_STATUSES:
                    raise ValueError("CARE Farm B has an undocumented status code")
                values = tuple(float(value) for value in row[5:])
                rows.append(
                    _SourceRow(
                        timestamp=timestamp,
                        source_index=source_index,
                        asset_id=asset,
                        split=split_value,  # type: ignore[arg-type]
                        status=status,
                        values=values,
                    )
                )
    if not rows or rows[0].source_index != 0 or not train_ended:
        raise ValueError("CARE Farm B CSV lacks its pinned train/prediction source roles")
    prediction_rows = [row for row in rows if row.split == "prediction"]
    if not prediction_rows:
        raise ValueError("CARE Farm B source prediction suffix is empty")
    if prediction_rows[0].timestamp != rows[len(rows) - len(prediction_rows)].timestamp:
        raise ValueError("CARE Farm B source split boundary is malformed")
    if any(
        right.timestamp - left.timestamp != timedelta(seconds=SAMPLING_SECONDS)
        for left, right in zip(prediction_rows, prediction_rows[1:], strict=False)
    ):
        raise ValueError("CARE Farm B prediction rows are not a complete 600-second grid")
    start = event["event_start"]
    end = event["event_end"]
    start_rows = [row for row in prediction_rows if row.source_index == event["event_start_id"]]
    end_rows = [row for row in prediction_rows if row.source_index == event["event_end_id"]]
    if (
        len(start_rows) != 1
        or len(end_rows) != 1
        or start_rows[0].timestamp != start
        or end_rows[0].timestamp != end
    ):
        raise ValueError("CARE Farm B event source IDs and timestamp endpoints do not align")
    return rows


def _longest_fit_segment(rows: Sequence[_SourceRow], *, cutoff: datetime) -> list[_SourceRow]:
    segments: list[list[_SourceRow]] = []
    current: list[_SourceRow] = []
    previous: _SourceRow | None = None
    for row in rows:
        if row.split != "train" or row.timestamp > cutoff:
            if current:
                segments.append(current)
                current = []
            previous = None
            continue
        eligible = row.status in NORMAL_STATUSES and row.finite_features
        if not eligible:
            if current:
                segments.append(current)
                current = []
            previous = None
            continue
        contiguous = previous is not None and (
            row.source_index == previous.source_index + 1
            and row.timestamp - previous.timestamp == timedelta(seconds=SAMPLING_SECONDS)
        )
        if current and not contiguous:
            segments.append(current)
            current = []
        current.append(row)
        previous = row
    if current:
        segments.append(current)
    best: list[_SourceRow] = []
    for segment in segments:
        if len(segment) > len(best):  # Strict > keeps the earliest segment on ties.
            best = segment
    if not best:
        raise ValueError("CARE Farm B has no contiguous healthy finite fit segment before cutoff")
    return best


def _grid_index(timestamp: datetime, start: datetime) -> int:
    delta = int((timestamp - start).total_seconds())
    if delta < 0 or delta % SAMPLING_SECONDS:
        raise ValueError("CARE Farm B observed timestamp is off the fixed reference grid")
    return delta // SAMPLING_SECONDS


def _materialize_task(
    *,
    event_id: int,
    event: Mapping[str, object],
    rows: Sequence[_SourceRow],
    source_member: str,
    source_member_sha256: str,
    source_archive_sha256: str,
    manifest_sha256: str,
    event_info_sha256: str = FARM_B_EVENT_INFO_SHA256,
    feature_description_sha256: str = FARM_B_FEATURE_DESCRIPTION_SHA256,
) -> FarmBCareTask:
    prior_time: datetime | None = None
    prior_index = -1
    train_ended = False
    for row in rows:
        if row.status not in DOCUMENTED_STATUSES:
            raise ValueError("CARE Farm B has an undocumented status code")
        if row.asset_id != str(event["asset_id"]):
            raise ValueError("CARE Farm B source row asset differs from event metadata")
        if row.source_index != prior_index + 1:
            raise ValueError("CARE Farm B source row order/identity is not contiguous")
        if row.split == "prediction":
            train_ended = True
        elif row.split != "train" or train_ended:
            raise ValueError("CARE Farm B source split is not a train prefix/prediction suffix")
        if prior_time is not None:
            delta = int((row.timestamp - prior_time).total_seconds())
            if delta <= 0 or delta % SAMPLING_SECONDS:
                raise ValueError("CARE Farm B source timestamp is off the 600-second grid")
        prior_time = row.timestamp
        prior_index = row.source_index
    prediction = [row for row in rows if row.split == "prediction"]
    if not prediction:
        raise ValueError("CARE Farm B prediction suffix is empty")
    prediction_start = prediction[0].timestamp
    prediction_end = prediction[-1].timestamp
    reference_start = prediction_start - timedelta(days=REFERENCE_DAYS)
    cutoff = reference_start - timedelta(seconds=EMBARGO_SECONDS)
    fit_rows = _longest_fit_segment(rows, cutoff=cutoff)
    fit_values = np.asarray([row.values for row in fit_rows], dtype=np.float64)
    if fit_values.ndim != 2 or not np.isfinite(fit_values).all():
        raise ValueError("CARE Farm B selected fit segment is not finite")
    window = derive_train_sliding_window(fit_values, minimum=MIN_WINDOW, maximum=MAX_WINDOW)
    if len(fit_rows) < max(MIN_FIT_ROWS_MULTIPLE * window, window + 1):
        raise ValueError("CARE Farm B has insufficient contiguous fit rows for its frozen window")
    medians = np.median(fit_values, axis=0)
    if not np.isfinite(medians).all():
        raise ValueError("CARE Farm B fit-only median is invalid")

    prefix_count = REFERENCE_DAYS * 24 * 60 * 60 // SAMPLING_SECONDS
    eval_count = int((prediction_end - reference_start).total_seconds() // SAMPLING_SECONDS)
    grid_count = eval_count + 1
    times = tuple(
        (reference_start + timedelta(seconds=SAMPLING_SECONDS * index)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        for index in range(grid_count)
    )
    matrix = np.tile(medians, (grid_count, 1)).astype(np.float64)
    masks = np.ones(grid_count, dtype=np.bool_)
    source_indices: list[int | None] = [None] * grid_count
    statuses = np.full(grid_count, -1, dtype=np.int8)
    expected_prefix_end = prediction_start
    if times[prefix_count] != prediction_start.strftime("%Y-%m-%d %H:%M:%S"):
        raise ValueError("CARE Farm B fixed seven-day reference does not join prediction start")

    for row in rows:
        if row.split == "train" and not (reference_start <= row.timestamp < prediction_start):
            continue
        if row.split == "prediction" and not (prediction_start <= row.timestamp <= prediction_end):
            continue
        if row.timestamp < reference_start or row.timestamp > prediction_end:
            continue
        index = _grid_index(row.timestamp, reference_start)
        if index >= grid_count or source_indices[index] is not None:
            raise ValueError("CARE Farm B source rows duplicate or exceed the fixed eval grid")
        source_indices[index] = row.source_index
        statuses[index] = row.status
        row_values = np.asarray(row.values, dtype=np.float64)
        finite = np.isfinite(row_values)
        matrix[index, finite] = row_values[finite]
        masks[index] = not (row.status in NORMAL_STATUSES and bool(finite.all()))

    # The reference prefix is source train role only; any missing expected slots
    # stay median-filled and fully masked. The full prediction suffix is exact.
    if any(index is None for index in source_indices[prefix_count:]):
        raise ValueError("CARE Farm B prediction rows do not cover the complete fixed grid")
    if any(
        source_indices[index] is not None and statuses[index] not in DOCUMENTED_STATUSES
        for index in range(grid_count)
    ):
        raise ValueError("CARE Farm B evaluation contains an undocumented source status")
    if matrix.shape != (prefix_count + len(prediction), len(FEATURE_COLUMNS)):
        raise ValueError(
            "CARE Farm B evaluation grid length differs from prefix plus full prediction"
        )
    if not np.isfinite(matrix).all():
        raise ValueError("CARE Farm B train-median placeholders did not make finite inputs")

    family: Literal["PDM", "NRM"] = "PDM" if event["event_label"] == "anomaly" else "NRM"
    event_start = cast(datetime, event["event_start"])
    event_end = cast(datetime, event["event_end"])
    event_start_id = cast(int, event["event_start_id"])
    event_end_id = cast(int, event["event_end_id"])
    start_index = _grid_index(event_start, reference_start)
    end_index = _grid_index(event_end, reference_start)
    if (
        start_index < prefix_count
        or end_index >= grid_count
        or times[start_index] != event_start.strftime("%Y-%m-%d %H:%M:%S")
        or times[end_index] != event_end.strftime("%Y-%m-%d %H:%M:%S")
    ):
        raise ValueError("CARE Farm B annotated source interval does not project onto eval grid")
    labels = np.zeros(grid_count, dtype=np.bool_)
    failures: tuple[tuple[int, int], ...] = ()
    if family == "PDM":
        failures = ((start_index, end_index),)
        labels[start_index : end_index + 1] = ~masks[start_index : end_index + 1]
        if not np.any(~masks[start_index : end_index + 1]):
            raise ValueError("CARE Farm B PDM has no unmasked positive support")
        healthy = [
            index
            for index in range(grid_count)
            if not masks[index] and not start_index <= index <= end_index
        ]
        if not healthy:
            raise ValueError("CARE Farm B PDM has no unmasked healthy reference/eval exposure")
    elif failures:
        raise AssertionError("NRM failure metadata must stay annotation-only")

    frame = pd.DataFrame(matrix, columns=list(FEATURE_COLUMNS))
    train_frame = pd.DataFrame(fit_values, columns=list(FEATURE_COLUMNS))
    if expected_prefix_end != prediction_start:
        raise AssertionError("unreachable fixed reference boundary mismatch")
    return FarmBCareTask(
        task_id=f"event-{event_id}",
        dataset_id="care-to-compare-farm-b-v6-local",
        split_id="Bholdout",
        session_id=f"farm-b-event-{event_id}",
        family=family,
        train_values=train_frame,
        train_source_indices=tuple(row.source_index for row in fit_rows),
        train_times=tuple(row.timestamp.strftime("%Y-%m-%d %H:%M:%S") for row in fit_rows),
        train_cutoff=cutoff.strftime("%Y-%m-%d %H:%M:%S"),
        fit_start=fit_rows[0].timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        fit_end=fit_rows[-1].timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        reference_start=reference_start.strftime("%Y-%m-%d %H:%M:%S"),
        prediction_start=prediction_start.strftime("%Y-%m-%d %H:%M:%S"),
        prediction_end=prediction_end.strftime("%Y-%m-%d %H:%M:%S"),
        evaluation_values=frame,
        evaluation_times=times,
        evaluation_source_indices=tuple(source_indices),
        masked_samples=tuple(bool(value) for value in masks),
        labels=tuple(bool(value) for value in labels),
        failure_windows=failures,
        event_annotation_times=(
            event_start.strftime("%Y-%m-%d %H:%M:%S"),
            event_end.strftime("%Y-%m-%d %H:%M:%S"),
        ),
        event_annotation_source_indices=(event_start_id, event_end_id),
        sampling_period_s=SAMPLING_SECONDS,
        sliding_window=window,
        source_member=source_member,
        source_member_sha256=source_member_sha256,
        source_archive_sha256=source_archive_sha256,
        source_manifest_sha256=manifest_sha256,
        source_event_info_sha256=event_info_sha256,
        source_feature_description_sha256=feature_description_sha256,
    )


def _suite_manifest_digest() -> str:
    payload = {
        "schema": "care-farm-b-private-holdout.v1",
        "record": FARM_B_RECORD_URL,
        "archive_sha256": FARM_B_ARCHIVE_SHA256,
        "source_manifest_sha256": FARM_B_SOURCE_MANIFEST_SHA256,
        "event_info_sha256": FARM_B_EVENT_INFO_SHA256,
        "feature_description_sha256": FARM_B_FEATURE_DESCRIPTION_SHA256,
        "member_pins": [
            {"event_id": event, "path": member, "sha256": digest, "bytes": size}
            for event, (member, digest, size) in sorted(MEMBER_PINS.items())
        ],
        "policy": {
            "revision": "care-farm-b-seven-day-reference-local-v1",
            "feature_columns": list(FEATURE_COLUMNS),
            "sampling_s": SAMPLING_SECONDS,
            "reference_days": REFERENCE_DAYS,
            "fit_embargo_seconds": EMBARGO_SECONDS,
            "fit_statuses": sorted(NORMAL_STATUSES),
            "evaluation_unmasked_statuses": sorted(NORMAL_STATUSES),
            "missing_feature_policy": "train-median fill; full-row mask; preserve observed zero",
            "missing_timestamp_policy": "fixed grid; train-median placeholder and full mask",
            "window_derivation": "autocorrelation <1/e; median lag; clip 10..100; fit segment only",
            "failure_window": "PDM event_info endpoints inclusive on extended grid; NRM none",
            "healthy_reference_label_scope": "reference is train-role, not guaranteed healthy",
            "usage_profile": USAGE_PROFILE,
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(canonical).hexdigest()


def _verify_feature_descriptions(payload: bytes) -> str:
    digest = hashlib.sha256(payload).hexdigest()
    if digest != FARM_B_FEATURE_DESCRIPTION_SHA256:
        raise ValueError("CARE Farm B feature description hash differs from pinned source")
    rows = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")), delimiter=";")
    described = {
        (row["sensor_name"], statistic.strip())
        for row in rows
        for statistic in row["statistics_type"].split(",")
    }
    for feature in FEATURE_COLUMNS:
        signal, statistic = feature.rsplit("_", 1)
        source_statistic = "average" if statistic == "avg" else statistic
        if (signal, source_statistic) not in described:
            raise ValueError("CARE Farm B fixed feature is not source-described")
    return digest


def load_care_farm_b_holdout(
    archive_path: Path = DEFAULT_ARCHIVE_PATH,
) -> FarmBCareSuite:
    """Load and verify the complete 15-task Farm B suite from the local ZIP.

    The archive is streamed once for its full SHA-256, and each selected CSV
    member is streamed for its member SHA-256 before chunked parsing. No Farm C
    payload is opened, and no rows, masks, windows, or features are selected by
    anomaly labels or measured prediction values.
    """
    if (
        not isinstance(archive_path, Path)
        or archive_path.is_symlink()
        or not archive_path.is_file()
    ):
        raise ValueError("CARE Farm B archive path is not a regular local file")
    if archive_path.stat().st_size != 5_503_439_673:
        raise ValueError("CARE Farm B archive size differs from pinned v6 source")
    with archive_path.open("rb") as raw:
        archive_digest, archive_bytes = _hash_stream(raw)
    if archive_bytes != 5_503_439_673 or archive_digest != FARM_B_ARCHIVE_SHA256:
        raise ValueError("CARE Farm B archive SHA-256 differs from pinned v6 source")

    suite_digest = _suite_manifest_digest()
    tasks: list[FarmBCareTask] = []
    with zipfile.ZipFile(archive_path, "r") as archive:
        event_info = _parse_event_info(archive.read(EVENT_INFO_MEMBER))
        feature_digest = _verify_feature_descriptions(archive.read(FEATURE_DESCRIPTION_MEMBER))
        if feature_digest != FARM_B_FEATURE_DESCRIPTION_SHA256:
            raise ValueError("CARE Farm B feature-description provenance changed")
        for event_id in sorted(MEMBER_PINS):
            member, expected_sha, expected_size = MEMBER_PINS[event_id]
            if archive.namelist().count(member) != 1:
                raise ValueError("CARE Farm B pinned CSV member is absent or duplicated")
            _validate_farm_b_member(event_id, member)
            event = event_info[event_id]
            rows = _read_source_rows(archive, event_id=event_id, event=event)
            task = _materialize_task(
                event_id=event_id,
                event=event,
                rows=rows,
                source_member=member,
                source_member_sha256=expected_sha,
                source_archive_sha256=archive_digest,
                manifest_sha256=suite_digest,
                event_info_sha256=FARM_B_EVENT_INFO_SHA256,
                feature_description_sha256=feature_digest,
            )
            if len(task.evaluation_values) != 1008 + sum(row.split == "prediction" for row in rows):
                raise ValueError("CARE Farm B task lost fixed-prefix or prediction timestamps")
            tasks.append(task)
    if (
        len(tasks) != 15
        or sum(task.family == "PDM" for task in tasks) != 6
        or sum(task.family == "NRM" for task in tasks) != 9
    ):
        raise ValueError("CARE Farm B suite task/family count differs from the pinned source")
    expected_eval_rows = 72_128 + 15 * 1008
    if sum(len(task.evaluation_values) for task in tasks) != expected_eval_rows:
        raise ValueError("CARE Farm B suite does not retain the complete 15-task eval grids")
    return FarmBCareSuite(
        tasks=tuple(tasks),
        manifest_sha256=suite_digest,
        source_archive_sha256=archive_digest,
        source_event_info_sha256=FARM_B_EVENT_INFO_SHA256,
        source_feature_description_sha256=FARM_B_FEATURE_DESCRIPTION_SHA256,
    )
