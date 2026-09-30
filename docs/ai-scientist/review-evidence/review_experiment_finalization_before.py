"""Reproduce premature run completion with a registered but undocumented experiment."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb
from sqlalchemy import insert, text

from lab.db.schema import task_terminal_outcomes
from lab.director.task_plan import RunTaskAssignment, plan_run_tasks, seal_run_task_plan
from lab.scorer.service import IndependentScorer
from review_experiment_ledger import ROOT, commit, connect, digest, documents, proposal, register
from review_scorer_queue import engine


def main() -> None:
    run_id = uuid4()
    dataset_id = f"finalization-review-{uuid4()}"
    sources = ["lab/scorer/service.py", "lab/director/task_plan.py", "lab/db/schema.py",
               "lab/db/migrations/versions/0011_experiment_ledger.py"]
    record = {
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": "Real PostgreSQL roles and production plan/finalizer services, one owned synthetic Planner crash outcome. No candidate execution, metric, model or public data. Negative counterexample, not an acceptance pass.",
        "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                          for name in sources},
    }
    planner, scorer = engine("planner"), engine("scorer")
    with connect("migrator") as migrator, connect("director") as director:
        record["database_revision"] = migrator.execute(
            "SELECT version_num FROM lab.alembic_version"
        ).fetchone()[0]
        value = proposal(run_id)
        try:
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
            register(director, value)
            assignment = RunTaskAssignment(
                experiment_id=value["experiment_id"], evaluation_kind="primary", task_id="review-task",
                seed=0, candidate_sha256=value["candidate_sha256"], dataset_id=dataset_id,
                split_id="review", session_id="review",
            )
            plan_run_tasks(planner, run_id=run_id, assignments=(assignment,))
            with planner.begin() as connection:
                connection.execute(insert(task_terminal_outcomes).values(
                    run_id=run_id, experiment_id=value["experiment_id"], evaluation_kind="primary",
                    task_id="review-task", seed=0, candidate_sha256=value["candidate_sha256"],
                    outcome_code="candidate_crash", producer_role="swapp_lab_planner",
                ))
            seal = seal_run_task_plan(planner, run_id=run_id)
            record["sealed_task_count"] = seal.task_count
            exp, traj = documents(value, status="crashed")
            try:
                with director.transaction():
                    commit(director, exp, traj)
                    record["terminal_commit_after_seal_rejected"] = False
                    raise psycopg.Rollback()
            except psycopg.Error as error:
                record["terminal_commit_after_seal_rejected"] = error.sqlstate == "P0001"
                record["terminal_commit_sqlstate"] = error.sqlstate
            result = IndependentScorer(scorer, harness_sha256="a" * 64).finalize_if_ready(run_id=run_id)
            row = migrator.execute(
                "SELECT r.state,e.status,"
                "(SELECT count(*) FROM lab.experiment_records WHERE experiment_id=e.experiment_id),"
                "(SELECT count(*) FROM lab.trajectory_records WHERE experiment_id=e.experiment_id) "
                "FROM lab.runs r JOIN lab.experiments e ON e.run_id=r.run_id WHERE r.run_id=%s",
                (run_id,),
            ).fetchone()
            record["final_state"] = dict(zip(
                ("run", "experiment", "experiment_record_count", "trajectory_record_count"), row,
            ))
            record["report_was_published"] = result is not None
            record["premature_completion_reproduced"] = result is not None and row == ("completed", "proposed", 0, 0)
        except Exception as error:
            record["error"] = {"type": type(error).__name__, "sqlstate": getattr(error, "sqlstate", None)}
        finally:
            with migrator.transaction():
                migrator.execute("DELETE FROM lab.runs WHERE run_id=%s", (run_id,))
                migrator.execute("DELETE FROM scorer.dataset_profiles WHERE dataset_id=%s", (dataset_id,))
            record["owned_rows_removed"] = migrator.execute(
                "SELECT count(*) FROM lab.runs WHERE run_id=%s", (run_id,),
            ).fetchone()[0] == 0
    planner.dispose()
    scorer.dispose()
    record["source_unchanged"] = all(
        hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == sha
        for name, sha in record["source_sha256"].items()
    )
    record["counterexample_verified"] = (
        not record.get("error") and record.get("premature_completion_reproduced")
        and record.get("terminal_commit_after_seal_rejected")
        and record["owned_rows_removed"] and record["source_unchanged"]
    )
    record["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    record["command"] = ".venv/bin/python docs/ai-scientist/review-evidence/review_experiment_finalization_before.py"
    Path(__file__).with_name("experiment-finalization-before.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))
    if not record["counterexample_verified"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
