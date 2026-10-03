"""Opt-in READ-ONLY providers; no service start, grant issuer or DB writes.

A trusted launcher must independently review and pin the private configuration
SHA. ``inspect`` prepares evidence but never authenticates a live observer.
Observation callbacks require this process to be the exact active MainPID of
the fixed observer unit. Reviewed historical ordering is a separate, pinned
source attestation; absence is derived afresh from DB/process/cgroup/container
observations. Missing source or physical provenance denies, never becomes an
empty inventory. Historical source63 pins are distinct from current source66.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat

# Fixed readonly systemctl, git and docker commands; no shell or caller-selected binary.
import subprocess  # nosec B404
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from lab.llm import gpu_scheduler
from lab.llm.aos_gpu_control_store import control_deadline
from lab.llm.aos_no_admission_observation import (
    ObservationExpectations,
    canonical,
    decode,
    digest,
    schema_hash,
    validate_observation,
)
from lab.llm.aos_no_admission_store import (
    REQUEST_SURFACES,
    TABLE,
    ObserverAuthority,
    PhysicalEvidence,
)

UNIT = "swapp-scientist-no-admission-observer.service"
PURPOSE = "observe_no_admission"
CONFIG_SCHEMA = "aos-scientist-no-admission-provider.v1"
_CONFIG_KEYS = {
    "schema",
    "version",
    "target",
    "aos_database",
    "canonical_database",
    "original",
    "cleanup_scope",
    "sources",
    "cleanup_evidence",
    "dispatch_review",
    "observer",
    "observation_schema_sha256",
    "historical_receipt",
    "historical_repository",
    "cleanup_authority",
}
_HISTORICAL_REQUIRED = {
    "lab/llm/aos_gpu_executor.py",
    "lab/llm/aos_gpu_control_store.py",
    "lab/llm/gpu_scheduler.py",
    "lab/llm/aos_gpu_broker.py",
    "lab/llm/native_runtime.py",
}
_SQL_ABSENCE = (
    "SELECT 1 FROM aos_control_requests WHERE request_id=? LIMIT 1",
    "SELECT 1 FROM aos_control_reservations WHERE request_id=? LIMIT 1",
    "SELECT 1 FROM aos_cleanup_grants WHERE request_id=? LIMIT 1",
    "SELECT 1 FROM aos_control_idempotency WHERE target_request_id=? LIMIT 1",
    "SELECT 1 FROM gpu_turn_requests WHERE request_id=? LIMIT 1",
    "SELECT 1 FROM gpu_turn_state WHERE active_request_id=? LIMIT 1",
    # Match every owner, fencing token and launch state, including delayed
    # uncertain launches; runtime bindings are durable dispatch provenance.
    "SELECT 1 FROM gpu_runtime_bindings WHERE request_id=? LIMIT 1",
    "SELECT 1 FROM aos_gpu_child_bindings WHERE request_id=? LIMIT 1",
    "SELECT 1 FROM aos_gpu_output_bindings WHERE request_id=? LIMIT 1",
    "SELECT 1 FROM aos_gpu_turn_results WHERE request_id=? LIMIT 1",
)


class ProviderDenied(RuntimeError):
    """Bounded denial codes; no private request/path/process output is exposed."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ProviderDenied(code)


def _keys(value: Any, keys: set[str], code: str = "invalid_config") -> dict[str, Any]:
    _require(type(value) is dict and set(value) == keys, code)
    return dict(value)


def _hex(value: Any, length: int = 64) -> bool:
    return type(value) is str and re.fullmatch(r"[0-9a-f]{" + str(length) + "}", value) is not None


def _remaining() -> float:
    deadline = control_deadline.get()
    remaining = 3.0 if deadline is None else min(3.0, deadline - time.monotonic())
    _require(remaining > 0, "deadline_exceeded")
    return remaining


def _path(value: Any) -> Path:
    _require(type(value) is str and "\x00" not in value, "invalid_path")
    path = Path(value)
    _require(path.is_absolute() and path.resolve() == path, "unsafe_path")
    return path


