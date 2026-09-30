"""Probe idempotent pre-score terminal outcomes using only owned PG fixtures."""

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, insert, select
from sqlalchemy.exc import DBAPIError

from lab.db.schema import dataset_profiles, runs, task_terminal_outcomes
from lab.director.baselines import baseline_candidate_source
from lab.director.ledger import register_experiment, transition_experiment
from lab.director.task_plan import (
    RunTaskAssignment, plan_run_tasks, record_planner_terminal_outcome,
)
from review_scorer_queue import ROOT, engine


def main() -> None:
    sources = [ROOT / "lab/director/task_plan.py", ROOT / "lab/db/migrations/versions/0010_terminal_recovery_fence.py"]
    hashes = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    engines = {role: engine(role) for role in ("migrator", "director", "planner")}
    run_id = uuid4()
    dataset_id = f"planner-outcome-review-{uuid4()}"
    experiment_id = f"exp_{uuid4().hex}"
    candidate_sha = hashlib.sha256(baseline_candidate_source("robust_z")).hexdigest()
    checks = {}
    result = {
        "checked_at": datetime.now(UTC).isoformat(), "run_id": str(run_id),
        "scope": "Actual PostgreSQL Planner wrapper with an injected guard_rejected outcome. No candidate execution, scientific measurements, baseline calibration or Director run acceptance.",
        "source_sha256": hashes, "checks": checks,
    }
    try:
        with engines["migrator"].begin() as connection:
            connection.execute(insert(runs).values(
                run_id=run_id, origin="local", owner_id="planner-outcome-review",
                idempotency_key=f"planner-outcome-{run_id}", payload_sha256="a" * 64,
                request_json={"review": "planner terminal outcome"}, state="running",
            ))
            connection.execute(insert(dataset_profiles).values(
                dataset_id=dataset_id, split_id="synthetic", session_id="local", sample_count=16,
                sliding_window=4, profile_sha256="c" * 64, visibility="dev",
            ))
        register_experiment(
            engines["director"], experiment_id=experiment_id, run_id=str(run_id), sequence=0,
            experiment_number=None, kind="baseline", baseline_name="robust_z",
            parent_experiment_id=None, candidate_sha256=candidate_sha,
            candidate_blob_sha256=candidate_sha, inputs_sha256="b" * 64,
            move_type="detector", system="S1", hypothesis="Injected wrapper control fixture",
            predicted_delta=None, proposal={"schema": "baseline.registration.v1", "algorithm": "robust_z"},
        )
        transition_experiment(engines["director"], experiment_id=experiment_id, status="primary_running")
        plan_run_tasks(engines["planner"], run_id=run_id, assignments=(RunTaskAssignment(
            experiment_id=experiment_id, evaluation_kind="baseline", task_id="wrapper-fixture", seed=0,
            candidate_sha256=candidate_sha, dataset_id=dataset_id, split_id="synthetic", session_id="local",
        ),))
        arguments = dict(run_id=run_id, experiment_id=experiment_id, evaluation_kind="baseline",
                         task_id="wrapper-fixture", seed=0, candidate_sha256=candidate_sha,
                         outcome_code="guard_rejected")
        record_planner_terminal_outcome(engines["planner"], **arguments)
        checks["first_write_succeeded"] = True
        try:
            record_planner_terminal_outcome(engines["planner"], **arguments)
        except (ValueError, DBAPIError) as error:
            checks["same_payload_retry_succeeded"] = False
            result["retry_error"] = {"type": type(error).__name__, "sqlstate": getattr(getattr(error, "orig", None), "sqlstate", None)}
        else:
            checks["same_payload_retry_succeeded"] = True
        try:
            record_planner_terminal_outcome(engines["planner"], **{**arguments, "outcome_code": "candidate_crash"})
        except (ValueError, DBAPIError):
            checks["different_outcome_rejected"] = True
        else:
            checks["different_outcome_rejected"] = False
        with engines["planner"].connect() as connection:
            rows = connection.execute(select(task_terminal_outcomes).where(task_terminal_outcomes.c.run_id == run_id)).mappings().all()
        checks["single_original_outcome_preserved"] = len(rows) == 1 and rows[0]["outcome_code"] == "guard_rejected"
        checks["producer_is_authenticated_planner"] = rows[0]["producer_role"] == "swapp_lab_planner"
    finally:
        with engines["migrator"].begin() as connection:
            connection.execute(delete(runs).where(runs.c.run_id == run_id))
            connection.execute(delete(dataset_profiles).where(dataset_profiles.c.dataset_id == dataset_id))
        for item in engines.values():
            item.dispose()
    result["source_unchanged"] = all(hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest for path, digest in hashes.items())
    result["all_passed"] = all(checks.values()) and result["source_unchanged"]
    result["command"] = ".venv/bin/python docs/ai-scientist/review-evidence/review_planner_candidate_outcome.py"
    result["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    Path(__file__).with_name("planner-candidate-outcome-review.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    if not result["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
