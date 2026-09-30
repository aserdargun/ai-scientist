"""Actual PostgreSQL concurrency through the API, using isolated review run IDs."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sys
from threading import Barrier
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, func, select

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from lab.api.app import create_app
from lab.db.schema import runs, run_events


def main() -> None:
    files = [ROOT / "lab/api/app.py", ROOT / "lab/api/contracts.py", ROOT / "lab/db/schema.py"]
    source_hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    credential_root = ROOT / "data/runtime/postgres"
    director = create_engine((credential_root / "director.dsn").read_text().strip(),
                             pool_size=2, max_overflow=0)
    migrator = create_engine((credential_root / "migrator.dsn").read_text().strip(),
                             pool_size=1, max_overflow=0)
    probe_id = uuid4().hex
    owner = f"root-review:{probe_id}"
    key = f"root-review-key-{probe_id}"
    token = f"ephemeral-test-token-{uuid4().hex}"
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"idempotency_key": key, "track": "anomaly", "suite": "synthetic.smoke.v1",
               "budget": {"experiments": 2, "wall_seconds": 120, "model_tokens": 0},
               "program_version": "root-review-only"}
    evidence = {"checked_at": datetime.now(UTC).isoformat(),
                "scope": "Real PostgreSQL + actual FastAPI app via in-process ASGI; no HTTP listener or AOS/model claim.",
                "source_sha256": source_hashes, "checks": {}}
    checks = evidence["checks"]
    try:
        app_a = create_app(director_engine=director, director_token=token, owner_id=owner)
        app_b = create_app(director_engine=director, director_token=token, owner_id=owner)
        with TestClient(app_a) as client_a, TestClient(app_b) as client_b:
            checks["unauthenticated_rejected"] = client_a.post("/v1/runs", json=payload).status_code == 401
            barrier = Barrier(2)

            def start(client):
                barrier.wait(timeout=10)
                response = client.post("/v1/runs", headers=headers, json=payload)
                return {"http_status": response.status_code, "body": response.json()}

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(start, client) for client in (client_a, client_b)]
                starts = [future.result(timeout=20) for future in futures]
            evidence["concurrent_starts"] = starts
            checks["one_created_one_reused"] = sorted(r["http_status"] for r in starts) == [200, 202]
            run_ids = {r["body"].get("run_id") for r in starts}
            checks["same_run_id"] = len(run_ids) == 1 and None not in run_ids
            if not checks["one_created_one_reused"] or not checks["same_run_id"]:
                raise RuntimeError("concurrent start did not produce one durable run")
            run_id = starts[0]["body"]["run_id"]
            changed = {**payload, "suite": "different.suite"}
            checks["changed_payload_rejected"] = client_a.post("/v1/runs", headers=headers, json=changed).status_code == 409
            other_app = create_app(director_engine=director, director_token=token, owner_id=f"other:{probe_id}")
            with TestClient(other_app) as other:
                checks["foreign_owner_hidden"] = other.get(f"/v1/runs/{run_id}", headers=headers).status_code == 404
            barrier = Barrier(2)

            def stop(client):
                barrier.wait(timeout=10)
                response = client.post(f"/v1/runs/{run_id}/stop", headers=headers)
                return {"http_status": response.status_code, "body": response.json()}

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(stop, client) for client in (client_a, client_b)]
                stops = [future.result(timeout=20) for future in futures]
            evidence["concurrent_stops"] = stops
            checks["stops_idempotent"] = all(r["http_status"] == 200 and r["body"].get("state") == "stop_requested" for r in stops)
        restarted = create_app(director_engine=director, director_token=token, owner_id=owner)
        with TestClient(restarted) as client:
            response = client.post("/v1/runs", headers=headers, json=payload)
            evidence["reopened_app_start"] = {"http_status": response.status_code, "body": response.json()}
            checks["restart_does_not_restart_work"] = (
                response.status_code == 200 and response.json()["run_id"] == run_id
                and response.json()["state"] == "stop_requested" and response.json()["reused"]
            )
        with director.connect() as connection:
            count = connection.execute(select(func.count()).select_from(runs).where(runs.c.owner_id == owner)).scalar_one()
            events = connection.execute(select(run_events.c.event_type).join(runs).where(runs.c.owner_id == owner)).scalars().all()
        evidence["stored_run_count"] = count
        evidence["stored_events"] = events
        checks["one_durable_run"] = count == 1
        checks["one_accept_one_stop_event"] = sorted(events) == ["run.accepted", "run.stop_requested"]
    finally:
        with migrator.begin() as connection:
            deleted = connection.execute(delete(runs).where(runs.c.owner_id == owner, runs.c.idempotency_key == key)).rowcount
        evidence["cleanup_deleted_probe_runs"] = deleted
        director.dispose()
        migrator.dispose()
    checks["source_stable_during_probe"] = all(hashlib.sha256(p.read_bytes()).hexdigest() == source_hashes[str(p.relative_to(ROOT))] for p in files)
    evidence["all_passed"] = all(checks.values())
    evidence["command"] = ".venv/bin/python docs/ai-scientist/review-evidence/review_postgres_api_races.py"
    evidence["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    path = ROOT / "docs/ai-scientist/review-evidence/postgres-api-race-review.json"
    path.write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence, indent=2))
    if not evidence["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
