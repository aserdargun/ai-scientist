"""Real Unix FD/credential CPU fixtures; no services, live DB, models or GPU."""

from __future__ import annotations

import array
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import asdict, replace

import pytest
import test_aos_retained_provider as fixtures

from lab.llm import aos_control_contract_v2 as metadata
from lab.llm import aos_evidence_transport, aos_retained_evidence_transport
from lab.llm import aos_retained_channel as channel
from lab.llm import aos_retained_provider as provider
from lab.llm.aos_gpu_broker import PeerGeneration
from lab.llm.aos_gpu_control_store import control_deadline

retained_fixture = fixtures.retained_fixture


def control_for(retained):
    control = fixtures.FakeControl(retained)
    control.peer = PeerGeneration(
        os.getuid(),
        os.getpid(),
        123,
        "cpu-fixture-boot",
        "swapp-aos-gpu-fixture.service",
        "a" * 32,
        "/cpu-fixture",
        os.getpid(),
        123,
    )
    control.authenticator = fixtures.Authentication(control.peer)
    control.server_generation = {
        key: value
        for key, value in asdict(control.peer).items()
        if key not in {"parent_pid", "parent_start_ticks"}
    }
    control.server_generation["unit"] = "swapp-lab-gpu-broker.service"
    control.server_current = lambda: True
    control.policy.config.update(
        caller_unit=control.peer.unit,
        evidence_schema_sha256=aos_evidence_transport.EVIDENCE_SCHEMA_SHA256,
        evidence_transport_schema_sha256=aos_evidence_transport.EVIDENCE_TRANSPORT_SCHEMA_HASH,
        retained_evidence_transport_schema_sha256=aos_retained_evidence_transport.EVIDENCE_TRANSPORT_SCHEMA_HASH,
    )
    control.policy.config["source_files"]["scientist"].update(
        {channel.SOURCE_FILE: "1" * 64, "lab/llm/aos_physical_readback.py": "2" * 64}
    )
    return control


def request(control, nonce="f" * 32):
    return {
        "schema": channel.SCHEMA,
        "version": 1,
        "op": "open",
        "nonce": nonce,
        "descriptor_sha256": channel.DESCRIPTOR_SHA256,
        "caller_generation_sha256": metadata.digest(asdict(control.peer)),
        "server_generation_sha256": metadata.digest(control.server_generation),
    }


def stream_pair():
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    for endpoint in (server, client):
        endpoint.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        endpoint.settimeout(3)
    return server, client


def start(bootstrap, server, *, deadline=None):
    def run():
        with server:
            bootstrap.serve(server, deadline=deadline or time.monotonic() + 3)

    return fixtures._worker(run)


def receive(client):
    data = bytearray()
    handles, credentials = [], []
    while b"\n" not in data:
        raw, ancillary, flags, _address = client.recvmsg(
            8193, socket.CMSG_SPACE(1024) + socket.CMSG_SPACE(12), socket.MSG_CMSG_CLOEXEC
        )
        assert not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)
        assert raw
        data.extend(raw)
        for level, kind, value in ancillary:
            assert level == socket.SOL_SOCKET
            if kind == socket.SCM_RIGHTS:
                descriptors = array.array("i")
                descriptors.frombytes(value)
                handles.extend(descriptors)
            else:
                assert kind == socket.SCM_CREDENTIALS
                credentials.append(struct.unpack("3i", value))
    assert data[-1:] == b"\n" and b"\n" not in data[:-1]
    assert all((pid, uid) == (os.getpid(), os.getuid()) for pid, uid, _gid in credentials)
    result = metadata.decode(bytes(data[:-1]))
    assert len(handles) == (1 if result["ok"] else 0)
    received = socket.socket(fileno=handles[0]) if handles else None
    return result, received


def open_channel(bootstrap, control, *, nonce="f" * 32):
    server, client = stream_pair()
    worker, errors = start(bootstrap, server)
    with client:
        sent = request(control, nonce)
        client.sendall(metadata.canonical(sent) + b"\n")
        response, endpoint = receive(client)
    fixtures._join(worker)
    assert errors == []
    assert {key: response[key] for key in sent} == sent
    return response, endpoint


def await_eof(endpoint):
    endpoint.settimeout(2)
    try:
        assert endpoint.recv(8192) == b""
    except ConnectionResetError:
        pass


def test_real_handoff_supports_successive_budget_and_physical_reads(retained_fixture):
    control = control_for(retained_fixture)
    bootstrap = channel.RetainedChannelBootstrap(control, object(), object())
    try:
        response, endpoint = open_channel(bootstrap, control)
        assert response["ok"] is True and response["reason_code"] is None
        with endpoint:
            assert endpoint.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) == socket.SOCK_SEQPACKET
            assert endpoint.getsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED) == 1
            assert endpoint.get_inheritable() is False
            client = provider.RetainedProviderClient(
                endpoint,
                authenticator=fixtures.Authentication(control.peer),
                expected_broker=control.peer,
            )
            first = client.exchange(fixtures._request(retained_fixture))
            second = client.exchange(
                fixtures._request(retained_fixture, sequence=2, operation="verify_physical")
            )
            assert first["data"] == retained_fixture[1]["original_budget_witness"]
            assert second["data"]["arbiter"]["active_owner"] == "lab"
        assert [call[0] for call in control.calls] == ["budget", "physical"]
        assert control_deadline.get() is None
    finally:
        bootstrap.close()


