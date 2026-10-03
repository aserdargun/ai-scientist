"""Cross-process, two-principal GPU turn arbitration with finite deadlines.

The caller identity resolver is trusted service composition. Callers never
submit a priority or claim a different principal by supplying an owner string.
The resolver must bind the requested lane to the exact live service process.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import sqlite3
import subprocess  # nosec B404 -- fixed systemctl command with validated unit names
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from lab.llm.aos_gpu_control_store import ControlStore

OWNERS = ("aos", "lab")
HEARTBEAT_SECONDS = 30
DEFAULT_ACTIVATION_SECONDS = 180
MAX_ACTIVATION_SECONDS = 600
DEFAULT_INFERENCE_SECONDS = 30
MAX_INFERENCE_SECONDS = 540
MAX_TOTAL_SECONDS = 720
DEFAULT_TOTAL_SECONDS = DEFAULT_ACTIVATION_SECONDS + DEFAULT_INFERENCE_SECONDS
MAX_DRAIN_SECONDS = 30
MAX_QUEUE_SECONDS = 900
MAX_REQUEST_BYTES = 128 * 1024
SYSTEMCTL = "/usr/bin/systemctl"
SYSTEMD_PROPERTIES = (
    "LoadState",
    "ActiveState",
    "InvocationID",
    "ControlGroup",
    "MainPID",
)


class LeaseConflict(RuntimeError):
    """Raised when a stale or foreign client mutates a GPU lease."""


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """Linux process identity resistant to PID reuse and host reboot."""

    pid: int
    start_ticks: int
    boot_id: str


@dataclass(frozen=True, slots=True)
class PrincipalReceipt:
    """Resolver-authenticated lane and exact service-process identity."""

    owner: str
    identity: ProcessIdentity
    unit: str
    invocation_id: str


class PrincipalResolver(Protocol):
    """Trusted deployment mapping from fixed lane names to live service units."""

    def resolve(self, owner: str) -> PrincipalReceipt: ...

    def verify(self, receipt: PrincipalReceipt) -> bool: ...


@dataclass(frozen=True, slots=True)
class GpuLease:
    """Fenced grant for one startup plus one short inference turn."""

    owner: str
    request_id: str
    fencing_token: int
    phase: str
    activation_deadline: float
    inference_deadline: float
    total_deadline: float
    heartbeat_deadline: float
    slice_seconds: int
    owner_identity: ProcessIdentity
    owner_unit: str
    owner_invocation_id: str

    @property
    def expires_at(self) -> float:
        """Compatibility projection: the current phase's finite deadline."""
        return self.activation_deadline if self.phase == "activating" else self.inference_deadline


@dataclass(frozen=True, slots=True)
class QueueEntry:
    """Idempotent, principal-bound FIFO ticket."""

    owner: str
    request_id: str
    payload_sha256: str
    submitted_at: float
    activation_seconds: int
    inference_seconds: int
    total_seconds: int
    queue_deadline: float
    owner_identity: ProcessIdentity
    owner_unit: str
    owner_invocation_id: str


def boottime() -> float:
    """Return a reboot-scoped monotonic clock suitable for persisted deadlines."""
    clock_id = getattr(time, "CLOCK_BOOTTIME", None)
    if clock_id is None:
        return time.monotonic()
    return time.clock_gettime(clock_id)


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()


