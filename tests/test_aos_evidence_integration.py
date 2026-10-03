"""Actual socket/SQLite flow with synthetic authentication; no GPU observation."""

from __future__ import annotations

import hashlib
import json
import socket
import struct
import threading
from contextlib import closing
from pathlib import Path

import pytest
import test_aos_cleanup_authority as cleanup_cases
from test_aos_control_store import CONFIG, DEPLOYMENT, PEER, PROFILE, SCHEMA, TARGET

from lab.llm import aos_evidence_transport as transport
from lab.llm.aos_gpu_control import ControlError, ControlPolicy, LabAOSControl, canonical

cleanup_fixture = cleanup_cases.cleanup_rig


def _request(op, identifier, capability=None):
    return {
        "schema": transport.SCHEMA,
        "version": 2,
        "op": op,
        "control_id": identifier * 32,
        "expected_capability_sha256": capability,
        "profile_id": PROFILE,
        "deployment_digest": DEPLOYMENT,
        "target": dict(TARGET),
        "evidence_schema_sha256": transport.EVIDENCE_SCHEMA_SHA256,
        "transport_schema_sha256": transport.EVIDENCE_TRANSPORT_SCHEMA_HASH,
    }


def _count(f):
    with closing(f.rig.store._connect()) as connection:
        return connection.execute("SELECT COUNT(*) FROM aos_control_idempotency").fetchone()[0]


def _enable(f, *, retained=False, physical=False):
    config = json.loads(f.policy_path.read_bytes())
    config.update(
        evidence_schema_sha256=transport.EVIDENCE_SCHEMA_SHA256,
        evidence_transport_schema_sha256=transport.EVIDENCE_TRANSPORT_SCHEMA_HASH,
    )
    if retained:
        from lab.llm import aos_retained_evidence_transport as retained_codec

        config["retained_evidence_transport_schema_sha256"] = (
            retained_codec.EVIDENCE_TRANSPORT_SCHEMA_HASH
        )
    repository = Path(__file__).resolve().parents[1]
    files = [
        repository / "lab/llm/aos_evidence_transport.py",
        repository / "lab/llm/aos_control_contract_v2.py",
    ]
    files.extend((repository / "lab/llm/contracts/control_v2").glob("*.json"))
    if retained:
        files.append(repository / "lab/llm/aos_retained_evidence_transport.py")
    if physical:
        files.append(repository / "lab/llm/aos_physical_readback.py")
    for original in files:
        name = str(original.relative_to(repository))
        copy = f.first.policy.source_root / name
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_bytes(original.read_bytes())
        config["source_files"]["scientist"][name] = hashlib.sha256(copy.read_bytes()).hexdigest()
    f.policy_path.write_bytes(canonical(config))
    policy = ControlPolicy(
        f.policy_path, profiles=f.first.policy.profiles, source_root=f.first.policy.source_root
    )
    control = LabAOSControl(
        policy=policy,
        authenticator=f.first.authenticator,
        store=f.rig.store,
        server_generation=dict(f.first.server_generation),
        clock=lambda: f.rig.now,
    )
    bootstrap = f.call(control)
    f.rig.admission = control.admit_infer(f.peer, PROFILE, DEPLOYMENT)
    f.rig.store.register_intent(
        PEER,
        TARGET["request_id"],
        TARGET["request_sha256"],
        PROFILE,
        DEPLOYMENT,
        CONFIG,
        SCHEMA,
        200.0,
        {
            "activation_seconds": 10,
            "inference_seconds": 5,
            "total_seconds": 15,
            "queue_seconds": 300,
            "max_output_tokens": 16,
            "context_tokens": 256,
        },
        admission=f.rig.admission,
    )
    return control, bootstrap


def _exchange(control, peer, request):
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
        return transport.validate_response(bytes(raw[:-1]), request)
    finally:
        worker.join(timeout=4)
        assert not worker.is_alive()
        assert not errors, errors


def test_explicit_policy_pin_required_before_any_control_mutation(cleanup_fixture):
    f = cleanup_fixture
    before = _count(f)
    with pytest.raises(ControlError, match="unsupported_schema"):
        f.first.handle(f.peer, _request("capability", "7"))
    assert _count(f) == before


