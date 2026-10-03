"""Owned socket/SQLite acceptance with synthetic authentication, no GPU."""

from __future__ import annotations

import json
import socket
import threading

import pytest
import test_aos_evidence_integration as legacy
from test_aos_control_store import DEPLOYMENT, PROFILE, TARGET

from lab.llm import aos_retained_evidence_transport as codec
from lab.llm.aos_gpu_control import ControlError, canonical

cleanup_fixture = legacy.cleanup_fixture


def _request(op, identifier, capability=None):
    request = legacy._request(op, identifier, capability)
    request.update(
        schema=codec.SCHEMA,
        version=codec.VERSION,
        transport_schema_sha256=codec.EVIDENCE_TRANSPORT_SCHEMA_HASH,
    )
    return request


def _exchange(control, peer, request, capability=None):
    errors = []
    server, client = socket.socketpair()
    control.authenticator.authenticate = lambda _pid, _uid: peer

    def run():
        try:
            with server:
                control.serve_connection(server)
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        with client:
            client.settimeout(3)
            client.sendall(canonical(request) + b"\n")
            raw = bytearray()
            while not raw.endswith(b"\n"):
                part = client.recv(4096)
                assert part, errors
                raw.extend(part)
        return codec.validate_response(bytes(raw[:-1]), request, expected_capability=capability)
    finally:
        worker.join(timeout=4)
        assert not worker.is_alive()
        assert not errors, errors


def test_v3_disabled_by_default_does_not_consume_control_id(cleanup_fixture):
    f = cleanup_fixture
    control, _ = legacy._enable(f)
    before = legacy._count(f)
    with pytest.raises(ControlError, match="unsupported_schema"):
        control.handle(f.peer, _request("capability", "7"))
    assert legacy._count(f) == before


def test_v3_real_socket_canceled_budget_and_idempotent_retry(cleanup_fixture):
    f = cleanup_fixture
    control, bootstrap = legacy._enable(f, retained=True)
    f.call(control, "cancel", target=TARGET, capability=bootstrap["capability_sha256"])
    cap = _exchange(control, f.peer, _request("capability", "7"))
    retained = cap["data"]["capability"]
    request = _request("reconcile", "8", cap["capability_sha256"])
    before = legacy._count(f)
    response = _exchange(control, f.peer, request, retained)
    assert response["ok"]
    data = response["data"]
    assert set(data) == {"evidence", "original_budget_witness"}
    witness = data["original_budget_witness"]
    terminal = json.loads(data["evidence"]["terminal_canonical"])
    assert json.loads(witness["budget_canonical"]) == terminal["original_budget"]
    assert witness["profile_id"] == PROFILE and witness["deployment_digest"] == DEPLOYMENT
    assert terminal["terminal_state"] == "canceled"
    assert terminal["release_outcome"] == "never_admitted"
    assert _exchange(control, f.peer, request, retained) == response
    assert legacy._count(f) == before + 1
    # Same admission and original capability remain usable through explicit v2.
    older = legacy._request("reconcile", "9", cap["capability_sha256"])
    assert legacy._exchange(control, f.peer, older)["data"] == data["evidence"]


def test_v3_bootstrap_infer_capability_is_not_export_authority(cleanup_fixture):
    f = cleanup_fixture
    control, bootstrap = legacy._enable(f, retained=True)
    f.call(control, "cancel", target=TARGET, capability=bootstrap["capability_sha256"])
    before = legacy._count(f)
    with pytest.raises(ControlError, match="capability_mismatch"):
        control.handle(f.peer, _request("reconcile", "8", bootstrap["capability_sha256"]))
    assert legacy._count(f) == before


def test_slow_final_capability_encoding_cannot_publish_after_call_deadline(
    cleanup_fixture, monkeypatch
):
    import struct

    from lab.llm import aos_gpu_control as module

    f = cleanup_fixture
    control, _ = legacy._enable(f, retained=True)
    request = _request("capability", "7")
    now = [0.0]
    original = codec.encode_response

    def slow_encode(*args, **kwargs):
        raw = original(*args, **kwargs)
        now[0] = module.CALL_SECONDS + 1.0
        return raw

    class Connection:
        sent = []

        @staticmethod
        def getsockopt(*_args):
            return struct.pack("3i", 123, 1000, 1000)

        @staticmethod
        def settimeout(_seconds):
            return None

        @staticmethod
        def recv(_size):
            return canonical(request) + b"\n"

        def sendall(self, raw):
            self.sent.append(raw)

    connection = Connection()
    control.authenticator.authenticate = lambda _pid, _uid: f.peer
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(codec, "encode_response", slow_encode)
    with pytest.raises(ControlError, match="deadline_exceeded"):
        control.serve_connection(connection)
    assert connection.sent == []
