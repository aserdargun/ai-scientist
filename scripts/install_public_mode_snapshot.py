"""Operator-only label-free import; existing Scorer registration is verified with SELECT only."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import Engine, create_engine, select, text

from lab.api.mode_experiments import (
    MAX_SNAPSHOTS,
    ModeGridRequest,
    ModeSnapshotStore,
    atomic_private,
    canonical_document,
    register_grid,
)
from lab.api.mode_sources import _private_bytes
from lab.api.registry import PublicDevStudy
from lab.db.schema import dataset_labels, dataset_profiles, dataset_task_semantics
from lab.director.public_suite import ScorerTaskRegistration
from lab.director.suite_manifest import SuiteManifest
from lab.operating_modes.public_snapshot import PUBLIC_SUITE_SHA256, materialize_public_snapshot


def verify_registration(engine: Engine, registration: ScorerTaskRegistration) -> None:
    """No install/admin fallback: absence or any difference requires separate operator setup."""
    identity = dict(
        dataset_id=registration.dataset_id,
        split_id=registration.split_id,
        session_id=registration.session_id,
    )
    expected_profile = dict(
        **identity,
        sample_count=registration.sample_count,
        sliding_window=registration.sliding_window,
        profile_sha256=registration.profile_sha256,
        task_family=registration.task_family,
        visibility="dev",
    )
    expected_semantics = dict(
        **identity,
        task_family=registration.task_family,
        sampling_s=registration.sampling_s,
        semantics_sha256=registration.semantics_sha256,
        evaluation_times_json=list(registration.evaluation_times),
        masked_samples_json=list(registration.masked_samples),
        failure_windows_json=[list(v) for v in registration.failure_windows],
    )
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(text("SET TRANSACTION READ ONLY"))
            connection.execute(text("SET LOCAL statement_timeout='10000ms'"))

        def where(table: Any) -> Any:
            return [table.c[name] == value for name, value in identity.items()]

        profile = (
            connection.execute(select(dataset_profiles).where(*where(dataset_profiles)))
            .mappings()
            .one_or_none()
        )
        semantics = (
            connection.execute(select(dataset_task_semantics).where(*where(dataset_task_semantics)))
            .mappings()
            .one_or_none()
        )
        labels = connection.execute(
            select(dataset_labels.c.sample_index, dataset_labels.c.is_anomaly)
            .where(*where(dataset_labels))
            .order_by(dataset_labels.c.sample_index)
            .limit(registration.sample_count + 1)
        ).all()
        if (
            profile is None
            or any(profile[name] != value for name, value in expected_profile.items())
            or semantics is None
            or any(semantics[name] != value for name, value in expected_semantics.items())
            or [(row.sample_index, row.is_anomaly) for row in labels]
            != list(enumerate(registration.labels))
        ):
            raise ValueError("exact public development Scorer registration is unavailable")


def import_snapshot(
    *,
    source_repository_root: Path,
    source_suite: Path,
    expected_sha256: str,
    engine: Engine,
    store: ModeSnapshotStore,
    owner: str,
    origin: Literal["local"],
    registry_path: Path,
    runtime_root: Path,
    configurations: list[dict[str, Any]],
) -> dict[str, Any]:
    snapshot, registration = materialize_public_snapshot(
        source_repository_root, source_suite, expected_sha256
    )
    policy = PublicDevStudy(
        owner_id=owner,
        origin=origin,
        snapshot_sha256=snapshot.sha256,
        binding_sha256=snapshot.binding.sha256,
        source_suite_manifest_sha256=expected_sha256,
        original_task_sha256=snapshot.binding.original_task_sha256,
        profile_sha256=snapshot.binding.original_task.profile_sha256,
        max_experiments=2,
        max_wall_seconds=600,
        model_tokens=0,
    )
    policy.verify_budget(len(configurations), 600, 0)
    verify_registration(engine, registration)
    if hashlib.sha256(source_suite.read_bytes()).hexdigest() != expected_sha256:
        raise ValueError("source suite changed before public import publication")
    directory = store.directory(snapshot.sha256)
    if not directory.exists() and len(list(store.root.iterdir())) >= MAX_SNAPSHOTS:
        raise ValueError("snapshot registry is full")
    public = canonical_document(snapshot.model_dump(mode="json", by_alias=True))
    atomic_private(directory / "snapshot.json", public)
    atomic_private(
        directory / "binding.json",
        canonical_document(snapshot.binding.model_dump(mode="json", by_alias=True)),
    )
    atomic_private(
        directory / "manifest.json",
        canonical_document(
            dict(
                schema="operating-mode-input.v1",
                source_kind="public_dev",
                snapshot_sha256=snapshot.sha256,
                snapshot_document_sha256=hashlib.sha256(public).hexdigest(),
            )
        ),
    )
    # Only one permitted change to the original task: the single-study weight.
    original = snapshot.binding.original_task.model_copy(update={"task_weight": 1.0})
    source = SuiteManifest(
        schema="director-suite.v1",
        suite_id=f"mode-public-{snapshot.sha256[:32]}",
        suite_version=1,
        tasks=(original,),
        weight_policy="single_snapshot_study.v1",
        family_cap=1.0,
        type_shares={original.family: 1.0},
        family_shares={original.independent_family: 1.0},
    )
    suite = canonical_document(source.model_dump(mode="json", by_alias=True))
    atomic_private(directory / "suite.json", suite)
    atomic_private(
        directory / "installed.json",
        canonical_document(
            dict(
                schema="mode-snapshot-installed.v1",
                snapshot_sha256=snapshot.sha256,
                binding_sha256=snapshot.binding.sha256,
                profile_sha256=registration.profile_sha256,
                semantics_sha256=registration.semantics_sha256,
                suite_manifest_sha256=hashlib.sha256(suite).hexdigest(),
                task_id=original.task_id,
                evaluation_rows=registration.sample_count,
            )
        ),
    )
    atomic_private(
        directory / f"access-{hashlib.sha256(owner.encode()).hexdigest()}.json",
        canonical_document(dict(owner_id=owner, snapshot_sha256=snapshot.sha256)),
    )
    _, entry = register_grid(
        store,
        ModeGridRequest(
            idempotency_key="operator-public-import-v1",
            snapshot_sha256=snapshot.sha256,
            configurations=configurations,
            wall_seconds=600,
        ),
        registry_path,
        runtime_root,
        public_dev_study=policy,
    )
    if hashlib.sha256(source_suite.read_bytes()).hexdigest() != expected_sha256:
        raise ValueError("source suite changed during public import")
    return {
        **store.describe(snapshot.sha256),
        "suite_id": entry.suite_id,
        "scorer_written": False,
        "source_labels_exported": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-repository-root", type=Path, required=True)
    parser.add_argument("--source-suite-manifest", type=Path, required=True)
    parser.add_argument("--expected-source-suite-sha256", default=PUBLIC_SUITE_SHA256)
    parser.add_argument("--scorer-dsn-file", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--registry-file", type=Path, required=True)
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--origin", choices=["local"], required=True)
    parser.add_argument("--configurations-file", type=Path, required=True)
    args = parser.parse_args()
    dsn = _private_bytes(args.scorer_dsn_file, 4096)[0].decode().strip()
    configurations = json.loads(_private_bytes(args.configurations_file, 128 * 1024)[0])
    engine = create_engine(dsn, hide_parameters=True)
    try:
        result = import_snapshot(
            source_repository_root=args.source_repository_root,
            source_suite=args.source_suite_manifest,
            expected_sha256=args.expected_source_suite_sha256,
            engine=engine,
            store=ModeSnapshotStore(args.runtime_root / "mode-snapshots"),
            owner=args.owner_id,
            origin=args.origin,
            registry_path=args.registry_file,
            runtime_root=args.runtime_root,
            configurations=configurations,
        )
        print(json.dumps(result, sort_keys=True))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
