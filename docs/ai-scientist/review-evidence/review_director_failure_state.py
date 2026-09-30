"""Check dispatcher exception transitions against actual PostgreSQL state fences.

Registry/files/provider and Director execution are fixtures; no model, API server,
Docker, Scorer or worker process runs. Actual dispatcher claim, event writes and
terminal/stop-state conditions execute using the Director database role.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import delete, insert, text

from lab import cli
from lab.db.schema import runs
from lab.director.journal import canonical_bytes
from review_scorer_queue import ROOT, engine


def hashes():
    paths = [ROOT / "lab/cli.py", ROOT / "lab/db/schema.py", Path(__file__)]
    paths.extend((ROOT / "lab/db/migrations/versions").glob("*.py"))
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def main(output: Path):
    if output.exists():
        raise ValueError("refusing to overwrite evidence")
    director, migrator = engine("director"), engine("migrator")
    owned_runs = []
    record = {"schema": "director-failure-state-review.v1", "scope": __doc__,
              "checked_at": datetime.now(UTC).isoformat(), "source_sha256": hashes(),
              "checks": {}, "scenarios": []}

    def check(name, value):
        record["checks"][name] = bool(value)
        if not value:
            raise AssertionError(name)

    entry = SimpleNamespace(suite_id="synthetic.dispatch-error-fixture", track="ad",
        program_version="fixture.v1", provider="fake-json", suite_manifest_sha256="a" * 64,
        scenario_sha256="b" * 64, provider_config_sha256=None, proposal_limit=2)
    registry = SimpleNamespace(get=lambda key: entry, entry_sha256=lambda item: "c" * 64,
        verify_entry=lambda item: (ROOT / "data/runtime/fixture-suite-never-read.json",
                                  ROOT / "data/runtime/fixture-scenario-never-read.json"))
    try:
        for label, initial_state, expected_state in (
            ("claimed_exception", "queued", "failed"),
            ("concurrent_stop", "queued", "stop_requested"),
            ("concurrent_terminal", "queued", "failed"),
            ("open_experiment", "queued", "stop_requested"),
            ("normal_budget_return", "queued", "failed"),
            ("normal_pause_return", "queued", "failed"),
            ("normal_unverified_return", "queued", "failed"),
            ("normal_finalizer_incomplete", "queued", "failed"),
            ("invalid_registry_request", "queued", "queued"),
            ("unclaimed_running", "running", "running"),
        ):
            run_id = uuid4()
            owned_runs.append(run_id)
            request = {"suite": entry.suite_id, "track": entry.track,
                "program_version": entry.program_version, "provider": entry.provider,
                "proposal_limit": 2, "suite_manifest_sha256": entry.suite_manifest_sha256,
                "scenario_sha256": entry.scenario_sha256, "provider_config_sha256": None,
                "provider_registry_entry_sha256": "c" * 64,
                "budget": {"experiments": 2, "wall_seconds": 600, "model_tokens": 10000}}
            if label == "invalid_registry_request":
                request["provider_registry_entry_sha256"] = "d" * 64
            with migrator.begin() as connection:
                connection.execute(insert(runs).values(run_id=run_id, origin="local",
                    owner_id="dispatcher-failure-review", idempotency_key=f"dispatch-error-{run_id}",
                    payload_sha256=hashlib.sha256(canonical_bytes(request)).hexdigest(),
                    request_json=request, state=initial_state))
            called = []

            def fail_execution(args):
                called.append(args.run_id)
                if label.startswith("normal_"):
                    status = {"normal_budget_return": "budget_exhausted",
                              "normal_pause_return": "paused_candidate_crashes",
                              "normal_unverified_return": "explore_family_unverified",
                              "normal_finalizer_incomplete": "proposal_limit_reached"}[label]
                    return {"run_id": str(run_id), "loop": {"status": status,
                            "completed_proposals": 2 if status == "proposal_limit_reached" else 1},
                            "finalizer": {"exit_code": 1, "research_status": "pending"}
                            if label == "normal_finalizer_incomplete" else None}
                if label == "open_experiment":
                    from lab.director.ledger import register_experiment
                    register_experiment(director, experiment_id="exp_" + run_id.hex,
                        run_id=str(run_id), sequence=0, experiment_number=None,
                        kind="baseline", baseline_name="robust_z", parent_experiment_id=None,
                        candidate_sha256="e" * 64, candidate_blob_sha256="e" * 64,
                        inputs_sha256="f" * 64, move_type="detector", system="S1",
                        hypothesis="Unexecuted baseline fixture for SQL terminal fence",
                        predicted_delta=None, proposal={"fixture": "nonterminal baseline metadata only"})
                if label in {"concurrent_stop", "concurrent_terminal"}:
                    with director.begin() as connection:
                        connection.execute(text(
                            "UPDATE lab.runs SET state=:state, stop_requested=:stop WHERE run_id=:run"),
                            {"state": expected_state, "stop": label == "concurrent_stop", "run": run_id})
                raise RuntimeError("fixture worker failure; do not store arbitrary error content")

            observed = {"scenario": label, "run_id": str(run_id)}
            with patch.dict(os.environ, {"LAB_SUITE_REGISTRY_FILE": str(ROOT / "data/runtime/fixture-unused-registry.json")}), \
                 patch.object(cli, "load_suite_registry", return_value=registry), \
                 patch.object(cli, "load_fake_provider", return_value=[object(), object()]), \
                 patch.object(cli, "_director_engine", return_value=director), \
                 patch.object(cli, "_run_director", side_effect=fail_execution):
                try:
                    observed["returned"] = cli._dispatch_director_run(run_id)
                except Exception as error:
                    observed["error_type"] = type(error).__name__
            with director.connect() as connection:
                row = dict(connection.execute(text(
                    "SELECT state,stop_requested,report_sha256 FROM lab.runs WHERE run_id=:run"),
                    {"run": run_id}).mappings().one())
                events = [dict(item) for item in connection.execute(text(
                    "SELECT event_type,event_json FROM lab.run_events WHERE run_id=:run ORDER BY created_at,event_type"),
                    {"run": run_id}).mappings()]
            observed.update({"state": row, "events": events, "execution_calls": len(called)})
            check(label + "_correct_state", row["state"] == expected_state)
            check(label + "_no_fabricated_report", row["report_sha256"] is None)
            check(label + "_only_claimed_execution", len(called) == (
                0 if label in {"invalid_registry_request", "unclaimed_running"} else 1))
            check(label + "_untrusted_exception_text_not_recorded",
                  "do not store arbitrary error content" not in json.dumps(events))
            if label == "claimed_exception":
                check(label + "_failure_event", any(item["event_type"] == "run.failed" for item in events))
            if label == "concurrent_stop":
                check(label + "_stop_preserved", row["stop_requested"] is True
                      and not any(item["event_type"] == "run.failed" for item in events))
            if label == "open_experiment":
                check(label + "_recovery_request_recorded", row["stop_requested"] is True
                      and any(item["event_type"] == "run.dispatch_recovery_required" for item in events)
                      and not any(item["event_type"] == "run.failed" for item in events))
                with director.connect() as connection:
                    check(label + "_terminal_fence_preserved", connection.execute(text(
                        "SELECT status FROM lab.experiments WHERE experiment_id=:experiment"),
                        {"experiment": "exp_" + run_id.hex}).scalar_one() == "proposed")
            if label.startswith("normal_"):
                check(label + "_failed_without_success_claim", row["state"] == "failed"
                      and any(item["event_type"] == "run.failed" for item in events)
                      and not any(item["event_type"] == "run.completed" for item in events))
            if label in {"invalid_registry_request", "unclaimed_running"}:
                check(label + "_no_claim_event", not events)
            record["scenarios"].append(observed)
    except Exception as error:
        record["error"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        with migrator.begin() as connection:
            for run_id in owned_runs:
                connection.execute(delete(runs).where(runs.c.run_id == run_id))
                runtime = ROOT / "data/runtime/director-artifacts" / str(run_id)
                if runtime.is_dir():
                    runtime.rmdir()
        director.dispose()
        migrator.dispose()
        record["source_after_sha256"] = hashes()
        record["checks"]["sources_unchanged"] = record["source_sha256"] == record["source_after_sha256"]
        code = int(bool(record.get("error")) or len(record["scenarios"]) != 10
                   or not all(record["checks"].values()))
        record["overall_exit_code"] = code
        with output.open("x") as stream:
            json.dump(record, stream, indent=2)
            stream.write("\n")
        print(json.dumps({"output": str(output), "checks": record["checks"],
                          "error": record.get("error"), "overall_exit_code": code}))
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(main(parser.parse_args().output))
