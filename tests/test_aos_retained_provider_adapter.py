"""Synthetic CPU channel and ACK-closure tests; no AOS bootstrap/GPU acceptance."""

import array
import dataclasses
import importlib.util
import json
import os
import socket
import struct
import threading
import types
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/aos_retained_provider_adapter.py"
SPEC = importlib.util.spec_from_file_location("retained_provider_adapter_under_test", SCRIPT)
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)


@dataclasses.dataclass(frozen=True)
class SyntheticPeer:
    pid: int
    uid: int
    start_ticks: int = 123
    boot_id: str = "synthetic-boot"
    invocation_id: str = "1" * 32
    control_group: str = "/synthetic/provider"


class SyntheticAuthenticator:
    def __init__(self, peer):
        self.peer = peer
        self.current = True
        self.calls = []

    def authenticate(self, pid, uid):
        self.calls.append((pid, uid))
        if not self.current or (pid, uid) != (self.peer.pid, self.peer.uid):
            raise adapter.RetainedProviderError("synthetic identity denied")
        return self.peer

    def still_current(self, peer):
        return self.current and peer == self.peer


@pytest.fixture
def pair(monkeypatch):
    # Deliberate synthetic BrokerPeer API fixture; does not claim a systemd
    # generation or execution under the separate real AOS interpreter.
    import sys

    package = types.ModuleType("aos")
    package.__path__ = []
    transport = types.ModuleType("aos.scientist_transport")
    transport.BrokerPeer = SyntheticPeer
    monkeypatch.setitem(sys.modules, "aos", package)
    monkeypatch.setitem(sys.modules, "aos.scientist_transport", transport)
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    auth = SyntheticAuthenticator(SyntheticPeer(os.getpid(), os.getuid()))
    client = adapter.RetainedProviderClient(
        left, authenticator=auth, expected_peer=auth.peer, timeout_seconds=0.3
    )
    right.settimeout(1)
    yield client, right, auth
    left.close()
    right.close()


def arguments():
    return {
        "op": "read_budget",
        "target": {
            "request_id": "1" * 32,
            "request_sha256": "2" * 64,
            "original_peer_generation_sha256": "3" * 64,
        },
        "profile_id": "aos.decider.turn.v1",
        "deployment_digest": "4" * 64,
        "expected_capability_sha256": "5" * 64,
    }


def reply(sequence=1, **changes):
    return {
        "schema": adapter.SCHEMA,
        "version": 1,
        "sequence": sequence,
        "ok": True,
        "data": {"retained": "complete synthetic value"},
        "reason_code": None,
        **changes,
    }


def responder(channel, transform=None, *, count=1, ancillary=None, after_send=None):
    received, errors = [], []

    def run():
        try:
            for _attempt in range(count):
                request = adapter.decode(channel.recv(adapter.FRAME_LIMIT + 1))
                received.append(request)
                value = reply(request["sequence"])
                encoded = adapter.canonical(value).encode()
                if transform is not None:
                    encoded = transform(value)
                channel.sendmsg([encoded], ancillary or [])
                if after_send is not None:
                    after_send()
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, received, errors


def finish(thread, errors):
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert not errors


def test_sequence_and_original_request_only(pair):
    client, server, auth = pair
    thread, requests, errors = responder(server, count=2)
    assert client.exchange(**arguments()) == reply()["data"]
    assert client.exchange(**arguments()) == reply()["data"]
    finish(thread, errors)
    assert [request["sequence"] for request in requests] == [1, 2]
    assert requests[0] == {
        "schema": adapter.SCHEMA,
        "version": 1,
        "sequence": 1,
        **arguments(),
        "expected_evidence": None,
    }
    assert len(auth.calls) >= 9
    assert not client.poisoned


