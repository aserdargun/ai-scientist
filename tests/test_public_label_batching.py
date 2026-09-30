"""Large trusted label vectors are inserted in bounded SQL batches."""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool

from lab.db.schema import dataset_labels, metadata
from lab.director.public_suite import ScorerTaskRegistration, install_scorer_task


def _engine():
    raw = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    return raw.execution_options(schema_translate_map={"lab": None, "scorer": None})


def test_large_public_label_registration_is_batched_and_idempotent() -> None:
    engine = _engine()
    metadata.create_all(engine)
    sample_count = 70_001
    labels = tuple(index % 113 == 0 for index in range(sample_count))
    registration = ScorerTaskRegistration(
        dataset_id="public-batch-fixture",
        split_id="full-evaluation-v2",
        session_id="session-1",
        sample_count=sample_count,
        sliding_window=16,
        embargo_seconds=0,
        profile_sha256="a" * 64,
        task_family="EVT",
        sampling_s=None,
        labels=labels,
        evaluation_times=tuple(range(sample_count)),
        masked_samples=(False,) * sample_count,
        failure_windows=(),
        semantics_sha256=None,
        visibility="dev",
    )

    install_scorer_task(engine, registration)
    install_scorer_task(engine, registration)

    with engine.connect() as connection:
        label_count = connection.execute(
            select(func.count()).select_from(dataset_labels)
        ).scalar_one()
        assert label_count == sample_count
        rows = connection.execute(
            select(dataset_labels.c.sample_index, dataset_labels.c.is_anomaly)
            .where(dataset_labels.c.sample_index.in_((0, sample_count - 1)))
            .order_by(dataset_labels.c.sample_index)
        ).all()
    assert rows == [(0, True), (sample_count - 1, False)]

    conflicting = replace(registration, labels=(*labels[:-1], True))
    with pytest.raises(ValueError, match="different trusted content"):
        install_scorer_task(engine, conflicting)
    engine.dispose()
