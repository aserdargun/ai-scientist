"""Director-facing, one-bit holdout admission and receipt API."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from lab.db.task_plan import run_plan_lock_key
from lab.director.ownership import (
    active_execution_owner,
    assert_execution_owner_closure_transaction,
    assert_execution_owner_transaction,
)

HoldoutState = Literal["reserved", "running", "passed", "reverted", "failed", "exhausted"]


def _budget_is_exhausted(budget: dict[str, object], *, wall_limit: int, token_limit: int) -> bool:
    """Mirror RunBudget admission: either remaining wall or tokens below one."""
    if (
        isinstance(wall_limit, bool)
        or not isinstance(wall_limit, int)
        or wall_limit < 1
        or isinstance(token_limit, bool)
        or not isinstance(token_limit, int)
        or token_limit < 0
    ):
        raise ValueError("immutable request budget limits are invalid")
    wall_names = ("wall_seconds", "elapsed_wall_seconds", "reserved_wall_seconds")
    token_names = ("model_tokens", "reserved_model_tokens")
    values = {name: budget.get(name) for name in (*wall_names, *token_names)}
    for name in wall_names:
        value = values[name]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError("terminal state has invalid durable wall budget accounting")
    for name in token_names:
        value = values[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("terminal state has invalid durable token accounting")
    wall_used = max(
        float(cast(int | float, values["wall_seconds"])),
        float(cast(int | float, values["elapsed_wall_seconds"])),
    )
    wall_remaining = (
        wall_limit - wall_used - float(cast(int | float, values["reserved_wall_seconds"]))
    )
    token_remaining = (
        token_limit - cast(int, values["model_tokens"]) - cast(int, values["reserved_model_tokens"])
    )
    return wall_remaining < 1 or token_remaining < 1


def _validated_abandonment_claims(
    lease: Any,
    *,
    run_id: UUID,
    proposal_ordinals: set[int],
    completed_proposals: int,
    artifact_root: Any,
) -> list[dict[str, object]]:
    """Read every non-experiment ordinal from its hash-verified durable checkpoint."""
    if any(
        isinstance(ordinal, bool)
        or not isinstance(ordinal, int)
        or ordinal < 1
        or ordinal > completed_proposals
        for ordinal in proposal_ordinals
    ):
        raise ValueError("proposal ledger contains an ordinal beyond terminal loop state")
    claims: list[dict[str, object]] = []
    for ordinal in range(1, completed_proposals + 1):
        if ordinal in proposal_ordinals:
            continue
        checkpoint = lease.read_checkpoint(
            key=f"proposal-abandoned:{ordinal}", artifact_root=artifact_root
        )
        if checkpoint is None:
            raise ValueError(f"proposal ordinal {ordinal} has no terminal receipt")
        payload = checkpoint.get("payload")
        receipt = checkpoint.get("receipt")
        if (
            not isinstance(payload, dict)
            or payload.get("run_id") != str(run_id)
            or payload.get("ordinal") != ordinal
            or payload.get("reason")
            not in {"tool_parse", "model_request", "budget_exhausted", "provider_error"}
            or not isinstance(receipt, dict)
            or receipt.get("phase") != "proposal_abandoned"
        ):
            raise ValueError(f"proposal abandonment {ordinal} is not a validated checkpoint")
        digest = receipt.get("payload_sha256")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise ValueError(f"proposal abandonment {ordinal} has no verified digest")
        claims.append(
            {
                "ordinal": ordinal,
                "key": f"proposal-abandoned:{ordinal}",
                "payload_sha256": digest,
            }
        )
    return claims


@dataclass(frozen=True, slots=True)
class RunEndIntent:
    """Verified terminal loop state bound to its immutable run budget."""

    candidate_experiment_id: str
    candidate_sha256: str
    state_checkpoint_sha256: str
    intent_checkpoint_sha256: str
    terminal_status: str


@dataclass(frozen=True, slots=True)
class HoldoutReceipt:
    """The only holdout result surface visible to Director code."""

    reservation_id: UUID
    state: HoldoutState
    bit: bool | None
    run_id: UUID | None = None
    candidate_experiment_id: str | None = None
    trigger_kind: Literal["keep_interval", "run_end"] | None = None
    trigger_index: int | None = None
    admitted_generation: int | None = None
    execution_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.state in {"passed", "reverted"}:
            if type(self.bit) is not bool:
                raise ValueError("terminal holdout receipt requires exactly one boolean bit")
            if self.bit != (self.state == "passed"):
                raise ValueError("holdout state and privacy bit disagree")
        elif self.bit is not None:
            raise ValueError("non-terminal holdout receipt cannot expose a result bit")
        identity = (
            self.run_id,
            self.candidate_experiment_id,
            self.trigger_kind,
            self.trigger_index,
        )
        if any(value is not None for value in identity) and not all(
            value is not None for value in identity
        ):
            raise ValueError("holdout receipt identity must be complete or absent")
        if self.run_id is not None:
            if not isinstance(self.run_id, UUID):
                raise TypeError("holdout receipt run id must be a UUID")
            if (
                not isinstance(self.candidate_experiment_id, str)
                or not self.candidate_experiment_id
            ):
                raise ValueError("holdout receipt candidate identity is invalid")
            if self.trigger_kind not in {"keep_interval", "run_end"}:
                raise ValueError("holdout receipt trigger kind is invalid")
            if (
                isinstance(self.trigger_index, bool)
                or not isinstance(self.trigger_index, int)
                or self.trigger_index < 1
            ):
                raise ValueError("holdout receipt trigger index is invalid")
        if (self.admitted_generation is None) != (self.execution_sha256 is None):
            raise ValueError("holdout receipt owner identity must be complete or absent")
        if self.admitted_generation is not None and (
            isinstance(self.admitted_generation, bool)
            or not isinstance(self.admitted_generation, int)
            or self.admitted_generation < 1
            or not isinstance(self.execution_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.execution_sha256) is None
        ):
            raise ValueError("holdout receipt owner identity is malformed")


@dataclass(frozen=True, slots=True)
class RunEndAdmissionFailure:
    """Bitless operational fence for an intent with no proven SQL reservation."""

    run_id: UUID
    candidate_experiment_id: str
    intent_checkpoint_sha256: str
    intent_checkpoint_sequence: int
    budget_checkpoint_key: str
    budget_checkpoint_sha256: str
    failure_kind: Literal["missing_reservation_unverifiable_budget"]
    state: Literal["failed"] = "failed"
    bit: None = None
    admitted_generation: int | None = None
    execution_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, UUID):
            raise TypeError("run-end failure run id must be a UUID")
        if not isinstance(self.candidate_experiment_id, str) or not self.candidate_experiment_id:
            raise ValueError("run-end failure candidate identity is invalid")
        for label, value in (
            ("intent", self.intent_checkpoint_sha256),
            ("budget", self.budget_checkpoint_sha256),
        ):
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(f"run-end failure {label} digest is invalid")
        if (
            isinstance(self.intent_checkpoint_sequence, bool)
            or not isinstance(self.intent_checkpoint_sequence, int)
            or self.intent_checkpoint_sequence < 0
        ):
            raise ValueError("run-end failure intent sequence is invalid")
        if not isinstance(self.budget_checkpoint_key, str) or not self.budget_checkpoint_key:
            raise ValueError("run-end failure budget checkpoint key is invalid")
        if (
            self.failure_kind != "missing_reservation_unverifiable_budget"
            or self.state != "failed"
            or self.bit is not None
        ):
            raise ValueError("run-end admission failure must be bitless and terminal")
        _validate_owner_pair(self.admitted_generation, self.execution_sha256)


@dataclass(frozen=True, slots=True)
class RunEndUnavailable:
    """Bitless terminal fence when a run-end check cannot safely be admitted."""

    run_id: UUID
    candidate_experiment_id: str
    reason: Literal[
        "fresh_wall_budget_unavailable",
        "intent_checkpoint_without_sql_registration",
        "reservation_checkpoint_without_sql_intent",
    ]
    prior_state_key: str
    prior_state_sha256: str
    prior_state_sequence: int
    budget_snapshot_sha256: str
    unavailable_checkpoint_sha256: str
    unavailable_checkpoint_sequence: int
    original_intent_checkpoint_sha256: str | None = None
    original_intent_checkpoint_sequence: int | None = None
    reservation_checkpoint_key: str | None = None
    reservation_checkpoint_sha256: str | None = None
    reservation_checkpoint_sequence: int | None = None
    state: Literal["failed"] = "failed"
    bit: None = None
    admitted_generation: int | None = None
    execution_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, UUID):
            raise TypeError("run-end unavailable run id must be a UUID")
        if not isinstance(self.candidate_experiment_id, str) or not self.candidate_experiment_id:
            raise ValueError("run-end unavailable candidate identity is invalid")
        if self.reason not in {
            "fresh_wall_budget_unavailable",
            "intent_checkpoint_without_sql_registration",
            "reservation_checkpoint_without_sql_intent",
        }:
            raise ValueError("run-end unavailable reason is invalid")
        for label, value in (
            ("prior state", self.prior_state_sha256),
            ("budget snapshot", self.budget_snapshot_sha256),
            ("unavailable checkpoint", self.unavailable_checkpoint_sha256),
        ):
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(f"run-end unavailable {label} digest is invalid")
        if not isinstance(self.prior_state_key, str) or not self.prior_state_key:
            raise ValueError("run-end unavailable prior state key is invalid")
        for sequence_name, sequence_value in (
            ("prior state", self.prior_state_sequence),
            ("unavailable", self.unavailable_checkpoint_sequence),
        ):
            if (
                isinstance(sequence_value, bool)
                or not isinstance(sequence_value, int)
                or sequence_value < 0
            ):
                raise ValueError(f"run-end unavailable {sequence_name} sequence is invalid")
        optional = (
            self.original_intent_checkpoint_sha256,
            self.original_intent_checkpoint_sequence,
        )
        if (optional[0] is None) != (optional[1] is None):
            raise ValueError("run-end unavailable original intent identity is incomplete")
        if optional[0] is not None and (
            not isinstance(optional[0], str)
            or not re.fullmatch(r"[0-9a-f]{64}", optional[0])
            or isinstance(optional[1], bool)
            or not isinstance(optional[1], int)
            or optional[1] < 0
        ):
            raise ValueError("run-end unavailable original intent identity is invalid")
        if (self.reason == "intent_checkpoint_without_sql_registration") != (
            optional[0] is not None
        ):
            raise ValueError("run-end unavailable reason does not match intent identity")
        reservation = (
            self.reservation_checkpoint_key,
            self.reservation_checkpoint_sha256,
            self.reservation_checkpoint_sequence,
        )
        if self.reason == "reservation_checkpoint_without_sql_intent":
            if (
                not isinstance(reservation[0], str)
                or re.fullmatch(r"holdout-budget-reserved:[0-9a-f-]{36}", reservation[0]) is None
                or not isinstance(reservation[1], str)
                or re.fullmatch(r"[0-9a-f]{64}", reservation[1]) is None
                or isinstance(reservation[2], bool)
                or not isinstance(reservation[2], int)
                or reservation[2] < 0
                or optional[0] is not None
            ):
                raise ValueError("reservation-only run-end unavailable identity is invalid")
        elif any(value is not None for value in reservation):
            raise ValueError("reservation checkpoint is only valid for reservation-only closure")
        if self.state != "failed" or self.bit is not None:
            raise ValueError("run-end unavailable receipt must be bitless and terminal")
        _validate_owner_pair(self.admitted_generation, self.execution_sha256)


def _validate_owner_pair(generation: object, execution_sha256: object) -> None:
    if (generation is None) != (execution_sha256 is None):
        raise ValueError("holdout owner identity must be complete or absent")
    if generation is not None and (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
        or not isinstance(execution_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", execution_sha256) is None
    ):
        raise ValueError("holdout owner identity is malformed")


def fence_run_end_unavailable(
    engine: Engine,
    *,
    run_id: UUID,
    candidate_experiment_id: str,
    reason: str,
    prior_state_key: str,
    prior_state_sha256: str,
    prior_state_sequence: int,
    budget_snapshot_sha256: str,
    unavailable_checkpoint_sha256: str,
    unavailable_checkpoint_sequence: int,
    remaining_wall_seconds: float,
    original_intent_checkpoint_sha256: str | None = None,
    original_intent_checkpoint_sequence: int | None = None,
) -> RunEndUnavailable:
    """Persist an immutable no-admission outcome without touching quota/results."""
    if not isinstance(run_id, UUID):
        raise TypeError("run id must be a UUID")
    with engine.begin() as connection:
        connection.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": run_plan_lock_key(run_id)},
        )
        owner = assert_execution_owner_closure_transaction(connection, run_id=run_id)
        payload = connection.execute(
            text(
                "SELECT lab.fence_run_end_unavailable(:run_id,:candidate,:reason,"
                ":state_key,:state_sha,:state_seq,:budget_sha,:checkpoint_sha,"
                ":checkpoint_seq,:remaining_wall,:intent_sha,:intent_seq,:generation,"
                ":invocation_id,:execution_sha256)"
            ),
            {
                "run_id": run_id,
                "candidate": candidate_experiment_id,
                "reason": reason,
                "state_key": prior_state_key,
                "state_sha": prior_state_sha256,
                "state_seq": prior_state_sequence,
                "budget_sha": budget_snapshot_sha256,
                "checkpoint_sha": unavailable_checkpoint_sha256,
                "checkpoint_seq": unavailable_checkpoint_sequence,
                "remaining_wall": remaining_wall_seconds,
                "intent_sha": original_intent_checkpoint_sha256,
                "intent_seq": original_intent_checkpoint_sequence,
                "generation": owner.generation,
                "invocation_id": owner.invocation_id,
                "execution_sha256": owner.execution_sha256,
            },
        ).scalar_one()
    return _parse_run_end_unavailable(payload)


def fence_run_end_reserved_budget_without_intent(
    engine: Engine,
    *,
    run_id: UUID,
    candidate_experiment_id: str,
    prior_state_key: str,
    prior_state_sha256: str,
    prior_state_sequence: int,
    budget_snapshot_sha256: str,
    unavailable_checkpoint_sha256: str,
    unavailable_checkpoint_sequence: int,
    reservation_checkpoint_key: str,
    reservation_checkpoint_sha256: str,
    reservation_checkpoint_sequence: int,
    reconciliation_checkpoint_key: str,
    reconciliation_checkpoint_sha256: str,
    reconciliation_checkpoint_sequence: int,
) -> RunEndUnavailable:
    """Freeze an unadmitted run-end reservation after its conservative charge."""
    if not isinstance(run_id, UUID):
        raise TypeError("run id must be a UUID")
    with engine.begin() as connection:
        connection.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": run_plan_lock_key(run_id)},
        )
        owner = assert_execution_owner_closure_transaction(connection, run_id=run_id)
        payload = connection.execute(
            text(
                "SELECT lab.fence_run_end_reserved_budget_without_intent("
                ":run_id,:candidate,:state_key,:state_sha,:state_seq,:budget_sha,"
                ":checkpoint_sha,:checkpoint_seq,:reservation_key,:reservation_sha,"
                ":reservation_seq,:reconciliation_key,:reconciliation_sha,"
                ":reconciliation_seq,:generation,:invocation_id,:execution_sha256)"
            ),
            {
                "run_id": run_id,
                "candidate": candidate_experiment_id,
                "state_key": prior_state_key,
                "state_sha": prior_state_sha256,
                "state_seq": prior_state_sequence,
                "budget_sha": budget_snapshot_sha256,
                "checkpoint_sha": unavailable_checkpoint_sha256,
                "checkpoint_seq": unavailable_checkpoint_sequence,
                "reservation_key": reservation_checkpoint_key,
                "reservation_sha": reservation_checkpoint_sha256,
                "reservation_seq": reservation_checkpoint_sequence,
                "reconciliation_key": reconciliation_checkpoint_key,
                "reconciliation_sha": reconciliation_checkpoint_sha256,
                "reconciliation_seq": reconciliation_checkpoint_sequence,
                "generation": owner.generation,
                "invocation_id": owner.invocation_id,
                "execution_sha256": owner.execution_sha256,
            },
        ).scalar_one()
    return _parse_run_end_unavailable(payload)


def read_run_end_admission_registration(engine: Engine, *, run_id: UUID) -> tuple[bool, bool]:
    """Read only whether the trusted run-end SQL intent/reservation rows exist."""
    with engine.connect() as connection:
        payload = connection.execute(
            text("SELECT lab.read_run_end_admission_registration(:run_id)"),
            {"run_id": run_id},
        ).scalar_one()
    if (
        not isinstance(payload, dict)
        or set(payload) != {"intent_registered", "reservation_exists"}
        or type(payload.get("intent_registered")) is not bool
        or type(payload.get("reservation_exists")) is not bool
    ):
        raise ValueError("database returned an invalid run-end registration status")
    return payload["intent_registered"], payload["reservation_exists"]


def read_run_end_unavailable(engine: Engine, *, run_id: UUID) -> RunEndUnavailable | None:
    """Read the Director-only durable no-admission receipt."""
    with engine.connect() as connection:
        payload = connection.execute(
            text("SELECT lab.read_run_end_unavailable_v2(:run_id)"), {"run_id": run_id}
        ).scalar_one()
    return None if payload is None else _parse_run_end_unavailable(payload)


def _parse_run_end_unavailable(payload: object) -> RunEndUnavailable:
    keys = {
        "run_id",
        "candidate_experiment_id",
        "reason",
        "prior_state_key",
        "prior_state_sha256",
        "prior_state_sequence",
        "budget_snapshot_sha256",
        "unavailable_checkpoint_sha256",
        "unavailable_checkpoint_sequence",
        "original_intent_checkpoint_sha256",
        "original_intent_checkpoint_sequence",
        "reservation_checkpoint_key",
        "reservation_checkpoint_sha256",
        "reservation_checkpoint_sequence",
        "state",
        "bit",
        "admitted_generation",
        "execution_sha256",
    }
    if not isinstance(payload, dict) or set(payload) != keys:
        raise ValueError("database returned an invalid run-end unavailable receipt")
    try:
        return RunEndUnavailable(
            run_id=UUID(str(payload["run_id"])),
            candidate_experiment_id=payload["candidate_experiment_id"],
            reason=payload["reason"],
            prior_state_key=payload["prior_state_key"],
            prior_state_sha256=payload["prior_state_sha256"],
            prior_state_sequence=payload["prior_state_sequence"],
            budget_snapshot_sha256=payload["budget_snapshot_sha256"],
            unavailable_checkpoint_sha256=payload["unavailable_checkpoint_sha256"],
            unavailable_checkpoint_sequence=payload["unavailable_checkpoint_sequence"],
            original_intent_checkpoint_sha256=payload["original_intent_checkpoint_sha256"],
            original_intent_checkpoint_sequence=payload["original_intent_checkpoint_sequence"],
            reservation_checkpoint_key=payload["reservation_checkpoint_key"],
            reservation_checkpoint_sha256=payload["reservation_checkpoint_sha256"],
            reservation_checkpoint_sequence=payload["reservation_checkpoint_sequence"],
            state=payload["state"],
            bit=payload["bit"],
            admitted_generation=payload["admitted_generation"],
            execution_sha256=payload["execution_sha256"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("database returned a malformed run-end unavailable receipt") from exc


def fence_missing_run_end_admission(
    engine: Engine,
    *,
    run_id: UUID,
    intent_checkpoint_sha256: str,
    intent_checkpoint_sequence: int,
    budget_checkpoint_key: str,
    budget_checkpoint_sha256: str,
) -> RunEndAdmissionFailure:
    """Persist the bitless no-row outcome without reserving quota or querying data."""
    if not isinstance(run_id, UUID):
        raise TypeError("run id must be a UUID")
    payload = _run_end_fence_rpc(
        engine,
        function="lab.fence_missing_holdout_run_end",
        run_id=run_id,
        intent_checkpoint_sha256=intent_checkpoint_sha256,
        intent_checkpoint_sequence=intent_checkpoint_sequence,
        budget_checkpoint_key=budget_checkpoint_key,
        budget_checkpoint_sha256=budget_checkpoint_sha256,
    )
    return _parse_run_end_admission_failure(payload)


def read_run_end_admission_failure(
    engine: Engine, *, run_id: UUID
) -> RunEndAdmissionFailure | None:
    """Read the immutable no-row fence through its Director-only RPC."""
    if not isinstance(run_id, UUID):
        raise TypeError("run id must be a UUID")
    with engine.connect() as connection:
        payload = connection.execute(
            text("SELECT lab.read_holdout_run_end_fence_v2(:run_id)"), {"run_id": run_id}
        ).scalar_one()
    if payload is None:
        return None
    return _parse_run_end_admission_failure(payload)


def _run_end_fence_rpc(
    engine: Engine,
    *,
    function: str,
    run_id: UUID,
    intent_checkpoint_sha256: str,
    intent_checkpoint_sequence: int,
    budget_checkpoint_key: str,
    budget_checkpoint_sha256: str,
) -> object:
    if function != "lab.fence_missing_holdout_run_end":
        raise ValueError("unsupported run-end failure RPC")
    with engine.begin() as connection:
        connection.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": run_plan_lock_key(run_id)},
        )
        owner = assert_execution_owner_closure_transaction(connection, run_id=run_id)
        return connection.execute(
            text(
                "SELECT lab.fence_missing_holdout_run_end(:run_id,:intent_sha,:intent_seq,"
                ":budget_key,:budget_sha,:generation,:invocation_id,:execution_sha256)"
            ),
            {
                "run_id": run_id,
                "intent_sha": intent_checkpoint_sha256,
                "intent_seq": intent_checkpoint_sequence,
                "budget_key": budget_checkpoint_key,
                "budget_sha": budget_checkpoint_sha256,
                "generation": owner.generation,
                "invocation_id": owner.invocation_id,
                "execution_sha256": owner.execution_sha256,
            },
        ).scalar_one()


def _parse_run_end_admission_failure(payload: object) -> RunEndAdmissionFailure:
    if not isinstance(payload, dict) or set(payload) != {
        "run_id",
        "candidate_experiment_id",
        "intent_checkpoint_sha256",
        "intent_checkpoint_sequence",
        "budget_checkpoint_key",
        "budget_checkpoint_sha256",
        "state",
        "bit",
        "failure_kind",
        "admitted_generation",
        "execution_sha256",
    }:
        raise ValueError("database returned an invalid run-end admission failure")
    run_id = payload.get("run_id")
    candidate_id = payload.get("candidate_experiment_id")
    intent_sha = payload.get("intent_checkpoint_sha256")
    intent_sequence = payload.get("intent_checkpoint_sequence")
    budget_key = payload.get("budget_checkpoint_key")
    budget_sha = payload.get("budget_checkpoint_sha256")
    failure_kind = payload.get("failure_kind")
    if (
        not isinstance(run_id, str)
        or not isinstance(candidate_id, str)
        or not isinstance(intent_sha, str)
        or isinstance(intent_sequence, bool)
        or not isinstance(intent_sequence, int)
        or not isinstance(budget_key, str)
        or not isinstance(budget_sha, str)
        or failure_kind != "missing_reservation_unverifiable_budget"
        or payload.get("state") != "failed"
        or payload.get("bit") is not None
    ):
        raise ValueError("database returned a malformed run-end admission failure")
    try:
        failure = RunEndAdmissionFailure(
            run_id=UUID(run_id),
            candidate_experiment_id=candidate_id,
            intent_checkpoint_sha256=intent_sha,
            intent_checkpoint_sequence=intent_sequence,
            budget_checkpoint_key=budget_key,
            budget_checkpoint_sha256=budget_sha,
            failure_kind="missing_reservation_unverifiable_budget",
            state="failed",
            bit=None,
            admitted_generation=payload["admitted_generation"],
            execution_sha256=payload["execution_sha256"],
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("database returned a malformed run-end admission failure") from exc
    if failure.state != "failed" or failure.bit is not None:
        raise ValueError("database run-end failure exposed a result bit")
    return failure


def reserve_holdout_check(
    engine: Engine,
    *,
    run_id: UUID,
    candidate_experiment_id: str,
    trigger_kind: Literal["keep_interval", "run_end"],
    trigger_index: int,
    request_key: str,
    reservation_id: UUID | None = None,
) -> HoldoutReceipt:
    """Atomically reserve one hidden-set query using the immutable run identity.

    Suite id, version, digest, candidate source digests, reference champion, epsilon,
    and both quotas are resolved inside the database function. Callers cannot select
    another suite or inspect task identities/metrics.
    """
    if not isinstance(run_id, UUID):
        raise TypeError("run id must be a UUID")
    if not isinstance(candidate_experiment_id, str) or not candidate_experiment_id:
        raise ValueError("candidate experiment identity is required")
    if trigger_kind not in {"keep_interval", "run_end"}:
        raise ValueError("unsupported holdout trigger")
    if isinstance(trigger_index, bool) or not isinstance(trigger_index, int) or trigger_index < 1:
        raise ValueError("holdout trigger index must be a positive integer")
    if not isinstance(request_key, str) or not 1 <= len(request_key) <= 128:
        raise ValueError("holdout idempotency key must contain 1..128 characters")
    chosen_id = reservation_id or uuid4()
    if not isinstance(chosen_id, UUID):
        raise TypeError("reservation id must be a UUID")
    with engine.begin() as connection:
        connection.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": run_plan_lock_key(run_id)},
        )
        owner = assert_execution_owner_transaction(connection, run_id=run_id)
        payload = connection.execute(
            text(
                "SELECT lab.reserve_holdout_check(:run_id,:reservation_id,:request_key,"
                ":candidate_experiment_id,:trigger_kind,:trigger_index,:generation,"
                ":invocation_id,:execution_sha256)"
            ),
            {
                "run_id": run_id,
                "reservation_id": chosen_id,
                "request_key": request_key,
                "candidate_experiment_id": candidate_experiment_id,
                "trigger_kind": trigger_kind,
                "trigger_index": trigger_index,
                "generation": owner.generation,
                "invocation_id": owner.invocation_id,
                "execution_sha256": owner.execution_sha256,
            },
        ).scalar_one()
    receipt = _parse_receipt(payload)
    if (
        receipt.admitted_generation != owner.generation
        or receipt.execution_sha256 != owner.execution_sha256
    ):
        raise RuntimeError("holdout reservation receipt differs from its captured owner")
    return receipt


def read_holdout_request(
    engine: Engine,
    *,
    run_id: UUID,
    candidate_experiment_id: str,
    trigger_kind: Literal["keep_interval", "run_end"],
    trigger_index: int,
) -> HoldoutReceipt | None:
    """Read an existing reservation by its deterministic identity without creating one."""
    import hashlib

    if not isinstance(run_id, UUID) or not candidate_experiment_id:
        raise ValueError("holdout request identity is invalid")
    candidate_key = hashlib.sha256(candidate_experiment_id.encode("utf-8")).hexdigest()
    request_key = f"{trigger_kind}:{trigger_index}:{candidate_key}"
    with engine.connect() as connection:
        payload = connection.execute(
            text(
                "SELECT lab.read_holdout_request_v2(:run_id,:request_key,"
                ":candidate_experiment_id,:trigger_kind,:trigger_index)"
            ),
            {
                "run_id": run_id,
                "request_key": request_key,
                "candidate_experiment_id": candidate_experiment_id,
                "trigger_kind": trigger_kind,
                "trigger_index": trigger_index,
            },
        ).scalar_one()
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("database returned an invalid holdout request receipt")
    payload["reservation_id"] = UUID(str(payload["reservation_id"]))
    return _parse_receipt(payload)


def holdout_suite_is_registered(engine: Engine, *, run_id: UUID) -> bool:
    """Ask the database's trusted registration authority before enabling loop hooks."""
    if not isinstance(run_id, UUID):
        raise TypeError("run id must be a UUID")
    with engine.connect() as connection:
        value = connection.execute(
            text("SELECT lab.holdout_suite_is_registered(:run_id)"),
            {"run_id": run_id},
        ).scalar_one()
    if type(value) is not bool:
        raise ValueError("database returned an invalid holdout registration status")
    return value


