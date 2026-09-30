"""Live PostgreSQL integration for separate Director and Scorer credentials."""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, insert, select, text, update
from sqlalchemy.exc import DBAPIError

from harness.contracts import FitContext
from harness.fingerprint import compute_harness_hash
from lab.api.app import create_app
from lab.db.schema import dataset_labels, dataset_profiles, runs, score_jobs
from lab.director.artifacts import read_director_artifact, store_director_artifact
from lab.director.baselines import baseline_candidate_source
from lab.director.contracts import ExperimentDocument, TrajectoryDocument
from lab.director.ledger import (
    canonical_json_bytes,
    commit_experiment_record,
    document_sha256,
    register_experiment,
    transition_experiment,
)
from lab.director.task_plan import RunTaskAssignment, plan_run_tasks, seal_run_task_plan
from lab.sandbox.docker_runner import DEFAULT_SANDBOX_IMAGE, LocalDockerRunner
from lab.sandbox.evaluation import run_guarded_seed_evaluation
from lab.scorer.jobs import (
    DEFAULT_ARTIFACT_ROOT,
    enqueue_score_job,
    read_candidate_artifact,
)
from lab.scorer.supervisor import run_scorer_finalize_process, run_scorer_process
from lab.scorer.worker import SCORER_ADMISSION_KEY

SECRETS = Path("data/runtime/postgres")
EXPERIMENT_ID = f"exp_{uuid4().hex}"
BASELINE_NAME = "robust_z"
BASELINE_SOURCE = baseline_candidate_source(BASELINE_NAME)


def _credential(name: str) -> str:
    return (SECRETS / name).read_text(encoding="utf-8").strip()


