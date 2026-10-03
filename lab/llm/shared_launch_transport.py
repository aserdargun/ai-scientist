"""Disabled proposal2 Unix launch exchange; no listener or service composition.

Trusted composition supplies already connected sockets, a pinned broker and a
bounded current-generation checker. One canonical frame and EOF are required in
both directions. This draft hash is for review, not an agreed wire capability.
Lost acknowledgements never trigger retry or turn status into spawn permission.
"""

from __future__ import annotations

import array
import hashlib
import math
import os
import secrets
import socket
import struct
import time
from collections.abc import Callable
from typing import Any, Protocol

from lab.llm import aos_control_contract_v2 as encoding
from lab.llm.shared_launch_ledger import ClaimResult, LaunchStatus, ServiceGeneration

ExpectedServiceGeneration = ServiceGeneration
BrokerCurrent = Callable[[ServiceGeneration, float], bool]
SCHEMA = "aos-scientist.shared-launch.transport.v1-proposal2.draft"
VERSION = 1
MAX_BYTES = 4096
MAX_SECONDS = 3.0
OPERATIONS = frozenset({"issue", "verify", "claim", "status", "revoke"})
_COMMON = frozenset(
    {
        "schema",
        "version",
        "schema_sha256",
        "nonce",
        "operation",
        "request_id",
        "binding_sha256",
        "broker_generation_sha256",
    }
)
_STATUS = frozenset(
    {"request_id", "binding_sha256", "state", "consumed", "cleanup_required", "expired"}
)
# Independent closed transport descriptor; no retained/inference schema adaptation.
WIRE_DESCRIPTOR = {
    "schema": SCHEMA,
    "version": VERSION,
    "max_bytes": MAX_BYTES,
    "framing": "canonical-json-LF-EOF; one request and response per connected Unix stream",
    "credentials": "SO_PEERCRED and each SCM_CREDENTIALS; identical pid/uid/gid",
    "request_fields": sorted(_COMMON),
    "claim_only_field": "intent_sha256",
    "response_extra_fields": ["consumed_now", "status"],
    "status_fields": sorted(_STATUS),
    "status_states": ["consumed", "reviewed", "revoked"],
    "operations": sorted(OPERATIONS),
    "nonce_request_id": "32 lowercase hex characters",
    "digests": "64 lowercase hex characters",
    "version_type": "nonboolean integer",
    "status_flags": "booleans; cleanup_required equals consumed",
    "fresh_claim": "claim only; consumed state, consumed and cleanup_required, not expired",
}
WIRE_SCHEMA_SHA256 = hashlib.sha256(encoding.canonical(WIRE_DESCRIPTOR)).hexdigest()
_CREDENTIALS = struct.Struct("3i")
_ANCILLARY_BYTES = socket.CMSG_SPACE(_CREDENTIALS.size) + socket.CMSG_SPACE(256 * 4)


class SharedLaunchTransportError(RuntimeError):
    """Fail closed without returning private authority or transport details."""


class LaunchController(Protocol):
    """Injected private authority interface; transport cannot select reviewed paths."""

    contract_sha256: str

    def handle(
        self,
        operation: str,
        request_id: str,
        binding_sha256: str,
        intent_sha256: str | None,
        *,
        peer_pid: int,
        peer_uid: int,
        deadline: float,
    ) -> dict[str, Any]:
        """Resolve reviewed bindings privately; kernel identity comes from transport."""


def _require(condition: bool) -> None:
    if not condition:
        raise SharedLaunchTransportError("launch_denied")


def _now() -> float:
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def _remaining(deadline: float) -> float:
    remaining = deadline - _now()
    _require(remaining > 0)
    return remaining


def _deadline(value: float) -> float:
    _require(type(value) in (float, int) and math.isfinite(value))
    # Never renew the caller's original window, including across suspend.
    result = min(value, _now() + MAX_SECONDS)
    _remaining(result)
    return result


def _hex(value: Any, length: int = 64) -> bool:
    return (
        type(value) is str
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _generation_hash(expected: ServiceGeneration) -> str:
    return hashlib.sha256(encoding.canonical(expected.model_dump(mode="json"))).hexdigest()


def _prepare(connection: socket.socket) -> tuple[int, int, int]:
    _require(
        connection.family == socket.AF_UNIX
        and connection.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) == socket.SOCK_STREAM
        and connection.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 0
    )
    connection.getpeername()
    connection.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    peer = _CREDENTIALS.unpack(
        connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _CREDENTIALS.size)
    )
    _require(peer[0] > 1 and peer[1] >= 0 and peer[2] >= 0)
    return peer


