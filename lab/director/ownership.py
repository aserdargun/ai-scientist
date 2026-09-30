"""Immutable Director execution identity and first-generation admission."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, text

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_INVOCATION = re.compile(r"[0-9a-f]{32}\Z")
_OWNER_CONTEXT: ContextVar[ExecutionOwner | None] = ContextVar(
    "director_execution_owner", default=None
)


def canonical_execution_bytes(payload: Mapping[str, Any]) -> bytes:
    """Encode only JSON-native contract data; unsupported objects fail closed."""
    if not isinstance(payload, Mapping):
        raise TypeError("Director execution contract must be an object")
    return json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class ExecutionContract:
    """Request plus independently verified registry/runtime pins."""

    run_id: UUID
    request_sha256: str
    execution_json: dict[str, Any]
    execution_sha256: str
    wall_seconds: int

    def validate(self) -> None:
        """Reject an internally inconsistent contract before its RPC is called."""
        request = self.execution_json.get("request")
        budget = request.get("budget") if isinstance(request, Mapping) else None
        request_wall = budget.get("wall_seconds") if isinstance(budget, Mapping) else None
        execution_wall = self.execution_json.get("wall_seconds")
        if (
            self.execution_json.get("schema") != "director-execution-contract.v1"
            or self.execution_json.get("run_id") != str(self.run_id)
            or self.execution_json.get("request_sha256") != self.request_sha256
            or isinstance(request_wall, bool)
            or not isinstance(request_wall, int)
            or request_wall != self.wall_seconds
            or isinstance(execution_wall, bool)
            or not isinstance(execution_wall, int)
            or execution_wall != request_wall
        ):
            raise ValueError("Director execution contract differs from its immutable request")
        if hashlib.sha256(canonical_execution_bytes(self.execution_json)).hexdigest() != (
            self.execution_sha256
        ):
            raise ValueError("Director execution contract digest is inconsistent")

    @classmethod
    def build(
        cls,
        *,
        run_id: UUID,
        request: Mapping[str, Any],
        request_sha256: str,
        suite_id: str,
        suite_version: int,
        suite_manifest_sha256: str,
        registry_entry_sha256: str,
        harness_sha256: str,
        image_sha256: str,
    ) -> ExecutionContract:
        if _SHA256.fullmatch(request_sha256) is None:
            raise ValueError("immutable Director request digest is malformed")
        budget = request.get("budget")
        if not isinstance(budget, Mapping):
            raise ValueError("immutable Director request has no budget object")
        wall_seconds = budget.get("wall_seconds")
        if (
            isinstance(wall_seconds, bool)
            or not isinstance(wall_seconds, int)
            or not 1 <= wall_seconds <= 14_400
        ):
            raise ValueError("immutable Director wall budget must be within the host limit")
        if not suite_id or isinstance(suite_version, bool) or suite_version < 1:
            raise ValueError("verified Director suite identity is malformed")
        for digest in (
            suite_manifest_sha256,
            registry_entry_sha256,
            harness_sha256,
            image_sha256,
        ):
            if _SHA256.fullmatch(digest) is None:
                raise ValueError("verified Director execution pin is malformed")
        document = {
            "schema": "director-execution-contract.v1",
            "run_id": str(run_id),
            "request_sha256": request_sha256,
            "request": {key: value for key, value in request.items() if key != "idempotency_key"},
            "suite_id": suite_id,
            "suite_version": suite_version,
            "suite_manifest_sha256": suite_manifest_sha256,
            "registry_entry_sha256": registry_entry_sha256,
            "provider": request.get("provider"),
            "provider_config_sha256": request.get("provider_config_sha256"),
            "scenario_sha256": request.get("scenario_sha256"),
            "harness_sha256": harness_sha256,
            "image_sha256": image_sha256,
            "wall_seconds": wall_seconds,
        }
        digest = hashlib.sha256(canonical_execution_bytes(document)).hexdigest()
        return cls(run_id, request_sha256, document, digest, wall_seconds)


@dataclass(frozen=True, slots=True)
class ExecutionOwner:
    """Unchanging generation captured at the atomic run claim."""

    run_id: UUID
    generation: int
    invocation_id: str
    execution_sha256: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
        ):
            raise ValueError("Director owner generation must be positive")
        if _INVOCATION.fullmatch(self.invocation_id) is None:
            raise ValueError("Director owner invocation ID is malformed")
        if _SHA256.fullmatch(self.execution_sha256) is None:
            raise ValueError("Director execution digest is malformed")


@dataclass(frozen=True, slots=True)
class OwnerProcessIdentity:
    """Systemd and kernel identity observed before the first run claim."""

    payload_sha256: str
    worker_pid: int
    worker_start_ticks: int
    worker_boot_id: str
    worker_unit: str
    worker_invocation_id: str
    worker_cgroup: str

    def __post_init__(self) -> None:
        if _SHA256.fullmatch(self.payload_sha256) is None:
            raise ValueError("Director process request digest is malformed")
        if (
            isinstance(self.worker_pid, bool)
            or not isinstance(self.worker_pid, int)
            or self.worker_pid < 2
        ):
            raise ValueError("Director process PID is invalid")
        if (
            isinstance(self.worker_start_ticks, bool)
            or not isinstance(self.worker_start_ticks, int)
            or self.worker_start_ticks < 1
        ):
            raise ValueError("Director process start time is invalid")
        if re.fullmatch(r"[0-9a-f-]{36}", self.worker_boot_id) is None:
            raise ValueError("Director process boot identity is malformed")
        if re.fullmatch(r"[A-Za-z0-9_.@-]+[.]service", self.worker_unit) is None:
            raise ValueError("Director process unit is malformed")
        if _INVOCATION.fullmatch(self.worker_invocation_id) is None:
            raise ValueError("Director process invocation identity is malformed")
        if re.fullmatch(
            r"/.+[.]service", self.worker_cgroup
        ) is None or not self.worker_cgroup.endswith("/" + self.worker_unit):
            raise ValueError("Director process cgroup differs from its unit")


def claim_initial_execution(
    engine: Engine,
    *,
    contract: ExecutionContract,
    process: OwnerProcessIdentity,
) -> ExecutionOwner:
    """Atomically claim a queued run and persist its immutable generation 1."""
    contract.validate()
    if process.payload_sha256 != contract.request_sha256:
        raise ValueError("Director process identity differs from the request digest")
    encoded = canonical_execution_bytes(contract.execution_json).decode("utf-8")
    with engine.begin() as connection:
        result = connection.execute(
            text(
                "SELECT lab.claim_initial_director_execution(:run_id,:request_sha256,"
                "CAST(:execution_json AS jsonb),:execution_sha256,:wall_seconds,:worker_pid,"
                ":worker_start_ticks,:worker_boot_id,:worker_unit,:worker_invocation_id,"
                ":worker_cgroup)"
            ),
            {
                "run_id": contract.run_id,
                "request_sha256": contract.request_sha256,
                "execution_json": encoded,
                "execution_sha256": contract.execution_sha256,
                "wall_seconds": contract.wall_seconds,
                "worker_pid": process.worker_pid,
                "worker_start_ticks": process.worker_start_ticks,
                "worker_boot_id": process.worker_boot_id,
                "worker_unit": process.worker_unit,
                "worker_invocation_id": process.worker_invocation_id,
                "worker_cgroup": process.worker_cgroup,
            },
        ).scalar_one()
    if isinstance(result, str):
        result = json.loads(result)
    if not isinstance(result, dict):
        raise ValueError("Director claim RPC returned a malformed owner")
    generation = result.get("generation")
    invocation_id = result.get("worker_invocation_id")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or not isinstance(invocation_id, str)
    ):
        raise ValueError("Director claim RPC returned a malformed owner")
    owner = ExecutionOwner(
        run_id=contract.run_id,
        generation=generation,
        invocation_id=invocation_id,
        execution_sha256=contract.execution_sha256,
    )
    if result.get("state") != "running" or result.get("newly_claimed") is not True:
        raise ValueError("Director claim RPC did not create generation 1")
    return owner


def active_execution_owner() -> ExecutionOwner | None:
    """Return only the owner bound by the current trusted execution context."""
    return _OWNER_CONTEXT.get()


def bind_execution_owner(owner: ExecutionOwner) -> Token[ExecutionOwner | None]:
    """Bind the immutable owner for synchronous Director mutation helpers."""
    return _OWNER_CONTEXT.set(owner)


@contextmanager
def owned_execution(owner: ExecutionOwner) -> Iterator[ExecutionOwner]:
    """Bind one captured generation across synchronous Director execution."""
    token = bind_execution_owner(owner)
    try:
        yield owner
    finally:
        reset_execution_owner(token)


def reset_execution_owner(token: Token[ExecutionOwner | None]) -> None:
    """Restore the caller's prior owner context."""
    _OWNER_CONTEXT.reset(token)