def test_socket_capability_then_canceled_proof_and_exact_retry(cleanup_fixture):
    f = cleanup_fixture
    control, bootstrap = _enable(f)
    f.call(control, "cancel", target=TARGET, capability=bootstrap["capability_sha256"])
    capability_request = _request("capability", "7")
    cap = _exchange(control, f.peer, capability_request)
    assert cap["data"]["admission"] == "denied"
    assert cap["data"]["evidence_schema_sha256"] == transport.EVIDENCE_SCHEMA_SHA256
    assert _exchange(control, f.peer, capability_request) == cap
    request = _request("reconcile", "8", cap["capability_sha256"])
    before = _count(f)
    response = _exchange(control, f.peer, request)
    assert response["ok"] is True
    receipt = json.loads(response["data"]["terminal_canonical"])
    assert receipt["terminal_state"] == "canceled"
    assert receipt["release_outcome"] == "never_admitted"
    assert response["data"]["result_canonical"] is None
    assert _count(f) == before + 1
    assert _exchange(control, f.peer, request) == response
    assert _count(f) == before + 1
    with closing(f.rig.store._connect()) as connection:
        assert (
            response["data"]["terminal_canonical"]
            == connection.execute("SELECT receipt_json FROM aos_control_requests").fetchone()[0]
        )
        assert tuple(
            connection.execute("SELECT active_owner,active_token FROM gpu_turn_state").fetchone()
        ) == (None, 0)


def test_infer_bootstrap_capability_cannot_read_evidence(cleanup_fixture):
    f = cleanup_fixture
    control, bootstrap = _enable(f)
    before = _count(f)
    response = _exchange(
        control, f.peer, _request("reconcile", "8", bootstrap["capability_sha256"])
    )
    assert response["ok"] is False
    assert response["error"]["code"] == "capability_mismatch"
    assert _count(f) == before


def test_inflight_rejected_without_consuming_reconcile_reservation(cleanup_fixture):
    f = cleanup_fixture
    control, _bootstrap = _enable(f)
    cap = _exchange(control, f.peer, _request("capability", "7"))
    before = _count(f)
    response = _exchange(control, f.peer, _request("reconcile", "8", cap["capability_sha256"]))
    assert response["ok"] is False
    assert response["error"]["code"] == "request_conflict"
    assert _count(f) == before


def test_expired_target_capability_denies_export(cleanup_fixture):
    f = cleanup_fixture
    control, bootstrap = _enable(f)
    f.call(control, "cancel", target=TARGET, capability=bootstrap["capability_sha256"])
    cap = _exchange(control, f.peer, _request("capability", "7"))
    f.rig.now += 61
    before = _count(f)
    response = _exchange(control, f.peer, _request("reconcile", "8", cap["capability_sha256"]))
    assert response["error"]["code"] == "capability_expired"
    assert _count(f) == before


def test_response_validation_failure_rolls_back_evidence_quota(cleanup_fixture, monkeypatch):
    f = cleanup_fixture
    control, bootstrap = _enable(f)
    f.call(control, "cancel", target=TARGET, capability=bootstrap["capability_sha256"])
    cap = control.handle(f.peer, _request("capability", "7"))
    before = _count(f)

    def deny(*_args, **_kwargs):
        raise transport.TransportError("invalid_frame")

    monkeypatch.setattr(transport, "encode_response", deny)
    with pytest.raises(transport.TransportError):
        control.handle(f.peer, _request("reconcile", "8", cap["capability_sha256"]))
    assert _count(f) == before


def test_policy_rejects_partial_pin_and_missing_dependency(cleanup_fixture):
    f = cleanup_fixture
    control, _bootstrap = _enable(f)
    config = json.loads(f.policy_path.read_bytes())
    del config["evidence_schema_sha256"]
    f.policy_path.write_bytes(canonical(config))
    with pytest.raises(ControlError, match="unsupported_schema"):
        ControlPolicy(
            f.policy_path, profiles=control.policy.profiles, source_root=control.policy.source_root
        )
    config["evidence_schema_sha256"] = transport.EVIDENCE_SCHEMA_SHA256
    del config["source_files"]["scientist"]["lab/llm/aos_evidence_transport.py"]
    f.policy_path.write_bytes(canonical(config))
    with pytest.raises(ControlError, match="invalid_frame"):
        ControlPolicy(
            f.policy_path, profiles=control.policy.profiles, source_root=control.policy.source_root
        )


