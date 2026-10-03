"""Immutable, bounded chronological inputs for unscored operating mode replay."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

import numpy as np
import pandas as pd
from pydantic import Field, model_validator

from .contracts import SCENARIOS, ModeConfig, StrictModel

MAX_TRAIN_ROWS = 4096
MAX_EVALUATION_ROWS = 65536
MAX_CHUNK_ROWS = 64
MAX_CHUNKS = 1024
MAX_SENSORS = 64
MAX_STREAM_OUTPUT_BYTES = 2 * 1024**2
MAX_MANIFEST_BYTES = 1024**2
MAX_TRAIN_BYTES = 8 * 1024**2
MAX_CHUNK_BYTES = 256 * 1024
MAX_INPUT_TOTAL_BYTES = 64 * 1024**2
_DIGEST = r"^[0-9a-f]{64}$"
_SENSOR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_FORBIDDEN = {"label", "labels", "anomaly", "is_anomaly", "changepoint", "timestamp", "datetime"}


def canonical_json(value: Any) -> bytes:
    """Canonical UTF-8 JSON used for every stream content identity."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def configuration_sha256(config: ModeConfig) -> str:
    """Hash the requested configuration, before the repetition seed is applied."""
    return hashlib.sha256(canonical_json(config.model_dump(mode="json"))).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate stream JSON key")
        result[key] = value
    return result


def strict_json(payload: bytes, *, limit: int) -> Any:
    """Bound bytes before parsing and reject duplicate keys/non-finite literals."""
    if len(payload) > limit:
        raise ValueError("stream JSON byte limit exceeded")

    def invalid(_value: str) -> None:
        raise ValueError("non-finite stream JSON literal")

    return json.loads(payload, object_pairs_hook=_pairs, parse_constant=invalid)


