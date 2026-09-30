"""Independent real PostgreSQL checks of migration 0009 claim fencing.

Synthetic invocation IDs deliberately exercise the database boundary only;
real systemd identity and drain evidence live in separate review scripts.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import time
from uuid import uuid4

from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import DBAPIError

from lab.db.schema import dataset_labels, dataset_profiles, runs, score_jobs, task_scores, task_terminal_outcomes
from lab.director.task_plan import RunTaskAssignment, plan_run_tasks, seal_run_task_plan
from lab.scorer.jobs import claim_score_job, enqueue_score_job
from lab.scorer.service import IndependentScorer
from review_scorer_queue import engine

ROOT = Path(__file__).resolve().parents[3]
PAYLOAD = json.dumps({"schema": "candidate-scores.v1", "sample_indices": list(range(16)),
                      "scores": [float(index) for index in range(16)]}).encode()


def main():
    engines = {role: engine(role) for role in ("migrator", "planner", "scorer", "director")}
    admin, planner, scorer = (engines[role] for role in ("migrator", "planner", "scorer"))
    service = IndependentScorer(scorer, harness_sha256="c" * 64)
    dataset = "owned-invocation-fence-" + uuid4().hex
    candidate = hashlib.sha256(dataset.encode()).hexdigest()
    owned_runs = []
    paths = ("lab/db/migrations/versions/0009_scorer_invocation_fence.py", "lab/db/schema.py",
             "lab/scorer/jobs.py", "lab/scorer/service.py", "lab/director/task_plan.py")
    sources = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in paths}
    record = {"checked_at": datetime.now(UTC).isoformat(), "source_sha256": sources,
              "scope": "Real PostgreSQL roles and production claim/scoring API; owned synthetic 16-row data and synthetic invocation IDs. No OS identity, process drain, expired cancellation recovery, GPU, public data or AOS acceptance.",
              "checks": {}}
    checks = record["checks"]

    def fixture():
        run_id = uuid4()
        owned_runs.append(run_id)
        with admin.begin() as connection:
            connection.execute(insert(runs).values(run_id=run_id, origin="local", owner_id=dataset,
                idempotency_key=str(run_id), payload_sha256="a" * 64,
                request_json={"review_only": True}, state="running", stop_requested=False))
        identity = dict(run_id=run_id, experiment_id="experiment", evaluation_kind="primary", task_id="task", seed=0)
        plan_run_tasks(planner, run_id=run_id, assignments=(RunTaskAssignment(
            **{key: value for key, value in identity.items() if key != "run_id"},
            candidate_sha256=candidate, dataset_id=dataset, split_id="synthetic-v1", session_id="session"),))
        return identity

    def queue(identity, root):
        return enqueue_score_job(planner, **identity, candidate_sha256=candidate,
                                 candidate_output=PAYLOAD, artifact_root=root)

    def claim(job):
        result = claim_score_job(scorer, job_id=job,
                                claim_unit=f"swapp-ai-scientist-scorer-{job.hex}.service",
                                claim_invocation_id=uuid4().hex)
        if result is None:
            raise RuntimeError("owned claim was unexpectedly unavailable")
        return result

    def denied(role, statement):
        try:
            with engines[role].begin() as connection:
                connection.execute(text("SET LOCAL statement_timeout='4s'"))
                connection.execute(statement)
            return False
        except DBAPIError:
            return True

    def stop(identity):
        with admin.begin() as connection:
            connection.execute(update(runs).where(runs.c.run_id == identity["run_id"]).values(
                state="stop_requested", stop_requested=True))

    def terminal(identity, code, **kwargs):
        return insert(task_terminal_outcomes).values(**identity, candidate_sha256=candidate,
                                                     outcome_code=code, producer_role="spoof", **kwargs)

    try:
        with admin.connect() as connection:
            revision = connection.execute(text("SELECT version_num FROM lab.alembic_version")).scalar_one()
        if revision != "0009_scorer_invocation_fence":
            raise RuntimeError("migration 0009 is required")
        record["migration"] = revision
        with admin.begin() as connection:
            connection.execute(insert(dataset_profiles).values(dataset_id=dataset, split_id="synthetic-v1",
                session_id="session", sample_count=16, sliding_window=4, profile_sha256="b" * 64))
            connection.execute(insert(dataset_labels), [dict(dataset_id=dataset, split_id="synthetic-v1",
                session_id="session", sample_index=index, is_anomaly=index >= 8) for index in range(16)])
        with tempfile.TemporaryDirectory(prefix="invocation-fence-", dir=ROOT / "data/runtime") as temporary:
            root = Path(temporary)
            identity = fixture()
            job = queue(identity, root)
            base_claim = dict(state="running", claimed_by="owned-test-token", lease_until=datetime.now(UTC) + timedelta(seconds=30),
                              claim_unit=f"swapp-ai-scientist-scorer-{job.hex}.service", claim_invocation_id=uuid4().hex)
            for field in ("claimed_by", "lease_until", "claim_unit", "claim_invocation_id"):
                checks[f"claim_missing_{field}_denied"] = denied("scorer", update(score_jobs).where(
                    score_jobs.c.job_id == job).values(base_claim | {field: None}))
            checks["claim_wrong_unit_denied"] = denied("scorer", update(score_jobs).where(
                score_jobs.c.job_id == job).values(base_claim | {"claim_unit": "swapp-ai-scientist-scorer-" + uuid4().hex + ".service"}))
            current = claim(job)
            checks["active_claim_not_reclaimed"] = claim_score_job(scorer, job_id=job,
                claim_unit=current.claim_unit, claim_invocation_id=uuid4().hex) is None
            metric = service.score_task(**identity, candidate_sha256=candidate, candidate_output=PAYLOAD,
                score_job_id=job, claim_token=current.claim_token, worker_invocation_id=current.claim_invocation_id)
            with scorer.connect() as connection:
                finished = connection.execute(select(score_jobs).where(score_jobs.c.job_id == job)).mappings().one()
                written = connection.execute(select(task_scores).where(task_scores.c.run_id == identity["run_id"])).mappings().one()
            checks["valid_score_atomically_completes_job"] = finished["state"] == "completed"
            checks["completed_job_clears_all_claim_fields"] = all(finished[key] is None for key in (
                "claimed_by", "lease_until", "claim_unit", "claim_invocation_id"))
            checks["score_retains_invocation_without_secret_token"] = (
                written["worker_invocation_id"] == current.claim_invocation_id and written["claim_token"] is None
                and written["score_job_id"] == job and written["score"] == metric)
            checks["score_rows_immutable"] = denied("scorer", delete(task_scores).where(task_scores.c.run_id == identity["run_id"]))

            identity = fixture()
            job = queue(identity, root)
            current = claim(job)
            score_values = dict(**identity, score=metric, guard_results={}, score_job_id=job,
                                claim_token=current.claim_token, worker_invocation_id=current.claim_invocation_id)
            for field in ("score_job_id", "claim_token", "worker_invocation_id"):
                checks[f"score_missing_{field}_denied"] = denied("scorer", insert(task_scores).values(score_values | {field: None}))
            for field, wrong in (("claim_token", "wrong-token"), ("worker_invocation_id", uuid4().hex),
                                 ("task_id", "wrong-task")):
                checks[f"score_wrong_{field}_denied"] = denied("scorer", insert(task_scores).values(score_values | {field: wrong}))
            for field in ("candidate_sha256", "candidate_output_sha256"):
                checks[f"score_missing_{field}_denied"] = denied("scorer", insert(task_scores).values(
                    score_values | {"score": {key: value for key, value in metric.items() if key != field}}))
            checks["running_job_cannot_finish_without_result"] = denied("scorer", update(score_jobs).where(
                score_jobs.c.job_id == job).values(state="completed", claimed_by=None, lease_until=None,
                                                  claim_unit=None, claim_invocation_id=None))
            # Expire a future lease naturally; never bypass the new trigger.
            with scorer.begin() as connection:
                connection.execute(update(score_jobs).where(score_jobs.c.job_id == job).values(
                    lease_until=datetime.now(UTC) + timedelta(milliseconds=300)))
            time.sleep(0.4)
            checks["expired_claim_score_denied"] = denied("scorer", insert(task_scores).values(score_values))
            renewed = claim(job)
            checks["reclaim_rotates_token_and_invocation"] = (
                renewed.claim_token != current.claim_token and renewed.claim_invocation_id != current.claim_invocation_id)
            checks["stale_generation_denied_after_reclaim"] = denied("scorer", insert(task_scores).values(score_values))
            term_values = dict(score_job_id=job, claim_token=renewed.claim_token,
                               worker_invocation_id=renewed.claim_invocation_id)
            checks["terminal_stale_invocation_denied"] = denied("scorer", terminal(identity, "scorer_error",
                **(term_values | {"worker_invocation_id": current.claim_invocation_id})))
            with scorer.begin() as connection:
                connection.execute(terminal(identity, "scorer_error", **term_values))
            with scorer.connect() as connection:
                finished = connection.execute(select(score_jobs).where(score_jobs.c.job_id == job)).mappings().one()
            checks["valid_terminal_completes_and_clears_claim"] = finished["state"] == "failed" and all(
                finished[key] is None for key in ("claimed_by", "lease_until", "claim_unit", "claim_invocation_id"))
            checks["terminal_cannot_also_score"] = denied("scorer", insert(task_scores).values(score_values | {
                "claim_token": renewed.claim_token, "worker_invocation_id": renewed.claim_invocation_id}))

            identity = fixture()
            with planner.begin() as connection:
                connection.execute(terminal(identity, "guard_rejected"))
            seal_run_task_plan(planner, run_id=identity["run_id"])
            report, _ = service.complete_run(run_id=identity["run_id"])
            checks["planner_guard_rejection_preserved"] = len(report["task_terminal_outcomes"]) == 1 and report["task_scores"] == []
            identity = fixture()
            job = queue(identity, root)
            stop(identity)
            checks["planner_queued_cancel_cannot_omit_job"] = denied("planner", terminal(identity, "cancelled"))
            with planner.begin() as connection:
                connection.execute(terminal(identity, "cancelled", score_job_id=job))
            with scorer.connect() as connection:
                state = connection.execute(select(score_jobs.c.state).where(score_jobs.c.job_id == job)).scalar_one()
            checks["planner_queued_cancel_preserved"] = state == "cancelled"
            identity = fixture()
            job = queue(identity, root)
            current = claim(job)
            stop(identity)
            checks["stop_requested_blocks_score"] = denied("scorer", insert(task_scores).values(
                **identity, score=metric, guard_results={}, score_job_id=job,
                claim_token=current.claim_token, worker_invocation_id=current.claim_invocation_id))
            checks["planner_cannot_cancel_running_claim"] = denied("planner", terminal(identity, "cancelled", score_job_id=job))
            with scorer.begin() as connection:
                connection.execute(terminal(identity, "cancelled", score_job_id=job,
                    claim_token=current.claim_token, worker_invocation_id=current.claim_invocation_id))
            with scorer.connect() as connection:
                state = connection.execute(select(score_jobs.c.state).where(score_jobs.c.job_id == job)).scalar_one()
            checks["current_scorer_cancellation_preserved"] = state == "cancelled"
    except Exception as error:
        record["error_type"] = type(error).__name__
    finally:
        with admin.begin() as connection:
            connection.execute(delete(runs).where(runs.c.run_id.in_(owned_runs)))
            connection.execute(delete(dataset_profiles).where(dataset_profiles.c.dataset_id == dataset))
        with admin.connect() as connection:
            checks["owned_rows_cleaned"] = connection.execute(select(runs.c.run_id).where(runs.c.run_id.in_(owned_runs))).first() is None
        for item in engines.values():
            item.dispose()
    record["source_unchanged"] = all(hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest for path, digest in sources.items())
    record["all_passed"] = not record.get("error_type") and len(checks) == 32 and all(checks.values()) and record["source_unchanged"]
    record["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    Path(__file__).with_name("scorer-invocation-fence-review.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))
    if not record["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