def read_holdout_bit(engine: Engine, *, run_id: UUID, reservation_id: UUID) -> HoldoutReceipt:
    """Read operational state and, only after completion, the one approved bit."""
    if not isinstance(run_id, UUID) or not isinstance(reservation_id, UUID):
        raise TypeError("run and reservation identities must be UUIDs")
    with engine.connect() as connection:
        payload = connection.execute(
            text("SELECT lab.read_holdout_bit_v2(:run_id,:reservation_id)"),
            {"run_id": run_id, "reservation_id": reservation_id},
        ).scalar_one()
    if not isinstance(payload, dict):
        raise ValueError("database returned an invalid holdout receipt")
    payload["reservation_id"] = reservation_id
    return _parse_receipt(payload)


def evaluate_holdout_check(
    engine: Engine,
    *,
    run_id: UUID,
    candidate_experiment_id: str,
    trigger_kind: Literal["keep_interval", "run_end"],
    trigger_index: int,
    remaining_seconds: int = 600,
) -> HoldoutReceipt:
    """Reserve once, run only a newly reserved check, and return only its receipt.

    A retry of an already running or terminal reservation never starts another
    Scorer process. The deterministic request key is scoped to the run trigger;
    SQL independently binds it to the candidate and immutable suite identity.
    """
    import hashlib

    if not isinstance(run_id, UUID):
        raise TypeError("run id must be a UUID")
    if not isinstance(candidate_experiment_id, str) or not candidate_experiment_id:
        raise ValueError("candidate experiment identity is required")
    if (
        isinstance(remaining_seconds, bool)
        or not isinstance(remaining_seconds, int)
        or not 1 <= remaining_seconds <= 600
    ):
        raise ValueError("holdout total budget must be 1..600 seconds")
    deadline = time.monotonic() + remaining_seconds
    candidate_key = hashlib.sha256(candidate_experiment_id.encode("utf-8")).hexdigest()
    request_key = f"{trigger_kind}:{trigger_index}:{candidate_key}"
    proposed_reservation_id = uuid4()
    receipt = reserve_holdout_check(
        engine,
        run_id=run_id,
        candidate_experiment_id=candidate_experiment_id,
        trigger_kind=trigger_kind,
        trigger_index=trigger_index,
        request_key=request_key,
        reservation_id=proposed_reservation_id,
    )

    def read_current_receipt() -> HoldoutReceipt:
        current = read_holdout_bit(engine, run_id=run_id, reservation_id=receipt.reservation_id)
        if (
            current.reservation_id != receipt.reservation_id
            or current.run_id != run_id
            or current.candidate_experiment_id != candidate_experiment_id
            or current.trigger_kind != trigger_kind
            or current.trigger_index != trigger_index
            or current.admitted_generation != receipt.admitted_generation
            or current.execution_sha256 != receipt.execution_sha256
        ):
            raise RuntimeError("durable holdout receipt differs from the requested identity")
        return current

    if receipt.reservation_id != proposed_reservation_id:
        if receipt.state not in {"reserved", "running"}:
            return read_current_receipt()
        recovery_seconds = min(30, int(deadline - time.monotonic()))
        if recovery_seconds < 1:
            return read_current_receipt()
        from lab.scorer.holdout_supervisor import run_holdout_recovery_process

        recovered = run_holdout_recovery_process(
            receipt.reservation_id,
            remaining_seconds=recovery_seconds,
        )
        current = read_current_receipt()
        status_matches = (
            current.state in {"reserved", "running"}
            if recovered.state == "pending"
            else current.state == recovered.state
        )
        if not status_matches:
            raise RuntimeError("holdout recovery status differs from the durable Scorer receipt")
        return current
    if receipt.state in {"passed", "reverted", "failed", "exhausted"}:
        return read_current_receipt()
    if receipt.admitted_generation is None or receipt.execution_sha256 is None:
        raise RuntimeError("active holdout reservation has no immutable owner pair")
    if receipt.state != "reserved":
        raise RuntimeError("new holdout reservation did not enter its reserved state")
    worker_seconds = int(deadline - time.monotonic())
    if worker_seconds < 1:
        raise TimeoutError("holdout reservation consumed the remaining run deadline")
    if receipt.admitted_generation is None or receipt.execution_sha256 is None:
        raise RuntimeError("new holdout reservation has no admitted execution owner")

    from lab.scorer.holdout_supervisor import run_holdout_process

    process_result = run_holdout_process(
        receipt.reservation_id,
        admitted_generation=receipt.admitted_generation,
        execution_sha256=receipt.execution_sha256,
        remaining_seconds=min(600, worker_seconds),
    )
    current = read_current_receipt()
    if (current.state, current.bit) != (process_result.state, process_result.bit):
        raise RuntimeError("holdout worker receipt differs from the durable Scorer result")
    return current


