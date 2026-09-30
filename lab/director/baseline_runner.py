"""Measured baseline bootstrap through the production Docker and Scorer path."""

from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from harness.fingerprint import compute_harness_hash
from lab.director.artifacts import (
    read_director_artifact,
    read_registered_calibration,
    register_frozen_calibration,
    store_director_artifact,
)
from lab.director.baselines import (
    BASELINE_NAMES,
    baseline_candidate_source,
    freeze_calibration_from_database,
)
from lab.director.budget import MAX_SEED_WALL_SECONDS, EpisodeReservation, RunBudget
from lab.director.contracts import ExperimentDocument, TrajectoryDocument
from lab.director.executor import evaluate_and_score_seed
from lab.director.journal import DirectorRunLease, canonical_bytes
from lab.director.ledger import (
    canonical_json_bytes,
    commit_experiment_record,
    register_experiment,
    transition_experiment,
)
from lab.director.runner import _append_next_checkpoint, _candidate_git_tree
from lab.director.suite import SuiteTask
from lab.director.task_plan import RunTaskAssignment, plan_run_tasks
from lab.sandbox.docker_runner import DEFAULT_SANDBOX_IMAGE, PROJECT_ROOT, LocalDockerRunner
from lab.scorer.jobs import DEFAULT_ARTIFACT_ROOT

_NAMESPACE = UUID("0fac846e-d8f8-4c99-8998-d70967764392")


