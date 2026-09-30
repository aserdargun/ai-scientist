"""PDM/NRM family semantics are independently recomputed by the Scorer."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import create_engine, insert
from sqlalchemy.pool import StaticPool

from lab.db.schema import (
    dataset_labels,
    dataset_profiles,
    dataset_task_semantics,
    experiments,
    metadata,
    run_tasks,
    runs,
)
from lab.director.public_suite import task_semantics_sha256
from lab.scorer.service import IndependentScorer


def _engine():
    raw = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    translated = raw.execution_options(schema_translate_map={"lab": None, "scorer": None})
    metadata.create_all(translated)
    return translated


def _score_family(family: str, *, utc_axis: bool = False) -> dict[str, object]:
    engine = _engine()
    scorer = IndependentScorer(engine, harness_sha256="1" * 64)
    run_id = uuid4()
    experiment_id = f"exp-{family.lower()}"
    task_id = f"task-{family.lower()}"
    dataset_id = f"fixture-{family.lower()}"
    session_id = f"session-{run_id.hex}"
    candidate_sha = "2" * 64
    sample_count = 16
    sampling_s = 600
    first = datetime(2025, 1, 1, tzinfo=UTC if utc_axis else None)
    times = [
        (first + timedelta(seconds=index * sampling_s)).isoformat() for index in range(sample_count)
    ]
    masks = [index in {4, 10} for index in range(sample_count)]
    failures = [[7, 11]] if family == "PDM" else []
    labels = [family == "PDM" and index in {7, 8, 9, 11} for index in range(sample_count)]
    profile_sha = hashlib.sha256(f"{family}:{run_id}".encode()).hexdigest()
    semantics_sha = task_semantics_sha256(
        task_family=family,
        sampling_s=sampling_s,
        evaluation_times=tuple(times),
        masked_samples=tuple(masks),
        failure_windows=tuple(tuple(window) for window in failures),
    )
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            insert(runs).values(
                run_id=run_id,
                origin="local",
                owner_id="local:scorer-family-tests",
                idempotency_key=f"{family}-{run_id}",
                payload_sha256="a" * 64,
                request_json={},
                state="running",
                created_at=now,
                updated_at=now,
                stop_requested=False,
            )
        )
        connection.execute(
            insert(dataset_profiles).values(
                dataset_id=dataset_id,
                split_id="dev",
                session_id=session_id,
                sample_count=sample_count,
                sliding_window=4,
                profile_sha256=profile_sha,
                task_family=family,
                visibility="dev",
            )
        )
        connection.execute(
            insert(dataset_task_semantics).values(
                dataset_id=dataset_id,
                split_id="dev",
                session_id=session_id,
                task_family=family,
                sampling_s=sampling_s,
                evaluation_times_json=times,
                masked_samples_json=masks,
                failure_windows_json=failures,
                semantics_sha256=semantics_sha,
            )
        )
        connection.execute(
            insert(dataset_labels),
            [
                {
                    "dataset_id": dataset_id,
                    "split_id": "dev",
                    "session_id": session_id,
                    "sample_index": index,
                    "is_anomaly": label,
                }
                for index, label in enumerate(labels)
            ],
        )
        connection.execute(
            insert(experiments).values(
                experiment_id=experiment_id,
                run_id=run_id,
                sequence=0,
                experiment_number=1,
                kind="proposal",
                baseline_name=None,
                parent_experiment_id=None,
                candidate_sha256=candidate_sha,
                candidate_blob_sha256=candidate_sha,
                inputs_sha256="3" * 64,
                move_type="detector",
                system="S1",
                hypothesis="Score a bounded trusted task-family fixture.",
                predicted_delta=None,
                calibration_sha256=None,
                proposal_json={},
                status="proposed",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(run_tasks).values(
                run_id=run_id,
                experiment_id=experiment_id,
                evaluation_kind="primary",
                task_id=task_id,
                seed=0,
                candidate_sha256=candidate_sha,
                dataset_id=dataset_id,
                split_id="dev",
                session_id=session_id,
            )
        )
    # PDM uses the same always-on positive control; NRM always-on verifies duty
    # and healthy exposure remain family metrics and never become invented VUS.
    output = json.dumps(
        {
            "schema": "candidate-scores.v1",
            "sample_indices": list(range(sample_count)),
            "scores": [1.0] * sample_count,
            "alarm_policy": {"threshold": 0.5, "release": 0.4, "dwell": 1},
        },
        separators=(",", ":"),
    ).encode()
    result = scorer.score_task(
        run_id=run_id,
        experiment_id=experiment_id,
        evaluation_kind="primary",
        task_id=task_id,
        seed=0,
        candidate_sha256=candidate_sha,
        candidate_output=output,
    )
    return result


def test_pdm_scorer_uses_masked_failure_window_and_no_vus() -> None:
    score = _score_family("PDM")
    assert score["task_family"] == "PDM"
    assert "vus_pr" not in score and "vus_roc" not in score
    assert score["task_score"] >= 0.0
    assert score["duty_fraction"] == 1.0
    assert score["sampling_s"] == 600
    assert score["failure_windows"] == [[7, 11]]


def test_nrm_scorer_counts_always_on_as_zero_for_task_score_without_vus() -> None:
    score = _score_family("NRM")
    assert score["task_family"] == "NRM"
    assert "vus_pr" not in score and "vus_roc" not in score
    assert score["task_score"] == 0.0
    assert score["duty_fraction"] == 1.0
    assert score["fa_per_day"] > 0.0
    assert score["position_bias"] == 0.0
