"""PostgreSQL admission test for the real API request identity used by holdout."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, insert, select, text

from harness.contracts import FitContext
from lab.api.app import create_app
from lab.api.registry import SuiteEntry, SuiteRegistry
from lab.db.schema import baseline_calibrations, dataset_labels, runs
from lab.director.artifacts import store_director_artifact
from lab.director.baselines import baseline_candidate_source
from lab.director.contracts import ExperimentDocument, TrajectoryDocument
from lab.director.holdout import evaluate_holdout_check, register_run_end_intent
from lab.director.journal import DirectorRunLease, canonical_bytes
from lab.director.ledger import commit_experiment_record, register_experiment, transition_experiment
from lab.director.loop import DirectorLoop, DirectorLoopResult, DirectorLoopState
from lab.director.task_plan import RunTaskAssignment, plan_run_tasks
from lab.scorer import holdout_supervisor
from lab.scorer.holdout import DEFAULT_HOLDOUT_INPUT_ROOT, HoldoutTaskSpec, register_holdout_suite
from lab.scorer.jobs import DEFAULT_ARTIFACT_ROOT, enqueue_score_job, store_artifact_bytes
from lab.scorer.supervisor import run_scorer_process


@pytest.mark.live
def test_production_api_request_binds_distinct_dev_and_holdout_manifest_shas() -> None:
    dsn_root_value = os.environ.get("LAB_HOLDOUT_TEST_DSN_DIR")
    if not dsn_root_value:
        pytest.skip("set LAB_HOLDOUT_TEST_DSN_DIR to the dedicated holdout test database")
    dsn_root = Path(dsn_root_value).resolve(strict=True)
    migrator = create_engine((dsn_root / "migrator.dsn").read_text().strip(), pool_pre_ping=True)
    director = create_engine((dsn_root / "director.dsn").read_text().strip(), pool_pre_ping=True)
    planner = create_engine((dsn_root / "planner.dsn").read_text().strip(), pool_pre_ping=True)
    scorer = create_engine((dsn_root / "scorer.dsn").read_text().strip(), pool_pre_ping=True)
    created_runs: list[UUID] = []
    temporary_roots: list[Path] = []
    suite_id = f"synthetic.holdout-identity-{uuid4().hex[:8]}"
    suite_version = 3
    private_manifest_sha = hashlib.sha256(b"private-holdout-manifest").hexdigest()
    dev_manifest_a = b'{"schema":"director-suite.v1","identity":"dev-A"}'
    dev_manifest_b = b'{"schema":"director-suite.v1","identity":"dev-B"}'
    dev_sha_a = hashlib.sha256(dev_manifest_a).hexdigest()
    dev_sha_b = hashlib.sha256(dev_manifest_b).hexdigest()
    try:
        with director.connect() as connection:
            database_name = connection.execute(text("SELECT current_database()")).scalar_one()
        assert database_name.startswith("swapp_lab_m0_holdout_030_")

        with scorer.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO scorer.holdout_suite_versions "
                    "(suite_id,suite_version,manifest_sha256,development_manifest_sha256,"
                    "task_count,epsilon) VALUES (:suite,:version,:private,:development,1,0.01)"
                ),
                {
                    "suite": suite_id,
                    "version": suite_version,
                    "private": private_manifest_sha,
                    "development": dev_sha_a,
                },
            )

        for ordinal, (manifest_bytes, manifest_sha) in enumerate(
            ((dev_manifest_a, dev_sha_a), (dev_manifest_b, dev_sha_b)), start=1
        ):
            runtime_root = Path(tempfile.mkdtemp(prefix="holdout-api-identity-"))
            runtime_root.chmod(0o700)
            temporary_roots.append(runtime_root)
            scenario_bytes = b'{"schema":"review-fake-provider-input.v1","items":[]}'
            (runtime_root / "suite.json").write_bytes(manifest_bytes)
            (runtime_root / "scenario.json").write_bytes(scenario_bytes)
            (runtime_root / "suite.json").chmod(0o600)
            (runtime_root / "scenario.json").chmod(0o600)
            entry = SuiteEntry(
                suite_id=suite_id,
                track="anomaly",
                program_version="fixture-only",
                suite_manifest_path="suite.json",
                suite_manifest_sha256=manifest_sha,
                provider="fake-json",
                scenario_path="scenario.json",
                scenario_sha256=hashlib.sha256(scenario_bytes).hexdigest(),
                proposal_limit=1,
            )
            registry = SuiteRegistry((entry,), runtime_root)
            token = "t" * 32
            app = create_app(
                director_engine=director,
                director_token=token,
                owner_id="fixture:holdout-api-identity",
                suite_registry=registry,
            )
            request = {
                "idempotency_key": f"holdout-api-identity-{uuid4().hex}",
                "track": "anomaly",
                "suite": suite_id,
                "budget": {"experiments": 1, "wall_seconds": 120, "model_tokens": 500},
                "program_version": "fixture-only",
            }
            with TestClient(app) as client:
                response = client.post(
                    "/v1/runs",
                    headers={"Authorization": f"Bearer {token}"},
                    json=request,
                )
            assert response.status_code == 202
            run_id = UUID(response.json()["run_id"])
            created_runs.append(run_id)
            with director.connect() as connection:
                stored = connection.execute(
                    select(runs.c.request_json).where(runs.c.run_id == run_id)
                ).scalar_one()
            assert stored["suite"] == suite_id
            assert stored["suite_manifest_sha256"] == manifest_sha
            assert "suite_id" not in stored and "suite_version" not in stored
            if ordinal == 1:
                assert manifest_sha != private_manifest_sha
            with migrator.begin() as connection:
                calibration_digest = hashlib.sha256(str(run_id).encode()).hexdigest()
                connection.execute(
                    insert(baseline_calibrations).values(
                        run_id=run_id,
                        suite_id=suite_id,
                        suite_version=suite_version,
                        calibration_sha256=calibration_digest,
                        blob_sha256=calibration_digest,
                        task_count=1,
                    )
                )
            reservation_id = uuid4()
            with pytest.raises(Exception) as error:
                with director.begin() as connection:
                    connection.execute(
                        text(
                            "SELECT lab.reserve_holdout_check(:run,:reservation,:key,:candidate,"
                            ":kind,:idx)"
                        ),
                        {
                            "run": run_id,
                            "reservation": reservation_id,
                            "key": f"api-identity-{ordinal}",
                            "candidate": f"candidate-never-reached-{ordinal}",
                            "kind": "run_end",
                            "idx": 1,
                        },
                    ).scalar_one()
            if ordinal == 1:
                assert "run is not active" in str(error.value)
            else:
                assert "not pinned to this development manifest" in str(error.value)
    finally:
        if created_runs:
            with migrator.begin() as connection:
                for run_id in created_runs:
                    connection.execute(
                        text("DELETE FROM lab.runs WHERE run_id=:run_id"),
                        {"run_id": run_id},
                    )
        with migrator.begin() as connection:
            connection.execute(
                text("DELETE FROM scorer.holdout_suite_versions WHERE suite_id=:suite"),
                {"suite": suite_id},
            )
        for root in temporary_roots:
            for child in root.iterdir():
                child.unlink()
            root.rmdir()
        migrator.dispose()
        director.dispose()
        planner.dispose()
        scorer.dispose()


@pytest.mark.live
def test_run_end_accepts_resumed_abandoned_ordinal_without_experiment_row(
    tmp_path, monkeypatch
) -> None:
    """A durable abandoned proposal counts once in terminal ordinal coverage."""
    dsn_root_value = os.environ.get("LAB_HOLDOUT_TEST_DSN_DIR")
    if not dsn_root_value:
        pytest.skip("set LAB_HOLDOUT_TEST_DSN_DIR to the dedicated holdout test database")
    dsn_root = Path(dsn_root_value).resolve(strict=True)
    migrator = create_engine((dsn_root / "migrator.dsn").read_text().strip(), pool_pre_ping=True)
    director = create_engine((dsn_root / "director.dsn").read_text().strip(), pool_pre_ping=True)
    planner = create_engine((dsn_root / "planner.dsn").read_text().strip(), pool_pre_ping=True)
    scorer = create_engine((dsn_root / "scorer.dsn").read_text().strip(), pool_pre_ping=True)
    run_id: UUID | None = None
    suite_id = f"synthetic.holdout-abandoned-{uuid4().hex[:8]}"
    suite_version = 3
    dev_manifest = (
        b'{"schema":"director-suite.v1","suite_id":"' + suite_id.encode() + b'"}'
    )
    dev_manifest_sha = hashlib.sha256(dev_manifest).hexdigest()
    private_manifest_sha = hashlib.sha256(b"separate-private-holdout-manifest").hexdigest()
    calibration_sha = hashlib.sha256(b"test-calibration").hexdigest()
    baseline_source = baseline_candidate_source("robust_z")
    candidate_sha = hashlib.sha256(baseline_source).hexdigest()
    profile_id = f"holdout-baseline-{uuid4().hex[:12]}"
    split_id = "synthetic-holdout-test-v1"
    session_id = "session-1"
    scorer_dsn_alias = Path("data/runtime/postgres/scorer.dsn")
    scorer_dsn_alias_bytes: bytes | None = None
    scorer_blob_path: Path | None = None
    scorer_blob_preexisting = False
    scorer_source_path: Path | None = None
    scorer_source_preexisting = False
    artifact_root = Path("data/runtime") / f"holdout-pg-artifacts-{uuid4().hex}"
    holdout_input_root = DEFAULT_HOLDOUT_INPUT_ROOT
    holdout_profile_id: str | None = None
    worker_results = []
    artifact_root.mkdir(mode=0o700)
    try:
        with director.connect() as connection:
            assert connection.execute(text("SELECT current_database()")).scalar_one().startswith(
                "swapp_lab_m0_holdout_030_"
            )
        scenario = b'{"schema":"review-fake-provider-input.v1","items":[]}'
        api_root = tmp_path / "api"
        api_root.mkdir(mode=0o700)
        (api_root / "suite.json").write_bytes(dev_manifest)
        (api_root / "scenario.json").write_bytes(scenario)
        (api_root / "suite.json").chmod(0o600)
        (api_root / "scenario.json").chmod(0o600)
        registry = SuiteRegistry(
            (
                SuiteEntry(
                    suite_id=suite_id,
                    track="anomaly",
                    program_version="fixture-only",
                    suite_manifest_path="suite.json",
                    suite_manifest_sha256=dev_manifest_sha,
                    provider="fake-json",
                    scenario_path="scenario.json",
                    scenario_sha256=hashlib.sha256(scenario).hexdigest(),
                    proposal_limit=1,
                ),
            ),
            api_root,
        )
        token = "r" * 32
        app = create_app(
            director_engine=director,
            director_token=token,
            owner_id="fixture:holdout-abandoned-ordinal",
            suite_registry=registry,
        )
        with TestClient(app) as client:
            response = client.post(
                "/v1/runs",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "idempotency_key": f"holdout-abandoned-{uuid4().hex}",
                    "track": "anomaly",
                    "suite": suite_id,
                    "budget": {
                        "experiments": 1,
                        "wall_seconds": 120,
                        "model_tokens": 100,
                    },
                    "program_version": "fixture-only",
                },
            )
        assert response.status_code == 202
        run_id = UUID(response.json()["run_id"])
        with migrator.begin() as connection:
            connection.execute(
                text("UPDATE lab.runs SET state='running' WHERE run_id=:run_id AND state='queued'"),
                {"run_id": run_id},
            )
            label_values = [False] * 8 + [True] * 8
            connection.execute(
                text(
                    "INSERT INTO scorer.dataset_profiles "
                    "(dataset_id,split_id,session_id,sample_count,sliding_window,"
                    "profile_sha256,visibility) "
                    "VALUES (:dataset,:split,:session,16,4,:sha,'dev')"
                ),
                {
                    "dataset": profile_id,
                    "split": split_id,
                    "session": session_id,
                    "sha": hashlib.sha256(profile_id.encode()).hexdigest(),
                },
            )
            connection.execute(
                insert(dataset_labels),
                [
                    {
                        "dataset_id": profile_id,
                        "split_id": split_id,
                        "session_id": session_id,
                        "sample_index": index,
                        "is_anomaly": label,
                    }
                    for index, label in enumerate(label_values)
                ],
            )
            holdout_profile_id = f"holdout-private-{uuid4().hex[:12]}"
            holdout_profile_sha = hashlib.sha256(holdout_profile_id.encode()).hexdigest()
            connection.execute(
                text(
                    "INSERT INTO scorer.dataset_profiles "
                    "(dataset_id,split_id,session_id,sample_count,sliding_window,profile_sha256,"
                    "task_family,visibility) VALUES (:dataset,:split,:session,512,4,:sha,'EVT',"
                    "'holdout')"
                ),
                {
                    "dataset": holdout_profile_id,
                    "split": split_id,
                    "session": session_id,
                    "sha": holdout_profile_sha,
                },
            )
            holdout_labels = [False] * 256 + [True] * 256
            connection.execute(
                insert(dataset_labels),
                [
                    {
                        "dataset_id": holdout_profile_id,
                        "split_id": split_id,
                        "session_id": session_id,
                        "sample_index": index,
                        "is_anomaly": label,
                    }
                    for index, label in enumerate(holdout_labels)
                ],
            )
            evaluation_times = list(range(512))
            masked_samples = [False] * 512
            failure_windows: list[list[int]] = []
            semantics_payload = {
                "schema": "public-task-semantics.v1",
                "task_family": "EVT",
                "sampling_s": 1,
                "evaluation_times": evaluation_times,
                "masked_samples": masked_samples,
                "failure_windows": failure_windows,
            }
            semantics_sha = hashlib.sha256(
                json.dumps(
                    semantics_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                ).encode("ascii")
            ).hexdigest()
            connection.execute(
                text(
                    "INSERT INTO scorer.dataset_task_semantics "
                    "(dataset_id,split_id,session_id,task_family,sampling_s,"
                    "evaluation_times_json,masked_samples_json,failure_windows_json,"
                    "semantics_sha256) VALUES (:dataset,:split,:session,'EVT',1,"
                    "CAST(:times AS jsonb),CAST(:masks AS jsonb),CAST(:windows AS jsonb),:sha)"
                ),
                {
                    "dataset": holdout_profile_id,
                    "split": split_id,
                    "session": session_id,
                    "times": json.dumps(evaluation_times),
                    "masks": json.dumps(masked_samples),
                    "windows": json.dumps(failure_windows),
                    "sha": semantics_sha,
                },
            )
        baseline_id = f"exp_{uuid4().hex}"
        candidate_blob = store_director_artifact(baseline_source, artifact_root=artifact_root)
        scorer_source_path = (
            DEFAULT_ARTIFACT_ROOT / candidate_sha[:2] / f"{candidate_sha}.json"
        )
        scorer_source_preexisting = scorer_source_path.exists() or scorer_source_path.is_symlink()
        scorer_source_sha = store_artifact_bytes(
            baseline_source, artifact_root=DEFAULT_ARTIFACT_ROOT
        )
        assert scorer_source_sha == candidate_sha
        inputs_sha = store_director_artifact(b"{}", artifact_root=artifact_root)
        registration = register_experiment(
            director,
            experiment_id=baseline_id,
            run_id=str(run_id),
            sequence=0,
            experiment_number=None,
            kind="baseline",
            baseline_name="robust_z",
            parent_experiment_id=None,
            candidate_sha256=candidate_sha,
            candidate_blob_sha256=candidate_blob,
            inputs_sha256=inputs_sha,
            move_type="detector",
            system="S1",
            hypothesis="fixture baseline for the run-end gate",
            predicted_delta=None,
            proposal={"schema": "baseline.registration.v1", "baseline_name": "robust_z"},
        )
        assert registration["status"] == "proposed"
        transition_experiment(director, experiment_id=baseline_id, status="primary_running")
        seeds = (0, 1, 2)
        plan_run_tasks(
            planner,
            run_id=run_id,
            assignments=tuple(
                RunTaskAssignment(
                    experiment_id=baseline_id,
                    evaluation_kind="baseline",
                    task_id=profile_id,
                    seed=seed,
                    candidate_sha256=candidate_sha,
                    dataset_id=profile_id,
                    split_id=split_id,
                    session_id=session_id,
                )
                for seed in seeds
            ),
        )
        score_payload = canonical_bytes(
            {
                "schema": "candidate-scores.v1",
                "sample_indices": list(range(16)),
                "scores": [0.1] * 8 + [0.9] * 8,
            }
        )
        score_digest = hashlib.sha256(score_payload).hexdigest()
        scorer_blob_path = DEFAULT_ARTIFACT_ROOT / score_digest[:2] / f"{score_digest}.json"
        scorer_blob_preexisting = scorer_blob_path.exists() or scorer_blob_path.is_symlink()
        score_job_ids = [
            enqueue_score_job(
                planner,
                run_id=run_id,
                experiment_id=baseline_id,
                evaluation_kind="baseline",
                task_id=profile_id,
                seed=seed,
                candidate_sha256=candidate_sha,
                candidate_output=score_payload,
            )
            for seed in seeds
        ]
        # The worker process derives its default private DSN from this isolated
        # worktree. Point only that private alias at this test's unique DB while
        # the exact transient worker runs, then restore it in finally.
        scorer_dsn_alias_bytes = scorer_dsn_alias.read_bytes()
        private_worker_dsn = (dsn_root / "scorer.dsn").read_bytes()
        scorer_dsn_alias.write_bytes(private_worker_dsn)
        scorer_dsn_alias.chmod(stat.S_IRUSR | stat.S_IWUSR)
        score_results = [
            run_scorer_process(job_id, remaining_seconds=90) for job_id in score_job_ids
        ]
        assert all(result.exit_code == 0 for result in score_results), [
            result.stderr_tail for result in score_results
        ]
        assert all(
            result.result is not None and result.result.get("state") == "completed"
            for result in score_results
        )
        with scorer.connect() as connection:
            score_rows = connection.execute(
                text(
                    "SELECT score FROM scorer.task_scores WHERE run_id=:run "
                    "AND experiment_id=:experiment AND evaluation_kind='baseline' "
                    "AND task_id=:task ORDER BY seed"
                ),
                {"run": run_id, "experiment": baseline_id, "task": profile_id},
            ).scalars().all()
        assert len(score_rows) == 3 and all(isinstance(row, dict) for row in score_rows)
        task_vus = [float(row["vus_pr"]) for row in score_rows]
        mean_vus = sum(task_vus) / len(task_vus)
        experiment = ExperimentDocument.model_validate(
            {
                "schema": "experiment.v1",
                "experiment_id": baseline_id,
                "run_id": run_id,
                "ordinal": 1,
                "kind": "baseline",
                "experiment_number": None,
                "baseline_name": "robust_z",
                "calibration_sha256": None,
                "agent_version": "holdout-test-baseline.v1",
                "parent_experiment_id": None,
                "candidate_sha256": candidate_sha,
                "candidate_blob_sha256": candidate_blob,
                "move_type": "detector",
                "system": "S1",
                "hypothesis": "fixture baseline for the run-end gate",
                "predicted_delta": None,
                "inputs_sha256": inputs_sha,
                "parent_tree": "e" * 40,
                "child_tree": "e" * 40,
                "harness_sha256": "c" * 64,
                "image_sha256": "d" * 64,
                "suite_id": suite_id,
                "suite_version": suite_version,
                "per_task": (
                    {
                        "task_id": profile_id,
                        "task_family": "EVT",
                        "score_norm": None,
                        "vus_pr": mean_vus,
                        "vus_roc": sum(float(row["vus_roc"]) for row in score_rows) / 3,
                        "task_score": mean_vus,
                        "fa_per_day": None,
                        "duty_fraction": None,
                        "event_f1": None,
                        "fit_seconds": 0.0,
                        "score_seconds": 0.0,
                    },
                ),
                "suite_score": None,
                "guards": {"hardcoding": "pass"},
                "decision": None,
                "status": "scored",
                "fit_seconds": 0.0,
                "score_seconds": 0.0,
                "llm_input_tokens": 0,
                "llm_output_tokens": 0,
                "wall_seconds": 0.0,
            },
            strict=True,
        )
        messages_blob = store_director_artifact(b"[]", artifact_root=artifact_root)
        trajectory = TrajectoryDocument.model_validate(
            {
                "schema": "trajectory.v1",
                "trajectory_id": f"trj_{uuid4().hex}",
                "run_id": run_id,
                "experiment_id": baseline_id,
                "kind": "baseline",
                "experiment_number": None,
                "baseline_name": "robust_z",
                "calibration_sha256": None,
                "agent_version": "holdout-test-baseline.v1",
                "model_id": "baseline/no-llm.v1",
                "usage_profile": "noncommercial_research",
                "source_provenance": (),
                "quantization": "none",
                "adapter": "none",
                "system": "S1",
                "thinking": False,
                "temperature": 0.0,
                "top_p": 1.0,
                "context_template": "baseline.no-llm.v1",
                "inputs_sha256": inputs_sha,
                "messages_blob_sha256": messages_blob,
                "tool_calls": 0,
                "outcome": None,
                "quality_tier": "bronze",
                "secrets_scrubbed": False,
                "people_scrubbed": False,
                "raw_values_scrubbed": False,
                "exclusions": ("synthetic fixture",),
            },
            strict=True,
        )
        experiment_bytes = canonical_bytes(experiment.model_dump(mode="json", by_alias=True))
        trajectory_bytes = canonical_bytes(trajectory.model_dump(mode="json", by_alias=True))
        experiment_blob = store_director_artifact(experiment_bytes, artifact_root=artifact_root)
        trajectory_blob = store_director_artifact(trajectory_bytes, artifact_root=artifact_root)
        commit_experiment_record(
            director,
            experiment=experiment,
            trajectory=trajectory,
            experiment_blob_sha256=experiment_blob,
            trajectory_blob_sha256=trajectory_blob,
        )
        with migrator.begin() as connection:
            connection.execute(
                insert(baseline_calibrations).values(
                    run_id=run_id,
                    suite_id=suite_id,
                    suite_version=suite_version,
                    calibration_sha256=calibration_sha,
                    blob_sha256=calibration_sha,
                    task_count=1,
                )
            )
        train_frame = pd.DataFrame({"signal": np.linspace(-1.0, 1.0, 128)})
        evaluation_frame = pd.DataFrame(
            {"signal": [*np.linspace(-1.0, 1.0, 256), *np.linspace(4.0, 6.0, 256)]}
        )
        register_holdout_suite(
            migrator,
            suite_id=suite_id,
            suite_version=suite_version,
            manifest_sha256=private_manifest_sha,
            development_manifest_sha256=dev_manifest_sha,
            epsilon=0.01,
            tasks=(
                HoldoutTaskSpec(
                    task_id="private-synthetic-evt",
                    dataset_id=holdout_profile_id,
                    split_id=split_id,
                    session_id=session_id,
                    profile_sha256=holdout_profile_sha,
                    family="EVT",
                    task_weight=1.0,
                    base_score=0.0,
                    reference_score=1.0,
                    sliding_window=4,
                    context=FitContext(
                        seed=0,
                        signals=("signal",),
                        regime_signals=(),
                        sampling_s=1,
                        time_budget_s=30.0,
                    ),
                    train=train_frame,
                    evaluation=evaluation_frame,
                ),
            ),
            input_root=holdout_input_root,
        )
        state = DirectorLoopState.model_validate(
            {
                "schema": "director-loop-state.v1",
                "run_id": run_id,
                "suite_manifest_sha256": dev_manifest_sha,
                "suite_id": suite_id,
                "suite_version": suite_version,
                "calibration_sha256": calibration_sha,
                "harness_sha256": "c" * 64,
                "image_sha256": "d" * 64,
                "proposal_limit": 1,
                "completed_proposals": 1,
                "next_ordinal": 2,
                "champion_experiment_id": baseline_id,
                "champion_source_sha256": candidate_sha,
                "champion_tree_sha256": "e" * 40,
                "champion_source_blob_sha256": candidate_blob,
                "champion_seed0_by_task": {profile_id: task_vus[0]},
                "champion_seed1_by_task": {profile_id: task_vus[1]},
                "champion_suite_seed_scores": tuple(task_vus),
                "champion_noise_sd": 0.0,
                "best_suite": 0.5,
                "consecutive_non_keep": 1,
                "explore_proposals": 0,
                "explore_family": None,
                "consecutive_candidate_crashes": 0,
                "discard_streak": 1,
                "previous_move_type": "feature_transform",
                "recent_feedback": (),
                "budget": {
                    "wall_seconds": 0,
                    "elapsed_wall_seconds": 0,
                    "reserved_wall_seconds": 0,
                    "model_tokens": 0,
                    "reserved_model_tokens": 0,
                    "reservations": [],
                },
                "holdout_approved_snapshot": {
                    "experiment_id": baseline_id,
                    "source_sha256": candidate_sha,
                    "tree_sha256": "e" * 40,
                    "source_blob_sha256": candidate_blob,
                    "seed0_by_task": {profile_id: task_vus[0]},
                    "seed1_by_task": {profile_id: task_vus[1]},
                    "suite_seed_scores": tuple(task_vus),
                    "noise_sd": 0.0,
                    "best_suite": 0.5,
                    "periodic_keep_watermark": 0,
                },
                "holdout_applied_keep_count": 0,
            },
            strict=True,
        ).verify_consistency()
        with DirectorRunLease(director, run_id) as lease:
            lease.append_checkpoint(
                sequence=0,
                key="proposal-abandoned:1",
                phase="proposal_abandoned",
                payload={
                    "run_id": str(run_id),
                    "ordinal": 1,
                    "reason": "tool_parse",
                    "move_type": "feature_transform",
                    "is_explore": False,
                },
                artifact_root=artifact_root,
            )
            state_checkpoint = lease.append_checkpoint(
                sequence=1,
                key="director-state:1",
                phase="director_loop_state",
                payload=state.model_dump(mode="json", by_alias=True),
                artifact_root=artifact_root,
            )
            result = DirectorLoopResult(
                run_id=run_id,
                status="proposal_limit_reached",
                completed_proposals=1,
                next_ordinal=2,
                champion_experiment_id=baseline_id,
                best_suite=0.5,
                checkpoint_sha256=state_checkpoint["payload_sha256"],
            )
            intent = register_run_end_intent(
                director,
                lease=lease,
                loop_result=result,
                artifact_root=artifact_root,
            )
            assert intent.terminal_status == "proposal_limit_reached"
            # A restarted caller may not register the same run-end intent against a
            # newer, otherwise plausible final state. The immutable first intent wins.
            changed_state = state.model_copy(
                update={"recent_feedback": ("tampered terminal state",)}
            ).verify_consistency()
            intent_checkpoint = lease.read_checkpoint(
                key="director-holdout-run-end-intent", artifact_root=artifact_root
            )
            assert intent_checkpoint is not None
            changed_checkpoint = lease.append_checkpoint(
                sequence=int(intent_checkpoint["receipt"]["sequence"]) + 1,
                key="director-state:tampered-terminal",
                phase="director_loop_state",
                payload=changed_state.model_dump(mode="json", by_alias=True),
                artifact_root=artifact_root,
            )
            changed_result = result.model_copy(
                update={"checkpoint_sha256": changed_checkpoint["payload_sha256"]}
            )
            with pytest.raises(ValueError, match="terminal intent conflicts"):
                register_run_end_intent(
                    director,
                    lease=lease,
                    loop_result=changed_result,
                    artifact_root=artifact_root,
                )
            run_worker = holdout_supervisor.run_holdout_process

            def capture_worker_result(*args, **kwargs):
                result = run_worker(*args, **kwargs)
                worker_results.append(result)
                return result

            monkeypatch.setattr(holdout_supervisor, "run_holdout_process", capture_worker_result)
            holdout_receipt = evaluate_holdout_check(
                director,
                run_id=run_id,
                candidate_experiment_id=baseline_id,
                trigger_kind="run_end",
                trigger_index=1,
                remaining_seconds=180,
            )
            assert holdout_receipt.state == "passed" and holdout_receipt.bit is True, (
                worker_results[-1].stderr_tail if worker_results else "worker did not run"
            )
            assert len(worker_results) == 1
            worker_result = worker_results[0]
            assert worker_result.exit_code == 0
            assert worker_result.unit == (
                f"swapp-ai-scientist-scorer-{holdout_receipt.reservation_id.hex}.service"
            )
            assert worker_result.invocation_id is not None
            loop = object.__new__(DirectorLoop)
            loop.director_engine = director
            loop.run_id = run_id
            loop.lease = lease
            loop.artifact_root = artifact_root
            applied = loop.apply_run_end_holdout(result, holdout_receipt)
            assert applied.champion_experiment_id == baseline_id
            assert lease.read_checkpoint(
                key="holdout-state-application:run_end:1", artifact_root=artifact_root
            ) is not None
            assert lease.read_checkpoint(
                key="director-state:1:run_end", artifact_root=artifact_root
            ) is not None
        with director.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT completed_proposals FROM lab.holdout_run_end_intents WHERE run_id=:run"
                ),
                {"run": run_id},
            ).one()
            assert row.completed_proposals == 1
            assert connection.execute(
                text("SELECT count(*) FROM lab.experiments WHERE run_id=:run AND kind='proposal'"),
                {"run": run_id},
            ).scalar_one() == 0
    finally:
        if scorer_dsn_alias_bytes is not None:
            scorer_dsn_alias.write_bytes(scorer_dsn_alias_bytes)
            scorer_dsn_alias.chmod(stat.S_IRUSR | stat.S_IWUSR)
        has_durable_holdout = False
        if run_id is not None:
            with migrator.connect() as connection:
                has_durable_holdout = bool(
                    connection.execute(
                        text(
                            "SELECT EXISTS(SELECT 1 FROM lab.holdout_reservations "
                            "WHERE run_id=:run_id)"
                        ),
                        {"run_id": run_id},
                    ).scalar_one()
                )
        if run_id is not None and not has_durable_holdout:
            with migrator.begin() as connection:
                connection.execute(
                    text("DELETE FROM lab.runs WHERE run_id=:run_id"), {"run_id": run_id}
                )
            # The registered private suite and tasks are immutable by contract. They
            # remain in this disposable DB even when the run fails before reservation.
            shutil.rmtree(artifact_root, ignore_errors=True)
            if scorer_source_path is not None and not scorer_source_preexisting:
                scorer_source_path.unlink(missing_ok=True)
                try:
                    scorer_source_path.parent.rmdir()
                except OSError:
                    pass
            if scorer_blob_path is not None and not scorer_blob_preexisting:
                scorer_blob_path.unlink(missing_ok=True)
                try:
                    scorer_blob_path.parent.rmdir()
                except OSError:
                    pass
        migrator.dispose()
        director.dispose()
        planner.dispose()
        scorer.dispose()
