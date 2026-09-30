"""Inject loss at real PostgreSQL model-attempt checkpoints and open a new lease.

The model runtime and token counts are fixtures. The actual provider, registration
boundary, PostgreSQL leases/checkpoints and hash-addressed files are exercised.
This is not a GPU, process-kill, Scorer or full-run restart acceptance.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import runpy
import tempfile
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import delete, insert, select

from lab.db.schema import runs
from lab.director.artifacts import read_director_artifact
from lab.director.journal import DirectorRunLease
from lab.director.local_llm import LocalQwenProposalProvider, ProviderOutputError
from lab.director.runner import register_proposal_before_execution
from review_scorer_queue import ROOT, engine


class InjectedLoss(BaseException):
    """Leave ordinary Exception recovery untouched to model a lost worker."""


class LossAfterCheckpoint:
    def __init__(self, lease, key):
        self.lease, self.key, self.triggered = lease, key, False

    def __getattr__(self, name):
        return getattr(self.lease, name)

    def append_checkpoint(self, **kwargs):
        result = self.lease.append_checkpoint(**kwargs)
        if kwargs["key"] == self.key:
            self.triggered = True
            raise InjectedLoss(self.key)
        return result


def hashes():
    paths = [ROOT / f"lab/director/{name}.py" for name in
             ("local_llm", "runner", "journal", "fake_llm", "artifacts")]
    paths.extend((ROOT / "tests/test_local_qwen_provider.py", Path(__file__)))
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths}


def main(output: Path) -> int:
    if output.exists():
        raise ValueError("refusing to overwrite evidence")
    fixture = runpy.run_path(str(ROOT / "tests/test_local_qwen_provider.py"))
    fixture_runtime = fixture["_FakeRuntime"]

    class RecordedRuntime(fixture_runtime):
        requests = []

        def run_turn(self, owner, request_id, messages, **kwargs):
            type(self).requests.append(request_id)
            return super().run_turn(owner, request_id, messages, **kwargs)

    migrator, director = engine("migrator"), engine("director")
    run_id = uuid4()
    record = {"schema": "local-qwen-attempt-recovery-review.v1",
              "checked_at": datetime.now(UTC).isoformat(), "run_id": str(run_id),
              "scope": __doc__, "source_sha256": hashes(), "checks": {}, "scenarios": []}

    def check(name, value):
        record["checks"][name] = bool(value)
        if not value:
            raise AssertionError(name)

    try:
        with migrator.begin() as connection:
            connection.execute(insert(runs).values(
                run_id=run_id, origin="local", owner_id="qwen-attempt-recovery-review",
                idempotency_key=f"qwen-attempt-recovery-{run_id}",
                payload_sha256=hashlib.sha256(run_id.bytes).hexdigest(),
                request_json={"review": "fixture model attempt recovery"}, state="running"))
        with tempfile.TemporaryDirectory(prefix="qwen-attempt-recovery-", dir=ROOT / "data/runtime") as tmp:
            artifacts = Path(tmp)
            for ordinal, (label, index, boundary, expected_before, expected_after) in enumerate((
                ("completed_primary", 0, "completed", 1, 2),
                ("uncertain_primary", 0, "started", 0, 0),
                ("uncertain_repair", 1, "started", 1, 1),
            ), start=1):
                RecordedRuntime.calls, RecordedRuntime.requests, RecordedRuntime.pins = [], [], []
                RecordedRuntime.request_schemas = []
                RecordedRuntime.truncate_first, RecordedRuntime.fail_systems = False, set()
                valid = fixture["_proposal_json"]("features")
                invalid = "```json\n" + valid + "\n```"
                RecordedRuntime.response_queue = [invalid, valid]
                RecordedRuntime.response_text = valid
                context = fixture["_context"](system="S2", move_type="features").model_copy(
                    update={"experiment_number": ordinal})
                ledger_calls = []

                def ledger_spy(*args, **kwargs):
                    ledger_calls.append(kwargs)
                    return {"status": "proposed"}

                def provider():
                    return LocalQwenProposalProvider(
                        run_id=run_id, owner="lab", principal_resolver=object(),
                        runtime_database=artifacts / "unused-fixture.sqlite3",
                        registry_entry_sha256="c" * 64)

                arguments = dict(run_id=run_id, ordinal=ordinal,
                    parent_experiment_id="exp_" + "b" * 32, parent_tree_sha256="c" * 40,
                    suite_id="synthetic.qwen-attempt-recovery.v1", suite_version=1,
                    calibration_sha256="d" * 64, harness_sha256="e" * 64, image_sha256="f" * 64,
                    system="S2", context=context, artifact_root=artifacts,
                    remaining_wall_seconds=720.0)
                target = f"provider-attempt:{ordinal}:{index}:{boundary}"
                result = {"label": label, "fault_key": target}
                with patch("lab.director.local_llm.OwnedVllmRuntime", RecordedRuntime), \
                     patch.object(LocalQwenProposalProvider, "_count_prompt_variants",
                                  lambda self, variants, profile, **kwargs: tuple(100 for _ in variants)), \
                     patch("lab.director.runner.register_experiment", ledger_spy):
                    with DirectorRunLease(director, run_id) as first:
                        faulty = LossAfterCheckpoint(first, target)
                        try:
                            register_proposal_before_execution(
                                director, lease=faulty, provider=provider(), **arguments)
                        except InjectedLoss:
                            pass
                        check(label + "_fault_reached", faulty.triggered)
                        check(label + "_calls_before_loss", len(RecordedRuntime.requests) == expected_before)
                        check(label + "_not_registered_before_loss", not ledger_calls)
                    with DirectorRunLease(director, run_id) as replacement:
                        persisted = replacement.read_checkpoint(key=target, artifact_root=artifacts)
                        check(label + "_checkpoint_survives_new_session", persisted is not None)
                        result["checkpoint"] = persisted
                        if label == "completed_primary":
                            for attribute in ("configuration_sha256", "registry_entry_sha256"):
                                changed = provider()
                                setattr(changed, attribute, "8" * 64)
                                try:
                                    register_proposal_before_execution(
                                        director, lease=replacement, provider=changed, **arguments)
                                except RuntimeError as error:
                                    check(label + "_changed_" + attribute + "_rejected",
                                          "identity differs" in str(error)
                                          and len(RecordedRuntime.requests) == expected_before)
                                else:
                                    raise AssertionError("changed provider identity was accepted")
                            try:
                                register_proposal_before_execution(
                                    director, lease=replacement, provider=provider(),
                                    **{**arguments, "remaining_wall_seconds": 1.0})
                            except ProviderOutputError as error:
                                check(label + "_cached_usage_survives_short_remaining_time",
                                      error.provider_receipt.input_tokens == 100
                                      and error.provider_receipt.output_tokens == 50
                                      and len(RecordedRuntime.requests) == expected_before)
                            else:
                                raise AssertionError("repair cannot activate in one remaining second")
                            resumed = register_proposal_before_execution(
                                director, lease=replacement, provider=provider(), **arguments)
                            transcript = json.loads(read_director_artifact(
                                resumed.messages_blob_sha256, artifact_root=artifacts))
                            check(label + "_both_raw_finals_preserved",
                                  [item["response_text"] for item in transcript["provider_attempts"]]
                                  == [invalid, valid])
                            check(label + "_usage_preserved", resumed.input_tokens == 200
                                  and resumed.output_tokens == 100)
                            repeated = register_proposal_before_execution(
                                director, lease=replacement, provider=provider(), **arguments)
                            check(label + "_idempotent_registration", resumed == repeated)
                            result["receipt"] = resumed.provider_receipt.model_dump(mode="json")
                        else:
                            try:
                                register_proposal_before_execution(
                                    director, lease=replacement, provider=provider(), **arguments)
                            except ProviderOutputError as error:
                                result["error_type"] = type(error).__name__
                                result["receipt"] = error.provider_receipt.model_dump(mode="json")
                            else:
                                raise AssertionError(label + "_must_not_repeat_uncertain_request")
                            check(label + "_no_fabricated_candidate", not ledger_calls)
                            if label == "uncertain_repair":
                                check(label + "_prior_usage_retained",
                                      result["receipt"]["input_tokens"] >= 100
                                      and result["receipt"]["output_tokens"] >= 50)
                    check(label + "_no_duplicate_model_call", len(RecordedRuntime.requests) == expected_after)
                    check(label + "_unique_request_ids", len(set(RecordedRuntime.requests))
                          == len(RecordedRuntime.requests))
                    result["request_ids"] = RecordedRuntime.requests.copy()
                    record["scenarios"].append(result)
    except Exception as error:
        record["error"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        with migrator.begin() as connection:
            connection.execute(delete(runs).where(runs.c.run_id == run_id))
            check("only_owned_run_removed", connection.execute(
                select(runs.c.run_id).where(runs.c.run_id == run_id)).first() is None)
        migrator.dispose()
        director.dispose()
        record["source_after_sha256"] = hashes()
        record["checks"]["source_unchanged"] = record["source_sha256"] == record["source_after_sha256"]
        code = int(bool(record.get("error")) or len(record["scenarios"]) != 3
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
