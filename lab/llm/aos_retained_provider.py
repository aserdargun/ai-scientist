"""Authenticated read-only retained-source channel for one existing Lab broker.

The channel is an explicitly inherited Linux SOCK_SEQPACKET socketpair.
It creates no listener, scheduler, Store, capability or cleanup authority.
Kernel credentials accompany every packet; socket creation credentials cannot
identify a sender after inheritance. Each channel consumes sequence numbers
once, with one outstanding exchange and no retries.
"""

from __future__ import annotations

import array
import math
import os
import socket
import struct
import threading
import time
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from lab.llm import aos_control_contract_v2 as metadata
from lab.llm import aos_retained_evidence_transport as retained
from lab.llm.aos_gpu_broker import PROFILE_IDS, PeerGeneration
from lab.llm.aos_gpu_control import ControlError, LabAOSControl
from lab.llm.aos_gpu_control_store import ControlStoreError, control_deadline
from lab.llm.aos_physical_readback import PhysicalReadbackError
from lab.llm.native_runtime import GpuObserver, UnitManager, observation_deadline

SCHEMA = "aos-scientist-retained-provider.v1"
VERSION = 1
MAX_FRAME_BYTES = 128 * 1024
MAX_SECONDS = 3.0
SOURCE_FILE = "lab/llm/aos_retained_provider.py"
_REQUEST_KEYS = frozenset(
    {
        "schema",
        "version",
        "sequence",
        "op",
        "target",
        "profile_id",
        "deployment_digest",
        "expected_capability_sha256",
        "expected_evidence",
    }
)
_RESPONSE_KEYS = frozenset({"schema", "version", "sequence", "ok", "data", "reason_code"})
_TARGET_KEYS = frozenset({"request_id", "request_sha256", "original_peer_generation_sha256"})
_REASONS = frozenset(
    {
        "unauthorized",
        "stale_generation",
        "capability_mismatch",
        "capability_expired",
        "profile_mismatch",
        "deployment_mismatch",
        "request_conflict",
        "history_denied",
        "unsupported_schema",
        "busy",
        "deadline_exceeded",
        "internal_unavailable",
        "provider_denied",
    }
)
_CREDENTIALS = struct.Struct("3i")
# Linux allows at most 253 descriptors per SCM_RIGHTS message. Reserve enough
# space to receive and close all of them, including a credential message.
_ANCILLARY_BYTES = socket.CMSG_SPACE(_CREDENTIALS.size) + socket.CMSG_SPACE(256 * 4)


class RetainedProviderError(ValueError):
    """Bounded denial; exception messages never contain private paths or data."""

    def __init__(self, code: str = "invalid_frame") -> None:
        self.code = code if code in _REASONS else "invalid_frame"
        super().__init__(self.code)


def _require(condition: bool, code: str = "invalid_frame") -> None:
    if not condition:
        raise RetainedProviderError(code)


def _hex(value: Any, length: int = 64) -> bool:
    return (
        type(value) is str
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _timeout(value: float) -> float:
    _require(type(value) in (int, float) and math.isfinite(value) and 0 < value <= MAX_SECONDS)
    return float(value)


def prepare_channel(channel: socket.socket) -> None:
    """Validate and enable credentials BEFORE either endpoint sends a packet.

    Trusted bootstrap must call this for both endpoints before handing one to
    another process. Receiving before SO_PASSCRED was enabled can produce
    synthetic zero-PID credentials, which are always denied.
    """
    try:
        _require(isinstance(channel, socket.socket))
        _require(channel.family == socket.AF_UNIX)
        _require(channel.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) == socket.SOCK_SEQPACKET)
        _require(channel.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 0)
        for address in (channel.getsockname(), channel.getpeername()):
            # Linux SO_PASSCRED may automatically name a socketpair endpoint
            # with a five-hex abstract address after sending its first packet.
            # This address is not caller identity and creates no listener.
            _require(
                address in ("", b"")
                or isinstance(address, bytes)
                and len(address) == 6
                and address[0] == 0
                and all(byte in b"0123456789abcdef" for byte in address[1:])
            )
        channel.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    except (OSError, ValueError, AttributeError):
        raise RetainedProviderError() from None


