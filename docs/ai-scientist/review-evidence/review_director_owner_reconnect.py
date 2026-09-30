"""Probe an owned Director connection invalidation without touching other sessions."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import tempfile
from uuid import uuid4

from sqlalchemy import create_engine, delete, insert, select

from lab.db.schema import run_events, runs
from lab.director.journal import DirectorRunLease

ROOT = Path(__file__).resolve().parents[3]


def main() -> None:
    """Ensure a disconnected old owner cannot append after a replacement acquires."""
    source_path = ROOT / "lab/director/journal.py"
    before = hashlib.sha256(source_path.read_bytes()).hexdigest()
    engines = {
        role: create_engine(
            (ROOT / f"data/runtime/postgres/{role}.dsn").read_text().strip(),
            pool_size=3,
            max_overflow=0,
        )
        for role in ("migrator", "director")
    }
    run_id = uuid4()
    result = {
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": "Owned PostgreSQL run and two Director sessions; explicit local invalidation only. No live AOS, Docker, GPU, model, or unrelated session changes.",
        "run_id": str(run_id),
        "source_sha256": before,
        "checks": {},
    }
    try:
        with engines["migrator"].begin() as connection:
            connection.execute(insert(runs).values(
                run_id=run_id, origin="local", owner_id="director-reconnect-review",
                idempotency_key=f"director-reconnect-{run_id}",
                payload_sha256=hashlib.sha256(str(run_id).encode()).hexdigest(),
                request_json={"review": "director-owner-reconnect"}, state="running",
                stop_requested=False,
            ))
        with tempfile.TemporaryDirectory(prefix="director-reconnect-", dir=ROOT / "data/runtime") as temporary:
            with DirectorRunLease(engines["director"], run_id) as old:
                old_connection = old._connection  # Inspect only this probe's owned connection.
                assert old_connection is not None
                old_connection.invalidate()
                result["checks"]["old_connection_invalidated"] = old_connection.invalidated
                with DirectorRunLease(engines["director"], run_id) as replacement:
                    replacement.heartbeat()
                    result["checks"]["replacement_acquired"] = True
                    try:
                        old.append_checkpoint(
                            sequence=0, key="stale-owner", phase="LOOP",
                            payload={"writer": "disconnected-owner"}, artifact_root=Path(temporary),
                        )
                    except RuntimeError as error:
                        result["stale_append_error"] = str(error)
                        result["checks"]["stale_owner_append_rejected"] = str(error) == "director_ownership_lost"
                    else:
                        result["checks"]["stale_owner_append_rejected"] = False
                    with engines["migrator"].connect() as connection:
                        rows = connection.execute(select(run_events.c.event_id).where(run_events.c.run_id == run_id)).all()
                    result["checks"]["no_stale_checkpoint_inserted"] = not rows
                    result["checks"]["no_stale_blob_written"] = not any(
                        path.is_file() for path in Path(temporary).rglob("*")
                    )
    finally:
        with engines["migrator"].begin() as connection:
            connection.execute(delete(runs).where(runs.c.run_id == run_id))
        for engine in engines.values():
            engine.dispose()
    result["source_unchanged"] = hashlib.sha256(source_path.read_bytes()).hexdigest() == before
    result["all_passed"] = result["source_unchanged"] and all(result["checks"].values())
    result["command"] = ".venv/bin/python docs/ai-scientist/review-evidence/review_director_owner_reconnect.py"
    result["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    Path(__file__).with_name("director-owner-reconnect-review.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    if not result["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