@pytest.mark.parametrize(
    "transform",
    [
        lambda value: adapter.canonical({**value, "version": 1.0}).encode(),
        lambda value: adapter.canonical({**value, "sequence": True}).encode(),
        lambda value: adapter.canonical({**value, "sequence": 2}).encode(),
        lambda value: adapter.canonical({**value, "extra": 0}).encode(),
        lambda value: adapter.canonical({**value, "ok": 1}).encode(),
        lambda value: adapter.canonical({**value, "data": None}).encode(),
        lambda value: adapter.canonical({**value, "reason_code": "secret host details"}).encode(),
        lambda value: json.dumps(value).encode(),
        lambda value: (
            adapter.canonical(value).replace('"version":1', '"version":1,"version":1').encode()
        ),
        lambda value: adapter.canonical(value).replace('"version":1', '"version":NaN').encode(),
        lambda _value: b"x" * (adapter.FRAME_LIMIT + 1),
    ],
)
def test_bad_response_permanently_poisons_channel(pair, transform):
    client, server, _auth = pair
    thread, _requests, errors = responder(server, transform)
    with pytest.raises(adapter.RetainedProviderError):
        client.exchange(**arguments())
    finish(thread, errors)
    assert client.poisoned
    with pytest.raises(adapter.RetainedProviderError, match="permanently uncertain"):
        client.exchange(**arguments())


def test_timeout_has_no_retry(pair):
    client, server, _auth = pair
    with pytest.raises((TimeoutError, adapter.RetainedProviderError)):
        client.exchange(**arguments())
    assert adapter.decode(server.recv(adapter.FRAME_LIMIT))["sequence"] == 1
    assert client.poisoned
    with pytest.raises(adapter.RetainedProviderError):
        client.exchange(**arguments())
    assert server.recv(1) == b""


def test_explicit_denial_is_generic_and_channel_closed(pair):
    client, server, _auth = pair
    thread, _requests, errors = responder(
        server,
        lambda value: adapter.canonical(
            {**value, "ok": False, "data": None, "reason_code": "provider_denied"}
        ).encode(),
    )
    with pytest.raises(adapter.RetainedProviderError, match="independent retained provider denied"):
        client.exchange(**arguments())
    finish(thread, errors)
    assert client.poisoned


def test_fd_transfer_is_denied_and_received_descriptor_closed(pair, monkeypatch):
    client, server, _auth = pair
    closed = []
    original_close = os.close

    def close(descriptor):
        closed.append(descriptor)
        original_close(descriptor)

    # Patch only the adapter's os reference, retaining real stdlib FD closure.
    monkeypatch.setattr(adapter, "os", types.SimpleNamespace(close=close))
    descriptor = os.open("/dev/null", os.O_RDONLY)
    try:
        thread, _requests, errors = responder(
            server,
            ancillary=[(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [descriptor]))],
        )
        with pytest.raises(adapter.RetainedProviderError, match="ancillary"):
            client.exchange(**arguments())
        finish(thread, errors)
        assert len(closed) == 1 and closed[0] != descriptor
        with pytest.raises(OSError):
            os.fstat(closed[0])
        os.fstat(descriptor)
    finally:
        original_close(descriptor)


@pytest.mark.parametrize(
    "ancillary,flags",
    [
        ([], 0),
        ([(socket.SOL_SOCKET, socket.SCM_CREDENTIALS, struct.pack("3i", 1, 2, 3))] * 2, 0),
        ([(socket.SOL_SOCKET, socket.SCM_CREDENTIALS, b"short")], 0),
        (
            [(socket.SOL_SOCKET, socket.SCM_CREDENTIALS, struct.pack("3i", 1, 2, 3))],
            socket.MSG_CTRUNC,
        ),
        (
            [(socket.SOL_SOCKET, socket.SCM_CREDENTIALS, struct.pack("3i", 1, 2, 3))],
            socket.MSG_TRUNC,
        ),
    ],
)
def test_missing_multiple_or_truncated_credentials_denied(ancillary, flags):
    with pytest.raises(adapter.RetainedProviderError):
        adapter._credentials(ancillary, flags)


