"""Read-only retained-proof recovery; never renew observation or GPU authority.

Private independently pinned configuration authorizes one finite recovery
window. Current sources and the original producer's private source snapshot
have separate inventories. Inspection grants no authority; context generation
requires this process to be the distinct fixed recovery service MainPID.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from lab.llm import aos_no_admission_observation as observation
from lab.llm import aos_no_admission_providers as readonly
from lab.llm import aos_no_admission_recovery as recovery
from lab.llm import gpu_scheduler
from lab.llm.aos_gpu_control_store import control_deadline
from lab.llm.aos_no_admission_providers import (
    _database,
    _hex,
    _keys,
    _path,
    _read_file,
    _remaining,
    _require,
)
from lab.llm.aos_no_admission_store import NoAdmissionObservationStore

UNIT = "swapp-scientist-no-admission-recovery.service"
CONFIG_SCHEMA = "aos-scientist-no-admission-recovery-provider.v1"
AUTHORIZATION_SCHEMA = "aos-scientist-no-admission-recovery-authorization.v1"
_CONFIG_KEYS = {
    "schema",
    "version",
    "target",
    "current_sources",
    "authorization",
    "retained_config",
    "retained_observation",
    "retained_sources",
    "cleanup_scope",
    "canonical_database",
    "aos_database",
    "cleanup_evidence",
    "recovery_schema_sha256",
}
_DENIED_PERMISSIONS = {
    "inference_allowed": False,
    "gpu_release_allowed": False,
    "observation_reissue_allowed": False,
}


@contextmanager
def _bounded() -> Iterator[None]:
    previous = control_deadline.get()
    deadline = time.monotonic() + 5.0
    token = control_deadline.set(min(previous, deadline) if previous is not None else deadline)
    try:
        _remaining()
        yield
        _remaining()
    finally:
        control_deadline.reset(token)


def schema_fingerprint(connection: sqlite3.Connection) -> str:
    """Fingerprint the complete SQLite catalog, including indexes and triggers."""
    return observation.digest(
        [
            list(row)
            for row in connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
            )
        ]
    )


def canonical_schema_fingerprint(connection: sqlite3.Connection) -> str:
    """Preserve the original canonical table/trigger schema pin convention."""
    return observation.digest(
        [
            {"name": row[0], "type": row[1], "sql": row[2]}
            for row in connection.execute(
                "SELECT name,type,sql FROM sqlite_master "
                "WHERE type IN ('table','trigger') AND name != 'sqlite_sequence' ORDER BY name"
            )
        ]
    )


def _raw_pin(pin: Any, *, private: bool = True) -> bytes:
    _keys(pin, {"path", "sha256"})
    _require(_hex(pin["sha256"]), "invalid_pin")
    raw = _read_file(_path(pin["path"]), private=private)
    _require(hashlib.sha256(raw).hexdigest() == pin["sha256"], "artifact_changed")
    return raw


def _generation() -> dict[str, Any]:
    properties = {"LoadState", "ActiveState", "MainPID", "InvocationID", "ControlGroup"}
    result = readonly._command(
        [
            "/usr/bin/systemctl",
            "--user",
            "show",
            "--no-pager",
            *["--property=" + key for key in sorted(properties)],
            UNIT,
        ],
        limit=8192,
    )
    _require(result.returncode == 0, "recovery_unit_unavailable")
    pairs = [line.split("=", 1) for line in result.stdout.decode().splitlines() if "=" in line]
    values = dict(pairs)
    identity = gpu_scheduler._read_process_identity(os.getpid())
    _require(
        len(pairs) == len(properties)
        and set(values) == properties
        and values["LoadState"] == "loaded"
        and values["ActiveState"] == "active"
        and values["MainPID"] == str(os.getpid())
        and _hex(values["InvocationID"], 32)
        and identity is not None,
        "recovery_not_current_process",
    )
    if identity is None:
        raise readonly.ProviderDenied("recovery_not_current_process")
    group = gpu_scheduler._process_cgroup(os.getpid())
    _require(
        values["ControlGroup"] == group and group.endswith("/" + UNIT),
        "recovery_cgroup_mismatch",
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


def _old_observer_gone(generation: dict[str, Any]) -> None:
    """Read only the fixed historical CPU observer; confer no GPU-unit authority."""
    _remaining()
    group = generation["control_group"]
    _require(
        generation["unit"] == readonly.UNIT
        and generation["uid"] == os.getuid()
        and generation["boot_id"] == gpu_scheduler._boot_id()
        and type(generation["pid"]) is int
        and generation["pid"] > 0
        and type(generation["start_ticks"]) is int
        and generation["start_ticks"] > 0
        and _hex(generation["invocation_id"], 32)
        and isinstance(group, str)
        and group.startswith("/")
        and group.endswith("/" + readonly.UNIT)
        and ".." not in PurePosixPath(group).parts
        and str(PurePosixPath(group)) == group
        and not group.startswith("//")
        and "\x00" not in group
        and "\n" not in group,
        "old_observer_generation_mismatch",
    )
    try:
        identity = gpu_scheduler._read_process_identity(generation["pid"])
    except (OSError, ValueError, IndexError) as exc:
        raise readonly.ProviderDenied("old_observer_identity_unknown") from exc
    _require(identity is None, "old_pid_present_or_reused")
    try:
        Path("/sys/fs/cgroup" + group).lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise readonly.ProviderDenied("old_observer_cgroup_unknown") from exc
    else:
        raise readonly.ProviderDenied("old_cgroup_present")
    properties = {"Id", "LoadState", "ActiveState", "MainPID", "InvocationID", "ControlGroup"}
    result = readonly._command(
        [
            "/usr/bin/systemctl",
            "--user",
            "show",
            "--no-pager",
            *["--property=" + key for key in sorted(properties)],
            readonly.UNIT,
        ],
        limit=8192,
    )
    _require(result.returncode == 0 and not result.stderr.strip(), "old_observer_unit_unknown")
    try:
        lines = result.stdout.decode().splitlines()
        pairs = [line.split("=", 1) for line in lines]
        values = dict(pairs)
    except (UnicodeError, ValueError) as exc:
        raise readonly.ProviderDenied("old_observer_unit_unknown") from exc
    _require(
        len(pairs) == len(properties)
        and set(values) == properties
        and values["Id"] == readonly.UNIT
        and values["MainPID"] == "0"
        and not values["ControlGroup"]
        and (
            (
                values["LoadState"] == "not-found"
                and values["ActiveState"] == "inactive"
                and values["InvocationID"] == ""
            )
            or (
                values["LoadState"] == "loaded"
                and values["ActiveState"] in {"inactive", "failed"}
                and values["InvocationID"] == generation["invocation_id"]
            )
        ),
        "old_observer_unit_not_absent",
    )
    _remaining()


class ReadOnlyRecoveryProviders:
    """Independently verify one original proof and one finite cleanup authority.

    All database connections use mode=ro/query_only. No callbacks, alternate
    allocator, write connection, migration or model runtime are accepted.
    """

    def __init__(self, config_path: Path, expected_config_sha256: str) -> None:
        with _bounded():
            _require(_hex(expected_config_sha256), "invalid_config_pin")
            self.config_path = _path(str(config_path))
            self.config_sha256 = expected_config_sha256
            self.config = self._config()
            self._issued: bytes | None = None

    def _config(self) -> dict[str, Any]:
        raw = _raw_pin({"path": str(self.config_path), "sha256": self.config_sha256})
        cfg = _keys(observation.decode(raw, exact=False), _CONFIG_KEYS)
        _require(
            cfg["schema"] == CONFIG_SCHEMA
            and type(cfg["version"]) is int
            and cfg["version"] == 1
            and cfg["recovery_schema_sha256"] == recovery.schema_hash(),
            "invalid_recovery_config",
        )
        return cfg

    @property
    def target(self) -> dict[str, str]:
        """Return the exact pinned historical request identifiers."""
        return dict(self.config["target"])

    @property
    def aos_database(self) -> Path:
        """Return the existing AOS database; this provider never writes it."""
        return _path(self.config["aos_database"]["path"])

    def _guard(self) -> None:
        _require(self._config() == self.config, "config_changed")
        pins = self.config["current_sources"]
        _require(type(pins) is list and 1 <= len(pins) <= 256, "source_inventory")
        paths = []
        for pin in pins:
            _raw_pin(pin, private=False)
            paths.append(pin["path"])
        required = {
            str(Path(__file__).resolve()),
            str(Path(readonly.__file__).resolve()),
            str(Path(recovery.__file__).resolve()),
            str(recovery.SCHEMA_PATH.resolve()),
            str(Path(observation.__file__).resolve()),
            str(observation.SCHEMA_PATH.resolve()),
            str(Path(gpu_scheduler.__file__).resolve()),
            str(Path(__file__).resolve().with_name("aos_no_admission_store.py")),
            str(Path(__file__).resolve().with_name("aos_gpu_control_store.py")),
            str(
                Path(__file__).resolve().parents[2]
                / "scripts/aos_no_admission_recovery_observer.py"
            ),
        }
        _require(len(paths) == len(set(paths)) and required <= set(paths), "source_inventory")
        for name in ("canonical_database", "aos_database"):
            pin = self.config[name]
            _keys(
                pin,
                {
                    "path",
                    "identity",
                    "schema_sha256" if name == "canonical_database" else "schemas",
                },
            )
            _require(
                readonly.store_identity(_path(pin["path"])) == pin["identity"],
                "database_replaced",
            )

    def _retained(self) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
        raw = _raw_pin(self.config["retained_observation"])
        old = observation.decode(raw)
        old_cfg = observation.decode(_raw_pin(self.config["retained_config"]), exact=False)
        _require(
            old["target"] == self.target == old_cfg["target"]
            and old["cleanup_scope"] == self.config["cleanup_scope"] == old_cfg["cleanup_scope"]
            and old["observer"]["config_sha256"] == self.config["retained_config"]["sha256"]
            and old["observer"]["source_sha256"]
            == observation.digest(old_cfg["sources"])
            == old_cfg["observer"]["source_sha256"]
            and old["canonical"]["store"]
            == self.config["canonical_database"]["identity"]
            == old_cfg["canonical_database"]["identity"]
            and old["original"]["store"]
            == self.config["aos_database"]["identity"]
            == old_cfg["aos_database"]["identity"],
            "retained_config_mismatch",
        )
        snapshots = self.config["retained_sources"]
        _require(
            type(snapshots) is list and len(snapshots) == len(old_cfg["sources"]),
            "retained_source_inventory",
        )
        mapped = {}
        for pin in snapshots:
            _keys(pin, {"original_path", "retained_path", "sha256"})
            _require(pin["original_path"] not in mapped, "retained_source_duplicate")
            _raw_pin({"path": pin["retained_path"], "sha256": pin["sha256"]})
            mapped[pin["original_path"]] = pin["sha256"]
        _require(
            mapped == {pin["path"]: pin["sha256"] for pin in old_cfg["sources"]}
            and len(mapped) == len(old_cfg["sources"]),
            "retained_source_inventory",
        )
        return raw, old, old_cfg

    def _canonical(self, raw: bytes, old: dict[str, Any]) -> None:
        pin = self.config["canonical_database"]
        with _database(pin) as connection:
            _require(
                canonical_schema_fingerprint(connection) == pin["schema_sha256"],
                "canonical_schema_changed",
            )
            NoAdmissionObservationStore._assert_absent(connection, self.target["request_id"])
            row = connection.execute(
                "SELECT * FROM aos_no_admission_observations WHERE request_id=?",
                (self.target["request_id"],),
            ).fetchone()
            _require(row is not None, "retained_tombstone_missing")
            if row is None:
                raise readonly.ProviderDenied("retained_tombstone_missing")
            dispatch = {
                "target": self.target,
                "source_fingerprints": old["original"]["admission_record"]["admission_binding"][
                    "source_fingerprints"
                ],
                "caller_generation_sha256": self.target["original_caller_generation_sha256"],
                "broker_generation_sha256": old["original"]["broker_generation_sha256"],
                "provenance_complete": True,
                "deferred_execution": [],
                "quarantined_allocations": [],
            }
            evidence = {
                key: old[key] for key in ("observer", "cleanup_scope", "original", "physical")
            }
            evidence["dispatch_provenance"] = dispatch
            _require(
                row["observation_json"].encode() == raw
                and row["observation_sha256"] == old["observation_sha256"]
                and row["request_sha256"] == self.target["request_sha256"]
                and row["original_caller_generation_sha256"]
                == self.target["original_caller_generation_sha256"]
                and row["admission_binding_sha256"] == old["original"]["admission_binding_sha256"]
                and row["dispatch_provenance_json"].encode() == observation.canonical(dispatch)
                and row["proof_sha256"] == observation.digest(evidence),
                "retained_tombstone_mismatch",
            )

    def _aos(self, old: dict[str, Any]) -> dict[str, Any]:
        cfg, original, scope = self.config, old["original"], old["cleanup_scope"]
        with _database(cfg["aos_database"]) as connection:
            version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            _require(
                str(version) in cfg["aos_database"]["schemas"]
                and schema_fingerprint(connection) == cfg["aos_database"]["schemas"][str(version)],
                "aos_schema_changed",
            )
            session = connection.execute(
                "SELECT * FROM desktop_sessions WHERE session_id=?", (scope["session_id"],)
            ).fetchone()
            row = connection.execute(
                "SELECT * FROM scientist_turn_intents WHERE request_id=?",
                (self.target["request_id"],),
            ).fetchone()
            history = connection.execute(
                "SELECT * FROM scientist_admission_history WHERE request_id=?",
                (self.target["request_id"],),
            ).fetchone()
            _require(
                session is not None and row is not None and history is not None, "original_missing"
            )
            if session is None or row is None or history is None:
                raise readonly.ProviderDenied("original_missing")
            _require(
                session["runtime_id"] == scope["runtime_id"]
                and session["lease_id"] == scope["lease_id"]
                and session["generation"] == scope["current_generation"] == 1
                and session["status"] == scope["current_status"] == "stopped"
                and session["owner"] == scope["current_owner"] == "PAUSED"
                and scope["original_generation"] == 0,
                "cleanup_scope_changed",
            )
            _require(
                row["state"] == "pending"
                and row["receipt_json"] is None
                and row["request_json"] == original["request_canonical"]
                and observation.decode(row["binding_json"].encode(), exact=False)
                == original["intent_binding"]
                and observation.decode(history["record_json"].encode(), exact=False)
                == original["admission_record"]
                and row["request_sha256"]
                == history["request_sha256"]
                == self.target["request_sha256"]
                and history["record_sha256"] == original["admission_record_sha256"]
                and history["admission_binding_sha256"] == original["admission_binding_sha256"]
                and history["intent_binding_sha256"] == original["intent_binding_sha256"]
                and row["session_id"] == history["session_id"] == scope["session_id"]
                and observation.decode(row["broker_peer_json"].encode())
                == {
                    key: value
                    for key, value in original["broker_generation"].items()
                    if key != "unit"
                },
                "original_mismatch",
            )
            return dict(row)

    def _physical(self, old: dict[str, Any], old_cfg: dict[str, Any]) -> None:
        generations = (
            old["physical"]["caller_generation"],
            old["original"]["broker_generation"],
            *(child["generation"] for child in old["physical"]["children"]),
        )
        for generation in generations:
            readonly.ReadOnlyObservationProviders._gone(generation)
        _old_observer_gone(old["observer"]["generation"])
        # Bind container identity to the original independently pinned producer
        # evidence, not a fresh arbitrary container name.
        cleanup_raw = _raw_pin(self.config["cleanup_evidence"])
        _require(
            hashlib.sha256(cleanup_raw).hexdigest() == old_cfg["cleanup_evidence"]["sha256"],
            "cleanup_evidence_mismatch",
        )
        cleanup = observation.decode(cleanup_raw, exact=False)
        container = cleanup.get("container_id")
        _require(_hex(container), "container_identity_missing")
        if not isinstance(container, str):
            raise readonly.ProviderDenied("container_identity_missing")
        for key, generation in zip(
            ("original_native", "original_broker"), generations[:2], strict=True
        ):
            prior = cleanup.get(key, {})
            _require(
                prior.get("MainPID") == str(generation["pid"])
                and prior.get("InvocationID") == generation["invocation_id"]
                and prior.get("ControlGroup") == generation["control_group"],
                "cleanup_original_mismatch",
            )
        result = readonly._command(
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
            and result.stdout.strip() in {b"", b"[]"}
            and result.stderr.decode().strip()
            in {
                "Error: No such container: " + container,
                "Error: No such object: " + container,
                "Error response from daemon: No such container: " + container,
            },
            "original_container_not_proven_absent",
        )
        for generation in generations:
            readonly.ReadOnlyObservationProviders._gone(generation)
        _old_observer_gone(old["observer"]["generation"])

    def _authority(self, old: dict[str, Any]) -> dict[str, Any]:
        authority = observation.decode(_raw_pin(self.config["authorization"]), exact=False)
        _keys(
            authority,
            {
                "schema",
                "version",
                "target",
                "recovery_request_id",
                "unit",
                "purpose",
                "source_sha256",
                "cleanup_scope_sha256",
                "observation_raw_sha256",
                "freshness",
                *_DENIED_PERMISSIONS,
            },
        )
        _require(
            authority["schema"] == AUTHORIZATION_SCHEMA
            and type(authority["version"]) is int
            and authority["version"] == 1
            and authority["target"] == self.target
            and authority["unit"] == UNIT
            and authority["purpose"] == recovery.PURPOSE
            and authority["source_sha256"] == observation.digest(self.config["current_sources"])
            and authority["cleanup_scope_sha256"] == observation.digest(old["cleanup_scope"])
            and authority["observation_raw_sha256"] == self.config["retained_observation"]["sha256"]
            and all(authority[key] is value for key, value in _DENIED_PERMISSIONS.items()),
            "recovery_authorization_mismatch",
        )
        return authority

    def _validate(
        self, raw: bytes, old_raw: bytes, old: dict[str, Any], principal: dict[str, Any]
    ) -> None:
        recovery.validate_recovery(
            raw,
            retained_observation=old_raw,
            expected=recovery.RecoveryExpectations(
                schema_sha256=self.config["recovery_schema_sha256"],
                authority_sha256=hashlib.sha256(raw).hexdigest(),
                authority_raw_sha256=hashlib.sha256(raw).hexdigest(),
                observation_raw_sha256=self.config["retained_observation"]["sha256"],
                observation=observation.ObservationExpectations(
                    schema_sha256=recovery.OBSERVATION_SCHEMA_SHA256,
                    **{
                        key: observation.canonical(old[key])
                        for key in (
                            "original",
                            "observer",
                            "canonical",
                            "physical",
                            "cleanup_scope",
                        )
                    },
                ),
                canonical_schema_sha256=self.config["canonical_database"]["schema_sha256"],
                principal=observation.canonical(principal),
            ),
            now_boottime_us=int(gpu_scheduler.boottime() * 1_000_000),
            current_boot_id=gpu_scheduler._boot_id(),
        )

    def inspect(self) -> dict[str, Any]:
        """Check preparation without authenticating or creating an authority."""
        with _bounded():
            self._guard()
            raw, old, _old_cfg = self._retained()
            self._canonical(raw, old)
            self._aos(old)
            self._guard()
            return {
                "runtime_authorized": False,
                "target": self.target,
                "config_sha256": self.config_sha256,
            }

    def context(self) -> bytes:
        """Return one fixed-window context after current independent checks."""
        with _bounded():
            self._guard()
            generation = _generation()
            old_raw, old, old_cfg = self._retained()
            authority = self._authority(old)
            principal = {
                "generation": generation,
                "generation_sha256": observation.digest(generation),
                "source_sha256": observation.digest(self.config["current_sources"]),
                "config_sha256": self.config_sha256,
            }
            value = {
                "schema": recovery.NAME,
                "version": 1,
                "purpose": recovery.PURPOSE,
                "recovery_request_id": authority["recovery_request_id"],
                "target": self.target,
                "retained": {
                    "observation_raw_sha256": self.config["retained_observation"]["sha256"],
                    "observation_sha256": old["observation_sha256"],
                    "observation_schema_sha256": recovery.OBSERVATION_SCHEMA_SHA256,
                    "canonical_store": old["canonical"]["store"],
                    "canonical_schema_sha256": self.config["canonical_database"]["schema_sha256"],
                    "cleanup_scope_sha256": observation.digest(old["cleanup_scope"]),
                    "observer_generation_sha256": old["observer"]["generation_sha256"],
                    "producer_source_sha256": old["observer"]["source_sha256"],
                    "producer_config_sha256": old["observer"]["config_sha256"],
                },
                "principal": principal,
                "cleanup_scope": old["cleanup_scope"],
                "freshness": authority["freshness"],
                **_DENIED_PERMISSIONS,
            }
            raw = observation.canonical(value)
            self._validate(raw, old_raw, old, principal)
            before = self._aos(old)
            self._canonical(old_raw, old)
            self._physical(old, old_cfg)
            self._canonical(old_raw, old)
            _require(self._aos(old) == before, "retained_scope_changed")
            _require(_generation() == generation, "recovery_generation_changed")
            _require(self._retained() == (old_raw, old, old_cfg), "retained_changed")
            _require(self._authority(old) == authority, "recovery_authorization_changed")
            self._guard()
            self._validate(raw, old_raw, old, principal)
            _require(self._issued is None or self._issued == raw, "recovery_context_changed")
            self._issued = raw
            return raw

    def read_closure(self, context: bytes) -> bool:
        """Verify AOS's immutable closure links this exact context and old proof."""
        with _bounded():
            _require(
                self._issued is not None and context == self._issued, "unissued_recovery_context"
            )
            _require(self.context() == context, "recovery_context_changed")
            old_raw, old, _old_cfg = self._retained()
            intent = self._aos(old)
            with _database(self.config["aos_database"]) as connection:
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='scientist_no_admission_closures' "
                    "AND type='table'"
                ).fetchone()
                if exists is None:
                    return False
                row = connection.execute(
                    "SELECT * FROM scientist_no_admission_closures WHERE request_id=?",
                    (self.target["request_id"],),
                ).fetchone()
                if row is None:
                    return False
                value = observation.decode(context)
                _require(
                    row["observation_json"].encode() == old_raw
                    and row["observation_sha256"] == old["observation_sha256"]
                    and row["schema_sha256"] == recovery.OBSERVATION_SCHEMA_SHA256
                    and row["session_id"] == old["cleanup_scope"]["session_id"]
                    and row["request_sha256"] == self.target["request_sha256"]
                    and row["admission_record_sha256"] == old["original"]["admission_record_sha256"]
                    and observation.decode(row["intent_snapshot_json"].encode()) == intent
                    and observation.decode(row["recovery_scope_json"].encode())
                    == {
                        **old["cleanup_scope"],
                        "retained_recovery": value,
                    },
                    "recovery_closure_conflict",
                )
            _require(self.context() == context, "recovery_context_changed")
            return True
