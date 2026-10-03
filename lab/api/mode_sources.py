"""Owner-scoped PostgreSQL catalog; browser recipes never carry connection secrets."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from lab.api.mode_experiments import (
    MAX_SNAPSHOT_BYTES,
    MAX_SNAPSHOTS,
    ModeSnapshotStore,
    atomic_private,
    canonical_document,
)
from lab.operating_modes import SourceSelection, read_postgres_snapshot
from lab.operating_modes.stream import canonical_json, validate_sensors


class DatabaseStreamRequest(BaseModel):
    """One owner-bound capture, with a row cap covering both train and monitoring."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    idempotency_key: str = Field(min_length=16, max_length=128)
    source_id: str = Field(min_length=1, max_length=128)
    sensors: list[str] = Field(min_length=1, max_length=50)
    start_utc: str = Field(max_length=64)
    end_utc: str = Field(max_length=64)
    entity: str | None = Field(default=None, max_length=128)
    row_limit: int = Field(default=8192, ge=17, le=65536)
    train_rows: int = Field(default=192, ge=16, le=4096)
    chunk_rows: int = Field(default=64, ge=1, le=64)

    @model_validator(mode="after")
    def bounded(self) -> DatabaseStreamRequest:
        """Validate recipe coordinates before opening any database connection."""
        validate_sensors(tuple(self.sensors))
        start, end = _utc(self.start_utc), _utc(self.end_utc)
        if (
            start >= end
            or self.train_rows >= self.row_limit
            or (self.row_limit - self.train_rows + self.chunk_rows - 1) // self.chunk_rows > 1024
        ):
            raise ValueError("invalid database stream bounds")
        return self

    def recipe(self) -> dict[str, Any]:
        """Canonical content selection; retry identity is not a data feature."""
        value = self.model_dump(mode="json", exclude={"idempotency_key"})
        value["start_utc"] = _utc(self.start_utc).isoformat()
        value["end_utc"] = _utc(self.end_utc).isoformat()
        return value


