"""Independent PostgreSQL review of score-or-terminal task resolution.

Uses only owned synthetic rows, real service roles, and bounded two-session
transactions. Does not prove host worker drain or Director end-to-end behavior.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import DBAPIError

from lab.api.app import create_app
from lab.db.schema import (
    dataset_labels, dataset_profiles, reports, runs, score_jobs, task_scores,
    task_terminal_outcomes,
)
from lab.director.task_plan import RunTaskAssignment, plan_run_tasks, seal_run_task_plan
from lab.scorer.jobs import claim_score_job, enqueue_score_job
from lab.scorer.service import IndependentScorer
from review_scorer_queue import engine

ROOT = Path(__file__).resolve().parents[3]
LEGACY = ROOT / "data/runtime/terminal-review/legacy-fixture.json"
OUTPUT = Path(__file__).with_name("terminal-outcomes-review.json")
PAYLOAD = json.dumps({
    "schema": "candidate-scores.v1", "sample_indices": list(range(16)),
    "scores": [float(index) for index in range(16)],
}).encode()


def main():
    engines = {role: engine(role) for role in ("migrator", "planner", "scorer", "director")}
    admin, planner, scorer = (engines[role] for role in ("migrator", "planner", "scorer"))
    service = IndependentScorer(scorer, harness_sha256="c" * 64)
    dataset = "owned-terminal-review-" + uuid4().hex
    candidate = hashlib.sha256(dataset.encode()).hexdigest()
    owned_runs = []
    legacy_verified = False
    legacy = None
    source_paths = [
        "lab/db/migrations/versions/0008_terminal_task_outcomes.py", "lab/db/schema.py",
        "lab/director/task_plan.py", "lab/scorer/jobs.py", "lab/scorer/service.py", "lab/api/app.py",
    ]
    sources = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in source_paths}
    record = {
        "checked_at": datetime.now(UTC).isoformat(), "source_sha256": sources,
        "scope": "Real PostgreSQL service roles; owned 16-row synthetic data and bounded concurrent transactions. No model, public-data, worker-drain or full Director acceptance.",
        "checks": {}, "races": [],
    }
    checks = record["checks"]

    def fixture(task_count=1):
        run_id = uuid4()
        owned_runs.append(run_id)
        with admin.begin() as connection:
            connection.execute(insert(runs).values(
                run_id=run_id, origin="local", owner_id=dataset,
                idempotency_key=str(run_id), payload_sha256="a" * 64,
                request_json={"review_only": True}, state="running", stop_requested=False,
            ))
        identities = []
        assignments = []
        for index in range(task_count):
            identity = dict(run_id=run_id, experiment_id="experiment", evaluation_kind="primary",
                            task_id=f"task-{index}", seed=0)
            identities.append(identity)
            assignments.append(RunTaskAssignment(
                **{key: value for key, value in identity.items() if key != "run_id"},
                candidate_sha256=candidate, dataset_id=dataset, split_id="synthetic-v1",
                session_id="session",
            ))
        if assignments:
            plan_run_tasks(planner, run_id=run_id, assignments=tuple(assignments))
        return run_id, identities

    def terminal(identity, code="guard_rejected", **extra):
        return insert(task_terminal_outcomes).values(
            **identity, candidate_sha256=extra.pop("candidate_sha256", candidate),
            outcome_code=code, producer_role="caller-spoof", **extra,
        )

    def denied(role, statement, isolation=None):
        try:
            with engines[role].connect() as connection:
                if isolation:
                    connection = connection.execution_options(isolation_level=isolation)
                with connection.begin():
                    connection.execute(statement)
            return False
        except DBAPIError:
            return True

    def stop(run_id):
        with admin.begin() as connection:
            connection.execute(update(runs).where(runs.c.run_id == run_id).values(
                state="stop_requested", stop_requested=True,
            ))

    def queue(identity, root):
        return enqueue_score_job(planner, **identity, candidate_sha256=candidate,
                                 candidate_output=PAYLOAD, artifact_root=root)

    def race(first_role, first_statement, second_role, second_statement):
        ready = threading.Event()
        second_pid = []

        def contender():
            with engines[second_role].begin() as connection:
                connection.execute(text("SET LOCAL statement_timeout = '5s'"))
                second_pid.append(connection.execute(text("SELECT pg_backend_pid()")).scalar_one())
                ready.set()
                try:
                    connection.execute(second_statement)
                    return False
                except DBAPIError:
                    return True

        with ThreadPoolExecutor(max_workers=1) as pool:
            with engines[first_role].connect() as holder:
                transaction = holder.begin()
                holder_pid = holder.execute(text("SELECT pg_backend_pid()")).scalar_one()
                holder.execute(first_statement)
                future = pool.submit(contender)
                if not ready.wait(2):
                    transaction.rollback()
                    raise RuntimeError("contender did not start within bound")
                blocked = False
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    with admin.connect() as observer:
                        blockers = observer.execute(text("SELECT pg_blocking_pids(:pid)"),
                                                    {"pid": second_pid[0]}).scalar_one()
                    if holder_pid in blockers:
                        blocked = True
                        break
                    time.sleep(0.02)
                transaction.commit()
                rejected = future.result(timeout=6)
        record["races"].append({"first_role": first_role, "second_role": second_role,
                                "observed_database_lock_wait": blocked,
                                "second_write_rejected": rejected})
        return blocked and rejected

    try:
        with admin.connect() as connection:
            revision = connection.execute(text("SELECT version_num FROM lab.alembic_version")).scalar_one()
        if revision != "0008_terminal_task_outcomes":
            raise RuntimeError("migration 0008 must be applied before this review")
        record["migration"] = revision
        if LEGACY.exists():
            legacy = json.loads(LEGACY.read_text())
            old_id = UUID(legacy["run_id"])
            with admin.connect() as connection:
                old_kind = connection.execute(text(
                    "SELECT completion_kind FROM scorer.task_completions WHERE run_id = :run"
                ), {"run": old_id}).scalar_one()
                old_score = connection.execute(select(task_scores.c.score).where(
                    task_scores.c.run_id == old_id)).scalar_one()
            checks["pre_upgrade_score_completion_backfilled"] = old_kind == "scored"
            checks["pre_upgrade_score_bytes_unchanged"] = (
                hashlib.sha256(json.dumps(old_score, sort_keys=True).encode()).hexdigest()
                == legacy["score_sha256"]
            )
            old_identity = {key: legacy[key] for key in (
                "experiment_id", "evaluation_kind", "task_id", "seed"
            )} | {"run_id": old_id}
            checks["pre_upgrade_score_cannot_also_terminate"] = denied("planner", terminal(
                old_identity, candidate_sha256=legacy["candidate_sha256"]
            ))
            legacy_verified = all(checks.values())
        else:
            record["legacy_fixture"] = "Already cleaned or not prepared; no new upgrade claim."
        with admin.begin() as connection:
            connection.execute(insert(dataset_profiles).values(
                dataset_id=dataset, split_id="synthetic-v1", session_id="session",
                sample_count=16, sliding_window=4, profile_sha256="b" * 64,
            ))
            connection.execute(insert(dataset_labels), [dict(
                dataset_id=dataset, split_id="synthetic-v1", session_id="session",
                sample_index=index, is_anomaly=index >= 8,
            ) for index in range(16)])
        run_id, identities = fixture(2)
        metric = service.score_task(**identities[0], candidate_sha256=candidate,
                                    candidate_output=PAYLOAD)
        with planner.begin() as connection:
            connection.execute(terminal(identities[1]))
        with planner.connect() as connection:
            producer = connection.execute(select(task_terminal_outcomes.c.producer_role).where(
                task_terminal_outcomes.c.run_id == run_id)).scalar_one()
        checks["producer_derived_from_real_session"] = producer == "swapp_lab_planner"
        checks["unsealed_mixed_run_not_finalized"] = service.finalize_if_ready(run_id=run_id) is None
        seal_run_task_plan(planner, run_id=run_id)
        report, digest = service.complete_run(run_id=run_id)
        checks["mixed_report_has_real_score_and_explicit_terminal"] = (
            report["status"] == "completed" and len(report["task_scores"]) == 1
            and report["task_scores"][0] == metric
            and len(report["task_terminal_outcomes"]) == 1
            and report["task_terminal_outcomes"][0]["outcome"] == "guard_rejected"
        )
        checks["mixed_report_idempotent"] = service.complete_run(run_id=run_id) == (report, digest)
        checks["terminal_rows_immutable_to_planner"] = denied("planner", update(
            task_terminal_outcomes).where(task_terminal_outcomes.c.run_id == run_id).values(
                outcome_code="candidate_crash"))
        checks["terminal_rows_immutable_to_scorer"] = denied("scorer", delete(
            task_terminal_outcomes).where(task_terminal_outcomes.c.run_id == run_id))
        checks["scores_immutable_to_scorer"] = denied("scorer", delete(task_scores).where(
            task_scores.c.run_id == run_id))

        _, identities = fixture()
        checks["director_cannot_write_terminal"] = denied("director", terminal(identities[0]))
        checks["planner_cannot_write_infrastructure_error"] = denied("planner", terminal(
            identities[0], "scorer_error"))
        checks["wrong_candidate_denied"] = denied("planner", terminal(
            identities[0], candidate_sha256="f" * 64))
        checks["unknown_task_denied"] = denied("planner", terminal(
            identities[0] | {"task_id": "unassigned"}))
        checks["repeatable_read_write_denied"] = denied("planner", terminal(identities[0]),
                                                        isolation="REPEATABLE READ")
        checks["scorer_requires_live_job_claim"] = denied("scorer", terminal(
            identities[0], "candidate_rejected"))

        for first in ("score", "terminal"):
            race_run, (identity,) = fixture()
            # Reuse actual independently computed metrics for identical labels/scores;
            # only trusted identity metadata changes for this statement-level race.
            race_metric = metric | {key: identity[key] for key in (
                "experiment_id", "evaluation_kind", "task_id", "seed"
            )}
            score_statement = insert(task_scores).values(
                **identity, score=race_metric,
                guard_results={"score_domain": "finite", "full_index_coverage": True},
            )
            arguments = (("scorer", score_statement, "planner", terminal(identity))
                         if first == "score" else
                         ("planner", terminal(identity), "scorer", score_statement))
            checks[f"{first}_wins_exclusive_completion_race"] = race(*arguments)
            with admin.connect() as connection:
                completions = connection.execute(text(
                    "SELECT completion_kind FROM scorer.task_completions WHERE run_id = :run"
                ), {"run": race_run}).scalars().all()
            checks[f"{first}_race_exactly_one_completion"] = completions == [
                "scored" if first == "score" else "terminal"
            ]

        with tempfile.TemporaryDirectory(prefix="terminal-review-", dir=ROOT / "data/runtime") as tmp:
            artifact_root = Path(tmp)
            stopped, (identity,) = fixture()
            job = queue(identity, artifact_root)
            stop(stopped)
            checks["queued_cancel_cannot_omit_actual_job"] = denied("planner", terminal(
                identity, "cancelled"))
            with planner.begin() as connection:
                connection.execute(terminal(identity, "cancelled", score_job_id=job))
            with scorer.connect() as connection:
                state = connection.execute(select(score_jobs.c.state).where(
                    score_jobs.c.job_id == job)).scalar_one()
            checks["queued_cancel_updates_job_atomically"] = state == "cancelled"
            seal_run_task_plan(planner, run_id=stopped)
            stopped_report, _ = service.complete_run(run_id=stopped)
            checks["stopped_report_has_no_fabricated_metric"] = (
                stopped_report["status"] == "stopped" and stopped_report["task_scores"] == []
                and stopped_report["task_terminal_outcomes"][0]["outcome"] == "cancelled"
            )
            _, (identity,) = fixture()
            with planner.begin() as connection:
                connection.execute(terminal(identity))
            try:
                queue(identity, artifact_root)
                checks["resolved_task_cannot_enqueue"] = False
            except (ValueError, DBAPIError):
                checks["resolved_task_cannot_enqueue"] = True

            failed, (identity,) = fixture()
            job = queue(identity, artifact_root)
            old = claim_score_job(scorer, job_id=job)
            assert old is not None
            with scorer.begin() as connection:
                connection.execute(update(score_jobs).where(score_jobs.c.job_id == job).values(
                    lease_until=datetime.now(UTC) - timedelta(seconds=1)))
            checks["expired_claim_cannot_terminate"] = denied("scorer", terminal(
                identity, "scorer_error", score_job_id=job, claim_token=old.claim_token))
            current = claim_score_job(scorer, job_id=job)
            assert current is not None
            checks["stale_claim_cannot_terminate_successor"] = denied("scorer", terminal(
                identity, "scorer_error", score_job_id=job, claim_token=old.claim_token))
            service.record_terminal_outcome(**identity, candidate_sha256=candidate,
                                            outcome_code="scorer_error", score_job_id=job,
                                            claim_token=current.claim_token)
            with scorer.connect() as connection:
                job_state = connection.execute(select(score_jobs.c.state).where(
                    score_jobs.c.job_id == job)).scalar_one()
                outcome = connection.execute(select(task_terminal_outcomes).where(
                    task_terminal_outcomes.c.run_id == failed)).mappings().one()
            checks["current_claim_closes_job_without_persisting_token"] = (
                job_state == "failed" and outcome["claim_token"] is None
                and outcome["producer_role"] == "swapp_lab_scorer"
            )
            seal_run_task_plan(planner, run_id=failed)
            failed_report, _ = service.complete_run(run_id=failed)
            checks["infrastructure_failure_report_is_failed"] = (
                failed_report["status"] == "failed" and failed_report["task_scores"] == []
            )

        empty, _ = fixture(0)
        try:
            seal_run_task_plan(planner, run_id=empty)
            checks["active_empty_plan_cannot_seal"] = False
        except ValueError:
            checks["active_empty_plan_cannot_seal"] = True
        stop(empty)
        seal = seal_run_task_plan(planner, run_id=empty)
        empty_report, _ = service.complete_run(run_id=empty)
        checks["prestart_stop_closes_without_fake_tasks"] = (
            seal.task_count == 0 and empty_report["status"] == "stopped"
            and empty_report["task_scores"] == [] and empty_report["task_terminal_outcomes"] == []
        )
        token = uuid4().hex
        headers = {"Authorization": "Bearer " + token}
        app = create_app(director_engine=engines["director"], director_token=token, owner_id=dataset)
        with TestClient(app) as client:
            for report_run, expected in ((stopped, stopped_report), (failed, failed_report), (empty, empty_report)):
                response = client.get(f"/v1/runs/{report_run}/report", headers=headers)
                checks[f"api_verified_{expected['status']}_{'empty' if report_run == empty else 'task'}_report"] = (
                    response.status_code == 200 and response.json()["report"] == expected
                    and response.json()["report_sha256"] == hashlib.sha256(json.dumps(
                        expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                    ).encode()).hexdigest()
                )
            checks["api_report_requires_auth"] = client.get(f"/v1/runs/{failed}/report").status_code == 401
            with admin.begin() as connection:
                connection.execute(update(runs).where(runs.c.run_id == failed).values(state="completed"))
            checks["api_run_report_status_mismatch_rejected"] = client.get(
                f"/v1/runs/{failed}/report", headers=headers).status_code == 500
            with admin.begin() as connection:
                connection.execute(update(runs).where(runs.c.run_id == failed).values(state="failed"))
                connection.execute(update(reports).where(reports.c.run_id == failed).values(
                    report_json=failed_report | {"review_tamper": True}))
            checks["api_report_content_hash_mismatch_rejected"] = client.get(
                f"/v1/runs/{failed}/report", headers=headers).status_code == 500
            with admin.begin() as connection:
                connection.execute(update(reports).where(reports.c.run_id == failed).values(
                    report_json=failed_report))
        wrong_owner = create_app(director_engine=engines["director"], director_token=token,
                                 owner_id=dataset + "-other")
        with TestClient(wrong_owner) as client:
            checks["api_wrong_owner_report_hidden"] = client.get(
                f"/v1/runs/{failed}/report", headers=headers).status_code == 404
    except Exception as error:
        # No connection strings, claim tokens or SQL bind values in evidence.
        record["error_type"] = type(error).__name__
    finally:
        with admin.begin() as connection:
            connection.execute(delete(runs).where(runs.c.run_id.in_(owned_runs)))
            connection.execute(delete(dataset_profiles).where(dataset_profiles.c.dataset_id == dataset))
            if legacy_verified and legacy is not None:
                connection.execute(delete(runs).where(runs.c.run_id == UUID(legacy["run_id"])))
                connection.execute(delete(dataset_profiles).where(
                    dataset_profiles.c.dataset_id == legacy["dataset_id"]))
                record["legacy_fixture_cleaned"] = True
        if legacy_verified:
            LEGACY.unlink()
        with admin.connect() as connection:
            checks["owned_run_rows_cleaned"] = connection.execute(select(runs.c.run_id).where(
                runs.c.run_id.in_(owned_runs))).first() is None
            checks["owned_dataset_rows_cleaned"] = connection.execute(select(
                dataset_profiles.c.dataset_id).where(dataset_profiles.c.dataset_id == dataset)).first() is None
        for item in engines.values():
            item.dispose()
    record["source_unchanged"] = all(
        hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == sha for path, sha in sources.items()
    )
    record["all_passed"] = not record.get("error_type") and bool(checks) and all(checks.values()) and record["source_unchanged"]
    record["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    OUTPUT.write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))
    if not record["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
