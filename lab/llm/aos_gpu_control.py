"""Unagreed control-v1 candidate. Private policy denies admission by default.

This module grants no lease, never consumes caller drain claims, and exposes
only authenticated views of the single arbiter's durable control records.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from lab.llm import aos_evidence_transport as evidence_transport
from lab.llm import aos_retained_evidence_transport as retained_transport
from lab.llm.aos_gpu_broker import PROFILE_IDS, PeerGeneration
from lab.llm.aos_gpu_control_store import (
    CONTROL_REFRESH_SECONDS,
    TERMINAL_SCHEMA_HASH,
    AdmissionGrant,
    ControlAuthority,
    ControlStoreError,
    control_deadline,
    validate_admission_binding,
    validate_profile_pin,
)
from lab.llm.aos_physical_readback import verify_physical_readback
from lab.llm.native_runtime import GpuObserver, UnitManager

SCHEMA = "aos-scientist-control.v1"
POLICY_SCHEMA = "swapp-aos-control-policy.v1"
OPS = frozenset({"capability", "status", "cancel", "reconcile"})
ERROR_CODES = frozenset(
    {
        "invalid_frame",
        "unsupported_version",
        "unsupported_schema",
        "unauthorized",
        "stale_generation",
        "capability_mismatch",
        "capability_expired",
        "profile_mismatch",
        "deployment_mismatch",
        "request_conflict",
        "history_denied",
        "busy",
        "deadline_exceeded",
        "internal_unavailable",
    }
)
STATE_CODES = frozenset(
    {
        "unknown",
        "intent",
        "queued",
        "activating",
        "running",
        "result_ready",
        "cancel_pending",
        "draining",
        "quarantined",
        "completed",
        "canceled",
        "expired",
        "failed",
    }
)
REQUEST_FIELDS = frozenset(
    {
        "schema",
        "version",
        "op",
        "control_id",
        "expected_capability_sha256",
        "profile_id",
        "deployment_digest",
        "target",
    }
)
TARGET_FIELDS = frozenset({"request_id", "request_sha256", "original_peer_generation_sha256"})
REQUEST_LIMIT = 8192
RESPONSE_LIMIT = 128 * 1024
FRAME_SECONDS = 5.0
CALL_SECONDS = 10.0
CAPABILITY_SECONDS = float(CONTROL_REFRESH_SECONDS)
MAX_CAPABILITIES = 256
CONTROL_WORKERS = 2
CONTROL_BACKLOG = 8
INFER_SCHEMA_HASH = hashlib.sha256(
    b"aos-scientist-runtime.v1:1:version,op=infer,request_id,profile_id,deployment_digest,payload"
).hexdigest()
CONTROL_CONTRACT = {
    "schema": SCHEMA,
    "version": 1,
    "operations": sorted(OPS),
    "request_fields": sorted(REQUEST_FIELDS),
    "target_fields": sorted(TARGET_FIELDS),
    "additional_fields": False,
    "canonical_json": True,
    "single_newline": True,
    "integer_version_only": True,
    "request_bytes": REQUEST_LIMIT,
    "response_bytes": RESPONSE_LIMIT,
    "id_lowercase_hex_length": 32,
    "hash_lowercase_hex_length": 64,
    "profiles": sorted(PROFILE_IDS),
    "capability_target": (
        "null for infer; exact retained target for current authority or cleanup ACL"
    ),
    "retained_control_capacity": {
        "poll_seconds": CONTROL_REFRESH_SECONDS,
        "refresh_quota": "ceil((original_queue_seconds+original_total_seconds+30)/60)+2; maximum40",
        "separate_buckets": ["capability", "status", "reconcile"],
        "cancel_slots": 2,
        "cleanup_grants": 8,
        "exact_capability_retry": "original persisted response; expiry never renewed",
    },
    "capability_expected_hash": None,
    "cleanup_grant_schema": "aos-scientist-cleanup-grant.v1",
    "new_infer_output_contract": {
        "name": "aos-scientist-profile-output.v2",
        "version": 2,
        "fields": ["name", "version", "bundle_sha256"],
    },
    "response_fields": [
        "schema",
        "version",
        "control_id",
        "op",
        "ok",
        "capability_sha256",
        "data",
        "error",
    ],
    "state_fields": [
        "target",
        "state",
        "cancel_requested",
        "first_cancel_boottime",
        "terminal_receipt",
        "result",
    ],
    "error_fields": ["code", "retryable"],
    "errors": sorted(ERROR_CODES),
    "states": sorted(STATE_CODES),
    "terminal_schema": "aos-scientist-terminal.v1",
    "admission_binding_fields": [
        "server_generation",
        "caller_generation",
        "policy_sha256",
        "source_fingerprints",
        "profile_id",
        "profile_pin",
        "infer_schema",
        "control_schema",
        "terminal_schema",
    ],
    "history_schema": "aos-scientist-control-history.v1",
}
CONTROL_SCHEMA_HASH = hashlib.sha256(
    json.dumps(
        CONTROL_CONTRACT,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
).hexdigest()
HISTORY_SCHEMA_HASH = hashlib.sha256(
    b"aos-scientist-control-history.v1:target,state,original_peer_generation_sha256,"
    b"terminal_receipt_sha256,release_outcome"
).hexdigest()


class ControlError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _hex(value: object, length: int = 64) -> bool:
    return isinstance(value, str) and re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is not None


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ControlError("invalid_frame")
        result[key] = value
    return result


def strict_json(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ControlError("invalid_frame")),
        )
        if not isinstance(value, dict) or canonical(value) != raw:
            raise ControlError("invalid_frame")
        return value
    except (UnicodeError, ValueError, TypeError) as exc:
        raise ControlError("invalid_frame") from exc


def decode_request(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > REQUEST_LIMIT:
        raise ControlError("invalid_frame")
    value = strict_json(raw)
    if set(value) != REQUEST_FIELDS:
        raise ControlError("invalid_frame")
    if value["schema"] != SCHEMA:
        raise ControlError("unsupported_schema")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ControlError("unsupported_version")
    if (
        not isinstance(value["op"], str)
        or value["op"] not in OPS
        or not _hex(value["control_id"], 32)
    ):
        raise ControlError("invalid_frame")
    if not isinstance(value["profile_id"], str) or value["profile_id"] not in PROFILE_IDS:
        raise ControlError("profile_mismatch")
    if not _hex(value["deployment_digest"]):
        raise ControlError("deployment_mismatch")
    if value["op"] == "capability":
        if value["expected_capability_sha256"] is not None:
            raise ControlError("invalid_frame")
    if value["op"] != "capability" or value["target"] is not None:
        target = value["target"]
        if (
            (value["op"] != "capability" and not _hex(value["expected_capability_sha256"]))
            or not isinstance(target, dict)
            or set(target) != TARGET_FIELDS
            or not _hex(target["request_id"], 32)
            or not _hex(target["request_sha256"])
            or not _hex(target["original_peer_generation_sha256"])
        ):
            raise ControlError("invalid_frame")
    return value


def _private_policy(path: Path) -> tuple[dict[str, Any], str]:
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) & 0o077
        or before.st_size > 65536
    ):
        raise ControlError("unauthorized")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        actual = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (actual.st_dev, actual.st_ino):
            raise ControlError("unauthorized")
        raw = os.read(fd, 65537)
    finally:
        os.close(fd)
    if len(raw) > 65536:
        raise ControlError("invalid_frame")
    # Policy files may use normal formatting; duplicate keys remain forbidden.
    try:
        value = json.loads(raw, object_pairs_hook=_object)
    except (UnicodeError, ValueError) as exc:
        raise ControlError("invalid_frame") from exc
    if not isinstance(value, dict):
        raise ControlError("invalid_frame")
    return value, digest(value)


class ControlPolicy:
    """Immutable reviewed config; on-disk replacement revokes this instance."""

    def __init__(self, path: Path | None, *, profiles: Any, source_root: Path) -> None:
        self.path = path
        self.profiles = profiles
        self.source_root = source_root
        self.config: dict[str, Any] | None = None
        self.sha256 = digest({"schema": POLICY_SCHEMA, "enabled": False})
        if path is None:
            return
        config, fingerprint = _private_policy(path)
        required = {
            "schema",
            "enabled",
            "caller_unit",
            "control_socket",
            "history_reconcile",
            "source_files",
            "profile_pins",
            "infer_schema_sha256",
            "control_schema_sha256",
        }
        evidence_pins = {"evidence_schema_sha256", "evidence_transport_schema_sha256"}
        if (
            set(config)
            - ({"cleanup_targets", "retained_evidence_transport_schema_sha256"} | evidence_pins)
            != required
        ):
            raise ControlError("invalid_frame")
        if set(config) & evidence_pins and (
            not evidence_pins.issubset(config)
            or config["evidence_schema_sha256"] != evidence_transport.EVIDENCE_SCHEMA_SHA256
            or config["evidence_transport_schema_sha256"]
            != evidence_transport.EVIDENCE_TRANSPORT_SCHEMA_HASH
        ):
            raise ControlError("unsupported_schema")
        if "retained_evidence_transport_schema_sha256" in config and (
            not evidence_pins.issubset(config)
            or config["retained_evidence_transport_schema_sha256"]
            != retained_transport.EVIDENCE_TRANSPORT_SCHEMA_HASH
        ):
            raise ControlError("unsupported_schema")
        if (
            config["schema"] != POLICY_SCHEMA
            or type(config["enabled"]) is not bool
            or config["history_reconcile"] is not False
            or not isinstance(config["caller_unit"], str)
            or not re.fullmatch(r"swapp-aos-[a-z0-9-]+\.service", config["caller_unit"])
            or config["infer_schema_sha256"] != INFER_SCHEMA_HASH
            or config["control_schema_sha256"] != CONTROL_SCHEMA_HASH
        ):
            raise ControlError("unsupported_schema")
        if not isinstance(config["control_socket"], str):
            raise ControlError("invalid_frame")
        cleanup = config.get("cleanup_targets", [])
        if not isinstance(cleanup, list) or len(cleanup) > 64:
            raise ControlError("invalid_frame")
        seen = set()
        for entry in cleanup:
            fields = {
                "target",
                "profile_id",
                "deployment_digest",
                "original_admission_binding_sha256",
                "original_cleanup_authorization_sha256",
                "operations",
            }
            if not isinstance(entry, dict) or set(entry) != fields:
                raise ControlError("invalid_frame")
            target = entry["target"]
            if (
                not isinstance(target, dict)
                or set(target) != TARGET_FIELDS
                or not _hex(target["request_id"], 32)
                or not _hex(target["request_sha256"])
                or not _hex(target["original_peer_generation_sha256"])
                or not isinstance(entry["profile_id"], str)
                or entry["profile_id"] not in PROFILE_IDS
                or not _hex(entry["deployment_digest"])
                or sum(
                    entry[key] is not None
                    for key in (
                        "original_admission_binding_sha256",
                        "original_cleanup_authorization_sha256",
                    )
                )
                != 1
                or any(
                    value is not None and not _hex(value)
                    for value in (
                        entry["original_admission_binding_sha256"],
                        entry["original_cleanup_authorization_sha256"],
                    )
                )
                or not isinstance(entry["operations"], list)
                or not entry["operations"]
                or any(
                    not isinstance(op, str) or op not in {"status", "cancel", "reconcile"}
                    for op in entry["operations"]
                )
                or entry["operations"] != sorted(set(entry["operations"]))
                or target["request_id"] in seen
            ):
                raise ControlError("invalid_frame")
            seen.add(target["request_id"])
        sources = config["source_files"]
        pins = config["profile_pins"]
        if (
            not isinstance(sources, dict)
            or set(sources) != {"scientist", "aos"}
            or not isinstance(pins, dict)
            or set(pins) != PROFILE_IDS
        ):
            raise ControlError("invalid_frame")
        scientist_required = {
            "lab/llm/aos_gpu_broker.py",
            "lab/llm/aos_gpu_service.py",
            "lab/llm/aos_gpu_executor.py",
            "lab/llm/gpu_scheduler.py",
            "lab/llm/aos_gpu_control.py",
            "lab/llm/aos_gpu_control_store.py",
        }
        if evidence_pins.issubset(config):
            scientist_required.update(
                {
                    "lab/llm/aos_evidence_transport.py",
                    "lab/llm/aos_control_contract_v2.py",
                    "lab/llm/contracts/control_v2/bundle.json",
                }
            )
            scientist_required.update(
                "lab/llm/contracts/control_v2/" + filename
                for filename in (
                    "admission-binding.schema.json",
                    "capability.schema.json",
                    "cleanup-grant.schema.json",
                    "legacy-terminal.schema.json",
                    "request.schema.json",
                    "response.template.schema.json",
                    "terminal-evidence.schema.json",
                    "terminal.schema.json",
                )
            )
        if "retained_evidence_transport_schema_sha256" in config:
            scientist_required.add("lab/llm/aos_retained_evidence_transport.py")
        aos_required = {
            "services/decider/broker_worker.py",
            "services/bonsai/broker_worker.py",
            "src/aos/scientist_transport.py",
            "src/aos/scientist_protocol.py",
            "src/aos/scientist_intents.py",
        }
        for name in ("scientist", "aos"):
            entries = sources[name]
            if (
                not isinstance(entries, dict)
                or not entries
                or len(entries) > 256
                or (name == "scientist" and not scientist_required <= set(entries))
                or (name == "aos" and not aos_required <= set(entries))
            ):
                raise ControlError("invalid_frame")
            for relative, expected in entries.items():
                if (
                    not isinstance(relative, str)
                    or Path(relative).is_absolute()
                    or ".." in Path(relative).parts
                    or not _hex(expected)
                ):
                    raise ControlError("invalid_frame")
        for profile_id, pin in pins.items():
            try:
                validate_profile_pin(pin, require_output_contract=True)
            except ControlStoreError as exc:
                raise ControlError(exc.code) from exc
            profile = profiles.get(profile_id, pin["deployment_digest"])
            if self._pin(profile) != pin:
                raise ControlError("profile_mismatch")
        self.config = config
        self.sha256 = fingerprint
        self.verify()

    @staticmethod
    def _pin(profile: Any) -> dict[str, Any]:
        pin = {
            "deployment_digest": profile.deployment_digest,
            "manifest_sha256": profile.manifest_sha256,
            "config_sha256": profile.config_sha256,
            "response_schema_sha256": profile.response_schema_sha256,
        }
        if profile.output_contract is not None:
            pin["output_contract"] = dict(profile.output_contract)
        return pin

    def verify_policy_hash(self) -> None:
        """Bounded final revocation check after potentially slow observations."""
        if self.config is None or self.path is None:
            raise ControlError("unauthorized")
        _, current = _private_policy(self.path)
        if current != self.sha256:
            raise ControlError("capability_mismatch")

    def verify(self) -> None:
        self.verify_policy_hash()
        if self.config is None:
            raise ControlError("unauthorized")
        roots = {"scientist": self.source_root}
        for profile_id, pin in self.config["profile_pins"].items():
            profile = self.profiles.get(profile_id, pin["deployment_digest"])
            if self._pin(profile) != pin:
                raise ControlError("profile_mismatch")
            if "aos" in roots and roots["aos"] != profile.source_root:
                raise ControlError("profile_mismatch")
            roots["aos"] = profile.source_root
        for name, files in self.config["source_files"].items():
            root = roots[name].resolve(strict=True)
            for relative, expected in files.items():
                end = control_deadline.get()
                if end is not None and time.monotonic() >= end:
                    raise ControlError("deadline_exceeded")
                candidate = root / relative
                resolved = candidate.resolve(strict=True)
                if resolved != candidate or not resolved.is_relative_to(root):
                    raise ControlError("capability_mismatch")
                fd = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_size > 8 * 1024 * 1024:
                        raise ControlError("capability_mismatch")
                    with os.fdopen(fd, "rb", closefd=False) as stream:
                        actual = hashlib.file_digest(stream, "sha256").hexdigest()
                finally:
                    os.close(fd)
                if actual != expected:
                    raise ControlError("capability_mismatch")

    def bound_profile(self, peer: PeerGeneration, profile_id: str, deployment: str) -> Any:
        self.verify()
        if self.config is None:
            raise ControlError("unauthorized")
        if peer.unit != self.config["caller_unit"]:
            raise ControlError("unauthorized")
        pin = self.config["profile_pins"].get(profile_id)
        if pin is None or pin["deployment_digest"] != deployment:
            raise ControlError("deployment_mismatch")
        return self.profiles.get(profile_id, deployment)

    def cleanup_target(
        self, peer: PeerGeneration, target: dict[str, Any], profile_id: str, deployment: str
    ) -> dict[str, Any]:
        self.verify()
        if (
            self.config is None
            or peer.unit != self.config["caller_unit"]
            or target["original_peer_generation_sha256"] != digest(asdict(peer))
        ):
            raise ControlError("unauthorized")
        for entry in self.config.get("cleanup_targets", []):
            if (
                entry["target"] == target
                and entry["profile_id"] == profile_id
                and entry["deployment_digest"] == deployment
            ):
                if not isinstance(entry, dict):
                    raise ControlError("unauthorized")
                return entry
        raise ControlError("unauthorized")


class LabAOSControl:
    def __init__(
        self,
        *,
        policy: ControlPolicy,
        authenticator: Any,
        store: Any,
        server_generation: dict[str, Any],
        clock: Any,
        server_current: Callable[[], bool] | None = None,
    ) -> None:
        self.policy = policy
        self.authenticator = authenticator
        self.store = store
        self.server_generation = server_generation
        self.clock = clock
        self.server_current = server_current
        self._capabilities: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _current(self, peer: PeerGeneration) -> None:
        if self.server_current is not None and not self.server_current():
            raise ControlError("stale_generation")
        if not self.authenticator.still_current(peer):
            raise ControlError("stale_generation")

    def _binding(self, peer: PeerGeneration, profile: Any) -> dict[str, Any]:
        config = self.policy.config
        if config is None:
            raise ControlError("unauthorized")
        return validate_admission_binding(
            {
                "server_generation": dict(self.server_generation),
                "caller_generation": asdict(peer),
                "policy_sha256": self.policy.sha256,
                "source_fingerprints": {
                    key: digest(value) for key, value in config["source_files"].items()
                },
                "profile_id": profile.profile_id,
                "profile_pin": self.policy._pin(profile),
                "infer_schema": {
                    "name": "aos-scientist-runtime.v1",
                    "version": 1,
                    "sha256": INFER_SCHEMA_HASH,
                },
                "control_schema": {"name": SCHEMA, "version": 1, "sha256": CONTROL_SCHEMA_HASH},
                "terminal_schema": {
                    "name": "aos-scientist-terminal.v1",
                    "version": 1,
                    "sha256": TERMINAL_SCHEMA_HASH,
                },
            }
        )

    def _mint(self, peer: PeerGeneration, profile: Any) -> dict[str, Any]:
        config = self.policy.config
        if config is None:
            raise ControlError("unauthorized")
        now = self.clock()
        binding = self._binding(peer, profile)
        capability = {
            "admission_binding": binding,
            "admission_binding_sha256": digest(binding),
            "server_generation_sha256": digest(self.server_generation),
            "caller_generation": asdict(peer),
            "caller_generation_sha256": digest(asdict(peer)),
            "policy_sha256": self.policy.sha256,
            "source_fingerprints": {
                key: digest(value) for key, value in config["source_files"].items()
            },
            "profile_id": profile.profile_id,
            **self.policy._pin(profile),
            "infer_schema": {
                "name": "aos-scientist-runtime.v1",
                "version": 1,
                "sha256": INFER_SCHEMA_HASH,
            },
            "control_schema": {"name": SCHEMA, "version": 1, "sha256": CONTROL_SCHEMA_HASH},
            "history_schema_sha256": HISTORY_SCHEMA_HASH if config["history_reconcile"] else None,
            "operations": sorted(OPS),
            "request_bytes": REQUEST_LIMIT,
            "response_bytes": RESPONSE_LIMIT,
            "frame_seconds": FRAME_SECONDS,
            "call_seconds": CALL_SECONDS,
            "boot_id": self.server_generation["boot_id"],
            "issued_boottime": now,
            "expires_boottime": now + CAPABILITY_SECONDS,
        }
        fingerprint = digest(capability)
        with self._lock:
            self._capabilities = {
                key: value
                for key, value in self._capabilities.items()
                if value["expires_boottime"] > now
            }
            if len(self._capabilities) >= MAX_CAPABILITIES:
                raise ControlError("busy")
            self._capabilities[fingerprint] = capability
        return {
            "capability": capability,
            "admission": "enabled" if config["enabled"] else "denied",
            "reason_code": "enabled" if config["enabled"] else "policy_disabled",
        }

    def _capability(
        self, peer: PeerGeneration, expected: str, profile_id: str, deployment: str
    ) -> dict[str, Any]:
        with self._lock:
            value = self._capabilities.get(expected)
        if value is None:
            raise ControlError("capability_mismatch")
        if value["expires_boottime"] <= self.clock():
            raise ControlError("capability_expired")
        if (
            value["caller_generation"] != asdict(peer)
            or value["policy_sha256"] != self.policy.sha256
            or value["profile_id"] != profile_id
            or value["deployment_digest"] != deployment
            or value["admission_binding"]
            != self._binding(peer, self.policy.bound_profile(peer, profile_id, deployment))
        ):
            raise ControlError("capability_mismatch")
        return value

    def admit_infer(self, peer: PeerGeneration, profile_id: str, deployment: str) -> AdmissionGrant:
        self._current(peer)
        profile = self.policy.bound_profile(peer, profile_id, deployment)
        binding_json = canonical(
            validate_admission_binding(self._binding(peer, profile), require_output_contract=True)
        ).decode("utf-8")

        def verify_current() -> None:
            # Called again while BEGIN IMMEDIATE owns the durable intent transition.
            self._current(peer)
            current = self.policy.bound_profile(peer, profile_id, deployment)
            if self.policy.config is None or not self.policy.config["enabled"]:
                raise ControlError("unauthorized")
            binding = validate_admission_binding(
                self._binding(peer, current), require_output_contract=True
            )
            if canonical(binding).decode("utf-8") != binding_json:
                raise ControlError("capability_mismatch")
            # Process observations can block. Freshness must be the final check
            # before the store commits, after every potentially slow lookup.
            self._current(peer)
            self.policy.verify_policy_hash()
            with self._lock:
                valid = any(
                    value["admission_binding_sha256"] == digest(binding)
                    and value["admission_binding"] == binding
                    and value["expires_boottime"] > self.clock()
                    for value in self._capabilities.values()
                )
            if not valid:
                raise ControlError("capability_mismatch")

        verify_current()
        return AdmissionGrant(binding_json, verify_current)

    def _control_authority(
        self,
        peer: PeerGeneration,
        expected: str,
        profile_id: str,
        deployment: str,
        target: dict[str, Any] | None = None,
    ) -> ControlAuthority:
        with self._lock:
            capability = self._capabilities.get(expected)
        retained = capability is None
        if retained:
            capability = self.store.target_capability(asdict(peer), expected)
        if capability is None:
            raise ControlError("capability_mismatch")
        if retained and (capability.get("target") != target or "control_binding" not in capability):
            raise ControlError("capability_mismatch")
        binding_json = canonical(
            capability["control_binding"] if retained else capability["admission_binding"]
        ).decode("utf-8")

        def verify_current() -> None:
            self._current(peer)
            profile = self.policy.bound_profile(peer, profile_id, deployment)
            if canonical(self._binding(peer, profile)).decode("utf-8") != binding_json:
                raise ControlError("capability_mismatch")
            self._current(peer)
            self.policy.verify_policy_hash()
            if retained:
                if capability["expires_boottime"] <= self.clock():
                    raise ControlError("capability_expired")
            else:
                with self._lock:
                    current = self._capabilities.get(expected)
                    if current is None or current["expires_boottime"] <= self.clock():
                        raise ControlError("capability_expired")

        verify_current()
        return ControlAuthority(binding_json, None, verify_current)

    def _cleanup_authority(
        self, peer: PeerGeneration, grant: dict[str, Any], expected: str | None = None
    ) -> ControlAuthority:
        grant_json = canonical(grant).decode("utf-8")
        cap = None if expected is None else self.store.target_capability(asdict(peer), expected)

        def verify_current() -> None:
            self._current(peer)
            entry = self.policy.cleanup_target(
                peer, grant["target"], grant["profile_id"], grant["deployment_digest"]
            )
            config = self.policy.config
            if (
                config is None
                or grant["caller_generation"] != asdict(peer)
                or grant["resolver_generation"] != self.server_generation
                or grant["policy_sha256"] != self.policy.sha256
                or grant["control_schema_sha256"] != CONTROL_SCHEMA_HASH
                or grant["source_fingerprints"]
                != {key: digest(value) for key, value in config["source_files"].items()}
                or grant["operations"] != entry["operations"]
                or any(
                    grant[key] != entry[key]
                    for key in (
                        "original_admission_binding_sha256",
                        "original_cleanup_authorization_sha256",
                    )
                )
            ):
                raise ControlError("unauthorized")
            self._current(peer)
            self.policy.verify_policy_hash()
            if expected is not None:
                if (
                    cap is None
                    or canonical(cap.get("cleanup_grant")).decode("utf-8") != grant_json
                    or cap["expires_boottime"] <= self.clock()
                ):
                    raise ControlError("capability_expired")

        verify_current()
        return ControlAuthority(None, grant_json, verify_current)

    def _mint_cleanup(
        self,
        peer: PeerGeneration,
        target: dict[str, Any],
        profile_id: str,
        deployment: str,
        request: dict[str, Any],
        response_validator: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        entry = self.policy.cleanup_target(peer, target, profile_id, deployment)
        original = self.store.cleanup_original(asdict(peer), target, profile_id, deployment)
        config = self.policy.config
        if config is None:
            raise ControlError("unauthorized")
        grant = {
            "schema": "aos-scientist-cleanup-grant.v1",
            "version": 1,
            "target": dict(target),
            "profile_id": profile_id,
            "deployment_digest": deployment,
            "caller_generation": asdict(peer),
            "resolver_generation": dict(self.server_generation),
            "policy_sha256": self.policy.sha256,
            "source_fingerprints": {
                key: digest(value) for key, value in config["source_files"].items()
            },
            "control_schema_sha256": CONTROL_SCHEMA_HASH,
            "operations": list(entry["operations"]),
            **original,
        }
        authority = self._cleanup_authority(peer, grant)
        now = self.clock()
        capability = {
            "cleanup_grant": grant,
            "cleanup_grant_sha256": digest(grant),
            "boot_id": self.server_generation["boot_id"],
            "issued_boottime": now,
            "expires_boottime": now + CAPABILITY_SECONDS,
        }
        response: dict[str, Any] = self.store.remember_target_capability(
            asdict(peer),
            target,
            profile_id,
            deployment,
            authority,
            request,
            {"capability": capability, "admission": "denied", "reason_code": "cleanup_only"},
            response_validator=response_validator,
        )
        return response

    def _mint_target(
        self,
        peer: PeerGeneration,
        request: dict[str, Any],
        response_validator: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        target, profile_id, deployment = (
            request["target"],
            request["profile_id"],
            request["deployment_digest"],
        )
        original = self.store.cleanup_original(asdict(peer), target, profile_id, deployment)
        expected = original["original_admission_binding"]
        if expected is None:
            expected = original["original_cleanup_authorization"]["binding"]
        try:
            profile = self.policy.bound_profile(peer, profile_id, deployment)
        except ControlError as exc:
            if exc.code != "deployment_mismatch":
                raise
            return self._mint_cleanup(
                peer, target, profile_id, deployment, request, response_validator
            )
        binding_json = canonical(self._binding(peer, profile)).decode("utf-8")
        if canonical(expected).decode("utf-8") != binding_json:
            return self._mint_cleanup(
                peer, target, profile_id, deployment, request, response_validator
            )

        def verify_current() -> None:
            self._current(peer)
            current = self.policy.bound_profile(peer, profile_id, deployment)
            if canonical(self._binding(peer, current)).decode("utf-8") != binding_json:
                raise ControlError("capability_mismatch")
            self._current(peer)
            self.policy.verify_policy_hash()

        authority = ControlAuthority(binding_json, None, verify_current)
        now = self.clock()
        capability = {
            "target": target,
            "control_binding": json.loads(binding_json),
            "operations": ["cancel", "reconcile", "status"],
            "boot_id": self.server_generation["boot_id"],
            "issued_boottime": now,
            "expires_boottime": now + CAPABILITY_SECONDS,
        }
        response: dict[str, Any] = self.store.remember_target_capability(
            asdict(peer),
            target,
            profile_id,
            deployment,
            authority,
            request,
            {
                "capability": capability,
                "admission": "denied",
                "reason_code": "retained_target_only",
            },
            response_validator=response_validator,
        )
        return response

    def read_original_budget(
        self,
        peer: PeerGeneration,
        target: dict[str, Any],
        profile_id: str,
        deployment_digest: str,
        *,
        expected_capability_sha256: str,
    ) -> dict[str, Any]:
        """Trusted in-process source read; never a socket operation or GPU release.

        Read original assignment columns under current retained-target rights.
        This creates no capability, frame, reservation or inference. A separate
        physical verifier and authenticated cross-process provider are required.
        """
        target, authority = self._original_source_authority(
            peer, target, profile_id, deployment_digest, expected_capability_sha256
        )
        witness: dict[str, Any] = self.store.read_original_budget(
            asdict(peer), target, profile_id, deployment_digest, authority=authority
        )
        authority.verify_current()
        self._current(peer)
        return witness

    def read_original_physical_snapshot(
        self,
        peer: PeerGeneration,
        target: dict[str, Any],
        profile_id: str,
        deployment_digest: str,
        *,
        expected_capability_sha256: str,
    ) -> dict[str, Any]:
        """Read original cleanup bindings under current retained-target rights.

        A separate read-only OS observer must prove physical absence. This does
        not release a lease, stop a worker, or grant journal resolution.
        """
        target, authority = self._original_source_authority(
            peer, target, profile_id, deployment_digest, expected_capability_sha256
        )
        snapshot: dict[str, Any] = self.store.read_original_physical_snapshot(
            asdict(peer), target, profile_id, deployment_digest, authority=authority
        )
        authority.verify_current()
        self._current(peer)
        return snapshot

    def verify_original_physical_cleanup(
        self,
        peer: PeerGeneration,
        target: dict[str, Any],
        profile_id: str,
        deployment_digest: str,
        *,
        expected_capability_sha256: str,
        expected_evidence: dict[str, Any],
        units: UnitManager,
        gpu: GpuObserver,
    ) -> dict[str, Any]:
        """Compose current retained source reads with independent OS observations.

        Trusted broker composition injects its read-only observers. No caller
        process command, stop/release operation, or journal authority is accepted.
        """
        self._require_physical_source()
        target, _authority = self._original_source_authority(
            peer, target, profile_id, deployment_digest, expected_capability_sha256
        )

        def read_snapshot() -> dict[str, Any]:
            return self.read_original_physical_snapshot(
                peer,
                target,
                profile_id,
                deployment_digest,
                expected_capability_sha256=expected_capability_sha256,
            )

        snapshot = verify_physical_readback(
            read_snapshot=read_snapshot, expected_evidence=expected_evidence, units=units, gpu=gpu
        )
        _target, authority = self._original_source_authority(
            peer, target, profile_id, deployment_digest, expected_capability_sha256
        )
        authority.verify_current()
        self._current(peer)
        self._require_physical_source()
        return snapshot

    def _require_physical_source(self) -> None:
        """Physical verification is opt-in and pins its executable observer."""
        config = self.policy.config
        if (
            config is None
            or "lab/llm/aos_physical_readback.py" not in config["source_files"]["scientist"]
        ):
            raise ControlError("unsupported_schema")
        self.policy.verify()

    def _original_source_authority(
        self,
        peer: PeerGeneration,
        target: dict[str, Any],
        profile_id: str,
        deployment_digest: str,
        expected_capability_sha256: str,
    ) -> tuple[dict[str, Any], ControlAuthority]:
        self._current(peer)
        self._require_evidence_policy(retained=True)
        if (
            not isinstance(target, dict)
            or set(target) != TARGET_FIELDS
            or not _hex(target.get("request_id"), 32)
            or not _hex(target.get("request_sha256"))
            or not _hex(target.get("original_peer_generation_sha256"))
            or not _hex(expected_capability_sha256)
        ):
            raise ControlError("invalid_frame")
        target = json.loads(canonical(target))
        if target["original_peer_generation_sha256"] != digest(asdict(peer)):
            raise ControlError("history_denied")
        authority = self._retained_target_authority(
            peer, target, profile_id, deployment_digest, expected_capability_sha256, retained=True
        )
        return target, authority

    def _retained_target_authority(
        self,
        peer: PeerGeneration,
        target: dict[str, Any],
        profile_id: str,
        deployment: str,
        fingerprint: str,
        *,
        retained: bool,
    ) -> ControlAuthority:
        capability = self.store.target_capability(asdict(peer), fingerprint)
        if capability is None:
            # Infer/bootstrap capabilities never grant retained source access.
            raise ControlError("capability_mismatch")
        if "cleanup_grant" in capability:
            grant = capability["cleanup_grant"]
            if (
                grant["target"] != target
                or grant["profile_id"] != profile_id
                or grant["deployment_digest"] != deployment
                or "reconcile" not in grant["operations"]
            ):
                raise ControlError("unauthorized")
            base = self._cleanup_authority(peer, grant, fingerprint)
        else:
            if capability.get("target") != target:
                raise ControlError("capability_mismatch")
            base = self._control_authority(peer, fingerprint, profile_id, deployment, target)

        def verify_current() -> None:
            self._require_evidence_policy(retained=retained)
            base.verify_current()

        return ControlAuthority(base.binding_json, base.cleanup_grant_json, verify_current)

    def _require_evidence_policy(self, *, retained: bool = False) -> None:
        config = self.policy.config
        if (
            config is None
            or config.get("evidence_schema_sha256") != evidence_transport.EVIDENCE_SCHEMA_SHA256
            or config.get("evidence_transport_schema_sha256")
            != evidence_transport.EVIDENCE_TRANSPORT_SCHEMA_HASH
        ):
            raise ControlError("unsupported_schema")
        if retained and (
            config.get("retained_evidence_transport_schema_sha256")
            != retained_transport.EVIDENCE_TRANSPORT_SCHEMA_HASH
        ):
            raise ControlError("unsupported_schema")
        self.policy.verify_policy_hash()

    def _handle_evidence(self, peer: PeerGeneration, request: dict[str, Any]) -> dict[str, Any]:
        """Opt-in proof transport, sharing existing target authority and quota."""
        retained = request["schema"] == retained_transport.SCHEMA
        codec: Any = retained_transport if retained else evidence_transport
        codec.decode_request(canonical(request))
        self._current(peer)
        self._require_evidence_policy(retained=retained)
        target, profile_id, deployment = (
            request["target"],
            request["profile_id"],
            request["deployment_digest"],
        )
        if target["original_peer_generation_sha256"] != digest(asdict(peer)):
            raise ControlError("history_denied")
        if request["op"] == "capability":

            def validate_capability(data: dict[str, Any]) -> None:
                codec.success_response(request, data, digest(data["capability"]))

            data = self._mint_target(peer, request, validate_capability)
            fingerprint = digest(data["capability"])
        else:
            fingerprint = request["expected_capability_sha256"]
            capability = self.store.target_capability(asdict(peer), fingerprint)
            if capability is None:
                # Bootstrap infer capabilities are never proof-export authority.
                raise ControlError("capability_mismatch")
            authority = self._retained_target_authority(
                peer, target, profile_id, deployment, fingerprint, retained=retained
            )

            def validate_evidence(evidence: dict[str, Any]) -> None:
                if retained:
                    response = codec.success_response(
                        request, evidence, fingerprint, expected_capability=capability
                    )
                    codec.encode_response(response, request, expected_capability=capability)
                else:
                    codec.encode_response(
                        codec.success_response(request, evidence, fingerprint), request
                    )

            reader = (
                self.store.read_retained_terminal_evidence
                if retained
                else self.store.read_terminal_evidence
            )
            data = reader(
                asdict(peer),
                target,
                profile_id,
                deployment,
                authority=authority,
                control_request=request,
                evidence_validator=validate_evidence,
            )
        self._current(peer)
        self._require_evidence_policy(retained=retained)
        if retained and request["op"] == "reconcile":
            return retained_transport.success_response(
                request, data, fingerprint, expected_capability=capability
            )
        response: dict[str, Any] = codec.success_response(request, data, fingerprint)
        return response

    def handle(self, peer: PeerGeneration, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("schema") in {evidence_transport.SCHEMA, retained_transport.SCHEMA}:
            return self._handle_evidence(peer, request)
        self._current(peer)
        profile_id, deployment = request["profile_id"], request["deployment_digest"]
        if request["op"] == "capability":
            if request["target"] is not None:
                data = self._mint_target(peer, request)
            else:
                profile = self.policy.bound_profile(peer, profile_id, deployment)

                def verify_bootstrap() -> None:
                    self._current(peer)
                    self.policy.bound_profile(peer, profile_id, deployment)
                    self._current(peer)
                    self.policy.verify_policy_hash()

                self.store.remember_control(
                    asdict(peer), request["control_id"], digest(request), verify_bootstrap
                )
                data = self._mint(peer, profile)
            fingerprint = digest(data["capability"])
        else:
            fingerprint = request["expected_capability_sha256"]
            target = request["target"]
            if target["original_peer_generation_sha256"] != digest(asdict(peer)):
                raise ControlError("history_denied")
            cleanup = self.store.target_capability(asdict(peer), fingerprint)
            if cleanup is not None and "cleanup_grant" in cleanup:
                grant = cleanup["cleanup_grant"]
                if (
                    grant["target"] != target
                    or grant["profile_id"] != profile_id
                    or grant["deployment_digest"] != deployment
                    or request["op"] not in grant["operations"]
                ):
                    raise ControlError("unauthorized")
                authority = self._cleanup_authority(peer, grant, fingerprint)
                if request["op"] == "cancel":
                    observation = self.store.cancel(
                        asdict(peer),
                        target,
                        profile_id,
                        deployment,
                        authority=authority,
                        control_request=request,
                    )
                else:
                    observation = self.store.lookup(
                        asdict(peer),
                        target,
                        profile_id,
                        deployment,
                        authority=authority,
                        operation=request["op"],
                        control_request=request,
                    )
                data = {
                    "cleanup_grant": grant,
                    "cleanup_grant_sha256": digest(grant),
                    "observation": observation,
                }
            elif request["op"] == "cancel":
                authority = self._control_authority(
                    peer, fingerprint, profile_id, deployment, target
                )
                profile = self.policy.bound_profile(peer, profile_id, deployment)
                data = self.store.cancel(
                    peer=asdict(peer),
                    target=target,
                    profile_id=profile.profile_id,
                    deployment_digest=profile.deployment_digest,
                    history=False,
                    profile_config_sha256=profile.config_sha256,
                    response_schema_sha256=profile.response_schema_sha256,
                    budget={
                        **asdict(profile.budgets),
                        "max_output_tokens": profile.max_output_tokens,
                        "context_tokens": profile.context_tokens,
                    },
                    authority=authority,
                    control_request=request,
                )
            else:
                authority = self._control_authority(
                    peer, fingerprint, profile_id, deployment, target
                )
                data = self.store.lookup(
                    peer=asdict(peer),
                    target=target,
                    profile_id=profile_id,
                    deployment_digest=deployment,
                    authority=authority,
                    operation=request["op"],
                    control_request=request,
                )
        self._current(peer)
        return {
            "schema": SCHEMA,
            "version": 1,
            "control_id": request["control_id"],
            "op": request["op"],
            "ok": True,
            "capability_sha256": fingerprint,
            "data": data,
            "error": None,
        }

    def serve_connection(self, connection: socket.socket, *, deadline: float | None = None) -> None:
        # A trusted listener may have already routed a peeked frame. Keep its
        # original budget instead of granting a second complete call window.
        now = time.monotonic()
        inherited = control_deadline.get()
        selected = now + CALL_SECONDS
        for outer in (inherited, deadline):
            if outer is not None:
                if type(outer) not in (float, int) or not now < outer <= now + CALL_SECONDS:
                    raise ControlError("deadline_exceeded")
                selected = min(selected, outer)
        token = control_deadline.set(selected)
        try:
            self._serve_connection(connection)
        finally:
            control_deadline.reset(token)

    def _serve_connection(self, connection: socket.socket) -> None:
        started = time.monotonic()
        deadline = control_deadline.get()
        if deadline is None:
            raise ControlError("deadline_exceeded")
        pid, uid, _gid = struct.unpack(
            "3i",
            connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")),
        )
        peer = self.authenticator.authenticate(pid, uid)
        request: dict[str, Any] | None = None
        try:
            raw = bytearray()
            while b"\n" not in raw:
                remaining = min(started + FRAME_SECONDS, deadline) - time.monotonic()
                if remaining <= 0:
                    raise ControlError("deadline_exceeded")
                connection.settimeout(remaining)
                part = connection.recv(min(4096, REQUEST_LIMIT + 1 - len(raw)))
                if not part or len(raw) + len(part) > REQUEST_LIMIT:
                    raise ControlError("invalid_frame")
                raw.extend(part)
            line, _, trailing = bytes(raw).partition(b"\n")
            if trailing:
                raise ControlError("invalid_frame")
            if strict_json(line).get("schema") == retained_transport.SCHEMA:
                request = retained_transport.decode_request(line)
            elif strict_json(line).get("schema") == evidence_transport.SCHEMA:
                request = evidence_transport.decode_request(line)
            else:
                request = decode_request(line)
            response = self.handle(peer, request)
        except (
            ControlError,
            ControlStoreError,
            evidence_transport.TransportError,
            retained_transport.TransportError,
        ) as exc:
            if request is None:
                raise
            code = exc.code if exc.code in ERROR_CODES else "internal_unavailable"
            response = {
                "schema": request["schema"],
                "version": request["version"],
                "control_id": request["control_id"],
                "op": request["op"],
                "ok": False,
                "capability_sha256": None,
                "data": None,
                "error": {
                    "code": code,
                    "retryable": code in {"busy", "deadline_exceeded", "internal_unavailable"},
                },
            }
        self._current(peer)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ControlError("deadline_exceeded")
        if request is not None and request["schema"] in {
            evidence_transport.SCHEMA,
            retained_transport.SCHEMA,
        }:
            retained = request["schema"] == retained_transport.SCHEMA
            if retained:
                capability = (
                    self.store.target_capability(
                        asdict(peer), request["expected_capability_sha256"]
                    )
                    if response["ok"] and request["op"] == "reconcile"
                    else None
                )
                encoded = (
                    retained_transport.encode_response(
                        response, request, expected_capability=capability
                    )
                    + b"\n"
                )
            else:
                encoded = evidence_transport.encode_response(response, request) + b"\n"
            if response["ok"]:
                self._require_evidence_policy(retained=retained)
                if request["op"] == "reconcile":
                    fingerprint = request["expected_capability_sha256"]
                    capability = self.store.target_capability(asdict(peer), fingerprint)
                    if capability is None:
                        raise ControlError("capability_mismatch")
                    if "cleanup_grant" in capability:
                        self._cleanup_authority(peer, capability["cleanup_grant"], fingerprint)
                    else:
                        self._control_authority(
                            peer,
                            fingerprint,
                            request["profile_id"],
                            request["deployment_digest"],
                            request["target"],
                        )
        else:
            encoded = canonical(response) + b"\n"
        if len(encoded) > RESPONSE_LIMIT:
            raise ControlError("internal_unavailable")
        # Encoding, policy/source reads and generation checks can be slow. The
        # earlier observation must not extend this connection's original budget.
        self._current(peer)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ControlError("deadline_exceeded")
        connection.settimeout(remaining)
        connection.sendall(encoded)