def recover_existing_holdout_request(
    engine: Engine,
    *,
    run_id: UUID,
    candidate_experiment_id: str,
    trigger_kind: Literal["keep_interval", "run_end"],
    trigger_index: int,
    remaining_seconds: int = 30,
) -> HoldoutReceipt:
    """Drain one already-admitted reservation without invoking admission again."""
    if not 1 <= remaining_seconds <= 600:
        raise ValueError("holdout recovery bound is invalid")
    receipt = read_holdout_request(
        engine,
        run_id=run_id,
        candidate_experiment_id=candidate_experiment_id,
        trigger_kind=trigger_kind,
        trigger_index=trigger_index,
    )
    if receipt is None:
        raise RuntimeError("no already-admitted holdout request exists to recover")
    from lab.director.ownership import active_execution_owner

    owner = active_execution_owner()
    if (
        owner is None
        or owner.run_id != run_id
        or receipt.execution_sha256 != owner.execution_sha256
    ):
        raise RuntimeError("existing holdout request differs from the captured execution owner")
    if receipt.admitted_generation != owner.generation:
        if receipt.admitted_generation is None or receipt.state in {"reserved", "running"}:
            raise RuntimeError("historical holdout must be terminal before generation takeover")
        from lab.director.resume import assert_historical_receipt

        assert_historical_receipt(
            engine,
            owner=owner,
            admitted_generation=receipt.admitted_generation,
            execution_sha256=receipt.execution_sha256,
        )
    if receipt.state not in {"reserved", "running"}:
        return read_holdout_bit(engine, run_id=run_id, reservation_id=receipt.reservation_id)
    from lab.scorer.holdout_supervisor import run_holdout_recovery_process

    recovered = run_holdout_recovery_process(
        receipt.reservation_id, remaining_seconds=remaining_seconds
    )
    current = read_holdout_bit(engine, run_id=run_id, reservation_id=receipt.reservation_id)
    if (
        current.admitted_generation != owner.generation
        or current.execution_sha256 != owner.execution_sha256
    ):
        raise RuntimeError("recovered holdout receipt changed its admitted owner")
    if recovered.state == "pending":
        if current.state not in {"reserved", "running"}:
            raise RuntimeError("holdout recovery stayed pending after a terminal DB receipt")
    elif current.state != recovered.state:
        raise RuntimeError("holdout recovery differs from the durable reservation state")
    return current


