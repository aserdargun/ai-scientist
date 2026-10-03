"""Synthetic CPU AF_UNIX channel tests; no live AOS/generation/GPU proof."""

import array
import dataclasses
import json
import os
import socket
import threading
import time

import pytest

from scripts import aos_native_retained_channel as channel


@dataclasses.dataclass(frozen=True)
class Peer:
    pid: int
    uid: int
    start_ticks: int = 123
    boot_id: str = "synthetic-boot"
    invocation_id: str = "1" * 32
    control_group: str = "/synthetic/broker"


class Authenticator:
    def __init__(self, peer):
        self.peer = peer
        self.valid = True
        self.calls = 0

    def authenticate(self, pid, uid):
        self.calls += 1
        if not self.valid or (pid, uid) != (self.peer.pid, self.peer.uid):
            raise channel.NativeRetainedChannelError("synthetic generation revoked")
        return self.peer

    def still_current(self, peer):
        return self.valid and peer == self.peer


@pytest.fixture
def configuration(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(2)
    listener.settimeout(0.5)
    provider, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    peer = Peer(os.getpid(), os.getuid())
    authenticator = Authenticator(peer)
    generation = dataclasses.asdict(peer) | {"unit": channel.BROKER_UNIT}
    caller = generation | {
        "unit": "synthetic-desktop.service",
        "parent_pid": os.getpid(),
        "parent_start_ticks": generation["start_ticks"],
    }

    # Tests replace only runtime-directory ancestry; all IPC/SCM credential and
    # descriptor verification remains the actual implementation under test.
    def ancestry(selected):
        assert selected == path
        info = path.lstat()
        return info.st_dev, info.st_ino

    monkeypatch.setattr(channel, "_ancestry", ancestry)
    value = type("Configuration", (), {})()
    value.listener, value.provider, value.receiver = listener, provider, receiver
    value.authenticator, value.peer, value.path = authenticator, peer, path
    value.arguments = dict(
        authenticator=authenticator,
        expected_peer=peer,
        caller_generation=caller,
        server_generation=generation,
        descriptor_sha256=channel.DESCRIPTOR_SHA256,
        verify_current=lambda: None,
        timeout_seconds=0.3,
    )
    yield value
    listener.close()
    provider.close()
    receiver.close()


def read_request(connection):
    raw = bytearray()
    while b"\n" not in raw:
        block = connection.recv(8192)
        if not block:
            raise EOFError("synthetic client disconnected")
        raw.extend(block)
    return json.loads(raw)


def send_response(connection, request, descriptors, *, changes=None):
    response = request | {"ok": True, "reason_code": None} | (changes or {})
    ancillary = []
    if descriptors:
        ancillary.append((socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", descriptors)))
    connection.sendmsg([channel._canonical(response) + b"\n"], ancillary)


def server(configuration, *, changes=None, descriptor_count=1, after_send=None, delay=0):
    received, errors = [], []

    def serve():
        try:
            with configuration.listener.accept()[0] as connection:
                request = read_request(connection)
                received.append(request)
                if delay:
                    time.sleep(delay)
                descriptors = [configuration.receiver.fileno()] * descriptor_count
                send_response(connection, request, descriptors, changes=changes)
                if after_send:
                    after_send()
        except (BrokenPipeError, ConnectionResetError):
            if not delay:
                errors.append("unexpected disconnect")
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=serve)
    thread.start()
    return thread, received, errors


def finish(thread, errors):
    thread.join(1)
    assert not thread.is_alive()
    assert errors == []


def test_exact_success_returns_one_connected_noninheritable_channel(configuration):
    thread, received, errors = server(configuration)
    with channel.open_retained_provider_channel(
        configuration.path, **configuration.arguments
    ) as result:
        assert not result.get_inheritable()
        assert result.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) == socket.SOCK_SEQPACKET
        assert result.getsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED) == 1
        result.send(b"synthetic readback")
        assert configuration.provider.recv(64) == b"synthetic readback"
    finish(thread, errors)
    assert len(received) == 1
    assert received[0]["caller_generation_sha256"] == channel._hash(
        configuration.arguments["caller_generation"]
    )
    assert received[0]["server_generation_sha256"] == channel._hash(
        configuration.arguments["server_generation"]
    )
    assert configuration.authenticator.calls >= 5


