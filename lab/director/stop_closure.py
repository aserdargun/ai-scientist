"""Bounded closure of an interrupted, uncalibrated baseline after operator stop.

The caller holds the global dispatch lock and run lease. This path never resumes
research and never changes the historical execution owner or budget checkpoints.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from lab.director import recovery
from lab.director.artifacts import (
    read_director_artifact,
    read_registered_calibration,
    store_director_artifact,
)
from lab.director.contracts import ExperimentDocument, TrajectoryDocument
from lab.director.journal import DirectorRunLease, canonical_bytes
from lab.director.ledger import canonical_json_bytes, commit_experiment_record
from lab.director.ownership import ExecutionOwner, owned_execution
from lab.director.resume import _drain_director_sandbox
from lab.director.runner import _candidate_git_tree
from lab.scorer.holdout_supervisor import run_stopped_director_recovery_process
from lab.scorer.recovery_identity import (
    capture_empty_baseline_native_observation,
    verify_attempted_stop_recovery_retirement,
)
from lab.scorer.supervisor import scorer_job_unit_is_quiescent

_TRAJECTORY_NAMESPACE = UUID("0fac846e-d8f8-4c99-8998-d70967764392")


def _process_cgroup(pid: int) -> str:
    try:
        rows = Path(f"/proc/{pid}/cgroup").read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise recovery.RecoveryPending("cannot verify replacement shared Director") from exc
    matches = [row[3:] for row in rows if row.startswith("0::")]
    if len(matches) != 1:
        raise recovery.RecoveryPending("replacement shared Director cgroup is ambiguous")
    return matches[0]


def prove_stopped_owner_dead(owner: recovery.OwnerGeneration, run_id: UUID) -> bool:
    """Allow only a verified retired generation of the fixed shared drain unit.

    A replacement generation is observed twice and is never stopped. Its shared
    cgroup may remain occupied; independent run-specific child proofs are required.
    """
    try:
        return recovery._owner_is_proven_dead(owner, run_id)
    except recovery.RecoveryPending:
        if owner.worker_unit != recovery.OWNER_DRAIN_UNIT:
            raise
    boot = recovery._current_boot_id()
    old_ticks = recovery._current_process_start_ticks(owner.worker_pid)
    if boot == owner.worker_boot_id and old_ticks == owner.worker_start_ticks:
        return False
    properties = recovery._systemctl_show(owner.worker_unit)
    invocation = properties.get("InvocationID", "")
    pid_text = properties.get("MainPID", "")
    if (
        properties.get("LoadState") != "loaded"
        or properties.get("ActiveState") != "active"
        or re.fullmatch(r"[0-9a-f]{32}", invocation) is None
        or invocation == owner.worker_invocation_id
        or not pid_text.isdecimal()
        or int(pid_text) < 1
        or properties.get("ControlGroup") != owner.worker_cgroup
        or not owner.worker_cgroup.endswith("/" + recovery.OWNER_DRAIN_UNIT)
    ):
        raise recovery.RecoveryPending("shared Director replacement is not authoritative")
    pid = int(pid_text)
    ticks = recovery._current_process_start_ticks(pid)
    if ticks is None or _process_cgroup(pid) != owner.worker_cgroup:
        raise recovery.RecoveryPending("replacement Director process does not match its unit")
    if (
        recovery._systemctl_show(owner.worker_unit) != properties
        or recovery._current_process_start_ticks(pid) != ticks
        or recovery._current_boot_id() != boot
        or (
            boot == owner.worker_boot_id
            and recovery._current_process_start_ticks(owner.worker_pid) == owner.worker_start_ticks
        )
    ):
        raise recovery.RecoveryPending("shared Director generation changed during proof")
    return True


def _retire_empty_baseline_worker(
    director: Engine,
    recovery_id: UUID,
    owner: recovery.OwnerGeneration,
    run_id: UUID,
    deadline: float,
    *,
    expected_invocation: str | None = None,
) -> bool:
    """Reuse only sealed evidence whose exact native worker independently retired.

    A registered but unsealed worker is never replaced by a new invocation. Its
    original authority stays pending until expiry. The ordinary supervisor still
    has to return success before this is called for a newly launched worker.
    """
    with director.connect() as connection:
        evidence = {
            str(row.phase): row.evidence_json
            for row in connection.execute(
                text(
                    "SELECT phase,evidence_json FROM lab.director_empty_baseline_stop_evidence "
                    "WHERE recovery_id=:id"
                ),
                {"id": recovery_id},
            ).all()
        }
    if not evidence:
        return False
    sealed = evidence.get("seal")
    if not isinstance(sealed, dict) or not isinstance(sealed.get("worker_identity"), dict):
        raise recovery.RecoveryPending("empty baseline registered worker has no sealed proof")
    identity = sealed["worker_identity"]
    if (
        expected_invocation is not None
        and identity.get("worker_invocation_id") != expected_invocation
    ):
        raise recovery.RecoveryPending("empty baseline seal belongs to another recovery worker")
    capture_empty_baseline_native_observation(owner, run_id, deadline=deadline)
    observation = verify_attempted_stop_recovery_retirement(identity)
    if time.monotonic() >= deadline:
        raise recovery.RecoveryPending("original empty baseline cleanup deadline expired")
    retired = evidence.get("retire")
    # Re-observe native retirement even on exact idempotent replay. Keep the
    # original durable observation bytes; a later timestamp is not a new proof.
    payload = (
        retired
        if retired is not None
        else {
            "context": sealed["context"],
            "worker_identity": identity,
            "observation": observation,
        }
    )
    with director.begin() as connection:
        result = connection.execute(
            text("SELECT lab.record_empty_baseline_stop(:id,'retire',:evidence)"),
            {"id": recovery_id, "evidence": json.dumps(payload, sort_keys=True)},
        ).scalar_one()
    if result != "retired":
        raise recovery.RecoveryPending("empty baseline worker retirement was not confirmed")
    return True


def _baseline_inputs(
    *,
    run_id: UUID,
    row: dict[str, Any],
    execution: dict[str, Any],
    artifact_root: Path,
    suite_manifest: bytes,
) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    """Validate original prepared bytes for completed and interrupted baselines."""
    source = read_director_artifact(row["candidate_blob_sha256"], artifact_root=artifact_root)
    raw = read_director_artifact(row["inputs_sha256"], artifact_root=artifact_root)
    inputs = json.loads(raw)
    if (
        row["kind"] != "baseline"
        or row["status"] not in {"proposed", "primary_running", "scored", "abandoned"}
        or row["baseline_name"] not in {"robust_z", "iforest", "ecod_train_frozen"}
        or row["candidate_blob_sha256"] != row["candidate_sha256"]
        or hashlib.sha256(source).hexdigest() != row["candidate_sha256"]
        or not isinstance(inputs, dict)
        or canonical_bytes(inputs) != raw
        or inputs.get("schema") != "baseline-inputs.v1"
        or inputs.get("run_id") != str(run_id)
        or inputs.get("baseline_name") != row["baseline_name"]
        or inputs.get("candidate_sha256") != row["candidate_sha256"]
        or inputs.get("seeds") != [0, 1, 2]
        or any(
            inputs.get(key) != execution.get(key)
            for key in ("suite_id", "suite_version", "harness_sha256", "image_sha256")
        )
    ):
        raise ValueError("interrupted baseline differs from its prepared immutable inputs")
    tasks = inputs.get("tasks")
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 1024:
        raise ValueError("interrupted baseline has no bounded original task provenance")
    from lab.director.suite_manifest import SuiteManifest

    if hashlib.sha256(suite_manifest).hexdigest() != execution["suite_manifest_sha256"]:
        raise ValueError("original suite manifest digest changed")
    manifest = SuiteManifest.model_validate_json(suite_manifest)
    original = {task.task_id: task for task in manifest.tasks}
    if (
        manifest.suite_id != execution["suite_id"]
        or manifest.suite_version != execution["suite_version"]
        or len(original) != len(tasks)
        or {task["task_id"] for task in tasks} != set(original)
    ):
        raise ValueError("original suite task inventory changed")
    for task in tasks:
        trusted = original.get(task["task_id"])
        if trusted is None or any(
            task[key] != getattr(trusted, key)
            for key in ("dataset_id", "split_id", "session_id", "profile_sha256", "family")
        ):
            raise ValueError("prepared baseline task differs from original suite")
    return source, inputs, original


def _baseline_documents(
    *,
    run_id: UUID,
    row: dict[str, Any],
    execution: dict[str, Any],
    artifact_root: Path,
    recovery_id: UUID,
    failures: list[dict[str, Any]],
    suite_manifest: bytes,
) -> tuple[ExperimentDocument, TrajectoryDocument]:
    """Build truthful abandoned documents only for existing unfinished baselines."""
    if row["status"] not in {"proposed", "primary_running"}:
        raise ValueError("completed baseline cannot be rewritten by stop closure")
    source, inputs, original = _baseline_inputs(
        run_id=run_id,
        row=row,
        execution=execution,
        artifact_root=artifact_root,
        suite_manifest=suite_manifest,
    )
    tasks = inputs["tasks"]
    tree = _candidate_git_tree(source)
    shared = {
        "run_id": run_id,
        "experiment_id": row["experiment_id"],
        "kind": "baseline",
        "experiment_number": None,
        "baseline_name": row["baseline_name"],
        "calibration_sha256": None,
        "agent_version": "baseline.v0.12.0",
        "inputs_sha256": row["inputs_sha256"],
    }
    experiment = ExperimentDocument.model_validate(
        {
            **shared,
            "schema": "experiment.v1",
            "parent_experiment_id": row["parent_experiment_id"],
            "candidate_sha256": row["candidate_sha256"],
            "candidate_blob_sha256": row["candidate_blob_sha256"],
            "move_type": row["move_type"],
            "system": row["system"],
            "hypothesis": row["hypothesis"],
            "predicted_delta": row["predicted_delta"],
            "parent_tree": tree,
            "child_tree": tree,
            **{
                key: execution[key]
                for key in ("suite_id", "suite_version", "harness_sha256", "image_sha256")
            },
            "per_task": (),
            "suite_score": None,
            "guards": {"hardcoding": "not_run", "determinism": "not_run", "causality": "not_run"},
            "decision": None,
            "status": "abandoned",
            "fit_seconds": None,
            "score_seconds": None,
            "llm_input_tokens": 0,
            "llm_output_tokens": 0,
            "wall_seconds": None,
            "ordinal": int(row["sequence"]) + 1,
        },
        strict=True,
    )
    # This provenance is a separate evidence artifact, not invented model messages.
    evidence = store_director_artifact(
        canonical_bytes(
            {
                "schema": "stopped-baseline-infrastructure.v1",
                "run_id": str(run_id),
                "recovery_id": str(recovery_id),
                "experiment_id": row["experiment_id"],
                "candidate_sha256": row["candidate_sha256"],
                "inputs_sha256": row["inputs_sha256"],
                "original_jobs": failures,
            }
        ),
        artifact_root=artifact_root,
    )
    trajectory = TrajectoryDocument.model_validate(
        {
            **shared,
            "schema": "trajectory.v1",
            "trajectory_id": "trj_"
            + uuid5(_TRAJECTORY_NAMESPACE, "trajectory:" + row["experiment_id"]).hex,
            "system": "S1",
            "model_id": "baseline/no-llm.v1",
            "usage_profile": "noncommercial_research",
            "source_provenance": tuple(original[task["task_id"]].provenance for task in tasks),
            "quantization": "none",
            "adapter": "none",
            "thinking": False,
            "temperature": 0.0,
            "top_p": 1.0,
            "context_template": "baseline.no-llm.v1",
            "messages_blob_sha256": store_director_artifact(b"[]", artifact_root=artifact_root),
            "tool_calls": 0,
            "outcome": None,
            "quality_tier": "bronze",
            "secrets_scrubbed": False,
            "people_scrubbed": False,
            "raw_values_scrubbed": False,
            "exclusions": (
                "Interrupted baseline abandoned after stop; no scores or calibration fabricated.",
                "Timing unavailable; original run budget and reservations retained.",
                "Infrastructure evidence artifact: " + evidence,
            ),
        },
        strict=True,
    )
    return experiment, trajectory


def remaining_stop_closure_seconds(director: Engine, recovery_id: UUID) -> int | None:
    """Return the original durable cleanup allowance; retries cannot renew it."""
    with director.connect() as connection:
        value = connection.execute(
            text(
                "SELECT extract(epoch FROM created_at+interval '120 seconds'-clock_timestamp()) "
                "FROM lab.director_stop_closures WHERE recovery_id=:id"
            ),
            {"id": recovery_id},
        ).scalar_one_or_none()
    return None if value is None else max(0, int(value))


def _has_baseline_calibration(director: Engine, run_id: UUID) -> bool:
    """Use the Director receipt boundary; only its exact missing-row error means absence."""
    try:
        with director.connect() as connection:
            receipt = connection.execute(
                text("SELECT lab.baseline_calibration_receipt(:run_id)"), {"run_id": run_id}
            ).scalar_one()
    except DBAPIError as exc:
        original = exc.orig
        if (
            getattr(original, "sqlstate", None) == "P0001"
            and getattr(getattr(original, "diag", None), "message_primary", None)
            == "run has no frozen baseline calibration"
        ):
            return False
        raise
    if not isinstance(receipt, dict) or receipt.get("run_id") != str(run_id):
        raise ValueError("database returned an invalid calibration receipt")
    return True


def _validate_baseline_cells(
    *,
    plans: list[dict[str, Any]],
    measurements: list[dict[str, Any]],
    inputs: dict[str, Any],
    row: dict[str, Any],
    execution: dict[str, Any],
) -> None:
    """Match existing cells to prepared bytes without inventing missing scores."""
    tasks = {task["task_id"]: task for task in inputs["tasks"]}
    expected = {(task_id, seed) for task_id in tasks for seed in (0, 1, 2)}
    actual: set[tuple[str, int]] = set()
    for plan in plans:
        task = tasks.get(plan["task_id"])
        key = (plan["task_id"], plan["seed"])
        if (
            task is None
            or key not in expected
            or key in actual
            or plan["experiment_id"] != row["experiment_id"]
            or plan["evaluation_kind"] != "baseline"
            or plan["candidate_sha256"] != row["candidate_sha256"]
            or any(plan[field] != task[field] for field in ("dataset_id", "split_id", "session_id"))
        ):
            raise recovery.RecoveryPending("baseline plan differs from original prepared tasks")
        actual.add(key)
    if measurements and actual != expected:
        raise recovery.RecoveryPending("measured baseline has an incomplete original task plan")
    seen: set[tuple[str, int]] = set()
    for measurement in measurements:
        key = (measurement["task_id"], measurement["seed"])
        task = tasks.get(measurement["task_id"])
        if (
            task is None
            or key not in actual
            or key in seen
            or measurement["experiment_id"] != row["experiment_id"]
            or measurement["evaluation_kind"] != "baseline"
            or measurement["candidate_sha256"] != row["candidate_sha256"]
            or measurement["harness_sha256"] != execution["harness_sha256"]
            or measurement["task_family"] != task["family"]
            or any(
                measurement[field] != task[field]
                for field in ("dataset_id", "split_id", "session_id", "profile_sha256")
            )
        ):
            raise recovery.RecoveryPending("measured baseline cell provenance changed")
        seen.add(key)


def _partition_stopped_baselines(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Accept only bounded distinct existing trusted baseline registrations."""
    names = {"robust_z", "iforest", "ecod_train_frozen"}
    if (
        not 1 <= len(rows) <= 3
        or len({row["experiment_id"] for row in rows}) != len(rows)
        or len({row["baseline_name"] for row in rows}) != len(rows)
        or any(
            row["kind"] != "baseline"
            or row["baseline_name"] not in names
            or row["status"] not in {"proposed", "primary_running", "scored", "abandoned"}
            for row in rows
        )
    ):
        raise recovery.RecoveryPending("stop requires one to three distinct trusted baselines")
    return [row for row in rows if row["status"] in {"proposed", "primary_running"}]


