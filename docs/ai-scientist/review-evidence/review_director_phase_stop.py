"""Prove an API stop between real Docker fit/score phases blocks the next phase."""

from __future__ import annotations

import hashlib
import json
import secrets
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from review_director_synthetic_scenario import RESIDUAL_SIMPLE, fixture_tasks
from review_scorer_queue import ROOT, engine
from sqlalchemy import delete, insert

from harness.contracts import FitContext
from harness.fingerprint import compute_harness_hash
from lab.api.app import create_app
from lab.db.schema import runs
from lab.director.journal import DirectorRunLease
from lab.sandbox.docker_runner import DEFAULT_SANDBOX_IMAGE, LocalDockerRunner
from lab.sandbox.evaluation import run_guarded_seed_evaluation


class ObservedRunner(LocalDockerRunner):
    """Record phase entry while preserving the actual Docker runner implementation."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.started = []
        self.finished = []

    def run_phase(self, **kwargs):
        self.started.append(kwargs["phase"])
        result = super().run_phase(**kwargs)
        self.finished.append(kwargs["phase"])
        return result


def main():
    run_id = uuid4()
    owner = f"phase-stop-review:{run_id}"
    token = secrets.token_urlsafe(32)
    migrator, director = engine("migrator"), engine("director")
    fingerprint = compute_harness_hash(ROOT).sha256
    record = {
        "checked_at": datetime.now(UTC).isoformat(),
        "run_id": str(run_id),
        "scope": (
            "One actual Docker fit on a tiny synthetic task. The admission callback commits "
            "an authenticated ASGI API stop in real PostgreSQL before the score phase. "
            "This tests the guarded evaluator's phase boundary; not in-flight cancellation, "
            "Director end-to-end, Scorer, real data, model/GPU or AOS acceptance."
        ),
        "harness_sha256": fingerprint,
        "image": DEFAULT_SANDBOX_IMAGE,
    }
    checks = {}
    try:
        with migrator.begin() as connection:
            connection.execute(insert(runs).values(
                run_id=run_id, origin="local", owner_id=owner,
                idempotency_key=str(run_id), payload_sha256="a" * 64,
                request_json={"review": "stop between fit and score"}, state="running",
            ))
        app = create_app(director_engine=director, director_token=token, owner_id=owner)
        with tempfile.TemporaryDirectory(prefix="phase-stop-", dir=ROOT / "data/runtime") as tmp:
            runner = ObservedRunner(image=DEFAULT_SANDBOX_IMAGE, work_root=Path(tmp))
            with TestClient(app) as client, DirectorRunLease(director, run_id) as lease:
                invocations = 0

                def admit():
                    nonlocal invocations
                    invocations += 1
                    if invocations == 2:
                        result = client.post(f"/v1/runs/{run_id}/stop", headers={
                            "Authorization": f"Bearer {token}"
                        })
                        checks["api_stop_committed_between_phases"] = (
                            result.status_code == 200 and result.json()["state"] == "stop_requested"
                        )
                    lease.require_run_active()

                train, evaluation, _labels = fixture_tasks()[0]
                try:
                    run_guarded_seed_evaluation(
                        runner, candidate_source=RESIDUAL_SIMPLE.encode(), train=train,
                        evaluation=evaluation,
                        context=FitContext(0, tuple(train.columns), (), 60, 90.0),
                        task_ids=frozenset({"phase-stop-task"}),
                        evaluation_instants=frozenset(), remaining_seconds=90,
                        admission_check=admit,
                    )
                except RuntimeError as exc:
                    record["guarded_evaluation_error"] = str(exc)
                    checks["stopped_run_denied"] = str(exc) == "director_run_is_not_active"
                else:
                    checks["stopped_run_denied"] = False
                checks["only_fit_started_and_finished"] = (
                    runner.started == runner.finished == ["fit"]
                )
                record["admission_checks"] = invocations
                record["started_phases"] = runner.started
                record["finished_phases"] = runner.finished
    finally:
        with migrator.begin() as connection:
            connection.execute(delete(runs).where(runs.c.run_id == run_id))
        migrator.dispose()
        director.dispose()
    record["checks"] = checks
    record["source_unchanged"] = compute_harness_hash(ROOT).sha256 == fingerprint
    record["all_passed"] = len(checks) == 3 and all(checks.values()) and record["source_unchanged"]
    record["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    Path(__file__).with_name("director-phase-stop-review.json").write_text(
        json.dumps(record, indent=2) + "\n"
    )
    print(json.dumps(record, indent=2))
    if not record["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