def _read_process_identity(pid: int) -> ProcessIdentity | None:
    """Read PID start time and boot identity; unknown access fails closed."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except FileNotFoundError:
        return None
    fields = stat.rsplit(")", maxsplit=1)[1].split()
    if fields[0] == "Z":
        return None
    return ProcessIdentity(pid, int(fields[19]), _boot_id())


def _current_process_identity() -> ProcessIdentity:
    identity = _read_process_identity(os.getpid())
    if identity is None:
        raise RuntimeError("cannot identify the current process")
    return identity


def _process_identity_alive(identity: ProcessIdentity) -> bool:
    if identity.boot_id != _boot_id():
        return False
    try:
        current = _read_process_identity(identity.pid)
    except (PermissionError, OSError, IndexError, ValueError):
        return True  # unreadable is not proof of death
    return current is not None and current == identity


def _principal_unit_matches(owner: str, unit: str) -> bool:
    if owner not in OWNERS:
        return False
    fixed_gpu_unit = re.fullmatch(rf"swapp-{owner}-gpu-[a-z0-9_.@-]+\.service", unit)
    run_bound_lab_dispatch = owner == "lab" and re.fullmatch(
        r"swapp-ai-scientist-director-dispatch-[0-9a-f]{32}\.service", unit
    )
    return fixed_gpu_unit is not None or bool(run_bound_lab_dispatch)


def _systemctl_show(unit: str, *, owner: str, timeout: float = 3.0) -> dict[str, str]:
    if not _principal_unit_matches(owner, unit):
        raise ValueError("GPU service unit name is outside the fixed deployment namespace")
    if not 0 < timeout <= 3.0:
        raise ValueError("systemd principal lookup timeout must be bounded")
    result = subprocess.run(  # nosec B603 -- fixed binary, fixed properties, validated unit
        [
            SYSTEMCTL,
            "--user",
            "show",
            "--no-pager",
            *[f"--property={x}" for x in SYSTEMD_PROPERTIES],
            unit,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("systemd principal lookup failed")
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in SYSTEMD_PROPERTIES:
            values[key] = value.strip()
    if set(values) != set(SYSTEMD_PROPERTIES):
        raise RuntimeError("systemd principal response is incomplete")
    return values


def _process_cgroup(pid: int) -> str:
    data = Path(f"/proc/{pid}/cgroup").read_text(encoding="ascii")
    for line in data.splitlines():
        hierarchy, _, path = line.partition(":")
        if hierarchy == "0":
            _, _, path = path.partition(":")
            if path.startswith("/"):
                return path.rstrip("/")
    raise RuntimeError("process cgroup identity is unavailable")


class SystemdPrincipalResolver:
    """Resolve only the deployment's fixed AOS/Lab unit mapping."""

    def __init__(self, units: dict[str, str], *, aggregate_slice: str = "swapp-gpu.slice") -> None:
        if set(units) != set(OWNERS):
            raise ValueError("principal map must define exactly aos and lab")
        self._units = dict(units)
        self._slice = aggregate_slice
        for owner, unit in self._units.items():
            if not _principal_unit_matches(owner, unit):
                raise ValueError("principal unit does not match its fixed owner")
        if aggregate_slice != "swapp-gpu.slice":
            raise ValueError("aggregate GPU slice is fixed by deployment")

    def resolve(self, owner: str) -> PrincipalReceipt:
        if owner not in self._units:
            raise LeaseConflict("unknown GPU principal")
        identity = _current_process_identity()
        values = _systemctl_show(self._units[owner], owner=owner)
        invocation = values["InvocationID"]
        cgroup = values["ControlGroup"].rstrip("/")
        expected_tail = "/" + self._units[owner]
        if (
            values["LoadState"] != "loaded"
            or values["ActiveState"] != "active"
            or not re.fullmatch(r"[0-9a-f]{32}", invocation)
            or values["MainPID"] != str(identity.pid)
            or not cgroup.endswith(expected_tail)
            or "/" + self._slice + "/" not in cgroup
            or _process_cgroup(identity.pid) != cgroup
        ):
            raise LeaseConflict("caller is not the authenticated GPU service process")
        return PrincipalReceipt(owner, identity, self._units[owner], invocation)

    def verify(self, receipt: PrincipalReceipt) -> bool:
        if receipt.owner not in self._units:
            return False
        if receipt.unit != self._units[receipt.owner] and not (
            receipt.owner == "lab"
            and re.fullmatch(
                r"swapp-ai-scientist-director-dispatch-[0-9a-f]{32}\.service",
                receipt.unit,
            )
        ):
            return False
        if not _process_identity_alive(receipt.identity):
            return False
        values = _systemctl_show(receipt.unit, owner=receipt.owner)
        cgroup = values["ControlGroup"].rstrip("/")
        return (
            values["LoadState"] == "loaded"
            and values["ActiveState"] == "active"
            and values["InvocationID"] == receipt.invocation_id
            and values["MainPID"] == str(receipt.identity.pid)
            and cgroup.endswith("/" + receipt.unit)
            and "/" + self._slice + "/" in cgroup
            and _process_cgroup(receipt.identity.pid) == cgroup
        )