def _retained_baseline_receipt(director: Engine, experiment_id: str) -> dict[str, Any]:
    with director.connect() as connection:
        receipt = connection.execute(
            text("SELECT lab.experiment_record_receipt(:id)"), {"id": experiment_id}
        ).scalar_one()
    if not isinstance(receipt, dict):
        raise recovery.RecoveryPending("terminal baseline record pair is missing")
    return receipt


def _validate_stopped_baseline_inventory(
    director: Engine,
    *,
    rows: list[dict[str, Any]],
    plans: list[dict[str, Any]],
    measurements: list[dict[str, Any]],
    run_id: UUID,
    execution: dict[str, Any],
    artifact_root: Path,
    suite_manifest: bytes,
) -> dict[str, dict[str, Any]]:
    """Validate each existing plan and preserve terminal pair digests without rewriting."""
    identifiers = {row["experiment_id"] for row in rows}
    if (
        len(plans) > 9216
        or len(measurements) > 9216
        or any(cell["experiment_id"] not in identifiers for cell in plans + measurements)
    ):
        raise recovery.RecoveryPending("stopped cell inventory is outside existing baselines")
    retained = {}
    for row in rows:
        _, inputs, _ = _baseline_inputs(
            run_id=run_id,
            row=row,
            execution=execution,
            artifact_root=artifact_root,
            suite_manifest=suite_manifest,
        )
        selected_plans = [cell for cell in plans if cell["experiment_id"] == row["experiment_id"]]
        selected_scores = [
            cell for cell in measurements if cell["experiment_id"] == row["experiment_id"]
        ]
        _validate_baseline_cells(
            plans=selected_plans,
            measurements=selected_scores,
            inputs=inputs,
            row=row,
            execution=execution,
        )
        if row["status"] not in {"scored", "abandoned"}:
            continue
        if row["status"] == "scored" and len(selected_scores) != 3 * len(inputs["tasks"]):
            raise recovery.RecoveryPending("scored baseline lacks its complete original cells")
        receipt = _retained_baseline_receipt(director, row["experiment_id"])
        if not isinstance(receipt, dict):
            raise recovery.RecoveryPending("terminal baseline record pair is missing")
        if any(
            receipt.get(key) != value
            for key, value in {
                "run_id": str(run_id),
                "experiment_id": row["experiment_id"],
                "status": row["status"],
            }.items()
        ):
            raise recovery.RecoveryPending("terminal baseline receipt identity changed")
        for kind, model in (("experiment", ExperimentDocument), ("trajectory", TrajectoryDocument)):
            raw = read_director_artifact(
                receipt[kind + "_blob_sha256"], artifact_root=artifact_root
            )
            document = model.model_validate_json(raw)
            if (
                hashlib.sha256(raw).hexdigest() != receipt[kind + "_sha256"]
                or canonical_json_bytes(document) != raw
                or document.run_id != run_id
                or document.experiment_id != row["experiment_id"]
                or document.kind != "baseline"
                or document.baseline_name != row["baseline_name"]
                or document.inputs_sha256 != row["inputs_sha256"]
                or document.calibration_sha256 is not None
            ):
                raise recovery.RecoveryPending(
                    "terminal baseline document differs from original pair"
                )
            if isinstance(document, ExperimentDocument) and (
                document.status != row["status"]
                or document.candidate_sha256 != row["candidate_sha256"]
                or document.harness_sha256 != execution["harness_sha256"]
                or document.image_sha256 != execution["image_sha256"]
            ):
                raise recovery.RecoveryPending("terminal baseline experiment provenance changed")
        retained[row["experiment_id"]] = receipt
    return retained


