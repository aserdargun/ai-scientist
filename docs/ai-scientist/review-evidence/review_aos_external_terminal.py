"""Exercise detached AOS terminal handling with controlled transport ordering."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
COPY = ROOT / "data/runtime/aos-coexistence/source-current"
sys.path[:0] = [str(COPY / "src"), str(COPY / "tests")]

from aos.lab_external import (  # noqa: E402
    LabExternalJobCoordinator,
    LabRunStatus,
    LabVerifiedReport,
)
from test_lab_external_jobs import FakeController, FakeLabClient, _database, _request  # noqa: E402


def report_for(run_id):
    report = {"run_id": str(run_id), "status": "completed", "task_scores": []}
    digest = hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return LabVerifiedReport(
        run_id=run_id,
        report_sha256=digest,
        report=report,
        verified_at="2026-09-24T00:02:00Z",
    )


class CompletedRemote(FakeLabClient):
    """An idempotent start recovers a handle for an already completed remote run."""

    def __init__(self):
        super().__init__()
        self.state = "completed"
        self.report_document = report_for(self.run_id)
        self.report_calls = 0

    def start(self, request):
        return super().start(request).model_copy(update={"reused": True})

    def status(self, run_id):
        return super().status(run_id).model_copy(
            update={"report_sha256": self.report_document.report_sha256}
        )

    def report(self, run_id):
        self.report_calls += 1
        return super().report(run_id)


class OutOfOrderRemote(FakeLabClient):
    """Hold one old running observation until a newer completion is persisted."""

    def __init__(self):
        super().__init__()
        self.first_entered = threading.Event()
        self.release_first = threading.Event()
        self.counter_lock = threading.Lock()
        self.status_calls = 0
        self.armed = False
        self.report_document = report_for(self.run_id)

    def status(self, run_id):
        assert run_id == self.run_id
        if not self.armed:
            return super().status(run_id)
        with self.counter_lock:
            self.status_calls += 1
            first = self.status_calls == 1
        if first:
            self.first_entered.set()
            if not self.release_first.wait(5):
                raise RuntimeError("review stale response was not released")
        return LabRunStatus(
            run_id=run_id,
            origin="aos",
            state="running" if first else "completed",
            created_at="2026-09-24T00:00:00Z",
            updated_at="2026-09-24T00:01:00Z" if first else "2026-09-24T00:02:00Z",
            stop_requested=False,
            report_sha256=None if first else self.report_document.report_sha256,
        )


async def probe() -> dict:
    observations = {}
    db = _database()
    try:
        remote = CompletedRemote()
        coordinator = LabExternalJobCoordinator(
            db, FakeController(), remote, allowed_suites=frozenset({"synthetic.allowed.v1"})
        )
        job = await coordinator.start("session-a", _request())
        verification_count = db.execute("SELECT count(*) FROM verifications").fetchone()[0]
        terminal = job["state"] in {"completed", "stopped", "failed"}
        observations["recovered_terminal_ack"] = {
            "public_state": job["state"],
            "report_sha256": job["report_sha256"],
            "report_calls": remote.report_calls,
            "verification_count": verification_count,
            "passed": not terminal or (
                job["report_sha256"] == remote.report_document.report_sha256
                and verification_count == 1 and remote.report_calls > 0
            ),
        }
    finally:
        db.close()

    db = _database()
    remote = OutOfOrderRemote()
    try:
        coordinator = LabExternalJobCoordinator(
            db, FakeController(), remote, allowed_suites=frozenset({"synthetic.allowed.v1"})
        )
        job = await coordinator.start("session-a", _request())
        remote.armed = True
        stale = asyncio.create_task(coordinator.status("session-a", job["job_id"]))
        assert await asyncio.to_thread(remote.first_entered.wait, 2)
        newest = await coordinator.status("session-a", job["job_id"])
        assert newest["state"] == "completed" and newest["report_sha256"]
        remote.release_first.set()
        late = await stale
        row = db.execute(
            "SELECT state,report_sha256 FROM aos_external_jobs WHERE job_id=?", (job["job_id"],)
        ).fetchone()
        observations["late_running_response"] = {
            "verified_state_before_late_response": newest["state"],
            "returned_state_after_late_response": late["state"],
            "persisted_state": row["state"],
            "persisted_report_sha256": row["report_sha256"],
            "aos_run_status": db.execute("SELECT status FROM runs").fetchone()[0],
            "passed": row["state"] == "completed"
            and row["report_sha256"] == remote.report_document.report_sha256,
        }
    finally:
        remote.release_first.set()
        db.close()
    return observations


def main():
    paths = [COPY / "src/aos/lab_external.py", COPY / "tests/test_lab_external_jobs.py"]
    hashes = {str(p.relative_to(COPY)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    record = {
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": (
            "Isolated AOS prototype, real SQLite migrations and coordinator, fake controller "
            "and transport with deliberately reordered valid responses. No live AOS, actual "
            "Lab server/research, model or GPU execution."
        ),
        "source_sha256": hashes,
        "observations": asyncio.run(probe()),
    }
    record["source_unchanged"] = all(
        hashlib.sha256((COPY / name).read_bytes()).hexdigest() == value
        for name, value in hashes.items()
    )
    record["all_passed"] = record["source_unchanged"] and all(
        item["passed"] for item in record["observations"].values()
    )
    record["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    Path(__file__).with_name("aos-external-terminal-review.json").write_text(
        json.dumps(record, indent=2) + "\n"
    )
    print(json.dumps(record, indent=2))
    if not record["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