def _remaining(clock: Callable[[], float], deadline: float) -> float:
    value = deadline - clock()
    _require(math.isfinite(value) and value > 0, "deadline_exceeded")
    return value


def _close(channel: socket.socket) -> None:
    try:
        channel.close()
    except OSError:
        pass


def _receive(
    channel: socket.socket, clock: Callable[[], float], deadline: float
) -> tuple[bytes, int, int]:
    channel.settimeout(_remaining(clock, deadline))
    raw, ancillary, flags, _address = channel.recvmsg(
        MAX_FRAME_BYTES + 1, _ANCILLARY_BYTES, socket.MSG_CMSG_CLOEXEC
    )
    credentials = []
    unexpected = False
    # Close every delivered descriptor even if the frame, credentials or
    # ancillary buffer were malformed. Linux closes undelivered truncated FDs.
    for level, kind, payload in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            descriptors = array.array("i")
            descriptors.frombytes(payload[: len(payload) - len(payload) % descriptors.itemsize])
            for descriptor in descriptors:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            unexpected = True
        elif level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS:
            if len(payload) == _CREDENTIALS.size:
                credentials.append(_CREDENTIALS.unpack(payload))
            else:
                unexpected = True
        else:
            unexpected = True
    _require(not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC))
    _require(not unexpected and len(credentials) == 1)
    _require(0 < len(raw) <= MAX_FRAME_BYTES)
    pid, uid, gid = credentials[0]
    _require(pid > 1 and uid >= 0 and gid >= 0, "unauthorized")
    _remaining(clock, deadline)
    return raw, pid, uid


def _send(channel: socket.socket, raw: bytes, clock: Callable[[], float], deadline: float) -> None:
    _require(0 < len(raw) <= MAX_FRAME_BYTES)
    channel.settimeout(_remaining(clock, deadline))
    _require(channel.send(raw) == len(raw))
    _remaining(clock, deadline)


def _check_definition(value: Any, name: str) -> None:
    metadata._check(value, {"$ref": "#/$defs/" + name}, retained.schema_document()["$defs"])


def decode_request(raw: bytes) -> dict[str, Any]:
    """Decode exact canonical JSON and the closed retained-provider request."""
    try:
        value = metadata.decode(raw, limit=MAX_FRAME_BYTES)
        _require(set(value) == _REQUEST_KEYS)
        _require(value["schema"] == SCHEMA)
        _require(type(value["version"]) is int and value["version"] == VERSION)
        _require(type(value["sequence"]) is int and 0 < value["sequence"] <= metadata.SAFE_INTEGER)
        _require(value["op"] in {"read_budget", "verify_physical"})
        target = value["target"]
        _require(type(target) is dict and set(target) == _TARGET_KEYS)
        _require(_hex(target["request_id"], 32))
        _require(_hex(target["request_sha256"]) and _hex(target["original_peer_generation_sha256"]))
        _require(type(value["profile_id"]) is str and value["profile_id"] in PROFILE_IDS)
        _require(_hex(value["deployment_digest"]) and _hex(value["expected_capability_sha256"]))
        if value["op"] == "read_budget":
            _require(value["expected_evidence"] is None)
        else:
            _check_definition(value["expected_evidence"], "terminal_evidence")
            _require(value["expected_evidence"]["target"] == target)
        return value
    except (metadata.ContractError, KeyError, TypeError, RecursionError):
        raise RetainedProviderError() from None


