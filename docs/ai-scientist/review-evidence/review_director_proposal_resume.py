"""Verify proposal checkpoint recovery with a fault at the ledger boundary."""

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import delete, insert

from lab.db.schema import runs
from lab.director.contracts import CandidateProposal
from lab.director.fake_llm import AgentContext, ProposalTurn
from lab.director.journal import DirectorRunLease
from lab.director.runner import register_proposal_before_execution
from review_director_synthetic_scenario import RESIDUAL_VERBOSE
from review_scorer_queue import ROOT, engine


class Provider:
    """Count requests so a resumed boundary cannot silently ask twice."""

    def __init__(self):
        self.calls = 0

    def propose(self, context):
        self.calls += 1
        if self.calls != 1:
            raise AssertionError("provider called again after durable proposal")
        return ProposalTurn(
            proposal=CandidateProposal(hypothesis="Fit the training sensor relation.",
                                       move_type="features", candidate_source=RESIDUAL_VERBOSE,
                                       predicted_delta=0.1),
            messages=("Use the training relation to score causal row residuals.",),
            input_tokens=100, output_tokens=200,
        )


def main() -> None:
    paths = [ROOT / f"lab/director/{name}.py" for name in ("runner", "journal", "fake_llm", "artifacts")]
    hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    migrator, director = engine("migrator"), engine("director")
    run_id = uuid4()
    checks = {}
    record = {
        "checked_at": datetime.now(UTC).isoformat(), "run_id": str(run_id),
        "scope": "Actual PG checkpoint/session and physical blob recovery with one injected ledger-call failure; ledger registration is a spy, so this does not validate SQL calibration admission, candidate execution, process crash, Director20 or full resume.",
        "source_sha256": hashes, "checks": checks,
    }
    provider = Provider()
    captured = []

    def ledger_boundary(*args, **kwargs):
        captured.append(kwargs)
        if len(captured) == 1:
            raise RuntimeError("injected-ledger-boundary-failure")
        return {"status": "proposed"}

    try:
        with migrator.begin() as connection:
            connection.execute(insert(runs).values(
                run_id=run_id, origin="local", owner_id="proposal-resume-review",
                idempotency_key=f"proposal-resume-{run_id}", payload_sha256="a" * 64,
                request_json={"review": "proposal resume fault"}, state="running",
            ))
        context = AgentContext(phase="LOOP", experiment_number=1,
                               task_cards=("Synthetic EVT; train summaries only.",),
                               champion_source="def build_candidate(): pass\n", recent_feedback=())
        with tempfile.TemporaryDirectory(prefix="proposal-resume-", dir=ROOT / "data/runtime") as temporary:
            arguments = {
                "run_id": run_id, "ordinal": 1, "parent_experiment_id": "exp_" + "b" * 32,
                "parent_tree_sha256": "c" * 40, "suite_id": "synthetic.proposal-review.v1",
                "suite_version": 1, "calibration_sha256": "d" * 64,
                "harness_sha256": "e" * 64, "image_sha256": "f" * 64,
                "system": "S2", "context": context, "provider": provider,
                "artifact_root": Path(temporary),
            }
            with patch("lab.director.runner.register_experiment", ledger_boundary):
                with DirectorRunLease(director, run_id) as first:
                    try:
                        register_proposal_before_execution(director, lease=first, **arguments)
                    except RuntimeError as error:
                        checks["fault_at_ledger_boundary_observed"] = str(error) == "injected-ledger-boundary-failure"
                    checks["checkpoint_committed_before_ledger_fault"] = first.read_checkpoint(key="proposal:1", artifact_root=Path(temporary)) is not None
                with DirectorRunLease(director, run_id) as replacement:
                    resumed = register_proposal_before_execution(director, lease=replacement, **arguments)
                    repeated = register_proposal_before_execution(director, lease=replacement, **arguments)
                    checks["exact_repeat_has_same_identity"] = resumed == repeated
                    checks["provider_called_once"] = provider.calls == 1
                    checks["all_ledger_attempts_same_immutable_registration"] = len(captured) == 3 and captured[0] == captured[1] == captured[2]
                    checks["physical_candidate_hash_matches"] = resumed.candidate_sha256 == hashlib.sha256(RESIDUAL_VERBOSE.encode()).hexdigest()
                    changed = context.model_copy(update={"champion_source": "changed champion"})
                    try:
                        register_proposal_before_execution(director, lease=replacement, **{**arguments, "context": changed})
                    except RuntimeError as error:
                        checks["changed_context_rejected_before_ledger"] = "context changed" in str(error) and len(captured) == 3
                    else:
                        checks["changed_context_rejected_before_ledger"] = False
    finally:
        with migrator.begin() as connection:
            connection.execute(delete(runs).where(runs.c.run_id == run_id))
        migrator.dispose()
        director.dispose()
    record["source_unchanged"] = all(hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest for path, digest in hashes.items())
    record["all_passed"] = len(checks) == 7 and all(checks.values()) and record["source_unchanged"]
    record["command"] = ".venv/bin/python docs/ai-scientist/review-evidence/review_director_proposal_resume.py"
    record["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    Path(__file__).with_name("director-proposal-resume-review.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))
    if not record["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