class DatabaseStreamPolicy(BaseModel):
    """Separate operator opt-in; existing 4096-row snapshot rights do not widen."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    enabled: bool
    source_schema: str = Field(alias="schema", pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
    row_limit: int = Field(ge=17, le=65536)
    capture_seconds: int = Field(default=20, ge=1, le=30)
    local_export_allowed: bool = False


def _utc(value: str) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    offset = stamp.utcoffset()
    if pd.isna(stamp) or stamp.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise ValueError("source timestamps must carry UTC timezone")
    return stamp


def _private_bytes(path: Path, limit: int) -> tuple[bytes, tuple[int, ...]]:
    """Read a private ordinary inode, rejecting replacement or mutation during read."""
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("source configuration cannot use symlinks")
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_mode & 0o077
            or before.st_size > limit
        ):
            raise ValueError("source configuration is not a bounded private file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read(limit + 1)
        after = os.fstat(descriptor)

        def identity(info: os.stat_result) -> tuple[int, ...]:
            return (
                info.st_dev,
                info.st_ino,
                info.st_uid,
                info.st_mode,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )

        if len(payload) != before.st_size or identity(before) != identity(after):
            raise ValueError("source configuration changed while reading")
        if identity(path.stat(follow_symlinks=False)) != identity(after):
            raise ValueError("source configuration path changed while reading")
        return payload, identity(after)
    finally:
        os.close(descriptor)


class DatabaseSnapshotRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    source_id: str = Field(min_length=1, max_length=128)
    sensors: list[str] = Field(min_length=1, max_length=50)
    start_utc: str = Field(max_length=64)
    end_utc: str = Field(max_length=64)
    entity: str | None = Field(default=None, max_length=128)
    row_limit: int = Field(default=512, ge=32, le=4096)
    train_rows: int = Field(default=192, ge=16, le=4095)


class SourceCatalog:
    """Operator-provisioned catalog with read-only credentials and per-owner entries."""

    def __init__(self, path: Path | None):
        self.entries: dict[str, Any] = {}
        self.path = path
        if path is None:
            return
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_mode & 0o077
            or path.stat().st_size > 65536
        ):
            raise ValueError("source catalog is unavailable")
        record = json.loads(_private_bytes(path, 65536)[0])
        if record.get("schema") != "mode-source-catalog.v1" or len(record.get("sources", [])) > 32:
            raise ValueError("invalid source catalog")
        for entry in record["sources"]:
            selection = SourceSelection.model_validate_json(json.dumps(entry["selection"]))
            sensors = entry["sensors"]
            if (
                not isinstance(sensors, list)
                or not 1 <= len(sensors) <= 50
                or len(set(sensors)) != len(sensors)
                or (selection.units and len(selection.units) != len(sensors))
                or not entry.get("owners")
                or selection.source_id in self.entries
            ):
                raise ValueError("invalid source catalog entry")
            stream = (
                DatabaseStreamPolicy.model_validate(entry["stream"])
                if entry.get("stream") is not None
                else None
            )
            self.entries[selection.source_id] = {**entry, "selection": selection, "stream": stream}

    def available(self, owner: str) -> list[dict[str, Any]]:
        return [
            {
                "source_id": key,
                "sensors": entry["sensors"],
                "source_version": entry["selection"].source_version,
                "entity_required": entry["selection"].entity_column is not None,
                "row_limit": entry["selection"].row_limit,
                "stream": {
                    "row_limit": entry["stream"].row_limit,
                    "capture_seconds": entry["stream"].capture_seconds,
                    "local_export_allowed": entry["stream"].local_export_allowed,
                }
                if entry["stream"] is not None and entry["stream"].enabled
                else None,
            }
            for key, entry in self.entries.items()
            if owner in entry["owners"]
        ]

    def stream_binding(self, request: DatabaseStreamRequest, *, owner: str) -> dict[str, Any]:
        """Freeze exact selected scope and private credentials for one finite capture."""
        request = DatabaseStreamRequest.model_validate(request.model_dump(mode="json"))
        entry = self.entries.get(request.source_id)
        if entry is None or owner not in entry["owners"]:
            raise ValueError("source unavailable")
        policy, selection = entry["stream"], entry["selection"]
        if (
            policy is None
            or not policy.enabled
            or request.row_limit > policy.row_limit
            or not set(request.sensors).issubset(entry["sensors"])
            or selection.timestamp_column in request.sensors
            or selection.entity_column in request.sensors
            or (selection.entity_column is None) != (request.entity is None)
            or selection.entity is not None
            and request.entity != selection.entity
            or not selection.source_version
            or selection.source_version == "unspecified"
            or selection.start_utc is not None
            and _utc(request.start_utc) < _utc(selection.start_utc)
            or selection.end_utc is not None
            and _utc(request.end_utc) > _utc(selection.end_utc)
        ):
            raise ValueError("source stream recipe exceeds its explicit scope")
        secret = Path(entry["dsn_file"])
        dsn_bytes, dsn_identity = _private_bytes(secret, 4096)
        scope = {
            "selection": selection.model_dump(mode="json"),
            "sensors": entry["sensors"],
            "owners": entry["owners"],
            "stream": policy.model_dump(mode="json", by_alias=True),
        }
        scope_sha = hashlib.sha256(canonical_json(scope)).hexdigest()
        units = (
            [selection.units[entry["sensors"].index(sensor)] for sensor in request.sensors]
            if selection.units
            else []
        )
        return {
            "definition": {
                "source_id": selection.source_id,
                "source_version": selection.source_version,
                "entity": request.entity,
                "sensors": request.sensors,
                "units": units,
                "sampling_seconds": selection.sampling_seconds,
                "source_definition_sha256": scope_sha,
                "local_export_allowed": policy.local_export_allowed,
            },
            "relation": {
                "schema": policy.source_schema,
                "table": selection.table,
                "timestamp_column": selection.timestamp_column,
                "entity_column": selection.entity_column,
            },
            "dsn": dsn_bytes.decode("utf-8").strip(),
            "private_binding": hashlib.sha256(
                canonical_json(
                    {
                        "scope": scope_sha,
                        "dsn_path": str(secret),
                        "dsn_sha256": hashlib.sha256(dsn_bytes).hexdigest(),
                        "dsn_identity": dsn_identity,
                    }
                )
            ).hexdigest(),
            "capture_seconds": policy.capture_seconds,
        }

    def verify_stream_binding(
        self, request: DatabaseStreamRequest, *, owner: str, expected: str
    ) -> None:
        """Re-read operator scope before an input receipt may authorize a run."""
        fresh = SourceCatalog(self.path).stream_binding(request, owner=owner)
        if fresh["private_binding"] != expected:
            raise ValueError("source scope changed during capture")

    def capture(
        self, request: DatabaseSnapshotRequest, *, owner: str, store: ModeSnapshotStore
    ) -> dict[str, Any]:
        from sqlalchemy import create_engine

        entry = self.entries.get(request.source_id)
        if entry is None or owner not in entry["owners"]:
            raise ValueError("source unavailable")
        if (
            len(set(request.sensors)) != len(request.sensors)
            or not set(request.sensors).issubset(entry["sensors"])
            or request.row_limit > entry["selection"].row_limit
            or request.train_rows >= request.row_limit
        ):
            raise ValueError("source recipe exceeds configured limits")
        selection = entry["selection"].model_copy(
            update={
                "start_utc": request.start_utc,
                "end_utc": request.end_utc,
                "entity": request.entity,
                "row_limit": request.row_limit,
                "units": tuple(
                    entry["selection"].units[entry["sensors"].index(sensor)]
                    for sensor in request.sensors
                )
                if entry["selection"].units
                else (),
            }
        )
        secret = Path(entry["dsn_file"])
        if (
            secret.is_symlink()
            or not secret.is_file()
            or secret.stat().st_mode & 0o077
            or secret.stat().st_size > 4096
        ):
            raise ValueError("source unavailable")
        engine = create_engine(
            secret.read_text().strip(),
            pool_size=1,
            max_overflow=0,
            connect_args={"connect_timeout": 3},
        )
        try:
            snapshot = read_postgres_snapshot(
                engine, selection, tuple(request.sensors), train_rows=request.train_rows
            )
        finally:
            engine.dispose()
        directory = store.directory(snapshot.sha256)
        if not directory.exists() and len(list(store.root.iterdir())) >= MAX_SNAPSHOTS:
            raise ValueError("snapshot registry is full")
        public = canonical_document(snapshot.to_dict())
        if len(public) > MAX_SNAPSHOT_BYTES:
            raise ValueError("snapshot exceeds its byte limit")
        atomic_private(directory / "snapshot.json", public)
        atomic_private(
            directory / "manifest.json",
            canonical_document(
                {
                    "schema": "operating-mode-input.v1",
                    "snapshot_sha256": snapshot.sha256,
                    "snapshot_document_sha256": hashlib.sha256(public).hexdigest(),
                    "source_kind": "private_database",
                    "labels": "unavailable",
                }
            ),
        )
        owner_digest = hashlib.sha256(owner.encode()).hexdigest()
        atomic_private(
            directory / f"access-{owner_digest}.json",
            canonical_document({"owner_id": owner, "snapshot_sha256": snapshot.sha256}),
        )
        return store.describe(snapshot.sha256)


def authorize_snapshot(store: ModeSnapshotStore, digest: str, owner: str) -> None:
    directory = store.directory(digest)
    manifest = json.loads((directory / "manifest.json").read_bytes())
    if manifest.get("source_kind") not in {"private_database", "public_dev"}:
        return
    owner_digest = hashlib.sha256(owner.encode()).hexdigest()
    receipt = directory / f"access-{owner_digest}.json"
    if (
        receipt.is_symlink()
        or not receipt.is_file()
        or receipt.read_bytes()
        != canonical_document({"owner_id": owner, "snapshot_sha256": digest})
    ):
        raise ValueError("snapshot unavailable")
