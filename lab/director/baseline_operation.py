"""Execute an admitted standalone baseline intent without constructing a provider."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import Engine, text

from harness.fingerprint import compute_harness_hash
from lab.director.artifacts import read_director_artifact, store_director_artifact
from lab.director.baseline_runner import _read_baseline_measurement, run_baseline_suite
from lab.director.baselines import BASELINE_NAMES, baseline_candidate_source, calibration_sha256
from lab.director.budget import MAX_RUN_WALL_SECONDS, BudgetSnapshot, RunBudget
from lab.director.contracts import ExperimentDocument, TrajectoryDocument
from lab.director.journal import DirectorRunLease
from lab.director.ledger import canonical_json_bytes, commit_experiment_record
from lab.director.ownership import active_execution_owner, assert_execution_owner_transaction
from lab.director.recovery import capture_current_owner
from lab.director.runner import _candidate_git_tree
from lab.director.suite_manifest import load_suite_manifest
from lab.director.task_plan import (
    cancel_queued_score_job,
    record_baseline_cancelled_task,
    seal_run_task_plan,
)
from lab.sandbox.docker_runner import DEFAULT_SANDBOX_IMAGE, LocalDockerRunner
from lab.scorer.supervisor import (
    MAX_WORKER_RUNTIME_SECONDS,
    run_scorer_finalize_process,
    run_scorer_recovery_process,
    scorer_job_unit_is_quiescent,
)

_BASELINE_TRAJECTORY_NAMESPACE = UUID("0fac846e-d8f8-4c99-8998-d70967764392")


def run_baseline_operation(
    director: Engine,
    planner: Engine,
    *,
    run_id: UUID,
    suite_file: Path,
    artifact_root: Path,
    expected_suite_manifest_sha256: str,
    wall_seconds: int,
    lease: DirectorRunLease,
) -> dict[str, Any]:
    """Run the exact registered baseline matrix and ask the independent Scorer to finalize."""
    if not 1 <= wall_seconds <= MAX_RUN_WALL_SECONDS:
        raise ValueError("baseline wall budget is outside the supported range")
    with director.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT r.state,r.request_json,r.payload_sha256,c.deadline_at "
                    "FROM lab.runs AS r JOIN lab.director_execution_contracts AS c USING(run_id) "
                    "WHERE r.run_id=:run_id"
                ),
                {"run_id": run_id},
            )
            .mappings()
            .one_or_none()
        )
    owner = active_execution_owner()
    if owner is None or owner.run_id != run_id:
        raise RuntimeError("baseline operation requires its captured execution owner")
    if row is None or row["state"] != "running":
        raise ValueError("baseline operation requires its claimed active run")
    deadline_at = row["deadline_at"]
    if not hasattr(deadline_at, "tzinfo"):
        raise RuntimeError("baseline operation has no immutable execution deadline")
    operation_deadline = time.monotonic() + max(
        0.0, (deadline_at - datetime.now(UTC)).total_seconds()
    )
    request = row["request_json"]
    if (
        not isinstance(request, dict)
        or request.get("purpose") != "baseline"
        or request.get("proposal_limit") != 0
        or request.get("provider") not in {"fake-json", "local-qwen"}
    ):
        raise ValueError("run does not contain a valid immutable baseline intent")
    budget_json = request.get("budget")
    if (
        not isinstance(budget_json, dict)
        or budget_json.get("experiments") != 0
        or budget_json.get("model_tokens") != 0
        or budget_json.get("wall_seconds") != wall_seconds
    ):
        raise ValueError("baseline intent does not have its strict zero-use budget")
    manifest, tasks, manifest_sha = load_suite_manifest(suite_file, planner)
    if (
        manifest_sha != expected_suite_manifest_sha256
        or manifest_sha != request.get("suite_manifest_sha256")
        or manifest.suite_id != request.get("suite")
    ):
        raise ValueError("baseline task manifest differs from its admitted identity")
    harness_sha = compute_harness_hash(Path(__file__).resolve().parents[2]).sha256
    image_digest = DEFAULT_SANDBOX_IMAGE.rsplit("@sha256:", 1)[-1].removeprefix("sha256:")
    if request.get("harness_sha256") != harness_sha or request.get("image_sha256") != image_digest:
        raise ValueError("baseline harness or sandbox image differs from its admitted pin")
    work_root = artifact_root.parent / "sandbox" / str(run_id)
    work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    runner = LocalDockerRunner(image=DEFAULT_SANDBOX_IMAGE, work_root=work_root)
    # Protect an allowance for Scorer at admission. A seed reservation may
    # consume the whole remaining execution budget, so stop cleanup must not
    # discover that no independent finalizer time was left.
    finalizer_reserve = min(MAX_WORKER_RUNTIME_SECONDS, wall_seconds // 3)
    execution_wall = max(1, wall_seconds - finalizer_reserve)
    setup_elapsed = max(0.0, wall_seconds - max(0.0, operation_deadline - time.monotonic()))
    budget = RunBudget(
        proposal_limit=0,
        wall_limit=execution_wall,
        token_limit=0,
        snapshot=BudgetSnapshot(elapsed_wall_seconds=setup_elapsed),
    )
    try:
        if budget.remaining_wall_seconds < 1:
            raise TimeoutError("baseline setup exhausted the reserved execution budget")
        calibration = run_baseline_suite(
            director,
            planner,
            run_id=run_id,
            tasks=tasks,
            lease=lease,
            runner=runner,
            suite_id=manifest.suite_id,
            suite_version=manifest.suite_version,
            harness_sha256=harness_sha,
            image_sha256=image_digest,
            budget=budget,
            artifact_root=artifact_root,
            seed_wall_seconds=min(600, wall_seconds),
        )
        lease.require_run_active()
        return _publish_completed_baseline(
            director,
            planner,
            run_id=run_id,
            calibration=calibration,
            manifest_sha=manifest_sha,
            wall_seconds=wall_seconds,
            budget=budget,
            lease=lease,
            deadline=operation_deadline,
        )
    except Exception:
        # Only an authoritative API stop enters this baseline-only drain. Other
        # runner failures retain the ordinary dispatcher failure semantics.
        with director.connect() as connection:
            latest = (
                connection.execute(
                    text(
                        "SELECT state,request_json,payload_sha256 "
                        "FROM lab.runs WHERE run_id=:run_id"
                    ),
                    {"run_id": run_id},
                )
                .mappings()
                .one_or_none()
            )
        if (
            latest is None
            or latest["state"] != "stop_requested"
            or not isinstance(latest["request_json"], dict)
            or latest["request_json"].get("purpose") != "baseline"
        ):
            raise
        try:
            return _finish_stopped_baseline(
                director,
                planner,
                run_id=run_id,
                request=latest["request_json"],
                payload_sha256=latest["payload_sha256"],
                tasks=tasks,
                suite_id=manifest.suite_id,
                suite_version=manifest.suite_version,
                harness_sha256=harness_sha,
                image_sha256=image_digest,
                lease=lease,
                runner=runner,
                artifact_root=artifact_root,
                deadline=operation_deadline,
            )
        except Exception as cleanup_error:
            # A stop remains pending if owner, drain, or finalization evidence is
            # insufficient. Letting the generic dispatcher terminalize it as a
            # research failure would conceal the unresolved cancellation work.
            return _cleanup_pending(run_id, f"baseline stop cleanup incomplete: {cleanup_error}")


def _publish_completed_baseline(
    director: Engine,
    planner: Engine,
    *,
    run_id: UUID,
    calibration: Any,
    manifest_sha: str,
    wall_seconds: int,
    budget: RunBudget,
    lease: DirectorRunLease,
    deadline: float,
) -> dict[str, Any]:
    lease.require_run_active()
    seal = seal_run_task_plan(planner, run_id=run_id)
    snapshot = budget.snapshot()
    remaining = int(deadline - time.monotonic())
    if remaining < 1:
        raise RuntimeError("original baseline wall deadline expired before finalization")
    finalizer_allowance = min(MAX_WORKER_RUNTIME_SECONDS, remaining)
    budget_receipt = {
        "proposal_count": snapshot.proposal_count,
        "wall_seconds": wall_seconds,
        "elapsed_wall_seconds": snapshot.elapsed_wall_seconds,
        "model_tokens": snapshot.model_tokens,
        "reserved_wall_seconds": snapshot.reserved_wall_seconds,
        "reserved_model_tokens": snapshot.reserved_model_tokens,
        "finalizer_allowance_seconds": finalizer_allowance,
        "reservations": [
            {
                "reservation_id": str(item.reservation_id),
                "wall_seconds": item.wall_seconds,
                "model_tokens": item.model_tokens,
            }
            for item in snapshot.reservations
        ],
    }
    owner = active_execution_owner()
    if owner is None or owner.run_id != run_id:
        raise RuntimeError("baseline registration requires its captured execution owner")
    with director.begin() as connection:
        connection.execute(text("SELECT lab.lock_run_plan(:run_id)"), {"run_id": run_id})
        assert_execution_owner_transaction(connection, owner, run_id=run_id)
        registered = connection.execute(
            text("SELECT lab.register_baseline_operation(:run_id,CAST(:budget AS jsonb))"),
            {"run_id": run_id, "budget": json.dumps(budget_receipt, separators=(",", ":"))},
        ).scalar_one()
    if not isinstance(registered, dict) or registered.get("task_plan_sha256") != seal.sha256:
        raise RuntimeError("database did not register the frozen baseline completion receipt")
    finalizer = run_scorer_finalize_process(
        run_id,
        admitted_generation=owner.generation,
        execution_sha256=owner.execution_sha256,
        remaining_seconds=finalizer_allowance,
    )
    return {
        "run_id": str(run_id),
        "purpose": "baseline",
        "dispatch": "baseline",
        "suite_manifest_sha256": manifest_sha,
        "calibration_sha256": calibration_sha256(calibration),
        "task_plan_sha256": seal.sha256,
        "task_plan_count": seal.task_count,
        "budget": budget_receipt,
        "finalizer": {
            "exit_code": finalizer.exit_code,
            "state": (finalizer.result or {}).get("state", "worker_failed"),
            "unit": finalizer.unit,
        },
    }


def _finish_stopped_baseline(
    director: Engine,
    planner: Engine,
    *,
    run_id: UUID,
    request: dict[str, Any],
    payload_sha256: str,
    tasks: tuple[Any, ...],
    suite_id: str,
    suite_version: int,
    harness_sha256: str,
    image_sha256: str,
    lease: DirectorRunLease,
    runner: LocalDockerRunner,
    artifact_root: Path,
    deadline: float,
) -> dict[str, Any]:
    """Drain only this stopped baseline's work and publish an incomplete report."""
    if lease.run_id != run_id:
        raise RuntimeError("baseline stop cleanup is outside its Director lease")
    lease.heartbeat()
    with director.connect() as connection:
        identity = (
            connection.execute(
                text(
                    "SELECT r.state,r.payload_sha256,r.request_json,r.report_sha256,"
                    "p.report_sha256 AS stored_report_sha256,p.report_json,"
                    "o.payload_sha256 AS owner_payload,o.worker_pid,"
                    "o.worker_start_ticks,o.worker_boot_id,o.worker_unit,"
                    "o.worker_invocation_id,o.worker_cgroup "
                    "FROM lab.runs r JOIN lab.director_run_owners o USING(run_id) "
                    "LEFT JOIN lab.reports p USING(run_id) "
                    "WHERE r.run_id=:run_id"
                ),
                {"run_id": run_id},
            )
            .mappings()
            .one_or_none()
        )
    if (
        identity is None
        or identity["payload_sha256"] != payload_sha256
        or identity["owner_payload"] != payload_sha256
        or identity["request_json"] != request
        or request.get("purpose") != "baseline"
    ):
        raise RuntimeError("stopped baseline owner or immutable request receipt changed")
    current_owner = capture_current_owner(payload_sha256, run_id)
    if any(
        identity[field] != getattr(current_owner, field)
        for field in (
            "payload_sha256",
            "worker_pid",
            "worker_start_ticks",
            "worker_boot_id",
            "worker_unit",
            "worker_invocation_id",
            "worker_cgroup",
        )
    ):
        raise RuntimeError("stopped baseline owner generation no longer matches this process")
    if identity["stored_report_sha256"] is not None:
        report_value = identity["report_json"]
        if not isinstance(report_value, dict):
            raise RuntimeError("prior baseline report is not a JSON object")
        encoded = json.dumps(
            report_value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        if (
            identity["state"] != "stopped"
            or identity["report_sha256"] != digest
            or identity["stored_report_sha256"] != digest
            or report_value.get("schema") != "lab.baseline-report.v1"
            or report_value.get("run_id") != str(run_id)
            or report_value.get("status") != "stopped"
            or report_value.get("calibration_complete") is not False
        ):
            raise RuntimeError("prior stopped baseline report failed status or hash verification")
        return {
            "run_id": str(run_id),
            "purpose": "baseline",
            "dispatch": "baseline",
            "state": "stopped",
            "report_sha256": digest,
        }
    if identity["state"] != "stop_requested":
        raise RuntimeError("baseline stop cleanup requires a stop-requested run")

    runner.reconcile_stopped_run(
        run_id,
        owner_pid=current_owner.worker_pid,
        owner_start_ticks=current_owner.worker_start_ticks,
        owner_boot_id=current_owner.worker_boot_id,
        deadline=deadline,
    )

    # Running Scorer units are drained by their recorded invocation. Queued jobs
    # use the Planner's existing stopped-run cancellation path.
    with planner.connect() as connection:
        all_run_jobs = {
            UUID(str(row.job_id)): row.claim_invocation_id
            for row in connection.execute(
                text(
                    "SELECT job_id,claim_invocation_id FROM scorer.score_jobs WHERE run_id=:run_id"
                ),
                {"run_id": run_id},
            )
        }
        jobs = (
            connection.execute(
                text(
                    "SELECT job_id,state,claim_invocation_id FROM scorer.score_jobs "
                    "WHERE run_id=:run_id AND state IN ('queued','running') "
                    "ORDER BY created_at,job_id"
                ),
                {"run_id": run_id},
            )
            .mappings()
            .all()
        )
    for job in jobs:
        lease.heartbeat()
        if job["state"] == "queued":
            cancel_queued_score_job(planner, job_id=UUID(str(job["job_id"])))
        else:
            invocation = job["claim_invocation_id"]
            if not isinstance(invocation, str) or len(invocation) != 32:
                return _cleanup_pending(run_id, "running Scorer has no recorded invocation")
            remaining = int(deadline - time.monotonic())
            if remaining < 1:
                return _cleanup_pending(
                    run_id, "original wall deadline expired before Scorer drain"
                )
            recovery = run_scorer_recovery_process(
                UUID(str(job["job_id"])),
                expected_claim_invocation_id=invocation,
                remaining_seconds=min(MAX_WORKER_RUNTIME_SECONDS, remaining),
                artifact_root=artifact_root,
            )
            if recovery.exit_code != 0 or recovery.result is None:
                return _cleanup_pending(run_id, "owned Scorer generation did not drain cleanly")
            if recovery.invocation_id is None:
                return _cleanup_pending(
                    run_id, "Scorer recovery did not return its exact generation"
                )
            all_run_jobs[UUID(str(job["job_id"]))] = recovery.invocation_id

    for job_id, invocation in all_run_jobs.items():
        remaining = int(deadline - time.monotonic())
        if remaining < 1:
            return _cleanup_pending(
                run_id, "original wall deadline expired before Scorer verification"
            )
        if not scorer_job_unit_is_quiescent(
            job_id,
            expected_invocation_id=invocation,
            timeout_seconds=min(30, remaining),
        ):
            return _cleanup_pending(run_id, "exact Scorer unit remains live after task resolution")

    with planner.connect() as connection:
        pending = connection.execute(
            text(
                "SELECT count(*) FROM scorer.score_jobs WHERE run_id=:run_id "
                "AND state IN ('queued','running')"
            ),
            {"run_id": run_id},
        ).scalar_one()
    if pending:
        return _cleanup_pending(run_id, "owned Scorer work remains active")

    # Any task cell without a score job or terminal outcome never
    # reached Scorer. Resolve only those planned baseline cells as cancelled.
    with planner.connect() as connection:
        unresolved = (
            connection.execute(
                text(
                    "SELECT t.experiment_id,t.task_id,t.seed,t.candidate_sha256 "
                    "FROM scorer.run_tasks t "
                    "LEFT JOIN scorer.task_terminal_outcomes o "
                    "USING(run_id,experiment_id,evaluation_kind,task_id,seed) "
                    "LEFT JOIN scorer.score_jobs j "
                    "USING(run_id,experiment_id,evaluation_kind,task_id,seed) "
                    "WHERE t.run_id=:run_id AND t.evaluation_kind='baseline' "
                    "AND o.run_id IS NULL AND j.job_id IS NULL "
                    "ORDER BY t.experiment_id,t.task_id,t.seed"
                ),
                {"run_id": run_id},
            )
            .mappings()
            .all()
        )
    for cell in unresolved:
        record_baseline_cancelled_task(
            planner,
            run_id=run_id,
            experiment_id=cell["experiment_id"],
            task_id=cell["task_id"],
            seed=cell["seed"],
            candidate_sha256=cell["candidate_sha256"],
        )

    with planner.connect() as connection:
        planned_cells = {
            (row.experiment_id, row.evaluation_kind, row.task_id, row.seed)
            for row in connection.execute(
                text(
                    "SELECT experiment_id,evaluation_kind,task_id,seed "
                    "FROM scorer.run_tasks WHERE run_id=:run_id"
                ),
                {"run_id": run_id},
            )
        }
        terminal_cells = {
            (row.experiment_id, row.evaluation_kind, row.task_id, row.seed)
            for row in connection.execute(
                text(
                    "SELECT experiment_id,evaluation_kind,task_id,seed "
                    "FROM scorer.task_terminal_outcomes WHERE run_id=:run_id"
                ),
                {"run_id": run_id},
            )
        }
    with director.connect() as connection:
        scored_keys = {
            (row.experiment_id, row.evaluation_kind, row.task_id, row.seed)
            for row in connection.execute(
                text(
                    "SELECT experiment_id,evaluation_kind,task_id,seed "
                    "FROM lab.dev_task_results WHERE run_id=:run_id"
                ),
                {"run_id": run_id},
            )
        }
    if scored_keys & terminal_cells or scored_keys | terminal_cells != planned_cells:
        return _cleanup_pending(run_id, "some planned baseline cells lack a score or typed outcome")

    # Complete only pairs for experiments already registered before stop. Their
    # task scores remain individual evidence; the abandoned documents carry no
    # aggregate per-task result and never imply calibration completion.
    with director.connect() as connection:
        open_experiments = (
            connection.execute(
                text(
                    "SELECT e.* FROM lab.experiments e "
                    "WHERE e.run_id=:run_id AND e.kind='baseline' "
                    "AND e.status IN ('proposed','primary_running') "
                    "ORDER BY e.sequence"
                ),
                {"run_id": run_id},
            )
            .mappings()
            .all()
        )
    for row in open_experiments:
        if row["baseline_name"] not in BASELINE_NAMES:
            raise RuntimeError("stopped baseline registration has an unknown algorithm")
        source = baseline_candidate_source(row["baseline_name"])
        if hashlib.sha256(source).hexdigest() != row["candidate_sha256"]:
            raise RuntimeError("stopped baseline source differs from its immutable registration")
        if row["candidate_blob_sha256"] != row["candidate_sha256"]:
            raise RuntimeError("stopped baseline candidate blob pin is invalid")
        with director.connect() as connection:
            scored_rows = (
                connection.execute(
                    text(
                        "SELECT task_id,seed FROM lab.dev_task_results "
                        "WHERE run_id=:run_id AND experiment_id=:experiment_id "
                        "AND evaluation_kind='baseline' ORDER BY task_id,seed"
                    ),
                    {"run_id": run_id, "experiment_id": row["experiment_id"]},
                )
                .mappings()
                .all()
            )
        fit_seconds = 0.0
        score_seconds = 0.0
        seed_elapsed: dict[int, float] = {}
        timing_known = False
        task_by_id = {task.task_id: task for task in tasks}
        for cell in scored_rows:
            task = task_by_id.get(cell["task_id"])
            if task is None:
                raise RuntimeError("scored cell is absent from the admitted baseline suite")
            measurement = _read_baseline_measurement(
                lease,
                artifact_root,
                name=row["baseline_name"],
                experiment_id=row["experiment_id"],
                task=task,
                seed=cell["seed"],
                candidate_sha=row["candidate_sha256"],
            )
            if measurement is None:
                # Scorer commits its score before Director writes this optional
                # timing checkpoint. Keep the durable score; leave its timing unknown.
                continue
            timing_known = True
            fit_seconds += float(measurement["fit_seconds"])
            score_seconds += float(measurement["score_seconds"])
            seed_checkpoint = lease.read_checkpoint(
                key=f"baseline-seed-complete:{row['baseline_name']}:{cell['seed']}",
                artifact_root=artifact_root,
            )
            seed_payload = None if seed_checkpoint is None else seed_checkpoint.get("payload")
            elapsed = (
                seed_payload.get("elapsed_wall_seconds") if isinstance(seed_payload, dict) else None
            )
            if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool):
                seed_elapsed[cell["seed"]] = max(
                    seed_elapsed.get(cell["seed"], 0.0), float(elapsed)
                )
        measured_wall = sum(seed_elapsed.values())
        tree_sha = _candidate_git_tree(source)
        experiment = ExperimentDocument.model_validate(
            {
                "schema": "experiment.v1",
                "run_id": run_id,
                "experiment_id": row["experiment_id"],
                "kind": "baseline",
                "experiment_number": None,
                "baseline_name": row["baseline_name"],
                "calibration_sha256": None,
                "agent_version": "baseline.v0.12.0",
                "parent_experiment_id": row["parent_experiment_id"],
                "candidate_sha256": row["candidate_sha256"],
                "candidate_blob_sha256": row["candidate_blob_sha256"],
                "move_type": row["move_type"],
                "system": row["system"],
                "hypothesis": row["hypothesis"],
                "predicted_delta": row["predicted_delta"],
                "inputs_sha256": row["inputs_sha256"],
                "parent_tree": tree_sha,
                "child_tree": tree_sha,
                "harness_sha256": harness_sha256,
                "image_sha256": image_sha256,
                "suite_id": suite_id,
                "suite_version": suite_version,
                "per_task": (),
                "suite_score": None,
                "guards": {
                    "hardcoding": "not_run",
                    "determinism": "not_run",
                    "causality": "not_run",
                },
                "decision": None,
                "status": "abandoned",
                "fit_seconds": fit_seconds if timing_known else None,
                "score_seconds": score_seconds if timing_known else None,
                "llm_input_tokens": 0,
                "llm_output_tokens": 0,
                "wall_seconds": measured_wall,
                "ordinal": int(row["sequence"]) + 1,
            },
            strict=True,
        )
        messages_blob = store_director_artifact(b"[]", artifact_root=artifact_root)
        trajectory = TrajectoryDocument.model_validate(
            {
                "schema": "trajectory.v1",
                "trajectory_id": (
                    "trj_"
                    + uuid5(
                        _BASELINE_TRAJECTORY_NAMESPACE,
                        "trajectory:" + row["experiment_id"],
                    ).hex
                ),
                "run_id": run_id,
                "experiment_id": row["experiment_id"],
                "kind": "baseline",
                "experiment_number": None,
                "baseline_name": row["baseline_name"],
                "calibration_sha256": None,
                "agent_version": "baseline.v0.12.0",
                "system": "S1",
                "model_id": "baseline/no-llm.v1",
                "usage_profile": "noncommercial_research",
                "source_provenance": tuple(
                    task.provenance.model_dump(mode="json") for task in tasks
                ),
                "quantization": "none",
                "adapter": "none",
                "thinking": False,
                "temperature": 0.0,
                "top_p": 1.0,
                "context_template": "baseline.no-llm.v1",
                "inputs_sha256": row["inputs_sha256"],
                "messages_blob_sha256": messages_blob,
                "tool_calls": 0,
                "outcome": None,
                "quality_tier": "bronze",
                "secrets_scrubbed": False,
                "people_scrubbed": False,
                "raw_values_scrubbed": False,
                "exclusions": (
                    "Baseline stopped before full calibration; partial scores remain separate.",
                    "Timing includes only verified measurement and complete-seed receipts.",
                ),
            },
            strict=True,
        )
        experiment_bytes = canonical_json_bytes(experiment)
        trajectory_bytes = canonical_json_bytes(trajectory)
        experiment_blob = store_director_artifact(experiment_bytes, artifact_root=artifact_root)
        trajectory_blob = store_director_artifact(trajectory_bytes, artifact_root=artifact_root)
        if read_director_artifact(experiment_blob, artifact_root=artifact_root) != experiment_bytes:
            raise RuntimeError("abandoned experiment artifact failed readback")
        if read_director_artifact(trajectory_blob, artifact_root=artifact_root) != trajectory_bytes:
            raise RuntimeError("abandoned trajectory artifact failed readback")
        commit_experiment_record(
            director,
            experiment=experiment,
            trajectory=trajectory,
            experiment_blob_sha256=experiment_blob,
            trajectory_blob_sha256=trajectory_blob,
        )

    lease.heartbeat()
    seal = seal_run_task_plan(planner, run_id=run_id)
    remaining = int(deadline - time.monotonic())
    if remaining < 1:
        return _cleanup_pending(
            run_id, "original baseline wall deadline expired before finalization"
        )
    allowance = min(MAX_WORKER_RUNTIME_SECONDS, remaining)
    owner = active_execution_owner()
    if owner is None or owner.run_id != run_id:
        raise RuntimeError("stopped baseline finalization requires its captured owner")
    finalizer = run_scorer_finalize_process(
        run_id,
        admitted_generation=owner.generation,
        execution_sha256=owner.execution_sha256,
        remaining_seconds=allowance,
    )
    if (
        finalizer.exit_code != 0
        or finalizer.result is None
        or finalizer.result.get("state") != "finalized"
        or finalizer.result.get("research_status") != "stopped"
    ):
        return _cleanup_pending(run_id, "independent Scorer finalizer did not publish a report")
    report_receipt = _read_stopped_report_receipt(director, run_id=run_id)
    if report_receipt is None:
        return _cleanup_pending(run_id, "stopped baseline report failed database/hash verification")
    return {
        "run_id": str(run_id),
        "purpose": "baseline",
        "dispatch": "baseline",
        "state": "stopped",
        "task_plan_sha256": seal.sha256,
        "task_plan_count": seal.task_count,
        "report_sha256": report_receipt,
        "finalizer": {"exit_code": finalizer.exit_code, "unit": finalizer.unit},
    }


