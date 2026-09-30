"""Prepare one owned scored task before migration 0008 for upgrade review."""
from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import insert, text

from lab.db.schema import runs, dataset_profiles, dataset_labels
from lab.director.task_plan import RunTaskAssignment, plan_run_tasks
from lab.scorer.service import IndependentScorer
from review_scorer_queue import engine

ROOT = Path(__file__).resolve().parents[3]
STATE = ROOT / "data/runtime/terminal-review/legacy-fixture.json"


def main():
    if STATE.exists():
        raise RuntimeError("owned legacy fixture already exists; inspect it before another preparation")
    migrator, planner, scorer = (engine(role) for role in ("migrator", "planner", "scorer"))
    run_id = uuid4()
    dataset_id = "owned-terminal-upgrade-review-" + uuid4().hex
    candidate = hashlib.sha256(b"owned synthetic legacy score before 0008").hexdigest()
    try:
        with migrator.connect() as connection:
            revision = connection.execute(text("SELECT version_num FROM lab.alembic_version")).scalar_one()
            if revision != "0007_seal_task_plan":
                raise RuntimeError("this legacy fixture requires unapplied migration 0008")
        record = {
            "schema": "terminal-legacy-fixture.v1", "prepared_at": datetime.now(UTC).isoformat(),
            "migration_before": revision, "run_id": str(run_id), "dataset_id": dataset_id,
            "candidate_sha256": candidate, "experiment_id": "legacy-experiment",
            "evaluation_kind": "primary", "task_id": "legacy-task", "seed": 0,
            "scope": "One owned synthetic task with a real pre-upgrade Scorer metric row. Preparation only; not a terminal-outcome acceptance result.",
        }
        STATE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        STATE.write_text(json.dumps(record, indent=2) + "\n")
        STATE.chmod(0o600)
        with migrator.begin() as connection:
            connection.execute(insert(runs).values(
                run_id=run_id, origin="local", owner_id=dataset_id, idempotency_key=dataset_id,
                payload_sha256="a" * 64, request_json={"review_only": True},
                state="running", stop_requested=False,
            ))
            connection.execute(insert(dataset_profiles).values(
                dataset_id=dataset_id, split_id="synthetic-v1", session_id="legacy-session",
                sample_count=16, sliding_window=4, profile_sha256="b" * 64,
            ))
            connection.execute(insert(dataset_labels), [dict(
                dataset_id=dataset_id, split_id="synthetic-v1", session_id="legacy-session",
                sample_index=index, is_anomaly=index >= 8,
            ) for index in range(16)])
        assignment = RunTaskAssignment(
            experiment_id=record["experiment_id"], evaluation_kind="primary",
            task_id=record["task_id"], seed=0, candidate_sha256=candidate,
            dataset_id=dataset_id, split_id="synthetic-v1", session_id="legacy-session",
        )
        plan_run_tasks(planner, run_id=run_id, assignments=(assignment,))
        payload = json.dumps({"schema": "candidate-scores.v1", "sample_indices": list(range(16)),
                              "scores": [float(index) for index in range(16)]}).encode()
        score = IndependentScorer(scorer, harness_sha256="c" * 64).score_task(
            run_id=run_id, experiment_id=assignment.experiment_id, evaluation_kind="primary",
            task_id=assignment.task_id, seed=0, candidate_sha256=candidate,
            candidate_output=payload,
        )
        record["score_sha256"] = hashlib.sha256(json.dumps(score, sort_keys=True).encode()).hexdigest()
        record["prepared"] = True
        record["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        STATE.write_text(json.dumps(record, indent=2) + "\n")
        Path(__file__).with_name("terminal-legacy-fixture-prepared.json").write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps({"prepared": True, "run_id": str(run_id), "migration_before": revision}))
    finally:
        for item in (migrator, planner, scorer):
            item.dispose()


if __name__ == "__main__":
    main()