def _assert_retained_baseline_receipts(
    director: Engine,
    retained: dict[str, dict[str, Any]],
) -> None:
    for experiment_id, receipt in retained.items():
        if _retained_baseline_receipt(director, experiment_id) != receipt:
            raise recovery.RecoveryPending("stop closure changed a retained baseline record pair")


def _prove_stopped_terminal_jobs(
    director: Engine,
    planner: Engine,
    run_id: UUID,
    deadline: float,
    owner: ExecutionOwner,
    recovery_id: UUID,
) -> None:
    """Repeat child death proof through the narrow drained-stop receipt authority."""
    with planner.connect() as connection:
        jobs = (
            connection.execute(
                text("SELECT * FROM scorer.score_jobs WHERE run_id=:run LIMIT 9217"),
                {"run": run_id},
            )
            .mappings()
            .all()
        )
    if len(jobs) > 9216:
        raise recovery.RecoveryPending("stopped child inventory exceeds bound")
    for job in jobs:
        remaining = int(deadline - time.monotonic())
        if (
            remaining < 1
            or job["state"] in {"queued", "running"}
            or job["admitted_generation"] != owner.generation
            or job["execution_sha256"] != owner.execution_sha256
        ):
            raise recovery.RecoveryPending("stopped child reconciliation is incomplete")
        invocation = job["claim_invocation_id"]
        if job["state"] == "completed":
            with director.connect() as connection:
                invocation = connection.execute(
                    text("SELECT lab.stopped_baseline_score_invocation(:stop,:job)"),
                    {"stop": recovery_id, "job": job["job_id"]},
                ).scalar_one()
            if not isinstance(invocation, str) or re.fullmatch(r"[0-9a-f]{32}", invocation) is None:
                raise recovery.RecoveryPending("stopped score invocation receipt is invalid")
        if not scorer_job_unit_is_quiescent(
            job["job_id"], expected_invocation_id=invocation, timeout_seconds=min(30, remaining)
        ):
            raise recovery.RecoveryPending("stopped exact Scorer generation is not quiescent")


