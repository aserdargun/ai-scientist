"""Real UDS transport with fixture auth/execution; no systemd or GPU acceptance."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from lab.llm.aos_gpu_broker import (
    MAX_FRAME_BYTES,
    BrokerProtocolError,
    LabAOSBroker,
    PeerGeneration,
    TurnReceipt,
)
from lab.llm.gpu_scheduler import (
    LeaseConflict,
    PrincipalReceipt,
    SharedGpuScheduler,
    _current_process_identity,
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _request() -> dict[str, object]:
    return {
        "version": 1,
        "op": "infer",
        "request_id": "a" * 32,
        "profile_id": "aos.decider.turn.v1",
        "deployment_digest": "b" * 64,
        "payload": {"text": "Türkçe ölçüm 🌡"},
    }


class FixtureAuthenticator:
    """Uses actual SO_PEERCRED; service-generation claims are fixtures."""

    def __init__(self) -> None:
        self.allowed = True
        self.current = True
        self.credentials: tuple[int, int] | None = None
        identity = _current_process_identity()
        self.peer = PeerGeneration(
            os.getuid(),
            os.getpid(),
            identity.start_ticks,
            identity.boot_id,
            "swapp-aos-gpu-test.service",
            "c" * 32,
            "/fixture",
        )

    def authenticate(self, pid: int, uid: int) -> PeerGeneration:
        self.credentials = (pid, uid)
        if not self.allowed or (pid, uid) != (os.getpid(), os.getuid()):
            raise BrokerProtocolError("fixture principal rejected")
        return self.peer

    def still_current(self, peer: PeerGeneration) -> bool:
        return self.current and peer == self.peer


class FixtureExecutor:
    """Records admission and emits synthetic metadata without model execution."""

    def __init__(self, authenticator: FixtureAuthenticator) -> None:
        self.authenticator = authenticator
        self.calls: list[dict[str, object]] = []
        self.revoke = False
        self.response: dict[str, object] = {"text": "sentetik ölçüm"}

    def run_turn(self, **arguments: object) -> TurnReceipt:
        self.calls.append(arguments)
        if self.revoke:
            self.authenticator.current = False
        return TurnReceipt(
            self.response,
            {"tokens": 1},
            "fixture-child.service",
            "d" * 32,
            123,
            "/fixture-child",
        )


@pytest.fixture
def transport() -> Iterator[
    tuple[socket.socket, socket.socket, FixtureAuthenticator, FixtureExecutor, LabAOSBroker]
]:
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    auth = FixtureAuthenticator()
    executor = FixtureExecutor(auth)
    try:
        yield client, server, auth, executor, LabAOSBroker(auth, executor)
    finally:
        client.close()
        server.close()


def _assert_no_delivery(client: socket.socket) -> None:
    client.settimeout(0.01)
    with pytest.raises(TimeoutError):
        client.recv(1)


def test_canonical_utf8_admission_hash_and_actual_peer_credentials(transport) -> None:
    client, server, auth, executor, broker = transport
    request = _request()
    canonical = _canonical(request)
    before = time.monotonic()
    client.sendall(canonical + b"\n")
    broker.serve_connection(server)
    assert auth.credentials == (os.getpid(), os.getuid())
    assert len(executor.calls) == 1
    call = executor.calls[0]
    assert call["peer"] == auth.peer
    assert call["request_bytes"] == canonical
    assert call["request_sha256"] == hashlib.sha256(canonical).hexdigest()
    assert call["payload"] == request["payload"]
    assert before < call["deadline"] <= time.monotonic() + 720
    response = client.recv(MAX_FRAME_BYTES)
    decoded = json.loads(response)
    assert response == _canonical(decoded) + b"\n"
    assert decoded["request_id"] == request["request_id"]
    assert decoded["profile_id"] == request["profile_id"]
    assert decoded["deployment_digest"] == request["deployment_digest"]
    assert decoded["response"] == {"text": "sentetik ölçüm"}


@pytest.mark.parametrize("revocation", ["wrong-principal", "revoked-generation"])
def test_untrusted_or_revoked_peer_never_reaches_executor(transport, revocation: str) -> None:
    client, server, auth, executor, broker = transport
    auth.allowed = revocation != "wrong-principal"
    auth.current = revocation != "revoked-generation"
    client.sendall(_canonical(_request()) + b"\n")
    with pytest.raises(BrokerProtocolError):
        broker.serve_connection(server)
    assert not executor.calls
    _assert_no_delivery(client)


def test_generation_revoked_during_execution_does_not_deliver_result(transport) -> None:
    client, server, _auth, executor, broker = transport
    executor.revoke = True
    client.sendall(_canonical(_request()) + b"\n")
    with pytest.raises(BrokerProtocolError, match="changed during"):
        broker.serve_connection(server)
    assert len(executor.calls) == 1
    _assert_no_delivery(client)


@pytest.mark.parametrize("field", ["owner", "fencing_token", "gpu", "drained", "command"])
def test_caller_cannot_supply_owner_fence_gpu_or_lifecycle_authority(transport, field: str) -> None:
    client, server, _auth, executor, broker = transport
    request = _request()
    request[field] = "untrusted"
    client.sendall(_canonical(request) + b"\n")
    with pytest.raises(BrokerProtocolError, match="fields"):
        broker.serve_connection(server)
    assert not executor.calls


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", 2),
        ("version", True),
        ("profile_id", "arbitrary"),
        ("op", "cancel"),
        ("request_id", "A" * 32),
        ("deployment_digest", "g" * 64),
    ],
)
def test_wrong_wire_identity_is_rejected_before_admission(transport, field: str, value) -> None:
    client, server, _auth, executor, broker = transport
    request = _request()
    request[field] = value
    client.sendall(_canonical(request) + b"\n")
    with pytest.raises(BrokerProtocolError, match="identity"):
        broker.serve_connection(server)
    assert not executor.calls


@pytest.mark.parametrize("variant", ["whitespace", "duplicate-key", "double-frame", "bad-utf8"])
def test_noncanonical_or_duplicate_frames_are_rejected(transport, variant: str) -> None:
    client, server, _auth, executor, broker = transport
    canonical = _canonical(_request())
    wire = {
        "whitespace": b" " + canonical + b"\n",
        "duplicate-key": canonical[:-1] + b',"version":1}\n',
        "double-frame": canonical + b"\n" + canonical + b"\n",
        "bad-utf8": b'{"payload":"\xff"}\n',
    }[variant]
    client.sendall(wire)
    with pytest.raises(BrokerProtocolError):
        broker.serve_connection(server)
    assert not executor.calls


def test_truncated_frame_never_reaches_executor(transport) -> None:
    client, server, _auth, executor, broker = transport
    client.sendall(_canonical(_request()))
    client.shutdown(socket.SHUT_WR)
    with pytest.raises(BrokerProtocolError, match="incomplete"):
        broker.serve_connection(server)
    assert not executor.calls


def test_oversized_request_never_reaches_executor(transport) -> None:
    client, server, _auth, executor, broker = transport
    request = _request()
    request["payload"] = {"text": "x" * MAX_FRAME_BYTES}
    client.sendall(_canonical(request) + b"\n")
    with pytest.raises(BrokerProtocolError, match="exceeds protocol bound"):
        broker.serve_connection(server)
    assert not executor.calls


def test_oversized_result_is_not_delivered(transport) -> None:
    client, server, _auth, executor, broker = transport
    executor.response = {"text": "x" * MAX_FRAME_BYTES}
    client.sendall(_canonical(_request()) + b"\n")
    with pytest.raises(BrokerProtocolError, match="result frame exceeds"):
        broker.serve_connection(server)
    assert len(executor.calls) == 1
    _assert_no_delivery(client)


class FixtureQueueResolver:
    """Only principal generation changes are simulated, not queue behavior."""

    def __init__(self) -> None:
        self.invocation = "e" * 32
        self.current = True

    def resolve(self, owner: str) -> PrincipalReceipt:
        return PrincipalReceipt(
            owner, _current_process_identity(), f"swapp-{owner}-gpu-test.service", self.invocation
        )

    def verify(self, receipt: PrincipalReceipt) -> bool:
        return self.current and receipt.invocation_id == self.invocation


def test_only_original_live_generation_can_cancel_queued_ticket(tmp_path: Path) -> None:
    resolver = FixtureQueueResolver()
    scheduler = SharedGpuScheduler(
        tmp_path / "queue.sqlite", principal_resolver=resolver, drain_verifier=lambda _lease: True
    )
    scheduler.submit("aos", "cancel-me", b"synthetic")
    resolver.invocation = "f" * 32
    assert scheduler.cancel_queued("aos", "cancel-me") is False
    resolver.invocation = "e" * 32
    resolver.current = False
    with pytest.raises(LeaseConflict, match="identity"):
        scheduler.cancel_queued("aos", "cancel-me")
    resolver.current = True
    assert scheduler.cancel_queued("aos", "cancel-me") is True
    assert scheduler.cancel_queued("aos", "cancel-me") is False
    assert scheduler.try_acquire("aos", "cancel-me") is None


def test_queued_cancel_does_not_cancel_active_lease(tmp_path: Path) -> None:
    resolver = FixtureQueueResolver()
    scheduler = SharedGpuScheduler(
        tmp_path / "queue.sqlite", principal_resolver=resolver, drain_verifier=lambda _lease: True
    )
    scheduler.submit("aos", "active", b"synthetic")
    lease = scheduler.try_acquire("aos", "active")
    assert lease is not None
    assert scheduler.cancel_queued("aos", "active") is False
    assert scheduler.heartbeat(lease) is not None
    scheduler.submit("lab", "waiting", b"synthetic-lab")
    assert scheduler.try_acquire("lab", "waiting") is None
    scheduler.release(lease)
    assert scheduler.try_acquire("lab", "waiting") is not None
