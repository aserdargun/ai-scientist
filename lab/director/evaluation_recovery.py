"""Durable pre-Scorer evaluation provenance and explicit historical result replay."""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Engine, text

from lab.director.journal import DirectorRunLease
from lab.director.ownership import active_execution_owner
from lab.scorer.jobs import store_candidate_artifact


class RecoveredInfrastructureFailure(RuntimeError):
    """Previously consumed evaluation work has no complete independently scored result."""


def validated_evaluation_timings(payload: Mapping[str, Any]) -> dict[str, float]:
    """Keep measured timing fields finite/nonnegative without inventing old fields."""
    fields = ("fit_seconds", "score_seconds") + tuple(
        field for field in ("guarded_wall_seconds", "scorer_wall_seconds") if field in payload
    )
    timings: dict[str, float] = {}
    for field in fields:
        value = payload.get(field)
        if type(value) not in (int, float):
            raise ValueError("evaluation timing must be a finite nonnegative number: " + field)
        numeric = cast(float, value)
        try:
            valid = math.isfinite(numeric) and numeric >= 0
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError("evaluation timing must be a finite nonnegative number: " + field)
        timings[field] = numeric
    return timings


def evidence_key(experiment_id: str, kind: str, task_id: str, seed: int) -> str:
    import hashlib
    import json

    identity = json.dumps([experiment_id, kind, task_id, seed], separators=(",", ":"))
    return "evaluation-evidence:" + hashlib.sha256(identity.encode()).hexdigest()


def persist_evaluation_evidence(
    engine: Engine,
    lease: DirectorRunLease,
    *,
    payload: dict[str, Any],
    score_artifact: bytes,
    artifact_root: Path,
) -> None:
    """Persist score bytes and trusted guards/timing before submitting to Scorer."""
    owner = active_execution_owner()
    if owner is None or payload.get("run_id") != str(owner.run_id):
        raise ValueError("evaluation evidence requires captured run ownership")
    validated_evaluation_timings(payload)
    digest = store_candidate_artifact(score_artifact, artifact_root=artifact_root)
    if digest != payload.get("candidate_output_sha256"):
        raise ValueError("evaluation evidence output digest changed")
    document = {
        "schema": "director-evaluation-evidence.v1",
        **payload,
        "admitted_generation": owner.generation,
        "execution_sha256": owner.execution_sha256,
    }
    key = evidence_key(
        payload["experiment_id"], payload["evaluation_kind"], payload["task_id"], payload["seed"]
    )
    with engine.connect() as connection:
        sequence = connection.execute(
            text(
                "SELECT coalesce(max((event_json->>'sequence')::integer),-1)+1 FROM lab.run_events "
                "WHERE run_id=:run AND event_type='director.checkpoint'"
            ),
            {"run": owner.run_id},
        ).scalar_one()
    lease.append_checkpoint(
        sequence=sequence,
        key=key,
        phase="evaluation_evidence",
        payload=document,
        artifact_root=artifact_root,
    )


def recover_evaluation_measurement(
    engine: Engine,
    lease: DirectorRunLease,
    *,
    run_id: UUID,
    experiment_id: str,
    kind: str,
    task_id: str,
    seed: int,
    expected: dict[str, Any],
    artifact_root: Path,
) -> dict[str, Any] | None:
    """Join exact evidence and actual score; never promote a bare dev result to evidence."""
    checkpoint = lease.read_checkpoint(
        key=evidence_key(experiment_id, kind, task_id, seed), artifact_root=artifact_root
    )
    if checkpoint is None:
        return None
    evidence = checkpoint["payload"]
    owner = active_execution_owner()
    if owner is None or owner.run_id != run_id:
        raise ValueError("evaluation replay requires captured ownership")
    if (
        evidence.get("schema") != "director-evaluation-evidence.v1"
        or evidence.get("run_id") != str(run_id)
        or evidence.get("experiment_id") != experiment_id
        or evidence.get("task_id") != task_id
        or evidence.get("evaluation_kind") != kind
        or evidence.get("seed") != seed
        or any(evidence.get(key) != value for key, value in expected.items())
    ):
        raise ValueError("evaluation evidence identity changed")
    timings = validated_evaluation_timings(evidence)
    # This checkpoint predates Scorer dispatch. It cannot establish how long
    # the later process took, even when a completed job can be read back.
    timings.pop("scorer_wall_seconds", None)
    from lab.director.resume import assert_historical_receipt

    assert_historical_receipt(
        engine,
        owner=owner,
        admitted_generation=evidence["admitted_generation"],
        execution_sha256=evidence["execution_sha256"],
    )
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT d.*,j.job_id,j.state AS "
                    "job_state,j.admitted_generation,j.execution_sha256,"
                    "j.artifact_sha256,j.claim_unit FROM lab.dev_task_results d JOIN "
                    "scorer.score_jobs j "
                    "USING(run_id,experiment_id,evaluation_kind,task_id,seed) WHERE d.run_id=:run "
                    "AND d.experiment_id=:experiment AND d.evaluation_kind=:kind AND "
                    "d.task_id=:task AND d.seed=:seed"
                ),
                {
                    "run": run_id,
                    "experiment": experiment_id,
                    "kind": kind,
                    "task": task_id,
                    "seed": seed,
                },
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise RecoveredInfrastructureFailure("recovered_evaluation_has_no_committed_score")
    if (
        row["job_state"] != "completed"
        or row["admitted_generation"] != evidence["admitted_generation"]
        or row["execution_sha256"] != evidence["execution_sha256"]
        or row["artifact_sha256"] != evidence["candidate_output_sha256"]
        or any(
            row[key] != evidence[key]
            for key in (*expected, "candidate_output_sha256", "sample_count")
        )
    ):
        raise ValueError("Scorer result does not match immutable pre-dispatch evidence")
    from lab.scorer.jobs import read_candidate_artifact

    read_candidate_artifact(evidence["candidate_output_sha256"], artifact_root=artifact_root)
    metrics = {
        key: (float(row[key]) if row[key] is not None else None)
        for key in (
            "task_score",
            "vus_pr",
            "vus_roc",
            "fa_per_day",
            "duty_fraction",
            "position_bias",
        )
    }
    if any(value is not None and not math.isfinite(value) for value in metrics.values()):
        raise ValueError("replayed Scorer metrics are not finite")
    return {
        "task_id": task_id,
        "experiment_id": experiment_id,
        "evaluation_kind": kind,
        "seed": seed,
        **expected,
        "candidate_output_sha256": evidence["candidate_output_sha256"],
        **metrics,
        **timings,
        "guards": evidence["guards"],
        "systemd_unit": row["claim_unit"],
        "scorer_process_exit_code": None,
        "scorer_process_job_id": str(row["job_id"]),
        "scorer_process_invocation_id": None,
        "scorer_process_provenance": "recovered",
        "recovered_from_evidence_sha256": checkpoint["receipt"]["payload_sha256"],
    }