def run_baseline_suite(
    director_engine: Engine,
    planner_engine: Engine,
    *,
    run_id: UUID,
    tasks: tuple[SuiteTask, ...],
    lease: DirectorRunLease,
    runner: LocalDockerRunner,
    suite_id: str,
    suite_version: int,
    harness_sha256: str,
    image_sha256: str,
    budget: RunBudget,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
    seed_wall_seconds: int = MAX_SEED_WALL_SECONDS,
) -> Any:
    """Measure all three trusted baselines at seeds 0..2, then freeze calibration.

    Scores are read only from the Director's dev-result view after independent
    Scorer completion. This entrypoint does not accept caller-supplied metrics.
    """
    if not tasks or len(tasks) > 256:
        raise ValueError("baseline suite requires 1..256 trusted tasks")
    if not 1 <= seed_wall_seconds <= MAX_SEED_WALL_SECONDS:
        raise ValueError("baseline seed deadline must be in 1..600 seconds")
    if runner.image != DEFAULT_SANDBOX_IMAGE:
        raise ValueError("baseline runner must use the pinned sandbox image")
    if compute_harness_hash(PROJECT_ROOT).sha256 != harness_sha256:
        raise ValueError("baseline harness digest differs from the trusted source tree")
    pinned_image_hash = DEFAULT_SANDBOX_IMAGE.rsplit("@sha256:", 1)[-1].removeprefix("sha256:")
    if image_sha256 != pinned_image_hash:
        raise ValueError("baseline image digest differs from the pinned sandbox")
    if len({task.task_id for task in tasks}) != len(tasks):
        raise ValueError("baseline suite task IDs must be unique")
    _restore_latest_baseline_budget(director_engine, lease, run_id, budget, artifact_root)

    calibration_tasks = tuple(task.calibration_identity for task in tasks)
    expected_identities = {
        (task.dataset_id, task.split_id, task.session_id, task.task_id)
        for task in calibration_tasks
    }
    try:
        with director_engine.connect() as connection:
            existing_calibration = connection.execute(
                text("SELECT lab.baseline_calibration_receipt(:run_id)"), {"run_id": run_id}
            ).scalar_one_or_none()
    except DBAPIError as exc:
        original = exc.orig
        sqlstate = getattr(original, "sqlstate", None)
        primary_message = getattr(getattr(original, "diag", None), "message_primary", None)
        if sqlstate == "P0001" and primary_message == "run has no frozen baseline calibration":
            existing_calibration = None
        else:
            raise
    if existing_calibration is not None:
        document = read_registered_calibration(
            director_engine, run_id=run_id, artifact_root=artifact_root
        )
        if (
            document.suite_id != suite_id
            or document.suite_version != suite_version
            or document.harness_sha256 != harness_sha256
            or document.image_sha256 != image_sha256
            or {
                (task.dataset_id, task.split_id, task.session_id, task.task_id)
                for task in document.tasks
            }
            != expected_identities
        ):
            raise RuntimeError("registered baseline calibration differs from the requested suite")
        return document

    for sequence, name in enumerate(BASELINE_NAMES):
        lease.heartbeat()
        source = baseline_candidate_source(name)
        candidate_sha = hashlib.sha256(source).hexdigest()
        candidate_blob = store_director_artifact(source, artifact_root=artifact_root)
        if (
            candidate_blob != candidate_sha
            or read_director_artifact(candidate_blob, artifact_root=artifact_root) != source
        ):
            raise RuntimeError("baseline candidate blob failed digest-bound readback")
        experiment_id = _baseline_experiment_id(run_id, name)
        input_bytes = canonical_bytes(
            {
                "schema": "baseline-inputs.v1",
                "run_id": str(run_id),
                "baseline_name": name,
                "candidate_sha256": candidate_sha,
                "suite_id": suite_id,
                "suite_version": suite_version,
                "harness_sha256": harness_sha256,
                "image_sha256": image_sha256,
                "tasks": [task.model_dump(mode="json") for task in calibration_tasks],
                "seeds": [0, 1, 2],
            }
        )
        inputs_sha = store_director_artifact(input_bytes, artifact_root=artifact_root)
        hypothesis = (
            f"Measure allowlisted {name} baseline using guarded fit and independent scoring."
        )
        registration = register_experiment(
            director_engine,
            experiment_id=experiment_id,
            run_id=str(run_id),
            sequence=sequence,
            experiment_number=None,
            kind="baseline",
            baseline_name=name,
            parent_experiment_id=None,
            candidate_sha256=candidate_sha,
            candidate_blob_sha256=candidate_blob,
            inputs_sha256=inputs_sha,
            move_type="detector",
            system="S1",
            hypothesis=hypothesis,
            predicted_delta=None,
            proposal={
                "schema": "baseline.registration.v1",
                "baseline_name": name,
                "input_sha256": hashlib.sha256(input_bytes).hexdigest(),
            },
        )
        if registration.get("status") not in {"proposed", "primary_running", "scored"}:
            raise RuntimeError("baseline registration returned an unsupported ledger state")
        if registration.get("status") == "scored":
            with director_engine.connect() as connection:
                receipt = connection.execute(
                    text("SELECT lab.experiment_record_receipt(:id)"),
                    {"id": experiment_id},
                ).scalar_one()
            if not isinstance(receipt, dict):
                raise RuntimeError("scored baseline has no immutable terminal receipt")
            continue
        assignments = tuple(
            RunTaskAssignment(
                experiment_id=experiment_id,
                evaluation_kind="baseline",
                task_id=task.task_id,
                seed=seed,
                candidate_sha256=candidate_sha,
                dataset_id=task.dataset_id,
                split_id=task.split_id,
                session_id=task.session_id,
            )
            for task in tasks
            for seed in range(3)
        )
        if registration.get("status") != "scored":
            plan_run_tasks(planner_engine, run_id=run_id, assignments=assignments)
            transition_experiment(
                director_engine, experiment_id=experiment_id, status="primary_running"
            )

        measurements: dict[tuple[str, int], dict[str, Any]] = {}
        for seed in range(3):
            lease.heartbeat()
            complete_key = f"baseline-seed-complete:{name}:{seed}"
            complete = lease.read_checkpoint(key=complete_key, artifact_root=artifact_root)
            if complete is not None:
                complete_payload = complete["payload"]
                if (
                    not isinstance(complete_payload, dict)
                    or complete_payload.get("experiment_id") != experiment_id
                    or complete_payload.get("seed") != seed
                ):
                    raise RuntimeError("baseline completed seed receipt identity mismatch")
                for task in tasks:
                    row = _read_baseline_measurement(
                        lease,
                        artifact_root,
                        name=name,
                        experiment_id=experiment_id,
                        task=task,
                        seed=seed,
                        candidate_sha=candidate_sha,
                    )
                    if row is None:
                        raise RuntimeError("completed baseline seed is missing a task measurement")
                    measurements[(task.task_id, seed)] = row
                continue
            reservation_key = f"baseline-seed-reservation:{name}:{seed}"
            reserved = lease.read_checkpoint(key=reservation_key, artifact_root=artifact_root)
            if reserved is None:
                # One reservation covers every task at this baseline/seed. Each
                # task retains its <=600-second executor bound; the group is
                # clipped to the remaining immutable run execution allowance.
                reserve_seconds = min(
                    len(tasks) * seed_wall_seconds, int(budget.remaining_wall_seconds)
                )
                if reserve_seconds < 1:
                    raise RuntimeError("run_wall_budget_exhausted")
                reservation = budget.reserve_work(wall_seconds=reserve_seconds, model_tokens=0)
                started_at = datetime.now(UTC)
                _append_next_checkpoint(
                    lease,
                    director_engine,
                    run_id=run_id,
                    key=reservation_key,
                    phase="baseline_seed_reserved",
                    payload={
                        "experiment_id": experiment_id,
                        "seed": seed,
                        "name": name,
                        "reservation_id": str(reservation.reservation_id),
                        "wall_seconds": reservation.wall_seconds,
                        "model_tokens": reservation.model_tokens,
                        "started_at": started_at.isoformat(),
                        "budget": _budget_json(budget),
                    },
                    artifact_root=artifact_root,
                )
            else:
                data = reserved["payload"]
                if not isinstance(data, dict) or any(
                    data.get(key) != value
                    for key, value in (
                        ("experiment_id", experiment_id),
                        ("seed", seed),
                        ("name", name),
                    )
                ):
                    raise RuntimeError("baseline seed reservation identity mismatch")
                reservation = EpisodeReservation(
                    reservation_id=UUID(str(data["reservation_id"])),
                    wall_seconds=int(data["wall_seconds"]),
                    model_tokens=int(data["model_tokens"]),
                )
                budget.adopt_reservation(reservation)
                started_at = datetime.fromisoformat(str(data["started_at"]))
                if started_at.tzinfo is None:
                    raise RuntimeError("baseline seed start time must include timezone")
            deadline = time.monotonic() + max(
                0.0, reservation.wall_seconds - (datetime.now(UTC) - started_at).total_seconds()
            )
            for task in tasks:
                lease.heartbeat()
                row = _read_baseline_measurement(
                    lease,
                    artifact_root,
                    name=name,
                    experiment_id=experiment_id,
                    task=task,
                    seed=seed,
                    candidate_sha=candidate_sha,
                )
                if row is None:
                    remaining = int(deadline - time.monotonic())
                    if remaining < 1:
                        raise RuntimeError("whole_suite_seed_budget_exhausted")
                    started = time.monotonic()
                    measurement = evaluate_and_score_seed(
                        director_engine,
                        planner_engine,
                        runner,
                        run_id=run_id,
                        experiment_id=experiment_id,
                        evaluation_kind="baseline",
                        task=task,
                        candidate_source=source,
                        candidate_sha256=candidate_sha,
                        seed=seed,
                        harness_sha256=harness_sha256,
                        image_sha256=image_sha256,
                        remaining_seconds=min(seed_wall_seconds, remaining),
                        lease=lease,
                        trusted_baseline_name=name,
                        artifact_root=artifact_root,
                    )
                    row = measurement
                    _append_next_checkpoint(
                        lease,
                        director_engine,
                        run_id=run_id,
                        key=f"baseline-measurement:{name}:{task.task_id}:{seed}",
                        phase="baseline_measured",
                        payload={
                            "measurement": measurement,
                            "elapsed_wall_seconds": time.monotonic() - started,
                        },
                        artifact_root=artifact_root,
                    )
                measurements[(task.task_id, seed)] = row
            elapsed = max(0.0, (datetime.now(UTC) - started_at).total_seconds())
            if elapsed > reservation.wall_seconds:
                raise RuntimeError("baseline suite seed exceeded its total wall reservation")
            budget.reconcile_proposal(
                reservation,
                measured_wall_seconds=elapsed,
                measured_model_tokens=0,
            )
            _append_next_checkpoint(
                lease,
                director_engine,
                run_id=run_id,
                key=complete_key,
                phase="seed_complete",
                payload={
                    "experiment_id": experiment_id,
                    "seed": seed,
                    "elapsed_wall_seconds": elapsed,
                    "budget": _budget_json(budget),
                },
                artifact_root=artifact_root,
            )

        per_task = []
        for task in tasks:
            rows = [measurements[(task.task_id, seed)] for seed in range(3)]
            family_primary = [
                float(row["task_score"] if row.get("task_score") is not None else row["vus_pr"])
                for row in rows
            ]
            per_task.append(
                {
                    "task_id": task.task_id,
                    "task_family": task.family,
                    "score_norm": None,
                    "task_score": sum(family_primary) / 3.0,
                    "vus_pr": (
                        sum(float(row["vus_pr"]) for row in rows) / 3.0
                        if task.family == "EVT"
                        else None
                    ),
                    "vus_roc": (
                        sum(float(row["vus_roc"]) for row in rows) / 3.0
                        if task.family == "EVT"
                        else None
                    ),
                    "fa_per_day": (
                        sum(float(row["fa_per_day"]) for row in rows) / 3.0
                        if task.family in {"PDM", "NRM"}
                        else None
                    ),
                    "duty_fraction": (
                        sum(float(row["duty_fraction"]) for row in rows) / 3.0
                        if task.family in {"PDM", "NRM"}
                        else None
                    ),
                    "event_f1": None,
                    "fit_seconds": sum(float(row["fit_seconds"]) for row in rows),
                    "score_seconds": sum(float(row["score_seconds"]) for row in rows),
                }
            )
        tree_sha = _candidate_git_tree(source)
        identity = {
            "run_id": run_id,
            "experiment_id": experiment_id,
            "kind": "baseline",
            "experiment_number": None,
            "baseline_name": name,
            "calibration_sha256": None,
            "agent_version": "baseline.v0.12.0",
            "parent_experiment_id": None,
            "candidate_sha256": candidate_sha,
            "candidate_blob_sha256": candidate_blob,
            "move_type": "detector",
            "system": "S1",
            "hypothesis": hypothesis,
            "predicted_delta": None,
            "inputs_sha256": hashlib.sha256(input_bytes).hexdigest(),
            "parent_tree": tree_sha,
            "child_tree": tree_sha,
            "harness_sha256": harness_sha256,
            "image_sha256": image_sha256,
            "suite_id": suite_id,
            "suite_version": suite_version,
            "per_task": tuple(per_task),
            "suite_score": None,
            "guards": {"hardcoding": "pass", "determinism": "pass", "causality": "pass"},
            "decision": None,
            "status": "scored",
            "fit_seconds": sum(float(row["fit_seconds"]) for row in measurements.values()),
            "score_seconds": sum(float(row["score_seconds"]) for row in measurements.values()),
            "llm_input_tokens": 0,
            "llm_output_tokens": 0,
            "wall_seconds": sum(
                float(row["fit_seconds"]) + float(row["score_seconds"])
                for row in measurements.values()
            ),
            "ordinal": sequence + 1,
        }
        experiment = ExperimentDocument.model_validate(
            {**identity, "schema": "experiment.v1"}, strict=True
        )
        messages_blob = store_director_artifact(b"[]", artifact_root=artifact_root)
        trajectory = TrajectoryDocument.model_validate(
            {
                "schema": "trajectory.v1",
                "trajectory_id": f"trj_{uuid5(_NAMESPACE, f'trajectory:{experiment_id}').hex}",
                "run_id": run_id,
                "experiment_id": experiment_id,
                "kind": "baseline",
                "experiment_number": None,
                "baseline_name": name,
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
                "inputs_sha256": hashlib.sha256(input_bytes).hexdigest(),
                "messages_blob_sha256": messages_blob,
                "tool_calls": 0,
                "outcome": None,
                "quality_tier": "bronze",
                "secrets_scrubbed": False,
                "people_scrubbed": False,
                "raw_values_scrubbed": False,
                "exclusions": ("Baseline measurement; no proposal or Referee decision.",),
            },
            strict=True,
        )
        experiment_bytes = canonical_json_bytes(experiment)
        trajectory_bytes = canonical_json_bytes(trajectory)
        experiment_blob = store_director_artifact(experiment_bytes, artifact_root=artifact_root)
        trajectory_blob = store_director_artifact(trajectory_bytes, artifact_root=artifact_root)
        if read_director_artifact(experiment_blob, artifact_root=artifact_root) != experiment_bytes:
            raise RuntimeError("baseline experiment blob readback mismatch")
        if read_director_artifact(trajectory_blob, artifact_root=artifact_root) != trajectory_bytes:
            raise RuntimeError("baseline trajectory blob readback mismatch")
        commit_experiment_record(
            director_engine,
            experiment=experiment,
            trajectory=trajectory,
            experiment_blob_sha256=experiment_blob,
            trajectory_blob_sha256=trajectory_blob,
        )

    trusted_tasks = tuple(task.calibration_identity for task in tasks)
    document = freeze_calibration_from_database(
        director_engine,
        run_id=run_id,
        runner_image=DEFAULT_SANDBOX_IMAGE,
        harness_sha256=harness_sha256,
        suite_id=suite_id,
        suite_version=suite_version,
        expected_tasks=trusted_tasks,
    )
    register_frozen_calibration(director_engine, document, artifact_root=artifact_root)
    return read_registered_calibration(director_engine, run_id=run_id, artifact_root=artifact_root)