def check_at_run_end(
    engine: Engine,
    *,
    run_id: UUID,
    lease: Any,
    loop_result: Any,
    artifact_root: Any,
    remaining_seconds: int = 600,
) -> HoldoutReceipt:
    """Record verified loop termination and evaluate its final champion.

    With no promoted proposal, the unchanged measured ``robust_z`` baseline is
    evaluated against itself and consumes a normal quota reservation. It is not
    reported as an improvement; this preserves the run-end check without making
    a candidate-promotion claim.
    """
    intent = register_run_end_intent(
        engine, lease=lease, loop_result=loop_result, artifact_root=artifact_root
    )
    return evaluate_holdout_check(
        engine,
        run_id=run_id,
        candidate_experiment_id=intent.candidate_experiment_id,
        trigger_kind="run_end",
        trigger_index=1,
        remaining_seconds=remaining_seconds,
    )


def check_after_tenth_keep(
    engine: Engine,
    *,
    run_id: UUID,
    experiment_id: str,
    remaining_seconds: int = 600,
    artifact_root: Any = None,
) -> HoldoutReceipt | None:
    """Evaluate only a verified tenth KEEP, using the Director receipt RPC."""
    from pathlib import Path

    from lab.director.artifacts import read_director_artifact
    from lab.director.contracts import ExperimentDocument
    from lab.scorer.jobs import DEFAULT_ARTIFACT_ROOT

    root = DEFAULT_ARTIFACT_ROOT if artifact_root is None else Path(artifact_root)
    with engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT experiment_id,status,experiment_number FROM lab.experiments "
                    "WHERE run_id=:run_id AND kind='proposal' AND status='scored' "
                    "ORDER BY experiment_number,experiment_id"
                ),
                {"run_id": run_id},
            )
            .mappings()
            .all()
        )
        row_by_id = {str(row["experiment_id"]): row for row in rows}
        if experiment_id not in row_by_id:
            return None
        keeps: list[tuple[int, str]] = []
        for row in rows:
            record = connection.execute(
                text("SELECT lab.experiment_record_receipt(:id)"),
                {"id": row["experiment_id"]},
            ).scalar_one()
            if not isinstance(record, dict) or record.get("run_id") != str(run_id):
                raise RuntimeError("scored proposal has no run-bound terminal receipt")
            blob_sha = record.get("experiment_blob_sha256")
            document_sha = record.get("experiment_sha256")
            if not isinstance(blob_sha, str) or not isinstance(document_sha, str):
                raise RuntimeError("scored proposal terminal receipt is malformed")
            payload = read_director_artifact(blob_sha, artifact_root=root)
            if hashlib.sha256(payload).hexdigest() != document_sha:
                raise RuntimeError("scored proposal terminal artifact hash differs")
            document = ExperimentDocument.model_validate_json(payload, strict=True)
            if (
                document.experiment_id != row["experiment_id"]
                or document.run_id != run_id
                or document.status != row["status"]
                or document.experiment_number != row["experiment_number"]
            ):
                raise RuntimeError("scored proposal receipt differs from its ledger row")
            if document.decision is not None and document.decision.verdict in {
                "KEEP",
                "KEEP_SIMPLER",
            }:
                keeps.append((int(row["experiment_number"]), str(row["experiment_id"])))
    keeps.sort()
    if not keeps or len(keeps) % 10 or keeps[-1][1] != experiment_id:
        return None
    return evaluate_holdout_check(
        engine,
        run_id=run_id,
        candidate_experiment_id=experiment_id,
        trigger_kind="keep_interval",
        trigger_index=len(keeps),
        remaining_seconds=remaining_seconds,
    )


