"""Director-only access to the immutable experiment ledger."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import Engine, text

from lab.director.contracts import ExperimentDocument, TrajectoryDocument
from lab.director.ownership import (
    assert_execution_owner_closure_transaction,
    assert_execution_owner_transaction,
)


def canonical_json_bytes(value: BaseModel | dict[str, Any]) -> bytes:
    """Encode a strict document canonically before hashing or storing it."""
    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json", by_alias=True, exclude_none=False)
    else:
        payload = value
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def document_sha256(value: BaseModel | dict[str, Any]) -> str:
    """Return SHA-256 of the canonical UTF-8 JSON document."""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def register_experiment(
    engine: Engine,
    *,
    experiment_id: str,
    run_id: str,
    sequence: int,
    experiment_number: int | None,
    kind: str,
    baseline_name: str | None,
    parent_experiment_id: str | None,
    candidate_sha256: str,
    candidate_blob_sha256: str,
    inputs_sha256: str,
    move_type: str,
    system: str,
    hypothesis: str,
    predicted_delta: float | None,
    proposal: dict[str, Any],
) -> dict[str, Any]:
    """Durably register an immutable proposal before any candidate execution."""
    with engine.begin() as connection:
        connection.execute(text("SELECT lab.lock_run_plan(:run_id)"), {"run_id": run_id})
        assert_execution_owner_transaction(connection, run_id=UUID(run_id))
        row = (
            connection.execute(
                text(
                    """
                SELECT (lab.register_experiment(
                    :experiment_id, :run_id, :sequence, :experiment_number, :kind,
                    :baseline_name, :parent_experiment_id, :candidate_sha256,
                    :candidate_blob_sha256, :inputs_sha256, :move_type, :system,
                    :hypothesis, :predicted_delta, CAST(:proposal_json AS jsonb)
                )).*
                """
                ),
                {
                    "experiment_id": experiment_id,
                    "run_id": run_id,
                    "sequence": sequence,
                    "experiment_number": experiment_number,
                    "kind": kind,
                    "baseline_name": baseline_name,
                    "parent_experiment_id": parent_experiment_id,
                    "candidate_sha256": candidate_sha256,
                    "candidate_blob_sha256": candidate_blob_sha256,
                    "inputs_sha256": inputs_sha256,
                    "move_type": move_type,
                    "system": system,
                    "hypothesis": hypothesis,
                    "predicted_delta": predicted_delta,
                    "proposal_json": canonical_json_bytes(proposal).decode("utf-8"),
                },
            )
            .mappings()
            .one()
        )
    return dict(row)


def transition_experiment(engine: Engine, *, experiment_id: str, status: str) -> str:
    """Advance a registered proposal through a nonterminal measured state."""
    with engine.begin() as connection:
        run_id = connection.execute(
            text("SELECT run_id FROM lab.experiments WHERE experiment_id=:experiment_id"),
            {"experiment_id": experiment_id},
        ).scalar_one_or_none()
        if run_id is None:
            raise ValueError("experiment is not registered")
        connection.execute(text("SELECT lab.lock_run_plan(:run_id)"), {"run_id": run_id})
        assert_execution_owner_transaction(connection, run_id=run_id)
        return str(
            connection.execute(
                text("SELECT lab.transition_experiment(:experiment_id, :status)"),
                {"experiment_id": experiment_id, "status": status},
            ).scalar_one()
        )


def commit_experiment_record(
    engine: Engine,
    *,
    experiment: ExperimentDocument,
    trajectory: TrajectoryDocument,
    experiment_blob_sha256: str,
    trajectory_blob_sha256: str,
) -> dict[str, Any]:
    """Atomically commit both terminal documents, with immutable replay readback."""
    if experiment.experiment_id != trajectory.experiment_id:
        raise ValueError("experiment and trajectory identities differ")
    if experiment.run_id != trajectory.run_id:
        raise ValueError("experiment and trajectory run identities differ")
    if (
        experiment.infrastructure_stop != trajectory.infrastructure_stop
        or experiment.kind != trajectory.kind
        or experiment.experiment_number != trajectory.experiment_number
        or experiment.baseline_name != trajectory.baseline_name
        or experiment.calibration_sha256 != trajectory.calibration_sha256
    ):
        raise ValueError("experiment and trajectory registered identity differs")
    if experiment.status not in {"scored", "crashed", "abandoned", "rejected"}:
        raise ValueError("experiment document must carry a terminal research status")
    experiment_bytes = canonical_json_bytes(experiment)
    trajectory_bytes = canonical_json_bytes(trajectory)
    values = {
        "experiment_id": experiment.experiment_id,
        "experiment_json": experiment_bytes.decode("utf-8"),
        "experiment_sha256": hashlib.sha256(experiment_bytes).hexdigest(),
        "experiment_blob_sha256": experiment_blob_sha256,
        "trajectory_json": trajectory_bytes.decode("utf-8"),
        "trajectory_sha256": hashlib.sha256(trajectory_bytes).hexdigest(),
        "trajectory_blob_sha256": trajectory_blob_sha256,
        "messages_blob_sha256": trajectory.messages_blob_sha256,
    }
    with engine.begin() as connection:
        connection.execute(text("SELECT lab.lock_run_plan(:run_id)"), {"run_id": experiment.run_id})
        if experiment.status == "abandoned":
            assert_execution_owner_closure_transaction(connection, run_id=experiment.run_id)
        else:
            assert_execution_owner_transaction(connection, run_id=experiment.run_id)
        result = connection.execute(
            text(
                """
                SELECT lab.commit_experiment_documents(
                    :experiment_id,
                    CAST(:experiment_json AS jsonb), :experiment_sha256,
                    :experiment_blob_sha256, CAST(:trajectory_json AS jsonb),
                    :trajectory_sha256, :trajectory_blob_sha256, :messages_blob_sha256
                )
                """
            ),
            values,
        ).scalar_one()
    return dict(result)