def recover_abandoned_experiment(
    engine: Engine,
    *,
    run_id: UUID,
    experiment_id: str,
    evidence_engine: Engine,
    lease: DirectorRunLease,
    artifact_root: Path,
) -> bool:
    """Explicitly close only unattempted planned cells of a Scorer-proven interrupted experiment."""
    from lab.director.journal import canonical_bytes
    from lab.director.ownership import assert_execution_owner_closure_transaction

    # Only one exact pre-enqueue checkpoint is needed to prove interrupted work.
    with evidence_engine.connect() as connection:
        keys = (
            connection.execute(
                text(
                    "SELECT event_json->>'key' FROM lab.run_events WHERE run_id=:run "
                    "AND event_type='director.checkpoint' "
                    "AND event_json->>'phase'='evaluation_evidence' "
                    "ORDER BY (event_json->>'sequence')::integer LIMIT 10001"
                ),
                {"run": run_id},
            )
            .scalars()
            .all()
        )
    if len(keys) > 10000:
        raise ValueError("evaluation recovery inventory exceeds bound")
    evidence = None
    for key in keys:
        checkpoint = lease.read_checkpoint(key=key, artifact_root=artifact_root)
        if checkpoint is None:
            raise ValueError("evaluation recovery checkpoint disappeared")
        if checkpoint["payload"].get("experiment_id") == experiment_id:
            document = {
                **checkpoint,
                "payload_text": canonical_bytes(checkpoint["payload"]).decode(),
            }
            evidence = canonical_bytes(document).decode()
            # SQL independently requires this exact cell to have no admitted job.
            # Multiple cells are checked below by selecting the missing one first.
            with evidence_engine.connect() as connection:
                exists = connection.execute(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM scorer.score_jobs WHERE run_id=:run "
                        "AND experiment_id=:experiment AND evaluation_kind=:kind "
                        "AND task_id=:task AND seed=:seed)"
                    ),
                    {
                        "run": run_id,
                        "experiment": experiment_id,
                        "kind": checkpoint["payload"]["evaluation_kind"],
                        "task": checkpoint["payload"]["task_id"],
                        "seed": checkpoint["payload"]["seed"],
                    },
                ).scalar_one()
            if not exists:
                break
            evidence = None
    with engine.begin() as connection:
        owner = assert_execution_owner_closure_transaction(connection, run_id=run_id)
        return bool(
            connection.execute(
                text(
                    "SELECT "
                    "lab.close_restart_unattempted_tasks("
                    ":run,:generation,:invocation,:execution,:experiment,:evidence)"
                ),
                {
                    "run": run_id,
                    "generation": owner.generation,
                    "invocation": owner.invocation_id,
                    "execution": owner.execution_sha256,
                    "experiment": experiment_id,
                    "evidence": evidence,
                },
            ).scalar_one()
        )


def reserve_infrastructure_retry(
    engine: Engine,
    lease: DirectorRunLease,
    *,
    experiment_id: str,
    artifact_root: Path,
    remaining_seconds: int,
) -> bool:
    """Consume at most one retry per experiment before retrying external work."""
    import hashlib

    if remaining_seconds < 1:
        return False
    key = "infrastructure-retry:" + hashlib.sha256(experiment_id.encode()).hexdigest()
    if lease.read_checkpoint(key=key, artifact_root=artifact_root) is not None:
        return False
    owner = active_execution_owner()
    if owner is None or owner.run_id != lease.run_id:
        raise ValueError("infrastructure retry requires the captured run owner")
    with engine.connect() as connection:
        sequence = connection.execute(
            text(
                "SELECT coalesce(max((event_json->>'sequence')::integer),-1)+1 FROM lab.run_events "
                "WHERE run_id=:run AND event_type='director.checkpoint'"
            ),
            {"run": owner.run_id},
        ).scalar_one()
    lease.append_checkpoint(
        sequence=sequence,
        key=key,
        phase="infrastructure_retry_reserved",
        payload={
            "run_id": str(owner.run_id),
            "experiment_id": experiment_id,
            "admitted_generation": owner.generation,
            "execution_sha256": owner.execution_sha256,
            "maximum_seconds": min(120, remaining_seconds),
            "retry_ordinal": 1,
            "budget_source": "existing_seed_reservation",
        },
        artifact_root=artifact_root,
    )
    return True
