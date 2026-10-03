"""UNWIRED / UNADMITTED observational tombstones in the canonical ControlStore.

This prototype has no endpoint, authority issuer, scheduler or GPU operations.
Its readers are trusted service composition, NEVER values/deserializers supplied
by a request. They must independently authenticate current cleanup authority,
read retained original capture/source evidence and prove physical absence of
the exact old generations (including external deferred/quarantined dispatch).
Returning matching hashes or forwarding caller-supplied booleans is not such a
reader. Providers must be bounded; this module checks their deadline after each
return, but cannot preempt arbitrary synchronous Python callbacks.

Only this new table is mutated. The existing admission transaction must call
``assert_not_observed`` in the same canonical database before admitting a request.
No production provider composition is installed by this module.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import stat
import time
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from typing import Any

from lab.llm.aos_gpu_control_store import (
    ControlStore,
    ControlStoreError,
    canonical,
    control_deadline,
    digest,
)

TABLE = "aos_no_admission_observations"
MAX_OBSERVATIONS = 10_000
MAX_OPERATION_SECONDS = 5.0
# Every known exact-request persistence surface is inspected regardless of state
# or owner. New/missing tables require a provenance review, not an absence claim.
REQUEST_SURFACES = {
    "aos_control_requests": "request_id",
    "aos_control_reservations": "request_id",
    "aos_cleanup_grants": "request_id",
    "aos_control_idempotency": "target_request_id",
    "gpu_turn_requests": "request_id",
    "gpu_turn_state": "active_request_id",
    # Native runtime launch intent uses the scheduler lease request_id. The
    # owner/token compound key must not hide another generation or an uncertain
    # launch whose delayed unit creation still requires quarantine.
    "gpu_runtime_bindings": "request_id",
    "aos_gpu_child_bindings": "request_id",
    "aos_gpu_output_bindings": "request_id",
    "aos_gpu_turn_results": "request_id",
}
_ABSENCE_QUERIES = (
    "SELECT 1 FROM aos_control_requests WHERE request_id=? LIMIT 1",
    "SELECT 1 FROM aos_control_reservations WHERE request_id=? LIMIT 1",
    "SELECT 1 FROM aos_cleanup_grants WHERE request_id=? LIMIT 1",
    "SELECT 1 FROM aos_control_idempotency WHERE target_request_id=? LIMIT 1",
    "SELECT 1 FROM gpu_turn_requests WHERE request_id=? LIMIT 1",
    "SELECT 1 FROM gpu_turn_state WHERE active_request_id=? LIMIT 1",
    "SELECT 1 FROM gpu_runtime_bindings WHERE request_id=? LIMIT 1",
    "SELECT 1 FROM aos_gpu_child_bindings WHERE request_id=? LIMIT 1",
    "SELECT 1 FROM aos_gpu_output_bindings WHERE request_id=? LIMIT 1",
    "SELECT 1 FROM aos_gpu_turn_results WHERE request_id=? LIMIT 1",
)
_TABLE_SQL = (
    "CREATE TABLE aos_no_admission_observations ("
    "request_id TEXT PRIMARY KEY,request_sha256 TEXT NOT NULL,"
    "original_caller_generation_sha256 TEXT NOT NULL,"
    "admission_binding_sha256 TEXT NOT NULL,"
    "proof_sha256 TEXT NOT NULL,observation_sha256 TEXT NOT NULL UNIQUE,"
    "dispatch_provenance_json TEXT NOT NULL,observation_json TEXT NOT NULL)"
)
_SCHEMA = {
    TABLE: ("table", _TABLE_SQL),
    **{
        f"aos_observations_no_{operation.lower()}": (
            "trigger",
            f"CREATE TRIGGER aos_observations_no_{operation.lower()} "
            f"BEFORE {operation} ON aos_no_admission_observations BEGIN "
            "SELECT RAISE(ABORT, 'immutable observation'); END",
        )
        for operation in ("UPDATE", "DELETE")
    },
    "aos_observations_no_replace": (
        "trigger",
        "CREATE TRIGGER aos_observations_no_replace "
        "BEFORE INSERT ON aos_no_admission_observations WHEN EXISTS("
        "SELECT 1 FROM aos_no_admission_observations WHERE "
        "request_id=NEW.request_id OR observation_sha256=NEW.observation_sha256) "
        "BEGIN SELECT RAISE(ABORT, 'immutable observation'); END",
    ),
}


@dataclass(frozen=True, slots=True)
class ObserverAuthority:
    """Canonical preimages independently authenticated by trusted composition.

    ``observer`` is the actual current authenticated generation/source/config/
    capability. ``cleanup_scope`` binds the original store/session/runtime to
    the current stopped/PAUSED generation; it confers no inference permission.
    """

    observer: bytes
    cleanup_scope: bytes


@dataclass(frozen=True, slots=True)
class PhysicalEvidence:
    """Independent physical proof and complete deferred/quarantine inventory.

    ``dispatch_provenance`` is an internal retained canonical object containing
    target, source_fingerprints, caller_generation_sha256,
    broker_generation_sha256, provenance_complete, deferred_execution and
    quarantined_allocations. The latter arrays must be independently enumerated
    and empty. It is not a caller-supplied wire claim or a second allocator.
    """

    physical: bytes
    dispatch_provenance: bytes


def assert_not_observed(connection: sqlite3.Connection, request_id: str) -> None:
    """Read-only late-admission fence; caller holds its admission write txn.

    An absent table supports existing databases where observation was never
    configured. Schema errors propagate and deny admission. A historical lookup
    of a tombstone grants no authority to produce a fresh observation.
    """
    if not connection.in_transaction:
        raise ControlStoreError("internal_unavailable")
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)
    ).fetchone()
    if exists:
        NoAdmissionObservationStore._assert_schema(connection)
        if connection.execute(
            "SELECT 1 FROM aos_no_admission_observations WHERE request_id=?", (request_id,)
        ).fetchone():
            raise ControlStoreError("request_conflict")


def _object(raw: bytes) -> dict[str, Any]:
    if not isinstance(raw, bytes) or len(raw) > 524_288:
        raise ControlStoreError("unauthorized")
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or canonical(value).encode() != raw:
            raise ValueError("noncanonical evidence")
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ControlStoreError("unauthorized") from exc
    return value


class NoAdmissionObservationStore:
    """Default-deny prototype composed with the ONE existing ControlStore.

    ``observer_authority`` authenticates the observer and current cleanup scope
    for the exact target. ``original_reader`` independently reads the immutable
    original request/capture/binding and verifies retained source pins. The
    ``physical_reader`` independently verifies those original caller/broker
    generations, cgroups, complete child provenance and external deferred or
    quarantined execution fences. None may accept the desired proof as input.

    All three run inside BEGIN IMMEDIATE, before and after insertion. Their
    canonical full preimages must remain identical. No default provider exists.
    """

    def __init__(
        self,
        control_store: ControlStore,
        *,
        observer_authority: Callable[[Mapping[str, str]], ObserverAuthority] | None = None,
        original_reader: Callable[[Mapping[str, str]], bytes] | None = None,
        physical_reader: Callable[[Mapping[str, str], bytes], PhysicalEvidence] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._control = control_store
        self._observer_authority = observer_authority
        self._original_reader = original_reader
        self._physical_reader = physical_reader
        self._monotonic = monotonic
        self._identity = self._store_identity()
        self._initialize()

    def _initialize(self) -> None:
        previous = control_deadline.get()
        deadline = min(
            self._monotonic() + MAX_OPERATION_SECONDS,
            previous if previous is not None else math.inf,
        )
        context = control_deadline.set(deadline)
        try:
            self._check_deadline(deadline)
            with closing(self._control._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name IN (?,?,?,?)", tuple(_SCHEMA)
                ).fetchone()
                if existing is None:
                    for _kind, sql in _SCHEMA.values():
                        connection.execute(sql)
                self._assert_schema(connection)
                self._check_deadline(deadline)
                connection.commit()
        finally:
            control_deadline.reset(context)

    @staticmethod
    def _assert_schema(connection: sqlite3.Connection) -> None:
        for name, expected in _SCHEMA.items():
            row = connection.execute(
                "SELECT type,sql FROM sqlite_master WHERE name=?", (name,)
            ).fetchone()
            if row is None or tuple(row) != expected:
                raise ControlStoreError("incomplete_provenance")
        triggers = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        if triggers != set(_SCHEMA) - {TABLE}:
            raise ControlStoreError("incomplete_provenance")

    def _store_identity(self) -> dict[str, Any]:
        path = self._control.database
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or path.is_symlink()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ControlStoreError("internal_unavailable")
        return {
            "path_sha256": hashlib.sha256(str(path.resolve()).encode()).hexdigest(),
            "device": info.st_dev,
            "inode": info.st_ino,
            "uid": info.st_uid,
        }

    def _check_deadline(self, deadline: float) -> None:
        now = self._monotonic()
        if not math.isfinite(now) or now >= deadline:
            raise ControlStoreError("deadline_exceeded")
        if self._store_identity() != self._identity:
            raise ControlStoreError("request_conflict")

    def _read_evidence(self, target: dict[str, str], deadline: float) -> dict[str, bytes]:
        if (
            self._observer_authority is None
            or self._original_reader is None
            or self._physical_reader is None
        ):
            raise ControlStoreError("unauthorized")
        authority = self._observer_authority(dict(target))
        self._check_deadline(deadline)
        if not isinstance(authority, ObserverAuthority):
            raise ControlStoreError("unauthorized")
        original = self._original_reader(dict(target))
        self._check_deadline(deadline)
        physical = self._physical_reader(dict(target), original)
        self._check_deadline(deadline)
        if not isinstance(physical, PhysicalEvidence):
            raise ControlStoreError("unauthorized")
        evidence = {
            "observer": authority.observer,
            "cleanup_scope": authority.cleanup_scope,
            "original": original,
            "physical": physical.physical,
            "dispatch_provenance": physical.dispatch_provenance,
        }
        for raw in evidence.values():
            _object(raw)
        self._dispatch(target, evidence)
        return evidence

    @staticmethod
    def _dispatch(target: dict[str, str], evidence: dict[str, bytes]) -> dict[str, Any]:
        dispatch = _object(evidence["dispatch_provenance"])
        original = _object(evidence["original"])
        expected = {
            "target": target,
            "source_fingerprints": original["admission_record"]["admission_binding"][
                "source_fingerprints"
            ],
            "caller_generation_sha256": target["original_caller_generation_sha256"],
            "broker_generation_sha256": original["broker_generation_sha256"],
            "provenance_complete": True,
            "deferred_execution": [],
            "quarantined_allocations": [],
        }
        if canonical(dispatch) != canonical(expected):
            raise ControlStoreError("incomplete_provenance")
        return dispatch

    @staticmethod
    def _assert_absent(connection: sqlite3.Connection, request_id: str) -> None:
        NoAdmissionObservationStore._assert_schema(connection)
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if tables - {"sqlite_sequence", TABLE} != set(REQUEST_SURFACES):
            raise ControlStoreError("incomplete_provenance")
        for query in _ABSENCE_QUERIES:
            if connection.execute(query, (request_id,)).fetchone():
                raise ControlStoreError("request_conflict")

    def _mint(self, target: dict[str, str], evidence: dict[str, bytes]) -> dict[str, Any]:
        # Imported lazily to avoid coupling the normal admission fence to a
        # proposed wire schema or creating a circular ControlStore dependency.
        from lab.llm.aos_no_admission_observation import MAX_AGE_US

        sections = {
            key: _object(value) for key, value in evidence.items() if key != "dispatch_provenance"
        }
        dispatch = self._dispatch(target, evidence)
        now_us = int(self._control._now() * 1_000_000)
        freshness = {
            "clock": "CLOCK_BOOTTIME",
            "unit": "microseconds",
            "boot_id": self._control._boot_id(),
            "observed_boottime_us": now_us,
            "expires_boottime_us": now_us + MAX_AGE_US,
            "max_age_us": MAX_AGE_US,
        }
        tombstone = {
            "id": digest({"target": target, "sections": sections, "freshness": freshness})[:32],
            "target": target,
            "store": self._identity,
            "admission_record_sha256": sections["original"]["admission_record_sha256"],
            "intent_binding_sha256": sections["original"]["intent_binding_sha256"],
            "observer_generation_sha256": sections["observer"]["generation_sha256"],
            "physical_proof_sha256": sections["physical"]["proof_sha256"],
            "freshness": freshness,
            "late_admission_fenced": True,
        }
        canonical_proof = {
            "target": target,
            "store": self._identity,
            "admission_absent": True,
            "gpu_queue_absent": True,
            "allocation_absent": True,
            "child_binding_absent": True,
            "deferred_execution_absent": not dispatch["deferred_execution"],
            "quarantined_allocation_absent": not dispatch["quarantined_allocations"],
            "tombstone": tombstone,
            "tombstone_sha256": digest(tombstone),
        }
        observation = {
            "schema": "aos-scientist-no-admission-observation.v1",
            "version": 1,
            "target": target,
            **sections,
            "canonical": canonical_proof,
            "outcome": "never_received",
            "admission_budget": None,
            "freshness": freshness,
        }
        observation["observation_sha256"] = digest(observation)
        return observation

    def _validate(self, observation: dict[str, Any], evidence: dict[str, bytes]) -> dict[str, Any]:
        from lab.llm.aos_no_admission_observation import (
            ObservationExpectations,
            schema_hash,
            validate_observation,
        )

        expected = ObservationExpectations(
            schema_sha256=schema_hash(),
            canonical=canonical(observation["canonical"]).encode(),
            original=evidence["original"],
            observer=evidence["observer"],
            physical=evidence["physical"],
            cleanup_scope=evidence["cleanup_scope"],
        )
        return validate_observation(
            canonical(observation).encode(),
            expected=expected,
            now_boottime_us=int(self._control._now() * 1_000_000),
            current_boot_id=self._control._boot_id(),
        )

    def observe(self, target: Mapping[str, str], *, deadline_monotonic: float) -> dict[str, Any]:
        """Append once after independent checks; never inserts an admission.

        Same-proof retries return the original committed bytes while still
        requiring current authority and unexpired evidence. An expired proof
        cannot be refreshed or replaced by a later observer.
        """
        fields = {"request_id", "request_sha256", "original_caller_generation_sha256"}
        if (
            not isinstance(target, Mapping)
            or set(target) != fields
            or any(
                not isinstance(value, str)
                or re.fullmatch(r"[0-9a-f]{32}" if key == "request_id" else r"[0-9a-f]{64}", value)
                is None
                for key, value in target.items()
            )
        ):
            raise ControlStoreError("invalid_frame")
        target = dict(target)
        if (
            isinstance(deadline_monotonic, bool)
            or not isinstance(deadline_monotonic, (int, float))
            or not math.isfinite(deadline_monotonic)
        ):
            raise ControlStoreError("invalid_frame")
        deadline = min(deadline_monotonic, self._monotonic() + MAX_OPERATION_SECONDS)
        previous = control_deadline.get()
        if previous is not None:
            deadline = min(deadline, previous)
        self._check_deadline(deadline)
        context = control_deadline.set(deadline)
        try:
            with closing(self._control._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                evidence = self._read_evidence(target, deadline)
                self._assert_absent(connection, target["request_id"])
                proof_sha256 = digest({key: _object(value) for key, value in evidence.items()})
                row = connection.execute(
                    "SELECT * FROM aos_no_admission_observations WHERE request_id=?",
                    (target["request_id"],),
                ).fetchone()
                if row is not None:
                    observation = json.loads(row["observation_json"])
                    if (
                        observation["target"] != target
                        or row["proof_sha256"] != proof_sha256
                        or observation["canonical"]["store"] != self._identity
                    ):
                        raise ControlStoreError("request_conflict")
                else:
                    count = connection.execute(
                        "SELECT COUNT(*) FROM aos_no_admission_observations"
                    ).fetchone()[0]
                    if count >= MAX_OBSERVATIONS:
                        raise ControlStoreError("capacity_exceeded")
                    observation = self._mint(target, evidence)
                observation = self._validate(observation, evidence)
                if row is None:
                    connection.execute(
                        "INSERT INTO aos_no_admission_observations VALUES(?,?,?,?,?,?,?,?)",
                        (
                            target["request_id"],
                            target["request_sha256"],
                            target["original_caller_generation_sha256"],
                            observation["original"]["admission_binding_sha256"],
                            proof_sha256,
                            observation["observation_sha256"],
                            evidence["dispatch_provenance"].decode(),
                            canonical(observation),
                        ),
                    )
                if self._read_evidence(target, deadline) != evidence:
                    raise ControlStoreError("request_conflict")
                self._assert_absent(connection, target["request_id"])
                observation = self._validate(observation, evidence)
                self._check_deadline(deadline)
                connection.commit()
                return observation
        finally:
            control_deadline.reset(context)