def test_one_atomic_slot_busy_then_eof_allows_new_channel(retained_fixture):
    control = control_for(retained_fixture)
    bootstrap = channel.RetainedChannelBootstrap(control, object(), object())
    try:
        _, first = open_channel(bootstrap, control)
        response, extra = open_channel(bootstrap, control, nonce="e" * 32)
        assert response["ok"] is False and response["reason_code"] == "busy" and extra is None
        first.close()
        for _ in range(100):
            with bootstrap._lock:
                if bootstrap._slot is None:
                    break
            time.sleep(0.01)
        response, second = open_channel(bootstrap, control, nonce="d" * 32)
        with second:
            assert response["ok"] is True
    finally:
        bootstrap.close()
    assert control.calls == []


def test_concurrent_handoffs_share_one_atomic_slot(retained_fixture):
    control = control_for(retained_fixture)
    bootstrap = channel.RetainedChannelBootstrap(control, object(), object())
    barrier = threading.Barrier(2)
    results = []

    def attempt(nonce):
        barrier.wait(timeout=3)
        results.append(open_channel(bootstrap, control, nonce=nonce))

    workers = [fixtures._worker(lambda n=n: attempt(n * 32)) for n in ("c", "d")]
    try:
        for worker, errors in workers:
            fixtures._join(worker)
            assert errors == []
        assert sorted(response["ok"] for response, _endpoint in results) == [False, True]
        assert [
            response["reason_code"] for response, _endpoint in results if not response["ok"]
        ] == ["busy"]
    finally:
        for _response, endpoint in results:
            if endpoint is not None:
                endpoint.close()
        bootstrap.close()
    assert control.calls == []


@pytest.mark.parametrize("fault", ["source", "disabled", "schema", "descriptor", "fifo", "broker"])
def test_configuration_missing_or_revoked_denies_before_handoff(
    retained_fixture, fault, monkeypatch, tmp_path
):
    control = control_for(retained_fixture)
    if fault == "source":
        del control.policy.config["source_files"]["scientist"][channel.SOURCE_FILE]
    elif fault == "disabled":
        control.policy.config["enabled"] = False
    elif fault == "schema":
        control.policy.config["retained_evidence_transport_schema_sha256"] = "0" * 64
    elif fault in ("descriptor", "fifo"):
        path = tmp_path / "descriptor.json"
        if fault == "fifo":
            os.mkfifo(path)
        else:
            path.write_bytes(b"{}")
        monkeypatch.setattr(channel, "DESCRIPTOR_PATH", path)
    else:
        control.server_current = None
    bootstrap = channel.RetainedChannelBootstrap(control, object(), object())
    with pytest.raises((channel.RetainedChannelError, OSError)):
        bootstrap.verify_configuration()
    assert control.calls == []


@pytest.mark.parametrize("fault", ["descriptor", "caller", "server", "non_main"])
def test_exact_original_handoff_bindings_required(retained_fixture, fault):
    control = control_for(retained_fixture)
    if fault == "non_main":
        control.peer = replace(control.peer, parent_pid=os.getpid() + 1)
        control.authenticator = fixtures.Authentication(control.peer)
    sent = request(control)
    if fault != "non_main":
        sent[
            {
                "descriptor": "descriptor_sha256",
                "caller": "caller_generation_sha256",
                "server": "server_generation_sha256",
            }[fault]
        ] = "0" * 64
    bootstrap = channel.RetainedChannelBootstrap(control, object(), object())
    server, client = stream_pair()
    worker, errors = start(bootstrap, server)
    with client:
        client.sendall(metadata.canonical(sent) + b"\n")
        response, endpoint = receive(client)
        assert response["ok"] is False and endpoint is None
    fixtures._join(worker)
    assert len(errors) == 1 and control.calls == [] and bootstrap._slot is None
    bootstrap.close()


def test_passed_stream_descriptor_cannot_impersonate_original_mainpid(retained_fixture):
    control = control_for(retained_fixture)
    bootstrap = channel.RetainedChannelBootstrap(control, object(), object())
    server, client = stream_pair()
    worker, errors = start(bootstrap, server)
    with client:
        payload = metadata.canonical(request(control)).decode() + "\n"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import socket,sys;s=socket.socket(fileno=int(sys.argv[1]));"
                "s.sendall(sys.argv[2].encode());s.close()",
                str(client.fileno()),
                payload,
            ],
            pass_fds=(client.fileno(),),
            timeout=3,
            check=False,
        )
        assert result.returncode == 0
        response, endpoint = receive(client)
        assert not response["ok"] and endpoint is None
    fixtures._join(worker)
    assert len(errors) == 1 and bootstrap._slot is None and control.calls == []
    bootstrap.close()


