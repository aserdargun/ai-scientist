"""Measure control calls while a specific owned Director process is running."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from review_scorer_queue import ROOT, engine
from sqlalchemy import text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--unit-id", type=UUID, required=True)
    parser.add_argument("--running-run", type=UUID, required=True)
    parser.add_argument("--stopped-run", type=UUID, required=True)
    args = parser.parse_args()
    token = (args.config_dir / "aos.token").read_text().strip()
    source = ROOT / "lab/api/app.py"
    initial_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    record = {"checked_at": datetime.now(UTC).isoformat(), "source_sha256": initial_hash,
              "scope": "Actual loopback status/health/repeated queued-job stop calls during an "
                       "owned live Director synthetic Docker/Scorer run. Does not stop the "
                       "active research, test a GPU model, or prove full in-flight cancellation.",
              "checks": {}, "calls": [], "units": {}}
    for purpose in ("api", "director"):
        unit = f"swapp-review-{purpose}-{args.unit_id.hex}.service"
        result = subprocess.run(["systemctl", "--user", "show", unit,
                                 "--property=ActiveState,MainPID,InvocationID"],
                                capture_output=True, text=True, timeout=5, check=True)
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines())
        record["units"][unit] = fields
        assert fields["ActiveState"] == "active" and int(fields["MainPID"]) > 1
    director = engine("director")
    try:
        with director.connect() as connection:
            row = connection.execute(text(
                "SELECT state,origin,owner_id,(SELECT count(*) FROM lab.dev_task_results "
                "WHERE run_id=:run) AS scores FROM lab.runs WHERE run_id=:run"
            ), {"run": args.running_run}).mappings().one()
            record["running_ledger"] = dict(row)
            assert row["state"] == "running" and row["origin"] == "aos"
            assert row["owner_id"] == "aos:lab-service" and row["scores"] > 0
        record["checks"]["actual_processes_and_measured_research_active"] = True
        for method, route in [
            ("GET", "/health"), ("GET", f"/v1/runs/{args.running_run}"),
            ("POST", f"/v1/runs/{args.stopped_run}/stop"),
            ("POST", f"/v1/runs/{args.stopped_run}/stop"),
            ("GET", f"/v1/runs/{args.running_run}"),
        ]:
            connection = http.client.HTTPConnection("127.0.0.1", args.port, timeout=3)
            started = time.monotonic()
            try:
                connection.request(method, route, body="{}" if method == "POST" else None,
                                   headers={"Authorization": f"Bearer {token}",
                                            "Content-Type": "application/json"})
                response = connection.getresponse()
                payload = json.loads(response.read(64 * 1024 + 1))
                elapsed = time.monotonic() - started
                assert response.status == 200 and elapsed < 3
                if method == "POST":
                    assert payload["run_id"] == str(args.stopped_run) and payload["stop_requested"]
                elif route != "/health":
                    assert payload["run_id"] == str(args.running_run) and payload["state"] == "running"
                record["calls"].append({"method": method, "path": route, "elapsed_s": elapsed,
                                        "status_code": response.status, "response": payload})
            finally:
                connection.close()
        record["checks"]["health_status_and_repeated_stop_responsive"] = True
        record["checks"]["different_research_run_remains_running"] = True
    finally:
        director.dispose()
    record["source_unchanged"] = hashlib.sha256(source.read_bytes()).hexdigest() == initial_hash
    record["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    record["all_passed"] = record["source_unchanged"] and all(record["checks"].values())
    Path(__file__).with_name("api-active-controls-review.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({"all_passed": record["all_passed"], "checks": record["checks"],
                      "max_control_seconds": max(call["elapsed_s"] for call in record["calls"])}))
    assert record["all_passed"]


if __name__ == "__main__":
    main()
