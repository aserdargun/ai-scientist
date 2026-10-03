"""CPU-testable Lab-side protocol shell for the shared AOS GPU broker.

This module is an isolated design artifact, not a production service. It keeps
peer authentication and the fixed profile boundary in the Lab process. The
injected turn executor must be backed by the production SharedGpuScheduler and
the exact-unit model lifecycle before this can be deployed.
"""

from __future__ import annotations

import hashlib
import json
import socket
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from lab.llm.aos_gpu_control_store import AdmissionGrant

MAX_FRAME_BYTES = 128 * 1024
MAX_TURN_SECONDS = 720
PROFILE_IDS = frozenset(
    {
        "aos.decider.turn.v1",
        "aos.bonsai.recovery.v1",
        "aos.bonsai.vision.v1",
    }
)


class BrokerProtocolError(ValueError):
    """Malformed, oversized, or untrusted broker request."""


@dataclass(frozen=True, slots=True)
class PeerGeneration:
    """Exact caller service generation proven from SO_PEERCRED and systemd."""

    uid: int
    pid: int
    start_ticks: int
    boot_id: str
    unit: str
    invocation_id: str
    control_group: str
    parent_pid: int = 0
    parent_start_ticks: int = 0


@dataclass(frozen=True, slots=True)
class TurnReceipt:
    """Result metadata returned only after the executor's verified drain."""

    response: dict[str, object]
    usage: dict[str, object]
    unit: str
    invocation_id: str
    main_pid: int
    control_group: str


class PeerAuthenticator(Protocol):
    def authenticate(self, pid: int, uid: int) -> PeerGeneration: ...

    def still_current(self, peer: PeerGeneration) -> bool: ...


class OwnedTurnExecutor(Protocol):
    """Trusted Lab adapter around the single shared fair scheduler.

    Implementations submit/replay the immutable ticket in the common scheduler,
    own fixed-profile process startup/inference, and return only after the exact
    child generation has been stopped and its cgroup/GPU use verified drained.
    There is deliberately no caller-supplied drained flag or launch command.
    """

    def run_turn(
        self,
        *,
        peer: PeerGeneration,
        request_id: str,
        request_bytes: bytes,
        request_sha256: str,
        profile_id: str,
        deployment_digest: str,
        payload: dict[str, object],
        deadline: float,
        admission: AdmissionGrant | None = None,
    ) -> TurnReceipt: ...


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise BrokerProtocolError("request is not canonical JSON") from exc


def _receive_line(connection: socket.socket, deadline: float) -> bytes:
    data = bytearray()
    while b"\n" not in data:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("broker request deadline expired")
        connection.settimeout(remaining)
        chunk = connection.recv(min(8192, MAX_FRAME_BYTES + 1 - len(data)))
        if not chunk:
            raise BrokerProtocolError("incomplete request frame")
        data.extend(chunk)
        if len(data) > MAX_FRAME_BYTES:
            raise BrokerProtocolError("request frame exceeds protocol bound")
        line, separator, trailing = bytes(data).partition(b"\n")
    if not separator or trailing:
        raise BrokerProtocolError("one newline-terminated request is required")
    return line


def _decode_request(line: bytes) -> tuple[str, str, str, dict[str, object], bytes, str]:
    try:
        value = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrokerProtocolError("request JSON is malformed") from exc
    if not isinstance(value, dict) or set(value) != {
        "version",
        "op",
        "request_id",
        "profile_id",
        "deployment_digest",
        "payload",
    }:
        raise BrokerProtocolError("request fields do not match the fixed protocol")
    request_id = value["request_id"]
    profile_id = value["profile_id"]
    deployment_digest = value["deployment_digest"]
    payload = value["payload"]
    if (
        type(value["version"]) is not int
        or value["version"] != 1
        or value["op"] != "infer"
        or not isinstance(request_id, str)
        or len(request_id) != 32
        or any(char not in "0123456789abcdef" for char in request_id)
        or not isinstance(profile_id, str)
        or profile_id not in PROFILE_IDS
        or not isinstance(deployment_digest, str)
        or len(deployment_digest) != 64
        or any(char not in "0123456789abcdef" for char in deployment_digest)
        or not isinstance(payload, dict)
    ):
        raise BrokerProtocolError("request identity or fixed profile is invalid")
    canonical = _canonical(value)
    # The caller must send the exact canonical form so signatures, ticket hashes,
    # and retries all use the same bytes.
    if canonical != line:
        raise BrokerProtocolError("request JSON is not in canonical form")
    return (
        request_id,
        profile_id,
        deployment_digest,
        payload,
        canonical,
        hashlib.sha256(canonical).hexdigest(),
    )


def _encode_receipt(request_id: str, profile_id: str, digest: str, receipt: TurnReceipt) -> bytes:
    value = {
        "version": 1,
        "request_id": request_id,
        "profile_id": profile_id,
        "deployment_digest": digest,
        "generation": {
            "unit": receipt.unit,
            "invocation_id": receipt.invocation_id,
            "main_pid": receipt.main_pid,
            "control_group": receipt.control_group,
        },
        "response": receipt.response,
        "usage": receipt.usage,
    }
    encoded = _canonical(value) + b"\n"
    if len(encoded) > MAX_FRAME_BYTES:
        raise BrokerProtocolError("result frame exceeds protocol bound")
    return encoded


class LabAOSBroker:
    """Authenticate one AOS peer and hand it to a trusted Lab turn executor."""

    def __init__(
        self,
        authenticator: PeerAuthenticator,
        executor: OwnedTurnExecutor,
        *,
        admission: Callable[[PeerGeneration, str, str], AdmissionGrant] | None = None,
    ) -> None:
        self._authenticator = authenticator
        self._executor = executor
        self._admission = admission

    def serve_connection(self, connection: socket.socket) -> None:
        credentials = connection.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
        )
        pid, uid, _gid = struct.unpack("3i", credentials)
        peer = self._authenticator.authenticate(pid, uid)
        # Do not let a peer which passed SO_PEERCRED keep the broker worker
        # occupied for the model-turn budget while slowly sending a frame.
        admission_deadline = time.monotonic() + 5.0
        line = _receive_line(connection, admission_deadline)
        deadline = time.monotonic() + MAX_TURN_SECONDS
        request_id, profile, digest, payload, canonical, payload_hash = _decode_request(line)
        if not self._authenticator.still_current(peer):
            raise BrokerProtocolError("AOS service generation changed before admission")
        authority = {}
        if self._admission is not None:
            authority["admission"] = self._admission(peer, profile, digest)
        receipt = self._executor.run_turn(
            peer=peer,
            request_id=request_id,
            request_bytes=canonical,
            request_sha256=payload_hash,
            profile_id=profile,
            deployment_digest=digest,
            payload=payload,
            deadline=deadline,
            **authority,
        )
        if not self._authenticator.still_current(peer):
            raise BrokerProtocolError("AOS service generation changed during its turn")
        response = _encode_receipt(request_id, profile, digest, receipt)
        connection.settimeout(max(0.001, deadline - time.monotonic()))
        connection.sendall(response)