def _set_execution_owner_context(connection: Connection, expected: ExecutionOwner) -> None:
    for key, value in (
        ("lab.owner_run_id", str(expected.run_id)),
        ("lab.owner_generation", str(expected.generation)),
        ("lab.owner_invocation_id", expected.invocation_id),
        ("lab.owner_execution_sha256", expected.execution_sha256),
    ):
        connection.execute(
            text("SELECT set_config(:key,:value,true)"), {"key": key, "value": value}
        )


def assert_execution_owner_transaction(
    connection: Connection,
    owner: ExecutionOwner | None = None,
    *,
    run_id: UUID | None = None,
) -> ExecutionOwner:
    """Set transaction-local DB context and verify it before the mutation."""
    expected = owner or active_execution_owner()
    if expected is None:
        raise RuntimeError("Director mutation requires a captured execution owner")
    if run_id is not None and expected.run_id != run_id:
        raise RuntimeError("captured Director owner belongs to another run")
    _set_execution_owner_context(connection, expected)
    connection.execute(
        text("SELECT lab.assert_director_owner_context(:run_id)"),
        {"run_id": expected.run_id},
    )
    return expected


def assert_execution_owner_closure_transaction(
    connection: Connection,
    owner: ExecutionOwner | None = None,
    *,
    run_id: UUID | None = None,
) -> ExecutionOwner:
    """Bind this generation for narrow same-owner stop closure without new work."""
    expected = owner or active_execution_owner()
    if expected is None:
        raise RuntimeError("Director stop closure requires a captured execution owner")
    if run_id is not None and expected.run_id != run_id:
        raise RuntimeError("captured Director owner belongs to another run")
    _set_execution_owner_context(connection, expected)
    connection.execute(
        text(
            "SELECT lab.assert_director_generation_identity("
            ":run_id,:generation,:invocation_id,:execution_sha256)"
        ),
        {
            "run_id": expected.run_id,
            "generation": expected.generation,
            "invocation_id": expected.invocation_id,
            "execution_sha256": expected.execution_sha256,
        },
    )
    return expected


def execution_started_at(engine: Engine, run_id: UUID) -> datetime | None:
    """Read immutable first-claim timing for diagnostics, never reset it."""
    with engine.connect() as connection:
        value = connection.execute(
            text("SELECT started_at FROM lab.director_execution_contracts WHERE run_id=:run_id"),
            {"run_id": run_id},
        ).scalar_one_or_none()
    return value.astimezone(UTC) if isinstance(value, datetime) else None
