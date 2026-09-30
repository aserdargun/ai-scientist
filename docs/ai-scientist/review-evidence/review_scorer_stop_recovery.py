"""Real worker lease expiry, API stop and separate cancellation recovery.

The metric function is deliberately paused by a trusted review-only launcher;
all claim, identity, stop, recovery, finalizer and report paths are production.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete, insert, select, update

from lab.api.app import create_app
from lab.cli import _dispatch_one
from lab.db.schema import dataset_labels, dataset_profiles, reports, runs, score_jobs, task_scores, task_terminal_outcomes
from lab.director.task_plan import RunTaskAssignment, plan_run_tasks, seal_run_task_plan
from lab.scorer.jobs import enqueue_score_job
from lab.scorer.supervisor import SCORER_SLICE, _ensure_aggregate_slice, run_scorer_finalize_process
from review_scorer_queue import engine
from review_scorer_unit_drain import cleanup_identity, launch, process_identity, running
from review_systemd_invocation import show

ROOT = Path(__file__).resolve().parents[3]
PAYLOAD = json.dumps({"schema": "candidate-scores.v1", "sample_indices": list(range(16)),
                      "scores": [float(index) for index in range(16)]}).encode()


def barrier_worker(job, marker):
    """Pause only trusted metric execution after the real worker claims its job."""
    import lab.scorer.service as service
    from lab.scorer.worker import main as worker_main

    def metric_barrier(*args, **kwargs):
        temporary = marker.with_suffix(".writing")
        temporary.write_text(json.dumps(process_identity(os.getpid())))
        temporary.replace(marker)
        while True:
            time.sleep(0.05)

    service.vus_metrics = metric_barrier
    raise SystemExit(worker_main(["--job-id", str(job)]))


def main():
    # Import only after the child has provided the completed production API.
    from lab.scorer.supervisor import run_scorer_recovery_process

    engines = {role: engine(role) for role in ("migrator", "planner", "scorer", "director")}
    admin, planner, scorer = (engines[role] for role in ("migrator", "planner", "scorer"))
    dataset = "owned-stop-recovery-" + uuid4().hex
    candidate = hashlib.sha256(dataset.encode()).hexdigest()
    run_id, job_id = uuid4(), None
    competing_run_id = uuid4()
    owned_runs = [run_id, competing_run_id]
    paths = ("lab/scorer/worker.py", "lab/scorer/supervisor.py", "lab/scorer/service.py",
             "lab/scorer/jobs.py", "lab/db/schema.py", "lab/cli.py", "lab/director/task_plan.py",
             "lab/db/migrations/versions/0010_terminal_recovery_fence.py")
    sources = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in paths}
    record = {"checked_at": datetime.now(UTC).isoformat(), "source_sha256": sources,
              "scope": "Actual bounded Scorer worker with a review-only metric barrier, natural lease expiry, authenticated in-process ASGI stop, production dispatcher with a competing queued job, separate recovery/finalizer and verified report. The cancelled task has no metric; the competing synthetic task then computes a real score in a separately launched production worker. No GPU/public-data/AOS acceptance.",
              "checks": {}}
    checks = record["checks"]
    launcher = None
    sentinel = None
    worker_unit = None
    worker_identity = None
    competing_unit = None
    try:
        _ensure_aggregate_slice()
        with admin.begin() as connection:
            connection.execute(insert(dataset_profiles).values(dataset_id=dataset, split_id="synthetic-v1",
                session_id="session", sample_count=16, sliding_window=4, profile_sha256="b" * 64))
            connection.execute(insert(dataset_labels), [dict(dataset_id=dataset, split_id="synthetic-v1",
                session_id="session", sample_index=index, is_anomaly=index >= 8) for index in range(16)])
            connection.execute(insert(runs).values(run_id=run_id, origin="local", owner_id=dataset,
                idempotency_key=str(run_id), payload_sha256="a" * 64, request_json={"review_only": True},
                state="running", stop_requested=False))
        identity = dict(run_id=run_id, experiment_id="experiment", evaluation_kind="primary", task_id="task", seed=0)
        plan_run_tasks(planner, run_id=run_id, assignments=(RunTaskAssignment(
            **{key: value for key, value in identity.items() if key != "run_id"}, candidate_sha256=candidate,
            dataset_id=dataset, split_id="synthetic-v1", session_id="session"),))
        with tempfile.TemporaryDirectory(prefix="stop-recovery-review-", dir=ROOT / "data/runtime") as temporary:
            base = Path(temporary)
            job_id = enqueue_score_job(planner, **identity, candidate_sha256=candidate,
                                       candidate_output=PAYLOAD, artifact_root=base / "blobs")
            seal_run_task_plan(planner, run_id=run_id)
            sentinel = launch(uuid4(), base)
            marker = base / "metric-barrier.json"
            worker_unit = f"swapp-ai-scientist-scorer-{job_id.hex}.service"
            command = ["/usr/bin/systemd-run", "--user", "--wait", "--collect", "--quiet", "--pipe",
                "--unit=" + worker_unit, "--slice=" + SCORER_SLICE, "--property=Type=exec",
                "--property=MemoryMax=2G", "--property=MemorySwapMax=0", "--property=CPUQuota=100%",
                "--property=TasksMax=32", "--property=RuntimeMaxSec=30", "--property=TimeoutStopSec=3",
                "--property=KillMode=control-group", "--property=NoNewPrivileges=yes",
                "--working-directory=" + str(ROOT), "--setenv=CUDA_VISIBLE_DEVICES=",
                "--setenv=OPENBLAS_NUM_THREADS=2", "--setenv=OMP_NUM_THREADS=2", "--setenv=MKL_NUM_THREADS=2",
                "--setenv=NUMEXPR_NUM_THREADS=2", "--setenv=BLIS_NUM_THREADS=2",
                "--setenv=LAB_ARTIFACT_ROOT=" + str(base / "blobs"),
                str(ROOT / ".venv/bin/python"), str(Path(__file__).resolve()), "barrier", str(job_id), str(marker)]
            record["worker_command"] = command
            launcher = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline and launcher.poll() is None:
                time.sleep(0.03)
            if not marker.exists():
                raise RuntimeError("owned worker did not reach its trusted metric barrier")
            worker_identity = json.loads(marker.read_text())
            properties = show(worker_unit)["properties"]
            with scorer.connect() as connection:
                claim = connection.execute(select(score_jobs).where(score_jobs.c.job_id == job_id)).mappings().one()
            checks["real_claim_matches_live_worker_invocation"] = (
                claim["state"] == "running" and claim["claim_unit"] == worker_unit
                and claim["claim_invocation_id"] == properties["InvocationID"]
                and properties["MainPID"] == str(worker_identity["pid"]) and running(worker_identity))
            original_invocation = claim["claim_invocation_id"]
            try:
                run_scorer_recovery_process(job_id, expected_claim_invocation_id=uuid4().hex, remaining_seconds=30)
                checks["wrong_generation_recovery_rejected"] = False
            except RuntimeError:
                checks["wrong_generation_recovery_rejected"] = True
            checks["wrong_generation_preserves_live_worker"] = running(worker_identity)
            with admin.begin() as connection:
                connection.execute(insert(runs).values(run_id=competing_run_id, origin="local", owner_id=dataset,
                    idempotency_key=str(competing_run_id), payload_sha256="a" * 64,
                    request_json={"review_only": True}, state="running", stop_requested=False))
            competing_identity = identity | {"run_id": competing_run_id}
            plan_run_tasks(planner, run_id=competing_run_id, assignments=(RunTaskAssignment(
                **{key: value for key, value in competing_identity.items() if key != "run_id"},
                candidate_sha256=candidate, dataset_id=dataset, split_id="synthetic-v1", session_id="session"),))
            competing_job = enqueue_score_job(planner, **competing_identity, candidate_sha256=candidate,
                candidate_output=PAYLOAD, artifact_root=base / "blobs")
            seal_run_task_plan(planner, run_id=competing_run_id)
            with scorer.begin() as connection:
                connection.execute(update(score_jobs).where(score_jobs.c.job_id == job_id).values(
                    lease_until=datetime.now(UTC) + timedelta(milliseconds=300)))
            time.sleep(0.4)
            checks["worker_still_alive_after_lease_expiry"] = running(worker_identity)
            token = uuid4().hex
            headers = {"Authorization": "Bearer " + token}
            app = create_app(director_engine=engines["director"], director_token=token, owner_id=dataset)
            with TestClient(app) as client:
                stopped = client.post(f"/v1/runs/{run_id}/stop", headers=headers)
                checks["authenticated_api_requests_stop"] = stopped.status_code == 200 and stopped.json()["state"] == "stop_requested"
                checks["stop_request_alone_does_not_claim_worker_drained"] = running(worker_identity)
                recovery = _dispatch_one(planner, remaining_seconds=30)
                record["recovery_dispatch"] = recovery
                checks["separate_recovery_exited_successfully"] = recovery.get("process_exit_code") == "0" and recovery.get("state") == "cancelled_after_drain"
                checks["dispatcher_recovered_stop_despite_competing_queued_job"] = recovery.get("job_id") == str(job_id)
                checks["old_worker_really_drained"] = not running(worker_identity)
                with scorer.connect() as connection:
                    job = connection.execute(select(score_jobs).where(score_jobs.c.job_id == job_id)).mappings().one()
                    outcomes = connection.execute(select(task_terminal_outcomes).where(task_terminal_outcomes.c.run_id == run_id)).mappings().all()
                    has_score = connection.execute(select(task_scores.c.run_id).where(task_scores.c.run_id == run_id)).first() is not None
                checks["expired_job_cancelled_and_claim_cleared"] = job["state"] == "cancelled" and all(job[key] is None for key in (
                    "claimed_by", "lease_until", "claim_unit", "claim_invocation_id"))
                checks["one_terminal_cancel_without_metric"] = len(outcomes) == 1 and outcomes[0]["outcome_code"] == "cancelled" and not has_score
                checks["target_invocation_retained"] = len(outcomes) == 1 and outcomes[0]["worker_invocation_id"] == original_invocation
                checks["recovery_is_a_distinct_verified_generation"] = len(outcomes) == 1 and bool(outcomes[0].get("recovery_invocation_id")) and outcomes[0]["recovery_invocation_id"] != original_invocation
                finalizer = run_scorer_finalize_process(run_id, remaining_seconds=30)
                checks["separate_finalizer_succeeded"] = finalizer.exit_code == 0
                response = client.get(f"/v1/runs/{run_id}/report", headers=headers)
                payload = response.json() if response.status_code == 200 else {}
                report = payload.get("report", {})
                checks["verified_stopped_report_contains_no_fake_score"] = response.status_code == 200 and report.get("status") == "stopped" and report.get("task_scores") == [] and len(report.get("task_terminal_outcomes", [])) == 1
                checks["report_content_hash_verified"] = payload.get("report_sha256") == hashlib.sha256(json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
                repeated = run_scorer_recovery_process(job_id, expected_claim_invocation_id=original_invocation, remaining_seconds=30)
                checks["duplicate_recovery_succeeds"] = repeated.exit_code == 0
                with scorer.connect() as connection:
                    after = connection.execute(select(task_terminal_outcomes).where(task_terminal_outcomes.c.run_id == run_id)).mappings().all()
                    report_after = connection.execute(select(reports.c.report_sha256).where(reports.c.run_id == run_id)).scalar_one()
                checks["duplicate_recovery_preserves_result_and_report"] = after == outcomes and report_after == payload.get("report_sha256")
                with scorer.connect() as connection:
                    queued = connection.execute(select(score_jobs).where(score_jobs.c.job_id == competing_job)).mappings().one()
                checks["competing_job_preserved_without_false_claim"] = queued["state"] == "queued" and queued["attempt"] == 0
                competing_unit = f"swapp-ai-scientist-scorer-{competing_job.hex}.service"
                competing_command = ["--unit=" + competing_unit if item.startswith("--unit=") else item for item in command[:-5]] + [
                    str(ROOT / ".venv/bin/python"), "-m", "lab.scorer.worker", "--job-id", str(competing_job)]
                competing = subprocess.run(competing_command, cwd=ROOT, capture_output=True, text=True, timeout=40, check=False)
                competing_result = json.loads(competing.stdout) if competing.returncode == 0 else None
                record["competing_worker"] = {"command": competing_command, "exit_code": competing.returncode, "result": competing_result}
                with scorer.connect() as connection:
                    competing_finished = connection.execute(select(score_jobs.c.state).where(score_jobs.c.job_id == competing_job)).scalar_one()
                    competing_score = connection.execute(select(task_scores.c.score).where(task_scores.c.run_id == competing_run_id)).scalar_one_or_none()
                checks["other_run_progresses_after_recovery_frees_capacity"] = competing.returncode == 0 and competing_finished == "completed" and competing_score is not None
                checks["separate_sentinel_kept_running"] = all(running(item) for item in sentinel["identities"].values())
    except Exception as error:
        record["error_type"] = type(error).__name__
    finally:
        if worker_unit is not None:
            subprocess.run(["/usr/bin/systemctl", "--user", "stop", worker_unit], capture_output=True, timeout=8, check=False)
        if competing_unit is not None:
            subprocess.run(["/usr/bin/systemctl", "--user", "stop", competing_unit], capture_output=True, timeout=8, check=False)
        if launcher is not None:
            try:
                launcher.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                launcher.terminate()
                launcher.communicate(timeout=5)
        if sentinel is not None:
            subprocess.run(["/usr/bin/systemctl", "--user", "stop", sentinel["unit"]], capture_output=True, timeout=8, check=False)
            for item in sentinel["identities"].values():
                cleanup_identity(item)
        checks["owned_processes_cleaned"] = (worker_identity is None or not running(worker_identity)) and (
            sentinel is None or all(not running(item) for item in sentinel["identities"].values()))
        with admin.begin() as connection:
            connection.execute(delete(runs).where(runs.c.run_id.in_(owned_runs)))
            connection.execute(delete(dataset_profiles).where(dataset_profiles.c.dataset_id == dataset))
        with admin.connect() as connection:
            checks["owned_rows_cleaned"] = connection.execute(select(runs.c.run_id).where(runs.c.run_id.in_(owned_runs))).first() is None
        for item in engines.values():
            item.dispose()
    record["source_unchanged"] = all(hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest for path, digest in sources.items())
    record["all_passed"] = not record.get("error_type") and len(checks) == 23 and all(checks.values()) and record["source_unchanged"]
    record["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    Path(__file__).with_name("scorer-stop-recovery-review.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({key: value for key, value in record.items() if key != "worker_command"}, indent=2))
    if not record["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "barrier":
        barrier_worker(UUID(sys.argv[2]), Path(sys.argv[3]))
    else:
        main()
