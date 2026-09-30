"""Hash-verified Director documents backed by the shared bounded blob store."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

from lab.db.task_plan import run_plan_lock_key
from lab.director.baselines import (
    FrozenCalibrationDocument,
    calibration_sha256,
    canonical_calibration_bytes,
    canonical_calibration_payload_bytes,
)
from lab.director.ownership import assert_execution_owner_transaction
from lab.scorer.jobs import DEFAULT_ARTIFACT_ROOT, read_artifact_bytes, store_artifact_bytes


def store_director_artifact(payload: bytes, *, artifact_root: Path = DEFAULT_ARTIFACT_ROOT) -> str:
    """Store bounded opaque Director bytes using the shared quota and digest rules."""
    return store_artifact_bytes(payload, artifact_root=artifact_root)


def read_director_artifact(digest: str, *, artifact_root: Path = DEFAULT_ARTIFACT_ROOT) -> bytes:
    """Read bounded opaque Director bytes and verify their content digest."""
    return read_artifact_bytes(digest, artifact_root=artifact_root)


def store_frozen_calibration(
    document: FrozenCalibrationDocument, *, artifact_root: Path = DEFAULT_ARTIFACT_ROOT
) -> tuple[str, str]:
    """Persist canonical calibration bytes and return identical content/blob digests."""
    payload = canonical_calibration_bytes(document)
    digest = calibration_sha256(document)
    blob_digest = store_director_artifact(payload, artifact_root=artifact_root)
    if blob_digest != digest:
        raise RuntimeError("stored calibration digest differs from canonical document hash")
    return digest, blob_digest


def register_frozen_calibration(
    engine: Engine,
    document: FrozenCalibrationDocument,
    *,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
) -> dict[str, Any]:
    """Write actual canonical bytes, then atomically pin one run-scoped receipt."""
    calibration_digest, blob_digest = store_frozen_calibration(
        document, artifact_root=artifact_root
    )
    with engine.begin() as connection:
        connection.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": run_plan_lock_key(document.run_id)},
        )
        assert_execution_owner_transaction(connection, run_id=document.run_id)
        result = connection.execute(
            text(
                """
                SELECT lab.register_baseline_calibration(
                    :run_id, :suite_id, :suite_version, :calibration_sha256,
                    :blob_sha256, :task_count
                )
                """
            ),
            {
                "run_id": document.run_id,
                "suite_id": document.suite_id,
                "suite_version": document.suite_version,
                "calibration_sha256": calibration_digest,
                "blob_sha256": blob_digest,
                "task_count": len(document.tasks),
            },
        ).scalar_one()
    if not isinstance(result, dict):
        raise ValueError("database returned an invalid calibration receipt")
    return result


def read_registered_calibration(
    engine: Engine,
    *,
    run_id: UUID,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
) -> FrozenCalibrationDocument:
    """Read calibration only through the Director receipt, then hash-check bytes."""
    with engine.connect() as connection:
        receipt = connection.execute(
            text("SELECT lab.baseline_calibration_receipt(:run_id)"),
            {"run_id": run_id},
        ).scalar_one()
    if not isinstance(receipt, dict):
        raise ValueError("database returned an invalid calibration receipt")
    calibration_digest = receipt.get("calibration_sha256")
    blob_digest = receipt.get("blob_sha256")
    if (
        not isinstance(calibration_digest, str)
        or not isinstance(blob_digest, str)
        or calibration_digest != blob_digest
    ):
        raise ValueError("calibration receipt digests do not identify the same canonical bytes")
    payload = read_director_artifact(blob_digest, artifact_root=artifact_root)
    from pydantic import ValidationError

    try:
        document = FrozenCalibrationDocument.model_validate_json(payload, strict=True)
    except ValidationError as exc:
        raise ValueError("stored calibration document does not match its strict schema") from exc
    if (
        document.run_id != run_id
        or document.suite_id != receipt.get("suite_id")
        or document.suite_version != receipt.get("suite_version")
        or len(document.tasks) != receipt.get("task_count")
        or hashlib.sha256(payload).hexdigest() != calibration_digest
    ):
        raise ValueError("stored calibration document differs from its DB receipt")
    if canonical_calibration_payload_bytes(payload) != payload:
        raise ValueError("stored calibration bytes are not the canonical JSON encoding")
    return document
