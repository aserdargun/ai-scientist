"""Immutable mode-study inputs; experiment execution remains in Director/Scorer."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from lab.api.registry import SuiteEntry, SuiteRegistry
    from lab.operating_modes import SourceSnapshot

MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_SNAPSHOTS = 64


class SyntheticSnapshotRequest(BaseModel):
    """A bounded reproducible source recipe, with no caller-supplied labels."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    scenario: Literal[
        "healthy_single",
        "healthy_multiple",
        "healthy_load",
        "step",
        "drift",
        "variance",
        "correlation_break",
        "oscillation_lag",
        "unseen_mode",
        "sensor_quality",
    ] = "healthy_multiple"
    seed: int = Field(default=0, ge=0, le=2**32 - 1)
    train_rows: int = Field(default=192, ge=32, le=1024)
    evaluation_rows: int = Field(default=96, ge=16, le=1024)


class ModeGridRequest(BaseModel):
    """Explicit deterministic configurations; scored by the existing experiment ledger."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    idempotency_key: str = Field(min_length=16, max_length=128)
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    configurations: list[dict[str, Any]] = Field(min_length=1, max_length=35)
    wall_seconds: int = Field(default=600, ge=1, le=14_400)


def canonical_document(value: Any) -> bytes:
    """Stable JSON identity used by registry artifacts, never a substitute for a score."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def atomic_private(path: Path, data: bytes) -> None:
    """Publish immutable bytes atomically; conflicting retries never overwrite data."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("snapshot paths must not be symlinks")
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError("immutable snapshot artifact already has different bytes")
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != data:
                raise ValueError(
                    "concurrent snapshot publication changed immutable bytes"
                ) from None
    finally:
        os.unlink(temporary)


class ModeSnapshotStore:
    """Public features and evaluator-only labels live in separate private artifacts."""

    def __init__(self, root: Path):
        if root.is_symlink():
            raise ValueError("snapshot root cannot be a symlink")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = root.resolve(strict=True)

    def directory(self, digest: str) -> Path:
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("invalid snapshot identity")
        path = self.root / digest
        if path.is_symlink():
            raise ValueError("snapshot directory cannot be a symlink")
        return path

    def create_synthetic(self, request: SyntheticSnapshotRequest) -> dict[str, Any]:
        from lab.operating_modes import synthetic_snapshot

        case = synthetic_snapshot(
            request.scenario,
            seed=request.seed,
            train_rows=request.train_rows,
            evaluation_rows=request.evaluation_rows,
        )
        directory = self.directory(case.snapshot.sha256)
        if not directory.exists() and len(list(self.root.iterdir())) >= MAX_SNAPSHOTS:
            raise ValueError("snapshot registry is full")
        public = canonical_document(case.snapshot.to_dict())
        if len(public) > MAX_SNAPSHOT_BYTES:
            raise ValueError("snapshot exceeds its byte limit")
        evaluator = canonical_document(
            {
                "schema": "operating-mode-evaluator.v1",
                "snapshot_sha256": case.snapshot.sha256,
                "scenario": case.scenario,
                "seed": case.seed,
                "generator": case.generator,
                "event_labels": case.event_labels,
                "quality_labels": case.quality_labels,
                "mode_labels": case.mode_labels,
            }
        )
        atomic_private(directory / "snapshot.json", public)
        atomic_private(directory / "evaluator.json", evaluator)
        atomic_private(
            directory / "manifest.json",
            canonical_document(
                {
                    "schema": "operating-mode-input.v1",
                    "snapshot_sha256": case.snapshot.sha256,
                    "snapshot_document_sha256": hashlib.sha256(public).hexdigest(),
                    "evaluator_document_sha256": hashlib.sha256(evaluator).hexdigest(),
                }
            ),
        )
        return self.describe(case.snapshot.sha256)

    def load(self, digest: str) -> SourceSnapshot:
        from lab.operating_modes import SourceSnapshot, snapshot_from_frame

        directory = self.directory(digest)
        path = directory / "snapshot.json"
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SNAPSHOT_BYTES:
            raise ValueError("snapshot artifact is unavailable or oversized")
        raw = path.read_bytes()
        manifest = json.loads((directory / "manifest.json").read_bytes())
        if hashlib.sha256(raw).hexdigest() != manifest.get("snapshot_document_sha256"):
            raise ValueError("snapshot document hash does not match its immutable manifest")
        snapshot = SourceSnapshot.model_validate_json(raw, strict=True)
        rebuilt = snapshot_from_frame(
            snapshot.frame(),
            snapshot.timestamps_utc,
            snapshot.selection,
            train_rows=snapshot.train_rows,
        )
        if snapshot.sha256 != digest or rebuilt.sha256 != digest:
            raise ValueError("snapshot values or split differ from its immutable identity")
        return snapshot

    def describe(self, digest: str) -> dict[str, Any]:
        snapshot = self.load(digest)
        manifest = json.loads((self.directory(digest) / "manifest.json").read_bytes())
        return {
            "source_kind": manifest.get("source_kind", "synthetic"),
            "scoring_available": manifest.get("source_kind") != "private_database",
            "snapshot_sha256": digest,
            "source": snapshot.selection.model_dump(mode="json"),
            "sensors": list(snapshot.sensors),
            "rows": len(snapshot.values),
            "train_rows": snapshot.train_rows,
            "evaluation_rows": len(snapshot.values) - snapshot.train_rows,
            "first_utc": snapshot.timestamps_utc[0],
            "last_utc": snapshot.timestamps_utc[-1],
            "readiness": "ready"
            if self.installed(digest) is not None
            else "pending_scorer_installation",
        }

    def statistics(
        self, digest: str, partition: Literal["all", "train", "evaluation"]
    ) -> dict[str, Any]:
        from lab.analytics.statistics import summarize_dataset

        snapshot = self.load(digest)
        frame = snapshot.frame(partition)
        stamps = snapshot.timestamps_utc
        if partition == "train":
            stamps = stamps[: snapshot.train_rows]
        elif partition == "evaluation":
            stamps = stamps[snapshot.train_rows :]
        frame["timestamp"] = list(stamps)
        return summarize_dataset(
            frame,
            feature_columns=list(snapshot.sensors),
            timestamp_column="timestamp",
            max_lag=12,
            histogram_bins=12,
        ).model_dump(mode="json")

    def installed(self, digest: str) -> dict[str, Any] | None:
        directory = self.directory(digest)
        receipt = directory / "installed.json"
        if not receipt.exists():
            return None
        if receipt.is_symlink() or receipt.stat().st_size > 4096:
            raise ValueError("invalid installation receipt")
        record = json.loads(receipt.read_bytes())
        suite = directory / "suite.json"
        if (
            record.get("schema") != "mode-snapshot-installed.v1"
            or record.get("snapshot_sha256") != digest
            or suite.is_symlink()
            or not suite.is_file()
            or suite.stat().st_size > MAX_SNAPSHOT_BYTES
            or hashlib.sha256(suite.read_bytes()).hexdigest() != record.get("suite_manifest_sha256")
        ):
            raise ValueError("installed suite differs from Scorer receipt")
        return cast(dict[str, Any], record)


def register_grid(
    store: ModeSnapshotStore, request: ModeGridRequest, registry_path: Path, runtime_root: Path
) -> tuple[SuiteRegistry, SuiteEntry]:
    """Persist one immutable provider entry using the existing private suite registry."""
    import fcntl

    from lab.api.registry import SuiteEntry, SuiteRegistryFile, load_suite_registry
    from lab.director.journal import canonical_bytes
    from lab.director.parameter_grid import grid_document

    installed = store.installed(request.snapshot_sha256)
    if installed is None:
        raise ValueError("snapshot has not been installed by Scorer")
    document = grid_document(request.snapshot_sha256, request.configurations)
    encoded = canonical_bytes(document)
    digest = hashlib.sha256(encoded).hexdigest()
    grid_path = store.directory(request.snapshot_sha256) / f"grid-{digest}.json"
    atomic_private(grid_path, encoded)
    suite_id = f"mode-grid-{digest[:48]}"
    suite_document = json.loads((grid_path.parent / "suite.json").read_bytes())
    suite_document["suite_id"] = suite_id
    suite_bytes = canonical_document(suite_document)
    suite_path = grid_path.parent / f"suite-{digest}.json"
    atomic_private(suite_path, suite_bytes)
    entry = SuiteEntry(
        suite_id=suite_id,
        track="mode",
        program_version="mode-grid.v1",
        suite_manifest_path=str(suite_path.relative_to(runtime_root)),
        suite_manifest_sha256=hashlib.sha256(suite_bytes).hexdigest(),
        provider="mode-grid",
        scenario_path=str(grid_path.relative_to(runtime_root)),
        scenario_sha256=digest,
        provider_config_sha256=digest,
        proposal_limit=len(document["entries"]),
    )
    # Serialize updates across API processes. The registry itself is atomically replaced.
    lock_path = registry_path.with_suffix(".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        existing = load_suite_registry(registry_path, runtime_root)
        entries = dict(existing.entries)
        if entry.suite_id in entries and entries[entry.suite_id] != entry:
            raise ValueError("registered grid identity already has different bytes")
        entries[entry.suite_id] = entry
        record = SuiteRegistryFile(schema="lab-suite-registry.v1", suites=tuple(entries.values()))
        payload = canonical_document(record.model_dump(mode="json", by_alias=True))
        fd, temporary = tempfile.mkstemp(prefix=".registry-", dir=registry_path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, registry_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return load_suite_registry(registry_path, runtime_root), entry
    finally:
        os.close(descriptor)
