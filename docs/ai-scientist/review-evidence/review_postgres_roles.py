"""Authenticate as each real runtime role and probe grants with rollback."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[3]

CHECKS = {
    "director": [
        ("read runs", "SELECT * FROM lab.runs LIMIT 0", True),
        ("read reports", "SELECT * FROM lab.reports LIMIT 0", True),
        ("read score queue", "SELECT * FROM scorer.score_jobs LIMIT 0", True),
        ("enqueue score job", "INSERT INTO scorer.score_jobs SELECT * FROM scorer.score_jobs WHERE false", False),
        ("claim score job", "UPDATE scorer.score_jobs SET state=state WHERE false", False),
        ("read task plan", "SELECT * FROM scorer.run_tasks LIMIT 0", False),
        ("read labels", "SELECT * FROM scorer.dataset_labels LIMIT 0", False),
        ("read label profiles", "SELECT * FROM scorer.dataset_profiles LIMIT 0", False),
        ("write reports", "INSERT INTO lab.reports DEFAULT VALUES", False),
        ("assume scorer", "SET ROLE swapp_lab_scorer", False),
        ("assume planner", "SET ROLE swapp_lab_planner", False),
        ("assume migrator", "SET ROLE swapp_lab_migrator", False),
        ("create in public", "CREATE TABLE public.root_review_probe(value integer)", False),
    ],
    "planner": [
        ("read runs", "SELECT * FROM lab.runs LIMIT 0", True),
        ("modify runs", "UPDATE lab.runs SET state=state WHERE false", False),
        ("write plan seal columns", "UPDATE lab.runs SET task_plan_sha256=task_plan_sha256, task_plan_count=task_plan_count WHERE false", True),
        ("read profiles", "SELECT * FROM scorer.dataset_profiles LIMIT 0", True),
        ("modify profiles", "UPDATE scorer.dataset_profiles SET sample_count=sample_count WHERE false", False),
        ("read labels", "SELECT * FROM scorer.dataset_labels LIMIT 0", False),
        ("read task plans", "SELECT * FROM scorer.run_tasks LIMIT 0", True),
        ("insert task plans", "INSERT INTO scorer.run_tasks SELECT * FROM scorer.run_tasks WHERE false", True),
        ("modify task plans", "UPDATE scorer.run_tasks SET seed=seed WHERE false", False),
        ("delete task plans", "DELETE FROM scorer.run_tasks WHERE false", False),
        ("read score queue", "SELECT * FROM scorer.score_jobs LIMIT 0", True),
        ("enqueue score job", "INSERT INTO scorer.score_jobs SELECT * FROM scorer.score_jobs WHERE false", True),
        ("claim score job", "UPDATE scorer.score_jobs SET state=state WHERE false", False),
        ("delete score job", "DELETE FROM scorer.score_jobs WHERE false", False),
        ("read scores", "SELECT * FROM scorer.task_scores LIMIT 0", False),
        ("write reports", "INSERT INTO lab.reports DEFAULT VALUES", False),
        ("assume scorer", "SET ROLE swapp_lab_scorer", False),
        ("assume director", "SET ROLE swapp_lab_director", False),
        ("assume migrator", "SET ROLE swapp_lab_migrator", False),
        ("create in public", "CREATE TABLE public.root_review_probe(value integer)", False),
    ],
    "scorer": [
        ("read labels", "SELECT * FROM scorer.dataset_labels LIMIT 0", True),
        ("read profiles", "SELECT * FROM scorer.dataset_profiles LIMIT 0", True),
        ("modify labels", "UPDATE scorer.dataset_labels SET is_anomaly=is_anomaly WHERE false", False),
        ("delete labels", "DELETE FROM scorer.dataset_labels WHERE false", False),
        ("modify profiles", "UPDATE scorer.dataset_profiles SET sample_count=sample_count WHERE false", False),
        ("change run owner", "UPDATE lab.runs SET owner_id=owner_id WHERE false", False),
        ("write run state", "UPDATE lab.runs SET state=state WHERE false", True),
        ("modify plan seal", "UPDATE lab.runs SET task_plan_sha256=task_plan_sha256, task_plan_count=task_plan_count WHERE false", False),
        ("read score queue", "SELECT * FROM scorer.score_jobs LIMIT 0", True),
        ("claim score job", "UPDATE scorer.score_jobs SET state=state WHERE false", True),
        ("enqueue score job", "INSERT INTO scorer.score_jobs SELECT * FROM scorer.score_jobs WHERE false", False),
        ("delete score job", "DELETE FROM scorer.score_jobs WHERE false", False),
        ("insert task plans", "INSERT INTO scorer.run_tasks SELECT * FROM scorer.run_tasks WHERE false", False),
        ("assume planner", "SET ROLE swapp_lab_planner", False),
        ("assume migrator", "SET ROLE swapp_lab_migrator", False),
    ],
}


def main() -> None:
    evidence = {
        "checked_at": datetime.now(UTC).isoformat(),
        "claim": "Actual independent role authentication and privilege checks; every probe transaction rolls back.",
        "checks": [],
        "source_sha256": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (*sorted((ROOT / "lab/db/migrations/versions").glob("*.py")),
                         ROOT / "scripts/bootstrap_db_roles.py")
        },
    }
    for role, checks in CHECKS.items():
        dsn = (ROOT / f"data/runtime/postgres/{role}.dsn").read_text().strip()
        dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)
        with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as connection:
            current_role = connection.execute("SELECT current_user").fetchone()[0]
            evidence[f"{role}_authenticated_as"] = current_role
            for label, query, expected in checks:
                result = {"role": role, "probe": label, "query": query,
                          "expected_allowed": expected}
                try:
                    with connection.transaction():
                        connection.execute(query)
                        result.update(allowed=True, sqlstate=None)
                        raise psycopg.Rollback()
                except psycopg.Error as exc:
                    result.update(allowed=False, sqlstate=exc.sqlstate,
                                  exception_type=type(exc).__name__)
                result["passed"] = (
                    result["allowed"] == expected
                    and (expected or result["sqlstate"] == "42501")
                )
                evidence["checks"].append(result)
    evidence["source_unchanged"] = all(
        hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest
        for path, digest in evidence["source_sha256"].items()
    )
    evidence["all_passed"] = (
        all(row["passed"] for row in evidence["checks"])
        and evidence["source_unchanged"]
    )
    evidence["command"] = ".venv/bin/python docs/ai-scientist/review-evidence/review_postgres_roles.py"
    evidence["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    output = ROOT / "docs/ai-scientist/review-evidence/postgres-role-review.json"
    output.write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence, indent=2))
    if not evidence["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