def register_run_end_intent(
    engine: Engine,
    *,
    lease: Any,
    loop_result: Any,
    artifact_root: Any,
) -> RunEndIntent:
    """Persist an exact final loop checkpoint only after verifying its budget fence."""
    from lab.director.loop import DirectorLoopResult, DirectorLoopState

    if not isinstance(loop_result, DirectorLoopResult):
        raise TypeError("run-end holdout requires the typed Director loop result")
    run_id = lease.run_id
    if loop_result.run_id != run_id:
        raise ValueError("loop result belongs to another run")
    captured_owner = active_execution_owner()
    if captured_owner is None or captured_owner.run_id != run_id:
        raise RuntimeError("run-end intent requires this run's captured execution owner")
    with engine.connect() as connection:
        state_key = connection.execute(
            text(
                "SELECT event_json->>'key' FROM lab.run_events WHERE run_id=:run_id "
                "AND event_type='director.checkpoint' AND event_json->>'phase'="
                "'director_loop_state' AND event_json->>'payload_sha256'=:digest"
            ),
            {"run_id": run_id, "digest": loop_result.checkpoint_sha256},
        ).scalar_one_or_none()
    if not isinstance(state_key, str):
        raise ValueError("loop result does not reference a durable state checkpoint")
    stored = lease.read_checkpoint(key=state_key, artifact_root=artifact_root)
    if stored is None or stored["receipt"].get("phase") != "director_loop_state":
        raise ValueError("terminal Director loop state checkpoint is missing")
    receipt = stored["receipt"]
    if receipt.get("payload_sha256") != loop_result.checkpoint_sha256:
        raise ValueError("loop result differs from its durable state checkpoint")
    payload = stored["payload"]
    if not isinstance(payload, dict):
        raise ValueError("terminal Director state payload is malformed")
    state = DirectorLoopState.model_validate_json(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False), strict=True
    ).verify_consistency()
    if (
        state.run_id != run_id
        or state.completed_proposals != loop_result.completed_proposals
        or state.next_ordinal != loop_result.next_ordinal
        or state.champion_experiment_id != loop_result.champion_experiment_id
        or state.best_suite != loop_result.best_suite
        or state.completed_proposals != loop_result.completed_proposals
    ):
        raise ValueError("loop result and final state checkpoint identity differ")
    with engine.connect() as connection:
        row = (
            connection.execute(
                text("SELECT request_json FROM lab.runs WHERE run_id=:run_id"),
                {"run_id": run_id},
            )
            .mappings()
            .one_or_none()
        )
        calibration_receipt = connection.execute(
            text("SELECT lab.baseline_calibration_receipt(:run_id)"),
            {"run_id": run_id},
        ).scalar_one_or_none()
        candidate = (
            connection.execute(
                text(
                    "SELECT candidate_sha256,status FROM lab.experiments "
                    "WHERE run_id=:run_id AND experiment_id=:candidate"
                ),
                {"run_id": run_id, "candidate": state.champion_experiment_id},
            )
            .mappings()
            .one_or_none()
        )
        sequence_value = connection.execute(
            text(
                "SELECT max((event_json->>'sequence')::integer) "
                "FROM lab.run_events WHERE run_id=:run_id "
                "AND event_type='director.checkpoint'"
            ),
            {"run_id": run_id},
        ).scalar_one()
    if (
        row is None
        or not isinstance(row["request_json"], dict)
        or not isinstance(calibration_receipt, dict)
        or candidate is None
    ):
        raise ValueError("immutable request, calibration, or final champion is unavailable")
    request = row["request_json"]
    budget_request = request.get("budget")
    if not isinstance(budget_request, dict):
        raise ValueError("immutable API run budget is missing")
    proposal_limit = budget_request.get("experiments")
    wall_limit = budget_request.get("wall_seconds")
    token_limit = budget_request.get("model_tokens")
    if (
        any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (proposal_limit, wall_limit, token_limit)
        )
        or proposal_limit != request.get("proposal_limit")
        or proposal_limit != state.proposal_limit
        or calibration_receipt.get("suite_id") != state.suite_id
        or calibration_receipt.get("suite_version") != state.suite_version
        or request.get("suite") != state.suite_id
        or request.get("suite_manifest_sha256") != state.suite_manifest_sha256
    ):
        raise ValueError("loop state differs from the immutable API suite or budget")
    budget_state = state.budget
    budget_values = {
        name: budget_state.get(name)
        for name in (
            "wall_seconds",
            "elapsed_wall_seconds",
            "reserved_wall_seconds",
            "model_tokens",
            "reserved_model_tokens",
        )
    }
    if loop_result.status == "proposal_limit_reached":
        if state.completed_proposals != proposal_limit:
            raise ValueError("proposal-limit status does not match the immutable budget")
    elif loop_result.status == "budget_exhausted":
        if not _budget_is_exhausted(
            budget_values,
            wall_limit=cast(int, wall_limit),
            token_limit=cast(int, token_limit),
        ):
            raise ValueError("budget-exhausted status has no exhausted immutable budget")
    else:
        raise ValueError("run-end holdout requires proposal completion or budget exhaustion")
    if (
        candidate["status"] != "scored"
        or not isinstance(candidate["candidate_sha256"], str)
        or candidate["candidate_sha256"] != state.champion_source_sha256
    ):
        raise ValueError("final champion is not a scored candidate")
    state_sequence = int(receipt["sequence"])
    if not isinstance(sequence_value, int) or sequence_value < state_sequence:
        raise ValueError("final state checkpoint sequence is not durable")
    state_application_key: str | None = None
    state_application_sha256: str | None = None
    state_application_receipt_sha256: str | None = None
    keep_count = state.holdout_applied_keep_count
    if keep_count and state_key.endswith(":keep_interval"):
        state_application_key = f"holdout-state-application:keep_interval:{keep_count}"
        marker = lease.read_checkpoint(key=state_application_key, artifact_root=artifact_root)
        application = lease.read_checkpoint(
            key=f"holdout-application:keep_interval:{keep_count}", artifact_root=artifact_root
        )
        if (
            marker is None
            or application is None
            or marker["receipt"].get("phase") != "holdout_state_application"
            or application["receipt"].get("phase") != "holdout_application"
            or not isinstance(marker.get("payload"), dict)
            or marker["payload"]
            != {
                "schema": "director-holdout-state-application.v1",
                "run_id": str(run_id),
                "application_sha256": application["receipt"].get("payload_sha256"),
                "state_sha256": loop_result.checkpoint_sha256,
            }
        ):
            raise ValueError("final holdout state has no exact application marker")
        state_application_sha256 = marker["receipt"].get("payload_sha256")
        if not isinstance(state_application_sha256, str):
            raise ValueError("final holdout state marker has no verified digest")
        state_application_receipt_sha256 = application["receipt"].get("payload_sha256")
        if not isinstance(state_application_receipt_sha256, str):
            raise ValueError("final holdout application has no verified digest")
    with engine.connect() as connection:
        proposal_rows = (
            connection.execute(
                text(
                    "SELECT experiment_number FROM lab.experiments "
                    "WHERE run_id=:run_id AND kind='proposal'"
                ),
                {"run_id": run_id},
            )
            .scalars()
            .all()
        )
    proposal_ordinals = {int(value) for value in proposal_rows}
    if len(proposal_ordinals) != len(proposal_rows):
        raise ValueError("proposal ledger contains duplicate terminal ordinals")
    abandoned_checkpoints = _validated_abandonment_claims(
        lease,
        run_id=run_id,
        proposal_ordinals=proposal_ordinals,
        completed_proposals=state.completed_proposals,
        artifact_root=artifact_root,
    )
    intent_payload = {
        "schema": "director-holdout-run-end-intent.v1",
        "terminal_status": loop_result.status,
        "run_id": str(run_id),
        "admitted_generation": captured_owner.generation,
        "execution_sha256": captured_owner.execution_sha256,
        "candidate_experiment_id": state.champion_experiment_id,
        "candidate_sha256": candidate["candidate_sha256"],
        "completed_proposals": state.completed_proposals,
        "abandoned_checkpoints": abandoned_checkpoints,
        "proposal_limit": proposal_limit,
        "state_checkpoint_key": state_key,
        "state_checkpoint_sha256": loop_result.checkpoint_sha256,
        "state_checkpoint_sequence": state_sequence,
        "state_application_key": state_application_key,
        "state_application_sha256": state_application_sha256,
        "state_application_receipt_sha256": state_application_receipt_sha256,
        "budget": budget_values,
    }
    intent_key = "director-holdout-run-end-intent"
    existing = lease.read_checkpoint(key=intent_key, artifact_root=artifact_root)
    if existing is None:
        intent_receipt = lease.append_checkpoint(
            sequence=int(sequence_value) + 1,
            key=intent_key,
            phase="holdout_run_end_intent",
            payload=intent_payload,
            artifact_root=artifact_root,
        )
    else:
        if (
            existing["receipt"].get("phase") != "holdout_run_end_intent"
            or existing["payload"] != intent_payload
        ):
            raise ValueError("terminal intent conflicts with the final loop checkpoint")
        intent_receipt = existing["receipt"]
    with engine.begin() as connection:
        connection.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": run_plan_lock_key(run_id)},
        )
        owner = assert_execution_owner_transaction(connection, run_id=run_id)
        if owner != captured_owner:
            raise RuntimeError("run-end owner changed while the intent was prepared")
        registered = connection.execute(
            text(
                "SELECT lab.register_holdout_run_end_intent(:run_id,CAST(:intent AS jsonb),"
                ":generation,:invocation_id,:execution_sha256)"
            ),
            {
                "run_id": run_id,
                "generation": owner.generation,
                "invocation_id": owner.invocation_id,
                "execution_sha256": owner.execution_sha256,
                "intent": json.dumps(
                    intent_payload, sort_keys=True, separators=(",", ":"), allow_nan=False
                ),
            },
        ).scalar_one()
    if not isinstance(registered, dict) or registered.get(
        "checkpoint_sha256"
    ) != intent_receipt.get("payload_sha256"):
        raise RuntimeError("database run-end intent differs from its durable checkpoint")
    return RunEndIntent(
        candidate_experiment_id=state.champion_experiment_id,
        candidate_sha256=candidate["candidate_sha256"],
        state_checkpoint_sha256=loop_result.checkpoint_sha256,
        intent_checkpoint_sha256=str(intent_receipt["payload_sha256"]),
        terminal_status=loop_result.status,
    )