def _utc(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp.utcoffset() != timedelta(0):
        raise ValueError("stream timestamps must be UTC")
    return stamp


def validate_sensors(sensors: tuple[str, ...]) -> None:
    """Keep identities and labels outside the sensor-only sandbox channel."""
    if (
        not 1 <= len(sensors) <= MAX_SENSORS
        or len(set(sensors)) != len(sensors)
        or any(_SENSOR.fullmatch(name) is None or name.lower() in _FORBIDDEN for name in sensors)
    ):
        raise ValueError("invalid stream sensors")


class SyntheticStreamRequest(StrictModel):
    """A finite synthetic process; chunking never restarts its random generator."""

    scenario: str
    seed: int = Field(ge=0, le=2**32 - 1)
    train_rows: int = Field(default=192, ge=16, le=MAX_TRAIN_ROWS)
    evaluation_rows: int = Field(default=8192, ge=1, le=MAX_EVALUATION_ROWS)
    chunk_rows: int = Field(default=64, ge=1, le=MAX_CHUNK_ROWS)

    @model_validator(mode="after")
    def known_scenario(self) -> SyntheticStreamRequest:
        if self.scenario not in SCENARIOS:
            raise ValueError("unknown synthetic stream scenario")
        if (self.evaluation_rows + self.chunk_rows - 1) // self.chunk_rows > MAX_CHUNKS:
            raise ValueError("stream cannot exceed 1024 chunks")
        return self


class StreamFrame(StrictModel):
    """A checksummed frame at an exact position in the source timeline."""

    name: str = Field(pattern=r"^(train|chunk-[0-9]{5})\.json$")
    sha256: str = Field(pattern=_DIGEST)
    size_bytes: int = Field(ge=1, le=MAX_TRAIN_BYTES)
    row_offset: int = Field(ge=0, le=MAX_EVALUATION_ROWS - 1)
    rows: int = Field(ge=1, le=MAX_TRAIN_ROWS)
    start_utc: str
    end_utc: str


class StreamManifest(StrictModel):
    """Source identity and complete ordered input inventory, without labels."""

    schema_version: Literal["mode-stream-input.v1"] = "mode-stream-input.v1"
    sha256: str = Field(pattern=_DIGEST)
    source_id: str
    source_version: Literal["chronological-synthetic.v1"] = "chronological-synthetic.v1"
    entity: Literal["synthetic-process-1"] = "synthetic-process-1"
    sampling_seconds: Literal[1] = 1
    sensors: tuple[str, ...]
    request: SyntheticStreamRequest
    request_sha256: str = Field(pattern=_DIGEST)
    scoring_available: Literal[False] = False
    train: StreamFrame
    chunks: tuple[StreamFrame, ...] = Field(min_length=1, max_length=MAX_CHUNKS)

    @model_validator(mode="after")
    def verify_content(self) -> StreamManifest:
        validate_sensors(self.sensors)
        request = self.request
        if (
            self.request_sha256
            != hashlib.sha256(canonical_json(request.model_dump(mode="json"))).hexdigest()
            or self.source_id != f"synthetic.{request.scenario}"
        ):
            raise ValueError("stream source/request identity mismatch")
        if self.train.name != "train.json" or self.train.rows != request.train_rows:
            raise ValueError("invalid training frame identity")
        if self.train.row_offset != 0:
            raise ValueError("invalid training offset")
        start = _utc(self.train.start_utc)
        if _utc(self.train.end_utc) != start + timedelta(seconds=request.train_rows - 1):
            raise ValueError("invalid training chronology")
        offset = 0
        total_bytes = self.train.size_bytes
        for index, chunk in enumerate(self.chunks):
            if (
                chunk.name != f"chunk-{index:05d}.json"
                or chunk.row_offset != offset
                or chunk.rows != min(request.chunk_rows, request.evaluation_rows - offset)
                or not 1 <= chunk.rows <= MAX_CHUNK_ROWS
                or chunk.size_bytes > MAX_CHUNK_BYTES
                or _utc(chunk.start_utc) != start + timedelta(seconds=request.train_rows + offset)
                or _utc(chunk.end_utc)
                != start + timedelta(seconds=request.train_rows + offset + chunk.rows - 1)
            ):
                raise ValueError("stream frames overlap or have invalid ordering/bounds")
            total_bytes += chunk.size_bytes
            offset += chunk.rows
        if offset != request.evaluation_rows or total_bytes > MAX_INPUT_TOTAL_BYTES:
            raise ValueError("stream total input bound mismatch")
        if (
            self.sha256
            != hashlib.sha256(
                canonical_json(self.model_dump(mode="json", exclude={"sha256"}))
            ).hexdigest()
        ):
            raise ValueError("stream manifest hash mismatch")
        return self

    @property
    def source_identity(self) -> dict[str, Any]:
        """Public source semantics, suitable for report/continuation binding."""
        return {
            "source_id": self.source_id,
            "source_version": self.source_version,
            "entity": self.entity,
            "sampling_seconds": self.sampling_seconds,
            "sensors": list(self.sensors),
            "request_sha256": self.request_sha256,
            "scoring_available": False,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @property
    def source_kind(self) -> Literal["synthetic"]:
        return "synthetic"

    @property
    def source_summary(self) -> None:
        return None

    @property
    def context_sampling_seconds(self) -> int:
        return 1


def _database_timestamp(value: str | datetime) -> pd.Timestamp:
    """Keep UTC instants, including nanoseconds, without synthesizing a clock."""
    if not isinstance(value, (str, datetime)):
        raise ValueError("database stream timestamps must be UTC strings or datetimes")
    if isinstance(value, str) and re.search(r"\.\d{10,}", value):
        raise ValueError("database stream timestamp precision exceeds nanoseconds")
    try:
        stamp = pd.Timestamp(value)
        if pd.isna(stamp) or stamp.tzinfo is None or stamp.utcoffset() != timedelta(0):
            raise ValueError("database stream timestamps must be UTC")
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("invalid database stream UTC timestamp") from exc
    return stamp


def _gap_before(previous: pd.Timestamp, current: pd.Timestamp, sampling: float | None) -> bool:
    if sampling is None:
        return False

    # Integer calendar arithmetic preserves subsecond precision without the 292-year
    # range restriction of pandas' nanosecond Timedelta representation.
    def ticks(stamp: pd.Timestamp) -> int:
        seconds = ((stamp.toordinal() * 24 + stamp.hour) * 60 + stamp.minute) * 60 + stamp.second
        return seconds * 10**9 + stamp.microsecond * 1000 + stamp.nanosecond

    return (ticks(current) - ticks(previous)) / 10**9 > sampling


class DatabaseStreamRequest(StrictModel):
    """A bounded, half-open source selection; the limit includes training rows."""

    source_id: str = Field(min_length=1)
    sensors: tuple[str, ...]
    start_utc: str
    end_utc: str
    entity: str | None
    row_limit: int = Field(ge=17, le=MAX_EVALUATION_ROWS)
    train_rows: int = Field(ge=16, le=MAX_TRAIN_ROWS)
    chunk_rows: int = Field(ge=1, le=MAX_CHUNK_ROWS)

    @model_validator(mode="after")
    def selection(self) -> DatabaseStreamRequest:
        validate_sensors(self.sensors)
        if (
            _database_timestamp(self.start_utc) >= _database_timestamp(self.end_utc)
            or self.train_rows >= self.row_limit
        ):
            raise ValueError("invalid database stream selection bounds")
        return self


class _DatabaseSourceDefinition(StrictModel):
    source_id: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    entity: str | None
    sensors: tuple[str, ...]
    units: tuple[str, ...]
    sampling_seconds: float | None = Field(gt=0)
    source_definition_sha256: str = Field(pattern=_DIGEST)
    local_export_allowed: bool

    @model_validator(mode="after")
    def sensor_units(self) -> _DatabaseSourceDefinition:
        validate_sensors(self.sensors)
        if self.units and len(self.units) != len(self.sensors):
            raise ValueError("database stream sensor units mismatch")
        return self


class DatabaseStreamManifest(_DatabaseSourceDefinition):
    """Private source provenance plus an immutable inventory of actual UTC rows."""

    schema_version: Literal["mode-stream-input.v2"] = "mode-stream-input.v2"
    sha256: str = Field(pattern=_DIGEST)
    request: DatabaseStreamRequest
    request_sha256: str = Field(pattern=_DIGEST)
    timeline_sha256: str = Field(pattern=_DIGEST)
    gap_count: int | None = Field(ge=0, lt=MAX_EVALUATION_ROWS)
    scoring_available: Literal[False] = False
    train: StreamFrame
    chunks: tuple[StreamFrame, ...] = Field(min_length=1, max_length=MAX_CHUNKS)

    @model_validator(mode="after")
    def verify_content(self) -> DatabaseStreamManifest:
        request = self.request
        if (
            request.source_id != self.source_id
            or request.sensors != self.sensors
            or request.entity != self.entity
            or self.request_sha256
            != hashlib.sha256(canonical_json(request.model_dump(mode="json"))).hexdigest()
        ):
            raise ValueError("database stream source/request identity mismatch")
        if (
            self.train.name != "train.json"
            or self.train.rows != request.train_rows
            or self.train.row_offset != 0
        ):
            raise ValueError("invalid database stream training identity")
        start, end = _database_timestamp(request.start_utc), _database_timestamp(request.end_utc)
        previous = None
        offset = 0
        total_bytes = self.train.size_bytes
        for index, ref in enumerate((self.train, *self.chunks)):
            first, last = _database_timestamp(ref.start_utc), _database_timestamp(ref.end_utc)
            if (
                not start <= first <= last < end
                or (first == last) != (ref.rows == 1)
                or previous is not None
                and first <= previous
            ):
                raise ValueError("database stream frames overlap or have invalid chronology")
            previous = last
            if index:
                if (
                    ref.name != f"chunk-{index - 1:05d}.json"
                    or ref.row_offset != offset
                    or ref.rows > request.chunk_rows
                    or index < len(self.chunks)
                    and ref.rows != request.chunk_rows
                    or ref.size_bytes > MAX_CHUNK_BYTES
                ):
                    raise ValueError("invalid database stream chunk ordering/bounds")
                total_bytes += ref.size_bytes
                offset += ref.rows
        if (
            request.train_rows + offset > request.row_limit
            or total_bytes > MAX_INPUT_TOTAL_BYTES
            or (self.gap_count is None) != (self.sampling_seconds is None)
            or self.gap_count is not None
            and self.gap_count >= request.train_rows + offset
        ):
            raise ValueError("database stream total input/gap bound mismatch")
        if (
            self.sha256
            != hashlib.sha256(
                canonical_json(self.model_dump(mode="json", exclude={"sha256"}))
            ).hexdigest()
        ):
            raise ValueError("database stream manifest hash mismatch")
        return self

    @property
    def source_kind(self) -> Literal["private_database"]:
        return "private_database"

    @property
    def context_sampling_seconds(self) -> None:
        # Declared cadence is descriptive and does not prove equally spaced rows.
        return None

    @property
    def source_summary(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_version": self.source_version,
            "entity": self.entity,
            "units": list(self.units),
            "first_utc": self.train.start_utc,
            "train_end_utc": self.train.end_utc,
            "last_utc": self.chunks[-1].end_utc,
            "sampling_seconds": self.sampling_seconds,
            "timeline_sha256": self.timeline_sha256,
            "source_definition_sha256": self.source_definition_sha256,
            "gap_count": self.gap_count,
            "alarm_basis": "observed_rows",
            "local_export_allowed": self.local_export_allowed,
        }

    @property
    def source_identity(self) -> dict[str, Any]:
        return {
            **self.source_summary,
            "source_kind": self.source_kind,
            "sensors": list(self.sensors),
            "request_sha256": self.request_sha256,
            "scoring_available": False,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


StreamInputManifest = StreamManifest | DatabaseStreamManifest


def _admit(check: Callable[[], None] | None) -> None:
    if check is not None:
        check()


def _root(root: Path, *, create: bool = False) -> Path:
    root = root.absolute()
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise ValueError("stream input directory cannot use symlinks")
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not root.is_dir():
        raise ValueError("stream input root is missing")
    return root


def _read(path: Path, limit: int, expected_size: int | None = None) -> bytes:
    """Read at most limit+1 bytes from one ordinary unlinked-to-other-files inode."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > limit
            or expected_size is not None
            and info.st_size != expected_size
        ):
            raise ValueError("invalid stream file type or byte bound")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read(limit + 1)
        after = os.fstat(descriptor)
        if len(payload) != info.st_size or after != info:
            # atime can change on a read; compare only fields that indicate content changes.
            if (
                len(payload) != info.st_size
                or after.st_size != info.st_size
                or after.st_mtime_ns != info.st_mtime_ns
                or after.st_ctime_ns != info.st_ctime_ns
            ):
                raise ValueError("stream file changed while reading")
        return payload
    finally:
        os.close(descriptor)


def _manifest(
    root: Path, sha256: str, admission_check: Callable[[], None] | None = None
) -> tuple[Path, StreamInputManifest]:
    _admit(admission_check)
    if re.fullmatch(_DIGEST, sha256) is None:
        raise ValueError("invalid stream input identity")
    directory = _root(root) / sha256
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("invalid stream input directory")
    _admit(admission_check)
    payload = _read(directory / "manifest.json", MAX_MANIFEST_BYTES)
    _admit(admission_check)
    value = strict_json(payload, limit=MAX_MANIFEST_BYTES)
    _admit(admission_check)
    if not isinstance(value, dict):
        raise ValueError("invalid stream input manifest")
    result: StreamInputManifest
    match value.get("schema_version"):
        case "mode-stream-input.v1":
            result = StreamManifest.model_validate_json(canonical_json(value))
        case "mode-stream-input.v2":
            result = DatabaseStreamManifest.model_validate_json(canonical_json(value))
        case _:
            raise ValueError("unsupported stream input manifest version")
    _admit(admission_check)
    if result.sha256 != sha256 or canonical_json(result.to_dict()) != payload:
        raise ValueError("stream input manifest identity mismatch")
    return directory, result


def _frame_with_timeline(
    directory: Path,
    manifest: StreamInputManifest,
    ref: StreamFrame,
    admission_check: Callable[[], None] | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    if isinstance(manifest, DatabaseStreamManifest):
        return _database_frame(directory, manifest, ref, admission_check)
    limit = MAX_TRAIN_BYTES if ref.name == "train.json" else MAX_CHUNK_BYTES
    payload = _read(directory / ref.name, limit, ref.size_bytes)
    if hashlib.sha256(payload).hexdigest() != ref.sha256:
        raise ValueError("stream frame hash mismatch")
    value = strict_json(payload, limit=limit)
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "source_id",
        "entity",
        "sampling_seconds",
        "sensors",
        "row_offset",
        "timestamps_utc",
        "values",
    }:
        raise ValueError("invalid stream frame shape")
    stamps, rows = value["timestamps_utc"], value["values"]
    if (
        value["schema"] != "mode-stream-frame.v1"
        or value["source_id"] != manifest.source_id
        or value["entity"] != manifest.entity
        or type(value["sampling_seconds"]) is not int
        or value["sampling_seconds"] != manifest.sampling_seconds
        or value["sensors"] != list(manifest.sensors)
        or type(value["row_offset"]) is not int
        or value["row_offset"] != ref.row_offset
        or not isinstance(stamps, list)
        or len(stamps) != ref.rows
        or not isinstance(rows, list)
        or len(rows) != ref.rows
    ):
        raise ValueError("stream frame identity mismatch")
    expected_start = _utc(ref.start_utc)
    for index, (stamp, row) in enumerate(zip(stamps, rows, strict=True)):
        if (
            not isinstance(stamp, str)
            or _utc(stamp) != expected_start + timedelta(seconds=index)
            or not isinstance(row, list)
            or len(row) != len(manifest.sensors)
            or any(item is not None and type(item) not in (float, int) for item in row)
        ):
            raise ValueError("invalid stream frame row/timestamp")
    data = pd.DataFrame(rows, columns=manifest.sensors, dtype=float)
    if np.isinf(data.to_numpy()).any():
        raise ValueError("infinite stream sensor value")
    return data, stamps


def _frame(directory: Path, manifest: StreamInputManifest, ref: StreamFrame) -> pd.DataFrame:
    return _frame_with_timeline(directory, manifest, ref)[0]


class _Timeline:
    """Incremental SHA256 of the canonical JSON array of every observed UTC stamp."""

    def __init__(self, sampling: float | None) -> None:
        self.sampling = sampling
        self.previous: pd.Timestamp | None = None
        self.count = 0
        self.gaps = 0
        self.digest = hashlib.sha256(b"[")

    def add(self, value: str) -> None:
        stamp = _database_timestamp(value)
        if value != stamp.isoformat() or self.previous is not None and stamp <= self.previous:
            raise ValueError("database stream timestamps must be canonical and strictly increasing")
        if self.previous is not None:
            self.gaps += _gap_before(self.previous, stamp, self.sampling)
            self.digest.update(b",")
        self.digest.update(canonical_json(value))
        self.previous = stamp
        self.count += 1

    @property
    def sha256(self) -> str:
        digest = self.digest.copy()
        digest.update(b"]")
        return digest.hexdigest()


def _database_frame(
    directory: Path,
    manifest: DatabaseStreamManifest,
    ref: StreamFrame,
    admission_check: Callable[[], None] | None,
) -> tuple[pd.DataFrame, list[str]]:
    _admit(admission_check)
    limit = MAX_TRAIN_BYTES if ref.name == "train.json" else MAX_CHUNK_BYTES
    payload = _read(directory / ref.name, limit, ref.size_bytes)
    _admit(admission_check)
    if hashlib.sha256(payload).hexdigest() != ref.sha256:
        raise ValueError("database stream frame hash mismatch")
    value = strict_json(payload, limit=limit)
    _admit(admission_check)
    bindings = {
        "schema": "mode-stream-frame.v2",
        "source_id": manifest.source_id,
        "source_version": manifest.source_version,
        "source_definition_sha256": manifest.source_definition_sha256,
        "entity": manifest.entity,
        "sampling_seconds": manifest.sampling_seconds,
        "sensors": list(manifest.sensors),
        "units": list(manifest.units),
        "row_offset": ref.row_offset,
    }
    if (
        not isinstance(value, dict)
        or set(value) != {*bindings, "timestamps_utc", "values"}
        or any(value[key] != expected for key, expected in bindings.items())
        or type(value["row_offset"]) is not int
        or value["sampling_seconds"] is not None
        and type(value["sampling_seconds"]) not in (int, float)
        or canonical_json(value) != payload
    ):
        raise ValueError("database stream frame identity mismatch")
    stamps, rows = value["timestamps_utc"], value["values"]
    if (
        not isinstance(stamps, list)
        or len(stamps) != ref.rows
        or not isinstance(rows, list)
        or len(rows) != ref.rows
        or stamps[0] != ref.start_utc
        or stamps[-1] != ref.end_utc
    ):
        raise ValueError("invalid database stream frame dimensions/timeline")
    timeline = _Timeline(manifest.sampling_seconds)
    for stamp, row in zip(stamps, rows, strict=True):
        _admit(admission_check)
        if (
            not isinstance(stamp, str)
            or not isinstance(row, list)
            or len(row) != len(manifest.sensors)
            or any(item is not None and type(item) not in (float, int) for item in row)
        ):
            raise ValueError("invalid database stream frame row/timestamp")
        timeline.add(stamp)
    _admit(admission_check)
    try:
        data = pd.DataFrame(rows, columns=manifest.sensors, dtype=float)
    except (OverflowError, TypeError) as exc:
        raise ValueError("invalid database stream sensor value") from exc
    if np.isinf(data.to_numpy()).any():
        raise ValueError("infinite database stream sensor value")
    _admit(admission_check)
    return data, stamps


def load_input(
    root: Path, sha256: str, *, admission_check: Callable[[], None] | None = None
) -> StreamInputManifest:
    """Fully verify persisted input at admission/resume, including every actual frame."""
    _admit(admission_check)
    directory, manifest = _manifest(root, sha256, admission_check)
    _admit(admission_check)
    if isinstance(manifest, DatabaseStreamManifest):
        timeline = _Timeline(manifest.sampling_seconds)
        for ref in (manifest.train, *manifest.chunks):
            _, stamps = _database_frame(directory, manifest, ref, admission_check)
            for stamp in stamps:
                _admit(admission_check)
                timeline.add(stamp)
        if timeline.sha256 != manifest.timeline_sha256 or manifest.gap_count != (
            timeline.gaps if manifest.sampling_seconds is not None else None
        ):
            raise ValueError("database stream full timeline hash/gap mismatch")
        _admit(admission_check)
        return manifest
    _frame(directory, manifest, manifest.train)
    for ref in manifest.chunks:
        _admit(admission_check)
        _frame(directory, manifest, ref)
    _admit(admission_check)
    return manifest


def _checked_directory(root: Path, manifest: StreamInputManifest) -> Path:
    directory, current = _manifest(root, manifest.sha256)
    if current != manifest:
        raise ValueError("stream input manifest changed")
    return directory


def load_train(root: Path, manifest: StreamInputManifest) -> pd.DataFrame:
    """Verify the manifest and bounded training frame, without rereading evaluation."""
    return _frame(_checked_directory(root, manifest), manifest, manifest.train)


def read_chunk(root: Path, manifest: StreamInputManifest, chunk_index: int) -> pd.DataFrame:
    """Verify one required frame and immutable manifest identity on each step."""
    if type(chunk_index) is not int or not 0 <= chunk_index < len(manifest.chunks):
        raise ValueError("invalid stream chunk index")
    return _frame(_checked_directory(root, manifest), manifest, manifest.chunks[chunk_index])


def read_chunk_timeline(
    root: Path, manifest: StreamInputManifest, chunk_index: int
) -> tuple[list[str], list[bool]]:
    """Verify actual stamps and the preceding frame; gaps never reset alarm state."""
    if type(chunk_index) is not int or not 0 <= chunk_index < len(manifest.chunks):
        raise ValueError("invalid stream chunk index")
    directory = _checked_directory(root, manifest)
    earlier = manifest.train if chunk_index == 0 else manifest.chunks[chunk_index - 1]
    _, prior_stamps = _frame_with_timeline(directory, manifest, earlier)
    _, stamps = _frame_with_timeline(directory, manifest, manifest.chunks[chunk_index])
    previous = _database_timestamp(prior_stamps[-1])
    gaps = []
    for value in stamps:
        current = _database_timestamp(value)
        if current <= previous:
            raise ValueError("database stream timestamps must be strictly increasing across frames")
        gaps.append(_gap_before(previous, current, manifest.sampling_seconds))
        previous = current
    return stamps, gaps


def iter_chunks(
    root: Path, manifest: StreamInputManifest
) -> Iterator[tuple[StreamFrame, pd.DataFrame]]:
    for index, ref in enumerate(manifest.chunks):
        yield ref, read_chunk(root, manifest, index)


def _synthetic_values(request: SyntheticStreamRequest) -> np.ndarray:
    count = request.train_rows + request.evaluation_rows
    clock = np.arange(count)
    noise = np.random.default_rng(request.seed).normal(size=(count, 5))
    latent = noise[:, 0] * 0.25
    if request.scenario == "healthy_multiple":
        latent += ((clock // 32) % 2) * 2
    if request.scenario == "healthy_load":
        latent += 0.8 * np.sin(clock / 14)
    data = np.column_stack(
        (
            latent + noise[:, 1] * 0.025,
            0.8 * latent + noise[:, 2] * 0.025,
            -0.5 * latent + noise[:, 3] * 0.025,
            np.full(count, 10.0),
        )
    )
    begin = request.train_rows + request.evaluation_rows // 3
    tail = count - begin
    match request.scenario:
        case "step":
            data[begin:, 0] += 1.5
        case "drift":
            data[begin:, 0] += np.linspace(0, 2, tail)
        case "variance":
            data[begin:, 0] += noise[begin:, 4]
        case "correlation_break":
            data[begin:, 1] = -0.8 * latent[begin:] + noise[begin:, 4] * 0.025
        case "oscillation_lag":
            data[begin:, 0] += np.sin(np.arange(tail) * 0.8)
            data[begin:, 1] = data[begin - 3 : count - 3, 1].copy()
        case "unseen_mode":
            data[begin:, :3] += np.array([2, -1, 1])
        case "sensor_quality":
            data[begin::3, 0] = np.nan
            data[begin + 1 :: 3, 1] = 0
            data[begin:, 3] = 11
    return data


def _write(path: Path, payload: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o444)


def create_database_input(
    root: Path,
    *,
    source_definition: dict[str, Any],
    request: dict[str, Any],
    rows: Iterable[tuple[str | datetime, tuple[float | None, ...]]],
    admission_check: Callable[[], None] | None = None,
) -> DatabaseStreamManifest:
    """Capture bounded observed rows, publishing only a fully validated inventory.

    At most the current training frame or monitor chunk is retained in memory.
    Publishing an input grants no access: the API separately rechecks source scope
    and writes the owner's admission receipt.
    """
    _admit(admission_check)
    source_bytes, request_bytes = canonical_json(source_definition), canonical_json(request)
    strict_json(source_bytes, limit=MAX_MANIFEST_BYTES)
    strict_json(request_bytes, limit=MAX_MANIFEST_BYTES)
    definition = _DatabaseSourceDefinition.model_validate_json(source_bytes)
    selection = DatabaseStreamRequest.model_validate_json(request_bytes)
    if (
        definition.source_id != selection.source_id
        or definition.sensors != selection.sensors
        or definition.entity != selection.entity
    ):
        raise ValueError("database stream source/request identity mismatch")
    _admit(admission_check)
    start = _database_timestamp(selection.start_utc)
    end = _database_timestamp(selection.end_utc)
    root = _root(root, create=True)
    _admit(admission_check)
    temporary = Path(tempfile.mkdtemp(prefix=".stream-input-", dir=root))
    timeline = _Timeline(definition.sampling_seconds)
    refs: list[StreamFrame] = []
    stamps: list[str] = []
    values: list[list[float | None]] = []
    total_bytes = 0
    offset = 0

    def flush() -> None:
        nonlocal total_bytes, offset
        _admit(admission_check)
        training = not refs
        if not training and len(refs) > MAX_CHUNKS:
            raise ValueError("database stream cannot exceed 1024 chunks")
        name = "train.json" if training else f"chunk-{len(refs) - 1:05d}.json"
        payload = canonical_json(
            {
                "schema": "mode-stream-frame.v2",
                "source_id": definition.source_id,
                "source_version": definition.source_version,
                "source_definition_sha256": definition.source_definition_sha256,
                "entity": definition.entity,
                "sampling_seconds": definition.sampling_seconds,
                "sensors": definition.sensors,
                "units": definition.units,
                "row_offset": offset,
                "timestamps_utc": stamps,
                "values": values,
            }
        )
        total_bytes += len(payload)
        if (
            len(payload) > (MAX_TRAIN_BYTES if training else MAX_CHUNK_BYTES)
            or total_bytes > MAX_INPUT_TOTAL_BYTES
        ):
            raise ValueError("database stream input byte bound exceeded")
        ref = StreamFrame(
            name=name,
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            row_offset=offset,
            rows=len(stamps),
            start_utc=stamps[0],
            end_utc=stamps[-1],
        )
        _admit(admission_check)
        _write(temporary / name, payload)
        _admit(admission_check)
        refs.append(ref)
        if not training:
            offset += len(stamps)
        stamps.clear()
        values.clear()

    try:
        _admit(admission_check)
        iterator = iter(rows)
        while True:
            _admit(admission_check)
            try:
                raw = next(iterator)
            except StopIteration:
                _admit(admission_check)
                break
            _admit(admission_check)
            if timeline.count >= selection.row_limit:
                raise ValueError("database stream row limit exceeded")
            if (
                not isinstance(raw, (tuple, list))
                or len(raw) != 2
                or not isinstance(raw[1], (tuple, list))
                or len(raw[1]) != len(definition.sensors)
            ):
                raise ValueError("invalid database stream row shape")
            stamp = _database_timestamp(raw[0])
            if not start <= stamp < end:
                raise ValueError("database stream row outside half-open selection")
            numeric: list[float | None] = []
            for item in raw[1]:
                if item is None:
                    numeric.append(None)
                    continue
                if type(item) not in (float, int):
                    raise ValueError("database stream values must be numeric or null")
                try:
                    number = float(item)
                except OverflowError as exc:
                    raise ValueError("invalid database stream sensor value") from exc
                if not math.isfinite(number):
                    raise ValueError("non-finite database stream sensor value")
                numeric.append(number)
            _admit(admission_check)
            canonical_stamp = stamp.isoformat()
            timeline.add(canonical_stamp)
            stamps.append(canonical_stamp)
            values.append(numeric)
            if len(stamps) == (selection.train_rows if not refs else selection.chunk_rows):
                flush()
        if timeline.count <= selection.train_rows:
            raise ValueError("database stream requires training and monitor rows")
        if stamps:
            flush()
        _admit(admission_check)
        value = {
            **definition.model_dump(mode="json"),
            "schema_version": "mode-stream-input.v2",
            "request": selection.model_dump(mode="json"),
            "request_sha256": hashlib.sha256(
                canonical_json(selection.model_dump(mode="json"))
            ).hexdigest(),
            "timeline_sha256": timeline.sha256,
            "gap_count": timeline.gaps if definition.sampling_seconds is not None else None,
            "scoring_available": False,
            "train": refs[0].model_dump(mode="json"),
            "chunks": [ref.model_dump(mode="json") for ref in refs[1:]],
        }
        digest = hashlib.sha256(canonical_json(value)).hexdigest()
        manifest = DatabaseStreamManifest.model_validate_json(
            canonical_json({**value, "sha256": digest})
        )
        _admit(admission_check)
        encoded = canonical_json(manifest.to_dict())
        if len(encoded) > MAX_MANIFEST_BYTES:
            raise ValueError("stream manifest byte limit exceeded")
        _admit(admission_check)
        _write(temporary / "manifest.json", encoded)
        _admit(admission_check)
        if _read(temporary / "manifest.json", MAX_MANIFEST_BYTES, len(encoded)) != encoded:
            raise ValueError("database stream manifest changed before publication")
        _admit(admission_check)
        # Verify files before the only publication point, including their actual
        # complete timeline. The temporary directory is private to this capture.
        verified = _Timeline(manifest.sampling_seconds)
        for ref in (manifest.train, *manifest.chunks):
            _, actual_stamps = _database_frame(temporary, manifest, ref, admission_check)
            for actual in actual_stamps:
                _admit(admission_check)
                verified.add(actual)
        if verified.sha256 != manifest.timeline_sha256 or verified.gaps != timeline.gaps:
            raise ValueError("database stream full timeline changed before publication")
        _admit(admission_check)
        target = root / digest
        try:
            temporary.rename(target)
        except OSError:
            if not target.exists():
                raise
            _admit(admission_check)
            existing = load_input(root, digest, admission_check=admission_check)
            if not isinstance(existing, DatabaseStreamManifest):
                raise ValueError("database stream manifest version changed") from None
            return existing
        _admit(admission_check)
        directory_fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        _admit(admission_check)
        return manifest
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def create_synthetic_input(
    root: Path, request: SyntheticStreamRequest | Mapping[str, Any]
) -> StreamManifest:
    """Generate a single chronological process and publish an immutable inventory."""
    # Revalidate instances too: Pydantic model_copy(update=...) can skip field bounds.
    request = SyntheticStreamRequest.model_validate(
        request.model_dump(mode="json") if isinstance(request, SyntheticStreamRequest) else request
    )
    root = _root(root, create=True)
    data = _synthetic_values(request)
    sensors = ("sensor_a", "sensor_b", "sensor_c", "constant_sensor")
    # A deterministic synthetic clock, continuous across training and all chunks.
    epoch = datetime(2000, 1, 1, tzinfo=UTC) + timedelta(seconds=request.seed)
    temporary = Path(tempfile.mkdtemp(prefix=".stream-input-", dir=root))
    try:
        refs = []
        windows = [("train.json", 0, 0, request.train_rows)] + [
            (
                f"chunk-{index:05d}.json",
                offset,
                request.train_rows + offset,
                min(request.chunk_rows, request.evaluation_rows - offset),
            )
            for index, offset in enumerate(range(0, request.evaluation_rows, request.chunk_rows))
        ]
        for name, row_offset, start, rows in windows:
            stamps = [(epoch + timedelta(seconds=start + i)).isoformat() for i in range(rows)]
            payload = canonical_json(
                {
                    "schema": "mode-stream-frame.v1",
                    "source_id": f"synthetic.{request.scenario}",
                    "entity": "synthetic-process-1",
                    "sampling_seconds": 1,
                    "sensors": sensors,
                    "row_offset": row_offset,
                    "timestamps_utc": stamps,
                    "values": [
                        [float(v) if np.isfinite(v) else None for v in row]
                        for row in data[start : start + rows]
                    ],
                }
            )
            ref = StreamFrame(
                name=name,
                sha256=hashlib.sha256(payload).hexdigest(),
                size_bytes=len(payload),
                row_offset=row_offset,
                rows=rows,
                start_utc=stamps[0],
                end_utc=stamps[-1],
            )
            _write(temporary / name, payload)
            refs.append(ref.model_dump(mode="json"))
        value = {
            "schema_version": "mode-stream-input.v1",
            "source_id": f"synthetic.{request.scenario}",
            "source_version": "chronological-synthetic.v1",
            "entity": "synthetic-process-1",
            "sampling_seconds": 1,
            "sensors": sensors,
            "request": request.model_dump(mode="json"),
            "request_sha256": hashlib.sha256(
                canonical_json(request.model_dump(mode="json"))
            ).hexdigest(),
            "scoring_available": False,
            "train": refs[0],
            "chunks": refs[1:],
        }
        digest = hashlib.sha256(canonical_json(value)).hexdigest()
        manifest = StreamManifest.model_validate_json(canonical_json({**value, "sha256": digest}))
        encoded = canonical_json(manifest.to_dict())
        if len(encoded) > MAX_MANIFEST_BYTES:
            raise ValueError("stream manifest byte limit exceeded")
        _write(temporary / "manifest.json", encoded)
        target = root / digest
        try:
            temporary.rename(target)
        except OSError:
            if not target.exists():
                raise
            return cast(StreamManifest, load_input(root, digest))
        directory_fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return cast(StreamManifest, load_input(root, digest))
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