def _read_file(path: Path, *, private: bool, limit: int = 4 * 1024 * 1024) -> bytes:
    """Open once with O_NOFOLLOW; reject links, replacement and partial reads."""
    _remaining()
    try:
        _path(str(path))
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            _require(
                stat.S_ISREG(before.st_mode)
                and before.st_nlink == 1
                and before.st_uid == os.getuid()
                and (
                    stat.S_IMODE(before.st_mode) == 0o600 if private else not before.st_mode & 0o022
                )
                and 0 < before.st_size <= limit,
                "unsafe_file",
            )
            raw = handle.read(limit + 1)
            after = os.fstat(handle.fileno())
            _require(
                before == after and len(raw) == before.st_size and path.lstat() == after,
                "file_changed",
            )
    except OSError as exc:
        raise ProviderDenied("file_unavailable") from exc
    _remaining()
    return raw


def _artifact(pin: Any) -> dict[str, Any]:
    pin = _keys(pin, {"path", "sha256"})
    _require(_hex(pin["sha256"]), "invalid_pin")
    raw = _read_file(_path(pin["path"]), private=True)
    _require(hashlib.sha256(raw).hexdigest() == pin["sha256"], "artifact_changed")
    return decode(raw, exact=False, limit=4 * 1024 * 1024)


def store_identity(path: Path) -> dict[str, Any]:
    """Observe an existing private DB identity without opening/migrating it."""
    try:
        _path(str(path))
        info = path.lstat()
    except OSError as exc:
        raise ProviderDenied("database_unavailable") from exc
    _require(
        stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o600,
        "unsafe_database",
    )
    return {
        "path_sha256": hashlib.sha256(str(path).encode()).hexdigest(),
        "device": info.st_dev,
        "inode": info.st_ino,
        "uid": info.st_uid,
    }


@contextmanager
def _database(pin: dict[str, Any]) -> Iterator[sqlite3.Connection]:
    path = _path(pin["path"])
    _require(store_identity(path) == pin["identity"], "database_replaced")
    # mode=ro never creates a database or changes application data. Existing
    # WAL is read normally; immutable=1 would incorrectly ignore live WAL data.
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=_remaining())
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("BEGIN")
        yield connection
        _require(store_identity(path) == pin["identity"], "database_replaced")
        _remaining()
    except sqlite3.Error as exc:
        raise ProviderDenied("database_read_denied") from exc
    finally:
        connection.close()


def _command(
    arguments: list[str], *, limit: int = 4 * 1024 * 1024
) -> subprocess.CompletedProcess[bytes]:
    try:
        # All call sites use fixed binaries/verbs and validated target/source pins.
        result = subprocess.run(  # nosec B603
            arguments,
            capture_output=True,
            timeout=_remaining(),
            check=False,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": os.environ.get("HOME", ""),
                "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
                "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.getuid()}/bus",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_OPTIONAL_LOCKS": "0",
                "LC_ALL": "C",
            },
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProviderDenied("observation_unavailable") from exc
    _require(len(result.stdout) <= limit and len(result.stderr) <= limit, "observation_oversize")
    _remaining()
    return result


def _observer_generation() -> dict[str, Any]:
    properties = ("LoadState", "ActiveState", "MainPID", "InvocationID", "ControlGroup")
    result = _command(
        [
            "/usr/bin/systemctl",
            "--user",
            "show",
            "--no-pager",
            *["--property=" + field for field in properties],
            UNIT,
        ],
        limit=8192,
    )
    _require(result.returncode == 0, "observer_unavailable")
    values = dict(line.split("=", 1) for line in result.stdout.decode().splitlines() if "=" in line)
    identity = gpu_scheduler._read_process_identity(os.getpid())
    _require(
        set(values) == set(properties)
        and values["LoadState"] == "loaded"
        and values["ActiveState"] == "active"
        and values["MainPID"] == str(os.getpid())
        and _hex(values["InvocationID"], 32)
        and identity is not None,
        "observer_not_current_process",
    )
    if identity is None:
        raise ProviderDenied("observer_not_current_process")
    group = gpu_scheduler._process_cgroup(os.getpid())
    _require(
        values["ControlGroup"] == group and group.endswith("/" + UNIT),
        "observer_cgroup_mismatch",
    )
    return {
        "uid": os.getuid(),
        "pid": identity.pid,
        "start_ticks": identity.start_ticks,
        "boot_id": identity.boot_id,
        "unit": UNIT,
        "invocation_id": values["InvocationID"],
        "control_group": group,
    }


