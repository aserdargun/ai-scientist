"""CPU socket/SQLite fixtures; no live AOS, model or GPU acceptance evidence."""

from __future__ import annotations

import array
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import threading
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_aos_evidence_integration as integration
import test_aos_retained_evidence_integration as retained_integration
import test_aos_retained_evidence_transport as transport_cases

from lab.llm import aos_control_contract_v2 as metadata
from lab.llm import aos_retained_provider as provider
from lab.llm.aos_gpu_control import ControlError
from lab.llm.aos_gpu_control_store import control_deadline
from lab.llm.aos_gpu_executor import SystemdAOSProfileRuntime
from lab.llm.native_runtime import observation_deadline

retained_fixture = transport_cases.retained
cleanup_fixture = integration.cleanup_fixture


def _request(retained_fixture, *, sequence=1, operation="read_budget"):
    capability, data = retained_fixture
    witness = data["original_budget_witness"]
    return {
        "schema": provider.SCHEMA,
        "version": 1,
        "sequence": sequence,
        "op": operation,
        "target": deepcopy(witness["target"]),
        "profile_id": witness["profile_id"],
        "deployment_digest": witness["deployment_digest"],
        "expected_capability_sha256": metadata.digest(capability),
        "expected_evidence": deepcopy(data["evidence"]) if operation == "verify_physical" else None,
    }


class Authentication:
    """Synthetic unit generation, with real per-message kernel PID/UID checks."""

    def __init__(self, peer, *, pid=None):
        self.peer = peer
        self.pid = os.getpid() if pid is None else pid
        self.current = True
        self.seen = []

    def authenticate(self, pid, uid):
        self.seen.append((pid, uid))
        if pid != self.pid or uid != os.getuid():
            raise ValueError("synthetic peer denied")
        return self.peer

    def still_current(self, peer):
        return self.current and peer == self.peer


class FakeControl:
    """Facade dispatch spy; no invented scheduler, Store or physical observation."""

    def __init__(self, retained_fixture):
        self.peer = object()
        self.authenticator = Authentication(self.peer)
        self.policy = SimpleNamespace(
            config={
                "enabled": True,
                "source_files": {"scientist": {provider.SOURCE_FILE: "1" * 64}},
            },
            verify=lambda: None,
        )
        self.data = retained_fixture[1]
        self.calls = []
        self.after_read = lambda: None

    def _current(self, peer):
        if not self.authenticator.still_current(peer):
            raise ControlError("stale_generation")

    def read_original_budget(self, *arguments, **keywords):
        self.calls.append(("budget", arguments, keywords))
        assert control_deadline.get() is not None
        assert control_deadline.get() == observation_deadline.get()
        self.after_read()
        return deepcopy(self.data["original_budget_witness"])

    def verify_original_physical_cleanup(self, *arguments, **keywords):
        self.calls.append(("physical", arguments, keywords))
        budget = metadata.decode(self.data["original_budget_witness"]["budget_canonical"].encode())
        return {
            "schema": "aos-scientist-physical-snapshot.v1",
            "version": 1,
            "evidence": deepcopy(self.data["evidence"]),
            "original_budget": budget,
            "child": None,
            "ticket": None,
            "handoff_stage": None,
            "arbiter": {
                "active_owner": "lab",
                "active_request_id": "7" * 32,
                "active_token": 4,
                "phase": "inference",
            },
        }


def _channels():
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    provider.prepare_channel(server)
    provider.prepare_channel(client)
    return server, client


def _worker(function):
    errors = []

    def run():
        try:
            function()
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    return worker, errors


def _join(worker):
    worker.join(timeout=4)
    assert not worker.is_alive()


