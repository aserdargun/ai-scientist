"""Exercise owned PostgreSQL checkpoint ordering, immutability and lock scope."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import tempfile
from uuid import uuid4

from sqlalchemy import delete, insert, select, text
from sqlalchemy.exc import DBAPIError

from lab.db.schema import run_events, runs
from lab.director.journal import DirectorRunLease
from review_scorer_queue import ROOT, engine


def main() -> None:
    sources = [ROOT / "lab/director/journal.py", ROOT / "lab/db/migrations/versions/0014_immutable_director_events.py"]
    hashes = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    engines = {role: engine(role) for role in ("migrator", "director", "planner", "scorer")}
    run_id = uuid4()
    checks = {}
    evidence = {"checked_at": datetime.now(UTC).isoformat(), "run_id": str(run_id),
                "scope": "Owned real PostgreSQL run and checkpoint blobs; role mutation denial, ordered idempotent writes, single owner and plan-lock compatibility. Not full Director resume or external-action fencing.",
                "source_sha256": hashes, "checks": checks}
    try:
        with engines["migrator"].begin() as connection:
            revision = connection.execute(text("SELECT version_num FROM lab.alembic_version")).scalar_one()
            assert revision == "0014_immutable_events"
            evidence["database_revision"] = revision
            connection.execute(insert(runs).values(
                run_id=run_id, origin="local", owner_id="journal-review",
                idempotency_key=f"journal-review-{run_id}", payload_sha256="a" * 64,
                request_json={"review": "journal"}, state="running", stop_requested=False,
            ))
        with tempfile.TemporaryDirectory(prefix="director-journal-", dir=ROOT / "data/runtime") as temporary:
            blob_root = Path(temporary)
            with DirectorRunLease(engines["director"], run_id) as lease:
                receipt = lease.append_checkpoint(sequence=0, key="first", phase="LOOP", payload={"ordinal": 1}, artifact_root=blob_root)
                checks["exact_retry_returns_same_receipt"] = lease.append_checkpoint(
                    sequence=0, key="first", phase="LOOP", payload={"ordinal": 1}, artifact_root=blob_root
                ) == receipt
                checks["readback_matches_payload_and_receipt"] = lease.read_checkpoint(key="first", artifact_root=blob_root) == {"receipt": receipt, "payload": {"ordinal": 1}}
                with engines["migrator"].connect() as connection:
                    event_id = connection.execute(select(run_events.c.event_id).where(run_events.c.run_id == run_id)).scalar_one()
                for label, arguments in (
                    ("changed_payload_rejected", {"sequence": 0, "key": "first", "payload": {"ordinal": 2}}),
                    ("sequence_gap_rejected", {"sequence": 2, "key": "skipped", "payload": {"ordinal": 3}}),
                ):
                    try:
                        lease.append_checkpoint(**arguments, phase="LOOP", artifact_root=blob_root)
                    except RuntimeError:
                        checks[label] = True
                    else:
                        checks[label] = False
                try:
                    with DirectorRunLease(engines["director"], run_id):
                        checks["second_owner_refused"] = False
                except RuntimeError as error:
                    checks["second_owner_refused"] = str(error) == "director_capacity_busy"
                with engines["director"].begin() as connection:
                    connection.execute(text("SET LOCAL statement_timeout = '1000ms'"))
                    connection.execute(text("SELECT lab.lock_run_plan(:run_id)"), {"run_id": run_id})
                checks["plan_lock_on_separate_connection_does_not_deadlock"] = True
                for role in ("director", "planner", "scorer"):
                    for operation, sql in (
                        ("update", "UPDATE lab.run_events SET event_json = '{}'::jsonb WHERE event_id = :event_id"),
                        ("delete", "DELETE FROM lab.run_events WHERE event_id = :event_id"),
                    ):
                        key = f"{role}_{operation}_denied"
                        try:
                            with engines[role].begin() as connection:
                                connection.execute(text(sql), {"event_id": event_id})
                        except DBAPIError as error:
                            checks[key] = getattr(error.orig, "sqlstate", None) == "42501"
                        else:
                            checks[key] = False
                checks["runtime_attempts_preserved_checkpoint"] = lease.read_checkpoint(key="first", artifact_root=blob_root) == {"receipt": receipt, "payload": {"ordinal": 1}}
                second = lease.append_checkpoint(sequence=1, key="second", phase="LOOP", payload={"ordinal": 2}, artifact_root=blob_root)
                checks["next_ordered_checkpoint_accepted"] = second["sequence"] == 1
            with DirectorRunLease(engines["director"], run_id) as replacement:
                checks["new_session_resumes_same_checkpoint"] = replacement.read_checkpoint(key="second", artifact_root=blob_root) == {"receipt": second, "payload": {"ordinal": 2}}
            with engines["migrator"].connect() as connection:
                checks["exactly_two_rows"] = len(connection.execute(select(run_events.c.event_id).where(run_events.c.run_id == run_id)).all()) == 2
    finally:
        with engines["migrator"].begin() as connection:
            connection.execute(delete(runs).where(runs.c.run_id == run_id))
            checks["owned_fixture_cleanup_succeeded"] = connection.execute(select(run_events.c.event_id).where(run_events.c.run_id == run_id)).first() is None
        for item in engines.values():
            item.dispose()
    evidence["source_unchanged"] = all(hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest for path, digest in hashes.items())
    evidence["all_passed"] = all(checks.values()) and evidence["source_unchanged"]
    evidence["command"] = ".venv/bin/python docs/ai-scientist/review-evidence/review_director_journal.py"
    evidence["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    Path(__file__).with_name("director-journal-review.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence, indent=2))
    if not evidence["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
