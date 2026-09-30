"""Probe stop responsiveness during a controlled slow metric on real PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sys
from threading import Event
import time
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, insert

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from harness.metrics import VUSResult
from lab.api.app import create_app
from lab.db.schema import dataset_labels, dataset_profiles, run_tasks, runs
import lab.scorer.service as scorer_module


def main() -> None:
    secret_root = ROOT / "data/runtime/postgres"
    engines = {
        role: create_engine((secret_root / f"{role}.dsn").read_text().strip(),
                            pool_size=1, max_overflow=0)
        for role in ("migrator", "director", "scorer")
    }
    run_id = uuid4()
    owner = f"root-stop-review:{run_id}"
    dataset_id = f"root-stop-review-{run_id}"
    now = datetime.now(UTC)
    candidate_hash = hashlib.sha256(b"review-only-candidate").hexdigest()
    source_hashes = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in ("lab/scorer/service.py", "lab/api/app.py", "lab/db/schema.py")
    }
    result = {
        "checked_at": now.isoformat(), "source_sha256": source_hashes,
        "scope": "Real PostgreSQL and actual API/scorer control flow; metric is a deliberately blocked fixture, not a VUS performance benchmark.",
        "api_response_target_seconds": 2,
    }
    entered = Event()
    release = Event()
    original_metric = scorer_module.vus_metrics

    def controlled_metric(*args, **kwargs):
        entered.set()
        if not release.wait(timeout=10):
            raise RuntimeError("review metric was not released")
        return VUSResult(vus_roc=0.75, vus_pr=0.65)

    try:
        with engines["migrator"].begin() as connection:
            connection.execute(insert(runs).values(
                run_id=run_id, origin="local", owner_id=owner,
                idempotency_key=owner, payload_sha256="a" * 64, request_json={},
                state="running", created_at=now, updated_at=now, stop_requested=False,
            ))
            connection.execute(insert(dataset_profiles).values(
                dataset_id=dataset_id, split_id="eval", session_id="fixture",
                sample_count=4, sliding_window=1, profile_sha256="b" * 64,
            ))
            connection.execute(insert(dataset_labels), [
                {"dataset_id": dataset_id, "split_id": "eval", "session_id": "fixture",
                 "sample_index": index, "is_anomaly": index >= 2}
                for index in range(4)
            ])
            connection.execute(insert(run_tasks).values(
                run_id=run_id, experiment_id="exp-01", evaluation_kind="primary",
                task_id="task-01", seed=0, candidate_sha256=candidate_hash,
                dataset_id=dataset_id, split_id="eval", session_id="fixture",
            ))
        scorer_module.vus_metrics = controlled_metric
        scorer = scorer_module.IndependentScorer(engines["scorer"], harness_sha256="c" * 64)
        artifact = json.dumps({"schema": "candidate-scores.v1", "sample_indices": [0, 1, 2, 3],
                               "scores": [0.1, 0.2, 0.8, 0.9]}).encode()
        app = create_app(director_engine=engines["director"], director_token="review-only-token", owner_id=owner)
        with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
            metric_future = pool.submit(
                scorer.score_task, run_id=run_id, experiment_id="exp-01", evaluation_kind="primary",
                task_id="task-01", seed=0, candidate_sha256=candidate_hash, candidate_output=artifact,
            )
            if not entered.wait(timeout=5):
                metric_future.result(timeout=1)
                raise RuntimeError("metric fixture was not reached")
            started = time.monotonic()
            stop_future = pool.submit(client.post, f"/v1/runs/{run_id}/stop",
                                      headers={"Authorization": "Bearer review-only-token"})
            try:
                response = stop_future.result(timeout=2.2)
                result["stop_returned_before_metric_release"] = True
            except TimeoutError:
                result["stop_returned_before_metric_release"] = False
            finally:
                release.set()
            response = stop_future.result(timeout=5)
            result["stop_elapsed_seconds"] = time.monotonic() - started
            result["stop_status"] = response.status_code
            result["stop_state"] = response.json().get("state")
            try:
                metric_future.result(timeout=5)
                result["late_score"] = "accepted"
            except ValueError as exc:
                result["late_score"] = {"rejected": type(exc).__name__, "reason": str(exc)}
        result["stop_blocked_by_metric_reproduced"] = (
            not result["stop_returned_before_metric_release"] and result["stop_elapsed_seconds"] > 2
        )
    finally:
        release.set()
        scorer_module.vus_metrics = original_metric
        with engines["migrator"].begin() as connection:
            result["cleanup_deleted_runs"] = connection.execute(delete(runs).where(runs.c.run_id == run_id, runs.c.owner_id == owner)).rowcount
            result["cleanup_deleted_profiles"] = connection.execute(delete(dataset_profiles).where(dataset_profiles.c.dataset_id == dataset_id)).rowcount
        for engine in engines.values():
            engine.dispose()
    result["source_stable_during_probe"] = all(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest for name, digest in source_hashes.items())
    result["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    result["command"] = ".venv/bin/python docs/ai-scientist/review-evidence/review_scorer_stop_lock.py"
    (ROOT / "docs/ai-scientist/review-evidence/scorer-stop-lock-review.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
