"""Probe the real 0011 role surface; no data or schema changes are committed."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[3]
CHECKS = {
    "director": [
        ("read proposal ledger", "SELECT * FROM lab.experiments LIMIT 0", True),
        ("read full experiment records", "SELECT * FROM lab.experiment_records LIMIT 0", False),
        ("read full trajectories", "SELECT * FROM lab.trajectory_records LIMIT 0", False),
        ("read dev results", "SELECT * FROM lab.dev_task_results LIMIT 0", True),
        ("direct proposal insert", "INSERT INTO lab.experiments SELECT * FROM lab.experiments WHERE false", False),
        ("direct lifecycle update", "UPDATE lab.experiments SET status=status WHERE false", False),
        ("direct experiment record insert", "INSERT INTO lab.experiment_records DEFAULT VALUES", False),
        ("direct trajectory insert", "INSERT INTO lab.trajectory_records DEFAULT VALUES", False),
        ("delete proposals", "DELETE FROM lab.experiments WHERE false", False),
        ("rewrite experiment records", "UPDATE lab.experiment_records SET experiment_json=experiment_json WHERE false", False),
        ("delete experiment records", "DELETE FROM lab.experiment_records WHERE false", False),
        ("rewrite trajectories", "UPDATE lab.trajectory_records SET trajectory_json=trajectory_json WHERE false", False),
        ("delete trajectories", "DELETE FROM lab.trajectory_records WHERE false", False),
        ("read unfiltered scores", "SELECT * FROM scorer.task_scores LIMIT 0", False),
        ("read private labels", "SELECT * FROM scorer.dataset_labels LIMIT 0", False),
    ],
    "planner": [
        ("rewrite profile visibility", "UPDATE scorer.dataset_profiles SET visibility=visibility WHERE false", False),
        ("read dev numeric results", "SELECT * FROM lab.dev_task_results LIMIT 0", False),
        ("direct proposal insert", "INSERT INTO lab.experiments DEFAULT VALUES", False),
        ("direct lifecycle update", "UPDATE lab.experiments SET status=status WHERE false", False),
        ("insert experiment record", "INSERT INTO lab.experiment_records DEFAULT VALUES", False),
        ("insert trajectory", "INSERT INTO lab.trajectory_records DEFAULT VALUES", False),
    ],
    "scorer": [
        ("rewrite profile visibility", "UPDATE scorer.dataset_profiles SET visibility=visibility WHERE false", False),
        ("direct proposal insert", "INSERT INTO lab.experiments DEFAULT VALUES", False),
        ("direct lifecycle update", "UPDATE lab.experiments SET status=status WHERE false", False),
        ("insert experiment record", "INSERT INTO lab.experiment_records DEFAULT VALUES", False),
        ("insert trajectory", "INSERT INTO lab.trajectory_records DEFAULT VALUES", False),
    ],
}


def connect(role: str) -> psycopg.Connection:
    dsn = (ROOT / f"data/runtime/postgres/{role}.dsn").read_text().strip()
    return psycopg.connect(
        dsn.replace("postgresql+psycopg://", "postgresql://", 1),
        autocommit=True,
        connect_timeout=5,
    )


def main() -> None:
    with connect("migrator") as connection:
        revision = connection.execute("SELECT version_num FROM lab.alembic_version").fetchone()[0]
    if revision != "0011_experiment_ledger":
        raise SystemExit("0011 must be applied before this review; no probe executed")
    sources = [
        ROOT / "lab/db/migrations/versions/0011_experiment_ledger.py",
        ROOT / "lab/db/schema.py",
    ]
    evidence = {
        "checked_at": datetime.now(UTC).isoformat(),
        "database_revision": revision,
        "scope": "Real separately authenticated PostgreSQL role permissions. Zero-row operations are rolled back. This does not establish row-level lifecycle, JSON/blob binding, holdout filtering, or Director end-to-end acceptance.",
        "source_sha256": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sources
        },
        "checks": [],
    }
    for role, checks in CHECKS.items():
        with connect(role) as connection:
            evidence[f"{role}_authenticated_as"] = connection.execute("SELECT session_user").fetchone()[0]
            for label, query, expected in checks:
                result = {"role": role, "probe": label, "query": query, "expected_allowed": expected}
                try:
                    with connection.transaction():
                        connection.execute(query)
                        result.update(allowed=True, sqlstate=None)
                        raise psycopg.Rollback()
                except psycopg.Error as error:
                    result.update(allowed=False, sqlstate=error.sqlstate)
                result["passed"] = result["allowed"] == expected and (
                    expected or result["sqlstate"] == "42501"
                )
                evidence["checks"].append(result)
            for name in (
                "register_experiment", "transition_experiment",
                "commit_experiment_documents", "experiment_record_receipt",
            ):
                count, allowed = connection.execute(
                    "SELECT count(*), bool_and(has_function_privilege(p.oid, 'EXECUTE')) "
                    "FROM pg_catalog.pg_proc p "
                    "JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace "
                    "WHERE n.nspname='lab' AND p.proname=%s",
                    (name,),
                ).fetchone()
                evidence["checks"].append({
                    "role": role,
                    "probe": f"execute {name}",
                    "matching_functions": count,
                    "expected_allowed": role == "director",
                    "allowed": allowed,
                    "passed": count == 1 and allowed is (role == "director"),
                })
    evidence["source_unchanged"] = all(
        hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest
        for path, digest in evidence["source_sha256"].items()
    )
    evidence["all_passed"] = (
        len(evidence["checks"]) == 38
        and all(row["passed"] for row in evidence["checks"])
        and evidence["source_unchanged"]
    )
    evidence["command"] = ".venv/bin/python docs/ai-scientist/review-evidence/review_experiment_role_surface.py"
    evidence["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    Path(__file__).with_name("experiment-role-surface-review.json").write_text(
        json.dumps(evidence, indent=2) + "\n"
    )
    print(json.dumps(evidence, indent=2))
    if not evidence["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