class SharedGpuScheduler:
    """SQLite-backed alternating AOS/Lab admission for bounded model calls.

    Heartbeats extend only liveness, never the activation, inference, or total
    deadline. An expired turn remains quarantined until its trusted drain
    verifier proves that exact service invocation is gone.
    """

    def __init__(
        self,
        database: Path,
        *,
        principal_resolver: PrincipalResolver,
        drain_verifier: Callable[[GpuLease], bool],
        max_activation_seconds: int = DEFAULT_ACTIVATION_SECONDS,
        max_inference_seconds: int = DEFAULT_INFERENCE_SECONDS,
        max_total_seconds: int = DEFAULT_TOTAL_SECONDS,
        queue_timeout_seconds: int = MAX_QUEUE_SECONDS,
        clock: Callable[[], float] = boottime,
        control_store: ControlStore | None = None,
    ) -> None:
        self._check_int(max_activation_seconds, 1, MAX_ACTIVATION_SECONDS, "activation")
        self._check_int(max_inference_seconds, 1, MAX_INFERENCE_SECONDS, "inference")
        self._check_int(max_total_seconds, 1, MAX_TOTAL_SECONDS, "total")
        self._check_int(queue_timeout_seconds, 1, 3600, "queue")
        if max_total_seconds < max_activation_seconds + max_inference_seconds:
            raise ValueError("total deadline must cover activation plus inference")
        if queue_timeout_seconds < max_total_seconds + MAX_DRAIN_SECONDS:
            raise ValueError("queue deadline must cover a full turn and bounded drain")
        self.max_activation_seconds = max_activation_seconds
        self.max_inference_seconds = max_inference_seconds
        self.max_total_seconds = max_total_seconds
        self.queue_timeout_seconds = queue_timeout_seconds
        self._principal_resolver = principal_resolver
        self._drain_verifier = drain_verifier
        self._clock = clock
        if control_store is not None and control_store.database.resolve() != database.resolve():
            raise ValueError("control records must use the sole arbiter database")
        self._control_store = control_store
        database.parent.mkdir(parents=True, exist_ok=True)
        self._database = database
        self._initialize_schema()

    @staticmethod
    def _check_int(value: int, low: int, high: int, label: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{label} seconds must be an integer in [{low}, {high}]")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database, timeout=5.0, isolation_level=None)
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_schema(self) -> None:
        with closing(self._connect()) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'gpu_%'"
                )
            }
            if tables and "gpu_turn_requests" not in tables:
                raise RuntimeError("legacy GPU scheduler schema needs explicit offline migration")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS gpu_turn_requests (
                    owner TEXT NOT NULL CHECK(owner IN ('aos','lab')),
                    request_id TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
                    submitted_at REAL NOT NULL,
                    activation_seconds INTEGER NOT NULL,
                    inference_seconds INTEGER NOT NULL,
                    total_seconds INTEGER NOT NULL,
                    queue_deadline REAL NOT NULL,
                    owner_pid INTEGER NOT NULL,
                    owner_start_ticks INTEGER NOT NULL,
                    owner_boot_id TEXT NOT NULL,
                    owner_unit TEXT NOT NULL,
                    owner_invocation_id TEXT NOT NULL,
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    state TEXT NOT NULL CHECK(
                        state IN ('queued','active','done','expired','canceled')
                    ),
                    UNIQUE(owner, request_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_queued_gpu_request_per_owner
                    ON gpu_turn_requests(owner) WHERE state='queued';
                CREATE TABLE IF NOT EXISTS gpu_turn_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    next_owner TEXT NOT NULL CHECK(next_owner IN ('aos','lab')),
                    active_owner TEXT,
                    active_request_id TEXT,
                    active_token INTEGER NOT NULL,
                    phase TEXT,
                    activation_deadline REAL,
                    inference_deadline REAL,
                    total_deadline REAL,
                    heartbeat_deadline REAL,
                    inference_seconds INTEGER,
                    owner_pid INTEGER,
                    owner_start_ticks INTEGER,
                    owner_boot_id TEXT,
                    owner_unit TEXT,
                    owner_invocation_id TEXT,
                    CHECK ((active_owner IS NULL) = (active_request_id IS NULL))
                );
                INSERT OR IGNORE INTO gpu_turn_state(singleton,next_owner,active_token)
                    VALUES(1,'aos',0);
                """
            )

    def _now(self) -> float:
        value = self._clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError("monotonic time must be finite and non-negative")
        return float(value)

    def _receipt(self, owner: str) -> PrincipalReceipt:
        if owner not in OWNERS:
            raise LeaseConflict("unknown GPU principal")
        receipt = self._principal_resolver.resolve(owner)
        if receipt.owner != owner or not self._principal_resolver.verify(receipt):
            raise LeaseConflict("GPU principal identity failed verification")
        return receipt

    @staticmethod
    def _entry(row: sqlite3.Row) -> QueueEntry:
        return QueueEntry(
            owner=row["owner"],
            request_id=row["request_id"],
            payload_sha256=row["payload_sha256"],
            submitted_at=float(row["submitted_at"]),
            activation_seconds=int(row["activation_seconds"]),
            inference_seconds=int(row["inference_seconds"]),
            total_seconds=int(row["total_seconds"]),
            queue_deadline=float(row["queue_deadline"]),
            owner_identity=ProcessIdentity(
                row["owner_pid"], row["owner_start_ticks"], row["owner_boot_id"]
            ),
            owner_unit=row["owner_unit"],
            owner_invocation_id=row["owner_invocation_id"],
        )

    def submit(
        self,
        owner: str,
        request_id: str,
        payload: bytes,
        *,
        activation_seconds: int | None = None,
        inference_seconds: int | None = None,
        total_seconds: int | None = None,
        queue_timeout_seconds: int | None = None,
    ) -> QueueEntry:
        """Create an idempotent authenticated FIFO ticket with immutable budgets."""
        if not isinstance(payload, bytes) or not payload or len(payload) > MAX_REQUEST_BYTES:
            raise ValueError("a non-empty payload exceeds the registered request bound")
        if not request_id or len(request_id) > 128:
            raise ValueError("request_id must contain 1..128 characters")
        activation = (
            self.max_activation_seconds if activation_seconds is None else activation_seconds
        )
        inference = self.max_inference_seconds if inference_seconds is None else inference_seconds
        total = self.max_total_seconds if total_seconds is None else total_seconds
        queue_timeout = (
            self.queue_timeout_seconds if queue_timeout_seconds is None else queue_timeout_seconds
        )
        self._check_int(activation, 1, self.max_activation_seconds, "activation")
        self._check_int(inference, 1, self.max_inference_seconds, "inference")
        self._check_int(total, 1, self.max_total_seconds, "total")
        self._check_int(queue_timeout, 1, 3600, "queue")
        if total < activation + inference:
            raise ValueError("total deadline must cover the requested activation and inference")
        timestamp = self._now()
        receipt = self._receipt(owner)
        digest = hashlib.sha256(payload).hexdigest()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            queue_deadline = timestamp + queue_timeout
            if owner == "aos" and self._control_store is not None:
                queue_deadline = self._control_store.before_submit(
                    connection, request_id, digest, queue_deadline, asdict(receipt)
                )
            existing = connection.execute(
                "SELECT * FROM gpu_turn_requests WHERE owner=? AND request_id=?",
                (owner, request_id),
            ).fetchone()
            if existing is not None:
                expected = (digest, activation, inference, total)
                actual = (
                    existing["payload_sha256"],
                    existing["activation_seconds"],
                    existing["inference_seconds"],
                    existing["total_seconds"],
                )
                if actual != expected:
                    connection.rollback()
                    raise LeaseConflict("idempotency key reused with different request terms")
                if (
                    existing["owner_unit"] != receipt.unit
                    or existing["owner_invocation_id"] != receipt.invocation_id
                ):
                    connection.rollback()
                    raise LeaseConflict("idempotent retry came from a different service invocation")
                entry = self._entry(existing)
                connection.commit()
                return entry
            try:
                connection.execute(
                    "INSERT INTO gpu_turn_requests(owner,request_id,payload_sha256,submitted_at,"
                    "activation_seconds,inference_seconds,total_seconds,queue_deadline,owner_pid,"
                    "owner_start_ticks,owner_boot_id,owner_unit,owner_invocation_id,state) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'queued')",
                    (
                        owner,
                        request_id,
                        digest,
                        timestamp,
                        activation,
                        inference,
                        total,
                        queue_deadline,
                        receipt.identity.pid,
                        receipt.identity.start_ticks,
                        receipt.identity.boot_id,
                        receipt.unit,
                        receipt.invocation_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise LeaseConflict("principal already has a queued GPU ticket") from exc
            created = connection.execute(
                "SELECT * FROM gpu_turn_requests WHERE owner=? AND request_id=?",
                (owner, request_id),
            ).fetchone()
            connection.commit()
            if created is None:
                raise RuntimeError("inserted GPU ticket could not be read back")
            return self._entry(created)

    def _lease_from_row(self, row: sqlite3.Row) -> GpuLease:
        return GpuLease(
            owner=row["active_owner"],
            request_id=row["active_request_id"],
            fencing_token=int(row["active_token"]),
            phase=row["phase"],
            activation_deadline=float(row["activation_deadline"]),
            inference_deadline=float(row["inference_deadline"]),
            total_deadline=float(row["total_deadline"]),
            heartbeat_deadline=float(row["heartbeat_deadline"]),
            slice_seconds=int(row["inference_seconds"]),
            owner_identity=ProcessIdentity(
                row["owner_pid"], row["owner_start_ticks"], row["owner_boot_id"]
            ),
            owner_unit=row["owner_unit"],
            owner_invocation_id=row["owner_invocation_id"],
        )

    @staticmethod
    def _same(lease: GpuLease, row: sqlite3.Row) -> bool:
        return (lease.owner, lease.request_id, lease.fencing_token) == (
            row["active_owner"],
            row["active_request_id"],
            row["active_token"],
        )

    def _assert_lease_caller(self, lease: GpuLease) -> None:
        receipt = self._receipt(lease.owner)
        if (
            receipt.identity != lease.owner_identity
            or receipt.unit != lease.owner_unit
            or receipt.invocation_id != lease.owner_invocation_id
        ):
            raise LeaseConflict("GPU lease belongs to a different service invocation")

    def try_acquire(self, owner: str, request_id: str) -> GpuLease | None:
        """Claim only the next principal's oldest live ticket."""
        timestamp = self._now()
        receipt = self._receipt(owner)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute("SELECT * FROM gpu_turn_state WHERE singleton=1").fetchone()
            if state["active_owner"] is not None:
                active_identity = ProcessIdentity(
                    state["owner_pid"], state["owner_start_ticks"], state["owner_boot_id"]
                )
                active_receipt = PrincipalReceipt(
                    state["active_owner"],
                    active_identity,
                    state["owner_unit"],
                    state["owner_invocation_id"],
                )
                try:
                    active_principal_valid = self._principal_resolver.verify(active_receipt)
                except (OSError, RuntimeError, subprocess.SubprocessError):
                    active_principal_valid = False
                phase_deadline = (
                    state["activation_deadline"]
                    if state["phase"] == "activating"
                    else state["inference_deadline"]
                )
                if (
                    state["owner_boot_id"] != _boot_id()
                    or not _process_identity_alive(active_identity)
                    or not active_principal_valid
                    or timestamp >= phase_deadline
                    or timestamp >= state["heartbeat_deadline"]
                    or timestamp >= state["total_deadline"]
                ):
                    connection.execute(
                        "UPDATE gpu_turn_state SET phase='quarantined' WHERE singleton=1"
                    )
                connection.commit()
                return None
            if self._control_store is not None:
                expired_tickets = connection.execute(
                    "SELECT request_id FROM gpu_turn_requests "
                    "WHERE owner='aos' AND state='queued' AND queue_deadline<=?",
                    (timestamp,),
                ).fetchall()
                for ticket in expired_tickets:
                    self._control_store.no_admission(
                        connection, ticket["request_id"], "expired", "queue_timeout"
                    )
            connection.execute(
                "UPDATE gpu_turn_requests SET state='expired' "
                "WHERE state='queued' AND queue_deadline<=?",
                (timestamp,),
            )
            requests = connection.execute(
                "SELECT * FROM gpu_turn_requests WHERE state='queued' ORDER BY sequence"
            ).fetchall()
            live = []
            for ticket in requests:
                identity = ProcessIdentity(
                    ticket["owner_pid"], ticket["owner_start_ticks"], ticket["owner_boot_id"]
                )
                persisted = PrincipalReceipt(
                    ticket["owner"], identity, ticket["owner_unit"], ticket["owner_invocation_id"]
                )
                if not _process_identity_alive(identity) or not self._principal_resolver.verify(
                    persisted
                ):
                    if ticket["owner"] == "aos" and self._control_store is not None:
                        self._control_store.no_admission(
                            connection, ticket["request_id"], "expired", "generation_lost"
                        )
                    connection.execute(
                        "UPDATE gpu_turn_requests SET state='expired' "
                        "WHERE owner=? AND request_id=?",
                        (ticket["owner"], ticket["request_id"]),
                    )
                else:
                    live.append(ticket)
            if not live:
                connection.commit()
                return None
            next_owner = state["next_owner"]
            chosen = next((row for row in live if row["owner"] == next_owner), None)
            if chosen is None:
                other = "lab" if next_owner == "aos" else "aos"
                chosen = next((row for row in live if row["owner"] == other), None)
            if chosen is None or (chosen["owner"], chosen["request_id"]) != (owner, request_id):
                connection.commit()
                return None
            if (
                chosen["owner_pid"],
                chosen["owner_start_ticks"],
                chosen["owner_boot_id"],
                chosen["owner_unit"],
                chosen["owner_invocation_id"],
            ) != (
                receipt.identity.pid,
                receipt.identity.start_ticks,
                receipt.identity.boot_id,
                receipt.unit,
                receipt.invocation_id,
            ):
                connection.commit()
                return None
            token = int(state["active_token"]) + 1
            activation_deadline = timestamp + int(chosen["activation_seconds"])
            total_deadline = timestamp + int(chosen["total_seconds"])
            if owner == "aos" and self._control_store is not None:
                total_deadline = min(
                    total_deadline, self._control_store.before_acquire(connection, request_id)
                )
            activation_deadline = min(
                activation_deadline, total_deadline - int(chosen["inference_seconds"])
            )
            inference_deadline = total_deadline
            heartbeat_deadline = min(timestamp + HEARTBEAT_SECONDS, activation_deadline)
            if activation_deadline <= timestamp:
                if owner == "aos" and self._control_store is not None:
                    self._control_store.no_admission(
                        connection, request_id, "expired", "queue_timeout"
                    )
                connection.execute(
                    "UPDATE gpu_turn_requests SET state='expired' WHERE owner=? AND request_id=?",
                    (owner, request_id),
                )
                connection.commit()
                return None
            connection.execute(
                "UPDATE gpu_turn_requests SET state='active' WHERE owner=? AND request_id=?",
                (owner, request_id),
            )
            connection.execute(
                "UPDATE gpu_turn_state SET active_owner=?,active_request_id=?,active_token=?,"
                "phase='activating',activation_deadline=?,inference_deadline=?,total_deadline=?,"
                "heartbeat_deadline=?,inference_seconds=?,"
                "owner_pid=?,owner_start_ticks=?,owner_boot_id=?,owner_unit=?,"
                "owner_invocation_id=? "
                "WHERE singleton=1",
                (
                    owner,
                    request_id,
                    token,
                    activation_deadline,
                    inference_deadline,
                    total_deadline,
                    heartbeat_deadline,
                    int(chosen["inference_seconds"]),
                    receipt.identity.pid,
                    receipt.identity.start_ticks,
                    receipt.identity.boot_id,
                    receipt.unit,
                    receipt.invocation_id,
                ),
            )
            if owner == "aos" and self._control_store is not None:
                allocated = connection.execute(
                    "SELECT * FROM gpu_turn_state WHERE singleton=1"
                ).fetchone()
                self._control_store.bind_allocation(
                    connection, asdict(self._lease_from_row(allocated))
                )
            connection.commit()
        return GpuLease(
            owner,
            request_id,
            token,
            "activating",
            activation_deadline,
            inference_deadline,
            total_deadline,
            heartbeat_deadline,
            int(chosen["inference_seconds"]),
            receipt.identity,
            receipt.unit,
            receipt.invocation_id,
        )

    def heartbeat(self, lease: GpuLease) -> GpuLease | None:
        """Renew the 30-second liveness window without extending hard deadlines."""
        timestamp = self._now()
        self._assert_lease_caller(lease)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute("SELECT * FROM gpu_turn_state WHERE singleton=1").fetchone()
            deadline = (
                state["activation_deadline"]
                if state["phase"] == "activating"
                else state["inference_deadline"]
            )
            if not self._same(lease, state) or state["phase"] == "quarantined":
                connection.commit()
                return None
            if (
                timestamp >= deadline
                or timestamp >= state["heartbeat_deadline"]
                or timestamp >= state["total_deadline"]
            ):
                connection.execute(
                    "UPDATE gpu_turn_state SET phase='quarantined' WHERE singleton=1"
                )
                connection.commit()
                return None
            new_heartbeat = min(timestamp + HEARTBEAT_SECONDS, deadline, state["total_deadline"])
            connection.execute(
                "UPDATE gpu_turn_state SET heartbeat_deadline=? WHERE singleton=1", (new_heartbeat,)
            )
            row = connection.execute("SELECT * FROM gpu_turn_state WHERE singleton=1").fetchone()
            connection.commit()
        return self._lease_from_row(row)

    def mark_ready(self, lease: GpuLease) -> GpuLease | None:
        """End startup and start one inference phase, only once."""
        timestamp = self._now()
        self._assert_lease_caller(lease)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute("SELECT * FROM gpu_turn_state WHERE singleton=1").fetchone()
            if not self._same(lease, state):
                connection.commit()
                return None
            if state["phase"] == "inference":
                # Idempotent acknowledgement: never extend the inference deadline.
                if (
                    timestamp >= state["inference_deadline"]
                    or timestamp >= state["heartbeat_deadline"]
                    or timestamp >= state["total_deadline"]
                ):
                    connection.execute(
                        "UPDATE gpu_turn_state SET phase='quarantined' WHERE singleton=1"
                    )
                    connection.commit()
                    return None
                connection.commit()
                return self._lease_from_row(state)
            if (
                state["phase"] != "activating"
                or timestamp >= state["activation_deadline"]
                or timestamp >= state["heartbeat_deadline"]
            ):
                if state["phase"] != "quarantined":
                    connection.execute(
                        "UPDATE gpu_turn_state SET phase='quarantined' WHERE singleton=1"
                    )
                connection.commit()
                return None
            inference_deadline = min(
                timestamp + int(state["inference_seconds"]), state["total_deadline"]
            )
            if inference_deadline <= timestamp:
                connection.execute(
                    "UPDATE gpu_turn_state SET phase='quarantined' WHERE singleton=1"
                )
                connection.commit()
                return None
            heartbeat_deadline = min(timestamp + HEARTBEAT_SECONDS, inference_deadline)
            connection.execute(
                "UPDATE gpu_turn_state SET phase='inference',inference_deadline=?,"
                "heartbeat_deadline=? WHERE singleton=1",
                (inference_deadline, heartbeat_deadline),
            )
            row = connection.execute("SELECT * FROM gpu_turn_state WHERE singleton=1").fetchone()
            if lease.owner == "aos" and self._control_store is not None:
                self._control_store.mark_ready(connection, asdict(self._lease_from_row(row)))
            connection.commit()
        return self._lease_from_row(row)

    def cancel_queued(self, owner: str, request_id: str) -> bool:
        """Cancel this authenticated invocation's own queued ticket."""
        receipt = self._receipt(owner)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner_pid,owner_start_ticks,owner_boot_id,owner_unit,"
                "owner_invocation_id,state "
                "FROM gpu_turn_requests WHERE owner=? AND request_id=?",
                (owner, request_id),
            ).fetchone()
            if (
                row is None
                or row["state"] != "queued"
                or tuple(row[:5])
                != (
                    receipt.identity.pid,
                    receipt.identity.start_ticks,
                    receipt.identity.boot_id,
                    receipt.unit,
                    receipt.invocation_id,
                )
            ):
                connection.commit()
                return False
            connection.execute(
                "UPDATE gpu_turn_requests SET state='canceled' WHERE owner=? AND request_id=?",
                (owner, request_id),
            )
            if owner == "aos" and self._control_store is not None:
                self._control_store.no_admission(
                    connection, request_id, "canceled", "caller_cancel"
                )
            connection.commit()
            return True

    def _finish(
        self,
        connection: sqlite3.Connection,
        lease: GpuLease,
        timestamp: float,
        *,
        expired: bool,
        recovered: bool = False,
    ) -> None:
        if lease.owner == "aos" and self._control_store is not None:
            self._control_store.finish(
                connection, asdict(lease), expired=expired, recovered=recovered
            )
        owner = "lab" if lease.owner == "aos" else "aos"
        connection.execute(
            "UPDATE gpu_turn_requests SET state=? "
            "WHERE owner=? AND request_id=? AND state='active'",
            ("expired" if expired else "done", lease.owner, lease.request_id),
        )
        connection.execute(
            "UPDATE gpu_turn_state SET next_owner=?,active_owner=NULL,active_request_id=NULL,"
            "phase=NULL,activation_deadline=NULL,inference_deadline=NULL,total_deadline=NULL,"
            "heartbeat_deadline=NULL,inference_seconds=NULL,owner_pid=NULL,owner_start_ticks=NULL,"
            "owner_boot_id=NULL,owner_unit=NULL,owner_invocation_id=NULL "
            "WHERE singleton=1",
            (owner,),
        )

    def release(self, lease: GpuLease) -> None:
        """Release only after the trusted callback drains the recorded process."""
        self._assert_lease_caller(lease)
        with closing(self._connect()) as connection:
            state = connection.execute("SELECT * FROM gpu_turn_state WHERE singleton=1").fetchone()
            if not self._same(lease, state) or state["phase"] == "quarantined":
                raise LeaseConflict("stale or quarantined GPU fencing token")
            current = PrincipalReceipt(
                state["active_owner"],
                ProcessIdentity(
                    state["owner_pid"], state["owner_start_ticks"], state["owner_boot_id"]
                ),
                state["owner_unit"],
                state["owner_invocation_id"],
            )
            if not self._principal_resolver.verify(current):
                raise LeaseConflict("recorded GPU principal invocation is no longer active")
        # Keep the durable turn active while trusted drain runs, but do not hold
        # SQLite's write lock across systemd/GPU observations. The callback may
        # persist newly observed owned GPU PIDs before stopping the unit.
        if not self._drain_verifier(lease):
            raise LeaseConflict("GPU process drain/unload has not been verified")
        self._assert_lease_caller(lease)
        timestamp = self._now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute("SELECT * FROM gpu_turn_state WHERE singleton=1").fetchone()
            if not self._same(lease, state):
                connection.rollback()
                raise LeaseConflict("GPU turn changed while the owned process drained")
            current = PrincipalReceipt(
                state["active_owner"],
                ProcessIdentity(
                    state["owner_pid"], state["owner_start_ticks"], state["owner_boot_id"]
                ),
                state["owner_unit"],
                state["owner_invocation_id"],
            )
            if not self._principal_resolver.verify(current):
                connection.rollback()
                raise LeaseConflict("recorded GPU principal invocation is no longer active")
            expired = (
                state["phase"] == "quarantined"
                or timestamp >= state["total_deadline"]
                or timestamp
                >= (
                    state["activation_deadline"]
                    if state["phase"] == "activating"
                    else state["inference_deadline"]
                )
            )
            self._finish(connection, lease, timestamp, expired=expired)
            connection.commit()

    def recover_quarantined(self, expected_lease: GpuLease | None = None) -> bool:
        """Clear a timed-out turn only after trusted verification of exact drain."""
        with closing(self._connect()) as connection:
            state = connection.execute("SELECT * FROM gpu_turn_state WHERE singleton=1").fetchone()
            if state["active_owner"] is None or state["phase"] != "quarantined":
                return False
            lease = self._lease_from_row(state)
            if expected_lease is not None and not self._same(expected_lease, state):
                return False
        if not self._drain_verifier(lease):
            return False
        timestamp = self._now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute("SELECT * FROM gpu_turn_state WHERE singleton=1").fetchone()
            if (
                not self._same(lease, state)
                or state["phase"] != "quarantined"
                or (expected_lease is not None and not self._same(expected_lease, state))
            ):
                connection.rollback()
                return False
            self._finish(connection, lease, timestamp, expired=True, recovered=True)
            connection.commit()
            return True

    def recover_controlled_turn(self) -> bool:
        """Trusted service recovery loop; never used by a control request worker.

        Quarantine is committed before physical inspection. Blocking exact child
        drain runs through the existing verifier outside SQLite's write lock.
        """
        if self._control_store is None:
            return False
        timestamp = self._now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            unallocated_recovered = self._control_store.recover_unallocated(connection, limit=8)
            state = connection.execute("SELECT * FROM gpu_turn_state WHERE singleton=1").fetchone()
            if state["active_owner"] != "aos":
                connection.commit()
                return unallocated_recovered > 0
            lease = self._lease_from_row(state)
            controlled = connection.execute(
                "SELECT allocation_sha256 FROM aos_control_requests WHERE request_id=?",
                (lease.request_id,),
            ).fetchone()
            if controlled is None or controlled["allocation_sha256"] is None:
                connection.commit()
                return unallocated_recovered > 0
            current = PrincipalReceipt(
                lease.owner, lease.owner_identity, lease.owner_unit, lease.owner_invocation_id
            )
            try:
                valid = self._principal_resolver.verify(current)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                valid = False
            phase_deadline = (
                lease.activation_deadline
                if lease.phase == "activating"
                else lease.inference_deadline
            )
            needs_recovery = (
                lease.phase == "quarantined"
                or not valid
                or timestamp >= phase_deadline
                or timestamp >= lease.total_deadline
                or timestamp >= lease.heartbeat_deadline
                or self._control_store.should_recover(connection, asdict(lease))
            )
            if not needs_recovery:
                connection.commit()
                return unallocated_recovered > 0
            connection.execute("UPDATE gpu_turn_state SET phase='quarantined' WHERE singleton=1")
            self._control_store.mark_quarantined(connection, asdict(lease))
            connection.commit()
        released = self.recover_quarantined(expected_lease=lease)
        return released or unallocated_recovered > 0
