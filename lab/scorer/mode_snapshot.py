"""Scorer-only installation of a reproducible synthetic operating-mode snapshot."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Literal

from sqlalchemy import Connection, Engine, text

from harness.contracts import FitContext
from harness.public_data import derive_train_sliding_window
from lab.api.mode_experiments import ModeSnapshotStore, atomic_private, canonical_document
from lab.director.contracts import SourceProvenance
from lab.director.public_suite import (
    ScorerTaskRegistration,
    task_semantics_sha256,
)
from lab.director.suite import SuiteTask
from lab.director.suite_manifest import write_suite_manifest
from lab.operating_modes import synthetic_snapshot


def materialize_synthetic(
    store: ModeSnapshotStore, digest: str
) -> tuple[SuiteTask, ScorerTaskRegistration]:
    """Recompute evaluator labels from their recipe; apply a train-derived embargo."""
    snapshot = store.load(digest)
    directory = store.directory(digest)
    evaluator_bytes = (directory / "evaluator.json").read_bytes()
    manifest = json.loads((directory / "manifest.json").read_bytes())
    if hashlib.sha256(evaluator_bytes).hexdigest() != manifest["evaluator_document_sha256"]:
        raise ValueError("evaluator artifact differs from immutable manifest")
    evaluator = json.loads(evaluator_bytes)
    case = synthetic_snapshot(
        evaluator["scenario"],
        evaluator["seed"],
        train_rows=snapshot.train_rows,
        evaluation_rows=len(snapshot.values) - snapshot.train_rows,
    )
    expected = dict(
        schema="operating-mode-evaluator.v1",
        snapshot_sha256=case.snapshot.sha256,
        scenario=case.scenario,
        seed=case.seed,
        generator=case.generator,
        event_labels=case.event_labels,
        quality_labels=case.quality_labels,
        mode_labels=case.mode_labels,
    )
    if case.snapshot.sha256 != digest or canonical_document(expected) != evaluator_bytes:
        raise ValueError("synthetic source or evaluator labels differ from generator recipe")
    train = snapshot.frame("train")
    window = derive_train_sliding_window(train.to_numpy())
    evaluation = snapshot.frame("evaluation").iloc[window:].reset_index(drop=True)
    if len(evaluation) < window or len(train) <= window:
        raise ValueError("train-derived embargo leaves insufficient evaluation samples")
    times = tuple(snapshot.timestamps_utc[snapshot.train_rows + window :])
    masks = tuple(case.quality_labels[window:])
    labels = tuple(case.event_labels[window:])
    family: Literal["EVT", "NRM"] = "EVT" if any(labels) else "NRM"
    semantics = task_semantics_sha256(
        task_family=family,
        sampling_s=1,
        evaluation_times=times,
        masked_samples=masks,
        failure_windows=(),
    )
    profile = hashlib.sha256(
        canonical_document(
            dict(
                schema="mode-snapshot-profile.v1",
                snapshot_sha256=digest,
                sliding_window=window,
                embargo_samples=window,
                semantics_sha256=semantics,
            )
        )
    ).hexdigest()
    dataset, split, session = "synthetic-operating-modes", "train-window-embargo.v1", digest
    provenance = SourceProvenance(
        dataset_id=dataset,
        split_id=split,
        session_id=session,
        source_manifest_sha256=digest,
        source_revision=case.generator,
        license_id="project-generated-synthetic",
        attribution="AI Scientist synthetic generator",
        access_terms="Generated fixture; not public-data or industrial acceptance",
        usage_profile="noncommercial_research",
    )
    task = SuiteTask.from_frames(
        task_id=f"mode-{digest[:16]}",
        dataset_id=dataset,
        split_id=split,
        session_id=session,
        profile_sha256=profile,
        family=family,
        task_weight=1.0,
        independent_family="synthetic-operating-modes",
        provenance=provenance,
        context=FitContext(
            seed=0, signals=snapshot.sensors, regime_signals=(), sampling_s=1, time_budget_s=30
        ),
        train=train,
        evaluation=evaluation,
        evaluation_instants=times,
    )
    registration = ScorerTaskRegistration(
        dataset_id=dataset,
        split_id=split,
        session_id=session,
        sample_count=len(evaluation),
        sliding_window=window,
        embargo_seconds=window,
        profile_sha256=profile,
        task_family=family,
        sampling_s=1,
        labels=labels,
        evaluation_times=times,
        masked_samples=masks,
        failure_windows=(),
        semantics_sha256=semantics,
        visibility="dev",
    )
    return task, registration


def _execute_registration_rpc(connection: Connection, *, payload: bytes, digest: str) -> object:
    """Transport seam for CPU fixtures; production always uses the Scorer RPC."""
    return connection.execute(
        text("SELECT scorer.install_mode_snapshot(:bundle,:sha256)"),
        {"bundle": payload.decode("utf-8"), "sha256": digest},
    ).scalar_one()


def install_synthetic_registration(
    engine: Engine, registration: ScorerTaskRegistration
) -> dict[str, object]:
    """Call the narrow Scorer RPC; generic dataset installation is never a fallback."""
    if engine.dialect.name != "postgresql":
        raise ValueError("synthetic registration requires the PostgreSQL Scorer RPC")
    document = {"schema": "mode-snapshot-registration.v1", **asdict(registration)}
    payload = canonical_document(document)
    if len(payload) > 262144:
        raise ValueError("synthetic registration bundle exceeds bound")
    digest = hashlib.sha256(payload).hexdigest()
    expected: dict[str, object] = dict(
        schema="mode-snapshot-registration-receipt.v1",
        registration_sha256=digest,
        snapshot_sha256=registration.session_id,
        profile_sha256=registration.profile_sha256,
        semantics_sha256=registration.semantics_sha256,
        sample_count=registration.sample_count,
        sliding_window=registration.sliding_window,
    )
    with engine.begin() as connection:
        receipt = _execute_registration_rpc(connection, payload=payload, digest=digest)
        if receipt != expected:
            raise ValueError("Scorer synthetic registration receipt differs from exact request")
    return expected


def install_snapshot(engine: Engine, store: ModeSnapshotStore, digest: str) -> dict[str, object]:
    """Publish readiness only after committed, exactly checked Scorer registration."""
    task, registration = materialize_synthetic(store, digest)
    install_synthetic_registration(engine, registration)
    path = store.directory(digest) / "suite.json"
    _, suite_digest = write_suite_manifest(
        (task,), path, suite_id=f"mode-snapshot-{digest[:32]}", single_snapshot_study=True
    )
    receipt = dict(
        schema="mode-snapshot-installed.v1",
        snapshot_sha256=digest,
        profile_sha256=task.profile_sha256,
        suite_manifest_sha256=suite_digest,
        task_id=task.task_id,
        embargo_samples=registration.sliding_window,
        evaluation_rows=registration.sample_count,
    )
    atomic_private(store.directory(digest) / "installed.json", canonical_document(receipt))
    return receipt