def _credentials(ancillary: list[tuple[int, int, bytes]], flags: int) -> tuple[int, int, int]:
    identities = []
    forbidden = bool(flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC))
    for level, kind, payload in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            handles = array.array("i")
            handles.frombytes(payload[: len(payload) - len(payload) % handles.itemsize])
            for handle in handles:
                try:
                    os.close(handle)
                except OSError:
                    pass
            forbidden = True
        elif (
            level == socket.SOL_SOCKET
            and kind == socket.SCM_CREDENTIALS
            and len(payload) == _CREDENTIALS.size
        ):
            identities.append(_CREDENTIALS.unpack(payload))
        else:
            forbidden = True
    _require(not forbidden and len(identities) == 1)
    return identities[0]


def _read(connection: socket.socket, peer: tuple[int, int, int], deadline: float) -> dict[str, Any]:
    data = bytearray()
    while True:
        connection.settimeout(_remaining(deadline))
        raw, ancillary, flags, _address = connection.recvmsg(
            MAX_BYTES + 2 - len(data), _ANCILLARY_BYTES, socket.MSG_CMSG_CLOEXEC
        )
        if not raw:
            # Linux SO_PASSCRED attaches synthetic zero credentials to stream EOF.
            # EOF has no sender message; only this exact sentinel is permitted.
            if ancillary:
                # Parse first so any delivered rights are closed even on truncation.
                _require(_credentials(ancillary, flags) == (0, 0, 0))
            else:
                _require(not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC))
            break
        observed = _credentials(ancillary, flags)
        _require(observed == peer and bool(raw))
        data.extend(raw)
        _require(len(data) <= MAX_BYTES + 1)
        # Await EOF to reject additional frames even when split across recvmsg calls.
        _require(data.count(b"\n") <= 1 and (b"\n" not in data or data.endswith(b"\n")))
    _remaining(deadline)
    _require(bool(data) and data.endswith(b"\n"))
    try:
        return encoding.decode(bytes(data[:-1]), limit=MAX_BYTES)
    except encoding.ContractError:
        raise SharedLaunchTransportError("launch_denied") from None


def _send(connection: socket.socket, value: dict[str, Any], deadline: float) -> None:
    raw = encoding.canonical(value) + b"\n"
    _require(len(raw) <= MAX_BYTES + 1)
    connection.settimeout(_remaining(deadline))
    credentials = _CREDENTIALS.pack(os.getpid(), os.getuid(), os.getgid())
    _require(
        connection.sendmsg([raw], [(socket.SOL_SOCKET, socket.SCM_CREDENTIALS, credentials)])
        == len(raw)
    )
    _remaining(deadline)
    connection.shutdown(socket.SHUT_WR)
    _remaining(deadline)


def _request(value: dict[str, Any], broker_hash: str) -> None:
    operation = value.get("operation")
    _require(type(operation) is str and operation in OPERATIONS)
    keys = _COMMON | ({"intent_sha256"} if operation == "claim" else set())
    _require(set(value) == keys and value["schema"] == SCHEMA)
    _require(type(value["version"]) is int and value["version"] == VERSION)
    _require(
        value["schema_sha256"] == WIRE_SCHEMA_SHA256
        and value["broker_generation_sha256"] == broker_hash
    )
    _require(
        _hex(value["nonce"], 32) and _hex(value["request_id"], 32) and _hex(value["binding_sha256"])
    )
    if operation == "claim":
        _require(_hex(value["intent_sha256"]))


def _result(value: dict[str, Any], request: dict[str, Any]) -> ClaimResult:
    _require(type(value) is dict and set(value) == {"status", "consumed_now"})
    status = value["status"]
    _require(type(status) is dict and set(status) == _STATUS)
    _require(
        status["request_id"] == request["request_id"]
        and status["binding_sha256"] == request["binding_sha256"]
    )
    _require(
        type(status["state"]) is str and status["state"] in {"reviewed", "consumed", "revoked"}
    )
    _require(all(type(status[key]) is bool for key in ("consumed", "cleanup_required", "expired")))
    _require(
        status["cleanup_required"] == status["consumed"]
        and (status["state"] != "consumed" or status["consumed"])
        and (status["state"] != "reviewed" or not status["consumed"])
    )
    fresh = value["consumed_now"]
    _require(type(fresh) is bool)
    if fresh:
        _require(
            request["operation"] == "claim"
            and status["state"] == "consumed"
            and status["consumed"]
            and status["cleanup_required"]
            and not status["expired"]
        )
    return ClaimResult(LaunchStatus(**status), fresh)


class _PinnedBroker:
    def __init__(self, expected_broker: ServiceGeneration, broker_current: BrokerCurrent) -> None:
        _require(isinstance(expected_broker, ServiceGeneration) and callable(broker_current))
        self.expected_broker = expected_broker
        self.broker_current = broker_current
        self.broker_hash = _generation_hash(expected_broker)

    def _current(self, pid: int, uid: int, deadline: float) -> None:
        _remaining(deadline)
        _require((pid, uid) == (self.expected_broker.pid, self.expected_broker.uid))
        _require(self.broker_current(self.expected_broker, deadline) is True)
        _remaining(deadline)


