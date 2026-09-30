"""Check malformed-episode continuation and crash recovery with actual PG journals.

Model/tokenizer, calibration and suite identity are fixtures. Production provider,
proposal budget reconciliation, Director loop/state recovery and PostgreSQL run
events/blob storage execute. No Docker, Scorer, GPU or public-data acceptance.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import runpy
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import delete, insert, text

from lab.db.schema import runs
from lab.director.artifacts import store_director_artifact
from lab.director.budget import RunBudget
from lab.director.journal import DirectorRunLease
from lab.director.local_llm import LocalQwenProposalProvider
from lab.director.loop import DirectorLoop, DirectorLoopState, _budget_to_json
from review_local_qwen_attempt_recovery import InjectedLoss, LossAfterCheckpoint
from review_scorer_queue import ROOT, engine


class ReachedNextOrdinal(BaseException):
    pass


def hashes():
    files = [ROOT / f"lab/director/{name}.py" for name in
             ("local_llm", "runner", "loop", "budget", "journal", "fake_llm")]
    files.extend((Path(__file__), ROOT / "tests/test_local_qwen_provider.py",
                  Path(__file__).with_name("review_local_qwen_attempt_recovery.py")))
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}


def main(output: Path):
    if output.exists():
        raise ValueError("refusing to overwrite evidence")
    fixture = runpy.run_path(str(ROOT / "tests/test_local_qwen_provider.py"))
    runtime = fixture["_FakeRuntime"]
    migrator, director = engine("migrator"), engine("director")
    owned_runs = []
    record = {"schema": "local-qwen-abandonment-review.v1", "scope": __doc__,
              "checked_at": datetime.now(UTC).isoformat(), "source_sha256": hashes(),
              "checks": {}, "scenarios": []}

    def check(name, value):
        record["checks"][name] = bool(value)
        if not value:
            raise AssertionError(name)

    try:
        with tempfile.TemporaryDirectory(prefix="qwen-abandonment-", dir=ROOT / "data/runtime") as tmp:
            for scenario in ("normal", "after_budget_receipt", "after_abandonment_receipt"):
                run_id = uuid4()
                owned_runs.append(run_id)
                artifacts = Path(tmp) / str(run_id)
                artifacts.mkdir(mode=0o700)
                with migrator.begin() as connection:
                    connection.execute(insert(runs).values(
                        run_id=run_id, origin="local", owner_id="qwen-abandonment-review",
                        idempotency_key=f"qwen-abandonment-{run_id}",
                        payload_sha256=hashlib.sha256(run_id.bytes).hexdigest(),
                        request_json={"review": "fixture malformed episode"}, state="running"))
                source = b"class Champion: pass\n"
                source_sha = store_director_artifact(source, artifact_root=artifacts)
                runtime.calls, runtime.pins, runtime.request_schemas = [], [], []
                runtime.truncate_first, runtime.fail_systems = False, set()
                raw = "```json\n" + fixture["_proposal_json"]() + "\n```"
                runtime.response_queue = []
                runtime.response_text = raw
                observed = {"scenario": scenario, "run_id": str(run_id)}
                reached = []

                def initial_state(loop, calibration):
                    return DirectorLoopState.model_validate({
                        "schema": "director-loop-state.v1", "run_id": run_id,
                        "suite_manifest_sha256": "a" * 64, "suite_id": "synthetic.fixture",
                        "suite_version": 1, "calibration_sha256": "b" * 64,
                        "harness_sha256": "c" * 64, "image_sha256": "d" * 64,
                        "proposal_limit": 2, "completed_proposals": 0, "next_ordinal": 1,
                        "champion_experiment_id": "exp_" + "e" * 32,
                        "champion_source_sha256": source_sha,
                        "champion_tree_sha256": "f" * 40,
                        "champion_source_blob_sha256": source_sha,
                        "champion_seed0_by_task": {"fixture": .5},
                        "champion_seed1_by_task": {"fixture": .5},
                        "champion_suite_seed_scores": (.5, .5, .5), "champion_noise_sd": 0.0,
                        "best_suite": .5, "consecutive_non_keep": 0, "explore_proposals": 0,
                        "explore_family": None, "consecutive_candidate_crashes": 0,
                        "discard_streak": 0, "previous_move_type": None, "recent_feedback": (),
                        "budget": _budget_to_json(loop.budget.snapshot()),
                    }, strict=True)

                original_run_one = DirectorLoop._run_one

                def stop_at_next(loop, **kwargs):
                    if kwargs["ordinal"] == 2:
                        reached.append({"state": kwargs["state"].model_dump(mode="json"),
                                        "budget": _budget_to_json(loop.budget.snapshot())})
                        raise ReachedNextOrdinal()
                    return original_run_one(loop, **kwargs)

                def new_loop(lease):
                    return DirectorLoop(director, director, lease=lease,
                        runner=SimpleNamespace(image="fixture"), run_id=run_id, tasks=(),
                        suite_manifest_sha256="a" * 64,
                        provider=LocalQwenProposalProvider(run_id=run_id, owner="lab",
                            principal_resolver=object(), runtime_database=artifacts / "unused.sqlite3",
                            registry_entry_sha256="c" * 64),
                        budget=RunBudget(proposal_limit=2, wall_limit=2000, token_limit=100000),
                        artifact_root=artifacts, proposal_limit=2, seed_wall_seconds=1)

                fault_key = {
                    "normal": None,
                    "after_budget_receipt": f"proposal-budget-reconciled:{run_id}:1",
                    "after_abandonment_receipt": "proposal-abandoned:1",
                }[scenario]
                with ExitStack() as stack:
                    stack.enter_context(patch("lab.director.local_llm.OwnedVllmRuntime", runtime))
                    stack.enter_context(patch.object(LocalQwenProposalProvider, "_count_prompt_variants",
                        lambda self, variants, profile, **kwargs: tuple(100 for _ in variants)))
                    stack.enter_context(patch("lab.director.loop.read_registered_calibration", return_value=object()))
                    stack.enter_context(patch("lab.director.loop.calibration_sha256", return_value="b" * 64))
                    stack.enter_context(patch("lab.director.runner.calibration_sha256", return_value="b" * 64))
                    stack.enter_context(patch.object(DirectorLoop, "_verify_execution_identity", lambda *args: None))
                    stack.enter_context(patch.object(DirectorLoop, "_initial_state", initial_state))
                    stack.enter_context(patch.object(DirectorLoop, "_task_cards",
                        lambda *args: ("task=fixture; measured_dev_vus=0.5; labels withheld",)))
                    stack.enter_context(patch.object(DirectorLoop, "_run_one", stop_at_next))
                    with DirectorRunLease(director, run_id) as first:
                        wrapped = LossAfterCheckpoint(first, fault_key)
                        try:
                            returned = new_loop(wrapped).run()
                        except ReachedNextOrdinal:
                            check(scenario + "_next_ordinal_before_restart", fault_key is None)
                        except InjectedLoss:
                            check(scenario + "_fault_reached", wrapped.triggered)
                        else:
                            raise AssertionError(f"loop returned {returned.status} instead of continuing")
                    if fault_key is not None:
                        check(scenario + "_one_primary_one_repair_before_loss", len(runtime.calls) == 2)
                        with DirectorRunLease(director, run_id) as replacement:
                            try:
                                returned = new_loop(replacement).run()
                            except ReachedNextOrdinal:
                                pass
                            else:
                                raise AssertionError(f"resumed loop returned {returned.status}")
                    check(scenario + "_continued_to_second_ordinal", len(reached) == 1)
                    check(scenario + "_no_duplicate_model_call", len(runtime.calls) == 2)
                    state, budget = reached[0]["state"], reached[0]["budget"]
                    check(scenario + "_abandoned_episode_counted_once",
                          state["completed_proposals"] == budget["proposal_count"] == 1
                          and state["next_ordinal"] == 2)
                    check(scenario + "_usage_charged_once", budget["model_tokens"] == 300
                          and budget["reserved_model_tokens"] == 0
                          and budget["reserved_wall_seconds"] == 0)
                    check(scenario + "_champion_unchanged", state["champion_source_sha256"] == source_sha
                          and state["best_suite"] == .5)
                    with DirectorRunLease(director, run_id) as inspection:
                        abandon = inspection.read_checkpoint(key="proposal-abandoned:1", artifact_root=artifacts)
                        check(scenario + "_typed_reason_retained", abandon["payload"]["reason"] == "tool_parse")
                        check(scenario + "_no_valid_proposal_checkpoint", inspection.read_checkpoint(
                            key="proposal:1", artifact_root=artifacts) is None)
                    with director.connect() as connection:
                        check(scenario + "_no_fabricated_experiment", connection.execute(text(
                            "SELECT count(*) FROM lab.experiments WHERE run_id=:run"),
                            {"run": run_id}).scalar_one() == 0)
                    observed.update(reached[0])
                    record["scenarios"].append(observed)
    except Exception as error:
        record["error"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        with migrator.begin() as connection:
            for run_id in owned_runs:
                connection.execute(delete(runs).where(runs.c.run_id == run_id))
        migrator.dispose()
        director.dispose()
        record["source_after_sha256"] = hashes()
        record["checks"]["sources_unchanged"] = record["source_sha256"] == record["source_after_sha256"]
        code = int(bool(record.get("error")) or len(record["scenarios"]) != 3
                   or not all(record["checks"].values()))
        record["overall_exit_code"] = code
        with output.open("x") as stream:
            json.dump(record, stream, indent=2)
            stream.write("\n")
        print(json.dumps({"checks": record["checks"], "error": record.get("error"),
                          "overall_exit_code": code, "output": str(output)}))
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(main(parser.parse_args().output))