def test_successive_reads_use_same_facade_and_observers(retained_fixture):
    control = FakeControl(retained_fixture)
    units, gpu, broker = object(), object(), object()
    dispatcher = provider.RetainedProviderServer(control, units=units, gpu=gpu)
    authentication = Authentication(broker)
    server, channel = _channels()
    with server, channel:
        client = provider.RetainedProviderClient(
            channel,
            authenticator=authentication,
            expected_broker=broker,
        )
        worker, errors = _worker(lambda: [dispatcher.serve_once(server, n) for n in (1, 2)])
        try:
            first = client.exchange(_request(retained_fixture))
            second = client.exchange(
                _request(retained_fixture, sequence=2, operation="verify_physical")
            )
        finally:
            _join(worker)
    assert errors == []
    assert first["data"] == retained_fixture[1]["original_budget_witness"]
    assert second["data"]["arbiter"]["active_owner"] == "lab"
    assert control.calls[0][1][0] is control.peer
    assert control.calls[1][2]["units"] is units and control.calls[1][2]["gpu"] is gpu
    assert len(control.authenticator.seen) == len(authentication.seen) == 2
    assert control_deadline.get() is None and observation_deadline.get() is None


@pytest.mark.parametrize(
    "change", ["version", "sequence", "extra", "budget_evidence", "target", "nan"]
)
def test_closed_canonical_request_contract(retained_fixture, change):
    request = _request(retained_fixture)
    if change == "version":
        request["version"] = 1.0
    elif change == "sequence":
        request["sequence"] = True
    elif change == "extra":
        request["deadline"] = 3
    elif change == "budget_evidence":
        request["expected_evidence"] = retained_fixture[1]["evidence"]
    elif change == "target":
        request["target"]["request_id"] = "f" * 31
    else:
        raw = metadata.canonical(request).replace(b'"sequence":1', b'"sequence":NaN')
        with pytest.raises(provider.RetainedProviderError):
            provider.decode_request(raw)
        return
    with pytest.raises(provider.RetainedProviderError):
        provider.decode_request(metadata.canonical(request))


def test_duplicate_keys_and_noncanonical_json_denied(retained_fixture):
    raw = metadata.canonical(_request(retained_fixture))
    for malformed in (b'{"version":1,' + raw[1:], b" " + raw):
        with pytest.raises(provider.RetainedProviderError):
            provider.decode_request(malformed)


@pytest.mark.parametrize("kind", [socket.SOCK_STREAM, socket.SOCK_DGRAM])
def test_non_seqpacket_channels_denied(kind):
    server, client = socket.socketpair(socket.AF_UNIX, kind)
    with server, client, pytest.raises(provider.RetainedProviderError):
        provider.prepare_channel(server)


def test_unconnected_channel_denied():
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as channel:
        with pytest.raises(provider.RetainedProviderError):
            provider.prepare_channel(channel)


@pytest.mark.parametrize("mode", ["missing", "disabled", "revoked"])
def test_source_policy_gate_precedes_dispatch(retained_fixture, mode):
    control = FakeControl(retained_fixture)
    if mode == "missing":
        control.policy.config["source_files"]["scientist"].clear()
    elif mode == "disabled":
        control.policy.config["enabled"] = False
    else:

        def revoke():
            raise ControlError("unauthorized")

        control.policy.verify = revoke
    dispatcher = provider.RetainedProviderServer(control, units=object(), gpu=object())
    with pytest.raises((provider.RetainedProviderError, ControlError)):
        dispatcher.verify_configuration()
    assert control.calls == []


@pytest.mark.parametrize("failure", ["late", "revoked", "pin"])
def test_read_expiry_or_revocation_cannot_send_success(retained_fixture, failure):
    control = FakeControl(retained_fixture)
    now = [10.0]
    if failure == "late":
        control.after_read = lambda: now.__setitem__(0, 13.0)
    elif failure == "revoked":
        control.after_read = lambda: setattr(control.authenticator, "current", False)
    else:
        control.after_read = lambda: control.policy.config["source_files"]["scientist"].clear()
    dispatcher = provider.RetainedProviderServer(
        control,
        units=object(),
        gpu=object(),
        clock=lambda: now[0],
    )
    server, channel = _channels()
    with server, channel:
        channel.send(metadata.canonical(_request(retained_fixture)))
        with pytest.raises(provider.RetainedProviderError):
            dispatcher.serve_once(server, 1)
        try:
            assert channel.recv(provider.MAX_FRAME_BYTES) == b""
        except ConnectionResetError:
            pass  # Linux may reset a closed sequenced-packet channel.
    assert control_deadline.get() is None and observation_deadline.get() is None


