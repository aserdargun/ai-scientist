"""Check stop admission with the real API/PG lease and a counting provider boundary."""

from __future__ import annotations

import hashlib
import json
import secrets
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from review_scorer_queue import ROOT, engine
from sqlalchemy import delete, insert, select

from lab.api.app import create_app
from lab.db.schema import run_events, runs
from lab.director.fake_llm import AgentContext
from lab.director.journal import DirectorRunLease
from lab.director.runner import register_proposal_before_execution


class CountingProvider:
    """Never load a model: a forbidden invocation stops at this counting boundary."""

    def __init__(self):
        self.calls = 0

    def propose(self, context):
        self.calls += 1
        raise RuntimeError("review_provider_invoked_after_stop")


def main() -> None:
    paths = ("lab/api/app.py", "lab/director/runner.py", "lab/director/journal.py")
    hashes = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in paths
    }
    migrator, director = engine("migrator"), engine("director")
    run_id = uuid4()
    owner = f"stop-admission-review:{run_id}"
    token = secrets.token_urlsafe(32)
    checks = {}
    record = {
        "checked_at": datetime.now(UTC).isoformat(),
        "run_id": str(run_id),
        "scope": (
            "Real authenticated ASGI API and PostgreSQL Director lease on one owned run. "
            "Direct proposal-entry admission uses well-typed synthetic identities and a "
            "counting provider which raises before generating output. No baseline/calibration "
            "fixture, model, Docker, GPU, AOS or full Director run is executed."
        ),
        "source_sha256": hashes,
        "checks": checks,
    }
    try:
        with migrator.begin() as connection:
            connection.execute(
                insert(runs).values(
                    run_id=run_id,
                    origin="local",
                    owner_id=owner,
                    idempotency_key=f"stop-admission:{run_id}",
                    payload_sha256="a" * 64,
                    request_json={"review": "stop admission"},
                    state="running",
                )
            )
        app = create_app(director_engine=director, director_token=token, owner_id=owner)
        with tempfile.TemporaryDirectory(
            prefix="director-stop-admission-", dir=ROOT / "data/runtime"
        ) as temporary, TestClient(app) as client, DirectorRunLease(director, run_id) as lease:
            response = client.post(
                f"/v1/runs/{run_id}/stop", headers={"Authorization": f"Bearer {token}"}
            )
            checks["api_stop_committed"] = (
                response.status_code == 200
                and response.json()["state"] == "stop_requested"
                and response.json()["stop_requested"] is True
            )
            assert checks["api_stop_committed"]
            provider = CountingProvider()
            try:
                register_proposal_before_execution(
                    director,
                    run_id=run_id,
                    ordinal=1,
                    parent_experiment_id="exp_" + "b" * 32,
                    parent_tree_sha256="c" * 40,
                    suite_id="synthetic.stop-admission.v1",
                    suite_version=1,
                    calibration_sha256="d" * 64,
                    harness_sha256="e" * 64,
                    image_sha256="f" * 64,
                    system="S1",
                    context=AgentContext(
                        phase="proposal",
                        experiment_number=1,
                        task_cards=("Synthetic metadata; no series or labels.",),
                        champion_source="def build_candidate(): pass\n",
                        recent_feedback=(),
                    ),
                    provider=provider,
                    lease=lease,
                    artifact_root=Path(temporary),
                )
            except RuntimeError as error:
                record["proposal_error"] = str(error)
            else:
                record["proposal_error"] = None
            record["provider_calls_after_stop"] = provider.calls
            checks["provider_not_called_after_stop"] = provider.calls == 0
            try:
                lease.append_checkpoint(
                    sequence=0,
                    key="after-stop",
                    phase="proposal",
                    payload={"review": "must be denied before blob write"},
                    artifact_root=Path(temporary),
                )
            except RuntimeError as error:
                record["checkpoint_error"] = str(error)
                checks["checkpoint_denied_after_stop"] = True
            else:
                checks["checkpoint_denied_after_stop"] = False
            blob_files = sorted(
                str(path.relative_to(temporary))
                for path in Path(temporary).rglob("*.json")
            )
            record["new_blob_files_after_stop"] = blob_files
            checks["no_blob_written_after_stop"] = not blob_files
            with director.connect() as connection:
                events = connection.execute(
                    select(run_events.c.event_type).where(run_events.c.run_id == run_id)
                ).scalars().all()
            checks["only_stop_event_persisted"] = events == ["run.stop_requested"]
    finally:
        with migrator.begin() as connection:
            connection.execute(delete(runs).where(runs.c.run_id == run_id))
            remaining = connection.execute(
                select(runs.c.run_id).where(runs.c.run_id == run_id)
            ).first()
            record["owned_sql_fixture_removed"] = remaining is None
        migrator.dispose()
        director.dispose()
        record["source_unchanged"] = all(
            hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
            for name, digest in hashes.items()
        )
        record["all_passed"] = (
            len(checks) == 5 and all(checks.values())
            and record["source_unchanged"] and record["owned_sql_fixture_removed"]
        )
        record["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        Path(__file__).with_name("director-stop-admission-review.json").write_text(
            json.dumps(record, indent=2) + "\n"
        )
    print(json.dumps(record, indent=2))
    if not record["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