def validate_response(raw: bytes, request: dict[str, Any]) -> dict[str, Any]:
    """Validate correlation and data shape; this does not grant journal authority."""
    try:
        request = decode_request(metadata.canonical(request))
        value = metadata.decode(raw, limit=MAX_FRAME_BYTES)
        _require(set(value) == _RESPONSE_KEYS)
        _require(value["schema"] == SCHEMA)
        _require(type(value["version"]) is int and value["version"] == VERSION)
        _require(type(value["sequence"]) is int and value["sequence"] == request["sequence"])
        _require(type(value["ok"]) is bool)
        data = value["data"]
        if not value["ok"]:
            _require(data is None and type(value["reason_code"]) is str)
            _require(value["reason_code"] == "provider_denied")
        else:
            _require(value["reason_code"] is None and type(data) is dict)
            if request["op"] == "read_budget":
                _check_definition(data, "original_budget_witness")
                _require(data["target"] == request["target"])
                _require(data["profile_id"] == request["profile_id"])
                _require(data["deployment_digest"] == request["deployment_digest"])
                budget = metadata.decode(data["budget_canonical"].encode(), limit=MAX_FRAME_BYTES)
                _check_definition(budget, "legacy_budget")
                metadata._budget(budget, legacy=True)
                _require(metadata.digest(budget) == data["budget_sha256"])
            else:
                _require(
                    set(data)
                    == {
                        "schema",
                        "version",
                        "evidence",
                        "original_budget",
                        "ticket",
                        "child",
                        "handoff_stage",
                        "arbiter",
                    }
                )
                _require(data["schema"] == "aos-scientist-physical-snapshot.v1")
                _require(type(data["version"]) is int and data["version"] == 1)
                _require(data["evidence"] == request["expected_evidence"])
                _check_definition(data["original_budget"], "legacy_budget")
                arbiter = data["arbiter"]
                _require(
                    type(arbiter) is dict
                    and set(arbiter)
                    == {
                        "active_owner",
                        "active_request_id",
                        "active_token",
                        "phase",
                    }
                )
                _require(type(arbiter["active_token"]) is int and arbiter["active_token"] >= 0)
                _require(
                    (arbiter["active_owner"], arbiter["active_request_id"])
                    != (
                        "aos",
                        request["target"]["request_id"],
                    )
                )
                _require(data["ticket"] is None or type(data["ticket"]) is dict)
                _require(data["child"] is None or type(data["child"]) is dict)
        return value
    except (metadata.ContractError, KeyError, TypeError, UnicodeError, RecursionError):
        raise RetainedProviderError() from None


class RetainedProviderServer:
    """Dispatch bounded reads through the exact already-running control facade."""

    def __init__(
        self,
        control: LabAOSControl,
        *,
        units: UnitManager,
        gpu: GpuObserver,
        clock: Callable[[], float] = time.monotonic,
        timeout_seconds: float = MAX_SECONDS,
        expected_peer_generation: PeerGeneration | None = None,
    ) -> None:
        self.control = control
        self.units = units
        self.gpu = gpu
        self.clock = clock
        self.timeout_seconds = _timeout(timeout_seconds)
        if expected_peer_generation is not None:
            _require(isinstance(expected_peer_generation, PeerGeneration), "stale_generation")
        self._expected_peer_generation = deepcopy(expected_peer_generation)
        self._channel: socket.socket | None = None
        self._next_sequence = 1
        self._lock = threading.Lock()

    def verify_configuration(self) -> None:
        """Fail before activation when this provider's executable source is unpinned."""
        config = self.control.policy.config
        _require(
            type(config) is dict
            and config.get("enabled") is True
            and SOURCE_FILE in config.get("source_files", {}).get("scientist", {}),
            "unsupported_schema",
        )
        self.control.policy.verify()

    def _current(self, peer: Any, deadline: float) -> None:
        _remaining(self.clock, deadline)
        if self._expected_peer_generation is not None:
            _require(peer == self._expected_peer_generation, "stale_generation")
        self.control._current(peer)
        self.verify_configuration()
        _remaining(self.clock, deadline)

    def serve_once(self, channel: socket.socket, expected_sequence: int) -> None:
        """Serve one packet; malformed, expired or uncertain channels close and raise.

        Authorized operation denials return a bounded error and consume their
        sequence. The caller advances its loop sequence after any normal return.
        """
        if not self._lock.acquire(blocking=False):
            _close(channel)
            raise RetainedProviderError("busy")
        token = None
        observation_token = None
        try:
            _require(type(expected_sequence) is int and expected_sequence == self._next_sequence)
            _require(self._channel is None or channel is self._channel)
            prepare_channel(channel)
            self._channel = channel
            deadline = self.clock() + self.timeout_seconds
            # Store/authentication helpers consume real monotonic deadlines.
            local_deadline = time.monotonic() + self.timeout_seconds
            token = control_deadline.set(local_deadline)
            observation_token = observation_deadline.set(local_deadline)
            raw, pid, uid = _receive(channel, self.clock, deadline)
            request = decode_request(raw)
            _require(request["sequence"] == expected_sequence)
            peer = self.control.authenticator.authenticate(pid, uid)
            self._current(peer, deadline)
            common = (
                peer,
                request["target"],
                request["profile_id"],
                request["deployment_digest"],
            )
            try:
                if request["op"] == "read_budget":
                    data = self.control.read_original_budget(
                        *common,
                        expected_capability_sha256=request["expected_capability_sha256"],
                    )
                else:
                    data = self.control.verify_original_physical_cleanup(
                        *common,
                        expected_capability_sha256=request["expected_capability_sha256"],
                        expected_evidence=request["expected_evidence"],
                        units=self.units,
                        gpu=self.gpu,
                    )
                response = {
                    "schema": SCHEMA,
                    "version": VERSION,
                    "sequence": expected_sequence,
                    "ok": True,
                    "data": data,
                    "reason_code": None,
                }
            except (ControlError, ControlStoreError, PhysicalReadbackError):
                response = {
                    "schema": SCHEMA,
                    "version": VERSION,
                    "sequence": expected_sequence,
                    "ok": False,
                    "data": None,
                    "reason_code": "provider_denied",
                }
            self._current(peer, deadline)
            raw_response = metadata.canonical(response)
            validate_response(raw_response, request)
            self._current(peer, deadline)
            _send(channel, raw_response, self.clock, deadline)
            self._next_sequence += 1
        except Exception:
            _close(channel)
            raise RetainedProviderError("internal_unavailable") from None
        finally:
            if observation_token is not None:
                observation_deadline.reset(observation_token)
            if token is not None:
                control_deadline.reset(token)
            self._lock.release()