def _baseline_experiment_id(run_id: UUID, name: str) -> str:
    return f"exp_{uuid5(_NAMESPACE, f'{run_id}:baseline:{name}').hex}"


def _restore_latest_baseline_budget(
    engine: Engine,
    lease: DirectorRunLease,
    run_id: UUID,
    budget: RunBudget,
    artifact_root: Path,
) -> None:
    with engine.connect() as connection:
        key = connection.execute(
            text(
                "SELECT event_json->>'key' FROM lab.run_events WHERE run_id=:run "
                "AND event_type='director.checkpoint' "
                "AND event_json->>'key' LIKE 'baseline-seed-complete:%' "
                "ORDER BY (event_json->>'sequence')::integer DESC LIMIT 1"
            ),
            {"run": run_id},
        ).scalar_one_or_none()
    if key is None:
        return
    checkpoint = lease.read_checkpoint(key=str(key), artifact_root=artifact_root)
    payload = None if checkpoint is None else checkpoint["payload"]
    if not isinstance(payload, dict) or not isinstance(payload.get("budget"), dict):
        raise RuntimeError("latest baseline seed checkpoint has no durable budget snapshot")
    data = payload["budget"]
    raw_reservations = data.get("reservations")
    if not isinstance(raw_reservations, list):
        raise RuntimeError("baseline budget snapshot reservations are malformed")
    reservations = tuple(
        EpisodeReservation(
            reservation_id=UUID(str(item["reservation_id"])),
            wall_seconds=int(item["wall_seconds"]),
            model_tokens=int(item["model_tokens"]),
        )
        for item in raw_reservations
        if isinstance(item, dict)
    )
    if len(reservations) != len(raw_reservations):
        raise RuntimeError("baseline budget reservation identity is malformed")
    from lab.director.budget import BudgetSnapshot

    budget.restore(
        BudgetSnapshot(
            proposal_count=int(data["proposal_count"]),
            wall_seconds=float(data["wall_seconds"]),
            model_tokens=int(data["model_tokens"]),
            reserved_wall_seconds=float(data["reserved_wall_seconds"]),
            reserved_model_tokens=int(data["reserved_model_tokens"]),
            elapsed_wall_seconds=float(data["elapsed_wall_seconds"]),
            reservations=reservations,
        )
    )


