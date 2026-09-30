"""Verify experiment documents fence final reports, including sealed-plan recovery."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb
from sqlalchemy import insert, text
from sqlalchemy.exc import DBAPIError

from lab.db.schema import task_terminal_outcomes
from lab.director.task_plan import RunTaskAssignment, plan_run_tasks, seal_run_task_plan
from lab.scorer.service import IndependentScorer
from review_experiment_ledger import ROOT, commit, connect, digest, documents, proposal, register
from review_scorer_queue import engine


def main() -> None:
    paths = [ROOT / "lab/scorer/service.py", ROOT / "lab/db/schema.py",
             ROOT / "lab/director/task_plan.py",
             ROOT / "lab/db/migrations/versions/0011_experiment_ledger.py",
             *sorted((ROOT / "lab/db/migrations/versions").glob("0012_*.py"))]
    evidence = {
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": "Real PostgreSQL roles and production Planner/Scorer services; owned synthetic crash/cancel outcomes and fabricated no-evaluation documents. Tests terminal report/document fencing and recovery after task-plan sealing. No candidate, LLM, metric, physical blob, public-data or AOS acceptance.",
        "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in paths},
        "checks": {},
    }
    planner, scorer = engine("planner"), engine("scorer")
    owned = []
    service = IndependentScorer(scorer, harness_sha256="a" * 64)
    with connect("migrator") as migrator, connect("director") as director:
        revision = migrator.execute("SELECT version_num FROM lab.alembic_version").fetchone()[0]
        if not revision.startswith("0012_"):
            raise SystemExit("0012 must be applied before this review; no fixtures created")
        evidence["database_revision"] = revision

        def fixture():
            run_id = uuid4()
            dataset_id = f"finalization-fixed-{uuid4()}"
            owned.append((run_id, dataset_id))
            migrator.execute(
                "INSERT INTO lab.runs(run_id,origin,owner_id,idempotency_key,payload_sha256,request_json,state) "
                "VALUES(%s,'local',%s,%s,%s,%s,'running')",
                (run_id, dataset_id, dataset_id, digest({"fixture": dataset_id}), Jsonb({"fixture": dataset_id})),
            )
            migrator.execute(
                "INSERT INTO scorer.dataset_profiles(dataset_id,split_id,session_id,sample_count,sliding_window,profile_sha256,visibility) "
                "VALUES(%s,'review','review',16,4,%s,'dev')",
                (dataset_id, digest({"profile": dataset_id})),
            )
            value = proposal(run_id)
            register(director, value)
            assignment = RunTaskAssignment(
                experiment_id=value["experiment_id"], evaluation_kind="primary", task_id="review-task",
                seed=0, candidate_sha256=value["candidate_sha256"], dataset_id=dataset_id,
                split_id="review", session_id="review",
            )
            plan_run_tasks(planner, run_id=run_id, assignments=(assignment,))
            return run_id, value

        def terminal(run_id, value, outcome):
            with planner.begin() as connection:
                connection.execute(insert(task_terminal_outcomes).values(
                    run_id=run_id, experiment_id=value["experiment_id"], evaluation_kind="primary",
                    task_id="review-task", seed=0, candidate_sha256=value["candidate_sha256"],
                    outcome_code=outcome, producer_role="swapp_lab_planner",
                ))

        try:
            run_id, value = fixture()
            unplanned = proposal(run_id, sequence=1)
            register(director, unplanned)
            terminal(run_id, value, "candidate_crash")
            seal = seal_run_task_plan(planner, run_id=run_id)
            evidence["checks"]["one_resolved_task_sealed"] = seal.task_count == 1
            evidence["checks"]["auto_finalizer_waits_for_documents"] = service.finalize_if_ready(run_id=run_id) is None
            try:
                service.complete_run(run_id=run_id)
                evidence["checks"]["explicit_finalizer_rejects_missing_documents"] = False
            except (ValueError, DBAPIError):
                evidence["checks"]["explicit_finalizer_rejects_missing_documents"] = True
            try:
                with scorer.begin() as connection:
                    connection.execute(text("UPDATE lab.runs SET state='completed' WHERE run_id=:run"), {"run": run_id})
                    evidence["checks"]["database_blocks_direct_premature_terminal_update"] = False
                    raise RuntimeError("rollback unexpected permitted update")
            except DBAPIError as error:
                evidence["checks"]["database_blocks_direct_premature_terminal_update"] = getattr(error.orig, "sqlstate", None) == "P0001"
            except RuntimeError:
                pass
            state, count = migrator.execute(
                "SELECT state,(SELECT count(*) FROM lab.reports WHERE run_id=r.run_id) "
                "FROM lab.runs r WHERE run_id=%s", (run_id,),
            ).fetchone()
            evidence["checks"]["failed_finalization_keeps_running_and_no_report"] = (state, count) == ("running", 0)
            exp, traj = documents(value, status="crashed")
            evidence["checks"]["terminal_pair_can_commit_after_seal"] = commit(director, exp, traj)["status"] == "committed"
            evidence["checks"]["registered_unplanned_experiment_also_blocks_report"] = service.finalize_if_ready(run_id=run_id) is None
            try:
                with director.transaction():
                    register(director, proposal(run_id, sequence=2))
                    evidence["checks"]["new_proposal_after_seal_rejected"] = False
                    raise psycopg.Rollback()
            except psycopg.Error as error:
                evidence["checks"]["new_proposal_after_seal_rejected"] = error.sqlstate == "P0001"
            try:
                with director.transaction():
                    director.execute("SELECT lab.transition_experiment(%s,'primary_running')", (unplanned["experiment_id"],))
                    evidence["checks"]["nonterminal_transition_after_seal_rejected"] = False
                    raise psycopg.Rollback()
            except psycopg.Error as error:
                evidence["checks"]["nonterminal_transition_after_seal_rejected"] = error.sqlstate == "P0001"
            other_exp, other_traj = documents(unplanned, status="abandoned")
            commit(director, other_exp, other_traj)
            result = service.finalize_if_ready(run_id=run_id)
            evidence["checks"]["report_completes_after_all_document_pairs"] = result is not None and result[0]["status"] == "completed"
            evidence["checks"]["completed_report_retry_identical"] = result is not None and service.complete_run(run_id=run_id) == result

            stopped_run, stopped_value = fixture()
            director.execute("UPDATE lab.runs SET state='stop_requested',stop_requested=true WHERE run_id=%s", (stopped_run,))
            terminal(stopped_run, stopped_value, "cancelled")
            stopped_seal = seal_run_task_plan(planner, run_id=stopped_run)
            evidence["checks"]["stopped_task_plan_can_seal"] = stopped_seal.task_count == 1
            evidence["checks"]["stopped_finalizer_waits_for_documents"] = service.finalize_if_ready(run_id=stopped_run) is None
            stopped_exp, stopped_traj = documents(stopped_value, status="abandoned")
            evidence["checks"]["abandoned_pair_can_commit_after_stop_and_seal"] = commit(director, stopped_exp, stopped_traj)["status"] == "committed"
            stopped_result = service.finalize_if_ready(run_id=stopped_run)
            evidence["checks"]["stopped_report_completes_after_document_pair"] = stopped_result is not None and stopped_result[0]["status"] == "stopped"
            evidence["checks"]["stopped_report_retry_identical"] = stopped_result is not None and service.complete_run(run_id=stopped_run) == stopped_result
            evidence["checks"]["terminal_document_retry_after_stopped_report"] = commit(director, stopped_exp, stopped_traj)["status"] == "already_committed"
        except Exception as error:
            evidence["error"] = {"type": type(error).__name__, "sqlstate": getattr(error, "sqlstate", None)}
        finally:
            with migrator.transaction():
                for run_id, dataset_id in owned:
                    migrator.execute("DELETE FROM lab.runs WHERE run_id=%s", (run_id,))
                    migrator.execute("DELETE FROM scorer.dataset_profiles WHERE dataset_id=%s", (dataset_id,))
            evidence["checks"]["owned_rows_removed"] = migrator.execute(
                "SELECT count(*) FROM lab.runs WHERE run_id=ANY(%s)", ([r for r, _ in owned],),
            ).fetchone()[0] == 0
    planner.dispose()
    scorer.dispose()
    evidence["source_unchanged"] = all(
        hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == sha
        for name, sha in evidence["source_sha256"].items()
    )
    evidence["all_passed"] = (
        not evidence.get("error") and len(evidence["checks"]) == 18
        and all(evidence["checks"].values()) and evidence["source_unchanged"]
    )
    evidence["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    evidence["command"] = ".venv/bin/python docs/ai-scientist/review-evidence/review_experiment_finalization.py"
    Path(__file__).with_name("experiment-finalization-review.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence, indent=2))
    if not evidence["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
