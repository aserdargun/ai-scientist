"""Private, durable control records sharing the sole GPU arbiter database.

This module has no transport, allocator, caller release, or process operations.
Its mutation APIs are trusted service composition; the transport authenticates
the full peer and invokes the verifier at its transaction boundary. Scheduler
hooks take the scheduler's existing write transaction, so a cancel cannot race
an allocation or its terminal receipt. Evidence APIs are internal runtime hooks.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import time
from collections.abc import Callable, Mapping
from contextlib import closing
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PEER_FIELDS = frozenset(
    {
        "uid",
        "pid",
        "start_ticks",
        "boot_id",
        "unit",
        "invocation_id",
        "control_group",
        "parent_pid",
        "parent_start_ticks",
    }
)
TARGET_FIELDS = frozenset({"request_id", "request_sha256", "original_peer_generation_sha256"})
TERMINAL = frozenset({"completed", "canceled", "expired", "failed"})
control_deadline: ContextVar[float | None] = ContextVar("aos_control_deadline", default=None)
MAX_REQUESTS = 10_000
MAX_CONTROL_IDS = 100_000
MAX_CLEANUP_GRANTS = 10_000
MAX_TARGET_CLEANUP_GRANTS = 8
CLEANUP_GRANT_RESERVE = 8
CONTROL_REFRESH_SECONDS = 60
CONTROL_DRAIN_SECONDS = 30
CONTROL_RECOVERY_MARGIN = 2
MAX_TARGET_REFRESHES = 40
CLEANUP_OPS = frozenset({"status", "cancel", "reconcile"})


class ControlStoreError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ControlCanceled(ControlStoreError):
    def __init__(self) -> None:
        super().__init__("request_conflict")


@dataclass(frozen=True, slots=True)
class AdmissionGrant:
    """Trusted in-process authority; never accepted from an infer/control frame.

    Canonical bytes contain stable pins only. The callback rechecks current
    service/policy/capability freshness inside the first-intent transaction.
    """

    binding_json: str
    verify_current: Callable[[], None]


@dataclass(frozen=True, slots=True)
class ControlAuthority:
    """Trusted control-only authority; never accepted by infer registration."""

    binding_json: str | None
    cleanup_grant_json: str | None
    verify_current: Callable[[], None]


def validate_profile_pin(value: Any, *, require_output_contract: bool = False) -> dict[str, Any]:
    legacy = {"deployment_digest", "manifest_sha256", "config_sha256", "response_schema_sha256"}
    if (
        not isinstance(value, dict)
        or set(value) not in (legacy, legacy | {"output_contract"})
        or any(not _hex(value[key], 64) for key in legacy)
    ):
        raise ControlStoreError("invalid_frame")
    if "output_contract" not in value:
        if require_output_contract:
            raise ControlStoreError("unauthorized")
        return value
    pin = value["output_contract"]
    if (
        not isinstance(pin, dict)
        or set(pin) != {"name", "version", "bundle_sha256"}
        or pin["name"] != "aos-scientist-profile-output.v2"
        or type(pin["version"]) is not int
        or pin["version"] != 2
        or not _hex(pin["bundle_sha256"], 64)
    ):
        raise ControlStoreError("invalid_frame")
    return value


def validate_admission_binding(
    value: Any, *, require_output_contract: bool = False
) -> dict[str, Any]:
    """Closed stable-pin object; freshness cannot silently enter its hash."""
    keys = {
        "server_generation",
        "caller_generation",
        "policy_sha256",
        "source_fingerprints",
        "profile_id",
        "profile_pin",
        "infer_schema",
        "control_schema",
        "terminal_schema",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise ControlStoreError("invalid_frame")
    for name, fields in (
        ("caller_generation", PEER_FIELDS),
        ("server_generation", PEER_FIELDS - {"parent_pid", "parent_start_ticks"}),
    ):
        generation = value[name]
        if not isinstance(generation, dict) or set(generation) != fields:
            raise ControlStoreError("invalid_frame")
        numeric = fields & {"uid", "pid", "start_ticks", "parent_pid", "parent_start_ticks"}
        if any(
            type(generation[key]) is not int or not 0 <= generation[key] <= 2**53 - 1
            for key in numeric
        ):
            raise ControlStoreError("invalid_frame")
        if any(
            not isinstance(generation[key], str)
            or not generation[key]
            or len(generation[key]) > 4096
            for key in fields - numeric
        ):
            raise ControlStoreError("invalid_frame")
        if (
            not 1 <= generation["pid"] <= 2**31 - 1
            or ("parent_pid" in fields and not 1 <= generation["parent_pid"] <= 2**31 - 1)
            or re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                generation["boot_id"],
            )
            is None
            or len(generation["unit"]) > 255
            or re.fullmatch(r"[a-zA-Z0-9_.@-]+\.service", generation["unit"]) is None
            or not _hex(generation["invocation_id"], 32)
            or not generation["control_group"].startswith("/")
            or ".." in Path(generation["control_group"]).parts
        ):
            raise ControlStoreError("invalid_frame")
    sources = value["source_fingerprints"]
    profile = value["profile_pin"]
    if (
        not _hex(value["policy_sha256"], 64)
        or not isinstance(sources, dict)
        or set(sources) != {"scientist", "aos"}
        or not all(_hex(pin, 64) for pin in sources.values())
        or not isinstance(value["profile_id"], str)
        or value["profile_id"]
        not in {"aos.decider.turn.v1", "aos.bonsai.recovery.v1", "aos.bonsai.vision.v1"}
    ):
        raise ControlStoreError("invalid_frame")
    validate_profile_pin(profile, require_output_contract=require_output_contract)
    for key, name in (
        ("infer_schema", "aos-scientist-runtime.v1"),
        ("control_schema", "aos-scientist-control.v1"),
        ("terminal_schema", "aos-scientist-terminal.v1"),
    ):
        pin = value[key]
        if (
            not isinstance(pin, dict)
            or set(pin) != {"name", "version", "sha256"}
            or pin["name"] != name
            or type(pin["version"]) is not int
            or pin["version"] != 1
            or not _hex(pin["sha256"], 64)
        ):
            raise ControlStoreError("invalid_frame")
    return value


def _remaining() -> float:
    deadline = control_deadline.get()
    remaining = 1.0 if deadline is None else min(1.0, deadline - time.monotonic())
    if remaining <= 0:
        raise ControlStoreError("deadline_exceeded")
    return remaining


class _Connection(sqlite3.Connection):
    """Apply the remaining call budget to every DB wait and hide SQL details."""

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        try:
            super().execute(f"PRAGMA busy_timeout={max(0, int(_remaining() * 1000))}")
            return super().execute(sql, parameters)
        except sqlite3.Error as exc:
            code = getattr(exc, "sqlite_errorcode", 0) & 255
            raise ControlStoreError("busy" if code in {5, 6} else "internal_unavailable") from exc


def canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


# Draft descriptor pin, not a claim that the complete recursive schema is agreed.
TERMINAL_SCHEMA_HASH = digest(
    {
        "schema": "aos-scientist-terminal.v1",
        "fields": [
            "schema",
            "request_id",
            "request_sha256",
            "original_principal",
            "profile_id",
            "deployment_digest",
            "profile_config_sha256",
            "response_schema_sha256",
            "admission_binding",
            "admission_binding_sha256",
            "original_budget",
            "allocation_binding_sha256",
            "child_generation",
            "drain_evidence_sha256",
            "no_admission_evidence_sha256",
            "release_outcome",
            "terminal_state",
            "reason_code",
            "result_sha256",
            "recorded_boot_id",
            "recorded_boottime",
            "receipt_sha256",
        ],
    }
)


def _boottime() -> float:
    return time.clock_gettime(getattr(time, "CLOCK_BOOTTIME", time.CLOCK_MONOTONIC))


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()


def _hex(value: object, length: int) -> bool:
    return isinstance(value, str) and re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is not None


class ControlStore:
    """Bounded DB waits; persistent records are never deleted or adopted.

    ``peer_verifier`` is a trusted, bounded liveness check supplied by service
    composition. Without it, only already authenticated internal callers may
    use peer APIs. Neither transport credentials nor physical drain claims may
    be forwarded from wire data to the internal evidence/lease hooks.
    """

    def __init__(
        self,
        database: Path,
        *,
        clock: Callable[[], float] = _boottime,
        boot_id: Callable[[], str] = _boot_id,
        peer_verifier: Callable[[dict[str, Any]], bool] | None = None,
    ) -> None:
        self.database = Path(database)
        self._clock = clock
        self._boot_id = boot_id
        self._peer_verifier = peer_verifier
        parent = self.database.parent.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or self.database.parent.is_symlink()
            or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) & 0o077
        ):
            raise ControlStoreError("internal_unavailable")
        try:
            descriptor = os.open(
                self.database, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
            )
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        info = self.database.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ControlStoreError("internal_unavailable")
        with closing(self._connect()) as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS aos_control_requests (
                    request_id TEXT PRIMARY KEY,
                    request_sha256 TEXT NOT NULL,
                    principal_json TEXT NOT NULL,
                    principal_sha256 TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    deployment_digest TEXT NOT NULL,
                    profile_config_sha256 TEXT,
                    response_schema_sha256 TEXT,
                    admission_binding_json TEXT,
                    admission_binding_sha256 TEXT,
                    budget_json TEXT,
                    original_deadline REAL,
                    queue_deadline REAL,
                    original_boot_id TEXT NOT NULL,
                    created_boottime REAL NOT NULL,
                    state TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    first_cancel_boottime REAL,
                    allocation_json TEXT,
                    allocation_sha256 TEXT,
                    ready_deadlines_json TEXT,
                    handoff_stage TEXT,
                    child_json TEXT,
                    drain_json TEXT,
                    no_admission_json TEXT,
                    result_json TEXT,
                    receipt_json TEXT,
                    cleanup_receipt_sha256 TEXT,
                    cleanup_attempts INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS aos_control_idempotency (
                    principal_sha256 TEXT NOT NULL,
                    control_id TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    PRIMARY KEY(principal_sha256,control_id)
                );
            """)
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(aos_control_requests)")
            }
            connection.execute(
                "CREATE TABLE IF NOT EXISTS aos_cleanup_grants ("
                "grant_sha256 TEXT PRIMARY KEY,request_id TEXT NOT NULL,grant_json TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS aos_control_reservations (request_id TEXT PRIMARY KEY,"
                "quota_json TEXT NOT NULL,quota_sha256 TEXT NOT NULL,"
                "capability_remaining INTEGER NOT NULL "
                "CHECK(capability_remaining BETWEEN 0 AND 40),"
                "status_remaining INTEGER NOT NULL CHECK(status_remaining BETWEEN 0 AND 40),"
                "cancel_remaining INTEGER NOT NULL CHECK(cancel_remaining BETWEEN 0 AND 2),"
                "reconcile_remaining INTEGER NOT NULL CHECK(reconcile_remaining BETWEEN 0 AND 40),"
                "grant_remaining INTEGER NOT NULL CHECK(grant_remaining BETWEEN 0 AND 8))"
            )
            control_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(aos_control_idempotency)")
            }
            for column in ("target_request_id", "operation", "response_json", "capability_sha256"):
                if column not in control_columns:
                    connection.execute(
                        f"ALTER TABLE aos_control_idempotency ADD COLUMN {column} TEXT"
                    )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS aos_control_capability_lookup ON "
                "aos_control_idempotency(principal_sha256,capability_sha256)"
            )
            for column in (
                "admission_binding_json",
                "admission_binding_sha256",
                "original_cleanup_authorization_json",
                "original_cleanup_authorization_sha256",
            ):
                if column not in columns:
                    connection.execute(f"ALTER TABLE aos_control_requests ADD COLUMN {column} TEXT")
            if "cleanup_receipt_sha256" not in columns:
                connection.execute(
                    "ALTER TABLE aos_control_requests ADD COLUMN cleanup_receipt_sha256 TEXT"
                )
            if "cleanup_attempts" not in columns:
                connection.execute(
                    "ALTER TABLE aos_control_requests ADD COLUMN "
                    "cleanup_attempts INTEGER NOT NULL DEFAULT 0"
                )

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                self.database, timeout=_remaining(), isolation_level=None, factory=_Connection
            )
        except sqlite3.Error as exc:
            raise ControlStoreError("internal_unavailable") from exc
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=1000")
        return connection

    def _now(self) -> float:
        value = self._clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ControlStoreError("internal_unavailable")
        return float(value)

    def _peer(self, peer: Mapping[str, Any]) -> dict[str, Any]:
        _remaining()
        peer = dict(peer)
        if set(peer) != PEER_FIELDS:
            raise ControlStoreError("unauthorized")
        if any(
            type(peer[key]) is not int or peer[key] < 0
            for key in ("uid", "pid", "start_ticks", "parent_pid", "parent_start_ticks")
        ):
            raise ControlStoreError("unauthorized")
        if any(
            not isinstance(peer[key], str) or not peer[key]
            for key in PEER_FIELDS
            - {"uid", "pid", "start_ticks", "parent_pid", "parent_start_ticks"}
        ):
            raise ControlStoreError("unauthorized")
        if self._peer_verifier is not None and not self._peer_verifier(peer):
            raise ControlStoreError("stale_generation")
        return peer

    def _target(self, peer: Mapping[str, Any], target: Mapping[str, Any]) -> None:
        if (
            set(target) != TARGET_FIELDS
            or not _hex(target.get("request_id"), 32)
            or any(
                not _hex(target.get(key), 64)
                for key in ("request_sha256", "original_peer_generation_sha256")
            )
        ):
            raise ControlStoreError("invalid_frame")
        if target["original_peer_generation_sha256"] != digest(peer):
            raise ControlStoreError("history_denied")

    @staticmethod
    def _row(connection: sqlite3.Connection, request_id: str) -> sqlite3.Row | None:
        row = connection.execute(
            "SELECT * FROM aos_control_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is not None and not isinstance(row, sqlite3.Row):
            raise ControlStoreError("internal_unavailable")
        return row

    @staticmethod
    def _request_capacity(connection: sqlite3.Connection) -> None:
        if (
            connection.execute("SELECT COUNT(*) FROM aos_control_requests").fetchone()[0]
            >= MAX_REQUESTS
        ):
            raise ControlStoreError("busy")

    @staticmethod
    def _reserved(connection: sqlite3.Connection, *, grants: bool = False) -> int:
        controls, grant_slots, corrupt = connection.execute(
            "SELECT COALESCE(SUM(capability_remaining+status_remaining+"
            "cancel_remaining+reconcile_remaining),0), COALESCE(SUM(grant_remaining),0),"
            "COALESCE(SUM(CASE WHEN "
            "typeof(capability_remaining)!='integer' OR capability_remaining NOT BETWEEN 0 AND 40 "
            "OR typeof(status_remaining)!='integer' OR status_remaining NOT BETWEEN 0 AND 40 "
            "OR typeof(cancel_remaining)!='integer' OR cancel_remaining NOT BETWEEN 0 AND 2 "
            "OR typeof(reconcile_remaining)!='integer' OR reconcile_remaining NOT BETWEEN 0 AND 40 "
            "OR typeof(grant_remaining)!='integer' OR grant_remaining NOT BETWEEN 0 AND 8 "
            "THEN 1 ELSE 0 END),0) "
            "FROM aos_control_reservations"
        ).fetchone()
        if corrupt:
            raise ControlStoreError("internal_unavailable")
        return int(grant_slots if grants else controls)

    def _reserve_controls(
        self, connection: sqlite3.Connection, request_id: str, budget: Mapping[str, Any] | None
    ) -> None:
        if (
            type(CONTROL_REFRESH_SECONDS) is not int
            or CONTROL_REFRESH_SECONDS < 1
            or type(CLEANUP_GRANT_RESERVE) is not int
            or not 1 <= CLEANUP_GRANT_RESERVE <= 8
            or type(MAX_TARGET_REFRESHES) is not int
            or not 1 <= MAX_TARGET_REFRESHES <= 40
        ):
            raise ControlStoreError("invalid_frame")
        if connection.execute(
            "SELECT 1 FROM aos_control_reservations WHERE request_id=?", (request_id,)
        ).fetchone():
            return
        # Slots cover one operation of each observational kind per refresh period,
        # plus bounded crash/recovery margin. Poll no faster than this period.
        raw_durations = (
            [0, 0] if budget is None else [budget.get("queue_seconds"), budget.get("total_seconds")]
        )
        durations: list[float] = []
        for value in raw_durations:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ControlStoreError("invalid_frame")
            durations.append(float(value))
        refreshes = (
            math.ceil((sum(durations) + CONTROL_DRAIN_SECONDS) / CONTROL_REFRESH_SECONDS)
            + CONTROL_RECOVERY_MARGIN
        )
        if refreshes > MAX_TARGET_REFRESHES or CLEANUP_GRANT_RESERVE > MAX_TARGET_CLEANUP_GRANTS:
            raise ControlStoreError("busy")
        slots = {"capability": refreshes, "status": refreshes, "cancel": 2, "reconcile": refreshes}
        used = connection.execute("SELECT COUNT(*) FROM aos_control_idempotency").fetchone()[0]
        grants = connection.execute("SELECT COUNT(*) FROM aos_cleanup_grants").fetchone()[0]
        if (
            used + self._reserved(connection) + sum(slots.values()) > MAX_CONTROL_IDS
            or grants + self._reserved(connection, grants=True) + CLEANUP_GRANT_RESERVE
            > MAX_CLEANUP_GRANTS
        ):
            raise ControlStoreError("busy")
        quota = {
            **slots,
            "cleanup_grants": CLEANUP_GRANT_RESERVE,
            "refresh_seconds": CONTROL_REFRESH_SECONDS,
            "drain_seconds": CONTROL_DRAIN_SECONDS,
            "recovery_margin": CONTROL_RECOVERY_MARGIN,
            "original_budget_sha256": digest(None if budget is None else dict(budget)),
        }
        connection.execute(
            "INSERT INTO aos_control_reservations VALUES(?,?,?,?,?,?,?,?)",
            (
                request_id,
                canonical(quota),
                digest(quota),
                slots["capability"],
                slots["status"],
                slots["cancel"],
                slots["reconcile"],
                CLEANUP_GRANT_RESERVE,
            ),
        )

    def _reservation(self, connection: sqlite3.Connection, request_id: str) -> sqlite3.Row | None:
        reservation = connection.execute(
            "SELECT * FROM aos_control_reservations WHERE request_id=?", (request_id,)
        ).fetchone()
        if reservation is not None and not isinstance(reservation, sqlite3.Row):
            raise ControlStoreError("internal_unavailable")
        if reservation is not None:
            quota = json.loads(reservation["quota_json"])
            row = self._row(connection, request_id)
            if (
                row is None
                or canonical(quota) != reservation["quota_json"]
                or digest(quota) != reservation["quota_sha256"]
                or quota["original_budget_sha256"]
                != digest(None if row["budget_json"] is None else json.loads(row["budget_json"]))
                or any(
                    not 0 <= reservation[operation + "_remaining"] <= quota[operation]
                    for operation in ("capability", "status", "cancel", "reconcile")
                )
                or not 0 <= reservation["grant_remaining"] <= quota["cleanup_grants"]
            ):
                raise ControlStoreError("request_conflict")
        return reservation

    def _consume_control_capacity(
        self,
        connection: sqlite3.Connection,
        request_id: str | None,
        operation: str | None,
        *,
        grant: bool = False,
    ) -> None:
        reservation = None if request_id is None else self._reservation(connection, request_id)
        if reservation is not None:
            updates = {
                "grant": (
                    "grant_remaining",
                    "UPDATE aos_control_reservations "
                    "SET grant_remaining=grant_remaining-1 WHERE request_id=?",
                ),
                "capability": (
                    "capability_remaining",
                    "UPDATE aos_control_reservations "
                    "SET capability_remaining=capability_remaining-1 WHERE request_id=?",
                ),
                "status": (
                    "status_remaining",
                    "UPDATE aos_control_reservations "
                    "SET status_remaining=status_remaining-1 WHERE request_id=?",
                ),
                "cancel": (
                    "cancel_remaining",
                    "UPDATE aos_control_reservations "
                    "SET cancel_remaining=cancel_remaining-1 WHERE request_id=?",
                ),
                "reconcile": (
                    "reconcile_remaining",
                    "UPDATE aos_control_reservations "
                    "SET reconcile_remaining=reconcile_remaining-1 WHERE request_id=?",
                ),
            }
            update = updates.get("grant" if grant else operation or "")
            if update is None or reservation[update[0]] <= 0:
                raise ControlStoreError("busy")
            connection.execute(update[1], (request_id,))
            return
        # Legacy pre-reservation records retain best-effort cleanup, never borrow
        # another target's slots or gain a new inference budget.
        count_query = (
            "SELECT COUNT(*) FROM aos_cleanup_grants"
            if grant
            else "SELECT COUNT(*) FROM aos_control_idempotency"
        )
        limit = MAX_CLEANUP_GRANTS if grant else MAX_CONTROL_IDS
        if (
            connection.execute(count_query).fetchone()[0] + self._reserved(connection, grants=grant)
            >= limit
        ):
            raise ControlStoreError("busy")

    def _require_reservation(self, connection: sqlite3.Connection, request_id: str) -> None:
        if self._reservation(connection, request_id) is None:
            raise ControlStoreError("unauthorized")

    def _exact(
        self,
        row: sqlite3.Row,
        peer: Mapping[str, Any],
        target: Mapping[str, Any],
        profile_id: str,
        deployment_digest: str,
    ) -> None:
        if row["principal_json"] != canonical(peer):
            raise ControlStoreError("history_denied")
        if (row["request_sha256"], row["profile_id"], row["deployment_digest"]) != (
            target["request_sha256"],
            profile_id,
            deployment_digest,
        ):
            raise ControlStoreError("request_conflict")

    def register_intent(
        self,
        peer: Mapping[str, Any],
        request_id: str,
        request_sha256: str,
        profile_id: str,
        deployment_digest: str,
        profile_config_sha256: str,
        response_schema_sha256: str,
        deadline: float,
        budget: Mapping[str, Any],
        *,
        admission: AdmissionGrant | None = None,
    ) -> float:
        """Return the original deadline, even when reconnect presents a later one."""
        if (
            not _hex(request_id, 32)
            or any(
                not _hex(value, 64)
                for value in (
                    request_sha256,
                    deployment_digest,
                    profile_config_sha256,
                    response_schema_sha256,
                )
            )
            or isinstance(deadline, bool)
            or not math.isfinite(deadline)
        ):
            raise ControlStoreError("invalid_frame")
        budget_json = canonical(dict(budget))
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            # The observation and admission share this write transaction and
            # database: a verified never-received request cannot arrive later.
            from lab.llm.aos_no_admission_store import assert_not_observed

            assert_not_observed(connection, request_id)
            peer = self._peer(peer)
            target = {
                "request_id": request_id,
                "request_sha256": request_sha256,
                "original_peer_generation_sha256": digest(peer),
            }
            row = self._row(connection, request_id)
            if row is not None and row["cancel_requested"]:
                self._exact(row, peer, target, profile_id, deployment_digest)
                raise ControlCanceled()
            if not isinstance(admission, AdmissionGrant):
                raise ControlStoreError("unauthorized")
            admission.verify_current()
            binding = validate_admission_binding(
                json.loads(admission.binding_json), require_output_contract=True
            )
            binding_json = canonical(binding)
            binding_sha256 = digest(binding)
            if (
                binding_json != admission.binding_json
                or binding["caller_generation"] != peer
                or binding["profile_id"] != profile_id
                or binding["profile_pin"]["deployment_digest"] != deployment_digest
                or binding["profile_pin"]["config_sha256"] != profile_config_sha256
                or binding["profile_pin"]["response_schema_sha256"] != response_schema_sha256
            ):
                raise ControlStoreError("request_conflict")
            if row is not None:
                self._exact(row, peer, target, profile_id, deployment_digest)
                if self._reservation(connection, request_id) is None:
                    raise ControlStoreError("unauthorized")
                if row["cancel_requested"]:
                    raise ControlCanceled()
                if row["original_boot_id"] != self._boot_id():
                    raise ControlStoreError("stale_generation")
                if (
                    row["admission_binding_json"] != binding_json
                    or row["admission_binding_sha256"] != binding_sha256
                ):
                    # Legacy null authority is retained as evidence, never adopted.
                    raise ControlStoreError("request_conflict")
                if (
                    row["profile_config_sha256"],
                    row["response_schema_sha256"],
                    row["budget_json"],
                ) != (profile_config_sha256, response_schema_sha256, budget_json):
                    raise ControlStoreError("request_conflict")
                admission.verify_current()
                connection.commit()
                return float(row["original_deadline"])
            # Legacy scheduler history cannot be adopted by a new control intent.
            if connection.execute(
                "SELECT 1 FROM gpu_turn_requests WHERE owner='aos' AND request_id=?", (request_id,)
            ).fetchone():
                raise ControlStoreError("request_conflict")
            timestamp = self._now()
            self._request_capacity(connection)
            self._reserve_controls(connection, request_id, budget)
            if deadline <= timestamp:
                raise ControlStoreError("deadline_exceeded")
            connection.execute(
                "INSERT INTO aos_control_requests(request_id,request_sha256,principal_json,"
                "principal_sha256,"
                "profile_id,deployment_digest,profile_config_sha256,response_schema_sha256,"
                "budget_json,admission_binding_json,admission_binding_sha256,"
                "original_deadline,original_boot_id,created_boottime,state) VALUES(?,?,?,?,?,?,?,"
                "?,?,?,?,?,?,?, 'intent')",
                (
                    request_id,
                    request_sha256,
                    canonical(peer),
                    digest(peer),
                    profile_id,
                    deployment_digest,
                    profile_config_sha256,
                    response_schema_sha256,
                    budget_json,
                    binding_json,
                    binding_sha256,
                    deadline,
                    self._boot_id(),
                    timestamp,
                ),
            )
            admission.verify_current()
            connection.commit()
            return deadline

    def _remember_control_tx(
        self,
        connection: sqlite3.Connection,
        peer: Mapping[str, Any],
        control_id: str,
        request_sha256: str,
        *,
        request_id: str | None = None,
        operation: str | None = None,
        response: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if not _hex(control_id, 32) or not _hex(request_sha256, 64):
            raise ControlStoreError("invalid_frame")
        key = digest(peer)
        row = connection.execute(
            "SELECT * FROM aos_control_idempotency WHERE principal_sha256=? AND control_id=?",
            (key, control_id),
        ).fetchone()
        if row is not None:
            if row["request_sha256"] != request_sha256:
                raise ControlStoreError("request_conflict")
            if response is not None:
                if row["response_json"] is None:
                    raise ControlStoreError("request_conflict")
                original = json.loads(row["response_json"])
                if not isinstance(original, dict):
                    raise ControlStoreError("internal_unavailable")

                def stable(value: dict[str, Any]) -> dict[str, Any]:
                    return {
                        key: item
                        for key, item in value["capability"].items()
                        if key not in {"issued_boottime", "expires_boottime"}
                    }

                if stable(original) != stable(response):
                    raise ControlStoreError("request_conflict")
                return original
            return None
        self._consume_control_capacity(connection, request_id, operation)
        connection.execute(
            "INSERT INTO aos_control_idempotency(principal_sha256,control_id,"
            "request_sha256,target_request_id,operation,response_json,capability_sha256) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                key,
                control_id,
                request_sha256,
                request_id,
                operation,
                None if response is None else canonical(response),
                None if response is None else digest(response["capability"]),
            ),
        )
        return response

    def _remember_frame(
        self,
        connection: sqlite3.Connection,
        peer: Mapping[str, Any],
        target: Mapping[str, Any],
        profile_id: str,
        deployment: str,
        operation: str,
        request: Mapping[str, Any] | None,
        response: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if request is None:
            return response
        if (
            request.get("target") != target
            or request.get("profile_id") != profile_id
            or request.get("deployment_digest") != deployment
            or request.get("op") != operation
        ):
            raise ControlStoreError("request_conflict")
        return self._remember_control_tx(
            connection,
            peer,
            request["control_id"],
            digest(request),
            request_id=target["request_id"],
            operation=operation,
            response=response,
        )

    def remember_control(
        self,
        peer: Mapping[str, Any],
        control_id: str,
        request_sha256: str,
        verify_current: Callable[[], None] | None = None,
    ) -> None:
        if not _hex(control_id, 32) or not _hex(request_sha256, 64):
            raise ControlStoreError("invalid_frame")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            peer = self._peer(peer)
            if verify_current is not None:
                verify_current()
            self._remember_control_tx(connection, peer, control_id, request_sha256)
            if verify_current is not None:
                verify_current()
            connection.commit()

    def target_capability(self, peer: Mapping[str, Any], fingerprint: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT response_json FROM aos_control_idempotency "
                "WHERE principal_sha256=? AND capability_sha256=? "
                "AND target_request_id IS NOT NULL",
                (digest(self._peer(peer)), fingerprint),
            ).fetchone()
            return None if row is None else json.loads(row[0])["capability"]

    def _saved_binding(
        self, row: sqlite3.Row, *, require_output_contract: bool = False
    ) -> dict[str, Any]:
        """Legacy null authority cannot authorize allocation, launch or publication."""
        try:
            binding = validate_admission_binding(
                json.loads(row["admission_binding_json"]),
                require_output_contract=require_output_contract,
            )
            if (
                canonical(binding) != row["admission_binding_json"]
                or digest(binding) != row["admission_binding_sha256"]
                or binding["caller_generation"] != json.loads(row["principal_json"])
                or binding["profile_id"] != row["profile_id"]
                or binding["profile_pin"]["deployment_digest"] != row["deployment_digest"]
                or binding["profile_pin"]["config_sha256"] != row["profile_config_sha256"]
                or binding["profile_pin"]["response_schema_sha256"] != row["response_schema_sha256"]
            ):
                raise ControlStoreError("request_conflict")
            return binding
        except (TypeError, ValueError, KeyError) as exc:
            raise ControlStoreError("request_conflict") from exc

    def _verified_completed_result(self, row: sqlite3.Row) -> dict[str, Any]:
        """Validate immutable publication evidence without treating ticket done as success."""
        if row["cancel_requested"] or row["state"] == "canceled":
            raise ControlCanceled()
        if row["state"] != "completed" or row["receipt_json"] is None or row["result_json"] is None:
            raise ControlStoreError("request_conflict")
        try:
            receipt = json.loads(row["receipt_json"])
            result = json.loads(row["result_json"])
            principal = json.loads(row["principal_json"])
            binding = self._saved_binding(row)
            if not isinstance(receipt, dict) or not isinstance(result, dict):
                raise ControlStoreError("internal_unavailable")
            if (
                receipt.get("schema") != "aos-scientist-terminal.v1"
                or receipt.get("terminal_state") != "completed"
                or receipt.get("release_outcome") not in {"released", "recovered_released"}
                or receipt.get("receipt_sha256")
                != digest({key: value for key, value in receipt.items() if key != "receipt_sha256"})
                or receipt.get("original_principal") != principal
                or row["principal_sha256"] != digest(principal)
                or binding["caller_generation"] != principal
                or row["admission_binding_sha256"] != digest(binding)
                or receipt.get("admission_binding") != binding
                or receipt.get("admission_binding_sha256") != row["admission_binding_sha256"]
                or any(
                    receipt.get(key) != row[key]
                    for key in (
                        "request_id",
                        "request_sha256",
                        "profile_id",
                        "deployment_digest",
                        "profile_config_sha256",
                        "response_schema_sha256",
                    )
                )
                or set(result) != {"response", "usage", "generation"}
                or any(not isinstance(result[key], dict) for key in result)
                or receipt.get("result_sha256") != digest(result)
                or row["allocation_json"] is None
                or json.loads(row["allocation_json"]).get("admission_binding_sha256")
                != row["admission_binding_sha256"]
                or receipt.get("allocation_binding_sha256") != row["allocation_sha256"]
                or row["allocation_sha256"] != digest(json.loads(row["allocation_json"]))
                or row["drain_json"] is None
                or receipt.get("drain_evidence_sha256") != digest(json.loads(row["drain_json"]))
            ):
                raise ControlStoreError("internal_unavailable")
        except (TypeError, ValueError, KeyError) as exc:
            raise ControlStoreError("internal_unavailable") from exc
        return result

    def authorize_cached_result(
        self,
        connection: sqlite3.Connection,
        cached: sqlite3.Row,
    ) -> dict[str, Any] | None:
        """Join cache promotion/publication to its terminal authority in one transaction."""
        if not connection.in_transaction:
            raise ControlStoreError("internal_unavailable")
        row = self._row(connection, cached["request_id"])
        if row is None or any(
            row[key] != cached[key]
            for key in (
                "request_id",
                "request_sha256",
                "principal_json",
                "profile_id",
                "deployment_digest",
                "profile_config_sha256",
            )
        ):
            raise ControlStoreError("request_conflict")
        self._peer(json.loads(row["principal_json"]))
        if row["cancel_requested"] or row["state"] == "canceled":
            raise ControlCanceled()
        if row["state"] not in TERMINAL:
            if row["receipt_json"] is not None:
                raise ControlStoreError("internal_unavailable")
            return None
        result = self._verified_completed_result(row)
        try:
            cache_result = {
                "response": json.loads(cached["response_json"]),
                "usage": json.loads(cached["usage_json"]),
                "generation": json.loads(cached["generation_json"]),
            }
        except (TypeError, ValueError) as exc:
            raise ControlStoreError("internal_unavailable") from exc
        if digest(cache_result) != digest(result):
            raise ControlStoreError("request_conflict")
        return result

    def _cleanup_evidence(
        self,
        connection: sqlite3.Connection,
        request_id: str,
    ) -> tuple[dict[str, Any], str]:
        row = self._row(connection, request_id)
        if (
            row is None
            or row["state"] not in TERMINAL
            or row["receipt_json"] is None
            or row["allocation_json"] is None
            or row["drain_json"] is None
        ):
            raise ControlStoreError("request_conflict")
        try:
            receipt = json.loads(row["receipt_json"])
            allocation = json.loads(row["allocation_json"])
            proof = json.loads(row["drain_json"])
            principal = json.loads(row["principal_json"])
            lease = allocation["lease"]
            if (
                receipt["receipt_sha256"]
                != digest({key: value for key, value in receipt.items() if key != "receipt_sha256"})
                or receipt["terminal_state"] != row["state"]
                or receipt["release_outcome"] not in {"released", "recovered_released"}
                or receipt["original_principal"] != principal
                or allocation["original_principal"] != principal
                or row["principal_sha256"] != digest(principal)
                or any(
                    receipt[key] != row[key]
                    for key in (
                        "request_id",
                        "request_sha256",
                        "profile_id",
                        "deployment_digest",
                        "profile_config_sha256",
                        "response_schema_sha256",
                    )
                )
                or digest(allocation) != row["allocation_sha256"]
                or receipt["allocation_binding_sha256"] != row["allocation_sha256"]
                or proof["allocation_binding_sha256"] != row["allocation_sha256"]
                or receipt["drain_evidence_sha256"] != digest(proof)
                or receipt["child_generation"] != proof["child_generation"]
                or proof["child_generation"] is None
                or lease["owner"] != "aos"
                or lease["request_id"] != request_id
                or lease["owner_identity"]
                != {
                    "pid": principal["pid"],
                    "start_ticks": principal["start_ticks"],
                    "boot_id": principal["boot_id"],
                }
                or lease["owner_unit"] != principal["unit"]
                or lease["owner_invocation_id"] != principal["invocation_id"]
            ):
                raise ControlStoreError("request_conflict")
            binding = connection.execute(
                "SELECT * FROM aos_gpu_child_bindings WHERE owner='aos' AND request_id=? "
                "AND fencing_token=?",
                (request_id, lease["fencing_token"]),
            ).fetchone()
            if binding is None or binding["launch_state"] != "drained":
                raise ControlStoreError("request_conflict")
            child = {
                "unit": binding["unit"],
                "invocation_id": binding["invocation_id"],
                "pid": binding["main_pid"],
                "start_ticks": binding["main_start_ticks"],
                "boot_id": binding["boot_id"],
                "control_group": binding["control_group"],
            }
            if (
                child != proof["child_generation"]
                or (binding["profile_id"], binding["deployment_digest"])
                != (row["profile_id"], row["deployment_digest"])
                or connection.execute(
                    "SELECT 1 FROM gpu_turn_state WHERE active_owner='aos' AND active_request_id=?",
                    (request_id,),
                ).fetchone()
                is not None
            ):
                raise ControlStoreError("request_conflict")
            return dict(lease), str(receipt["receipt_sha256"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ControlStoreError("internal_unavailable") from exc

    def pending_cleanup_leases(self, *, limit: int = 8) -> list[tuple[dict[str, Any], str]]:
        if type(limit) is not int or not 1 <= limit <= 8:
            raise ValueError("cleanup limit must be between one and eight")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='aos_gpu_child_bindings'"
                ).fetchone()
                is None
            ):
                return []
            rows = connection.execute(
                "SELECT c.request_id FROM aos_control_requests c "
                "WHERE c.state IN ('completed','canceled','expired','failed') "
                "AND c.receipt_json IS NOT NULL AND c.allocation_json IS NOT NULL "
                "AND c.cleanup_receipt_sha256 IS NULL "
                "AND EXISTS(SELECT 1 FROM aos_gpu_child_bindings b WHERE b.owner='aos' "
                "AND b.request_id=c.request_id AND b.launch_state='drained') "
                "ORDER BY c.cleanup_attempts,c.created_boottime,c.request_id LIMIT ?",
                (limit,),
            ).fetchall()
            verified = []
            for row in rows:
                connection.execute(
                    "UPDATE aos_control_requests SET cleanup_attempts=cleanup_attempts+1 "
                    "WHERE request_id=?",
                    (row["request_id"],),
                )
                try:
                    verified.append(self._cleanup_evidence(connection, row["request_id"]))
                except ControlStoreError:
                    continue
            connection.commit()
            return verified

    def complete_cleanup(self, request_id: str, receipt_sha256: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            _lease, expected = self._cleanup_evidence(connection, request_id)
            if expected != receipt_sha256:
                raise ControlStoreError("request_conflict")
            connection.execute(
                "UPDATE aos_control_requests SET cleanup_receipt_sha256=? WHERE request_id=? "
                "AND (cleanup_receipt_sha256 IS NULL OR cleanup_receipt_sha256=?)",
                (expected, request_id, expected),
            )
            connection.commit()

    def _projection(self, row: sqlite3.Row | None, target: Mapping[str, Any]) -> dict[str, Any]:
        receipt = (
            None if row is None or row["receipt_json"] is None else json.loads(row["receipt_json"])
        )
        state = "unknown" if row is None else row["state"]
        if state in TERMINAL and receipt is None:
            raise ControlStoreError("internal_unavailable")
        return {
            "target": dict(target),
            "state": state,
            "cancel_requested": False if row is None else bool(row["cancel_requested"]),
            "first_cancel_boottime": None if row is None else row["first_cancel_boottime"],
            "terminal_receipt": receipt,
            "result": self._verified_completed_result(row)
            if row is not None and state == "completed"
            else None,
        }

    def _original_control_evidence(self, row: sqlite3.Row) -> dict[str, Any]:
        if row["admission_binding_json"] is not None:
            binding = self._saved_binding(row)
            authorization = None
        else:
            binding = None
            raw = row["original_cleanup_authorization_json"]
            if raw is None:
                raise ControlStoreError("unauthorized")
            authorization = json.loads(raw)
            if (
                not isinstance(authorization, dict)
                or set(authorization) != {"target", "binding", "operations"}
                or authorization["operations"] != ["cancel"]
                or canonical(authorization) != raw
                or digest(authorization) != row["original_cleanup_authorization_sha256"]
                or authorization["target"]
                != {
                    "request_id": row["request_id"],
                    "request_sha256": row["request_sha256"],
                    "original_peer_generation_sha256": row["principal_sha256"],
                }
            ):
                raise ControlStoreError("request_conflict")
            original = validate_admission_binding(authorization["binding"])
            if (
                original["caller_generation"] != json.loads(row["principal_json"])
                or original["profile_id"] != row["profile_id"]
                or original["profile_pin"]["deployment_digest"] != row["deployment_digest"]
            ):
                raise ControlStoreError("request_conflict")
        return {
            "original_admission_binding": binding,
            "original_admission_binding_sha256": row["admission_binding_sha256"],
            "original_cleanup_authorization": authorization,
            "original_cleanup_authorization_sha256": row["original_cleanup_authorization_sha256"],
        }

    def cleanup_original(
        self,
        peer: Mapping[str, Any],
        target: Mapping[str, Any],
        profile_id: str,
        deployment_digest: str,
    ) -> dict[str, Any]:
        """Read immutable evidence only; possession never authorizes cleanup."""
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            peer = self._peer(peer)
            self._target(peer, target)
            row = self._row(connection, target["request_id"])
            if row is None:
                raise ControlStoreError("unauthorized")
            self._exact(row, peer, target, profile_id, deployment_digest)
            result = self._original_control_evidence(row)
            connection.commit()
            return result

    def _authorize_control(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row | None,
        peer: Mapping[str, Any],
        target: Mapping[str, Any],
        profile_id: str,
        deployment_digest: str,
        operation: str,
        authority: ControlAuthority | None,
        *,
        persist: bool = False,
    ) -> None:
        # None is reserved for existing trusted internal fixture/recovery callers.
        # Every socket control path supplies a typed authority.
        if authority is None:
            return
        if not isinstance(authority, ControlAuthority) or operation not in CLEANUP_OPS:
            raise ControlStoreError("unauthorized")
        authority.verify_current()
        if authority.cleanup_grant_json is None:
            if authority.binding_json is None:
                raise ControlStoreError("unauthorized")
            binding = validate_admission_binding(json.loads(authority.binding_json))
            if (
                canonical(binding) != authority.binding_json
                or binding["caller_generation"] != peer
                or binding["profile_id"] != profile_id
                or binding["profile_pin"]["deployment_digest"] != deployment_digest
            ):
                raise ControlStoreError("unauthorized")
            if row is not None:
                original = self._original_control_evidence(row)
                expected = original["original_admission_binding"]
                if expected is None:
                    expected = original["original_cleanup_authorization"]["binding"]
                if binding != expected:
                    raise ControlStoreError("unauthorized")
            return
        if authority.binding_json is not None or row is None:
            raise ControlStoreError("unauthorized")
        grant = json.loads(authority.cleanup_grant_json)
        required = {
            "schema",
            "version",
            "target",
            "profile_id",
            "deployment_digest",
            "caller_generation",
            "resolver_generation",
            "policy_sha256",
            "source_fingerprints",
            "control_schema_sha256",
            "operations",
            "original_admission_binding",
            "original_admission_binding_sha256",
            "original_cleanup_authorization",
            "original_cleanup_authorization_sha256",
        }
        if (
            not isinstance(grant, dict)
            or set(grant) != required
            or canonical(grant) != authority.cleanup_grant_json
            or grant["schema"] != "aos-scientist-cleanup-grant.v1"
            or type(grant["version"]) is not int
            or grant["version"] != 1
            or grant["target"] != target
            or grant["caller_generation"] != peer
            or grant["profile_id"] != profile_id
            or grant["deployment_digest"] != deployment_digest
            or not isinstance(grant["operations"], list)
            or not grant["operations"]
            or any(not isinstance(op, str) or op not in CLEANUP_OPS for op in grant["operations"])
            or grant["operations"] != sorted(set(grant["operations"]))
            or operation not in grant["operations"]
        ):
            raise ControlStoreError("unauthorized")
        original = self._original_control_evidence(row)
        if any(grant[key] != value for key, value in original.items()):
            raise ControlStoreError("request_conflict")
        base = original["original_admission_binding"]
        if base is None:
            base = original["original_cleanup_authorization"]["binding"]
        validate_admission_binding(
            {
                **base,
                "caller_generation": grant["caller_generation"],
                "server_generation": grant["resolver_generation"],
                "policy_sha256": grant["policy_sha256"],
                "source_fingerprints": grant["source_fingerprints"],
                "control_schema": {
                    "name": "aos-scientist-control.v1",
                    "version": 1,
                    "sha256": grant["control_schema_sha256"],
                },
            }
        )
        fingerprint = digest(grant)
        existing = connection.execute(
            "SELECT grant_json FROM aos_cleanup_grants WHERE grant_sha256=?", (fingerprint,)
        ).fetchone()
        if existing is not None:
            if existing[0] != authority.cleanup_grant_json:
                raise ControlStoreError("request_conflict")
        elif not persist:
            raise ControlStoreError("unauthorized")
        else:
            if (
                connection.execute(
                    "SELECT COUNT(*) FROM aos_cleanup_grants WHERE request_id=?",
                    (target["request_id"],),
                ).fetchone()[0]
                >= MAX_TARGET_CLEANUP_GRANTS
            ):
                raise ControlStoreError("busy")
            self._consume_control_capacity(connection, target["request_id"], None, grant=True)
            connection.execute(
                "INSERT INTO aos_cleanup_grants VALUES(?,?,?)",
                (fingerprint, target["request_id"], authority.cleanup_grant_json),
            )

    def remember_target_capability(
        self,
        peer: Mapping[str, Any],
        target: Mapping[str, Any],
        profile_id: str,
        deployment_digest: str,
        authority: ControlAuthority,
        control_request: Mapping[str, Any],
        response: dict[str, Any],
        response_validator: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            peer = self._peer(peer)
            self._target(peer, target)
            row = self._row(connection, target["request_id"])
            if row is None:
                raise ControlStoreError("unauthorized")
            self._exact(row, peer, target, profile_id, deployment_digest)
            authority.verify_current()
            result = self._remember_frame(
                connection,
                peer,
                target,
                profile_id,
                deployment_digest,
                "capability",
                control_request,
                response,
            )
            operations = (
                ["status"]
                if authority.cleanup_grant_json is None
                else json.loads(authority.cleanup_grant_json)["operations"]
            )
            self._authorize_control(
                connection,
                row,
                peer,
                target,
                profile_id,
                deployment_digest,
                operations[0],
                authority,
                persist=True,
            )
            if result is None:
                raise ControlStoreError("internal_unavailable")
            if response_validator is not None:
                response_validator(result)
            authority.verify_current()
            if result is response and response["capability"]["expires_boottime"] <= self._now():
                raise ControlStoreError("capability_expired")
            connection.commit()
            if result is None:
                raise ControlStoreError("internal_unavailable")
            return result

    def _original_budget_witness(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        peer: Mapping[str, Any],
        target: Mapping[str, Any],
        profile_id: str,
        deployment_digest: str,
    ) -> dict[str, Any]:
        """Build only from retained assignment columns in the caller's snapshot."""
        if row["state"] not in TERMINAL:
            # Before readiness, the inference deadline can still be assigned.
            raise ControlStoreError("request_conflict")
        original = self._original_control_evidence(row)
        self._require_reservation(connection, row["request_id"])
        if row["allocation_json"] is not None:
            allocation = json.loads(row["allocation_json"])
            if (
                digest(allocation) != row["allocation_sha256"]
                or canonical(allocation) != row["allocation_json"]
                or allocation["request_sha256"] != row["request_sha256"]
                or allocation["original_principal"] != dict(peer)
                or allocation["admission_binding_sha256"] != row["admission_binding_sha256"]
                or allocation["original_deadline"] != row["original_deadline"]
                or allocation["original_budget"] != json.loads(row["budget_json"])
            ):
                raise ControlStoreError("request_conflict")
        budget = self._original_budget(row)
        witness = {
            "schema": "aos-scientist-original-budget-witness.v1",
            "version": 1,
            "target": dict(target),
            "profile_id": profile_id,
            "deployment_digest": deployment_digest,
            "profile_config_sha256": row["profile_config_sha256"],
            "response_schema_sha256": row["response_schema_sha256"],
            "original_admission_binding_sha256": original["original_admission_binding_sha256"],
            "original_cleanup_authorization_sha256": original[
                "original_cleanup_authorization_sha256"
            ],
            "allocation_binding_sha256": row["allocation_sha256"],
            "budget_canonical": canonical(budget),
            "budget_sha256": digest(budget),
        }
        return witness

    def read_original_budget(
        self,
        peer: Mapping[str, Any],
        target: Mapping[str, Any],
        profile_id: str,
        deployment_digest: str,
        *,
        authority: ControlAuthority,
    ) -> dict[str, Any]:
        """Trusted read-only adapter for an independently retained budget witness.

        No receipt field supplies this budget. It is reconstructed from original
        intent/allocation/readiness rows, after assignment has become terminal.
        The caller must retain the witness through its trusted admission history;
        this API is not a socket operation or authority for journal resolution.
        """
        if not isinstance(authority, ControlAuthority):
            raise ControlStoreError("unauthorized")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            peer = self._peer(peer)
            self._target(peer, target)
            row = self._row(connection, target["request_id"])
            if row is None:
                raise ControlStoreError("unauthorized")
            self._exact(row, peer, target, profile_id, deployment_digest)
            self._authorize_control(
                connection, row, peer, target, profile_id, deployment_digest, "reconcile", authority
            )
            witness = self._original_budget_witness(
                connection, row, peer, target, profile_id, deployment_digest
            )
            authority.verify_current()
            connection.commit()
            return witness

    def _terminal_evidence_snapshot(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        peer: Mapping[str, Any],
        target: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Verify immutable terminal preimages in the caller's transaction."""
        if row["state"] not in TERMINAL or row["receipt_json"] is None:
            raise ControlStoreError("request_conflict")
        try:
            receipt = json.loads(row["receipt_json"])
            if not isinstance(receipt, dict):
                raise ControlStoreError("request_conflict")
            if (
                canonical(receipt) != row["receipt_json"]
                or receipt["receipt_sha256"]
                != digest({key: value for key, value in receipt.items() if key != "receipt_sha256"})
                or receipt["terminal_state"] != row["state"]
                or receipt["original_principal"] != dict(peer)
                or receipt["admission_binding"]
                != (None if row["admission_binding_json"] is None else self._saved_binding(row))
                or receipt["admission_binding_sha256"] != row["admission_binding_sha256"]
                or receipt["original_budget"] != self._original_budget(row)
                or receipt["child_generation"]
                != (None if row["child_json"] is None else json.loads(row["child_json"]))
                or any(
                    receipt[key] != row[key]
                    for key in (
                        "request_id",
                        "request_sha256",
                        "profile_id",
                        "deployment_digest",
                        "profile_config_sha256",
                        "response_schema_sha256",
                    )
                )
            ):
                raise ControlStoreError("request_conflict")
            for column, hash_field in (
                ("allocation_json", "allocation_binding_sha256"),
                ("drain_json", "drain_evidence_sha256"),
                ("no_admission_json", "no_admission_evidence_sha256"),
            ):
                raw = row[column]
                if raw is None:
                    if receipt[hash_field] is not None:
                        raise ControlStoreError("request_conflict")
                elif (
                    canonical(json.loads(raw)) != raw
                    or digest(json.loads(raw)) != receipt[hash_field]
                ):
                    raise ControlStoreError("request_conflict")
            if receipt["release_outcome"] in {"released", "recovered_released"}:
                self._cleanup_evidence(connection, target["request_id"])
                allocation = json.loads(row["allocation_json"])
                if (
                    row["no_admission_json"] is not None
                    or allocation["admission_binding_sha256"] != row["admission_binding_sha256"]
                    or allocation["request_sha256"] != row["request_sha256"]
                    or allocation["original_deadline"] != row["original_deadline"]
                    or allocation["original_budget"] != json.loads(row["budget_json"])
                ):
                    raise ControlStoreError("request_conflict")
            elif receipt["release_outcome"] == "never_admitted":
                proof = json.loads(row["no_admission_json"])
                if (
                    row["allocation_json"] is not None
                    or row["drain_json"] is not None
                    or row["child_json"] is not None
                    or row["handoff_stage"] is not None
                    or proof
                    != {
                        "request_id": row["request_id"],
                        "request_sha256": row["request_sha256"],
                        "principal_sha256": row["principal_sha256"],
                        "cancel_before_intent": row["original_deadline"] is None,
                        "never_allocated": True,
                        "reason": receipt["reason_code"],
                    }
                    or connection.execute(
                        "SELECT 1 FROM gpu_turn_state WHERE active_owner='aos' "
                        "AND active_request_id=?",
                        (row["request_id"],),
                    ).fetchone()
                    is not None
                ):
                    raise ControlStoreError("request_conflict")
                if (
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='aos_gpu_child_bindings'"
                    ).fetchone()
                    is not None
                    and connection.execute(
                        "SELECT 1 FROM aos_gpu_child_bindings WHERE owner='aos' AND request_id=?",
                        (row["request_id"],),
                    ).fetchone()
                    is not None
                ):
                    raise ControlStoreError("request_conflict")
                ticket = connection.execute(
                    "SELECT state FROM gpu_turn_requests WHERE owner='aos' AND request_id=?",
                    (row["request_id"],),
                ).fetchone()
                if ticket is not None and ticket["state"] not in {"canceled", "expired"}:
                    raise ControlStoreError("request_conflict")
            else:
                raise ControlStoreError("request_conflict")
            if row["state"] == "completed":
                verified = self._verified_completed_result(row)
                if canonical(verified) != row["result_json"]:
                    raise ControlStoreError("request_conflict")
                result = row["result_json"]
            else:
                if receipt["result_sha256"] is not None:
                    raise ControlStoreError("request_conflict")
                result = None
        except (ValueError, TypeError, KeyError) as exc:
            raise ControlStoreError("request_conflict") from exc
        evidence = {
            "schema": "aos-scientist-terminal-evidence.v2",
            "version": 2,
            "target": dict(target),
            "terminal_canonical": row["receipt_json"],
            "allocation_canonical": row["allocation_json"],
            "drain_canonical": row["drain_json"],
            "no_admission_canonical": row["no_admission_json"],
            "result_canonical": result,
        }
        if len(canonical(evidence).encode("utf-8")) > 128 * 1024 - 1:
            raise ControlStoreError("internal_unavailable")
        return evidence

    def read_original_physical_snapshot(
        self,
        peer: Mapping[str, Any],
        target: Mapping[str, Any],
        profile_id: str,
        deployment_digest: str,
        *,
        authority: ControlAuthority,
    ) -> dict[str, Any]:
        """Read original terminal bindings for a separate physical observation.

        This is trusted service composition, not a socket operation. Retained
        drain claims are verified against their original rows but do not prove
        current GPU absence. No scheduler, authority, or reservation is created.
        """
        if not isinstance(authority, ControlAuthority):
            raise ControlStoreError("unauthorized")
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            peer = self._peer(peer)
            self._target(peer, target)
            row = self._row(connection, target["request_id"])
            if row is None:
                raise ControlStoreError("unauthorized")
            self._exact(row, peer, target, profile_id, deployment_digest)
            self._authorize_control(
                connection, row, peer, target, profile_id, deployment_digest, "reconcile", authority
            )
            evidence = self._terminal_evidence_snapshot(connection, row, peer, target)
            ticket = connection.execute(
                "SELECT * FROM gpu_turn_requests WHERE owner='aos' AND request_id=?",
                (row["request_id"],),
            ).fetchone()
            states = connection.execute("SELECT * FROM gpu_turn_state").fetchall()
            children = connection.execute(
                "SELECT * FROM aos_gpu_child_bindings WHERE owner='aos' AND request_id=?",
                (row["request_id"],),
            ).fetchall()
            try:
                if len(states) != 1 or states[0]["singleton"] != 1:
                    raise ControlStoreError("request_conflict")
                state = states[0]
                if (
                    type(state["active_token"]) is not int
                    or state["active_token"] < 0
                    or (state["active_owner"] is None) != (state["active_request_id"] is None)
                    or state["active_owner"] is None
                    and state["phase"] is not None
                    or state["active_owner"] is not None
                    and (
                        state["active_owner"] not in {"aos", "lab"}
                        or state["phase"] not in {"activating", "inference", "quarantined"}
                    )
                    or (state["active_owner"], state["active_request_id"])
                    == ("aos", row["request_id"])
                ):
                    raise ControlStoreError("request_conflict")
                budget = self._original_budget(row)
                if ticket is not None:
                    if (
                        budget is None
                        or ticket["payload_sha256"] != row["request_sha256"]
                        or any(
                            ticket[column] != peer[field]
                            for column, field in (
                                ("owner_pid", "pid"),
                                ("owner_start_ticks", "start_ticks"),
                                ("owner_boot_id", "boot_id"),
                                ("owner_unit", "unit"),
                                ("owner_invocation_id", "invocation_id"),
                            )
                        )
                        or any(
                            ticket[key] != budget[key]
                            for key in (
                                "activation_seconds",
                                "inference_seconds",
                                "total_seconds",
                                "queue_deadline",
                            )
                        )
                    ):
                        raise ControlStoreError("request_conflict")
                receipt = json.loads(row["receipt_json"])
                child_projection = None
                if receipt["release_outcome"] == "never_admitted":
                    if (
                        any(
                            row[key] is not None
                            for key in (
                                "allocation_json",
                                "allocation_sha256",
                                "drain_json",
                                "child_json",
                                "handoff_stage",
                                "ready_deadlines_json",
                            )
                        )
                        or children
                        or ticket is not None
                        and ticket["state"] not in {"canceled", "expired"}
                    ):
                        raise ControlStoreError("request_conflict")
                else:
                    lease, _receipt_hash = self._cleanup_evidence(connection, row["request_id"])
                    if (
                        receipt["release_outcome"] not in {"released", "recovered_released"}
                        or ticket is None
                        or ticket["state"] not in {"done", "expired"}
                        or type(lease["fencing_token"]) is not int
                        or lease["fencing_token"] < 1
                        or state["active_token"] < lease["fencing_token"]
                        or state["active_owner"] is not None
                        and state["active_token"] <= lease["fencing_token"]
                        or len(children) != 1
                        or children[0]["fencing_token"] != lease["fencing_token"]
                    ):
                        raise ControlStoreError("request_conflict")
                    child = children[0]
                    proof = json.loads(row["drain_json"])
                    intent = {
                        key: child[key]
                        for key in (
                            "unit",
                            "nonce",
                            "created_boottime",
                            "total_seconds",
                        )
                    }
                    observed = json.loads(child["observed_gpu_pids_json"])
                    if (
                        row["handoff_stage"] not in {"start", "go"}
                        or proof["handoff_stage"] != row["handoff_stage"]
                        or proof["kind"] != "physical_drain"
                        or proof["boot_id"] != child["boot_id"]
                        or child["boot_id"] != peer["boot_id"]
                        or proof["never_started"] is not False
                        or proof["child_intent"] != intent
                        or not _hex(child["nonce"], 64)
                        or any(
                            child[key] is None
                            for key in (
                                "invocation_id",
                                "main_pid",
                                "main_start_ticks",
                                "boot_id",
                                "control_group",
                            )
                        )
                        or type(child["main_pid"]) is not int
                        or not 2 <= child["main_pid"] <= 2**31 - 1
                        or type(child["main_start_ticks"]) is not int
                        or child["main_start_ticks"] <= 0
                        or not _hex(child["invocation_id"], 32)
                        or budget is None
                        or child["total_seconds"] != budget["total_seconds"]
                        or not math.isfinite(child["created_boottime"])
                        or child["created_boottime"] < 0
                        or proof["late_start_fence"]
                        != child["created_boottime"]
                        + child["total_seconds"]
                        + CONTROL_DRAIN_SECONDS
                        or any(
                            proof[key] is not True
                            for key in (
                                "late_start_fenced",
                                "cgroup_empty",
                                "gpu_absent",
                            )
                        )
                        or proof["remaining_owned_gpu_pids"] != []
                        or proof["remaining_foreign_gpu_pids"] != []
                        or not isinstance(observed, list)
                        or any(
                            type(pid) is not int or not 1 <= pid <= 2**31 - 1 for pid in observed
                        )
                        or observed != sorted(set(observed))
                        or canonical(observed) != child["observed_gpu_pids_json"]
                        or observed != proof["observed_gpu_pids"]
                    ):
                        raise ControlStoreError("request_conflict")
                    output = connection.execute(
                        "SELECT * FROM aos_gpu_output_bindings WHERE request_id=?",
                        (row["request_id"],),
                    ).fetchone()
                    if output is None:
                        raise ControlStoreError("request_conflict")
                    frame = json.loads(output["request_json"])
                    if (
                        hashlib.sha256(output["request_json"].encode("utf-8")).hexdigest()
                        != row["request_sha256"]
                        or output["principal_json"] != row["principal_json"]
                        or any(
                            output[key] != row[key]
                            for key in (
                                "request_id",
                                "request_sha256",
                                "profile_id",
                                "deployment_digest",
                                "profile_config_sha256",
                            )
                        )
                        or type(frame["version"]) is not int
                        or frame["version"] != 1
                        or canonical(frame) != output["request_json"]
                        or frame["op"] != "infer"
                        or any(
                            frame[key] != row[key]
                            for key in (
                                "request_id",
                                "profile_id",
                                "deployment_digest",
                            )
                        )
                        or not isinstance(frame["payload"], dict)
                        or digest(frame["payload"]) != child["request_sha256"]
                    ):
                        raise ControlStoreError("request_conflict")
                    child_projection = {
                        key: child[key]
                        for key in (
                            "owner",
                            "request_id",
                            "fencing_token",
                            "profile_id",
                            "deployment_digest",
                            "request_sha256",
                            "unit",
                            "nonce",
                            "launch_state",
                            "created_boottime",
                            "total_seconds",
                            "invocation_id",
                            "main_pid",
                            "main_start_ticks",
                            "boot_id",
                            "control_group",
                        )
                    }
                    child_projection["observed_gpu_pids"] = observed
                snapshot = {
                    "schema": "aos-scientist-physical-snapshot.v1",
                    "version": 1,
                    "evidence": evidence,
                    "original_budget": budget,
                    "ticket": None
                    if ticket is None
                    else {
                        key: ticket[key]
                        for key in (
                            "owner",
                            "request_id",
                            "payload_sha256",
                            "submitted_at",
                            "activation_seconds",
                            "inference_seconds",
                            "total_seconds",
                            "queue_deadline",
                            "owner_pid",
                            "owner_start_ticks",
                            "owner_boot_id",
                            "owner_unit",
                            "owner_invocation_id",
                            "sequence",
                            "state",
                        )
                    },
                    "child": child_projection,
                    "handoff_stage": row["handoff_stage"],
                    "arbiter": {
                        key: state[key]
                        for key in (
                            "active_owner",
                            "active_request_id",
                            "active_token",
                            "phase",
                        )
                    },
                }
            except (KeyError, IndexError, TypeError, ValueError, OverflowError) as exc:
                raise ControlStoreError("request_conflict") from exc
            authority.verify_current()
            connection.commit()
            return snapshot

    def read_terminal_evidence(
        self,
        peer: Mapping[str, Any],
        target: Mapping[str, Any],
        profile_id: str,
        deployment_digest: str,
        *,
        authority: ControlAuthority,
        control_request: Mapping[str, Any],
        evidence_validator: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Read original proof preimages; never release or adopt an allocation.

        The trusted control adapter must supply current retained-target authority.
        Exact stored canonical strings preserve historical float clocks and hashes.
        This method does not expose a new unauthenticated socket operation.
        """
        if not isinstance(authority, ControlAuthority):
            raise ControlStoreError("unauthorized")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            peer = self._peer(peer)
            self._target(peer, target)
            row = self._row(connection, target["request_id"])
            if row is None:
                raise ControlStoreError("unauthorized")
            self._exact(row, peer, target, profile_id, deployment_digest)
            self._authorize_control(
                connection,
                row,
                peer,
                target,
                profile_id,
                deployment_digest,
                "reconcile",
                authority,
            )
            evidence = self._terminal_evidence_snapshot(connection, row, peer, target)
            if evidence_validator is not None:
                evidence_validator(evidence)
            self._remember_frame(
                connection,
                peer,
                target,
                profile_id,
                deployment_digest,
                "reconcile",
                control_request,
            )
            authority.verify_current()
            connection.commit()
            return evidence

    def read_retained_terminal_evidence(
        self,
        peer: Mapping[str, Any],
        target: Mapping[str, Any],
        profile_id: str,
        deployment_digest: str,
        *,
        authority: ControlAuthority,
        control_request: Mapping[str, Any],
        evidence_validator: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Atomically export original budget and evidence, without new authority.

        The callback validates the complete combined payload before consuming any
        control capacity. Existing wire containers and immutable clocks are kept.
        This trusted adapter does not add a socket operation or release resources.
        """
        if not isinstance(authority, ControlAuthority):
            raise ControlStoreError("unauthorized")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            peer = self._peer(peer)
            self._target(peer, target)
            row = self._row(connection, target["request_id"])
            if row is None:
                raise ControlStoreError("unauthorized")
            self._exact(row, peer, target, profile_id, deployment_digest)
            self._authorize_control(
                connection, row, peer, target, profile_id, deployment_digest, "reconcile", authority
            )
            witness = self._original_budget_witness(
                connection, row, peer, target, profile_id, deployment_digest
            )
            evidence = self._terminal_evidence_snapshot(connection, row, peer, target)
            if witness["budget_canonical"] == "null" or witness["budget_canonical"] != canonical(
                json.loads(evidence["terminal_canonical"])["original_budget"]
            ):
                raise ControlStoreError("request_conflict")
            combined = {"evidence": evidence, "original_budget_witness": witness}
            if len(canonical(combined).encode("utf-8")) > 128 * 1024 - 1:
                raise ControlStoreError("internal_unavailable")
            if evidence_validator is not None:
                evidence_validator(combined)
            self._remember_frame(
                connection,
                peer,
                target,
                profile_id,
                deployment_digest,
                "reconcile",
                control_request,
            )
            authority.verify_current()
            connection.commit()
            return combined

    def lookup(
        self,
        peer: Mapping[str, Any],
        target: Mapping[str, Any],
        profile_id: str,
        deployment_digest: str,
        history: bool = False,
        *,
        authority: ControlAuthority | None = None,
        operation: str = "status",
        control_request: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE" if control_request is not None else "BEGIN")
            peer = self._peer(peer)
            if history:
                if (
                    set(target) != TARGET_FIELDS
                    or not _hex(target.get("request_id"), 32)
                    or any(
                        not _hex(target.get(key), 64)
                        for key in ("request_sha256", "original_peer_generation_sha256")
                    )
                ):
                    raise ControlStoreError("invalid_frame")
                row = self._row(connection, target["request_id"])
                if row is None:
                    raise ControlStoreError("history_denied")
                original = json.loads(row["principal_json"])
                if (
                    peer["uid"],
                    peer["unit"],
                    profile_id,
                    deployment_digest,
                    target["request_sha256"],
                    target["original_peer_generation_sha256"],
                ) != (
                    original["uid"],
                    original["unit"],
                    row["profile_id"],
                    row["deployment_digest"],
                    row["request_sha256"],
                    row["principal_sha256"],
                ):
                    raise ControlStoreError("history_denied")
                receipt = None if row["receipt_json"] is None else json.loads(row["receipt_json"])
                result = {
                    "target": dict(target),
                    "state": row["state"],
                    "original_peer_generation_sha256": row["principal_sha256"],
                    "terminal_receipt_sha256": None
                    if receipt is None
                    else receipt["receipt_sha256"],
                    "release_outcome": None if receipt is None else receipt["release_outcome"],
                }
                self._peer(peer)
                connection.commit()
                return result
            self._target(peer, target)
            row = self._row(connection, target["request_id"])
            if row is not None:
                self._exact(row, peer, target, profile_id, deployment_digest)
            self._authorize_control(
                connection, row, peer, target, profile_id, deployment_digest, operation, authority
            )
            result = self._projection(row, target)
            self._remember_frame(
                connection, peer, target, profile_id, deployment_digest, operation, control_request
            )
            self._peer(peer)
            if authority is not None:
                authority.verify_current()
            connection.commit()
            return result

    def cancel(
        self,
        peer: Mapping[str, Any],
        target: Mapping[str, Any],
        profile_id: str,
        deployment_digest: str,
        history: bool = False,
        *,
        profile_config_sha256: str | None = None,
        response_schema_sha256: str | None = None,
        budget: Mapping[str, Any] | None = None,
        authority: ControlAuthority | None = None,
        control_request: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if history:
            raise ControlStoreError("history_denied")
        if any(
            value is not None and not _hex(value, 64)
            for value in (profile_config_sha256, response_schema_sha256)
        ):
            raise ControlStoreError("invalid_frame")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            peer = self._peer(peer)
            self._target(peer, target)
            row = self._row(connection, target["request_id"])
            self._authorize_control(
                connection, row, peer, target, profile_id, deployment_digest, "cancel", authority
            )
            if row is not None:
                self._exact(row, peer, target, profile_id, deployment_digest)
            else:
                self._request_capacity(connection)
                self._reserve_controls(connection, target["request_id"], budget)
                # Never manufacture no-admission evidence for an unbound legacy allocation.
                if connection.execute(
                    "SELECT 1 FROM gpu_turn_requests WHERE owner='aos' AND request_id=?",
                    (target["request_id"],),
                ).fetchone():
                    raise ControlStoreError("request_conflict")
                connection.execute(
                    "INSERT INTO aos_control_requests(request_id,request_sha256,principal_json,"
                    "principal_sha256,"
                    "profile_id,deployment_digest,original_boot_id,created_boottime,"
                    "profile_config_sha256,"
                    "response_schema_sha256,budget_json,state) VALUES(?,?,?,?,?,?,?,?,?,?,?,"
                    "'intent')",
                    (
                        target["request_id"],
                        target["request_sha256"],
                        canonical(peer),
                        digest(peer),
                        profile_id,
                        deployment_digest,
                        self._boot_id(),
                        self._now(),
                        profile_config_sha256,
                        response_schema_sha256,
                        None if budget is None else canonical(dict(budget)),
                    ),
                )
                row = self._row(connection, target["request_id"])
                if authority is not None:
                    if authority.binding_json is None:
                        raise ControlStoreError("unauthorized")
                    original = {
                        "target": dict(target),
                        "binding": json.loads(authority.binding_json),
                        "operations": ["cancel"],
                    }
                    connection.execute(
                        "UPDATE aos_control_requests SET original_cleanup_authorization_json=?,"
                        "original_cleanup_authorization_sha256=? WHERE request_id=?",
                        (canonical(original), digest(original), target["request_id"]),
                    )
                    row = self._row(connection, target["request_id"])
            if row is None:
                raise ControlStoreError("internal_unavailable")
            if row["state"] not in TERMINAL:
                connection.execute(
                    "UPDATE aos_control_requests SET cancel_requested=1,"
                    "first_cancel_boottime=COALESCE(first_cancel_boottime,?),"
                    "state='cancel_pending' WHERE request_id=?",
                    (self._now(), row["request_id"]),
                )
                if row["allocation_json"] is None:
                    self.no_admission(connection, row["request_id"], "canceled", "caller_cancel")
            result = self._projection(self._row(connection, target["request_id"]), target)
            self._remember_frame(
                connection, peer, target, profile_id, deployment_digest, "cancel", control_request
            )
            self._peer(peer)
            if authority is not None:
                authority.verify_current()
            connection.commit()
            return result

    def no_admission(
        self, connection: sqlite3.Connection, request_id: str, state: str, reason: str
    ) -> None:
        """Scheduler write-transaction hook; never admissible after allocation."""
        row = self._row(connection, request_id)
        if row is None or row["receipt_json"] is not None:
            return
        if (
            row["allocation_json"] is not None
            or row["handoff_stage"] is not None
            or row["child_json"] is not None
            or row["drain_json"] is not None
        ) or connection.execute(
            "SELECT 1 FROM gpu_turn_state WHERE active_owner='aos' AND active_request_id=?",
            (request_id,),
        ).fetchone():
            raise ControlStoreError("internal_unavailable")
        if (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='aos_gpu_child_bindings'"
            ).fetchone()
            is not None
            and connection.execute(
                "SELECT 1 FROM aos_gpu_child_bindings WHERE owner='aos' AND request_id=?",
                (request_id,),
            ).fetchone()
            is not None
        ):
            raise ControlStoreError("internal_unavailable")
        ticket = connection.execute(
            "SELECT state FROM gpu_turn_requests WHERE owner='aos' AND request_id=?", (request_id,)
        ).fetchone()
        if ticket is not None and ticket["state"] not in {"queued", "canceled", "expired"}:
            raise ControlStoreError("internal_unavailable")
        connection.execute(
            "UPDATE gpu_turn_requests SET state=? WHERE owner='aos' AND request_id=? AND "
            "state='queued'",
            ("canceled" if state == "canceled" else "expired", request_id),
        )
        evidence = {
            "request_id": request_id,
            "request_sha256": row["request_sha256"],
            "principal_sha256": row["principal_sha256"],
            "cancel_before_intent": row["original_deadline"] is None,
            "never_allocated": True,
            "reason": reason,
        }
        connection.execute(
            "UPDATE aos_control_requests SET no_admission_json=? WHERE request_id=?",
            (canonical(evidence), request_id),
        )
        self._terminal(connection, row, state, reason, "never_admitted", None, digest(evidence))

    def recover_unallocated(self, connection: sqlite3.Connection, *, limit: int = 8) -> int:
        """Bounded trusted orphan scan in the scheduler's write transaction.

        No new peer is supplied or adopted. The original record's expiry or
        trusted original-peer liveness check decides eligibility; no_admission
        then proves there is no allocation, child or launch in this transaction.
        """
        if not connection.in_transaction or type(limit) is not int or not 1 <= limit <= 8:
            raise ControlStoreError("internal_unavailable")
        rows = connection.execute(
            "SELECT * FROM aos_control_requests WHERE receipt_json IS NULL "
            "AND allocation_json IS NULL AND state IN ('intent','queued','cancel_pending') "
            "ORDER BY created_boottime,request_id LIMIT ?",
            (limit,),
        ).fetchall()
        recovered = 0
        for row in rows:
            reason = None
            state = "expired"
            if row["cancel_requested"]:
                reason, state = "caller_cancel", "canceled"
            elif row["original_boot_id"] != self._boot_id():
                reason = "generation_lost"
            elif (
                row["original_deadline"] is not None
                and self._now() >= row["original_deadline"]
                or row["queue_deadline"] is not None
                and self._now() >= row["queue_deadline"]
            ):
                reason = "queue_timeout" if row["queue_deadline"] is not None else "turn_timeout"
            elif self._peer_verifier is not None:
                try:
                    self._peer(json.loads(row["principal_json"]))
                except ControlStoreError as exc:
                    if exc.code != "stale_generation":
                        raise
                    reason = "generation_lost"
            if reason is not None:
                self.no_admission(connection, row["request_id"], state, reason)
                recovered += 1
        return recovered

    def before_submit(
        self,
        connection: sqlite3.Connection,
        request_id: str,
        request_sha256: str,
        queue_deadline: float,
        principal: Mapping[str, Any],
    ) -> float:
        row = self._row(connection, request_id)
        if row is None or row["request_sha256"] != request_sha256:
            raise ControlStoreError("request_conflict")
        if row["cancel_requested"] or row["state"] in TERMINAL:
            raise ControlCanceled()
        self._saved_binding(row, require_output_contract=True)
        self._require_reservation(connection, request_id)
        peer = self._peer(json.loads(row["principal_json"]))
        if (principal["identity"], principal["unit"], principal["invocation_id"]) != (
            {"pid": peer["pid"], "start_ticks": peer["start_ticks"], "boot_id": peer["boot_id"]},
            peer["unit"],
            peer["invocation_id"],
        ):
            raise ControlStoreError("stale_generation")
        if row["original_boot_id"] != self._boot_id() or self._now() >= row["original_deadline"]:
            raise ControlStoreError("deadline_exceeded")
        queue_deadline = (
            min(queue_deadline, float(row["original_deadline"]))
            if row["queue_deadline"] is None
            else float(row["queue_deadline"])
        )
        connection.execute(
            "UPDATE aos_control_requests SET state='queued',queue_deadline=? WHERE request_id=? "
            "AND state='intent'",
            (queue_deadline, request_id),
        )
        return queue_deadline

    def before_acquire(self, connection: sqlite3.Connection, request_id: str) -> float:
        row = self._row(connection, request_id)
        if row is None or row["cancel_requested"] or row["state"] != "queued":
            raise ControlCanceled()
        self._saved_binding(row, require_output_contract=True)
        self._require_reservation(connection, request_id)
        self._peer(json.loads(row["principal_json"]))
        if row["original_boot_id"] != self._boot_id() or self._now() >= row["original_deadline"]:
            raise ControlStoreError("deadline_exceeded")
        return float(row["original_deadline"])

    def bind_allocation(self, connection: sqlite3.Connection, lease: Mapping[str, Any]) -> None:
        row = self._row(connection, lease["request_id"])
        if row is None or row["allocation_json"] is not None or row["cancel_requested"]:
            raise ControlCanceled()
        self._saved_binding(row, require_output_contract=True)
        self._require_reservation(connection, lease["request_id"])
        binding = {
            "lease": dict(lease),
            "admission_binding_sha256": row["admission_binding_sha256"],
            "original_principal": json.loads(row["principal_json"]),
            "request_sha256": row["request_sha256"],
            "original_deadline": row["original_deadline"],
            "original_budget": json.loads(row["budget_json"]),
        }
        connection.execute(
            "UPDATE aos_control_requests SET allocation_json=?,allocation_sha256=?,"
            "state='activating' WHERE request_id=?",
            (canonical(binding), digest(binding), row["request_id"]),
        )

    def _bound(self, connection: sqlite3.Connection, lease: Mapping[str, Any]) -> sqlite3.Row:
        row = self._row(connection, lease["request_id"])
        if row is None or row["allocation_json"] is None:
            raise ControlStoreError("request_conflict")
        original = json.loads(row["allocation_json"])["lease"]
        if any(
            original.get(key) != lease.get(key)
            for key in (
                "owner",
                "request_id",
                "fencing_token",
                "owner_identity",
                "owner_unit",
                "owner_invocation_id",
            )
        ):
            raise ControlStoreError("request_conflict")
        live = connection.execute(
            "SELECT active_owner,active_request_id,active_token FROM gpu_turn_state WHERE "
            "singleton=1"
        ).fetchone()
        if live is None or tuple(live) != (
            lease["owner"],
            lease["request_id"],
            lease["fencing_token"],
        ):
            raise ControlStoreError("request_conflict")
        return row

    def check_running(self, lease: Mapping[str, Any]) -> None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            self._admission(connection, lease)
            connection.commit()

    def requires_bound_child(self, lease: Mapping[str, Any]) -> bool:
        """A committed start/go cannot be disproven by elapsed time alone."""
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            row = self._bound(connection, lease)
            required = row["handoff_stage"] in {"start", "go"}
            connection.commit()
            return required

    def _admission(self, connection: sqlite3.Connection, lease: Mapping[str, Any]) -> sqlite3.Row:
        row = self._bound(connection, lease)
        self._saved_binding(row, require_output_contract=True)
        self._require_reservation(connection, lease["request_id"])
        if row["cancel_requested"]:
            raise ControlCanceled()
        live = connection.execute("SELECT * FROM gpu_turn_state WHERE singleton=1").fetchone()
        phase_deadline = (
            live["activation_deadline"]
            if live["phase"] == "activating"
            else live["inference_deadline"]
        )
        if (
            live["phase"] == "quarantined"
            or row["drain_json"] is not None
            or row["receipt_json"] is not None
        ):
            raise ControlStoreError("request_conflict")
        if row["original_boot_id"] != self._boot_id() or self._now() >= min(
            row["original_deadline"],
            phase_deadline,
            live["total_deadline"],
            live["heartbeat_deadline"],
        ):
            raise ControlStoreError("deadline_exceeded")
        self._peer(json.loads(row["principal_json"]))
        return row

    def mark_ready(self, connection: sqlite3.Connection, lease: Mapping[str, Any]) -> None:
        row = self._admission(connection, lease)
        deadlines = {
            key: lease[key]
            for key in ("activation_deadline", "inference_deadline", "total_deadline")
        }
        encoded = canonical(deadlines)
        if row["ready_deadlines_json"] is not None and row["ready_deadlines_json"] != encoded:
            raise ControlStoreError("request_conflict")
        connection.execute(
            "UPDATE aos_control_requests SET ready_deadlines_json=? WHERE request_id=?",
            (encoded, row["request_id"]),
        )

    def should_recover(self, connection: sqlite3.Connection, lease: Mapping[str, Any]) -> bool:
        row = self._row(connection, lease["request_id"])
        if row is None:
            return False  # Unbound legacy work is never adopted by control.
        row = self._bound(connection, lease)
        recover = (
            bool(row["cancel_requested"])
            or row["original_boot_id"] != self._boot_id()
            or self._now() >= row["original_deadline"]
        )
        try:
            self._peer(json.loads(row["principal_json"]))
        except ControlStoreError as exc:
            if exc.code != "stale_generation":
                raise
            recover = True
        return recover

    def mark_quarantined(self, connection: sqlite3.Connection, lease: Mapping[str, Any]) -> None:
        row = self._bound(connection, lease)
        if row["receipt_json"] is None:
            connection.execute(
                "UPDATE aos_control_requests SET state='quarantined' WHERE request_id=?",
                (row["request_id"],),
            )

    def launch_handoff(
        self, connection: sqlite3.Connection, lease: Mapping[str, Any], stage: str = "plan"
    ) -> None:
        """Commit start intent before blocking launch; go write stays in transaction.

        A committed start is *potentially live* even before child discovery. Its
        lease may only finish with trusted late-start fence and drain evidence.
        A repeated start/go is rejected, so crash recovery cannot launch twice.
        """
        if not connection.in_transaction or stage not in {"plan", "start", "go"}:
            raise ControlStoreError("internal_unavailable")
        row = self._admission(connection, lease)
        expected = {"plan": None, "start": "plan", "go": "start"}[stage]
        if row["handoff_stage"] != expected:
            raise ControlStoreError("request_conflict")
        connection.execute(
            "UPDATE aos_control_requests SET handoff_stage=?,state=? WHERE request_id=?",
            (stage, "running" if stage == "go" else "activating", row["request_id"]),
        )

    def record_result(
        self,
        request_id: str,
        resultdict: Mapping[str, Any],
        usage: Mapping[str, Any],
        childdict: Mapping[str, Any],
    ) -> None:
        result = {"response": dict(resultdict), "usage": dict(usage), "generation": dict(childdict)}
        encoded = canonical(result)
        if len(encoded.encode("utf-8")) > 96 * 1024:
            raise ControlStoreError("internal_unavailable")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, request_id)
            if (
                row is None
                or row["allocation_json"] is None
                or row["receipt_json"] is not None
                or row["drain_json"] is not None
            ):
                raise ControlStoreError("request_conflict")
            self._admission(connection, json.loads(row["allocation_json"])["lease"])
            connection.execute(
                "UPDATE aos_control_requests SET result_json=?,state='result_ready' WHERE "
                "request_id=?",
                (encoded, request_id),
            )
            connection.commit()

    def record_drain(self, lease: Mapping[str, Any], evidence: Mapping[str, Any]) -> None:
        """Called only after actual trusted physical verification, never by control."""
        proof = dict(evidence)
        required = {"child_generation", "late_start_fenced", "cgroup_empty", "gpu_absent"}
        if not required <= set(proof) or any(
            proof[key] is not True for key in required - {"child_generation"}
        ):
            raise ControlStoreError("internal_unavailable")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._bound(connection, lease)
            child = proof["child_generation"]
            if child is None and row["handoff_stage"] in {"start", "go"}:
                # The submitting handler may be paused between its committed
                # handoff and subprocess.run. A timeout cannot prove it absent.
                raise ControlStoreError("internal_unavailable")
            has_bindings = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='aos_gpu_child_bindings'"
            ).fetchone()
            binding = (
                None
                if has_bindings is None
                else connection.execute(
                    "SELECT * FROM aos_gpu_child_bindings WHERE owner=? AND request_id=? AND "
                    "fencing_token=?",
                    (lease["owner"], lease["request_id"], lease["fencing_token"]),
                ).fetchone()
            )
            if binding is None:
                if (
                    row["handoff_stage"] is not None
                    or child is not None
                    or proof.get("no_child_intent") is not True
                ):
                    raise ControlStoreError("internal_unavailable")
            else:
                if row["handoff_stage"] is None or (
                    binding["profile_id"],
                    binding["deployment_digest"],
                ) != (row["profile_id"], row["deployment_digest"]):
                    raise ControlStoreError("internal_unavailable")
                intent = {
                    key: binding[key]
                    for key in ("unit", "nonce", "created_boottime", "total_seconds")
                }
                if child is None:
                    if (
                        proof.get("never_started") is not True
                        or proof.get("child_intent") != intent
                        or binding["invocation_id"] is not None
                        or self._now()
                        < float(binding["created_boottime"]) + int(binding["total_seconds"]) + 30
                    ):
                        raise ControlStoreError("internal_unavailable")
                else:
                    expected_child = {
                        "unit": binding["unit"],
                        "invocation_id": binding["invocation_id"],
                        "pid": binding["main_pid"],
                        "start_ticks": binding["main_start_ticks"],
                        "boot_id": binding["boot_id"],
                        "control_group": binding["control_group"],
                    }
                    if child != expected_child or any(
                        value is None for value in expected_child.values()
                    ):
                        raise ControlStoreError("internal_unavailable")
                proof["child_intent"] = intent
            proof["allocation_binding_sha256"] = row["allocation_sha256"]
            proof["handoff_stage"] = row["handoff_stage"]
            connection.execute(
                "UPDATE aos_control_requests SET drain_json=?,child_json=?,state='draining' "
                "WHERE request_id=?",
                (canonical(proof), canonical(child), row["request_id"]),
            )
            connection.commit()

    def finish(
        self,
        connection: sqlite3.Connection,
        lease: Mapping[str, Any],
        *,
        expired: bool,
        recovered: bool = False,
    ) -> None:
        """Terminal receipt is written before scheduler clears this exact lease."""
        row = self._bound(connection, lease)
        if row["receipt_json"] is not None or row["drain_json"] is None:
            raise ControlStoreError("internal_unavailable")
        proof = json.loads(row["drain_json"])
        if proof["child_generation"] is None and row["handoff_stage"] in {"start", "go"}:
            # Also reject an older persisted time-only proof at every release
            # path, including ordinary release and quarantined recovery.
            raise ControlStoreError("internal_unavailable")
        if (
            proof["allocation_binding_sha256"] != row["allocation_sha256"]
            or proof["handoff_stage"] != row["handoff_stage"]
        ):
            raise ControlStoreError("internal_unavailable")
        state, reason = (
            ("canceled", "caller_cancel")
            if row["cancel_requested"]
            else (
                ("expired", "recovered_after_crash" if recovered else "turn_timeout")
                if expired
                else (
                    ("completed", "success")
                    if row["result_json"]
                    else ("failed", "execution_failed")
                )
            )
        )
        self._terminal(
            connection,
            row,
            state,
            reason,
            "recovered_released" if recovered else "released",
            digest(proof),
            None,
        )

    @staticmethod
    def _original_budget(row: sqlite3.Row) -> dict[str, Any] | None:
        """Reconstruct assigned clocks from retained rows without rebasing them."""
        if row["budget_json"] is None:
            return None
        allocation = (
            None if row["allocation_json"] is None else json.loads(row["allocation_json"])["lease"]
        )
        deadlines = {
            key: None if allocation is None else allocation[key]
            for key in ("activation_deadline", "inference_deadline", "total_deadline")
        }
        if row["ready_deadlines_json"] is not None:
            deadlines.update(json.loads(row["ready_deadlines_json"]))
        return {
            **json.loads(row["budget_json"]),
            **deadlines,
            "envelope_deadline": row["original_deadline"],
            "queue_deadline": row["queue_deadline"],
            "admitted_boottime": None
            if row["original_deadline"] is None
            else row["created_boottime"],
            "boot_id": row["original_boot_id"],
        }

    def _terminal(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        state: str,
        reason: str,
        outcome: str,
        drain_hash: str | None,
        no_admission_hash: str | None,
    ) -> None:
        receipt = {
            "schema": "aos-scientist-terminal.v1",
            "request_id": row["request_id"],
            "request_sha256": row["request_sha256"],
            "original_principal": json.loads(row["principal_json"]),
            "profile_id": row["profile_id"],
            "deployment_digest": row["deployment_digest"],
            "profile_config_sha256": row["profile_config_sha256"],
            "response_schema_sha256": row["response_schema_sha256"],
            "admission_binding": None
            if row["admission_binding_json"] is None
            else json.loads(row["admission_binding_json"]),
            "admission_binding_sha256": row["admission_binding_sha256"],
            "original_budget": self._original_budget(row),
            "allocation_binding_sha256": row["allocation_sha256"],
            "child_generation": None
            if row["child_json"] is None
            else json.loads(row["child_json"]),
            "drain_evidence_sha256": drain_hash,
            "no_admission_evidence_sha256": no_admission_hash,
            "release_outcome": outcome,
            "terminal_state": state,
            "reason_code": reason,
            "result_sha256": digest(json.loads(row["result_json"]))
            if state == "completed"
            else None,
            "recorded_boot_id": self._boot_id(),
            "recorded_boottime": self._now(),
        }
        receipt["receipt_sha256"] = digest(receipt)
        connection.execute(
            "UPDATE aos_control_requests SET receipt_json=?,state=? WHERE request_id=? AND "
            "receipt_json IS NULL",
            (canonical(receipt), state, row["request_id"]),
        )


Store = ControlStore
