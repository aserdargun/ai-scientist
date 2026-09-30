"""Real-role PostgreSQL Scorer worker tests for fixture-only PDM and NRM tasks."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, insert, text, update

from lab.api.app import create_app
from lab.db.schema import (
    dataset_labels,
    dataset_profiles,
    dataset_task_semantics,
    runs,
)
from lab.director.baselines import baseline_candidate_source
from lab.director.ledger import register_experiment, transition_experiment
from lab.director.task_plan import RunTaskAssignment, plan_run_tasks
from lab.scorer.jobs import DEFAULT_ARTIFACT_ROOT, enqueue_score_job, read_candidate_artifact
from lab.scorer.supervisor import run_scorer_process

SECRETS = Path("data/runtime/postgres")
BASELINE_NAME = "robust_z"
BASELINE_SOURCE = baseline_candidate_source(BASELINE_NAME)


def _credential(name: str) -> str:
    return (SECRETS / name).read_text(encoding="utf-8").strip()


@pytest.mark.live
@pytest.mark.parametrize("family", ("PDM", "NRM"))
def test_postgres_scorer_worker_scores_public_family_fixture_without_vus(family: str) -> None:
    """Drive the real Director→Planner→Scorer worker path with synthetic labels."""
    migrator = create_engine(_credential("migrator.dsn"), pool_pre_ping=True)
    director = create_engine(_credential("director.dsn"), pool_pre_ping=True)
    planner = create_engine(_credential("planner.dsn"), pool_pre_ping=True)
    profile_id = f"family-fixture-{family.lower()}-{uuid4().hex}"
    run_id: str | None = None
    artifact_digest: str | None = None
    artifact_preexisting = False
    token = _credential("director.token")
    experiment_id = f"exp_family_{uuid4().hex}"
    candidate_hash = hashlib.sha256(BASELINE_SOURCE).hexdigest()
    session_id = uuid4().hex
    sample_count = 16
    sampling_s = 600
    times = [
        (datetime(2025, 1, 1) + timedelta(seconds=index * sampling_s)).isoformat()
        for index in range(sample_count)
    ]
    masks = [index in {4, 10} for index in range(sample_count)]
    failure_windows = [[7, 11]] if family == "PDM" else []
    labels = [family == "PDM" and index in {7, 8, 9, 11} for index in range(sample_count)]
    semantics = {
        "schema": "public-task-semantics.v1",
        "task_family": family,
        "sampling_s": sampling_s,
        "evaluation_times": times,
        "masked_samples": masks,
        "failure_windows": failure_windows,
    }
    semantics_sha = hashlib.sha256(
        json.dumps(semantics, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
            "ascii"
        )
    ).hexdigest()
    profile_hash = hashlib.sha256(profile_id.encode("ascii")).hexdigest()
    run_request = {
        "idempotency_key": f"public-family-{family}-{uuid4()}",
        "track": "anomaly",
        "suite": f"synthetic.public-family-{family.lower()}.v1",
        "budget": {"experiments": 1, "wall_seconds": 30, "model_tokens": 0},
        "program_version": "fixture-only",
    }
    app = create_app(
        director_engine=director,
        director_token=token,
        owner_id=f"fixture:{family}:{uuid4()}",
        allow_unregistered_suites=True,
    )
    try:
        with TestClient(app) as client:
            started = client.post(
                "/v1/runs", headers={"Authorization": f"Bearer {token}"}, json=run_request
            )
            assert started.status_code == 202
            run_id = started.json()["run_id"]
            with director.begin() as connection:
                connection.execute(
                    update(runs).where(runs.c.run_id == run_id).values(state="running")
                )
            with migrator.begin() as connection:
                connection.execute(
                    insert(dataset_profiles).values(
                        dataset_id=profile_id,
                        split_id="official-family-fixture.v1",
                        session_id=session_id,
                        sample_count=sample_count,
                        sliding_window=4,
                        profile_sha256=profile_hash,
                        task_family=family,
                        visibility="dev",
                    )
                )
                connection.execute(
                    insert(dataset_task_semantics).values(
                        dataset_id=profile_id,
                        split_id="official-family-fixture.v1",
                        session_id=session_id,
                        task_family=family,
                        sampling_s=sampling_s,
                        evaluation_times_json=times,
                        masked_samples_json=masks,
                        failure_windows_json=failure_windows,
                        semantics_sha256=semantics_sha,
                    )
                )
                connection.execute(
                    insert(dataset_labels),
                    [
                        {
                            "dataset_id": profile_id,
                            "split_id": "official-family-fixture.v1",
                            "session_id": session_id,
                            "sample_index": index,
                            "is_anomaly": label,
                        }
                        for index, label in enumerate(labels)
                    ],
                )
            input_hash = hashlib.sha256(f"{family}-synthetic-input".encode()).hexdigest()
            register_experiment(
                director,
                experiment_id=experiment_id,
                run_id=run_id,
                sequence=0,
                experiment_number=None,
                kind="baseline",
                baseline_name=BASELINE_NAME,
                parent_experiment_id=None,
                candidate_sha256=candidate_hash,
                candidate_blob_sha256=candidate_hash,
                inputs_sha256=input_hash,
                move_type="detector",
                system="S1",
                hypothesis=f"Synthetic {family} Scorer family fixture.",
                predicted_delta=None,
                proposal={
                    "schema": "experiment.baseline.v1",
                    "kind": "baseline",
                    "baseline_name": BASELINE_NAME,
                    "candidate_sha256": candidate_hash,
                    "candidate_blob_sha256": candidate_hash,
                    "inputs_sha256": input_hash,
                },
            )
            transition_experiment(director, experiment_id=experiment_id, status="primary_running")
            assignment = RunTaskAssignment(
                experiment_id=experiment_id,
                evaluation_kind="baseline",
                task_id=f"fixture-{family.lower()}",
                seed=0,
                candidate_sha256=candidate_hash,
                dataset_id=profile_id,
                split_id="official-family-fixture.v1",
                session_id=session_id,
            )
            plan_run_tasks(planner, run_id=UUID(run_id), assignments=(assignment,))
            scores = [1.0] * sample_count
            output = json.dumps(
                {
                    "schema": "candidate-scores.v1",
                    "sample_indices": list(range(sample_count)),
                    "scores": scores,
                    "alarm_policy": {"threshold": 0.5, "release": 0.4, "dwell": 1},
                },
                separators=(",", ":"),
                allow_nan=False,
            ).encode("ascii")
            artifact_digest = hashlib.sha256(output).hexdigest()
            path = DEFAULT_ARTIFACT_ROOT / artifact_digest[:2] / f"{artifact_digest}.json"
            artifact_preexisting = path.exists() or path.is_symlink()
            job = enqueue_score_job(
                planner,
                run_id=UUID(run_id),
                experiment_id=experiment_id,
                evaluation_kind="baseline",
                task_id=assignment.task_id,
                seed=0,
                candidate_sha256=candidate_hash,
                candidate_output=output,
            )
            result = run_scorer_process(job)
            assert result.exit_code == 0, result.stderr_tail
            assert result.result is not None and result.result["state"] == "completed"
            with director.connect() as connection:
                measured = (
                    connection.execute(
                        text(
                            "SELECT task_family, task_score, fa_per_day, duty_fraction, "
                            "sampling_s, position_bias FROM lab.dev_task_results "
                            "WHERE run_id=:run_id AND experiment_id=:experiment_id "
                            "AND evaluation_kind='baseline' AND seed=0"
                        ),
                        {"run_id": run_id, "experiment_id": experiment_id},
                    )
                    .mappings()
                    .one()
                )
            assert measured["task_family"] == family
            assert "vus_pr" not in measured and "vus_roc" not in measured
            assert measured["task_score"] is not None
            assert float(measured["duty_fraction"]) == 1.0
            assert int(measured["sampling_s"]) == sampling_s
            if family == "NRM":
                assert float(measured["position_bias"]) == 0.0
            else:
                assert measured["position_bias"] is None
    finally:
        if run_id is not None:
            with migrator.begin() as connection:
                connection.execute(delete(runs).where(runs.c.run_id == run_id))
        with migrator.begin() as connection:
            connection.execute(
                delete(dataset_profiles).where(dataset_profiles.c.dataset_id == profile_id)
            )
        if artifact_digest is not None and not artifact_preexisting:
            path = DEFAULT_ARTIFACT_ROOT / artifact_digest[:2] / f"{artifact_digest}.json"
            if path.exists() and not path.is_symlink():
                payload = read_candidate_artifact(artifact_digest)
                assert hashlib.sha256(payload).hexdigest() == artifact_digest
                path.unlink()
                try:
                    path.parent.rmdir()
                except OSError:
                    pass
        planner.dispose()
        director.dispose()
        migrator.dispose()