class SharedLaunchServer(_PinnedBroker):
    """Serve one provided connection, with no listener or live authority defaults."""

    def __init__(
        self,
        controller: LaunchController,
        *,
        expected_broker: ServiceGeneration,
        broker_current: BrokerCurrent,
    ) -> None:
        super().__init__(expected_broker, broker_current)
        _require(getattr(controller, "contract_sha256", None) == WIRE_SCHEMA_SHA256)
        self.controller = controller

    def serve_once(self, connection: socket.socket, *, deadline: float) -> None:
        """Consume and close one original connection under its fixed BOOTTIME limit."""
        try:
            deadline = _deadline(deadline)
            peer = _prepare(connection)
            self._current(os.getpid(), os.getuid(), deadline)
            request = _read(connection, peer, deadline)
            self._current(os.getpid(), os.getuid(), deadline)
            _request(request, self.broker_hash)
            result = self.controller.handle(
                request["operation"],
                request["request_id"],
                request["binding_sha256"],
                request.get("intent_sha256"),
                peer_pid=peer[0],
                peer_uid=peer[1],
                deadline=deadline,
            )
            _result(result, request)
            self._current(os.getpid(), os.getuid(), deadline)
            _send(connection, {**request, **result}, deadline)
            self._current(os.getpid(), os.getuid(), deadline)
        except (OSError, ValueError, RuntimeError) as error:
            raise SharedLaunchTransportError("launch_denied") from error
        finally:
            connection.close()


class SharedLaunchClient(_PinnedBroker):
    """One socket per call from trusted composition; no retry or peer chosen path."""

    def __init__(
        self,
        socket_factory: Callable[[float], socket.socket],
        *,
        expected_broker: ServiceGeneration,
        broker_current: BrokerCurrent,
    ) -> None:
        super().__init__(expected_broker, broker_current)
        _require(callable(socket_factory))
        self.socket_factory = socket_factory

    def request(
        self,
        operation: str,
        request_id: str,
        binding_sha256: str,
        intent_sha256: str | None = None,
        *,
        deadline: float,
    ) -> ClaimResult:
        """Exchange once; no response or repeated status implies fresh permission."""
        request = {
            "schema": SCHEMA,
            "version": VERSION,
            "schema_sha256": WIRE_SCHEMA_SHA256,
            "nonce": secrets.token_hex(16),
            "operation": operation,
            "request_id": request_id,
            "binding_sha256": binding_sha256,
            "broker_generation_sha256": self.broker_hash,
        }
        if intent_sha256 is not None:
            request["intent_sha256"] = intent_sha256
        connection = None
        try:
            deadline = _deadline(deadline)
            _request(request, self.broker_hash)
            connection = self.socket_factory(deadline)
            peer = _prepare(connection)
            self._current(peer[0], peer[1], deadline)
            _send(connection, request, deadline)
            self._current(peer[0], peer[1], deadline)
            response = _read(connection, peer, deadline)
            self._current(peer[0], peer[1], deadline)
            _require(set(response) == set(request) | {"status", "consumed_now"})
            _request({key: response[key] for key in request}, self.broker_hash)
            _require(all(response[key] == value for key, value in request.items()))
            result = _result({key: response[key] for key in ("status", "consumed_now")}, request)
            _remaining(deadline)
            return result
        except (OSError, ValueError, RuntimeError) as error:
            raise SharedLaunchTransportError("launch_denied") from error
        finally:
            if connection is not None:
                connection.close()

    def claim(
        self, request_id: str, binding_sha256: str, intent_sha256: str, *, deadline: float
    ) -> ClaimResult:
        """Return consumption readback; only consumed_now distinguishes fresh claim."""
        return self.request("claim", request_id, binding_sha256, intent_sha256, deadline=deadline)

    def require_fresh_claim(
        self, request_id: str, binding_sha256: str, intent_sha256: str, *, deadline: float
    ) -> ClaimResult:
        """Deny duplicate consumption, including a previous claim whose ACK was lost."""
        result = self.claim(request_id, binding_sha256, intent_sha256, deadline=deadline)
        _remaining(deadline)
        _require(result.consumed_now)
        return result

    def activation_claimer(
        self, request_id: str, binding_sha256: str, intent_sha256: str, *, deadline: float
    ) -> None:
        """AOS seam adapter: return None only after the original fresh consumption."""
        self.require_fresh_claim(request_id, binding_sha256, intent_sha256, deadline=deadline)