def _parse_receipt(payload: object) -> HoldoutReceipt:
    if not isinstance(payload, dict) or set(payload) not in (
        {"reservation_id", "state", "bit"},
        {"reservation_id", "state", "bit", "admitted_generation", "execution_sha256"},
        {
            "reservation_id",
            "run_id",
            "candidate_experiment_id",
            "trigger_kind",
            "trigger_index",
            "state",
            "bit",
        },
        {
            "reservation_id",
            "run_id",
            "candidate_experiment_id",
            "trigger_kind",
            "trigger_index",
            "state",
            "bit",
            "admitted_generation",
            "execution_sha256",
        },
    ):
        raise ValueError("database returned an invalid holdout receipt")
    identifier = payload.get("reservation_id")
    if not isinstance(identifier, UUID):
        try:
            identifier = UUID(str(identifier))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("database returned an invalid holdout reservation id") from exc
    state = payload.get("state")
    if state not in {"reserved", "running", "passed", "reverted", "failed", "exhausted"}:
        raise ValueError("database returned an invalid holdout state")
    bit = payload.get("bit")
    if bit is not None and type(bit) is not bool:
        raise ValueError("database returned an invalid holdout bit")
    run_id = payload.get("run_id")
    if run_id is not None and not isinstance(run_id, UUID):
        try:
            run_id = UUID(str(run_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("database returned an invalid holdout run id") from exc
    candidate_id = payload.get("candidate_experiment_id")
    trigger_kind = payload.get("trigger_kind")
    trigger_index = payload.get("trigger_index")
    if isinstance(trigger_index, bool) or (
        trigger_index is not None and not isinstance(trigger_index, int)
    ):
        raise ValueError("database returned an invalid holdout trigger index")
    return HoldoutReceipt(
        reservation_id=identifier,
        state=state,
        bit=bit,
        run_id=run_id,
        candidate_experiment_id=candidate_id,
        trigger_kind=trigger_kind,
        trigger_index=trigger_index,
        admitted_generation=payload.get("admitted_generation"),
        execution_sha256=payload.get("execution_sha256"),
    )
