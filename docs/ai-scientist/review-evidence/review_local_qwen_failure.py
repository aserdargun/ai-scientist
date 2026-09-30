"""Diagnose a failed real model run without resuming or changing its ledger."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import text

from review_director20 import verify_scored_decisions
from review_local_qwen_wire import source_hashes
from review_scorer_queue import ROOT, engine
from lab.director.artifacts import read_director_artifact, read_registered_calibration
from lab.director.contracts import CandidateProposal, ExperimentDocument, TrajectoryDocument
from lab.director.fake_llm import ProviderReceipt


def diagnose(wire_path: Path, output: Path) -> None:
    wire = json.loads(wire_path.read_bytes())
    run_id = UUID(wire["run_id"])
    artifacts = ROOT / "data/runtime/director-artifacts" / str(run_id)
    before = source_hashes()
    record = {
        "schema": "local-qwen-failure-diagnosis.v1",
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": "Read-only diagnosis and verification of completed proposals in a failed run; "
                 "not six-proposal, full report, S2 proposal or AOS acceptance.",
        "run_id": str(run_id), "wire_sha256": hashlib.sha256(wire_path.read_bytes()).hexdigest(),
        "source_sha256": before, "checks": {},
    }

    def check(name: str, condition: bool) -> None:
        record["checks"][name] = bool(condition)
        if not condition:
            raise AssertionError(name)

    def read_blob(digest: str) -> bytes:
        raw = read_director_artifact(digest, artifact_root=artifacts)
        if hashlib.sha256(raw).hexdigest() != digest:
            raise AssertionError("physical blob digest mismatch")
        return raw

    director = engine("director")
    try:
        check("wire_records_real_failure_and_clean_shutdown", wire["director_exit_code"] == 1
              and wire["all_passed"] is False and wire["execution_observed"] is True
              and wire["checks"]["owned_model_cgroups_drained"]
              and wire["checks"]["owned_gpu_contexts_drained"]
              and "cleanup_error_type" not in wire)
        check("current_sources_match_execution", before == wire["source_sha256"])
        with director.connect() as connection:
            run = connection.execute(text("SELECT * FROM lab.runs WHERE run_id=:run"),
                                     {"run": run_id}).mappings().one()
            record["run_state_after_worker_exit"] = run["state"]
            check("actual_local_provider", run["origin"] == "local"
                  and run["request_json"]["provider"] == "local-qwen")
            documents = []
            for row in connection.execute(text(
                "SELECT * FROM lab.experiments WHERE run_id=:run ORDER BY sequence"
            ), {"run": run_id}).mappings():
                receipt = connection.execute(text("SELECT lab.experiment_record_receipt(:id)"),
                                             {"id": row["experiment_id"]}).scalar_one()
                pair = {}
                for name, model in (("experiment", ExperimentDocument), ("trajectory", TrajectoryDocument)):
                    raw = read_blob(receipt[f"{name}_blob_sha256"])
                    check(f"{row['experiment_id']}_{name}_hash", hashlib.sha256(raw).hexdigest()
                          == receipt[f"{name}_sha256"])
                    document = model.model_validate_json(raw, strict=True)
                    check(f"{row['experiment_id']}_{name}_identity", document.run_id == run_id
                          and document.experiment_id == row["experiment_id"])
                    pair[name] = document.model_dump(mode="json", by_alias=True)
                proposal = pair["experiment"]
                check(f"{row['experiment_id']}_candidate_hash", hashlib.sha256(
                    read_blob(proposal["candidate_blob_sha256"])).hexdigest() == proposal["candidate_sha256"])
                documents.append(pair)
            scores = connection.execute(text(
                "SELECT * FROM lab.dev_task_results WHERE run_id=:run "
                "ORDER BY experiment_id,evaluation_kind,seed,task_id"
            ), {"run": run_id}).mappings().all()
            checkpoints = connection.execute(text(
                "SELECT event_json FROM lab.run_events WHERE run_id=:run "
                "AND event_type='director.checkpoint' ORDER BY (event_json->>'sequence')::integer"
            ), {"run": run_id}).scalars().all()
        failures = []
        for item in checkpoints:
            if item["phase"] != "proposal_budget_reconciled":
                continue
            payload = json.loads(read_blob(item["blob_sha256"]))
            if payload.get("status", "").startswith("provider_error"):
                failures.append(payload)
        check("one_durable_provider_failure", len(failures) == 1)
        failure = failures[0]
        receipt = ProviderReceipt.model_validate_json(json.dumps(failure["provider_receipt"]), strict=True)
        raw = read_blob(failure["response_blob_sha256"]).decode("utf-8")
        rejected = False
        try:
            CandidateProposal.model_validate_json(raw, strict=True)
        except ValidationError:
            rejected = True
        check("actual_response_rejected_by_strict_schema", rejected)
        check("failure_is_final_content_markdown_fence", raw.startswith("```json\n")
              and raw.rstrip().endswith("```"))
        # Diagnostic only. This stripped content is never submitted or accepted.
        inner = raw.removeprefix("```json\n").rstrip().removesuffix("```").rstrip()
        parsed = CandidateProposal.model_validate_json(inner, strict=True)
        check("fence_removed_only_for_diagnosis_is_typed_proposal", parsed.move_type == "features")
        check("completed_S2_runtime_has_measured_usage", receipt.actual_system == "S2"
              and receipt.enable_thinking and len(receipt.attempts) == 1
              and receipt.attempts[0].outcome == "completed"
              and receipt.attempts[0].preflight_prompt_tokens == receipt.input_tokens
              and receipt.input_tokens + receipt.output_tokens == failure["charged_model_tokens"]
              and not failure["usage_incomplete"])
        calibration = read_registered_calibration(director, run_id=run_id, artifact_root=artifacts)
        trace = verify_scored_decisions(documents, scores, calibration, artifacts)
        check("two_S1_decisions_reconstructed", len(trace) == 2
              and all(p["experiment"]["system"] == "S1" for p in documents
                      if p["experiment"]["kind"] == "proposal"))
        check("44_independent_scorer_results", len(scores) == 44)
        record.update({"documents": documents, "decision_trace": trace, "score_count": len(scores),
                       "failure_checkpoint": failure, "failure_response_bytes": len(raw.encode()),
                       "failure_response_format": "markdown-fenced-json", "sources_unchanged": source_hashes() == before,
                       "all_diagnostic_checks_passed": all(record["checks"].values())})
    finally:
        director.dispose()
    check("diagnosis_sources_unchanged", record["sources_unchanged"])
    with output.open("x") as stream:
        json.dump(record, stream, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps({"diagnostic_checks": len(record["checks"]),
                      "all_diagnostic_checks_passed": record["all_diagnostic_checks_passed"],
                      "score_count": record["score_count"], "run_state": record["run_state_after_worker_exit"],
                      "decisions": [{"verdict": t["verdict"], "delta_hex": t["delta_hex"],
                                     "ci_low_hex": t["ci_low_hex"]} for t in trace]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wire", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    diagnose(args.wire, args.output)