class ReadOnlyObservationProviders:
    """Configuration-pinned readers for NoAdmissionObservationStore callbacks.

    This object never instantiates ControlStore and never writes either DB.
    ``inspect`` can run from a shell; actual callbacks cannot. Caller-supplied
    proof or desired absence booleans are not accepted by these methods.
    """

    def __init__(self, config_path: Path, expected_config_sha256: str) -> None:
        _require(_hex(expected_config_sha256), "invalid_config_pin")
        self.config_path = _path(str(config_path))
        self.config_sha256 = expected_config_sha256
        self.config = self._config()
        self._validate_config()

    @property
    def target(self) -> dict[str, str]:
        """Return a copy of the sole reviewed original target."""
        return dict(self.config["target"])

    @property
    def canonical_database(self) -> Path:
        """Return the reviewed existing canonical database path."""
        return _path(self.config["canonical_database"]["path"])

    @property
    def aos_database(self) -> Path:
        """Return the independently owned AOS database path for readonly access."""
        return _path(self.config["aos_database"]["path"])

    def _config(self) -> dict[str, Any]:
        raw = _read_file(self.config_path, private=True)
        _require(hashlib.sha256(raw).hexdigest() == self.config_sha256, "config_changed")
        return _keys(decode(raw, exact=False), _CONFIG_KEYS)

    def _validate_config(self) -> None:
        cfg = self.config
        _require(
            cfg["schema"] == CONFIG_SCHEMA and type(cfg["version"]) is int and cfg["version"] == 1,
            "invalid_config",
        )
        target = _keys(
            cfg["target"],
            {
                "request_id",
                "request_sha256",
                "original_caller_generation_sha256",
            },
        )
        _require(
            all(_hex(value, 32 if key == "request_id" else 64) for key, value in target.items()),
            "invalid_target",
        )
        for name in ("aos_database", "canonical_database"):
            pin = _keys(
                cfg[name],
                {"path", "identity"} | ({"schema_version"} if name == "aos_database" else set()),
            )
            _path(pin["path"])
            _keys(pin["identity"], {"path_sha256", "device", "inode", "uid"})
        _require(
            type(cfg["aos_database"]["schema_version"]) is int
            and cfg["aos_database"]["schema_version"] == 27,
            "unsupported_aos_schema",
        )
        _keys(
            cfg["original"],
            {"admission_record_sha256", "intent_binding_sha256", "source_fingerprints"},
        )
        _keys(cfg["observer"], {"unit", "source_sha256"})
        _require(
            cfg["observer"]["unit"] == UNIT and cfg["observation_schema_sha256"] == schema_hash(),
            "unsupported_observer",
        )
        _require(
            type(cfg["sources"]) is list and 1 <= len(cfg["sources"]) <= 256, "source_inventory"
        )
        paths = []
        for pin in cfg["sources"]:
            _keys(pin, {"path", "sha256"})
            paths.append(str(_path(pin["path"])))
            _require(_hex(pin["sha256"]), "source_pin")
        _require(
            len(set(paths)) == len(paths) and str(Path(__file__).resolve()) in paths,
            "source_inventory",
        )
        _require(
            digest(cfg["sources"]) == cfg["observer"]["source_sha256"], "source_manifest_mismatch"
        )
        _path(cfg["historical_repository"])

    def _guard(self, target: Mapping[str, str] | None = None) -> None:
        _require(self._config() == self.config, "config_changed")
        if target is not None:
            _require(dict(target) == self.config["target"], "target_mismatch")
        for pin in self.config["sources"]:
            raw = _read_file(_path(pin["path"]), private=False)
            _require(hashlib.sha256(raw).hexdigest() == pin["sha256"], "source_changed")
        for name in ("aos_database", "canonical_database"):
            pin = self.config[name]
            _require(store_identity(_path(pin["path"])) == pin["identity"], "database_replaced")

    def _scope(self, connection: sqlite3.Connection) -> dict[str, Any]:
        scope = self.config["cleanup_scope"]
        _keys(
            scope,
            {
                "store",
                "session_id",
                "runtime_id",
                "lease_id",
                "original_generation",
                "current_generation",
                "current_status",
                "current_owner",
                "authorization_context_sha256",
                "purpose",
            },
        )
        row = connection.execute(
            "SELECT * FROM desktop_sessions WHERE session_id=?", (scope["session_id"],)
        ).fetchone()
        if row is None:
            raise ProviderDenied("session_missing")
        expected = {
            "runtime_id": scope["runtime_id"],
            "lease_id": scope["lease_id"],
            "generation": scope["current_generation"],
            "status": "stopped",
            "owner": "PAUSED",
        }
        _require(
            all(row[key] == value for key, value in expected.items())
            and scope["current_status"] == "stopped"
            and scope["current_owner"] == "PAUSED"
            and scope["current_generation"] > scope["original_generation"]
            and scope["purpose"] == PURPOSE
            and scope["store"] == self.config["aos_database"]["identity"],
            "cleanup_scope_changed",
        )
        context = _artifact(self.config["cleanup_authority"])
        expected_context = {
            "schema": "aos-scientist-no-admission-cleanup-authority.v1",
            "version": 1,
            "target": self.config["target"],
            "cleanup_scope_without_context": {
                key: value for key, value in scope.items() if key != "authorization_context_sha256"
            },
            "observer_unit": UNIT,
            "purpose": PURPOSE,
            "inference_allowed": False,
        }
        _require(
            canonical(context) == canonical(expected_context)
            and digest(context) == scope["authorization_context_sha256"],
            "cleanup_authority_mismatch",
        )
        return dict(scope)

    def _original(self, *, closure_read: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
        cfg, target = self.config, self.config["target"]
        with _database(cfg["aos_database"]) as connection:
            version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            accepted = {27, 28} if closure_read else {cfg["aos_database"]["schema_version"]}
            _require(version in accepted, "aos_schema_changed")
            row = connection.execute(
                "SELECT * FROM scientist_turn_intents WHERE request_id=?", (target["request_id"],)
            ).fetchone()
            history = connection.execute(
                "SELECT * FROM scientist_admission_history WHERE request_id=?",
                (target["request_id"],),
            ).fetchone()
            if row is None or history is None:
                raise ProviderDenied("original_missing")
            _require(row["state"] == "pending" and row["receipt_json"] is None, "original_resolved")
            request = decode(row["request_json"].encode())
            intent = decode(row["binding_json"].encode(), exact=False)
            record = decode(history["record_json"].encode(), exact=False)
            binding = record["admission_binding"]
            _require(
                target["request_id"] == request["request_id"] == record["request_id"]
                and digest(request)
                == target["request_sha256"]
                == row["request_sha256"]
                == history["request_sha256"]
                and digest(record)
                == history["record_sha256"]
                == cfg["original"]["admission_record_sha256"]
                and digest(intent)
                == record["intent_binding_sha256"]
                == history["intent_binding_sha256"]
                == cfg["original"]["intent_binding_sha256"]
                and digest(binding)
                == record["admission_binding_sha256"]
                == history["admission_binding_sha256"]
                and digest(binding["caller_generation"])
                == target["original_caller_generation_sha256"]
                and binding["source_fingerprints"] == cfg["original"]["source_fingerprints"]
                and row["session_id"]
                == history["session_id"]
                == record["session_id"]
                == intent["session_id"]
                # AOS BrokerPeer retains its six authenticated socket fields.
                # The independently pinned admission binding retains the unit;
                # do not invent a unit field in the immutable peer record.
                and decode(row["broker_peer_json"].encode())
                == {
                    key: value
                    for key, value in binding["server_generation"].items()
                    if key != "unit"
                },
                "original_mismatch",
            )
            scope = self._scope(connection)
            _require(
                intent["session_id"] == scope["session_id"]
                and intent["runtime_id"] == scope["runtime_id"]
                and intent["generation"] == scope["original_generation"]
                and intent["owner"] == "AGENT"
                and intent["lease_id"] != scope["lease_id"],
                "original_scope_mismatch",
            )
        capture = {
            key: record[key]
            for key in ("admission_binding", "capability_sha256", "capability_freshness")
        }
        original = {
            "request_canonical": canonical(request).decode(),
            "admission_record": record,
            "admission_record_sha256": digest(record),
            "admission_binding_sha256": digest(binding),
            "admission_capture_sha256": digest(capture),
            "intent_binding": intent,
            "intent_binding_sha256": digest(intent),
            "broker_generation": binding["server_generation"],
            "broker_generation_sha256": digest(binding["server_generation"]),
            "store": cfg["aos_database"]["identity"],
        }
        return original, scope

    def inspect(self) -> dict[str, Any]:
        """Read/check retained preparation only; shell use creates no authority."""
        self._guard()
        original, scope = self._original()
        self._historical_ordering(original)
        _require(self._original() == (original, scope), "retained_scope_changed")
        self._guard()
        return {
            "runtime_authorized": False,
            "target": dict(self.config["target"]),
            "original_sha256": digest(original),
            "cleanup_scope_sha256": digest(scope),
            "config_sha256": self.config_sha256,
        }

    def _closure_schema(self, connection: sqlite3.Connection) -> None:
        pins = [
            pin
            for pin in self.config["sources"]
            if pin["path"].endswith("/database/migrations/0028_scientist_no_admission_closures.sql")
        ]
        _require(len(pins) == 1, "closure_schema_source_missing")
        text = _read_file(_path(pins[0]["path"]), private=False).decode()
        statement = ""
        reviewed = {}
        for line in text.splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                sql = statement.strip().rstrip(";")
                match = re.match(r"CREATE (?:TABLE|TRIGGER) (scientist_[a-z_]+)\b", sql)
                if match:
                    reviewed[match[1]] = sql
                statement = ""
        _require(
            "scientist_no_admission_closures" in reviewed and len(reviewed) >= 7,
            "closure_schema_source_incomplete",
        )
        for name, expected in reviewed.items():
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name=?", (name,)
            ).fetchone()
            _require(row is not None and row[0] == expected, "closure_schema_changed")

    def read_closure(self, observation_sha256: str) -> bool:
        """Read matching immutable AOS closure; never resolves/migrates its DB."""
        _require(_hex(observation_sha256), "invalid_observation_hash")
        self._guard()
        original, scope = self._original(closure_read=True)
        with _database(self.config["aos_database"]) as connection:
            version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            exists = connection.execute(
                "SELECT type FROM sqlite_master WHERE name='scientist_no_admission_closures'"
            ).fetchone()
            if version == 27 and exists is None:
                return False
            _require(
                version == 28 and exists is not None and exists[0] == "table",
                "closure_schema_changed",
            )
            self._closure_schema(connection)
            row = connection.execute(
                "SELECT * FROM scientist_no_admission_closures WHERE request_id=?",
                (self.target["request_id"],),
            ).fetchone()
            if row is None:
                return False
            intent = connection.execute(
                "SELECT * FROM scientist_turn_intents WHERE request_id=?",
                (self.target["request_id"],),
            ).fetchone()
            if intent is None:
                raise ProviderDenied("original_missing")
            value = decode(row["observation_json"].encode())
            _require(
                row["observation_sha256"] == observation_sha256 == value["observation_sha256"]
                and digest(
                    {key: item for key, item in value.items() if key != "observation_sha256"}
                )
                == observation_sha256
                and row["schema_sha256"] == self.config["observation_schema_sha256"]
                and row["request_sha256"] == self.target["request_sha256"]
                and row["session_id"] == scope["session_id"]
                and row["admission_record_sha256"] == original["admission_record_sha256"]
                and json.loads(row["intent_snapshot_json"]) == dict(intent)
                and json.loads(row["recovery_scope_json"]) == scope
                and value["target"] == self.target
                and value["original"] == original
                and value["cleanup_scope"] == scope
                and value["observer"]["source_sha256"] == self.config["observer"]["source_sha256"]
                and value["observer"]["config_sha256"] == self.config_sha256,
                "closure_conflict",
            )
        with _database(self.config["canonical_database"]) as connection:
            row = connection.execute(
                "SELECT observation_json,observation_sha256 FROM aos_no_admission_observations "
                "WHERE request_id=?",
                (self.target["request_id"],),
            ).fetchone()
            _require(
                row is not None
                and row["observation_sha256"] == observation_sha256
                and row["observation_json"].encode() == canonical(value),
                "closure_tombstone_mismatch",
            )
        _require(value["observer"]["generation"] == _observer_generation(), "observer_changed")
        validate_observation(
            canonical(value),
            expected=ObservationExpectations(
                schema_sha256=self.config["observation_schema_sha256"],
                original=canonical(original),
                observer=canonical(value["observer"]),
                canonical=canonical(value["canonical"]),
                physical=canonical(value["physical"]),
                cleanup_scope=canonical(scope),
            ),
            now_boottime_us=int(gpu_scheduler.boottime() * 1_000_000),
            current_boot_id=gpu_scheduler._boot_id(),
        )
        _require(self._original(closure_read=True) == (original, scope), "retained_scope_changed")
        self._guard()
        return True

    def observer_authority(self, target: Mapping[str, str]) -> ObserverAuthority:
        """Authenticate this process and the independently reviewed cleanup scope."""
        self._guard(target)
        generation = _observer_generation()
        original, scope = self._original()
        capability = {
            "feature": "no-admission-observation.v1",
            "version": 1,
            "schema_sha256": self.config["observation_schema_sha256"],
            "generation_sha256": digest(generation),
            "source_sha256": self.config["observer"]["source_sha256"],
            "config_sha256": self.config_sha256,
            "purpose": PURPOSE,
        }
        observer = {
            "generation": generation,
            "generation_sha256": digest(generation),
            "source_sha256": capability["source_sha256"],
            "config_sha256": self.config_sha256,
            "capability": capability,
            "capability_sha256": digest(capability),
            "purpose": PURPOSE,
        }
        _require(_observer_generation() == generation, "observer_changed")
        _require(self._original() == (original, scope), "retained_scope_changed")
        self._guard(target)
        return ObserverAuthority(canonical(observer), canonical(scope))

    def original_reader(self, target: Mapping[str, str]) -> bytes:
        """Read exact retained DB bytes under current authenticated observer scope."""
        self._guard(target)
        _observer_generation()
        original, scope = self._original()
        _require(self._original() == (original, scope), "retained_scope_changed")
        self._guard(target)
        return canonical(original)

    def _historical_ordering(self, original: dict[str, Any]) -> None:
        cfg = self.config
        receipt = _artifact(cfg["historical_receipt"])
        binding = original["admission_record"]["admission_binding"]
        _require(
            receipt["reviewed_bindings"][binding["profile_id"]] == binding,
            "historical_capture_mismatch",
        )
        review = _artifact(cfg["dispatch_review"])
        _keys(
            review,
            {
                "schema",
                "version",
                "target",
                "historical_source_fingerprints",
                "historical_receipt_sha256",
                "canonical_store",
                "ordering",
                "deferred_storage",
                "quarantine_storage",
                "reviewed_source_manifest_sha256",
                "historical_scientist_commit",
                "historical_source_files",
            },
            "dispatch_review_shape",
        )
        _require(
            review["schema"] == "aos-scientist-no-admission-dispatch-review.v1"
            and type(review["version"]) is int
            and review["version"] == 1
            and review["target"] == cfg["target"]
            and review["historical_source_fingerprints"] == binding["source_fingerprints"]
            and review["historical_receipt_sha256"] == cfg["historical_receipt"]["sha256"]
            and review["canonical_store"] == cfg["canonical_database"]["identity"]
            and review["ordering"] == "canonical_intent_before_queue_before_child"
            and review["deferred_storage"] == review["quarantine_storage"] == "canonical_only"
            and review["reviewed_source_manifest_sha256"] == digest(cfg["sources"])
            and _hex(review["historical_scientist_commit"], 40),
            "dispatch_review_mismatch",
        )
        files = review["historical_source_files"]
        _require(
            type(files) is list and len(_HISTORICAL_REQUIRED) <= len(files) <= 64,
            "historical_source_inventory",
        )
        paths = set()
        for pin in files:
            _keys(pin, {"path", "sha256"})
            path = PurePosixPath(pin["path"])
            _require(
                not path.is_absolute()
                and ".." not in path.parts
                and str(path) == pin["path"]
                and pin["path"].startswith("lab/")
                and _hex(pin["sha256"]),
                "historical_source_pin",
            )
            _require(pin["path"] not in paths, "historical_source_duplicate")
            paths.add(pin["path"])
            retained = receipt["snapshot"]["source_inputs"].get(
                str(_path(cfg["historical_repository"]) / path)
            )
            _require(
                type(retained) is dict and retained["sha256"] == pin["sha256"],
                "historical_source_not_retained",
            )
            result = _command(
                [
                    "/usr/bin/git",
                    "--no-pager",
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "core.pager=cat",
                    "-C",
                    cfg["historical_repository"],
                    "show",
                    "--no-ext-diff",
                    "--no-textconv",
                    review["historical_scientist_commit"] + ":" + str(path),
                ]
            )
            _require(
                result.returncode == 0
                and hashlib.sha256(result.stdout).hexdigest() == pin["sha256"],
                "historical_source_unavailable",
            )
        _require(_HISTORICAL_REQUIRED <= paths, "historical_ordering_incomplete")

    def _canonical_absent(self) -> None:
        with _database(self.config["canonical_database"]) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            _require(
                tables - {"sqlite_sequence", TABLE} == set(REQUEST_SURFACES),
                "canonical_inventory_unknown",
            )
            for query in _SQL_ABSENCE:
                _require(
                    connection.execute(query, (self.config["target"]["request_id"],)).fetchone()
                    is None,
                    "canonical_provenance_present",
                )

    @staticmethod
    def _gone(generation: dict[str, Any]) -> None:
        _require(generation["boot_id"] == gpu_scheduler._boot_id(), "old_boot_not_proven")
        identity = gpu_scheduler._read_process_identity(generation["pid"])
        _require(identity is None, "old_pid_present_or_reused")
        group = generation["control_group"]
        _require(
            type(group) is str
            and group.startswith("/")
            and ".." not in PurePosixPath(group).parts
            and str(PurePosixPath(group)) == group,
            "old_group_ambiguous",
        )
        try:
            Path("/sys/fs/cgroup" + group).lstat()
        except FileNotFoundError:
            pass
        else:
            raise ProviderDenied("old_cgroup_present")
        # A recreated original unit is not adopted, even if its PID differs.
        owner = "aos" if generation["unit"].startswith("swapp-aos-") else "lab"
        shown = gpu_scheduler._systemctl_show(generation["unit"], owner=owner, timeout=_remaining())
        _require(
            shown["MainPID"] == "0"
            and shown["ActiveState"] in {"inactive", "failed"}
            and not shown["ControlGroup"],
            "old_unit_can_dispatch",
        )

    def physical_reader(self, target: Mapping[str, str], original: bytes) -> PhysicalEvidence:
        """Prove canonical and physical absence under reviewed original dispatch ordering."""
        self._guard(target)
        observer = _observer_generation()
        retained, scope = self._original()
        _require(canonical(retained) == original, "original_preimage_mismatch")
        self._historical_ordering(retained)
        self._canonical_absent()
        binding = retained["admission_record"]["admission_binding"]
        caller, broker = binding["caller_generation"], binding["server_generation"]
        cleanup = _artifact(self.config["cleanup_evidence"])
        container = cleanup.get("container_id")
        if not isinstance(container, str) or not _hex(container):
            raise ProviderDenied("container_identity_missing")
        for key, generation in (("original_native", caller), ("original_broker", broker)):
            proof = cleanup.get(key, {})
            _require(
                proof.get("MainPID") == str(generation["pid"])
                and proof.get("InvocationID") == generation["invocation_id"]
                and proof.get("ControlGroup") == generation["control_group"],
                "cleanup_original_mismatch",
            )
            self._gone(generation)
        # Fixed local daemon endpoint, never a caller/env selected Docker context.
        result = _command(
            [
                "/usr/bin/docker",
                "--host",
                "unix:///var/run/docker.sock",
                "container",
                "inspect",
                container,
            ],
            limit=131072,
        )
        _require(
            result.returncode == 1
            and result.stderr.decode().strip()
            in {
                "Error: No such container: " + container,
                "Error: No such object: " + container,
                "Error response from daemon: No such container: " + container,
            },
            "original_container_not_proven_absent",
        )
        self._canonical_absent()
        self._gone(caller)
        self._gone(broker)
        _require(_observer_generation() == observer, "observer_changed")
        _require(self._original() == (retained, scope), "retained_scope_changed")
        self._guard(target)
        # Empty inventories follow the reviewed single-canonical dispatch ordering
        # AND current absence at every persistence/physical dispatch surface.
        physical = {
            "target": dict(target),
            "caller_generation": caller,
            "caller_generation_sha256": digest(caller),
            "broker_generation": broker,
            "broker_generation_sha256": digest(broker),
            "children": [],
            "caller_absent": True,
            "broker_absent": True,
            "original_cgroups_absent": True,
            "children_absent": True,
            "provenance_complete": True,
            "late_dispatch_fenced": True,
        }
        physical["proof_sha256"] = digest(physical)
        dispatch = {
            "target": dict(target),
            "source_fingerprints": binding["source_fingerprints"],
            "caller_generation_sha256": digest(caller),
            "broker_generation_sha256": digest(broker),
            "provenance_complete": True,
            "deferred_execution": [],
            "quarantined_allocations": [],
        }
        return PhysicalEvidence(canonical(physical), canonical(dispatch))
