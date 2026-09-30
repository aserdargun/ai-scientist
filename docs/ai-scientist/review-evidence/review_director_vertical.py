"""Measure one real Director proposal after a fresh three-algorithm calibration."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import numpy as np
from review_director_synthetic_scenario import RESIDUAL_VERBOSE, fixture_tasks
from review_scorer_queue import ROOT, engine
from sqlalchemy import delete, insert, text

from harness.contracts import FitContext
from harness.fingerprint import compute_harness_hash
from lab.db.schema import dataset_labels, dataset_profiles, runs
from lab.director.baselines import (
    BASELINE_NAMES,
    TrustedCalibrationTask,
    baseline_candidate_source,
    freeze_calibration_from_database,
)
from lab.director.budget import RunBudget
from lab.director.contracts import (
    CandidateProposal,
    ExperimentDocument,
    SourceProvenance,
    TrajectoryDocument,
)
from lab.director.fake_llm import AgentContext, FakeLLM, ProposalTurn
from lab.director.journal import DirectorRunLease
from lab.director.ledger import (
    canonical_json_bytes,
    commit_experiment_record,
    register_experiment,
    transition_experiment,
)
from lab.director.runner import run_one_proposal
from lab.director.suite import SuiteTask
from lab.director.task_plan import RunTaskAssignment, plan_run_tasks, seal_run_task_plan
from lab.sandbox.docker_runner import DEFAULT_SANDBOX_IMAGE, LocalDockerRunner
from lab.sandbox.evaluation import run_guarded_seed_evaluation
from lab.scorer.jobs import enqueue_score_job, read_candidate_artifact
from lab.scorer.supervisor import run_scorer_finalize_process, run_scorer_process


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def git_tree(directory: Path, source: bytes) -> str:
    """Create actual candidate Git objects in this review's private bare repo."""
    subprocess.run(["git", "init", "--bare", "--quiet", str(directory)], check=True)
    blob = subprocess.run(
        ["git", "--git-dir", str(directory), "hash-object", "-w", "--stdin"],
        input=source,
        capture_output=True,
        check=True,
    ).stdout.strip()
    return (
        subprocess.run(
            ["git", "--git-dir", str(directory), "mktree"],
            input=b"100644 blob " + blob + b"\tpipeline.py\n",
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )


def main() -> None:
    # Imported at execution time so a missing production storage API fails before fixtures.
    from lab.director.artifacts import (
        read_director_artifact as read_blob,
    )
    from lab.director.artifacts import (
        read_registered_calibration,
        register_frozen_calibration,
    )
    from lab.director.artifacts import (
        store_director_artifact as store_blob,
    )

    run_id = uuid4()
    runtime = ROOT / "data/runtime/director-vertical-review" / str(run_id)
    runtime.mkdir(parents=True, mode=0o700)
    blob_root = runtime / "blobs"
    migrator, director, planner = (engine(role) for role in ("migrator", "director", "planner"))
    initial_hash = compute_harness_hash(ROOT).sha256
    evidence = {
        "schema": "director-vertical-review.v1",
        "checked_at": datetime.now(UTC).isoformat(),
        "run_id": str(run_id),
        "scope": (
            "Four synthetic EVT profiles; three algorithms and seeds 0–2; real isolated "
            "Docker fit/score/guards, separate systemd Scorer and PostgreSQL. Then one "
            "production run_one_proposal with actual seed 0/1/2, terminal pair and report. "
            "No Director20, public-data, real LLM/GPU or AOS acceptance."
        ),
        "harness_sha256": initial_hash,
        "image": DEFAULT_SANDBOX_IMAGE,
        "checks": {},
        "measurements": [],
        "blob_root": str(blob_root.relative_to(ROOT)),
        "documents": [],
    }
    owned_datasets = []
    blob_digests = set()

    def persist(payload: bytes) -> str:
        value = store_blob(payload, artifact_root=blob_root)
        assert value == digest(payload)
        assert read_blob(value, artifact_root=blob_root) == payload
        blob_digests.add(value)
        return value

    try:
        with migrator.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM lab.alembic_version")).scalar_one()
                == "0014_immutable_events"
            )
        with director.connect() as connection:
            connection.execute(
                text("SELECT experiment_id,sequence,status FROM lab.experiments LIMIT 0")
            )
            connection.execute(text("SELECT * FROM lab.dev_task_results LIMIT 0"))
            connection.execute(text("SELECT report_json,report_sha256 FROM lab.reports LIMIT 0"))
            assert connection.execute(
                text(
                    "SELECT has_function_privilege(current_user, "
                    "'lab.experiment_record_receipt(text)', 'EXECUTE')"
                )
            ).scalar_one()
        budget = RunBudget(proposal_limit=1, wall_limit=3600, token_limit=40000)
        fixtures = []
        for index, (train_frame, eval_frame, labels) in enumerate(fixture_tasks()):
            name = f"director-live-{run_id}-{index}"
            train = train_frame.to_numpy()
            evaluation = eval_frame.to_numpy()
            profile_payload = canonical_json_bytes(
                {
                    "dataset_id": name,
                    "train": train.tolist(),
                    "evaluation": evaluation.tolist(),
                    "labels": labels.tolist(),
                    "sampling_s": 60,
                    "sliding_window": 4,
                }
            )
            profile_sha = digest(profile_payload)
            (runtime / f"synthetic-profile-{index}.json").write_bytes(profile_payload)
            task = TrustedCalibrationTask(
                task_id=f"synthetic-task-{index}",
                dataset_id=name,
                split_id="review-v1",
                session_id="local",
                profile_sha256=profile_sha,
                family="EVT",
                weight=0.25,
            )
            fixtures.append((task, train_frame, eval_frame, labels))
            owned_datasets.append(name)
        with migrator.begin() as connection:
            request = {"suite": "synthetic.director-vertical.v1", "profile": "review-only"}
            connection.execute(
                insert(runs).values(
                    run_id=run_id,
                    origin="local",
                    owner_id=f"review:{run_id}",
                    idempotency_key=str(run_id),
                    payload_sha256=digest(canonical_json_bytes(request)),
                    request_json=request,
                    state="running",
                )
            )
            for task, _, _, labels in fixtures:
                connection.execute(
                    insert(dataset_profiles).values(
                        dataset_id=task.dataset_id,
                        split_id=task.split_id,
                        session_id=task.session_id,
                        sample_count=len(labels),
                        sliding_window=4,
                        profile_sha256=task.profile_sha256,
                        visibility="dev",
                    )
                )
                connection.execute(
                    insert(dataset_labels),
                    [
                        {
                            "dataset_id": task.dataset_id,
                            "split_id": task.split_id,
                            "session_id": task.session_id,
                            "sample_index": i,
                            "is_anomaly": bool(label),
                        }
                        for i, label in enumerate(labels)
                    ],
                )
        runner = LocalDockerRunner(image=DEFAULT_SANDBOX_IMAGE, work_root=runtime / "sandbox")
        for ordinal, algorithm in enumerate(BASELINE_NAMES, start=1):
            started = time.monotonic()
            source = baseline_candidate_source(algorithm)
            candidate_sha = persist(source)
            tree = git_tree(runtime / f"{algorithm}.git", source)
            exp_id = f"exp_{uuid4().hex}"
            inputs_sha = persist(
                canonical_json_bytes(
                    {
                        "algorithm": algorithm,
                        "harness_sha256": initial_hash,
                        "tasks": [task.model_dump(mode="json") for task, *_ in fixtures],
                        "seeds": [0, 1, 2],
                    }
                )
            )
            hypothesis = f"Measure frozen {algorithm} reference on the declared synthetic tasks."
            registration = register_experiment(
                director,
                experiment_id=exp_id,
                run_id=str(run_id),
                sequence=ordinal - 1,
                experiment_number=None,
                kind="baseline",
                baseline_name=algorithm,
                parent_experiment_id=None,
                candidate_sha256=candidate_sha,
                candidate_blob_sha256=candidate_sha,
                inputs_sha256=inputs_sha,
                move_type="detector",
                system="S1",
                hypothesis=hypothesis,
                predicted_delta=None,
                proposal={"schema": "baseline.registration.v1", "algorithm": algorithm},
            )
            assert registration["status"] == "proposed"
            assert (
                transition_experiment(director, experiment_id=exp_id, status="primary_running")
                == "primary_running"
            )
            assignments = tuple(
                RunTaskAssignment(
                    experiment_id=exp_id,
                    evaluation_kind="baseline",
                    task_id=task.task_id,
                    seed=seed,
                    candidate_sha256=candidate_sha,
                    dataset_id=task.dataset_id,
                    split_id=task.split_id,
                    session_id=task.session_id,
                )
                for task, *_ in fixtures
                for seed in range(3)
            )
            plan_run_tasks(planner, run_id=run_id, assignments=assignments)
            timings = {}
            for task, train, evaluation, _ in fixtures:
                for seed in range(3):
                    assert compute_harness_hash(ROOT).sha256 == initial_hash
                    output = run_guarded_seed_evaluation(
                        runner,
                        candidate_source=source,
                        train=train,
                        evaluation=evaluation,
                        context=FitContext(
                            seed=seed,
                            signals=("sensor_a", "sensor_b"),
                            regime_signals=(),
                            sampling_s=60,
                            time_budget_s=90.0,
                        ),
                        task_ids=frozenset(item.task_id for item, *_ in fixtures),
                        evaluation_instants=frozenset(),
                        trusted_baseline_name=algorithm,
                        remaining_seconds=120,
                    )
                    assert (
                        output.determinism.passed
                        and output.causality.passed
                        and output.hardcoding.passed
                    )
                    job_id = enqueue_score_job(
                        planner,
                        run_id=run_id,
                        experiment_id=exp_id,
                        evaluation_kind="baseline",
                        task_id=task.task_id,
                        seed=seed,
                        candidate_sha256=candidate_sha,
                        candidate_output=output.evaluation.score_document,
                    )
                    process = run_scorer_process(job_id)
                    assert process.exit_code == 0 and process.result is not None
                    assert process.result["state"] == "completed"
                    assert process.result["worker_pid"] != str(os.getpid())
                    assert process.result["run_finalized"] == "false"
                    artifact_sha = digest(output.evaluation.score_document)
                    assert read_candidate_artifact(artifact_sha) == output.evaluation.score_document
                    timings[(task.task_id, seed)] = (
                        output.evaluation.fit_seconds,
                        output.evaluation.score_seconds,
                    )
                    evidence["measurements"].append(
                        {
                            "algorithm": algorithm,
                            "experiment_id": exp_id,
                            "task_id": task.task_id,
                            "seed": seed,
                            "job_id": str(job_id),
                            "scorer_unit": process.unit,
                            "worker_pid": process.result["worker_pid"],
                            "artifact_sha256": artifact_sha,
                            "fit_container": output.evaluation.fit_container_name,
                            "score_container": output.evaluation.score_container_name,
                            "fit_seconds": output.evaluation.fit_seconds,
                            "score_seconds": output.evaluation.score_seconds,
                        }
                    )
                    print(
                        json.dumps(
                            {
                                "measured": len(evidence["measurements"]),
                                "total": 36,
                                "algorithm": algorithm,
                                "task": task.task_id,
                                "seed": seed,
                            }
                        ),
                        flush=True,
                    )
            with director.connect() as connection:
                measured = (
                    connection.execute(
                        text(
                            "SELECT * FROM lab.dev_task_results WHERE run_id=:run "
                            "AND experiment_id=:exp ORDER BY task_id,seed"
                        ),
                        {"run": run_id, "exp": exp_id},
                    )
                    .mappings()
                    .all()
                )
            assert len(measured) == 12
            evidence.setdefault("scorer_rows", []).extend(
                dict(row) | {"run_id": str(run_id)} for row in measured
            )
            per_task = []
            for task, *_ in fixtures:
                rows = [row for row in measured if row["task_id"] == task.task_id]
                assert {int(row["seed"]) for row in rows} == {0, 1, 2}
                per_task.append(
                    {
                        "task_id": task.task_id,
                        "score_norm": None,
                        "vus_pr": float(np.mean([float(row["vus_pr"]) for row in rows])),
                        "vus_roc": float(np.mean([float(row["vus_roc"]) for row in rows])),
                        "fa_per_day": None,
                        "event_f1": None,
                        "fit_seconds": sum(timings[(task.task_id, seed)][0] for seed in range(3)),
                        "score_seconds": sum(timings[(task.task_id, seed)][1] for seed in range(3)),
                    }
                )
            identity = dict(
                run_id=run_id,
                experiment_id=exp_id,
                kind="baseline",
                experiment_number=None,
                baseline_name=algorithm,
                calibration_sha256=None,
                agent_version="baseline-review/no-llm",
                inputs_sha256=inputs_sha,
                system="S1",
            )
            experiment = ExperimentDocument.model_validate(
                dict(
                    identity,
                    schema="experiment.v1",
                    ordinal=ordinal,
                    parent_experiment_id=None,
                    candidate_sha256=candidate_sha,
                    candidate_blob_sha256=candidate_sha,
                    move_type="detector",
                    hypothesis=hypothesis,
                    predicted_delta=None,
                    parent_tree=tree,
                    child_tree=tree,
                    harness_sha256=initial_hash,
                    image_sha256=DEFAULT_SANDBOX_IMAGE.removeprefix("sha256:"),
                    suite_id="synthetic.director-vertical.v1",
                    suite_version=1,
                    per_task=tuple(per_task),
                    suite_score=None,
                    guards={"determinism": "pass", "causality": "pass", "hardcoding": "pass"},
                    decision=None,
                    status="scored",
                    fit_seconds=sum(item[0] for item in timings.values()),
                    score_seconds=sum(item[1] for item in timings.values()),
                    llm_input_tokens=0,
                    llm_output_tokens=0,
                    wall_seconds=time.monotonic() - started,
                )
            )
            trajectory = TrajectoryDocument.model_validate(
                dict(
                    identity,
                    schema="trajectory.v1",
                    trajectory_id=f"trj_{uuid4().hex}",
                    model_id="baseline/no-llm",
                    usage_profile="noncommercial_research",
                    source_provenance=tuple(
                        {
                            "dataset_id": task.dataset_id,
                            "split_id": task.split_id,
                            "session_id": task.session_id,
                            "source_manifest_sha256": task.profile_sha256,
                            "source_revision": "synthetic-review-v1",
                            "license_id": "CC0-1.0",
                            "attribution": "Local generated review fixture",
                            "access_terms": "Synthetic data generated for this review",
                            "usage_profile": "noncommercial_research",
                        }
                        for task, *_ in fixtures
                    ),
                    quantization="none",
                    adapter="none",
                    thinking=False,
                    temperature=0.0,
                    top_p=1.0,
                    context_template="baseline.review.v1",
                    messages_blob_sha256=persist(b"[]"),
                    tool_calls=0,
                    outcome=None,
                    quality_tier="bronze",
                    secrets_scrubbed=False,
                    people_scrubbed=False,
                    raw_values_scrubbed=False,
                    exclusions=("Baseline calibration; no LLM episode or candidate verdict.",),
                )
            )
            exp_blob = persist(canonical_json_bytes(experiment))
            traj_blob = persist(canonical_json_bytes(trajectory))
            receipt = commit_experiment_record(
                director,
                experiment=experiment,
                trajectory=trajectory,
                experiment_blob_sha256=exp_blob,
                trajectory_blob_sha256=traj_blob,
            )
            assert receipt["status"] == "committed"
            evidence["documents"].append(
                {
                    "experiment_id": exp_id,
                    "experiment_blob": exp_blob,
                    "trajectory_blob": traj_blob,
                    "candidate_blob": candidate_sha,
                }
            )
        document = freeze_calibration_from_database(
            director,
            run_id=run_id,
            runner_image=DEFAULT_SANDBOX_IMAGE,
            harness_sha256=initial_hash,
            suite_id="synthetic.director-vertical.v1",
            suite_version=1,
            expected_tasks=tuple(task for task, *_ in reversed(fixtures)),
        )
        receipt = register_frozen_calibration(director, document, artifact_root=blob_root)
        recovered = read_registered_calibration(director, run_id=run_id, artifact_root=blob_root)
        assert recovered == document
        evidence["calibration"] = document.model_dump(mode="json", by_alias=True)
        evidence["receipt"] = receipt
        evidence["checks"]["physical_calibration_roundtrip"] = True
        evidence["checks"]["exact_36_guarded_measurements"] = len(evidence["measurements"]) == 36
        evidence["checks"]["deterministic_champion_noise_zero"] = document.champion_noise_sd == 0.0
        # All reference scores above were measured by the real sandbox and Scorer.
        # The fake provider supplies only a candidate source and metadata.
        parent_source = baseline_candidate_source("robust_z")
        parent_doc = next(
            item
            for item in evidence["documents"]
            if item["experiment_id"] == document.champion_experiment_id
        )
        parent_record = json.loads(
            read_blob(parent_doc["experiment_blob"], artifact_root=blob_root)
        )
        task_ids = frozenset(task.task_id for task, *_ in fixtures)
        suite_tasks = tuple(
            SuiteTask.from_frames(
                task_id=task.task_id,
                dataset_id=task.dataset_id,
                split_id=task.split_id,
                session_id=task.session_id,
                profile_sha256=task.profile_sha256,
                family="EVT",
                task_weight=task.weight,
                provenance=SourceProvenance(
                    dataset_id=task.dataset_id,
                    split_id=task.split_id,
                    session_id=task.session_id,
                    source_manifest_sha256=task.profile_sha256,
                    source_revision="synthetic-review-v1",
                    license_id="CC0-1.0",
                    attribution="Local generated review fixture",
                    access_terms="Synthetic data generated for this review",
                    usage_profile="noncommercial_research",
                ),
                context=FitContext(
                    seed=0,
                    signals=tuple(train.columns),
                    regime_signals=(),
                    sampling_s=60,
                    time_budget_s=600.0,
                ),
                train=train,
                evaluation=evaluation,
                task_ids_for_guard=task_ids,
            )
            for task, train, evaluation, _ in fixtures
        )
        hypothesis = (
            "Learn the normal training relationship between sensors and score absolute residuals."
        )
        provider = FakeLLM(
            (
                ProposalTurn(
                    proposal=CandidateProposal(
                        hypothesis=hypothesis,
                        move_type="detector",
                        candidate_source=RESIDUAL_VERBOSE,
                        predicted_delta=0.1,
                    ),
                    messages=(hypothesis,),
                    input_tokens=100,
                    output_tokens=600,
                ),
            )
        )
        context = AgentContext(
            phase="LOOP",
            experiment_number=1,
            task_cards=(
                "Four synthetic EVT tasks; two sensors; 60-second cadence; labels withheld.",
            ),
            champion_source=parent_source.decode("utf-8"),
            recent_feedback=(),
        )
        print(json.dumps({"phase": "director_proposal", "proposal": 1}), flush=True)
        with DirectorRunLease(director, run_id) as lease:
            proposal_receipt = run_one_proposal(
                director,
                planner,
                lease=lease,
                runner=runner,
                run_id=run_id,
                ordinal=1,
                parent_experiment_id=document.champion_experiment_id,
                parent_tree_sha256=parent_record["child_tree"],
                parent_source=parent_source,
                suite_id=document.suite_id,
                suite_version=document.suite_version,
                calibration=document,
                harness_sha256=initial_hash,
                image_sha256=DEFAULT_SANDBOX_IMAGE.removeprefix("sha256:"),
                system="S1",
                context=context,
                provider=provider,
                tasks=suite_tasks,
                budget=budget,
                artifact_root=blob_root,
                best_suite=0.0,
                champion_noise=document.champion_noise_sd,
            )
            evidence["proposal_receipt"] = proposal_receipt
            with director.connect() as connection:
                proposal_row = dict(
                    connection.execute(
                        text(
                            "SELECT experiment_id, sequence, status FROM lab.experiments "
                            "WHERE run_id=:run AND kind='proposal'"
                        ),
                        {"run": run_id},
                    )
                    .mappings()
                    .one()
                )
                receipt = connection.execute(
                    text("SELECT lab.experiment_record_receipt(:experiment)"),
                    {"experiment": proposal_row["experiment_id"]},
                ).scalar_one()
                proposal_row.update(receipt)
                for key in ("experiment", "trajectory"):
                    payload = read_blob(receipt[f"{key}_blob_sha256"], artifact_root=blob_root)
                    assert digest(payload) == receipt[f"{key}_sha256"]
                    proposal_row[f"{key}_json"] = json.loads(payload)
                proposal_scores = (
                    connection.execute(
                        text(
                            "SELECT * FROM lab.dev_task_results "
                            "WHERE run_id=:run AND experiment_id=:exp "
                            "ORDER BY evaluation_kind,seed,task_id"
                        ),
                        {"run": run_id, "exp": proposal_row["experiment_id"]},
                    )
                    .mappings()
                    .all()
                )
                noise_record = lease.read_checkpoint(
                    key=f"champion-noise:{proposal_row['experiment_id']}", artifact_root=blob_root
                )
            evidence["proposal_receipt"] = proposal_receipt
            evidence["proposal_document"] = proposal_row["experiment_json"]
            evidence["proposal_trajectory"] = proposal_row["trajectory_json"]
            evidence["proposal_scores"] = [
                dict(row) | {"run_id": str(run_id)} for row in proposal_scores
            ]
            evidence["champion_noise_checkpoint"] = noise_record
            evidence["checks"]["actual_referee_keep_after_confirmation"] = (
                proposal_row["status"] == "scored"
                and proposal_row["experiment_json"]["decision"]["verdict"] == "KEEP"
                and len(proposal_scores) == 12
                and {(row["evaluation_kind"], row["seed"]) for row in proposal_scores}
                == {("primary", 0), ("confirmation", 1), ("confirmation", 2)}
            )
            evidence["checks"]["one_proposal_after_three_baselines"] = (
                proposal_row["sequence"] == 3
                and proposal_row["experiment_json"]["ordinal"] == 4
                and proposal_row["experiment_json"]["experiment_number"] == 1
            )
            for field, document_name in (
                ("experiment_blob_sha256", "experiment_json"),
                ("trajectory_blob_sha256", "trajectory_json"),
            ):
                payload = read_blob(proposal_row[field], artifact_root=blob_root)
                assert digest(payload) == proposal_row[field]
                assert json.loads(payload) == proposal_row[document_name]
                blob_digests.add(proposal_row[field])
            evidence["checks"]["measured_new_champion_noise"] = (
                noise_record is not None
                and noise_record["payload"]["seed_set"] == [0, 1, 2]
                and noise_record["payload"]["noise_sd"] == 0.0
            )
        evidence["budget"] = {
            "proposal_count": budget.proposal_count,
            "model_tokens": budget.model_tokens,
            "wall_seconds": budget.wall_seconds,
            "remaining_wall_seconds": budget.remaining_wall_seconds,
        }
        seal = seal_run_task_plan(planner, run_id=run_id)
        assert seal.task_count == 48
        finalized = run_scorer_finalize_process(run_id)
        assert finalized.exit_code == 0 and finalized.result["state"] == "finalized"
        evidence["checks"]["separate_finalizer"] = finalized.result["worker_pid"] != str(
            os.getpid()
        )
        retry = register_frozen_calibration(director, document, artifact_root=blob_root)
        evidence["checks"]["same_receipt_retry_after_terminal"] = (
            retry["status"] == "already_registered"
        )
        with director.connect() as connection:
            report = (
                connection.execute(
                    text("SELECT report_json,report_sha256 FROM lab.reports WHERE run_id=:run"),
                    {"run": run_id},
                )
                .mappings()
                .one()
            )
        assert digest(canonical_json_bytes(report["report_json"])) == report["report_sha256"]
        evidence["report"] = dict(report)
        evidence["checks"]["report_has_all_measurements"] = (
            len(report["report_json"]["task_scores"]) == 48
        )
        evidence["checks"]["all_generic_blobs_read_back"] = all(
            digest(read_blob(sha, artifact_root=blob_root)) == sha for sha in blob_digests
        )
    except Exception as error:
        evidence["error_type"] = type(error).__name__
        raise
    finally:
        with migrator.begin() as connection:
            connection.execute(delete(runs).where(runs.c.run_id == run_id))
            for dataset in owned_datasets:
                connection.execute(
                    delete(dataset_profiles).where(dataset_profiles.c.dataset_id == dataset)
                )
        for item in (migrator, director, planner):
            item.dispose()
        evidence["source_unchanged"] = compute_harness_hash(ROOT).sha256 == initial_hash
        evidence["all_passed"] = (
            bool(evidence["checks"])
            and all(evidence["checks"].values())
            and evidence["source_unchanged"]
            and "error_type" not in evidence
        )
        evidence["script_sha256"] = digest(Path(__file__).read_bytes())
        evidence["command"] = (
            "OPENBLAS_NUM_THREADS=2 OMP_NUM_THREADS=2 .venv/bin/python "
            "docs/ai-scientist/review-evidence/review_director_vertical.py"
        )
        Path(__file__).with_name("director-vertical-review.json").write_text(
            json.dumps(evidence, indent=2) + "\n"
        )
    assert evidence["all_passed"]
    print(
        json.dumps(
            {
                "all_passed": True,
                "baseline_measurements": 36,
                "proposal_measurements": 12,
                "checks": evidence["checks"],
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
