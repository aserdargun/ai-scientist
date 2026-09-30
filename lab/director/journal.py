"""Append-only, resumable Director checkpoints with single-owner fencing."""

from __future__ import annotations

import hashlib
import json
import math
import re
from contextlib import AbstractContextManager
from typing import Any, cast
from uuid import UUID, uuid5

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from lab.director.artifacts import read_director_artifact, store_director_artifact
from lab.director.ownership import (
    assert_execution_owner_closure_transaction,
    assert_execution_owner_transaction,
)
from lab.scorer.jobs import DEFAULT_ARTIFACT_ROOT

_LOCK_NAMESPACE = UUID("a0230b88-0cbb-48f4-94e7-7fc8cf74f899")
_EVENT_NAMESPACE = UUID("cbf0d4bf-c30c-45e5-a2f9-0d8745deebc1")


def canonical_bytes(value: dict[str, Any]) -> bytes:
    """Canonical bounded JSON encoding for trusted checkpoint payloads."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _event_id(run_id: UUID, key: str) -> UUID:
    return uuid5(_EVENT_NAMESPACE, f"{run_id}:{key}")


def _lock_key(run_id: UUID) -> int:
    """Namespace Director ownership apart from the task-plan transaction lock."""
    raw = hashlib.sha256(b"swapp-lab-director-owner-v1:" + run_id.bytes).digest()[:8]
    return int.from_bytes(raw, byteorder="big", signed=True)


class DirectorRunLease(AbstractContextManager["DirectorRunLease"]):
    """Hold one PostgreSQL session advisory lock for a Director run lifetime."""

    def __init__(self, engine: Engine, run_id: UUID) -> None:
        self._engine = engine
        self.run_id = run_id
        self._connection: Connection | None = None
        self._held = False

    def __enter__(self) -> DirectorRunLease:
        connection = self._engine.connect()
        try:
            acquired = bool(
                connection.execute(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": _lock_key(self.run_id)}
                ).scalar_one()
            )
        except Exception:
            connection.close()
            raise
        if not acquired:
            connection.close()
            raise RuntimeError("director_capacity_busy")
        connection.commit()
        self._connection = connection
        self._held = True
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        try:
            if self._held and not connection.invalidated:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": _lock_key(self.run_id)}
                )
        finally:
            self._held = False
            connection.close()

    def heartbeat(self) -> None:
        """Fail closed if the session that fences this Director has been lost."""
        connection = self._connection
        if not self._held or connection is None or connection.invalidated:
            raise RuntimeError("director_ownership_lost")
        try:
            connection.execute(text("SELECT 1")).scalar_one()
            connection.commit()
        except Exception as exc:
            self._drop_owner_connection()
            raise RuntimeError("director_ownership_lost") from exc

    def _drop_owner_connection(self) -> None:
        """Never return a possibly locked session to the SQLAlchemy pool."""
        connection = self._connection
        self._held = False
        self._connection = None
        if connection is not None:
            try:
                connection.invalidate()
            finally:
                connection.close()

    def require_run_active(self) -> None:
        """Reject new Director work unless the owned run is still running."""
        self.heartbeat()
        with self._engine.connect() as connection:
            state = connection.execute(
                text("SELECT state FROM lab.runs WHERE run_id = :run_id"),
                {"run_id": self.run_id},
            ).scalar_one_or_none()
        if state != "running":
            raise RuntimeError("director_run_is_not_active")

    def append_checkpoint(
        self,
        *,
        sequence: int,
        key: str,
        phase: str,
        payload: dict[str, Any],
        artifact_root: Any = DEFAULT_ARTIFACT_ROOT,
    ) -> dict[str, Any]:
        """Store an immutable checkpoint blob and append its digest receipt."""
        return self._append_checkpoint(
            sequence=sequence,
            key=key,
            phase=phase,
            payload=payload,
            artifact_root=artifact_root,
            holdout_closure=False,
        )

    def append_holdout_closure_checkpoint(
        self,
        *,
        sequence: int,
        key: str,
        phase: str,
        payload: dict[str, Any],
        artifact_root: Any = DEFAULT_ARTIFACT_ROOT,
    ) -> dict[str, Any]:
        """Append only an owner-bound, bitless run-end holdout closure receipt."""
        from lab.director.ownership import active_execution_owner

        owner = active_execution_owner()
        if owner is None or owner.run_id != self.run_id:
            raise RuntimeError("holdout closure checkpoint lacks the captured execution owner")
        if not isinstance(payload, dict) or payload.get("run_id") != str(self.run_id):
            raise ValueError("holdout closure checkpoint payload has another run identity")
        if key == "director-holdout-run-end-unavailable-intent:1":
            valid = (
                phase == "holdout_run_end_unavailable_intent"
                and payload.get("schema") == "director-run-end-unavailable-intent.v1"
                and payload.get("admitted_generation") == owner.generation
                and payload.get("execution_sha256") == owner.execution_sha256
                and payload.get("reason")
                in {
                    "fresh_wall_budget_unavailable",
                    "intent_checkpoint_without_sql_registration",
                    "reservation_checkpoint_without_sql_intent",
                }
                and (
                    payload.get("reason") != "reservation_checkpoint_without_sql_intent"
                    or (
                        isinstance(payload.get("reservation_checkpoint_key"), str)
                        and isinstance(payload.get("reservation_checkpoint_sha256"), str)
                        and isinstance(payload.get("reservation_checkpoint_sequence"), int)
                        and not isinstance(payload.get("reservation_checkpoint_sequence"), bool)
                        and isinstance(payload.get("reconciliation_checkpoint_key"), str)
                        and isinstance(payload.get("reconciliation_checkpoint_sha256"), str)
                        and isinstance(payload.get("reconciliation_checkpoint_sequence"), int)
                        and not isinstance(payload.get("reconciliation_checkpoint_sequence"), bool)
                    )
                )
            )
        elif key == "holdout-run-end-unavailable:1":
            valid = (
                phase == "holdout_run_end_unavailable"
                and payload.get("schema") == "director-run-end-unavailable-application.v1"
                and payload.get("admitted_generation") == owner.generation
                and payload.get("execution_sha256") == owner.execution_sha256
                and payload.get("state") == "failed"
                and payload.get("bit") is None
            )
        elif key == "holdout-run-end-admission-failure:1":
            valid = (
                phase == "holdout_run_end_admission_failure"
                and payload.get("schema") == "director-run-end-admission-failure.v1"
                and payload.get("admitted_generation") == owner.generation
                and payload.get("execution_sha256") == owner.execution_sha256
                and payload.get("state") == "failed"
                and payload.get("bit") is None
            )
        elif key == "holdout-application:run_end:1":
            valid = (
                phase == "holdout_application"
                and payload.get("schema") == "director-holdout-application.v1"
                and payload.get("trigger_kind") == "run_end"
                and payload.get("trigger_index") == 1
                and payload.get("admitted_generation") == owner.generation
                and payload.get("execution_sha256") == owner.execution_sha256
                and payload.get("state") == "failed"
                and payload.get("bit") is None
            )
        elif key == "holdout-state-application:run_end:1":
            valid = (
                phase == "holdout_state_application"
                and payload.get("schema") == "director-holdout-state-application.v1"
            )
        elif re.fullmatch(r"holdout-budget-reconciled:[0-9a-f-]{36}", key):
            from lab.director.loop import _budget_from_json

            reservation_id = key.removeprefix("holdout-budget-reconciled:")
            try:
                prior = self.read_checkpoint(
                    key=f"holdout-budget-reserved:{reservation_id}",
                    artifact_root=artifact_root,
                )
                prior_payload = prior.get("payload") if prior is not None else None
                prior_receipt = prior.get("receipt") if prior is not None else None
                prior_budget = (
                    _budget_from_json(prior_payload["budget"])
                    if isinstance(prior_payload, dict)
                    and isinstance(prior_payload.get("budget"), dict)
                    else None
                )
                next_budget_payload = payload.get("budget")
                next_budget = (
                    _budget_from_json(next_budget_payload)
                    if isinstance(next_budget_payload, dict)
                    else None
                )
                intent = self.read_checkpoint(
                    key="director-holdout-run-end-intent", artifact_root=artifact_root
                )
                intent_payload = intent.get("payload") if intent is not None else None
                intent_receipt = intent.get("receipt") if intent is not None else None
                reservation = next(
                    (
                        item
                        for item in (prior_budget.reservations if prior_budget is not None else ())
                        if str(item.reservation_id) == reservation_id
                    ),
                    None,
                )
                remaining_reservations = (
                    tuple(item for item in prior_budget.reservations if item != reservation)
                    if prior_budget is not None and reservation is not None
                    else ()
                )
                valid = (
                    phase == "holdout_budget_reconciled"
                    and payload.get("run_id") == str(self.run_id)
                    and payload.get("trigger_kind") == "run_end"
                    and payload.get("trigger_index") == 1
                    and payload.get("reservation_id") == reservation_id
                    and isinstance(payload.get("candidate_experiment_id"), str)
                    and payload.get("admitted_generation") == owner.generation
                    and payload.get("execution_sha256") == owner.execution_sha256
                    and isinstance(payload.get("reserved_wall_seconds"), int)
                    and not isinstance(payload.get("reserved_wall_seconds"), bool)
                    and isinstance(payload.get("measured_wall_seconds"), (int, float))
                    and not isinstance(payload.get("measured_wall_seconds"), bool)
                    and math.isfinite(payload["measured_wall_seconds"])
                    and payload["measured_wall_seconds"] >= 0
                    and isinstance(prior_payload, dict)
                    and isinstance(prior_receipt, dict)
                    and prior_receipt.get("phase") == "holdout_budget_reserved"
                    and prior_payload.get("run_id") == str(self.run_id)
                    and prior_payload.get("trigger_kind") == "run_end"
                    and prior_payload.get("trigger_index") == 1
                    and prior_payload.get("reservation_id") == reservation_id
                    and prior_payload.get("model_tokens") == 0
                    and reservation is not None
                    and reservation.model_tokens == 0
                    and reservation.wall_seconds == payload.get("reserved_wall_seconds")
                    and prior_budget is not None
                    and next_budget is not None
                    and next_budget.proposal_count == prior_budget.proposal_count
                    and next_budget.model_tokens == prior_budget.model_tokens
                    and math.isclose(
                        next_budget.wall_seconds,
                        prior_budget.wall_seconds + payload["measured_wall_seconds"],
                        rel_tol=0,
                        abs_tol=1e-6,
                    )
                    and next_budget.reserved_wall_seconds
                    == prior_budget.reserved_wall_seconds - reservation.wall_seconds
                    and next_budget.reserved_model_tokens == prior_budget.reserved_model_tokens
                    and next_budget.reservations == remaining_reservations
                    and next_budget.elapsed_wall_seconds >= prior_budget.elapsed_wall_seconds
                    and (
                        (
                            isinstance(intent_receipt, dict)
                            and intent_receipt.get("phase") == "holdout_run_end_intent"
                            and cast(dict[str, Any], intent_payload).get("run_id")
                            == str(self.run_id)
                            and cast(dict[str, Any], intent_payload).get("admitted_generation")
                            == owner.generation
                            and cast(dict[str, Any], intent_payload).get("execution_sha256")
                            == owner.execution_sha256
                        )
                        if intent is not None
                        else (
                            prior_payload.get("trigger_kind") == "run_end"
                            and prior_payload.get("trigger_index") == 1
                            and prior_payload.get("candidate_experiment_id")
                            == payload.get("candidate_experiment_id")
                            and prior_payload.get("admitted_generation") == owner.generation
                            and prior_payload.get("execution_sha256") == owner.execution_sha256
                        )
                    )
                )
            except (KeyError, TypeError, ValueError, OverflowError):
                valid = False
        elif re.fullmatch(r"director-state:[0-9]+(?::holdout:[0-9]+)?:run_end", key):
            # Run-end closure persists the current typed loop document. New execution
            # writes are v2 and carry the pinned strategy state; validate the whole
            # document so this narrow path cannot append a malformed state blob.
            from lab.director.loop import DirectorLoopState

            try:
                state = DirectorLoopState.model_validate_json(
                    json.dumps(payload, allow_nan=False)
                ).verify_consistency()
            except (TypeError, ValueError):
                state = None
            valid = (
                phase == "director_loop_state"
                and state is not None
                and state.schema_version == "director-loop-state.v2"
                and state.run_id == self.run_id
                and state.holdout_last_status == "failed"
                and state.holdout_approved_snapshot is not None
            )
        else:
            valid = False
        if not valid:
            raise ValueError("checkpoint is not an allowed run-end holdout closure receipt")
        return self._append_checkpoint(
            sequence=sequence,
            key=key,
            phase=phase,
            payload=payload,
            artifact_root=artifact_root,
            holdout_closure=True,
        )

    def _validate_closure_application(self, payload: dict[str, Any], artifact_root: Any) -> None:
        """Derive the rollback hash from immutable prior state, never from caller trust."""
        from lab.director.holdout import HoldoutReceipt, RunEndAdmissionFailure
        from lab.director.loop import DirectorLoop, DirectorLoopState

        with self._engine.connect() as connection:
            prior_key = connection.execute(
                text(
                    "SELECT event_json->>'key' FROM lab.run_events "
                    "WHERE run_id=:run_id AND event_type='director.checkpoint' "
                    "AND event_json->>'phase'='director_loop_state' "
                    "AND event_json->>'payload_sha256'=:digest"
                ),
                {"run_id": self.run_id, "digest": payload.get("prior_state_sha256")},
            ).scalar_one()
        prior = self.read_checkpoint(key=prior_key, artifact_root=artifact_root)
        if prior is None:
            raise ValueError("closure application prior state is unavailable")
        state = DirectorLoopState.model_validate_json(
            json.dumps(prior["payload"], allow_nan=False)
        ).verify_consistency()
        if state.run_id != self.run_id or state.champion_experiment_id != payload.get(
            "candidate_experiment_id"
        ):
            raise ValueError("closure application candidate differs from its prior champion")
        budget = DirectorLoop._validated_application_budget(payload)
        if payload.get("schema") == "director-run-end-unavailable-application.v1":
            expected = DirectorLoop._run_end_unavailable_state(state, budget)
        elif payload.get("schema") == "director-run-end-admission-failure.v1":
            failure = RunEndAdmissionFailure(
                run_id=self.run_id,
                candidate_experiment_id=payload["candidate_experiment_id"],
                intent_checkpoint_sha256=payload["intent_checkpoint_sha256"],
                intent_checkpoint_sequence=payload["intent_checkpoint_sequence"],
                budget_checkpoint_key=payload["budget_checkpoint_key"],
                budget_checkpoint_sha256=payload["budget_checkpoint_sha256"],
                failure_kind=payload["failure_kind"],
                admitted_generation=payload["admitted_generation"],
                execution_sha256=payload["execution_sha256"],
            )
            expected, _ = DirectorLoop._record_failure_expected_state(state, failure, payload)
        else:
            failure_receipt = HoldoutReceipt(
                reservation_id=UUID(payload["reservation_id"]),
                state="failed",
                bit=None,
                run_id=self.run_id,
                candidate_experiment_id=payload["candidate_experiment_id"],
                trigger_kind="run_end",
                trigger_index=1,
                admitted_generation=payload["admitted_generation"],
                execution_sha256=payload["execution_sha256"],
            )
            expected = DirectorLoop._state_after_holdout(
                state,
                failure_receipt,
                trigger_kind="run_end",
                trigger_index=1,
                budget_snapshot=budget,
            )
        digest = hashlib.sha256(canonical_bytes(expected.model_dump(mode="json", by_alias=True)))
        if digest.hexdigest() != payload.get("expected_state_sha256"):
            raise ValueError("closure application does not bind the exact approved rollback")

    def _closure_application(self, artifact_root: Any) -> dict[str, Any]:
        """Find the unique durable bitless application authorizing final state writes."""
        applications = [
            checkpoint
            for key in (
                "holdout-run-end-unavailable:1",
                "holdout-run-end-admission-failure:1",
                "holdout-application:run_end:1",
            )
            if (checkpoint := self.read_checkpoint(key=key, artifact_root=artifact_root))
            is not None
        ]
        if len(applications) != 1:
            raise ValueError("closure requires one immutable application receipt")
        application = applications[0]
        payload = application.get("payload", {})
        if payload.get("state") != "failed" or payload.get("bit") is not None:
            raise ValueError("closure application must be failed and bitless")
        return application

    def _append_checkpoint(
        self,
        *,
        sequence: int,
        key: str,
        phase: str,
        payload: dict[str, Any],
        artifact_root: Any,
        holdout_closure: bool,
    ) -> dict[str, Any]:
        """Shared immutable receipt writer; closure has an explicit narrow authorization."""
        if isinstance(sequence, bool) or sequence < 0 or sequence > 1_000_000:
            raise ValueError("checkpoint sequence is outside its bounded range")
        if not key or len(key) > 160 or not phase or len(phase) > 48:
            raise ValueError("checkpoint key or phase is invalid")
        self.heartbeat()
        payload_bytes = canonical_bytes(payload)
        if len(payload_bytes) > 2 * 1024 * 1024:
            raise ValueError("checkpoint payload exceeds the 2 MiB limit")
        digest = hashlib.sha256(payload_bytes).hexdigest()
        receipt = {
            "schema": "director-checkpoint.v1",
            "sequence": sequence,
            "key": key,
            "phase": phase,
            "payload_sha256": digest,
            "blob_sha256": digest,
        }
        if (
            key.startswith("holdout-budget-reserved:")
            and phase == "holdout_budget_reserved"
            and payload.get("trigger_kind") == "run_end"
        ):
            from lab.director.ownership import active_execution_owner

            owner = active_execution_owner()
            reservation_id = key.removeprefix("holdout-budget-reserved:")
            if (
                owner is None
                or owner.run_id != self.run_id
                or payload.get("run_id") != str(self.run_id)
                or payload.get("reservation_id") != reservation_id
                or payload.get("trigger_kind") != "run_end"
                or payload.get("trigger_index") != 1
                or payload.get("model_tokens") != 0
                or isinstance(payload.get("wall_seconds"), bool)
                or not isinstance(payload.get("wall_seconds"), int)
                or payload.get("wall_seconds", 0) < 1
                or payload.get("admitted_generation") != owner.generation
                or payload.get("execution_sha256") != owner.execution_sha256
            ):
                raise ValueError("run-end reservation checkpoint is not bound to its owner")
            receipt["holdout_reservation"] = {
                "reservation_id": reservation_id,
                "trigger_kind": "run_end",
                "trigger_index": 1,
                "candidate_experiment_id": payload.get("candidate_experiment_id"),
                "wall_seconds": payload.get("wall_seconds"),
                "model_tokens": 0,
                "budget": payload.get("budget"),
                "admitted_generation": owner.generation,
                "execution_sha256": owner.execution_sha256,
            }
        elif (
            key == "director-holdout-run-end-unavailable-intent:1"
            and phase == "holdout_run_end_unavailable_intent"
            and payload.get("reason") == "reservation_checkpoint_without_sql_intent"
        ):
            receipt["holdout_reservation_unavailable"] = {
                name: payload.get(name)
                for name in (
                    "run_id",
                    "candidate_experiment_id",
                    "reservation_checkpoint_key",
                    "reservation_checkpoint_sha256",
                    "reservation_checkpoint_sequence",
                    "reconciliation_checkpoint_key",
                    "reconciliation_checkpoint_sha256",
                    "reconciliation_checkpoint_sequence",
                    "budget_snapshot_sha256",
                    "admitted_generation",
                    "execution_sha256",
                )
            }
        elif (
            key == "holdout-run-end-unavailable:1"
            and phase == "holdout_run_end_unavailable"
            and payload.get("reason") == "reservation_checkpoint_without_sql_intent"
        ):
            receipt["holdout_reservation_unavailable_application"] = {
                name: payload.get(name)
                for name in (
                    "run_id",
                    "candidate_experiment_id",
                    "reservation_checkpoint_key",
                    "reservation_checkpoint_sha256",
                    "reservation_checkpoint_sequence",
                    "budget_snapshot_sha256",
                    "admitted_generation",
                    "execution_sha256",
                    "state",
                    "bit",
                )
            }
        elif (
            key == "holdout-application:run_end:1"
            and phase == "holdout_application"
            and holdout_closure
        ):
            from lab.director.ownership import active_execution_owner

            owner = active_execution_owner()
            if (
                owner is None
                or owner.run_id != self.run_id
                or payload.get("run_id") != str(self.run_id)
                or payload.get("trigger_kind") != "run_end"
                or payload.get("trigger_index") != 1
                or payload.get("state") != "failed"
                or payload.get("bit") is not None
                or payload.get("admitted_generation") != owner.generation
                or payload.get("execution_sha256") != owner.execution_sha256
                or not isinstance(payload.get("reservation_id"), str)
                or not isinstance(payload.get("candidate_experiment_id"), str)
                or not isinstance(payload.get("prior_state_sha256"), str)
                or not isinstance(payload.get("budget_snapshot_sha256"), str)
            ):
                raise ValueError("bitless run-end application is not bound to its owner")
            receipt["holdout_application"] = {
                key: payload[key]
                for key in (
                    "run_id",
                    "trigger_kind",
                    "trigger_index",
                    "reservation_id",
                    "candidate_experiment_id",
                    "state",
                    "bit",
                    "prior_state_sha256",
                    "budget_snapshot_sha256",
                    "admitted_generation",
                    "execution_sha256",
                )
            }
        elif (
            key.startswith("holdout-budget-reconciled:")
            and phase == "holdout_budget_reconciled"
            and payload.get("trigger_kind") == "run_end"
        ):
            from lab.director.loop import _budget_from_json
            from lab.director.ownership import active_execution_owner

            owner = active_execution_owner()
            reservation_id = key.removeprefix("holdout-budget-reconciled:")
            reserved = self.read_checkpoint(
                key=f"holdout-budget-reserved:{reservation_id}",
                artifact_root=artifact_root,
            )
            prior_payload = reserved.get("payload") if reserved is not None else None
            after = payload.get("budget")
            before = prior_payload.get("budget") if isinstance(prior_payload, dict) else None
            try:
                before_budget = _budget_from_json(before) if isinstance(before, dict) else None
                after_budget = _budget_from_json(after) if isinstance(after, dict) else None
                receipt_metadata = (
                    reserved.get("receipt", {}).get("holdout_reservation")
                    if reserved is not None
                    else None
                )
                matching = next(
                    (
                        item
                        for item in (
                            before_budget.reservations if before_budget is not None else ()
                        )
                        if str(item.reservation_id) == reservation_id
                    ),
                    None,
                )
                if (
                    owner is None
                    or owner.run_id != self.run_id
                    or not isinstance(receipt_metadata, dict)
                    or receipt_metadata.get("admitted_generation") != owner.generation
                    or receipt_metadata.get("execution_sha256") != owner.execution_sha256
                    or receipt_metadata.get("candidate_experiment_id")
                    != payload.get("candidate_experiment_id")
                    or payload.get("run_id") != str(self.run_id)
                    or payload.get("trigger_kind") != "run_end"
                    or payload.get("trigger_index") != 1
                    or payload.get("reservation_id") != reservation_id
                    or before_budget is None
                    or after_budget is None
                    or matching is None
                    or str(matching.reservation_id) != reservation_id
                    or matching.wall_seconds != payload.get("reserved_wall_seconds")
                    or after_budget.reservations
                    != tuple(item for item in before_budget.reservations if item != matching)
                    or after_budget.proposal_count != before_budget.proposal_count
                    or after_budget.model_tokens != before_budget.model_tokens
                    or after_budget.reserved_model_tokens != before_budget.reserved_model_tokens
                    or after_budget.reserved_wall_seconds
                    != before_budget.reserved_wall_seconds - matching.wall_seconds
                    or (
                        holdout_closure
                        and after_budget.wall_seconds
                        < before_budget.wall_seconds + matching.wall_seconds
                    )
                    or not math.isclose(
                        after_budget.wall_seconds,
                        before_budget.wall_seconds + payload["measured_wall_seconds"],
                        rel_tol=0,
                        abs_tol=1e-6,
                    )
                    or after_budget.elapsed_wall_seconds < before_budget.elapsed_wall_seconds
                ):
                    raise ValueError("run-end budget reconciliation differs from its reservation")
                receipt["holdout_budget_reconciliation"] = {
                    "reservation_id": reservation_id,
                    "trigger_kind": "run_end",
                    "trigger_index": 1,
                    "candidate_experiment_id": payload.get("candidate_experiment_id"),
                    "admitted_generation": owner.generation,
                    "execution_sha256": owner.execution_sha256,
                    "reserved_wall_seconds": matching.wall_seconds,
                    "measured_wall_seconds": payload.get("measured_wall_seconds"),
                    "budget_snapshot_sha256": hashlib.sha256(
                        canonical_bytes(cast(dict[str, Any], after))
                    ).hexdigest(),
                    "proposal_count": after_budget.proposal_count,
                    "proposal_count_before": before_budget.proposal_count,
                    "proposal_count_after": after_budget.proposal_count,
                    "model_tokens": matching.model_tokens,
                    "model_tokens_before": before_budget.model_tokens,
                    "model_tokens_after": after_budget.model_tokens,
                    "wall_seconds_before": before_budget.wall_seconds,
                    "wall_seconds_after": after_budget.wall_seconds,
                    "elapsed_wall_seconds_before": before_budget.elapsed_wall_seconds,
                    "elapsed_wall_seconds_after": after_budget.elapsed_wall_seconds,
                    "reserved_wall_seconds_before": before_budget.reserved_wall_seconds,
                    "reserved_wall_seconds_after": after_budget.reserved_wall_seconds,
                    "reserved_model_tokens_before": before_budget.reserved_model_tokens,
                    "reserved_model_tokens_after": after_budget.reserved_model_tokens,
                    "reservations_before": cast(dict[str, Any], before)["reservations"],
                    "reservations_after": cast(dict[str, Any], after)["reservations"],
                    "reservation_ids_before": sorted(
                        str(item.reservation_id) for item in before_budget.reservations
                    ),
                    "reservation_ids_after": sorted(
                        str(item.reservation_id) for item in after_budget.reservations
                    ),
                }
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise ValueError("run-end budget reconciliation checkpoint is invalid") from exc
        if holdout_closure:
            from lab.director.ownership import active_execution_owner

            owner = active_execution_owner()
            if owner is None:
                raise RuntimeError("holdout closure owner disappeared")
            receipt["holdout_closure_owner"] = {
                "admitted_generation": owner.generation,
                "execution_sha256": owner.execution_sha256,
            }
            if phase == "holdout_run_end_unavailable_intent":
                receipt["holdout_unavailable_freeze"] = {
                    name: payload.get(name)
                    for name in (
                        "run_id",
                        "candidate_experiment_id",
                        "reason",
                        "prior_state_key",
                        "prior_state_sha256",
                        "prior_state_sequence",
                        "budget_snapshot_sha256",
                        "original_intent_checkpoint_sha256",
                        "original_intent_checkpoint_sequence",
                        "remaining_wall_seconds",
                        "admitted_generation",
                        "execution_sha256",
                    )
                }
            if phase in {
                "holdout_run_end_unavailable",
                "holdout_run_end_admission_failure",
                "holdout_application",
            }:
                self._validate_closure_application(payload, artifact_root)
                expected_sha = payload.get("expected_state_sha256")
                if not isinstance(expected_sha, str) or not re.fullmatch(
                    r"[0-9a-f]{64}", expected_sha
                ):
                    raise ValueError("closure application lacks its exact resulting state digest")
                receipt["holdout_closure_application"] = {
                    name: payload.get(name)
                    for name in (
                        "run_id",
                        "candidate_experiment_id",
                        "prior_state_sha256",
                        "expected_state_sha256",
                        "budget_snapshot_sha256",
                        "state",
                        "bit",
                        "admitted_generation",
                        "execution_sha256",
                        "reason",
                        "unavailable_checkpoint_sha256",
                        "unavailable_checkpoint_sequence",
                        "intent_checkpoint_sha256",
                        "intent_checkpoint_sequence",
                        "budget_checkpoint_key",
                        "budget_checkpoint_sha256",
                    )
                }
            if phase == "director_loop_state":
                application = self._closure_application(artifact_root)
                app_payload = application["payload"]
                if app_payload.get("expected_state_sha256") != digest:
                    raise ValueError("closure state differs from its approved rollback application")
                receipt["holdout_closure_state"] = {
                    "application_sha256": application["receipt"]["payload_sha256"],
                    "state_sha256": digest,
                }
            if phase == "holdout_state_application":
                application = self._closure_application(artifact_root)
                if (
                    payload.get("application_sha256") != application["receipt"]["payload_sha256"]
                    or payload.get("state_sha256")
                    != application["payload"]["expected_state_sha256"]
                ):
                    raise ValueError("closure marker differs from its exact application and state")
                receipt["holdout_closure_marker"] = {
                    "application_sha256": payload["application_sha256"],
                    "state_sha256": payload["state_sha256"],
                }
        event_id = _event_id(self.run_id, key)
        connection = self._connection
        if not self._held or connection is None:
            raise RuntimeError("director_ownership_lost")
        try:
            with connection.begin():
                connection.execute(
                    text("SELECT lab.lock_run_plan(:run_id)"), {"run_id": self.run_id}
                )
                if holdout_closure:
                    owner = assert_execution_owner_closure_transaction(
                        connection, run_id=self.run_id
                    )
                    connection.execute(
                        text(
                            "SELECT lab.assert_director_holdout_checkpoint(:run_id,:generation,"
                            ":invocation_id,:execution_sha256,:key,:phase,:sequence,:payload_sha,"
                            "CAST(:metadata AS jsonb))"
                        ),
                        {
                            "run_id": self.run_id,
                            "generation": owner.generation,
                            "invocation_id": owner.invocation_id,
                            "execution_sha256": owner.execution_sha256,
                            "key": key,
                            "phase": phase,
                            "sequence": sequence,
                            "payload_sha": digest,
                            "metadata": json.dumps(
                                receipt,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        },
                    )
                else:
                    assert_execution_owner_transaction(connection, run_id=self.run_id)
                run_state = connection.execute(
                    text("SELECT state FROM lab.runs WHERE run_id = :run_id FOR UPDATE"),
                    {"run_id": self.run_id},
                ).scalar_one_or_none()
                if holdout_closure:
                    if run_state not in {"running", "stop_requested", "stopped"}:
                        raise RuntimeError("holdout closure checkpoint run is no longer closable")
                elif run_state != "running":
                    raise RuntimeError("director_run_is_not_active")
                existing = (
                    connection.execute(
                        text(
                            "SELECT event_type, event_json FROM lab.run_events "
                            "WHERE event_id = :event_id"
                        ),
                        {"event_id": event_id},
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing is not None:
                    if (
                        existing["event_type"] != "director.checkpoint"
                        or existing["event_json"] != receipt
                    ):
                        raise RuntimeError("checkpoint key conflicts with a different payload")
                    if read_director_artifact(digest, artifact_root=artifact_root) != payload_bytes:
                        raise RuntimeError("checkpoint blob failed read-back verification")
                    return receipt
                previous = connection.execute(
                    text(
                        "SELECT max((event_json->>'sequence')::integer) "
                        "FROM lab.run_events WHERE run_id = :run_id "
                        "AND event_type = 'director.checkpoint'"
                    ),
                    {"run_id": self.run_id},
                ).scalar_one()
                expected = 0 if previous is None else int(previous) + 1
                if sequence != expected:
                    raise RuntimeError("checkpoint sequence is not the next durable boundary")
                # The run row lock serializes this bounded artifact write with
                # the API stop transition. A stopped run cannot leave a new
                # unreferenced checkpoint blob behind.
                blob_digest = store_director_artifact(payload_bytes, artifact_root=artifact_root)
                if blob_digest != digest:
                    raise RuntimeError("checkpoint blob digest differs from the payload")
                if (
                    read_director_artifact(blob_digest, artifact_root=artifact_root)
                    != payload_bytes
                ):
                    raise RuntimeError("checkpoint blob failed read-back verification")
                connection.execute(
                    text(
                        "INSERT INTO lab.run_events(event_id, run_id, event_type, event_json) "
                        "VALUES (:event_id, :run_id, 'director.checkpoint', "
                        "CAST(:event_json AS jsonb))"
                    ),
                    {
                        "event_id": event_id,
                        "run_id": self.run_id,
                        "event_json": json.dumps(receipt, sort_keys=True, separators=(",", ":")),
                    },
                )
        except Exception:
            if connection.invalidated:
                self._drop_owner_connection()
            raise
        return receipt

    def read_checkpoint(
        self, *, key: str, artifact_root: Any = DEFAULT_ARTIFACT_ROOT
    ) -> dict[str, Any] | None:
        """Read a checkpoint only after receipt, blob, and canonical hashes agree."""
        self.heartbeat()
        connection = self._connection
        if connection is None:
            raise RuntimeError("director_ownership_lost")
        row = (
            connection.execute(
                text(
                    "SELECT event_type, event_json FROM lab.run_events WHERE event_id = :event_id"
                ),
                {"event_id": _event_id(self.run_id, key)},
            )
            .mappings()
            .one_or_none()
        )
        connection.commit()
        if row is None:
            return None
        receipt = row["event_json"]
        if row["event_type"] != "director.checkpoint" or not isinstance(receipt, dict):
            raise ValueError("checkpoint event has an invalid envelope")
        digest = receipt.get("payload_sha256")
        blob_digest = receipt.get("blob_sha256")
        if not isinstance(digest, str) or digest != blob_digest:
            raise ValueError("checkpoint receipt digest is invalid")
        payload_bytes = read_director_artifact(blob_digest, artifact_root=artifact_root)
        if hashlib.sha256(payload_bytes).hexdigest() != digest:
            raise ValueError("checkpoint payload hash differs from its receipt")
        payload = json.loads(payload_bytes)
        if canonical_bytes(payload) != payload_bytes:
            raise ValueError("checkpoint payload is not canonical JSON")
        return {"receipt": receipt, "payload": payload}
