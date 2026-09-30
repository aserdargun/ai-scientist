"""Label-free executable suite manifest round trips and recomputes its family weights."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from harness.contracts import FitContext
from lab.api.registry import MAX_SUITE_MANIFEST_BYTES as API_MANIFEST_LIMIT
from lab.api.registry import SuiteRegistry
from lab.db.schema import metadata
from lab.director.contracts import SourceProvenance
from lab.director.suite import SuiteTask
from lab.director.suite_manifest import (
    MAX_SUITE_MATRIX_BYTES,
    load_suite_manifest,
    write_suite_manifest,
)
from lab.director.suite_weights import SuiteWeightInput, suite_weights
from lab.suite_limits import MAX_SUITE_MANIFEST_BYTES


def _engine():
    raw = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    engine = raw.execution_options(schema_translate_map={"lab": None, "scorer": None})
    return engine


def _suite() -> tuple[SuiteTask, ...]:
    identities = (
        ("S1", "EVT", "S1:process:EVT"),
        ("CARE", "PDM", "CARE:wind:PDM"),
        ("CARE", "NRM", "CARE:wind:NRM"),
        ("SMD", "EVT", "SMD:server:EVT"),
    )
    initial = []
    for index, (dataset, family, group) in enumerate(identities):
        split = "dev"
        session = f"session-{index}"
        columns = ("wind_speed", "power")
        initial.append(
            SuiteTask.from_frames(
                task_id=f"task-{index}",
                dataset_id=dataset,
                split_id=split,
                session_id=session,
                profile_sha256=f"{index + 1:064x}",
                family=family,
                task_weight=1.0,
                independent_family=group,
                label_tier="silver",
                provenance=SourceProvenance(
                    dataset_id=dataset,
                    split_id=split,
                    session_id=session,
                    source_manifest_sha256=f"{index + 5:064x}",
                    source_revision="fixed-test-revision",
                    license_id="test-license",
                    attribution="manifest roundtrip test",
                    access_terms="noncommercial research profile",
                    usage_profile="noncommercial_research",
                ),
                context=FitContext(
                    seed=0,
                    signals=columns,
                    regime_signals=("wind_speed",),
                    sampling_s=60,
                    time_budget_s=30.0,
                ),
                train=pd.DataFrame(np.arange(16, dtype=np.float64).reshape(8, 2), columns=columns),
                evaluation=pd.DataFrame(
                    np.arange(12, dtype=np.float64).reshape(6, 2), columns=columns
                ),
            )
        )
    allocation = suite_weights(
        tuple(
            SuiteWeightInput(
                task_id=f"{task.dataset_id}/{task.split_id}/{task.session_id}/{task.task_id}",
                task_type=task.family,
                label_tier=task.label_tier,
                family=task.independent_family,
            )
            for task in initial
        )
    )
    return tuple(
        replace(task, task_weight=weight)
        for task, weight in zip(initial, allocation.weights, strict=True)
    )


def test_public_suite_manifest_is_bounded_private_and_executes_from_scorer_profiles(
    tmp_path: Path,
) -> None:
    engine = _engine()
    metadata.create_all(engine)
    with engine.connect() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS scorer")
        connection.exec_driver_sql(
            "CREATE TABLE scorer.dataset_profiles ("
            "dataset_id TEXT, split_id TEXT, session_id TEXT, profile_sha256 TEXT, "
            "visibility TEXT, sample_count INTEGER, task_family TEXT)"
        )
    tasks = _suite()
    with engine.begin() as connection:
        for task in tasks:
            connection.exec_driver_sql(
                "INSERT INTO scorer.dataset_profiles VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    task.dataset_id,
                    task.split_id,
                    task.session_id,
                    task.profile_sha256,
                    "dev",
                    len(task._evaluation),
                    task.family,
                ),
            )
    path = tmp_path / "suite.json"
    byte_count, digest = write_suite_manifest(tasks, path, suite_version=2)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.stat().st_size == byte_count < MAX_SUITE_MANIFEST_BYTES
    assert len(digest) == 64
    raw = path.read_bytes()
    assert b'"labels"' not in raw.lower()
    assert b"timestamp" not in raw.lower()
    document, loaded, checked_digest = load_suite_manifest(path, engine)
    assert checked_digest == digest
    assert document.suite_version == 2
    assert document.weight_policy == "spec-3.2.5-appendix-c-waterfill.v1"
    assert len(loaded) == 4
    assert {task.family for task in loaded} == {"EVT", "PDM", "NRM"}
    assert sum(task.task_weight for task in loaded) == 1.0
    assert all(not task._train.flags.writeable for task in loaded)


def test_api_and_director_share_manifest_limit_without_raising_matrix_budget(
    tmp_path: Path,
) -> None:
    assert API_MANIFEST_LIMIT == MAX_SUITE_MANIFEST_BYTES == 96 * 1024**2
    assert MAX_SUITE_MATRIX_BYTES == 64 * 1024**2
    payload_path = tmp_path / "full-eval-suite.json"
    payload_size = 64 * 1024**2 + 1
    with payload_path.open("wb") as stream:
        stream.truncate(payload_size)
    payload_path.chmod(0o600)
    import hashlib

    digest = hashlib.sha256()
    with payload_path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    runtime_root = tmp_path.resolve()
    verified = SuiteRegistry((), runtime_root)
    assert verified.verify_file(
        payload_path.name, digest.hexdigest()
    ).stat().st_size == payload_size
