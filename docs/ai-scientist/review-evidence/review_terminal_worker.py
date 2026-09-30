"""Run real bounded worker processes for artifact retry and candidate rejection."""
from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from uuid import uuid4

from sqlalchemy import delete, insert, select

from lab.db.schema import dataset_profiles, dataset_labels, runs, score_jobs, task_scores, task_terminal_outcomes
from lab.director.task_plan import RunTaskAssignment, plan_run_tasks, seal_run_task_plan
from lab.scorer.jobs import enqueue_score_job
from lab.scorer.supervisor import _ensure_aggregate_slice, SCORER_SLICE
from review_scorer_queue import engine

ROOT = Path(__file__).resolve().parents[3]
OUTPUT = Path(__file__).with_name("terminal-worker-review.json")


def main():
    admin, planner, scorer = (engine(role) for role in ("migrator", "planner", "scorer"))
    dataset = "owned-terminal-worker-review-" + uuid4().hex
    candidate = hashlib.sha256(dataset.encode()).hexdigest()
    owned_runs = []
    record = {
        "checked_at": datetime.now(UTC).isoformat(), "checks": {}, "processes": [],
        "scope": "Actual lab.scorer.worker subprocesses under the production aggregate Scorer slice and equivalent per-unit CPU/RAM/PID/deadline bounds. Private artifact root is supplied by a review launcher. Real PostgreSQL roles and owned synthetic rows only. No worker-drain, Director, GPU or AOS acceptance.",
        "source_sha256": {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in (
            "lab/scorer/worker.py", "lab/scorer/service.py", "lab/scorer/jobs.py", "lab/scorer/supervisor.py",
        )},
    }
    checks = record["checks"]

    def fixture(payload, blob_root):
        run_id = uuid4()
        owned_runs.append(run_id)
        with admin.begin() as connection:
            connection.execute(insert(runs).values(
                run_id=run_id, origin="local", owner_id=dataset, idempotency_key=str(run_id),
                payload_sha256="a" * 64, request_json={"review_only": True}, state="running",
                stop_requested=False,
            ))
        assignment = RunTaskAssignment(experiment_id="experiment", evaluation_kind="primary",
                                       task_id="task", seed=0, candidate_sha256=candidate,
                                       dataset_id=dataset, split_id="synthetic-v1", session_id="session")
        plan_run_tasks(planner, run_id=run_id, assignments=(assignment,))
        job_id = enqueue_score_job(planner, run_id=run_id, experiment_id="experiment",
                                   evaluation_kind="primary", task_id="task", seed=0,
                                   candidate_sha256=candidate, candidate_output=payload,
                                   artifact_root=blob_root)
        seal_run_task_plan(planner, run_id=run_id)
        return run_id, job_id

    def worker(identifier, blob_root, finalize=False):
        # Use exact review-owned UUID unit names; no external command or candidate text.
        unit = "swapp-ai-scientist-review-terminal-" + uuid4().hex
        command = [
            "/usr/bin/systemd-run", "--user", "--wait", "--collect", "--quiet", "--pipe",
            "--unit=" + unit, "--slice=" + SCORER_SLICE,
            "--property=MemoryMax=2G", "--property=MemorySwapMax=0",
            "--property=CPUQuota=100%", "--property=TasksMax=32",
            "--property=RuntimeMaxSec=30", "--property=TimeoutStopSec=3",
            "--property=KillMode=control-group", "--property=OOMPolicy=stop",
            "--property=NoNewPrivileges=yes", "--working-directory=" + str(ROOT),
            "--setenv=CUDA_VISIBLE_DEVICES=", "--setenv=OPENBLAS_NUM_THREADS=2",
            "--setenv=OMP_NUM_THREADS=2", "--setenv=MKL_NUM_THREADS=2",
            "--setenv=NUMEXPR_NUM_THREADS=2", "--setenv=BLIS_NUM_THREADS=2",
            "--setenv=LAB_ARTIFACT_ROOT=" + str(blob_root),
            str(ROOT / ".venv/bin/python"), "-m", "lab.scorer.worker",
            "--finalize-run-id" if finalize else "--job-id", str(identifier),
        ]
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=45, check=False)
        result = json.loads(completed.stdout) if completed.returncode == 0 else None
        state = subprocess.run([
            "/usr/bin/systemctl", "--user", "show", unit + ".service",
            "--property=LoadState", "--property=ActiveState", "--property=MainPID",
        ], capture_output=True, text=True, timeout=5, check=False)
        properties = dict(line.split("=", 1) for line in state.stdout.splitlines() if "=" in line)
        inactive = properties.get("ActiveState") in {"inactive", "failed"} and properties.get("MainPID") == "0"
        record["processes"].append({"command": command, "exit_code": completed.returncode,
                                    "result": result, "unit_state": properties,
                                    "unit_has_no_main_process": inactive})
        if completed.returncode != 0:
            raise RuntimeError("owned review worker failed")
        return result

    try:
        _ensure_aggregate_slice()
        with admin.begin() as connection:
            connection.execute(insert(dataset_profiles).values(
                dataset_id=dataset, split_id="synthetic-v1", session_id="session",
                sample_count=16, sliding_window=4, profile_sha256="b" * 64,
            ))
            connection.execute(insert(dataset_labels), [dict(
                dataset_id=dataset, split_id="synthetic-v1", session_id="session",
                sample_index=index, is_anomaly=index >= 8,
            ) for index in range(16)])
        with tempfile.TemporaryDirectory(prefix="owned-terminal-worker-", dir=ROOT / "data/runtime") as tmp:
            blob_root = Path(tmp)
            payload = json.dumps({"schema": "candidate-scores.v1", "sample_indices": list(range(16)),
                                  "scores": [float(index) for index in range(16)]}).encode()
            missing_run, missing_job = fixture(payload, blob_root)
            digest = hashlib.sha256(payload).hexdigest()
            # Only the explicitly created private fixture blob is removed.
            (blob_root / digest[:2] / (digest + ".json")).unlink()
            for attempt in (1, 2, 3):
                result = worker(missing_job, blob_root)
                with scorer.connect() as connection:
                    job = connection.execute(select(score_jobs).where(
                        score_jobs.c.job_id == missing_job)).mappings().one()
                    outcomes = connection.execute(select(task_terminal_outcomes).where(
                        task_terminal_outcomes.c.run_id == missing_run)).mappings().all()
                if attempt < 3:
                    checks[f"missing_artifact_attempt_{attempt}_requeued_without_terminal"] = (
                        result["state"] == "retrying" and job["state"] == "queued"
                        and job["attempt"] == attempt and outcomes == []
                        and job["claimed_by"] is None and job["lease_until"] is None
                    )
                else:
                    checks["missing_artifact_attempt_3_terminal_infrastructure_error"] = (
                        result == {"job_id": str(missing_job), "state": "terminal", "outcome": "scorer_error"}
                        and job["state"] == "failed" and job["attempt"] == 3
                        and len(outcomes) == 1 and outcomes[0]["outcome_code"] == "scorer_error"
                    )
            final = worker(missing_run, blob_root, finalize=True)
            checks["failed_research_finalizer_success_distinguished"] = (
                final["state"] == "finalized" and final["research_status"] == "failed"
                and final["worker_pid"] != str(os.getpid())
            )
            short_payload = json.dumps({"schema": "candidate-scores.v1", "sample_indices": [0, 1],
                                        "scores": [0.0, 1.0]}).encode()
            rejected_run, rejected_job = fixture(short_payload, blob_root)
            result = worker(rejected_job, blob_root)
            with scorer.connect() as connection:
                job = connection.execute(select(score_jobs).where(
                    score_jobs.c.job_id == rejected_job)).mappings().one()
                outcomes = connection.execute(select(task_terminal_outcomes).where(
                    task_terminal_outcomes.c.run_id == rejected_run)).mappings().all()
            checks["candidate_index_error_terminal_without_infrastructure_retry"] = (
                result["state"] == "terminal" and result["outcome"] == "candidate_rejected"
                and job["attempt"] == 1 and job["state"] == "failed"
                and len(outcomes) == 1 and outcomes[0]["outcome_code"] == "candidate_rejected"
            )
            final = worker(rejected_run, blob_root, finalize=True)
            checks["candidate_reject_can_close_run_with_explicit_outcome"] = (
                final["state"] == "finalized" and final["research_status"] == "completed"
            )
            with scorer.connect() as connection:
                checks["terminal_paths_create_no_metric_scores"] = connection.execute(select(
                    task_scores.c.run_id).where(task_scores.c.run_id.in_(owned_runs))).first() is None
        checks["private_artifact_root_removed"] = not blob_root.exists()
        checks["all_six_worker_units_inactive"] = (
            len(record["processes"]) == 6
            and all(process["unit_has_no_main_process"] for process in record["processes"])
        )
    except Exception as error:
        record["error_type"] = type(error).__name__
    finally:
        with admin.begin() as connection:
            connection.execute(delete(runs).where(runs.c.run_id.in_(owned_runs)))
            connection.execute(delete(dataset_profiles).where(dataset_profiles.c.dataset_id == dataset))
        with admin.connect() as connection:
            checks["owned_rows_removed"] = connection.execute(select(runs.c.run_id).where(
                runs.c.run_id.in_(owned_runs))).first() is None
        for item in (admin, planner, scorer):
            item.dispose()
    record["source_unchanged"] = all(hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == sha
                                     for path, sha in record["source_sha256"].items())
    record["all_passed"] = not record.get("error_type") and len(checks) == 10 and all(checks.values()) and record["source_unchanged"]
    record["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    OUTPUT.write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({key: value for key, value in record.items() if key != "processes"}, indent=2))
    if not record["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