def reconcile_stopped_baseline(
    director: Engine,
    planner: Engine,
    *,
    run_id: UUID,
    recovery_id: UUID,
    owner: recovery.OwnerGeneration,
    execution_owner: ExecutionOwner,
    lease: DirectorRunLease,
    artifact_root: Path,
    deadline_at: datetime | None = None,
) -> None:
    """Close unfinished baselines or retain a fully verified frozen baseline phase."""
    if deadline_at is not None and datetime.now(UTC) >= deadline_at:
        raise recovery.RecoveryPending("original automatic stop cleanup deadline expired")
    with director.connect() as connection:
        execution = connection.execute(
            text("SELECT execution_json FROM lab.director_execution_contracts WHERE run_id=:run"),
            {"run": run_id},
        ).scalar_one()
        raw_rows = (
            connection.execute(
                text("SELECT * FROM lab.experiments WHERE run_id=:run ORDER BY sequence LIMIT 4"),
                {"run": run_id},
            )
            .mappings()
            .all()
        )
        measurements = [
            dict(value)
            for value in connection.execute(
                text("SELECT * FROM lab.dev_task_results WHERE run_id=:run"),
                {"run": run_id},
            )
            .mappings()
            .all()
        ]
    if not raw_rows:
        return
    rows: list[dict[str, Any]] = [dict(row) for row in raw_rows]
    unfinished = _partition_stopped_baselines(rows)
    calibrated = _has_baseline_calibration(director, run_id)
    if calibrated and (
        len(rows) != 3 or unfinished or any(row["status"] != "scored" for row in rows)
    ):
        raise recovery.RecoveryPending("stopped baseline already has frozen calibration")
    from lab.api.registry import load_suite_registry
    from lab.sandbox.docker_runner import PROJECT_ROOT

    registry_path = os.environ.get("LAB_SUITE_REGISTRY_FILE")
    if not registry_path:
        raise recovery.RecoveryPending("original suite registry is unavailable")
    registry = load_suite_registry(Path(registry_path), PROJECT_ROOT / "data/runtime")
    entry = registry.get(execution["suite_id"])
    if registry.entry_sha256(entry) != execution["registry_entry_sha256"]:
        raise recovery.RecoveryPending("original suite registration changed")
    suite_path, _ = registry.verify_entry(entry)
    suite_manifest = suite_path.read_bytes()
    with planner.connect() as connection:
        plans = [
            dict(value)
            for value in connection.execute(
                text("SELECT * FROM scorer.run_tasks WHERE run_id=:run"),
                {"run": run_id},
            )
            .mappings()
            .all()
        ]
    retained = _validate_stopped_baseline_inventory(
        director,
        rows=rows,
        plans=plans,
        measurements=measurements,
        run_id=run_id,
        execution=execution,
        artifact_root=artifact_root,
        suite_manifest=suite_manifest,
    )
    if not prove_stopped_owner_dead(owner, run_id):
        raise recovery.RecoveryPending("original Director is still alive")
    if calibrated:
        # Frozen baselines need no closure grant. Preserve their documents and the
        # calibration, then let outer recovery prove children and seal/finalize.
        calibration = read_registered_calibration(
            director, run_id=run_id, artifact_root=artifact_root
        )
        manifest = json.loads(suite_manifest)
        fields = ("task_id", "dataset_id", "split_id", "session_id", "profile_sha256", "family")
        sources = {row["baseline_name"]: row["candidate_sha256"] for row in rows}
        if (
            calibration.run_id != run_id
            or any(
                getattr(calibration, key) != execution[key]
                for key in ("suite_id", "suite_version", "harness_sha256", "image_sha256")
            )
            or {tuple(getattr(task, key) for key in fields) for task in calibration.tasks}
            != {tuple(task[key] for key in fields) for task in manifest["tasks"]}
            or any(
                {version.name: version.candidate_sha256 for version in task.baseline_versions}
                != sources
                for task in calibration.tasks
            )
            or calibration.champion_experiment_id not in retained
            or next(
                row["baseline_name"]
                for row in rows
                if row["experiment_id"] == calibration.champion_experiment_id
            )
            != calibration.champion_baseline_name
        ):
            raise recovery.RecoveryPending(
                "frozen calibration differs from retained baseline phase"
            )
        if deadline_at is not None and datetime.now(UTC) >= deadline_at:
            raise recovery.RecoveryPending("original automatic stop cleanup deadline expired")
        _assert_retained_baseline_receipts(director, retained)
        lease.heartbeat()
        return
    with director.begin() as connection:
        seconds = float(
            connection.execute(
                text("SELECT lab.begin_stopped_baseline_closure(:id)"), {"id": recovery_id}
            ).scalar_one()
        )
    if seconds <= 0:
        raise recovery.RecoveryPending("original stopped-closure cleanup deadline expired")
    if deadline_at is not None:
        seconds = min(seconds, (deadline_at.astimezone(UTC) - datetime.now(UTC)).total_seconds())
        if seconds <= 0:
            raise recovery.RecoveryPending("original automatic stop cleanup deadline expired")
    deadline = time.monotonic() + min(120.0, seconds)
    _drain_director_sandbox(run_id, asdict(owner), artifact_root, deadline)
    remaining = min(120, int(deadline - time.monotonic()))
    if remaining < 1:
        raise recovery.RecoveryPending("original stopped-closure cleanup deadline expired")
    reused_empty_proof = not measurements and _retire_empty_baseline_worker(
        director, recovery_id, owner, run_id, deadline
    )
    if not reused_empty_proof:
        result = run_stopped_director_recovery_process(recovery_id, remaining_seconds=remaining)
        if result.state != "drained" or result.exit_code != 0:
            raise recovery.RecoveryPending("stopped child reconciliation is incomplete")
        if not measurements:
            _retire_empty_baseline_worker(
                director,
                recovery_id,
                owner,
                run_id,
                deadline,
                expected_invocation=result.invocation_id,
            )
    _prove_stopped_terminal_jobs(director, planner, run_id, deadline, execution_owner, recovery_id)
    if not prove_stopped_owner_dead(owner, run_id):
        raise recovery.RecoveryPending("original Director identity no longer proves dead")
    with director.connect() as connection:
        failures = (
            connection.execute(
                text(
                    "SELECT original_job FROM lab.director_stop_job_drains WHERE recovery_id=:id "
                    "ORDER BY job_id"
                ),
                {"id": recovery_id},
            )
            .scalars()
            .all()
        )
    for row in unfinished:
        with planner.begin() as connection:
            connection.execute(
                text("SELECT lab.close_stopped_unattempted_tasks(:id,:experiment)"),
                {"id": recovery_id, "experiment": row["experiment_id"]},
            ).scalar_one()
        experiment, trajectory = _baseline_documents(
            run_id=run_id,
            row=row,
            execution=execution,
            artifact_root=artifact_root,
            recovery_id=recovery_id,
            failures=[
                failure for failure in failures if failure["experiment_id"] == row["experiment_id"]
            ],
            suite_manifest=suite_manifest,
        )
        if deadline_at is not None and datetime.now(UTC) >= deadline_at:
            raise recovery.RecoveryPending("original automatic stop cleanup deadline expired")
        commit_remaining = remaining_stop_closure_seconds(director, recovery_id)
        if commit_remaining is None or commit_remaining < 1:
            raise recovery.RecoveryPending(
                "original stopped-closure deadline expired before commit"
            )
        with owned_execution(execution_owner):
            commit_experiment_record(
                director,
                experiment=experiment,
                trajectory=trajectory,
                experiment_blob_sha256=store_director_artifact(
                    canonical_json_bytes(experiment), artifact_root=artifact_root
                ),
                trajectory_blob_sha256=store_director_artifact(
                    canonical_json_bytes(trajectory), artifact_root=artifact_root
                ),
            )
    _assert_retained_baseline_receipts(director, retained)
    lease.heartbeat()