def test_request_fd_injection_is_closed_and_denied(retained_fixture):
    control = control_for(retained_fixture)
    bootstrap = channel.RetainedChannelBootstrap(control, object(), object())
    server, client = stream_pair()
    reader, writer = os.pipe()
    try:
        count = len(os.listdir("/proc/self/fd"))
        worker, errors = start(bootstrap, server)
        with client:
            client.sendmsg(
                [metadata.canonical(request(control)) + b"\n"],
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [reader, writer]))],
            )
            assert client.recv(8192) == b""
        fixtures._join(worker)
        assert len(errors) == 1 and bootstrap._slot is None
        assert len(os.listdir("/proc/self/fd")) == count - 2
    finally:
        os.close(reader)
        os.close(writer)
        bootstrap.close()


@pytest.mark.parametrize("fault", ["revoked", "replacement", "lifetime"])
def test_idle_channels_close_on_stale_generation_or_original_lifetime(
    retained_fixture, fault, monkeypatch
):
    monkeypatch.setattr(channel, "IDLE_SECONDS", 0.02)
    if fault == "lifetime":
        monkeypatch.setattr(channel, "CHANNEL_SECONDS", 0.06)
    control = control_for(retained_fixture)
    bootstrap = channel.RetainedChannelBootstrap(control, object(), object())
    try:
        _, endpoint = open_channel(bootstrap, control)
        with endpoint:
            if fault == "revoked":
                control.authenticator.current = False
            elif fault == "replacement":
                control.authenticator.peer = replace(control.peer, invocation_id="b" * 32)
            await_eof(endpoint)
        assert control.calls == []
    finally:
        bootstrap.close()


def test_generation_change_after_fd_send_closes_uncertain_channel(retained_fixture, monkeypatch):
    control = control_for(retained_fixture)
    bootstrap = channel.RetainedChannelBootstrap(control, object(), object())
    original_send = bootstrap._send

    def revoke_after_send(*args, **kwargs):
        original_send(*args, **kwargs)
        control.authenticator.current = False

    monkeypatch.setattr(bootstrap, "_send", revoke_after_send)
    server, client = stream_pair()
    worker, errors = start(bootstrap, server)
    with client:
        client.sendall(metadata.canonical(request(control)) + b"\n")
        response, endpoint = receive(client)
        assert response["ok"]
        with endpoint:
            await_eof(endpoint)
        assert client.recv(8192) == b""
    fixtures._join(worker)
    assert len(errors) == 1 and bootstrap._slot is None
    bootstrap.close()


def test_absolute_original_deadline_and_close_do_not_create_channel(retained_fixture):
    control = control_for(retained_fixture)
    bootstrap = channel.RetainedChannelBootstrap(control, object(), object())
    server, client = stream_pair()
    with server, client:
        token = control_deadline.set(time.monotonic() - 1)
        original = control_deadline.get()
        try:
            with pytest.raises(channel.RetainedChannelError):
                bootstrap.serve(server, deadline=time.monotonic() + 3)
            assert control_deadline.get() == original
        finally:
            control_deadline.reset(token)
    bootstrap.close()
    bootstrap.close()
    assert bootstrap._slot is None and control.calls == []


def test_authentication_cannot_renew_deadline_or_send_late_fd(retained_fixture):
    control = control_for(retained_fixture)
    control.policy.verify = lambda: time.sleep(0.03)
    bootstrap = channel.RetainedChannelBootstrap(control, object(), object())
    server, client = stream_pair()
    worker, errors = start(bootstrap, server, deadline=time.monotonic() + 0.015)
    with client:
        client.sendall(metadata.canonical(request(control)) + b"\n")
        assert client.recv(8192) == b""
    fixtures._join(worker)
    assert len(errors) == 1 and bootstrap._slot is None and control.calls == []
    bootstrap.close()


def test_provider_birth_generation_rejects_new_current_sender(retained_fixture):
    control = control_for(retained_fixture)
    original = control.peer
    server, client = fixtures._channels()
    dispatcher = provider.RetainedProviderServer(
        control, units=object(), gpu=object(), expected_peer_generation=original
    )
    control.peer = replace(original, invocation_id="b" * 32)
    control.authenticator.peer = control.peer
    with server, client:
        client.send(metadata.canonical(fixtures._request(retained_fixture)))
        with pytest.raises(provider.RetainedProviderError):
            dispatcher.serve_once(server, 1)
    assert control.calls == []


@pytest.mark.parametrize("fault", ["extra", "float_version", "noncanonical", "nonce"])
def test_closed_request_and_pure_schema_peek(retained_fixture, fault):
    sent = request(control_for(retained_fixture))
    assert channel.is_request(metadata.canonical(sent))
    if fault == "extra":
        sent["deadline"] = 3
    elif fault == "float_version":
        sent["version"] = 1.0
    elif fault == "nonce":
        sent["nonce"] = "f" * 31
    raw = metadata.canonical(sent)
    if fault == "noncanonical":
        raw += b" "
        assert channel.is_request(raw) is False
    with pytest.raises(channel.RetainedChannelError):
        channel._decode(raw)