def test_default_authority_and_revoked_generation_deny_before_connect(configuration):
    arguments = configuration.arguments.copy()
    arguments.pop("verify_current")
    with pytest.raises(channel.NativeRetainedChannelError, match="not configured"):
        channel.open_retained_provider_channel(configuration.path, **arguments)
    configuration.authenticator.valid = False
    with pytest.raises(channel.NativeRetainedChannelError, match="revoked"):
        channel.open_retained_provider_channel(configuration.path, **configuration.arguments)
    configuration.listener.settimeout(0.01)
    with pytest.raises(TimeoutError):
        configuration.listener.accept()


@pytest.mark.parametrize(
    "change",
    [{"pid": 999999}, {"invocation_id": "2" * 32}, {"start_ticks": 124}, {"unit": "other.service"}],
)
def test_expected_server_generation_exact_match_before_connect(configuration, change):
    arguments = configuration.arguments | {
        "server_generation": configuration.arguments["server_generation"] | change
    }
    with pytest.raises(channel.NativeRetainedChannelError, match="generation differs"):
        channel.open_retained_provider_channel(configuration.path, **arguments)


@pytest.mark.parametrize(
    "changes,count,reason",
    [
        ({"nonce": "f" * 32}, 1, "correlation"),
        ({"version": 2}, 1, "correlation"),
        ({}, 2, "exactly one"),
        ({"ok": False, "reason_code": "provider_denied"}, 1, "malformed channel denial"),
    ],
)
def test_bad_response_closes_every_received_descriptor(
    configuration, monkeypatch, changes, count, reason
):
    closed = []
    original_close = os.close

    def record_close(descriptor):
        original_close(descriptor)
        closed.append(descriptor)

    monkeypatch.setattr(channel.os, "close", record_close)
    thread, received, errors = server(configuration, changes=changes, descriptor_count=count)
    with pytest.raises(channel.NativeRetainedChannelError, match=reason):
        channel.open_retained_provider_channel(configuration.path, **configuration.arguments)
    finish(thread, errors)
    assert len(received) == 1
    assert len(closed) == count
    for descriptor in closed:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_clean_denial_has_no_descriptor_and_no_retry(configuration):
    thread, received, errors = server(
        configuration, descriptor_count=0, changes={"ok": False, "reason_code": "busy"}
    )
    with pytest.raises(channel.NativeRetainedChannelError, match="do not retry"):
        channel.open_retained_provider_channel(configuration.path, **configuration.arguments)
    finish(thread, errors)
    assert len(received) == 1


def test_forked_response_sender_rejected_despite_original_socket_creator(configuration):
    # Listener and transferred socket are born in this parent, so SO_PEERCRED
    # alone matches. The child actually sends the reply: SCM_CREDENTIALS must
    # reject it, before adopting the descriptor.
    pid = os.fork()
    if pid == 0:
        try:
            with configuration.listener.accept()[0] as connection:
                request = read_request(connection)
                send_response(connection, request, [configuration.receiver.fileno()])
            os._exit(0)
        except BaseException:
            os._exit(2)
    try:
        with pytest.raises(channel.NativeRetainedChannelError, match="response sender"):
            channel.open_retained_provider_channel(configuration.path, **configuration.arguments)
    finally:
        waited, status = os.waitpid(pid, 0)
        assert waited == pid and os.waitstatus_to_exitcode(status) == 0


def test_original_deadline_timeout_has_single_request_and_no_retry(configuration):
    thread, received, errors = server(configuration, delay=0.08)
    started = time.monotonic()
    with pytest.raises((TimeoutError, channel.NativeRetainedChannelError)):
        channel.open_retained_provider_channel(
            configuration.path, **(configuration.arguments | {"timeout_seconds": 0.025})
        )
    assert time.monotonic() - started < 0.15
    finish(thread, errors)
    assert len(received) == 1
    configuration.listener.settimeout(0.01)
    with pytest.raises(TimeoutError):
        configuration.listener.accept()


def test_rights_revocation_during_response_denies_adoption(configuration):
    calls = 0

    def rights():
        nonlocal calls
        calls += 1
        if calls >= 4:
            raise channel.NativeRetainedChannelError("synthetic current rights revoked")

    thread, received, errors = server(configuration)
    with pytest.raises(channel.NativeRetainedChannelError, match="rights revoked"):
        channel.open_retained_provider_channel(
            configuration.path, **(configuration.arguments | {"verify_current": rights})
        )
    finish(thread, errors)
    assert len(received) == 1