def test_expiry_during_final_policy_read_rolls_back(cleanup_fixture, monkeypatch):
    f = cleanup_fixture
    control, bootstrap = _enable(f)
    f.call(control, "cancel", target=TARGET, capability=bootstrap["capability_sha256"])
    cap = control.handle(f.peer, _request("capability", "7"))
    before = _count(f)
    phase = {"written": False, "reads": 0}
    remember = f.rig.store._remember_frame
    verify = control.policy.verify_policy_hash

    def after_write(*args, **kwargs):
        result = remember(*args, **kwargs)
        phase["written"] = True
        return result

    def slow_read():
        verify()
        if phase["written"]:
            phase["reads"] += 1
            if phase["reads"] == 3:
                f.rig.now += 61

    monkeypatch.setattr(f.rig.store, "_remember_frame", after_write)
    monkeypatch.setattr(control.policy, "verify_policy_hash", slow_read)
    with pytest.raises(ControlError, match="capability_expired"):
        control.handle(f.peer, _request("reconcile", "8", cap["capability_sha256"]))
    assert _count(f) == before


def test_retired_acl_without_reconcile_does_not_persist_capability(cleanup_fixture):
    f = cleanup_fixture
    control, _bootstrap = _enable(f)
    original = f.rig.store.cleanup_original(PEER, TARGET, PROFILE, DEPLOYMENT)
    config = json.loads(f.policy_path.read_bytes())
    config["enabled"] = False
    config["cleanup_targets"] = [f.entry(original, ["status"])]
    f.policy_path.write_bytes(canonical(config))
    control.policy = ControlPolicy(
        f.policy_path, profiles=control.policy.profiles, source_root=control.policy.source_root
    )
    before = _count(f)
    with closing(f.rig.store._connect()) as connection:
        grants_before = connection.execute("SELECT COUNT(*) FROM aos_cleanup_grants").fetchone()[0]
    with pytest.raises(transport.TransportError):
        control.handle(f.peer, _request("capability", "7"))
    assert _count(f) == before
    with closing(f.rig.store._connect()) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM aos_cleanup_grants").fetchone()[0]
            == grants_before
        )


def test_retired_profile_reads_original_proof_with_explicit_reconcile_acl(cleanup_fixture):
    f = cleanup_fixture
    control, bootstrap = _enable(f)
    canceled = f.call(control, "cancel", target=TARGET, capability=bootstrap["capability_sha256"])
    original = f.rig.store.cleanup_original(PEER, TARGET, PROFILE, DEPLOYMENT)
    config = json.loads(f.policy_path.read_bytes())
    config["enabled"] = False
    config["cleanup_targets"] = [f.entry(original, ["reconcile"])]
    f.policy_path.write_bytes(canonical(config))
    control.policy = ControlPolicy(
        f.policy_path, profiles=control.policy.profiles, source_root=control.policy.source_root
    )
    cap = _exchange(control, f.peer, _request("capability", "7"))
    assert cap["data"]["reason_code"] == "cleanup_only"
    grant = cap["data"]["capability"]["cleanup_grant"]
    assert grant["original_admission_binding"] == original["original_admission_binding"]
    assert grant["operations"] == ["reconcile"]
    response = _exchange(control, f.peer, _request("reconcile", "8", cap["capability_sha256"]))
    assert (
        json.loads(response["data"]["terminal_canonical"]) == canceled["data"]["terminal_receipt"]
    )
    assert (
        control._capabilities[bootstrap["capability_sha256"]]["policy_sha256"]
        != control.policy.sha256
    )


def test_revocation_during_last_socket_peer_check_does_not_publish(cleanup_fixture, monkeypatch):
    f = cleanup_fixture
    control, bootstrap = _enable(f)
    f.call(control, "cancel", target=TARGET, capability=bootstrap["capability_sha256"])
    cap = control.handle(f.peer, _request("capability", "7"))
    request = _request("reconcile", "8", cap["capability_sha256"])
    phase = {"handled": False}
    original_handle = control.handle

    def handle(*args):
        response = original_handle(*args)
        phase["handled"] = True
        return response

    def current(_peer):
        if phase["handled"]:
            config = json.loads(f.policy_path.read_bytes())
            config["enabled"] = False
            f.policy_path.write_bytes(canonical(config))
        return True

    class Connection:
        sent = []

        @staticmethod
        def getsockopt(*_args):
            return struct.pack("3i", 123, 1000, 1000)

        @staticmethod
        def settimeout(_timeout):
            return None

        @staticmethod
        def recv(_size):
            return canonical(request) + b"\n"

        def sendall(self, raw):
            self.sent.append(raw)

    connection = Connection()
    monkeypatch.setattr(control, "handle", handle)
    monkeypatch.setattr(control.authenticator, "still_current", current)
    control.authenticator.authenticate = lambda _pid, _uid: f.peer
    with pytest.raises(ControlError):
        control.serve_connection(connection)
    assert connection.sent == []