@pytest.mark.live
def test_postgres_director_and_independent_scorer_share_only_verified_report() -> None:
    """Exercise a complete synthetic score without exposing credentials or labels."""
    migrator = create_engine(_credential("migrator.dsn"), pool_pre_ping=True)
    director = create_engine(_credential("director.dsn"), pool_pre_ping=True)
    planner = create_engine(_credential("planner.dsn"), pool_pre_ping=True)
    scorer_lock_engine = create_engine(_credential("scorer.dsn"), pool_pre_ping=True)
    owner_id = f"live-scorer:{uuid4()}"
    token = _credential("director.token")
    run_id = None
    job_id = None
    artifact_digest: str | None = None
    score_artifact_preexisting = False
    director_blob_root = Path("data/runtime/director-test-blobs") / str(uuid4())
    director_blob_digests: list[str] = []
    profile_id = f"synthetic-{uuid4()}"
    run_request = {
        "idempotency_key": f"integration-{uuid4()}",
        "track": "anomaly",
        "suite": "synthetic.postgres-scorer.v1",
        "budget": {"experiments": 1, "wall_seconds": 30, "model_tokens": 0},
        "program_version": "test-only",
    }
    profile_hash = hashlib.sha256(profile_id.encode("ascii")).hexdigest()
    candidate_hash = hashlib.sha256(BASELINE_SOURCE).hexdigest()
    inputs_blob = json.dumps(
        {
            "schema": "synthetic-inputs.v1",
            "train": {"sensor": [0.0] * 32},
            "evaluation": {"sensor": [0.0] * 8 + [2.0] * 8},
            "context": {"seed": 0, "sampling_s": 60, "time_budget_s": 90.0},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    inputs_hash = hashlib.sha256(inputs_blob).hexdigest()
    label_vector = [False] * 8 + [True] * 8
    app = create_app(
        director_engine=director,
        director_token=token,
        owner_id=owner_id,
        allow_unregistered_suites=True,
    )
    try:
        with TestClient(app) as client:
            headers = {"Authorization": f"Bearer {token}"}
            started = client.post("/v1/runs", headers=headers, json=run_request)
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
                        split_id="official-synthetic-v1",
                        session_id="entity-001",
                        sample_count=len(label_vector),
                        sliding_window=4,
                        profile_sha256=profile_hash,
                        visibility="dev",
                    )
                )
                connection.execute(
                    insert(dataset_labels),
                    [
                        {
                            "dataset_id": profile_id,
                            "split_id": "official-synthetic-v1",
                            "session_id": "entity-001",
                            "sample_index": index,
                            "is_anomaly": label,
                        }
                        for index, label in enumerate(label_vector)
                    ],
                )
            candidate_blob_sha = store_director_artifact(
                BASELINE_SOURCE, artifact_root=director_blob_root
            )
            director_blob_digests.append(candidate_blob_sha)
            input_blob_sha = store_director_artifact(inputs_blob, artifact_root=director_blob_root)
            director_blob_digests.append(input_blob_sha)
            assert candidate_blob_sha == candidate_hash
            assert input_blob_sha == inputs_hash
            baseline_hypothesis = "Trusted robust-z baseline smoke; calibration is not frozen."
            baseline_identity = {
                "schema": "experiment.baseline.v1",
                "kind": "baseline",
                "baseline_name": BASELINE_NAME,
                "candidate_sha256": candidate_hash,
                "candidate_blob_sha256": candidate_blob_sha,
                "inputs_sha256": input_blob_sha,
            }
            register_experiment(
                director,
                experiment_id=EXPERIMENT_ID,
                run_id=run_id,
                sequence=0,
                experiment_number=None,
                kind="baseline",
                baseline_name=BASELINE_NAME,
                parent_experiment_id=None,
                candidate_sha256=candidate_hash,
                candidate_blob_sha256=candidate_blob_sha,
                inputs_sha256=input_blob_sha,
                move_type="detector",
                system="S1",
                hypothesis=baseline_hypothesis,
                predicted_delta=None,
                proposal=baseline_identity,
            )
            transition_experiment(
                director, experiment_id=EXPERIMENT_ID, status="primary_running"
            )
            task_assignment = RunTaskAssignment(
                experiment_id=EXPERIMENT_ID,
                evaluation_kind="baseline",
                task_id="synthetic-task",
                seed=0,
                candidate_sha256=candidate_hash,
                dataset_id=profile_id,
                split_id="official-synthetic-v1",
                session_id="entity-001",
            )
            plan_run_tasks(planner, run_id=UUID(run_id), assignments=(task_assignment,))
            plan_run_tasks(planner, run_id=UUID(run_id), assignments=(task_assignment,))

            evaluator = LocalDockerRunner(
                image=DEFAULT_SANDBOX_IMAGE,
                work_root=Path("data/postgres-typed-candidate") / str(uuid4()),
            )
            evaluation_started = time.monotonic()
            typed_output = run_guarded_seed_evaluation(
                evaluator,
                candidate_source=BASELINE_SOURCE,
                train=pd.DataFrame({"sensor": [0.0] * 32}),
                evaluation=pd.DataFrame({"sensor": [0.0] * 8 + [2.0] * 8}),
                context=FitContext(
                    seed=0,
                    signals=("sensor",),
                    regime_signals=("sensor",),
                    sampling_s=60,
                    time_budget_s=90.0,
                ),
                task_ids=frozenset({"synthetic-task"}),
                evaluation_instants=frozenset(),
                trusted_baseline_name=BASELINE_NAME,
            )
            assert hashlib.sha256(BASELINE_SOURCE).hexdigest() == candidate_hash
            candidate_output = typed_output.evaluation.score_document
            artifact_digest = hashlib.sha256(candidate_output).hexdigest()
            score_artifact_path = (
                DEFAULT_ARTIFACT_ROOT
                / artifact_digest[:2]
                / f"{artifact_digest}.json"
            )
            score_artifact_preexisting = (
                score_artifact_path.exists() or score_artifact_path.is_symlink()
            )
            job_id = enqueue_score_job(
                planner,
                run_id=UUID(run_id),
                experiment_id=EXPERIMENT_ID,
                evaluation_kind="baseline",
                task_id="synthetic-task",
                seed=0,
                candidate_sha256=candidate_hash,
                candidate_output=candidate_output,
            )
            duplicate_job_id = enqueue_score_job(
                planner,
                run_id=UUID(run_id),
                experiment_id=EXPERIMENT_ID,
                evaluation_kind="baseline",
                task_id="synthetic-task",
                seed=0,
                candidate_sha256=candidate_hash,
                candidate_output=candidate_output,
            )
            assert duplicate_job_id == job_id
            changed_output = json.dumps(
                {
                    "schema": "candidate-scores.v1",
                    "sample_indices": list(range(len(label_vector))),
                    "scores": [
                        float(len(label_vector) - index) for index in range(len(label_vector))
                    ],
                },
                separators=(",", ":"),
            ).encode("ascii")
            with pytest.raises(ValueError, match="different durable artifact"):
                enqueue_score_job(
                    planner,
                    run_id=UUID(run_id),
                    experiment_id=EXPERIMENT_ID,
                    evaluation_kind="baseline",
                    task_id="synthetic-task",
                    seed=0,
                    candidate_sha256=candidate_hash,
                    candidate_output=changed_output,
                )
            lock_connection = scorer_lock_engine.connect()
            lock_connection.execute(
                text("SELECT pg_advisory_lock(:lock_key)"),
                {"lock_key": SCORER_ADMISSION_KEY},
            )
            try:
                busy_result = run_scorer_process(job_id)
                assert busy_result.exit_code == 0
                assert busy_result.result == {"state": "capacity_busy"}
            finally:
                lock_connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": SCORER_ADMISSION_KEY},
                )
                lock_connection.close()
            with migrator.connect() as connection:
                assert (
                    connection.execute(
                        select(score_jobs.c.state).where(score_jobs.c.job_id == job_id)
                    ).scalar_one()
                    == "queued"
                )
            process_result = run_scorer_process(job_id)
            assert process_result.exit_code == 0
            assert process_result.result is not None
            assert process_result.result["state"] == "completed"
            assert process_result.result["worker_pid"] != str(os.getpid())
            assert process_result.result["run_finalized"] == "false"
            assert process_result.result["research_status"] == "pending"
            response = client.get(f"/v1/runs/{run_id}/report", headers=headers)
            assert response.status_code == 404
            with director.connect() as connection:
                measured = connection.execute(
                    text(
                        """
                        SELECT vus_pr, vus_roc, sample_count, profile_sha256,
                               harness_sha256, candidate_output_sha256
                          FROM lab.dev_task_results
                         WHERE run_id = :run_id AND experiment_id = :experiment_id
                           AND evaluation_kind = 'baseline' AND task_id = 'synthetic-task'
                           AND seed = 0
                        """
                    ),
                    {"run_id": run_id, "experiment_id": EXPERIMENT_ID},
                ).mappings().one()
            assert measured["candidate_output_sha256"] == artifact_digest
            assert measured["profile_sha256"] == profile_hash
            assert int(measured["sample_count"]) == len(label_vector)
            seal = seal_run_task_plan(planner, run_id=UUID(run_id))
            assert seal.task_count == 1
            with pytest.raises(ValueError, match="sealed"):
                plan_run_tasks(planner, run_id=UUID(run_id), assignments=(task_assignment,))
            pending_finalize = run_scorer_finalize_process(UUID(run_id))
            assert pending_finalize.exit_code == 0, pending_finalize.stderr_tail
            assert pending_finalize.result is not None
            assert pending_finalize.result["state"] == "not_ready"
            assert pending_finalize.result["research_status"] == "pending"
            assert client.get(f"/v1/runs/{run_id}/report", headers=headers).status_code == 404
            measured_status: str = "scored"
            harness_sha = compute_harness_hash(Path.cwd()).sha256
            image_sha = DEFAULT_SANDBOX_IMAGE.removeprefix("sha256:")
            experiment_document = ExperimentDocument.model_validate(
                {
                    "schema": "experiment.v1",
                    "experiment_id": EXPERIMENT_ID,
                    "run_id": UUID(run_id),
                    "ordinal": 1,
                    "kind": "baseline",
                    "experiment_number": None,
                    "baseline_name": BASELINE_NAME,
                    "calibration_sha256": None,
                    "agent_version": "0.10.0",
                    "parent_experiment_id": None,
                    "candidate_sha256": candidate_hash,
                    "candidate_blob_sha256": candidate_blob_sha,
                    "move_type": "detector",
                    "system": "S1",
                    "hypothesis": baseline_hypothesis,
                    "predicted_delta": None,
                    "inputs_sha256": input_blob_sha,
                    "parent_tree": hashlib.sha256(b"synthetic-parent-tree").hexdigest(),
                    "child_tree": candidate_hash,
                    "harness_sha256": harness_sha,
                    "image_sha256": image_sha,
                    "suite_id": "synthetic.postgres-scorer.v1",
                    "suite_version": 1,
                    "per_task": (
                        {
                            "task_id": "synthetic-task",
                            "score_norm": None,
                            "vus_pr": float(measured["vus_pr"]),
                            "vus_roc": float(measured["vus_roc"]),
                            "fa_per_day": None,
                            "event_f1": None,
                            "fit_seconds": typed_output.evaluation.fit_seconds,
                            "score_seconds": typed_output.evaluation.score_seconds,
                        },),
                    "suite_score": None,
                    "guards": {
                        "determinism": "pass" if typed_output.determinism.passed else "fail",
                        "causality": "pass" if typed_output.causality.passed else "fail",
                        "hardcoding": "pass" if typed_output.hardcoding.passed else "fail",
                    },
                    "decision": None,
                    "status": measured_status,
                    "fit_seconds": typed_output.evaluation.fit_seconds,
                    "score_seconds": typed_output.evaluation.score_seconds,
                    "llm_input_tokens": 0,
                    "llm_output_tokens": 0,
                    "wall_seconds": time.monotonic() - evaluation_started,
                }
            )
            messages_blob = b"[]"
            messages_digest = store_director_artifact(
                messages_blob, artifact_root=director_blob_root
            )
            director_blob_digests.append(messages_digest)
            assert read_director_artifact(
                messages_digest, artifact_root=director_blob_root
            ) == messages_blob
            trajectory_document = TrajectoryDocument.model_validate(
                {
                    "schema": "trajectory.v1",
                    "trajectory_id": f"trj_{uuid4().hex}",
                    "run_id": UUID(run_id),
                    "experiment_id": EXPERIMENT_ID,
                    "kind": "baseline",
                    "experiment_number": None,
                    "baseline_name": BASELINE_NAME,
                    "calibration_sha256": None,
                    "agent_version": "0.10.0",
                    "model_id": "synthetic-fixture/no-llm",
                    "usage_profile": "noncommercial_research",
                    "source_provenance": (
                        {
                            "dataset_id": profile_id,
                            "split_id": "official-synthetic-v1",
                            "session_id": "entity-001",
                            "source_manifest_sha256": profile_hash,
                            "source_revision": "local-synthetic-v1",
                            "license_id": "CC0-synthetic",
                            "attribution": "Generated locally for integration testing.",
                            "access_terms": "Synthetic fixture; no external data.",
                            "usage_profile": "noncommercial_research",
                        },),
                    "quantization": "none",
                    "adapter": "none",
                    "system": "S1",
                    "thinking": False,
                    "temperature": 0.0,
                    "top_p": 1.0,
                    "context_template": "synthetic.integration.v1",
                    "inputs_sha256": input_blob_sha,
                    "messages_blob_sha256": messages_digest,
                    "tool_calls": 0,
                    "outcome": None,
                    "quality_tier": "bronze",
                    "secrets_scrubbed": False,
                    "people_scrubbed": False,
                    "raw_values_scrubbed": False,
                    "exclusions": (
                        "Only robust-z was measured; full baseline calibration was omitted.",
                    ),
                }
            )
            experiment_document_bytes = canonical_json_bytes(experiment_document)
            trajectory_document_bytes = canonical_json_bytes(trajectory_document)
            experiment_blob_sha = store_director_artifact(
                experiment_document_bytes, artifact_root=director_blob_root
            )
            director_blob_digests.append(experiment_blob_sha)
            trajectory_blob_sha = store_director_artifact(
                trajectory_document_bytes, artifact_root=director_blob_root
            )
            director_blob_digests.append(trajectory_blob_sha)
            assert experiment_blob_sha == document_sha256(experiment_document)
            assert trajectory_blob_sha == document_sha256(trajectory_document)
            assert read_director_artifact(
                experiment_blob_sha, artifact_root=director_blob_root
            ) == experiment_document_bytes
            assert read_director_artifact(
                trajectory_blob_sha, artifact_root=director_blob_root
            ) == trajectory_document_bytes
            receipt = commit_experiment_record(
                director,
                experiment=experiment_document,
                trajectory=trajectory_document,
                experiment_blob_sha256=experiment_blob_sha,
                trajectory_blob_sha256=trajectory_blob_sha,
            )
            assert receipt["status"] == "committed"
            with director.connect() as connection:
                readback = connection.execute(
                    text("SELECT lab.experiment_record_receipt(:experiment_id)"),
                    {"experiment_id": EXPERIMENT_ID},
                ).scalar_one()
            assert readback["status"] == measured_status
            assert readback["experiment_id"] == EXPERIMENT_ID
            finalize_result = run_scorer_finalize_process(UUID(run_id))
            assert finalize_result.exit_code == 0, finalize_result.stderr_tail
            assert finalize_result.result is not None
            assert finalize_result.result["state"] == "finalized"
            assert finalize_result.result["research_status"] == "completed"
            assert finalize_result.result["worker_pid"] != str(os.getpid())
            response = client.get(f"/v1/runs/{run_id}/report", headers=headers)
            assert response.status_code == 200
            report = response.json()["report"]
            assert (
                response.json()["report_sha256"]
                == hashlib.sha256(
                    json.dumps(
                        report, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                    ).encode()
                ).hexdigest()
            )
            assert report["status"] == "completed"
            assert len(report["task_scores"]) == 1
            assert report["task_scores"][0]["dataset_id"] == profile_id
            assert report["task_scores"][0]["sample_count"] == 16
            with migrator.connect() as connection:
                job = connection.execute(
                    select(score_jobs.c.state, score_jobs.c.claimed_by).where(
                        score_jobs.c.job_id == job_id
                    )
                ).one()
            assert job.state == "completed"
            assert job.claimed_by is None
            assert "labels" not in json.dumps(report).lower()
            with pytest.raises(DBAPIError):
                with director.connect() as connection:
                    connection.execute(select(dataset_labels.c.is_anomaly).limit(1)).all()
            proof_files = [
                Path("lab/db/schema.py"),
                Path("lab/director/task_plan.py"),
                Path("lab/cli.py"),
                Path("lab/db/migrations/versions/0004_durable_score_jobs.py"),
                Path("lab/db/migrations/versions/0005_planner_run_read.py"),
                Path("lab/db/migrations/versions/0006_narrow_director_score_jobs.py"),
                Path("lab/db/migrations/versions/0007_seal_task_plan.py"),
                Path("lab/db/migrations/versions/0011_experiment_ledger.py"),
                Path("lab/db/migrations/versions/0012_experiment_finalization_fence.py"),
                Path("lab/db/migrations/versions/0013_baseline_calibration.py"),
                Path("lab/db/task_plan.py"),
                Path("lab/director/contracts.py"),
                Path("lab/director/ledger.py"),
                Path("lab/director/artifacts.py"),
                Path("lab/director/baselines.py"),
                Path("lab/scorer/jobs.py"),
                Path("lab/scorer/service.py"),
                Path("lab/scorer/supervisor.py"),
                Path("lab/scorer/worker.py"),
                Path("lab/sandbox/candidate_entrypoint.py"),
                Path("lab/sandbox/docker_runner.py"),
                Path("lab/sandbox/evaluation.py"),
                Path("lab/sandbox/sandbox_wrapper.py"),
                Path("harness/baselines.py"),
                Path("harness/VERSION"),
                Path("harness/fingerprint.py"),
                Path("ops/sandbox-image.lock"),
                Path(__file__),
            ]
            Path("docs/ai-scientist/evidence/postgres-scorer-process-latest.json").write_text(
                json.dumps(
                    {
                        "schema": "postgres-scorer-process-check.v1",
                        "checked_at": datetime.now(UTC).isoformat(),
                        "command": [
                            "uv",
                            "run",
                            "--python",
                            "3.12",
                            "pytest",
                            "-q",
                            "-m",
                            "live",
                            "tests/test_postgres_scorer_integration.py",
                        ],
                        "result": "pass",
                        "scope": (
                            "synthetic local PostgreSQL; Planner enqueued a trusted robust-z "
                            "baseline by dedicated role; the allowlisted baseline wrapper "
                            "passed hardcoding, full-fit determinism, and "
                            "same-frozen-fit causality guards in fresh Docker containers; its "
                            "score artifact entered Planner queue; Scorer ran in fresh transient "
                            "systemd cgroup processes; scoring "
                            "completed while the plan was open; a sealed task plan with a "
                            "running baseline stayed pending until its immutable terminal "
                            "experiment/trajectory pair was committed, after which a separate "
                            "Scorer process finalized; no AOS, "
                            "GPU, public labels or source data were touched; candidate source, "
                            "synthetic inputs, messages, experiment document, and trajectory "
                            "document were persisted and hash-verified in a private generic "
                            "blob store; those owned fixture blobs were removed during cleanup; "
                            "the full 3-baseline × 3-seed calibration remains the separate "
                            "baseline-pipeline probe"
                        ),
                        "proof": {
                            "worker_pid": process_result.result["worker_pid"],
                            "finalizer_pid": finalize_result.result["worker_pid"],
                            "finalizer_unit": finalize_result.unit,
                            "director_test_pid": str(os.getpid()),
                            "distinct_process": process_result.result["worker_pid"]
                            != str(os.getpid()),
                            "systemd_unit": process_result.unit,
                            "global_scorer_slot_busy_backpressure": busy_result.result,
                            "same_task_same_artifact_idempotent": duplicate_job_id == job_id,
                            "different_artifact_rejected": True,
                            "planner_task_and_job_insert": True,
                            "director_label_select_denied": True,
                            "physical_content_blobs_persisted": True,
                            "physical_blob_digests": sorted(set(director_blob_digests)),
                            "score_job_state": job.state,
                            "run_finalized": process_result.result["run_finalized"],
                            "task_plan_sha256": seal.sha256,
                            "task_plan_count": seal.task_count,
                            "finalization_waited_for_experiment_records": (
                                pending_finalize.result["state"] == "not_ready"
                            ),
                            "report_sha256": response.json()["report_sha256"],
                            "trusted_baseline": {
                                "name": BASELINE_NAME,
                                "fit_container": typed_output.evaluation.fit_container_name,
                                "score_container": typed_output.evaluation.score_container_name,
                                "distinct_containers": (
                                    typed_output.evaluation.fit_container_name
                                    != typed_output.evaluation.score_container_name
                                ),
                                "fit_artifact_sha256": (
                                    typed_output.evaluation.fit_artifact_sha256
                                ),
                                "determinism": typed_output.determinism.code,
                                "causality": typed_output.causality.code,
                                "hardcoding": typed_output.hardcoding.code,
                            },
                        },
                        "source_sha256": {
                            file.as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
                            for file in proof_files
                        },
                        "secrets_recorded": False,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
    finally:
        if run_id is not None:
            with migrator.begin() as connection:
                connection.execute(delete(runs).where(runs.c.run_id == run_id))
        with migrator.begin() as connection:
            connection.execute(
                delete(dataset_profiles).where(dataset_profiles.c.dataset_id == profile_id)
            )
        if artifact_digest is not None and not score_artifact_preexisting:
            score_artifact_path = (
                DEFAULT_ARTIFACT_ROOT
                / artifact_digest[:2]
                / f"{artifact_digest}.json"
            )
            if score_artifact_path.exists() and not score_artifact_path.is_symlink():
                score_artifact = read_candidate_artifact(artifact_digest)
                assert hashlib.sha256(score_artifact).hexdigest() == artifact_digest
                score_artifact_path.unlink()
                try:
                    score_artifact_path.parent.rmdir()
                except OSError:
                    pass
        if director_blob_root.exists():
            for digest in set(director_blob_digests):
                payload = read_director_artifact(digest, artifact_root=director_blob_root)
                assert hashlib.sha256(payload).hexdigest() == digest
                blob_path = director_blob_root / digest[:2] / f"{digest}.json"
                blob_path.unlink()
            for shard in {digest[:2] for digest in director_blob_digests}:
                (director_blob_root / shard).rmdir()
            quota_lock = director_blob_root / ".quota.lock"
            quota_lock.unlink(missing_ok=True)
            director_blob_root.rmdir()
            try:
                director_blob_root.parent.rmdir()
            except OSError:
                pass
        migrator.dispose()
        director.dispose()
        planner.dispose()
        scorer_lock_engine.dispose()
