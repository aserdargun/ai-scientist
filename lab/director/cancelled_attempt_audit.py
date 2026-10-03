"""Read-only conservative audit of model reservations at verified stop closure.

This is evidence inside the existing recovery receipt, never a budget checkpoint
or a refund. Missing provider completion leaves actual consumption unknown.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from lab.director.journal import DirectorRunLease
from lab.director.ownership import ExecutionOwner


def _reference(checkpoint: dict[str, Any], *, key: str, run_id: UUID) -> dict[str, Any]:
    receipt = checkpoint["receipt"]
    payload = checkpoint["payload"]
    digest = receipt.get("payload_sha256")
    if (
        receipt.get("key") != key
        or payload.get("run_id") != str(run_id)
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or receipt.get("blob_sha256") != digest
    ):
        raise ValueError("canceled model audit checkpoint identity is inconsistent")
    return {"key": key, "payload_sha256": digest, "sequence": receipt["sequence"]}


def _positive_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("canceled model audit bound is not a positive integer")
    return value


def cancelled_attempt_audit(
    director: Engine,
    *,
    lease: DirectorRunLease,
    owner: ExecutionOwner,
    request: dict[str, Any],
    artifact_root: Path,
) -> dict[str, Any]:
    """Link validated outstanding reservations without changing their counters.

    The caller captures this before finalization while holding the run lease.
    Persistence rechecks this exact owner in the recovery receipt transaction.
    An unavailable audit is explicit and does not obstruct physical cleanup.
    """
    audit: dict[str, Any] = {
        "schema": "cancelled-model-budget-audit.v1",
        "run_id": str(owner.run_id),
        "generation": owner.generation,
        "invocation_id": owner.invocation_id,
        "execution_sha256": owner.execution_sha256,
        "accounting_mutated": False,
        "conservation_verified": False,
        "actual_model_tokens": None,
        "actual_model_wall_seconds": None,
        "refund_model_tokens": 0,
        "refund_wall_seconds": 0,
        "retained_reservations": [],
    }
    try:
        budget = request["budget"]
        token_cap = _positive_integer(budget["model_tokens"])
        wall_cap = _positive_integer(budget["wall_seconds"])
        with director.connect() as connection:
            keys = connection.execute(
                text(
                    "SELECT event_json->>'key' FROM lab.run_events WHERE run_id=:run_id "
                    "AND event_type='director.checkpoint' "
                    "AND event_json->>'phase'='proposal_budget_reserved' "
                    "ORDER BY (event_json->>'sequence')::integer LIMIT 36"
                ),
                {"run_id": owner.run_id},
            ).scalars().all()
        if len(keys) > 35 or len(set(keys)) != len(keys):
            raise ValueError("canceled model audit reservation inventory is unbounded")
        retained: list[dict[str, Any]] = []
        for key in keys:
            match = re.fullmatch(r"proposal-budget-reservation:([0-9a-f-]{36}):([0-9]+)", key)
            if match is None or match[1] != str(owner.run_id):
                raise ValueError("canceled model audit reservation belongs to another run")
            ordinal = int(match[2])
            checkpoint = lease.read_checkpoint(key=key, artifact_root=artifact_root)
            if checkpoint is None:
                raise ValueError("canceled model audit reservation artifact is absent")
            reference = _reference(checkpoint, key=key, run_id=owner.run_id)
            payload = checkpoint["payload"]
            if payload.get("ordinal") != ordinal:
                raise ValueError("canceled model audit reservation ordinal differs")
            reconciled_key = f"proposal-budget-reconciled:{owner.run_id}:{ordinal}"
            reconciled = lease.read_checkpoint(key=reconciled_key, artifact_root=artifact_root)
            if reconciled is not None:
                _reference(reconciled, key=reconciled_key, run_id=owner.run_id)
                continue
            tokens = _positive_integer(payload["model_tokens"])
            wall = _positive_integer(payload["wall_seconds"])
            reservation_id = str(UUID(payload["reservation_id"]))
            with director.connect() as connection:
                attempt_keys = connection.execute(
                    text(
                        "SELECT event_json->>'key' FROM lab.run_events WHERE run_id=:run_id "
                        "AND event_type='director.checkpoint' "
                        "AND event_json->>'phase'='provider_attempt' "
                        "AND event_json->>'key' LIKE :prefix "
                        "ORDER BY (event_json->>'sequence')::integer LIMIT 17"
                    ),
                    {"run_id": owner.run_id, "prefix": f"provider-attempt:{ordinal}:%"},
                ).scalars().all()
            if len(attempt_keys) > 16:
                raise ValueError("canceled model audit provider inventory is unbounded")
            attempts = []
            for attempt_key in attempt_keys:
                attempt_match = re.fullmatch(
                    rf"provider-attempt:{ordinal}:([0-7]):(started|completed)", attempt_key
                )
                attempt = lease.read_checkpoint(key=attempt_key, artifact_root=artifact_root)
                if (
                    attempt_match is None
                    or attempt is None
                    or attempt["payload"].get("ordinal") != ordinal
                    or attempt["payload"].get("attempt_index") != int(attempt_match[1])
                ):
                    raise ValueError("canceled model audit provider identity differs")
                attempts.append(_reference(attempt, key=attempt_key, run_id=owner.run_id))
            retained.append({
                "reservation_id": reservation_id,
                "ordinal": ordinal,
                "model_tokens": tokens,
                "wall_seconds": wall,
                "reservation_checkpoint": reference,
                "provider_checkpoints": attempts,
                "disposition": "retained_unknown_consumption",
                "actual_model_tokens": None,
                "actual_model_wall_seconds": None,
            })
        if len({item["reservation_id"] for item in retained}) != len(retained):
            raise ValueError("canceled model audit reservation identities are duplicated")
        if (
            sum(item["model_tokens"] for item in retained) > token_cap
            or sum(item["wall_seconds"] for item in retained) > wall_cap
        ):
            raise ValueError("canceled model audit retained bounds exceed the immutable run cap")
        audit.update(
            state="recorded",
            immutable_model_token_cap=token_cap,
            immutable_wall_second_cap=wall_cap,
            retained_reservations=retained,
            retained_bounds_within_run_caps=True,
        )
    except (KeyError, TypeError, ValueError, RuntimeError, OSError, SQLAlchemyError) as exc:
        audit.update(state="unavailable", reason=type(exc).__name__)
    return audit
