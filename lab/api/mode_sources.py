"""Owner-scoped PostgreSQL catalog; browser recipes never carry connection secrets."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from lab.api.mode_experiments import (
    MAX_SNAPSHOT_BYTES,
    MAX_SNAPSHOTS,
    ModeSnapshotStore,
    atomic_private,
    canonical_document,
)
from lab.operating_modes import SourceSelection, read_postgres_snapshot


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
        if path is None:
            return
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_mode & 0o077
            or path.stat().st_size > 65536
        ):
            raise ValueError("source catalog is unavailable")
        record = json.loads(path.read_bytes())
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
            self.entries[selection.source_id] = {**entry, "selection": selection}

    def available(self, owner: str) -> list[dict[str, Any]]:
        return [
            {
                "source_id": key,
                "sensors": entry["sensors"],
                "source_version": entry["selection"].source_version,
                "entity_required": entry["selection"].entity_column is not None,
                "row_limit": entry["selection"].row_limit,
            }
            for key, entry in self.entries.items()
            if owner in entry["owners"]
        ]

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
                ) if entry["selection"].units else (),
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
    if manifest.get("source_kind") != "private_database":
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