def test_sender_of_forwarded_inherited_socket_is_authenticated(pair):
    client, server, _auth = pair
    child_pid = os.fork()
    if child_pid == 0:
        try:
            request = adapter.decode(server.recv(adapter.FRAME_LIMIT))
            server.send(adapter.canonical(reply(request["sequence"])).encode())
            os._exit(0)
        except BaseException:
            os._exit(1)
    try:
        with pytest.raises(adapter.RetainedProviderError, match="sender generation"):
            client.exchange(**arguments())
        assert client.poisoned
    finally:
        _pid, status = os.waitpid(child_pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0


def test_generation_revocation_before_send_denies_without_frame(pair):
    client, server, auth = pair
    auth.current = False
    with pytest.raises(adapter.RetainedProviderError):
        client.exchange(**arguments())
    assert server.recv(1) == b""


def test_only_one_outstanding_exchange(pair):
    client, _server, _auth = pair
    assert client._lock.acquire(blocking=False)
    try:
        with pytest.raises(adapter.RetainedProviderError, match="outstanding"):
            client.exchange(**arguments())
        assert not client.poisoned
    finally:
        client._lock.release()


def test_nonseqpacket_and_unconnected_sockets_denied(pair):
    _client, _server, auth = pair
    for kind in (socket.SOCK_STREAM, socket.SOCK_SEQPACKET):
        with socket.socket(socket.AF_UNIX, kind) as channel:
            with pytest.raises(adapter.RetainedProviderError):
                adapter.RetainedProviderClient(channel, authenticator=auth, expected_peer=auth.peer)


def test_oversized_and_float_timeout_denied(pair):
    _client, _server, auth = pair
    for timeout in (0, True, 3.01, float("nan"), float("inf")):
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            with pytest.raises(adapter.RetainedProviderError):
                adapter.RetainedProviderClient(
                    left, authenticator=auth, expected_peer=auth.peer, timeout_seconds=timeout
                )
        finally:
            left.close()
            right.close()


def physical_adapter():
    """Isolate closure plumbing; deliberately no synthetic authority acceptance."""
    provider = object.__new__(adapter.RetainedProviderAdapter)
    evidence = {
        "target": arguments()["target"],
        "terminal_canonical": '{"terminal":"exact"}',
        "allocation_canonical": None,
        "drain_canonical": None,
        "no_admission_canonical": '{"proof":"original"}',
        "result_canonical": '{"result":"must stay intact"}',
    }
    calls = []
    provider._authority = lambda *args: calls.append(("authority", args))
    provider._reconcile = lambda _original: (evidence.copy(), "5" * 64)
    provider._bindings = lambda _original: {
        key: value
        for key, value in arguments().items()
        if key in ("target", "profile_id", "deployment_digest")
    }

    def exchange(**values):
        calls.append(("exchange", values))
        return {
            "schema": "aos-scientist-physical-snapshot.v1",
            "version": 1,
            "evidence": values["expected_evidence"].copy(),
        }

    provider.client = types.SimpleNamespace(exchange=exchange)
    terminal = types.SimpleNamespace(model_dump=lambda **_kw: {"terminal": "exact"})
    return provider, terminal, evidence, calls


def test_physical_exchange_preserves_entire_pinned_result_closure():
    provider, terminal, evidence, calls = physical_adapter()
    assert provider.verify_physical(object(), terminal, None, None, {"proof": "original"}) is None
    exchange = [value for kind, value in calls if kind == "exchange"]
    assert len(exchange) == 1
    assert exchange[0]["expected_evidence"] == evidence
    assert exchange[0]["expected_evidence"]["result_canonical"] == evidence["result_canonical"]
    assert [kind for kind, _value in calls] == ["authority", "exchange", "authority"]


def test_candidate_preimages_cannot_change_physical_source_request():
    provider, terminal, _evidence, calls = physical_adapter()
    with pytest.raises(adapter.RetainedProviderError, match="arguments differ"):
        provider.verify_physical(object(), terminal, None, None, {"proof": "substituted"})
    assert all(kind != "exchange" for kind, _value in calls)


def test_snapshot_without_retained_result_is_denied():
    provider, terminal, evidence, _calls = physical_adapter()
    provider.client.exchange = lambda **_values: {
        "schema": "aos-scientist-physical-snapshot.v1",
        "version": 1,
        "evidence": {**evidence, "result_canonical": None},
    }
    with pytest.raises(adapter.RetainedProviderError, match="snapshot differs"):
        provider.verify_physical(object(), terminal, None, None, {"proof": "original"})


def test_current_authority_is_default_deny():
    with pytest.raises(adapter.RetainedProviderError, match="authority is not configured"):
        adapter._deny_current("read_budget", object(), object())
