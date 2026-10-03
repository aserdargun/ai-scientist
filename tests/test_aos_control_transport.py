"""Pending root CPU checks for an unagreed, disabled control candidate."""

from __future__ import annotations

import concurrent.futures
import socket
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from aos_admission_fixture import BOOT, output_pin

from lab.llm.aos_gpu_broker import BrokerProtocolError, PeerGeneration, _decode_request
from lab.llm.aos_gpu_control import (
    SCHEMA,
    ControlError,
    LabAOSControl,
    canonical,
    decode_request,
)
from lab.llm.aos_gpu_service import WORKERS, _control_listener


def test_control_version_and_scope_do_not_extend_infer_wire() -> None:
    infer = {
        "version": 1,
        "op": "infer",
        "request_id": "a" * 32,
        "profile_id": "aos.decider.turn.v1",
        "deployment_digest": "b" * 64,
        "payload": {},
    }
    _decode_request(canonical(infer))
    with pytest.raises(BrokerProtocolError):
        _decode_request(canonical({**infer, "op": "cancel"}))
    with pytest.raises(BrokerProtocolError):
        _decode_request(canonical({**infer, "expected_capability_sha256": "c" * 64}))
    request = {
        "schema": SCHEMA,
        "version": 1,
        "op": "capability",
        "control_id": "a" * 32,
        "expected_capability_sha256": None,
        "profile_id": infer["profile_id"],
        "deployment_digest": infer["deployment_digest"],
        "target": None,
    }
    assert decode_request(canonical(request)) == request
    for wrong in (True, 1.0, 2, "1"):
        with pytest.raises(ControlError, match="unsupported_version"):
            decode_request(canonical({**request, "version": wrong}))
    with pytest.raises(ControlError, match="invalid_frame"):
        decode_request(canonical({**request, "drained": True}))


def test_capability_is_current_generation_bound_and_disabled_policy_denies_infer() -> None:
    peer = PeerGeneration(1000, 201, 1, BOOT, "swapp-aos-test.service", "a" * 32, "/aos", 200, 1)
    profile = SimpleNamespace(profile_id="aos.decider.turn.v1", deployment_digest="b" * 64)
    now = [1.0]

    class Policy:
        config = {
            "enabled": False,
            "history_reconcile": False,
            "source_files": {"aos": {"test": "c" * 64}, "scientist": {"test": "d" * 64}},
        }
        sha256 = "e" * 64

        @staticmethod
        def verify_policy_hash():
            return None

        @staticmethod
        def bound_profile(_peer, _profile_id, _deployment):
            return profile

        @staticmethod
        def _pin(_profile):
            return {
                "deployment_digest": profile.deployment_digest,
                "manifest_sha256": "f" * 64,
                "config_sha256": "0" * 64,
                "response_schema_sha256": "1" * 64,
                "output_contract": output_pin(),
            }

    control = LabAOSControl(
        policy=Policy(),
        authenticator=SimpleNamespace(still_current=lambda _peer: True),
        store=SimpleNamespace(remember_control=lambda *_args: None),
        server_generation={
            "uid": 1000,
            "boot_id": BOOT,
            "pid": 300,
            "start_ticks": 1,
            "unit": "swapp-lab-gpu-fixture.service",
            "invocation_id": "b" * 32,
            "control_group": "/fixture/lab",
        },
        clock=lambda: now[0],
    )
    request = {
        "schema": SCHEMA,
        "version": 1,
        "op": "capability",
        "control_id": "a" * 32,
        "expected_capability_sha256": None,
        "profile_id": profile.profile_id,
        "deployment_digest": profile.deployment_digest,
        "target": None,
    }
    receipt = control.handle(peer, request)
    assert receipt["data"]["admission"] == "denied"
    with pytest.raises(ControlError, match="unauthorized"):
        control.admit_infer(peer, profile.profile_id, profile.deployment_digest)
    with pytest.raises(ControlError, match="capability_mismatch"):
        control._capability(
            replace(peer, invocation_id="9" * 32),
            receipt["capability_sha256"],
            profile.profile_id,
            profile.deployment_digest,
        )
    now[0] = 62.0
    with pytest.raises(ControlError, match="capability_expired"):
        control._capability(
            peer, receipt["capability_sha256"], profile.profile_id, profile.deployment_digest
        )


def test_control_listener_progresses_while_all_infer_workers_are_occupied(tmp_path) -> None:
    ready = threading.Barrier(WORKERS + 1)
    release = threading.Event()
    stopping = threading.Event()

    def long_infer():
        ready.wait(timeout=3)
        assert release.wait(timeout=10)

    class Control:
        @staticmethod
        def serve_connection(connection, *, deadline):
            assert deadline > 0
            connection.sendall(b"control-ready\n")

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    address = str(tmp_path / "control.sock")
    server.bind(address)
    server.listen(8)
    server.settimeout(0.1)
    listener = threading.Thread(target=_control_listener, args=(server, Control(), stopping))
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(long_infer) for _ in range(WORKERS)]
        try:
            ready.wait(timeout=3)
            listener.start()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2)
                client.connect(address)
                assert client.recv(64) == b"control-ready\n"
            assert all(not future.done() for future in futures)
        finally:
            stopping.set()
            server.close()
            release.set()
            listener.join(timeout=3)
        assert not listener.is_alive()
        for future in futures:
            future.result(timeout=3)