def test_domain_denial_is_generic_and_client_poisoned(retained_fixture):
    control = FakeControl(retained_fixture)

    def deny(*_arguments, **_keywords):
        raise ControlError("history_denied")

    control.read_original_budget = deny
    dispatcher = provider.RetainedProviderServer(control, units=object(), gpu=object())
    broker = object()
    server, channel = _channels()
    with server, channel:
        client = provider.RetainedProviderClient(
            channel,
            authenticator=Authentication(broker),
            expected_broker=broker,
        )
        worker, errors = _worker(lambda: dispatcher.serve_once(server, 1))
        try:
            with pytest.raises(provider.RetainedProviderError, match="provider_denied"):
                client.exchange(_request(retained_fixture))
        finally:
            _join(worker)
        with pytest.raises(provider.RetainedProviderError):
            client.exchange(_request(retained_fixture, sequence=2))
        assert channel.fileno() == -1
    assert errors == []


def test_client_rejects_an_authenticated_different_broker(retained_fixture):
    control = FakeControl(retained_fixture)
    dispatcher = provider.RetainedProviderServer(control, units=object(), gpu=object())
    broker, impostor = object(), object()
    server, channel = _channels()
    with server, channel:
        client = provider.RetainedProviderClient(
            channel,
            authenticator=Authentication(impostor),
            expected_broker=broker,
        )
        # Current expected broker is accepted by this fixture; message sender
        # authentication returns a distinct generation and must still deny.
        client.authenticator.still_current = lambda _peer: True
        worker, errors = _worker(lambda: dispatcher.serve_once(server, 1))
        try:
            with pytest.raises(provider.RetainedProviderError, match="unauthorized"):
                client.exchange(_request(retained_fixture))
        finally:
            _join(worker)
    assert errors == []


def test_server_refuses_replayed_sequence(retained_fixture):
    control = FakeControl(retained_fixture)
    dispatcher = provider.RetainedProviderServer(control, units=object(), gpu=object())
    server, channel = _channels()
    with server, channel:
        channel.send(metadata.canonical(_request(retained_fixture)))
        dispatcher.serve_once(server, 1)
        channel.recv(provider.MAX_FRAME_BYTES)
        with pytest.raises(provider.RetainedProviderError):
            dispatcher.serve_once(server, 1)
    assert len(control.calls) == 1


@pytest.mark.parametrize("flags", [0, socket.MSG_TRUNC, socket.MSG_CTRUNC])
def test_ancillary_rights_closed_even_when_truncated(monkeypatch, flags):
    closed = []
    monkeypatch.setattr(provider.os, "close", closed.append)
    credential = (socket.SOL_SOCKET, socket.SCM_CREDENTIALS, struct.pack("3i", 123, 1000, 1000))
    rights = (socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [901, 902]).tobytes())
    connection = SimpleNamespace(
        settimeout=lambda _seconds: None,
        recvmsg=lambda *_arguments: (b"{}", [credential, rights], flags, None),
    )
    with pytest.raises(provider.RetainedProviderError):
        provider._receive(connection, lambda: 0.0, 3.0)
    assert closed == [901, 902]


@pytest.mark.parametrize("case", ["missing", "multiple", "short", "synthetic", "extra"])
def test_exactly_one_valid_kernel_credential_required(case):
    credential = (socket.SOL_SOCKET, socket.SCM_CREDENTIALS, struct.pack("3i", 123, 1000, 1000))
    ancillary = [credential]
    if case == "missing":
        ancillary = []
    elif case == "multiple":
        ancillary *= 2
    elif case == "short":
        ancillary = [(socket.SOL_SOCKET, socket.SCM_CREDENTIALS, b"short")]
    elif case == "synthetic":
        ancillary = [
            (socket.SOL_SOCKET, socket.SCM_CREDENTIALS, struct.pack("3i", 0, 65534, 65534))
        ]
    else:
        ancillary.append((socket.SOL_SOCKET, 999, b"unexpected"))
    connection = SimpleNamespace(
        settimeout=lambda _seconds: None,
        recvmsg=lambda *_arguments: (b"{}", ancillary, 0, None),
    )
    with pytest.raises(provider.RetainedProviderError):
        provider._receive(connection, lambda: 0.0, 3.0)


