"""Disabled CPU draft for proposal2; not an agreed or deployable launch producer.

No transport, service composition, GPU allocation or process effects exist here.
Trusted composition must authenticate the issuer and supply a bounded verifier
that checks current policy, recursive source/config pins, original generations,
prerequisites and unit absence inside each transaction. A hash is not authority.
Claim additionally verifies an already durable AOS intent, before any OS spawn.
The spawn gap and actual post-spawn caller guard remain integration obligations;
neither verify nor a status response grants model/backend or cleanup rights.
"""

from __future__ import annotations

import math
import os
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lab.llm.aos_gpu_control_store import canonical, digest

SHA = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ID = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
BOOT = Annotated[str, Field(pattern=r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")]
UINT = Annotated[int, Field(ge=0, le=2**53 - 1)]
Operation = Literal["issue", "verify", "claim", "status", "revoke"]


class LaunchLedgerError(RuntimeError):
    """Stable, bounded failure code; never exposes SQL or private pins."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


class ProcessGeneration(_Strict):
    """An existing observed process, never a hypothetical future shared caller."""

    uid: UINT
    pid: Annotated[int, Field(ge=1, le=2**31 - 1)]
    start_ticks: Annotated[int, Field(ge=1, le=2**53 - 1)]
    boot_id: BOOT


class ServiceGeneration(ProcessGeneration):
    """Observed service invocation bound to its process and original cgroup."""

    unit: Annotated[str, Field(max_length=255, pattern=r"^[a-zA-Z0-9_.@-]+\.service$")]
    invocation_id: ID
    control_group: Annotated[str, Field(min_length=1, max_length=4096)]

    @model_validator(mode="after")
    def absolute_group(self) -> Self:
        """Reject relative or traversing cgroups."""
        _absolute(self.control_group)
        return self


class LaunchPins(_Strict):
    """Exact reviewed dependency/config closure hashes, checked by composition."""

    aos_root: Annotated[str, Field(max_length=4096)]
    scientist_root: Annotated[str, Field(max_length=4096)]
    aos_import_closure_sha256: SHA
    scientist_import_closure_sha256: SHA
    python_identity_sha256: SHA
    launcher_sha256: SHA
    config_map_sha256: SHA
    model_profile_sha256: SHA
    reviewed_launch_input_sha256: SHA
    policy_revision_sha256: SHA
    contract_sha256: SHA

    @model_validator(mode="after")
    def absolute_roots(self) -> Self:
        """Keep both reviewed source roots explicit."""
        _absolute(self.aos_root)
        _absolute(self.scientist_root)
        return self


class LaunchBinding(_Strict):
    """Independent closed draft schema, intentionally outside ControlPolicy."""

    schema_id: Literal["aos-scientist.shared-launch.v1-proposal2.draft"] = Field(alias="schema")
    request_id: ID
    review_id: ID
    operation: Literal["launch_shared_desktop"]
    purpose: Literal["reviewed_shared_desktop"]
    issuer: ProcessGeneration
    broker: ServiceGeneration
    manager: ProcessGeneration
    unit: Literal["swapp-aos-gpu-shared-desktop-default.service"]
    uid: UINT
    boot_id: BOOT
    app_session: Annotated[str, Field(pattern=r"^app-[0-9a-f]{32}$")]
    workspace: Annotated[str, Field(max_length=4096)]
    workspace_device: UINT
    workspace_inode: Annotated[int, Field(ge=1, le=2**53 - 1)]
    workspace_uid: UINT
    plan_sha256: SHA
    provision_sha256: SHA
    predecessor_identity_sha256: SHA | None
    pins: LaunchPins
    drain_seal_sha256: SHA
    native_maintenance_sha256: SHA
    prelaunch_exclusion_sha256: SHA
    prerequisite_deadline_boottime: Annotated[float, Field(gt=0)]
    clock: Literal["CLOCK_BOOTTIME"]
    issued_boottime: Annotated[float, Field(ge=0)]
    expires_boottime: Annotated[float, Field(gt=0)]

    @model_validator(mode="after")
    def original_scope(self) -> Self:
        """Validate finite original clocks and consistent current principals."""
        _absolute(self.workspace)
        if not 0 < self.expires_boottime - self.issued_boottime <= 900:
            raise ValueError("original lifetime must be positive and at most 900 seconds")
        if self.expires_boottime > self.prerequisite_deadline_boottime:
            raise ValueError("launch cannot outlive its original prerequisite evidence")
        if any(item.boot_id != self.boot_id for item in (self.issuer, self.broker, self.manager)):
            raise ValueError("all original generations must belong to the same boot")
        if any(
            value != self.uid
            for value in (self.workspace_uid, self.issuer.uid, self.manager.uid, self.broker.uid)
        ):
            raise ValueError("issuer, manager, broker and workspace require the reviewed UID")
        return self

    def sha256(self) -> str:
        """Digest all immutable original bindings, including original deadlines."""
        return digest(self.model_dump(mode="json", by_alias=True))


class DurableLaunchIntent(_Strict):
    """Pointer to AOS intent; verifier must independently prove durable bytes."""

    request_id: ID
    binding_sha256: SHA
    intent_sha256: SHA
    marker: Annotated[str, Field(max_length=4096)]

    @model_validator(mode="after")
    def absolute_marker(self) -> Self:
        """Require an explicit marker path; integrity is not durable authority."""
        _absolute(self.marker)
        return self


class CurrentVerifier(Protocol):
    """Required trusted boundary provided by future reviewed composition."""

    def __call__(
        self,
        connection: sqlite3.Connection,
        binding: LaunchBinding,
        operation: Operation,
        intent: DurableLaunchIntent | None,
    ) -> None:
        """Raise on denial; authenticate real current identities, never wire assertions.

        Issue/verify/claim require fresh admission policy and source checks.
        Claim also checks AOS intent durability and prelaunch exclusion. Status
        and revoke require separate original-target read/control authority even
        after expiry, reboot or policy disablement. Cleanup rights are external.
        The connection is for bounded read checks, not commit/rollback or writes.
        """


@dataclass(frozen=True, slots=True)
class LaunchStatus:
    """Original state readback, never spawn permission."""

    request_id: str
    binding_sha256: str
    state: Literal["reviewed", "consumed", "revoked"]
    consumed: bool
    cleanup_required: bool
    expired: bool


@dataclass(frozen=True, slots=True)
class ClaimResult:
    """Distinguish the original consumption from status-only duplicate replies."""

    status: LaunchStatus
    consumed_now: bool
    # Only the original successful claim returns true. It is not GPU authority.


def _absolute(value: str) -> None:
    if not value.startswith("/") or ".." in Path(value).parts or "\x00" in value:
        raise ValueError("an absolute path without traversal is required")


def _boottime() -> float:
    # Suspend must consume the original window; MONOTONIC fallback is invalid.
    return time.clock_gettime(time.CLOCK_BOOTTIME)


class SharedLaunchLedger:
    """Explicit opt-in draft records in the existing sole arbiter database.

    Construction is inert. initialize_draft_schema is an explicit offline/test
    mutation and never initializes an allocator. No records are deleted; lost
    claim ACK and crashes remain consumed with an unresolved cleanup obligation.
    """

    def __init__(
        self,
        database: Path,
        *,
        draft_enabled: bool = False,
        verifier: CurrentVerifier | None = None,
        clock: Callable[[], float] = _boottime,
        boot_id: Callable[[], str] = lambda: (
            Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        ),
    ) -> None:
        self.database = Path(database).absolute()
        self._enabled = draft_enabled
        self._verifier = verifier
        self._clock = clock
        self._boot_id = boot_id

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        if not self._enabled or self._verifier is None:
            raise LaunchLedgerError("draft_disabled")
        if self.database.resolve() != self.database:
            raise LaunchLedgerError("private_arbiter_required")
        for path, directory in ((self.database.parent, True), (self.database, False)):
            info = path.lstat()
            if (
                path.is_symlink()
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077
                or (not directory and info.st_nlink != 1)
                or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            ):
                raise LaunchLedgerError("private_arbiter_required")
        connection = sqlite3.connect(
            self.database.as_uri() + "?mode=rw", uri=True, timeout=1, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        try:
            if not write:
                connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gpu_turn_state'"
                ).fetchone()
                is None
            ):
                raise LaunchLedgerError("canonical_arbiter_required")
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            raise LaunchLedgerError("ledger_unavailable") from exc
        finally:
            connection.close()

    def initialize_draft_schema(self) -> None:
        """Explicitly initialize only this draft table in an existing arbiter."""
        with self._transaction(write=True) as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS aos_shared_launch_draft_v1 (
                request_id TEXT PRIMARY KEY,
                binding_sha256 TEXT NOT NULL CHECK(length(binding_sha256)=64),
                target_scope_sha256 TEXT NOT NULL UNIQUE,
                target_uid INTEGER NOT NULL,
                binding_json TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('reviewed','consumed','revoked')),
                intent_json TEXT,
                consumed_boottime REAL,
                revoked_boottime REAL,
                cleanup_required INTEGER NOT NULL DEFAULT 0 CHECK(cleanup_required IN (0,1)),
                CHECK((intent_json IS NULL) = (consumed_boottime IS NULL)),
                CHECK((consumed_boottime IS NOT NULL) = (cleanup_required=1))
            )""")

    def _current(
        self,
        connection: sqlite3.Connection,
        binding: LaunchBinding,
        operation: Operation,
        intent: DurableLaunchIntent | None = None,
    ) -> None:
        if self._verifier is None:
            raise LaunchLedgerError("draft_disabled")
        self._verifier(connection, binding, operation, intent)

    def _time(self) -> float:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise LaunchLedgerError("invalid_boottime")
        if not math.isfinite(value) or value < 0:
            raise LaunchLedgerError("invalid_boottime")
        return float(value)

    def _live(self, binding: LaunchBinding) -> float:
        if self._boot_id() != binding.boot_id:
            raise LaunchLedgerError("wrong_boot")
        now = self._time()
        if not binding.issued_boottime <= now < binding.expires_boottime:
            raise LaunchLedgerError("expired_or_not_yet_issued")
        return now

    @staticmethod
    def _row(connection: sqlite3.Connection, binding: LaunchBinding) -> sqlite3.Row | None:
        row = connection.execute(
            "SELECT * FROM aos_shared_launch_draft_v1 WHERE request_id=?", (binding.request_id,)
        ).fetchone()
        if row is not None and not isinstance(row, sqlite3.Row):
            raise LaunchLedgerError("ledger_corrupt")
        if row is not None and (
            row["binding_sha256"] != binding.sha256()
            or row["binding_json"] != canonical(binding.model_dump(mode="json", by_alias=True))
        ):
            raise LaunchLedgerError("request_conflict")
        return row

    def _status(self, row: sqlite3.Row, binding: LaunchBinding) -> LaunchStatus:
        return LaunchStatus(
            binding.request_id,
            row["binding_sha256"],
            row["state"],
            row["consumed_boottime"] is not None,
            bool(row["cleanup_required"]),
            self._boot_id() != binding.boot_id or self._time() >= binding.expires_boottime,
        )

    def issue(self, binding: LaunchBinding) -> LaunchStatus:
        """Trusted review insertion; duplicate original binding returns status only."""
        with self._transaction(write=True) as connection:
            row = self._row(connection, binding)
            self._current(connection, binding, "status" if row is not None else "issue")
            if row is None:
                self._live(binding)
                scope = digest([binding.uid, binding.unit, binding.app_session])
                if (
                    connection.execute(
                        "SELECT 1 FROM aos_shared_launch_draft_v1 WHERE target_scope_sha256=?",
                        (scope,),
                    ).fetchone()
                    is not None
                ):
                    raise LaunchLedgerError("target_scope_reused")
                if (
                    connection.execute(
                        "SELECT COUNT(*) FROM aos_shared_launch_draft_v1"
                    ).fetchone()[0]
                    >= 10_000
                ):
                    raise LaunchLedgerError("capacity_exhausted")
                connection.execute(
                    "INSERT INTO aos_shared_launch_draft_v1 "
                    "(request_id,binding_sha256,target_scope_sha256,target_uid,binding_json,state) "
                    "VALUES(?,?,?,?,?,'reviewed')",
                    (
                        binding.request_id,
                        binding.sha256(),
                        scope,
                        binding.uid,
                        canonical(binding.model_dump(mode="json", by_alias=True)),
                    ),
                )
                row = self._row(connection, binding)
            if row is None:
                raise LaunchLedgerError("ledger_corrupt")
            return self._status(row, binding)

    def status(self, binding: LaunchBinding) -> LaunchStatus:
        """Authenticated original-target status remains available after expiry."""
        with self._transaction(write=False) as connection:
            row = self._row(connection, binding)
            if row is None:
                raise LaunchLedgerError("unknown_request")
            self._current(connection, binding, "status")
            return self._status(row, binding)

    def verify(self, binding: LaunchBinding) -> LaunchStatus:
        """Repeatable read-only freshness check, never single-use consumption."""
        with self._transaction(write=False) as connection:
            row = self._row(connection, binding)
            if row is None or row["state"] == "revoked":
                raise LaunchLedgerError("admission_closed")
            self._current(connection, binding, "verify")
            self._live(binding)
            return self._status(row, binding)

    def claim(self, binding: LaunchBinding, intent: DurableLaunchIntent) -> ClaimResult:
        """Consume once after durable AOS intent; commit before returning to spawner."""
        if intent.request_id != binding.request_id or intent.binding_sha256 != binding.sha256():
            raise LaunchLedgerError("intent_conflict")
        with self._transaction(write=True) as connection:
            row = self._row(connection, binding)
            if row is None:
                raise LaunchLedgerError("unknown_request")
            if row["intent_json"] is not None and row["intent_json"] != canonical(
                intent.model_dump(mode="json")
            ):
                raise LaunchLedgerError("intent_conflict")
            if row["state"] != "reviewed":
                self._current(connection, binding, "status")
                return ClaimResult(self._status(row, binding), consumed_now=False)
            self._current(connection, binding, "claim", intent)
            consumed_at = self._live(binding)
            if (
                connection.execute(
                    "SELECT 1 FROM aos_shared_launch_draft_v1 WHERE target_uid=? "
                    "AND cleanup_required=1",
                    (binding.uid,),
                ).fetchone()
                is not None
            ):
                raise LaunchLedgerError("original_cleanup_unresolved")
            connection.execute(
                "UPDATE aos_shared_launch_draft_v1 SET state='consumed',intent_json=?,"
                "consumed_boottime=?,cleanup_required=1 WHERE request_id=? AND state='reviewed'",
                (canonical(intent.model_dump(mode="json")), consumed_at, binding.request_id),
            )
            row = self._row(connection, binding)
            if row is None:
                raise LaunchLedgerError("ledger_corrupt")
            return ClaimResult(self._status(row, binding), consumed_now=True)

    def revoke(self, binding: LaunchBinding) -> LaunchStatus:
        """Close admission; preserve intent/tombstone and any cleanup obligation."""
        with self._transaction(write=True) as connection:
            row = self._row(connection, binding)
            if row is None:
                raise LaunchLedgerError("unknown_request")
            self._current(connection, binding, "revoke")
            if row["state"] != "revoked":
                connection.execute(
                    "UPDATE aos_shared_launch_draft_v1 SET state='revoked',revoked_boottime=? "
                    "WHERE request_id=?",
                    (self._time(), binding.request_id),
                )
            row = self._row(connection, binding)
            if row is None:
                raise LaunchLedgerError("ledger_corrupt")
            return self._status(row, binding)
