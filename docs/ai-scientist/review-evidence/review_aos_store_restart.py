"""Reopen the real isolated AOS store while one detached Lab job is active."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
COPY = ROOT / "data/runtime/aos-coexistence/source-current"
sys.path[:0] = [str(COPY / "src"), str(COPY / "tests")]

from aos.lab_external import LabExternalJobCoordinator  # noqa: E402
from aos.storage import TrajectoryStore  # noqa: E402
from test_lab_external_jobs import FakeController, FakeLabClient, _request  # noqa: E402


def main():
    paths = (
        "src/aos/lab_external.py", "src/aos/storage.py", "tests/test_lab_external_jobs.py"
    )
    hashes = {name: hashlib.sha256((COPY / name).read_bytes()).hexdigest() for name in paths}
    record = {
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": (
            "Real AOS TrajectoryStore constructor/migrations/reconcile on a private temporary "
            "on-disk SQLite database, production external-job coordinator, fake controller and "
            "Lab transport. No live AOS database/service, actual Lab server, model or GPU."
        ),
        "source_sha256": hashes,
    }
    with tempfile.TemporaryDirectory(prefix="aos-store-restart-", dir=ROOT / "data/runtime") as tmp:
        database = Path(tmp) / "review.sqlite"
        first = TrajectoryStore(database)
        remote = FakeLabClient()
        try:
            with first.connection:
                first.connection.execute(
                    "INSERT INTO desktop_sessions VALUES(?,?,?,?,?,?,?,?,?)",
                    ("session-a", "runtime", "image", "HUMAN", "lease-current", 3,
                     "running", "2026-09-24T00:00:00Z", "2026-09-24T00:00:00Z"),
                )
            coordinator = LabExternalJobCoordinator(
                first.connection, FakeController(), remote,
                allowed_suites=frozenset({"synthetic.allowed.v1"}),
            )
            job = asyncio.run(coordinator.start("session-a", _request()))
            record["before_restart"] = {
                "job_state": job["state"],
                "aos_run_status": first.connection.execute(
                    "SELECT status FROM runs WHERE run_id=?", (job["aos_run_id"],)
                ).fetchone()[0],
                "runtime_state_count": first.connection.execute(
                    "SELECT count(*) FROM runtime_states WHERE run_id=?", (job["aos_run_id"],)
                ).fetchone()[0],
            }
        finally:
            first.close()
        # Keep a reference for cleanup even if __init__ fails after opening DB/lock.
        second = TrajectoryStore.__new__(TrajectoryStore)
        record["reopen_succeeded"] = False
        try:
            second.__init__(database)
            record["reopen_succeeded"] = True
            row = second.connection.execute(
                "SELECT lab_run_id,state FROM aos_external_jobs WHERE job_id=?", (job["job_id"],)
            ).fetchone()
            record["handle_preserved"] = row is not None and row["lab_run_id"] == job["lab_run_id"]
        except Exception as exc:
            record["reopen_error"] = {"type": type(exc).__name__, "message": str(exc)}
            record["handle_preserved"] = False
        finally:
            if getattr(second, "connection", None) is not None:
                second.connection.close()
            if getattr(second, "lock", None) is not None:
                os.close(second.lock)
    record["source_unchanged"] = all(
        hashlib.sha256((COPY / name).read_bytes()).hexdigest() == digest
        for name, digest in hashes.items()
    )
    record["all_passed"] = (
        record["source_unchanged"] and record["reopen_succeeded"] and record["handle_preserved"]
    )
    record["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    Path(__file__).with_name("aos-store-restart-review.json").write_text(
        json.dumps(record, indent=2) + "\n"
    )
    print(json.dumps(record, indent=2))
    if not record["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
