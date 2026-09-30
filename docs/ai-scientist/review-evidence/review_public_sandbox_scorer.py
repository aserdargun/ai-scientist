"""Actual 27-task public robust-z fit/guards/Scorer check, without an LLM.

Runs only against a frozen private Lab worktree and its pre-registered public DB.
This checks seed zero, not the three-algorithm/three-seed calibration or research.
"""

from __future__ import annotations

from dataclasses import replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
import traceback
from uuid import uuid4

from sqlalchemy import create_engine, delete, insert, text
from sqlalchemy.engine import make_url

from harness.fingerprint import compute_harness_hash
from lab.db.schema import runs
from lab.director.baselines import baseline_candidate_source
from lab.director.ledger import register_experiment, transition_experiment
from lab.director.suite_manifest import load_suite_manifest
from lab.director.task_plan import RunTaskAssignment, plan_run_tasks
from lab.sandbox.docker_runner import DEFAULT_SANDBOX_IMAGE, LocalDockerRunner
from lab.sandbox.evaluation import run_guarded_seed_evaluation
from lab.scorer.jobs import enqueue_score_job
from lab.scorer.supervisor import PROJECT_ROOT as SCORER_ROOT, run_scorer_process

MAIN = Path("/home/cachyos/ai-scientist")
ROOT = MAIN / "data/runtime/parallel-m0/public-execution"
EVIDENCE = MAIN / "docs/ai-scientist/review-evidence/public-sandbox-scorer-024-review.json"
DATABASE = "swapp_lab_m0_public_suite_3ddaa2d8"
EXPECTED_HARNESS = "7d8f157024790b7c5b4b47b66340a861bc80b5390e2c4f27ebba56aac1d6861e"
EXPECTED_MANIFEST = "4aeeb8c015ebb7f3ef68a76d38b158a7d286f0b9a46b6fa19a8682c3a4a8ca1f"


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def main() -> int:
    os.umask(0o077)
    assert SCORER_ROOT == ROOT and not EVIDENCE.exists()
    assert compute_harness_hash(ROOT).sha256 == EXPECTED_HARNESS
    engines = {}
    for role in ("migrator", "director", "planner"):
        path = ROOT / f"data/runtime/postgres/{role}.dsn"
        assert not path.is_symlink() and path.stat().st_mode & 0o077 == 0
        url = make_url(path.read_text().strip())
        assert url.database == DATABASE
        engines[role] = create_engine(url, hide_parameters=True, pool_size=2, max_overflow=0)
    run_id = uuid4()
    experiment_id = f"exp_{uuid4().hex}"
    private = ROOT / "data/runtime/public-scorer-review" / run_id.hex
    private.mkdir(parents=True, mode=0o700)
    started = time.monotonic()
    deadline = started + 1800
    source = baseline_candidate_source("robust_z")
    source_sha = digest(source)
    record = {"schema": "actual-public-sandbox-scorer.v1", "passed": False,
              "scope": "Actual public data; robust_z seed0, fresh Docker fit/score/guards and separate Scorer. No LLM, full calibration, Referee, training or AOS acceptance.",
              "database": DATABASE, "worktree": str(ROOT.relative_to(MAIN)),
              "source_commit": "7d3fd22633ae280537f88924332563a09ae4d357",
              "harness_sha256": EXPECTED_HARNESS, "image": DEFAULT_SANDBOX_IMAGE,
              "manifest_sha256": EXPECTED_MANIFEST, "run_id": str(run_id),
              "experiment_id": experiment_id, "candidate_sha256": source_sha,
              "driver_sha256": digest(Path(__file__).read_bytes()), "measurements": [],
              "cleanup": {}}

    def persist() -> None:
        record["elapsed_seconds"] = time.monotonic() - started
        pending = EVIDENCE.with_suffix(".pending")
        pending.write_text(json.dumps(record, indent=2, allow_nan=False) + "\n")
        os.replace(pending, EVIDENCE)

    try:
        document, tasks, manifest_sha = load_suite_manifest(
            ROOT / "data/runtime/public-suite-v2.json", engines["planner"]
        )
        assert manifest_sha == EXPECTED_MANIFEST and len(tasks) == 27
        assert document.suite_version == 2
        request = {"suite": document.suite_id, "suite_version": 2,
                   "profile": "actual-public-scorer-review/no-llm", "seed": 0}
        input_payload = json.dumps(request, sort_keys=True).encode()
        with engines["migrator"].begin() as connection:
            connection.execute(insert(runs).values(
                run_id=run_id, origin="local", owner_id=f"public-scorer-review:{run_id}",
                idempotency_key=str(run_id), payload_sha256=digest(input_payload),
                request_json=request, state="running",
            ))
        register_experiment(
            engines["director"], experiment_id=experiment_id, run_id=str(run_id), sequence=0,
            experiment_number=None, kind="baseline", baseline_name="robust_z",
            parent_experiment_id=None, candidate_sha256=source_sha,
            candidate_blob_sha256=source_sha, inputs_sha256=digest(input_payload),
            move_type="detector", system="S1", hypothesis="Measure public robust-z seed zero.",
            predicted_delta=None, proposal={"baseline_name": "robust_z", "review_only": True},
        )
        transition_experiment(engines["director"], experiment_id=experiment_id, status="primary_running")
        plan_run_tasks(engines["planner"], run_id=run_id, assignments=tuple(
            RunTaskAssignment(experiment_id=experiment_id, evaluation_kind="baseline",
                              task_id=task.task_id, seed=0, candidate_sha256=source_sha,
                              dataset_id=task.dataset_id, split_id=task.split_id,
                              session_id=task.session_id) for task in tasks
        ))
        runner = LocalDockerRunner(image=DEFAULT_SANDBOX_IMAGE, work_root=private / "sandbox")
        for task in tasks:
            # Release between tasks so other bounded CPU validations can advance.
            with (MAIN / "data/runtime/parallel-m0/cpu-check.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                remaining = min(600, int(deadline - time.monotonic()))
                if remaining < 1:
                    raise TimeoutError("public review wall budget exhausted")
                assert compute_harness_hash(ROOT).sha256 == EXPECTED_HARNESS
                task_started = time.monotonic()
                train, evaluation = task.frames()
                output = run_guarded_seed_evaluation(
                    runner, candidate_source=source, train=train, evaluation=evaluation,
                    context=replace(task.context, seed=0, time_budget_s=float(remaining)),
                    task_ids=task.task_ids_for_guard,
                    evaluation_instants=frozenset(task.evaluation_instants),
                    remaining_seconds=remaining, trusted_baseline_name="robust_z",
                )
                job = enqueue_score_job(
                    engines["planner"], run_id=run_id, experiment_id=experiment_id,
                    evaluation_kind="baseline", task_id=task.task_id, seed=0,
                    candidate_sha256=source_sha, candidate_output=output.evaluation.score_document,
                )
                scorer_seconds = min(600, int(deadline - time.monotonic()),
                                     int(remaining - (time.monotonic() - task_started)))
                if scorer_seconds < 1:
                    raise TimeoutError("public task wall budget exhausted")
                process = run_scorer_process(job, remaining_seconds=scorer_seconds)
                if process.exit_code != 0 or process.result is None or process.result.get("state") != "completed":
                    record["failed_worker"] = {"exit_code": process.exit_code,
                                               "state": (process.result or {}).get("state"),
                                               "unit": process.unit}
                    raise RuntimeError("actual public Scorer did not complete")
                with engines["director"].connect() as connection:
                    row = connection.execute(text(
                        "SELECT task_family,task_score,vus_pr,vus_roc FROM lab.dev_task_results "
                        "WHERE run_id=:run AND experiment_id=:experiment AND task_id=:task "
                        "AND evaluation_kind='baseline' AND seed=0"
                    ), {"run": run_id, "experiment": experiment_id, "task": task.task_id}).mappings().one()
                assert row["task_family"] == task.family and row["task_score"] is not None
                assert int(process.result["worker_pid"]) != os.getpid()
                record["measurements"].append({
                    "task_id": task.task_id, "dataset_id": task.dataset_id, "family": task.family,
                    "profile_sha256": task.profile_sha256, "seed": 0, "score": dict(row),
                    "fit_seconds": output.evaluation.fit_seconds,
                    "score_seconds": output.evaluation.score_seconds,
                    "elapsed_seconds": time.monotonic() - task_started,
                    "fit_container": output.evaluation.fit_container_name,
                    "score_container": output.evaluation.score_container_name,
                    "candidate_output_sha256": digest(output.evaluation.score_document),
                    "worker_unit": process.unit, "worker_pid": process.result["worker_pid"],
                    "guards_passed": all((output.determinism.passed, output.causality.passed,
                                          output.hardcoding.passed)),
                })
                persist()
                print(json.dumps({"completed": len(record["measurements"]), "total": 27,
                                  "task_id": task.task_id, "family": task.family}), flush=True)
        record["passed"] = len(record["measurements"]) == 27
    except Exception as error:
        record["error_type"] = type(error).__name__
        record["error_frames"] = [{"file": Path(frame.filename).name,
                                   "line": frame.lineno, "function": frame.name}
                                  for frame in traceback.extract_tb(error.__traceback__)]
        reason = getattr(error, "code", None) or getattr(error, "reason", None)
        if isinstance(reason, str) and reason.isidentifier():
            record["error_code"] = reason
    finally:
        # This private test does not publish a full research report. Remove only
        # its own run, retaining immutable public dataset registrations.
        try:
            with engines["migrator"].begin() as connection:
                connection.execute(delete(runs).where(runs.c.run_id == run_id))
            record["cleanup"]["own_private_run_removed"] = True
        except Exception as error:
            record["cleanup"]["error_type"] = type(error).__name__
            record["passed"] = False
        for engine in engines.values():
            engine.dispose()
        record["sources_unchanged"] = compute_harness_hash(ROOT).sha256 == EXPECTED_HARNESS
        record["passed"] = record["passed"] and record["sources_unchanged"]
        persist()
    print(json.dumps({"passed": record["passed"], "completed": len(record["measurements"]),
                      "error_type": record.get("error_type")}), flush=True)
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