class RetainedProviderClient:
    """One sequential inherited channel, authenticated by each response sender."""

    def __init__(
        self,
        channel: socket.socket,
        *,
        authenticator: Any,
        expected_broker: Any,
        timeout_seconds: float = MAX_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.channel = channel
        self.authenticator = authenticator
        self.expected_broker = expected_broker
        self.timeout_seconds = _timeout(timeout_seconds)
        self.clock = clock
        self._next_sequence = 1
        self._lock = threading.Lock()
        self._poisoned = False
        try:
            prepare_channel(channel)
        except RetainedProviderError:
            _close(channel)
            raise

    def _current(self, deadline: float) -> None:
        _remaining(self.clock, deadline)
        _require(self.authenticator.still_current(self.expected_broker) is True, "stale_generation")
        _remaining(self.clock, deadline)

    def exchange(self, request: dict[str, Any]) -> dict[str, Any]:
        """Consume the explicit next sequence; any failure permanently closes the channel."""
        if not self._lock.acquire(blocking=False):
            self._poisoned = True
            _close(self.channel)
            raise RetainedProviderError("busy")
        try:
            _require(not self._poisoned, "internal_unavailable")
            deadline = self.clock() + self.timeout_seconds
            prepare_channel(self.channel)
            raw = metadata.canonical(request)
            request = decode_request(raw)
            _require(request["sequence"] == self._next_sequence)
            self._current(deadline)
            _send(self.channel, raw, self.clock, deadline)
            raw_response, pid, uid = _receive(self.channel, self.clock, deadline)
            broker = self.authenticator.authenticate(pid, uid)
            _require(broker == self.expected_broker, "unauthorized")
            self._current(deadline)
            response = validate_response(raw_response, request)
            self._current(deadline)
            if not response["ok"]:
                raise RetainedProviderError(response["reason_code"])
            self._next_sequence += 1
            return response
        except Exception as error:
            self._poisoned = True
            _close(self.channel)
            code = (
                error.code if isinstance(error, RetainedProviderError) else "internal_unavailable"
            )
            raise RetainedProviderError(code) from None
        finally:
            self._lock.release()
