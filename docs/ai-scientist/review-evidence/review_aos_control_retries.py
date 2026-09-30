"""Probe repeated stop and revoked-state start retries in the isolated AOS copy."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
COPY = ROOT / "data/runtime/aos-coexistence/source-typed"
sys.path[:0] = [str(COPY / "src"), str(COPY / "tests")]

from aos.contracts import Phase, State  # noqa: E402
from aos.lab_external import LabExternalJobCoordinator, LabRunHandle  # noqa: E402
from test_lab_external_jobs import FakeController, FakeLabClient, _database, _request  # noqa: E402


class LostStartReply(FakeLabClient):
    """An idempotent remote accepts the first start but its response is lost."""

    def start(self, request):
        self.start_calls.append(request)
        if len(self.start_calls) == 1:
            raise TimeoutError("synthetic lost start response")
        return LabRunHandle(run_id=self.run_id, state=self.state, reused=True)


def repeated_stop() -> dict:
    database, controller, remote = _database(), FakeController(), FakeLabClient()
    coordinator = LabExternalJobCoordinator(
        database, controller, remote, allowed_suites=frozenset({"synthetic.allowed.v1"})
    )
    result = {}
    try:
        job = asyncio.run(coordinator.start("session-a", _request()))
        first = asyncio.run(coordinator.stop("session-a", job["job_id"], "lease-current", 3))
        result["first_stop_state"] = first["state"]
        try:
            second = asyncio.run(coordinator.stop("session-a", job["job_id"], "lease-current", 3))
            result["second_stop_state"] = second["state"]
            result["passed"] = first["state"] == second["state"] == "stop_requested"
        except Exception as error:
            result["second_stop_error_type"] = type(error).__name__
            result["passed"] = False
    finally:
        database.close()
    return result


def revoked_retry() -> dict:
    database, controller, remote = _database(), FakeController(), LostStartReply()
    coordinator = LabExternalJobCoordinator(
        database, controller, remote, allowed_suites=frozenset({"synthetic.allowed.v1"})
    )
    result = {}
    try:
        try:
            asyncio.run(coordinator.start("session-a", _request()))
        except TimeoutError:
            pass
        assert len(remote.start_calls) == 1
        row = database.execute("SELECT state_json FROM runtime_states").fetchone()
        state = State.model_validate_json(row["state_json"])
        paused = state.model_copy(update={"phase": Phase.PAUSED, "owner": "PAUSED",
                                          "owner_lease_id": "revoked-review",
                                          "state_version": state.state_version + 1})
        # Simulate a persisted revocation independently of the fresh controller
        # lease. This is a boundary probe, not the actual takeover/rebind flow.
        with database:
            database.execute("UPDATE runtime_states SET state_json=?,state_version=? WHERE run_id=?",
                             (paused.model_dump_json(), paused.state_version, paused.run_id))
        controller.current.update({"lease_id": "lease-new", "generation": 4})
        denied = False
        try:
            asyncio.run(coordinator.start("session-a", _request(lease_id="lease-new", generation=4)))
        except Exception as error:
            denied = True
            result["retry_error_type"] = type(error).__name__
        result["start_calls_after_revocation"] = len(remote.start_calls)
        result["passed"] = denied and len(remote.start_calls) == 1
    finally:
        database.close()
    return result


def main() -> None:
    names = ["src/aos/lab_external.py", "src/aos/computer.py", "src/aos/contracts.py",
             "tests/test_lab_external_jobs.py"]
    hashes = {name: hashlib.sha256((COPY / name).read_bytes()).hexdigest() for name in names}
    record = {
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": "Actual typed AOS coordinator/policy and SQLite schema; fake controller/remote. "
                 "One synthetic lost response and a manually persisted typed-state revocation. "
                 "No live AOS, real HTTP, full restart, model or GPU acceptance.",
        "source_sha256": hashes,
    }
    for name, probe in [("repeated_stop", repeated_stop), ("revoked_start_retry", revoked_retry)]:
        try:
            record[name] = probe()
        except Exception as error:
            # Setup/admission failures do not prove the target retry behavior.
            # Retain them without turning an unfinished probe into a pass.
            record[name] = {"passed": False, "probe_completed": False,
                            "error_type": type(error).__name__, "error": str(error)}
    record["source_unchanged"] = all(hashlib.sha256((COPY / name).read_bytes()).hexdigest() == value
                                      for name, value in hashes.items())
    record["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    record["all_passed"] = (record["source_unchanged"] and record["repeated_stop"]["passed"]
                             and record["revoked_start_retry"]["passed"])
    name = "aos-control-retries-review.json" if record["all_passed"] else "aos-control-retries-before.json"
    Path(__file__).with_name(name).write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))
    if not record["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
