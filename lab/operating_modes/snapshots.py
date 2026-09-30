"""Immutable bounded source snapshots and evaluator-only synthetic labels."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import Field, model_validator
from sqlalchemy import bindparam, column, select, table, text

from .contracts import SCENARIOS, Finite, StrictModel
from .model import MAX_ROWS, _frame


class SourceSelection(StrictModel):
    """Credential-free, allowlisted table and UTC selection recipe."""

    source_id: str = Field(min_length=1, max_length=128)
    source_version: str = Field(default="unspecified", max_length=128)
    table: str = Field(default="sensor_data", pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
    timestamp_column: str = Field(default="timestamp", pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
    entity_column: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
    entity: str | None = Field(default=None, max_length=128)
    start_utc: str | None = None
    end_utc: str | None = None
    row_limit: int = Field(default=4096, ge=2, le=4096)
    sampling_seconds: Finite | None = Field(default=None, gt=0)
    units: tuple[str, ...] = ()


class SourceSnapshot(StrictModel):
    """Values and split boundary are frozen; labels never enter this record."""

    version: Literal["sensor-snapshot.v1"] = "sensor-snapshot.v1"
    sha256: str
    selection: SourceSelection
    sensors: tuple[str, ...]
    timestamps_utc: tuple[str, ...]
    values: tuple[tuple[Finite | None, ...], ...]
    train_rows: int

    @model_validator(mode="after")
    def verify_content(self) -> SourceSnapshot:
        """Check imported artifacts just as strictly as locally constructed snapshots."""
        if not 2 <= self.train_rows < len(self.values) <= self.selection.row_limit:
            raise ValueError("invalid snapshot split or row bound")
        if not 1 <= len(self.sensors) <= 64 or len(set(self.sensors)) != len(self.sensors):
            raise ValueError("invalid snapshot sensors")
        if any(len(row) != len(self.sensors) for row in self.values):
            raise ValueError("snapshot row width mismatch")
        stamps = tuple(_utc(value) for value in self.timestamps_utc)
        if len(stamps) != len(self.values) or any(
            a > b for a, b in zip(stamps, stamps[1:], strict=False)
        ):
            raise ValueError("invalid snapshot timeline")
        payload = self.model_dump(mode="json", exclude={"sha256"})
        expected = hashlib.sha256(
            json.dumps(payload, sort_keys=True, allow_nan=False).encode()
        ).hexdigest()
        if self.sha256 != expected:
            raise ValueError("snapshot content hash mismatch")
        return self

    def frame(self, partition: Literal["all", "train", "evaluation"] = "all") -> pd.DataFrame:
        data = pd.DataFrame(self.values, columns=self.sensors, dtype=float)
        if partition == "train":
            return data.iloc[: self.train_rows].reset_index(drop=True)
        if partition == "evaluation":
            return data.iloc[self.train_rows :].reset_index(drop=True)
        return data

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def _utc(value: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    offset = stamp.utcoffset()
    if stamp.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise ValueError("timestamps must carry UTC timezone")
    if pd.isna(stamp):
        raise ValueError("timestamp cannot be missing")
    return stamp


def snapshot_from_frame(
    data: pd.DataFrame, timestamps: Any, selection: SourceSelection, *, train_rows: int
) -> SourceSnapshot:
    """Freeze chronological values; represent missing/nonfinite sensors as null."""
    values = _frame(data)
    if not 2 <= train_rows < len(data) <= selection.row_limit:
        raise ValueError(
            "snapshot needs at least two training rows and one evaluation row within limit"
        )
    stamps = tuple(_utc(value).isoformat() for value in timestamps)
    if len(stamps) != len(data) or any(a > b for a, b in zip(stamps, stamps[1:], strict=False)):
        raise ValueError("timestamps must match rows and be chronological")
    if selection.units and len(selection.units) != len(data.columns):
        raise ValueError("units must match sensors")
    payload = {
        "version": "sensor-snapshot.v1",
        "selection": selection.model_dump(mode="json"),
        "sensors": tuple(data.columns),
        "timestamps_utc": stamps,
        "values": tuple(tuple(float(v) if np.isfinite(v) else None for v in row) for row in values),
        "train_rows": train_rows,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()
    return SourceSnapshot.model_validate_json(
        json.dumps({"sha256": digest, **payload}, allow_nan=False)
    )


def read_postgres_snapshot(
    engine: Any, selection: SourceSelection, sensors: tuple[str, ...], *, train_rows: int
) -> SourceSnapshot:
    """Read an existing authorized engine in a bounded read-only transaction.

    Callers own credentials and source authorization. This function stores neither.
    It does not create a service, engine, source account, or schema.
    """
    if engine.dialect.name != "postgresql":
        raise ValueError("PostgreSQL source required")
    if (
        not 1 <= len(sensors) <= 64
        or len(set(sensors)) != len(sensors)
        or selection.timestamp_column in sensors
        or any(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", name) is None for name in sensors)
    ):
        raise ValueError("invalid sensor identifier")
    if selection.start_utc is None or selection.end_utc is None:
        raise ValueError("bounded UTC time window required")
    start, end = _utc(selection.start_utc), _utc(selection.end_utc)
    if start >= end or (selection.entity_column is None) != (selection.entity is None):
        raise ValueError("invalid time window or entity selection")
    names = list(
        dict.fromkeys(
            (
                selection.timestamp_column,
                *sensors,
                *((selection.entity_column,) if selection.entity_column else ()),
            )
        )
    )
    source = table(selection.table, *(column(name) for name in names))
    timestamp = source.c[selection.timestamp_column]
    selected = [source.c[name] for name in (selection.timestamp_column, *sensors)]
    query = select(*selected).where(timestamp >= bindparam("start"), timestamp < bindparam("end"))
    params: dict[str, Any] = {
        "start": start.to_pydatetime(),
        "end": end.to_pydatetime(),
        "limit": selection.row_limit + 1,
    }
    if selection.entity_column is not None:
        query = query.where(source.c[selection.entity_column] == bindparam("entity"))
        params["entity"] = selection.entity
    query = query.order_by(*selected).limit(bindparam("limit"))
    with engine.connect() as connection, connection.begin():
        connection.execute(text("SET TRANSACTION READ ONLY"))
        connection.execute(text("SET LOCAL statement_timeout = '5000ms'"))
        rows = connection.execute(query, params).fetchall()
    if len(rows) > selection.row_limit:
        raise ValueError("selection exceeds row limit; narrow the time window")
    frame = pd.DataFrame(rows, columns=[selection.timestamp_column, *sensors])
    return snapshot_from_frame(
        frame[list(sensors)], frame[selection.timestamp_column], selection, train_rows=train_rows
    )


@dataclass(frozen=True)
class SyntheticCase:
    """Evaluator-owned labels kept outside candidate/model snapshots."""

    snapshot: SourceSnapshot
    scenario: str
    seed: int
    event_labels: tuple[bool, ...]
    quality_labels: tuple[bool, ...]
    mode_labels: tuple[int, ...]
    generator: str = "operating-modes-synthetic.v1"


def synthetic_snapshot(
    scenario: str = "healthy_single",
    seed: int = 0,
    *,
    train_rows: int = 192,
    evaluation_rows: int = 96,
) -> SyntheticCase:
    """Ten reproducible scenarios, with healthy chronological training prefixes."""
    if (
        scenario not in SCENARIOS
        or not 16 <= train_rows
        or not 16 <= evaluation_rows
        or train_rows + evaluation_rows > MAX_ROWS
    ):
        raise ValueError("unknown scenario or invalid bounded synthetic dimensions")
    rng = np.random.default_rng(seed)
    count = train_rows + evaluation_rows
    time = np.arange(count)
    modes = (time // 32) % 2 if scenario == "healthy_multiple" else np.zeros(count, dtype=int)
    latent = rng.normal(0, 0.25, count)
    if scenario == "healthy_load":
        latent += 0.8 * np.sin(time / 14)
    latent += modes * 2
    data = np.column_stack(
        [
            latent + rng.normal(0, 0.025, count),
            0.8 * latent + rng.normal(0, 0.025, count),
            -0.5 * latent + rng.normal(0, 0.025, count),
            np.ones(count) * 10,
        ]
    )
    event, quality = np.zeros(count, dtype=bool), np.zeros(count, dtype=bool)
    begin = train_rows + evaluation_rows // 3
    tail = count - begin
    if scenario == "step":
        data[begin:, 0] += 1.5
    elif scenario == "drift":
        data[begin:, 0] += np.linspace(0, 2, tail)
    elif scenario == "variance":
        data[begin:, 0] += rng.normal(0, 1, tail)
    elif scenario == "correlation_break":
        data[begin:, 1] = -0.8 * latent[begin:] + rng.normal(0, 0.025, tail)
    elif scenario == "oscillation_lag":
        data[begin:, 0] += np.sin(np.arange(tail) * 0.8)
        data[begin:, 1] = data[begin - 3 : count - 3, 1].copy()
    elif scenario == "unseen_mode":
        data[begin:, :3] += np.array([2, -1, 1])
        modes[begin:] = 2
    elif scenario == "sensor_quality":
        data[begin::3, 0] = np.nan
        data[begin + 1 :: 3, 1] = 0
        data[begin:, 3] = 11
        quality[begin:] = True
    if scenario not in {"healthy_single", "healthy_multiple", "healthy_load", "sensor_quality"}:
        event[begin:] = True
    frame = pd.DataFrame(data, columns=["sensor_a", "sensor_b", "sensor_c", "constant_sensor"])
    timestamps = pd.date_range(datetime(2024, 1, 1), periods=count, freq="s", tz="UTC")
    snapshot = snapshot_from_frame(
        frame,
        timestamps,
        SourceSelection(
            source_id=f"synthetic.{scenario}", source_version=f"v1.seed{seed}", sampling_seconds=1.0
        ),
        train_rows=train_rows,
    )
    return SyntheticCase(
        snapshot,
        scenario,
        seed,
        tuple(bool(v) for v in event[train_rows:]),
        tuple(bool(v) for v in quality[train_rows:]),
        tuple(int(v) for v in modes[train_rows:]),
    )