def _cleanup_pending(run_id: UUID, reason: str) -> dict[str, Any]:
    """Leave a stopped run visibly pending when owned work cannot be proven drained."""
    return {
        "run_id": str(run_id),
        "purpose": "baseline",
        "dispatch": "baseline",
        "state": "stop_requested",
        "cleanup": "pending",
        "reason": reason[:240],
    }


def _read_stopped_report_receipt(director: Engine, *, run_id: UUID) -> str | None:
    """Return the hash only for a persisted, canonical stopped baseline report."""
    with director.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT r.state,r.report_sha256,p.report_sha256 AS stored_sha256,p.report_json "
                    "FROM lab.runs r JOIN lab.reports p USING(run_id) WHERE r.run_id=:run_id"
                ),
                {"run_id": run_id},
            )
            .mappings()
            .one_or_none()
        )
    if row is None or row["state"] != "stopped" or not isinstance(row["report_json"], dict):
        return None
    report = row["report_json"]
    encoded = json.dumps(
        report,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    if (
        digest != row["report_sha256"]
        or digest != row["stored_sha256"]
        or report.get("schema") != "lab.baseline-report.v1"
        or report.get("run_id") != str(run_id)
        or report.get("status") != "stopped"
        or report.get("calibration_complete") is not False
    ):
        return None
    return digest