def _read_baseline_measurement(
    lease: DirectorRunLease,
    artifact_root: Path,
    *,
    name: str,
    experiment_id: str,
    task: SuiteTask,
    seed: int,
    candidate_sha: str,
) -> dict[str, Any] | None:
    checkpoint = lease.read_checkpoint(
        key=f"baseline-measurement:{name}:{task.task_id}:{seed}",
        artifact_root=artifact_root,
    )
    if checkpoint is None:
        return None
    payload = checkpoint["payload"]
    row = payload.get("measurement") if isinstance(payload, dict) else None
    if not isinstance(row, dict) or any(
        row.get(key) != expected
        for key, expected in (
            ("experiment_id", experiment_id),
            ("candidate_sha256", candidate_sha),
            ("task_id", task.task_id),
            ("seed", seed),
            ("evaluation_kind", "baseline"),
            ("dataset_id", task.dataset_id),
            ("split_id", task.split_id),
            ("session_id", task.session_id),
            ("profile_sha256", task.profile_sha256),
        )
    ):
        raise RuntimeError("baseline measurement checkpoint has a mismatched identity")
    return row


def _budget_json(budget: RunBudget) -> dict[str, Any]:
    snapshot = budget.snapshot()
    return {
        "proposal_count": snapshot.proposal_count,
        "wall_seconds": snapshot.wall_seconds,
        "model_tokens": snapshot.model_tokens,
        "reserved_wall_seconds": snapshot.reserved_wall_seconds,
        "reserved_model_tokens": snapshot.reserved_model_tokens,
        "elapsed_wall_seconds": snapshot.elapsed_wall_seconds,
        "reservations": [
            {
                "reservation_id": str(item.reservation_id),
                "wall_seconds": item.wall_seconds,
                "model_tokens": item.model_tokens,
            }
            for item in snapshot.reservations
        ],
    }
