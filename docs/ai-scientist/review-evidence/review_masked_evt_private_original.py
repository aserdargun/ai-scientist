"""Private PostgreSQL proof that Scorer applies registered EVT masks."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, delete, text

from harness.metrics import vus_metrics
from lab.db.schema import dataset_labels, dataset_profiles, runs
from lab.director.ledger import register_experiment, transition_experiment
from lab.director.task_plan import RunTaskAssignment, plan_run_tasks
from lab.scorer.jobs import enqueue_score_job
from lab.scorer.supervisor import (
    SCORER_SLICE,
    SYSTEMD_RUN,
    WORKER_CPU_QUOTA,
    WORKER_MEMORY,
    WORKER_TASKS,
    _ensure_aggregate_slice,
)
from lab.scorer.worker import _secret

ROOT = Path(__file__).resolve().parents[1]
SECRETS = ROOT / "data/runtime/postgres"


def main() -> int:
    migrator = create_engine(_secret(SECRETS / "migrator.dsn"), pool_size=1, max_overflow=0)
    director = create_engine(_secret(SECRETS / "director.dsn"), pool_size=1, max_overflow=0)
    planner = create_engine(_secret(SECRETS / "planner.dsn"), pool_size=1, max_overflow=0)
    scorer_engine = create_engine(_secret(SECRETS / "scorer.dsn"), pool_size=1, max_overflow=0)
    run_id = uuid4()
    artifact_root = ROOT / "data/runtime/holdout-mask-review" / run_id.hex / "blobs"
    profile = (f"holdout-mask-{uuid4().hex}", "eval", "session")
    experiment_id = f"exp_{uuid4().hex}"
    task_id = "masked-evt"
    candidate_sha = "a" * 64
    labels = [False, False, True, False, False, True, False, False]
    masks = [False, True, False, False, False, False, False, False]
    scores = [0.1, 0.99, 0.8, 0.3, 0.2, 0.9, 0.1, 0.05]
    times = list(range(0, 480, 60))
    semantics = {
        "schema": "public-task-semantics.v1",
        "task_family": "EVT",
        "sampling_s": 60,
        "evaluation_times": times,
        "masked_samples": masks,
        "failure_windows": [],
    }
    semantics_sha = hashlib.sha256(
        json.dumps(semantics, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
            "ascii"
        )
    ).hexdigest()
    profile_sha = hashlib.sha256(f"{profile[0]}:{profile[1]}:{profile[2]}".encode()).hexdigest()
    payload = json.dumps(
        {
            "schema": "candidate-scores.v1",
            "sample_indices": list(range(len(scores))),
            "scores": scores,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    try:
        with migrator.begin() as connection:
            connection.execute(
                runs.insert().values(
                    run_id=run_id,
                    origin="local",
                    owner_id=f"holdout-mask-review:{uuid4()}",
                    idempotency_key=f"holdout-mask-{uuid4().hex}",
                    payload_sha256=hashlib.sha256(str(run_id).encode()).hexdigest(),
                    request_json={"suite": "synthetic.holdout-mask-review.v1"},
                    state="running",
                )
            )
            connection.execute(
                text(
                    "INSERT INTO scorer.dataset_profiles "
                    "(dataset_id,split_id,session_id,sample_count,sliding_window,profile_sha256,"
                    "visibility,task_family) VALUES "
                    "(:dataset,:split,:session,8,2,:profile,'dev','EVT')"
                ),
                {
                    "dataset": profile[0],
                    "split": profile[1],
                    "session": profile[2],
                    "profile": profile_sha,
                },
            )
            connection.execute(
                dataset_labels.insert(),
                [
                    {
                        "dataset_id": profile[0],
                        "split_id": profile[1],
                        "session_id": profile[2],
                        "sample_index": index,
                        "is_anomaly": label,
                    }
                    for index, label in enumerate(labels)
                ],
            )
            connection.execute(
                text(
                    "INSERT INTO scorer.dataset_task_semantics "
                    "(dataset_id,split_id,session_id,task_family,sampling_s,evaluation_times_json,"
                    "masked_samples_json,failure_windows_json,semantics_sha256) "
                    "VALUES (:dataset,:split,:session,'EVT',60,CAST(:times AS jsonb),"
                    "CAST(:masks AS jsonb),'[]'::jsonb,:digest)"
                ),
                {
                    "dataset": profile[0],
                    "split": profile[1],
                    "session": profile[2],
                    "times": json.dumps(times),
                    "masks": json.dumps(masks),
                    "digest": semantics_sha,
                },
            )
        register_experiment(
            director,
            experiment_id=experiment_id,
            run_id=str(run_id),
            sequence=0,
            experiment_number=None,
            kind="baseline",
            baseline_name="robust_z",
            parent_experiment_id=None,
            candidate_sha256=candidate_sha,
            candidate_blob_sha256="c" * 64,
            inputs_sha256="d" * 64,
            move_type="detector",
            system="S1",
            hypothesis="Synthetic masked EVT scoring review.",
            predicted_delta=None,
            proposal={"baseline_name": "robust_z"},
        )
        transition_experiment(director, experiment_id=experiment_id, status="primary_running")
        plan_run_tasks(
            planner,
            run_id=run_id,
            assignments=(
                RunTaskAssignment(
                    experiment_id=experiment_id,
                    evaluation_kind="baseline",
                    task_id=task_id,
                    seed=0,
                    candidate_sha256=candidate_sha,
                    dataset_id=profile[0],
                    split_id=profile[1],
                    session_id=profile[2],
                ),
            ),
        )
        job_id = enqueue_score_job(
            planner,
            run_id=run_id,
            experiment_id=experiment_id,
            evaluation_kind="baseline",
            task_id=task_id,
            seed=0,
            candidate_sha256=candidate_sha,
            candidate_output=payload,
            artifact_root=artifact_root,
        )
        _ensure_aggregate_slice()
        unit = f"swapp-ai-scientist-scorer-{job_id.hex}.service"
        worker_run = subprocess.run(
            [
                SYSTEMD_RUN,
                "--user",
                "--wait",
                "--collect",
                "--quiet",
                "--pipe",
                f"--unit={unit}",
                f"--slice={SCORER_SLICE}",
                f"--property=MemoryMax={WORKER_MEMORY}",
                "--property=MemorySwapMax=0",
                f"--property=CPUQuota={WORKER_CPU_QUOTA}",
                f"--property=TasksMax={WORKER_TASKS}",
                "--property=RuntimeMaxSec=120",
                "--property=TimeoutStopSec=3",
                "--property=KillMode=control-group",
                "--property=OOMPolicy=stop",
                "--property=NoNewPrivileges=yes",
                f"--working-directory={ROOT}",
                "--setenv=CUDA_VISIBLE_DEVICES=",
                "--setenv=LAB_SCORER_DSN_FILE=" + str(SECRETS / "scorer.dsn"),
                "--setenv=LAB_ARTIFACT_ROOT=" + str(artifact_root),
                "--setenv=OPENBLAS_NUM_THREADS=1",
                "--setenv=OMP_NUM_THREADS=1",
                "--setenv=MKL_NUM_THREADS=1",
                str(ROOT / ".venv/bin/python"),
                "-m",
                "lab.scorer.worker",
                "--job-id",
                str(job_id),
            ],
            capture_output=True,
            text=True,
            timeout=150,
            check=False,
        )
        worker_payload = json.loads(worker_run.stdout.strip().splitlines()[-1])
        assert worker_run.returncode == 0, (worker_run.returncode, worker_payload)
        assert worker_payload.get("state") == "completed", worker_payload
        keep = [not masked for masked in masks]
        expected = vus_metrics(
            [label for label, include in zip(labels, keep, strict=True) if include],
            [score for score, include in zip(scores, keep, strict=True) if include],
            sliding_window=2,
        )
        with migrator.connect() as connection:
            result = connection.execute(
                text(
                    "SELECT score FROM scorer.task_scores WHERE run_id=:run "
                    "AND experiment_id=:experiment AND task_id=:task"
                ),
                {"run": run_id, "experiment": experiment_id, "task": task_id},
            ).scalar_one()
        assert result["masked_sample_count"] == 1
        assert result["scored_sample_count"] == 7
        assert result["vus_pr"] == expected.vus_pr
        assert result["vus_roc"] == expected.vus_roc
        print(json.dumps({"checks": 6, "masked_samples": 1, "scored_samples": 7, "exit_code": 0}))
        return 0
    finally:
        if artifact_root.parent.exists() and not artifact_root.parent.is_symlink():
            shutil.rmtree(artifact_root.parent)
        with migrator.begin() as connection:
            connection.execute(delete(runs).where(runs.c.run_id == run_id))
            connection.execute(
                delete(dataset_profiles).where(
                    dataset_profiles.c.dataset_id == profile[0],
                    dataset_profiles.c.split_id == profile[1],
                    dataset_profiles.c.session_id == profile[2],
                )
            )
        scorer_engine.dispose()
        planner.dispose()
        director.dispose()
        migrator.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