def test_inherited_channel_authenticates_actual_child_sender(retained_fixture):
    control = FakeControl(retained_fixture)
    dispatcher = provider.RetainedProviderServer(control, units=object(), gpu=object())
    server, channel = _channels()
    raw = metadata.canonical(_request(retained_fixture))
    script = (
        "import socket,sys; s=socket.socket(fileno=int(sys.argv[1])); "
        "s.settimeout(3); s.send(sys.stdin.buffer.read()); "
        "assert s.recv(131072); s.close()"
    )
    with (
        server,
        channel,
        subprocess.Popen(
            [sys.executable, "-c", script, str(channel.fileno())],
            pass_fds=(channel.fileno(),),
            stdin=subprocess.PIPE,
        ) as child,
    ):
        control.authenticator.pid = child.pid
        assert child.stdin is not None
        child.stdin.write(raw)
        child.stdin.close()
        try:
            dispatcher.serve_once(server, 1)
            assert child.wait(timeout=3) == 0
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=3)
    assert control.authenticator.seen == [(child.pid, os.getuid())]
    assert child.pid != os.getpid()


def test_existing_control_store_reads_without_mutation(cleanup_fixture):
    f = cleanup_fixture
    # Pin this new provider BEFORE admission/capability creation, so all original
    # bindings include the reviewed executable source in the fixture policy.
    config = json.loads(f.policy_path.read_bytes())
    original = Path(provider.__file__)
    copy = f.first.policy.source_root / provider.SOURCE_FILE
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_bytes(original.read_bytes())
    config["source_files"]["scientist"][provider.SOURCE_FILE] = hashlib.sha256(
        copy.read_bytes()
    ).hexdigest()
    f.policy_path.write_bytes(metadata.canonical(config))
    control, bootstrap = integration._enable(f, retained=True, physical=True)
    # The actual broker initializes the child table before serving readbacks.
    # This private fixture uses the same schema producer, without starting work.
    SystemdAOSProfileRuntime(
        f.rig.store.database,
        units=object(),
        gpu=object(),
        work_root=f.rig.store.database.parent / "unused-workers",
        control_store=f.rig.store,
    )
    f.call(control, "cancel", target=integration.TARGET, capability=bootstrap["capability_sha256"])
    capability = control.handle(f.peer, retained_integration._request("capability", "7"))
    proof = control.handle(
        f.peer,
        retained_integration._request("reconcile", "8", capability["capability_sha256"]),
    )["data"]
    control.authenticator = Authentication(f.peer)
    control.server_current = lambda: True
    fixtures = (capability["data"]["capability"], proof)
    dispatcher = provider.RetainedProviderServer(control, units=object(), gpu=object())
    dispatcher.verify_configuration()
    expected_snapshot = control.read_original_physical_snapshot(
        f.peer,
        proof["evidence"]["target"],
        proof["original_budget_witness"]["profile_id"],
        proof["original_budget_witness"]["deployment_digest"],
        expected_capability_sha256=capability["capability_sha256"],
    )

    def rows():
        with closing(f.rig.store._connect()) as connection:
            return list(connection.iterdump())

    before = rows()
    broker = object()
    server, channel = _channels()
    with server, channel:
        client = provider.RetainedProviderClient(
            channel,
            authenticator=Authentication(broker),
            expected_broker=broker,
        )
        worker, errors = _worker(lambda: [dispatcher.serve_once(server, n) for n in (1, 2)])
        try:
            budget = client.exchange(_request(fixtures))
            physical = client.exchange(_request(fixtures, sequence=2, operation="verify_physical"))
        finally:
            _join(worker)
    assert errors == []
    assert budget["data"] == proof["original_budget_witness"]
    assert physical["data"]["evidence"] == proof["evidence"]
    assert physical["data"]["child"] is None
    assert physical["data"] == expected_snapshot
    assert rows() == before
