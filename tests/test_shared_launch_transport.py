"""Real Unix CPU exchanges; generation checks are explicit fixtures, not systemd acceptance."""

from __future__ import annotations

import array
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import asdict

import pytest
from test_shared_launch_ledger import Rig, intent

from lab.llm import shared_launch_transport as wire
from lab.llm.shared_launch_ledger import LaunchStatus, ServiceGeneration

REQUEST = "1" * 32
BINDING = "2" * 64
INTENT = "3" * 64


def deadline():
    return time.clock_gettime(time.CLOCK_BOOTTIME) + 2


def broker(**updates):
    return ServiceGeneration.model_validate(
        {
            "pid": os.getpid(),
            "uid": os.getuid(),
            "start_ticks": 42,
            "boot_id": "00000000-0000-0000-0000-000000000001",
            "unit": "fixture.service",
            "invocation_id": "a" * 32,
            "control_group": "/fixture.service",
            **updates,
        }
    )


def current(expected, original_deadline):
    assert expected == broker()
    assert original_deadline > time.clock_gettime(time.CLOCK_BOOTTIME)
    return True  # Explicit fixture; production requires the actual process/systemd checker.


def pair():
    endpoints = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    for endpoint in endpoints:
        endpoint.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    return endpoints


def worker(callback):
    failures = []

    def run():
        try:
            callback()
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, failures


def joined(thread):
    thread.join(4)
    assert not thread.is_alive()


class Controller:
    contract_sha256 = wire.WIRE_SCHEMA_SHA256

    def __init__(self):
        self.calls = []
        self.consumed = False

    def handle(self, operation, request_id, binding_sha256, intent_sha256, **context):
        self.calls.append((operation, request_id, binding_sha256, intent_sha256, context))
        fresh = operation == "claim" and not self.consumed
        self.consumed |= operation == "claim"
        return {
            "status": asdict(
                LaunchStatus(
                    request_id,
                    binding_sha256,
                    "consumed" if self.consumed else "reviewed",
                    self.consumed,
                    self.consumed,
                    False,
                )
            ),
            "consumed_now": fresh,
        }


def exchange_client(controller, *, checker=current, serve=None):
    threads, failures = [], []

    def factory(original_deadline):
        server, client = pair()
        producer = wire.SharedLaunchServer(
            controller, expected_broker=broker(), broker_current=current
        )
        thread, errors = worker(
            lambda: (
                serve(server, original_deadline)
                if serve
                else producer.serve_once(server, deadline=original_deadline)
            )
        )
        threads.append(thread)
        failures.append(errors)
        return client

    client = wire.SharedLaunchClient(factory, expected_broker=broker(), broker_current=checker)
    return client, threads, failures


def request(operation="verify", **updates):
    value = {
        "schema": wire.SCHEMA,
        "version": 1,
        "schema_sha256": wire.WIRE_SCHEMA_SHA256,
        "nonce": "f" * 32,
        "operation": operation,
        "request_id": REQUEST,
        "binding_sha256": BINDING,
        "broker_generation_sha256": wire._generation_hash(broker()),
    }
    if operation == "claim":
        value["intent_sha256"] = INTENT
    return {**value, **updates}


def test_real_credentials_all_operations_and_original_deadline():
    controller = Controller()
    client, threads, errors = exchange_client(controller)
    for operation in ("issue", "verify", "status", "claim", "revoke"):
        original = deadline()
        result = client.request(
            operation, REQUEST, BINDING, INTENT if operation == "claim" else None, deadline=original
        )
        assert result.consumed_now == (operation == "claim")
        context = controller.calls[-1][-1]
        assert context == {"peer_pid": os.getpid(), "peer_uid": os.getuid(), "deadline": original}
    for thread in threads:
        joined(thread)
    assert all(error == [] for error in errors)


def test_repeated_claim_is_status_only_and_aos_callback_denies():
    controller = Controller()
    client, threads, errors = exchange_client(controller)
    assert client.activation_claimer(REQUEST, BINDING, INTENT, deadline=deadline()) is None
    assert client.claim(REQUEST, BINDING, INTENT, deadline=deadline()).consumed_now is False
    with pytest.raises(wire.SharedLaunchTransportError):
        client.require_fresh_claim(REQUEST, BINDING, INTENT, deadline=deadline())
    with pytest.raises(wire.SharedLaunchTransportError):
        client.activation_claimer(REQUEST, BINDING, INTENT, deadline=deadline())
    for thread in threads:
        joined(thread)
    assert all(error == [] for error in errors)


@pytest.mark.parametrize(
    "raw",
    [
        wire.encoding.canonical(request(version=True)) + b"\n",
        wire.encoding.canonical(request(schema_sha256="0" * 64)) + b"\n",
        wire.encoding.canonical(request(marker="/peer/selected")) + b"\n",
        wire.encoding.canonical(request(intent_sha256=INTENT)) + b"\n",
        wire.encoding.canonical(request(operation="claim")).replace(INTENT.encode(), b"bad")
        + b"\n",
        b'{"version":1,"version":1}\n',
        b' {"version":1}\n',
        wire.encoding.canonical(request()) + b"\n{}\n",
        b"x" * (wire.MAX_BYTES + 2),
    ],
)
def test_malformed_or_extra_frames_never_reach_controller(raw):
    controller = Controller()
    server, client = pair()
    producer = wire.SharedLaunchServer(controller, expected_broker=broker(), broker_current=current)
    thread, errors = worker(lambda: producer.serve_once(server, deadline=deadline()))
    with client:
        try:
            client.sendall(raw)
            client.shutdown(socket.SHUT_WR)
        except OSError:
            pass
    joined(thread)
    assert len(errors) == 1 and isinstance(errors[0], wire.SharedLaunchTransportError)
    assert controller.calls == []


def test_delayed_second_frame_is_denied_before_dispatch():
    controller = Controller()
    server, client = pair()
    producer = wire.SharedLaunchServer(controller, expected_broker=broker(), broker_current=current)
    thread, errors = worker(lambda: producer.serve_once(server, deadline=deadline()))
    with client:
        client.sendall(wire.encoding.canonical(request()) + b"\n")
        time.sleep(0.02)
        client.sendall(b"{}\n")
        client.shutdown(socket.SHUT_WR)
    joined(thread)
    assert len(errors) == 1
    assert not controller.calls


@pytest.mark.parametrize(
    "changed",
    [
        "nonce",
        "request_id",
        "binding_sha256",
        "operation",
        "schema_sha256",
        "broker_generation_sha256",
        "version",
        "status",
        "consumed_now",
        "extra",
    ],
)
def test_client_rejects_uncorrelated_or_invalid_response(changed):
    def serve(endpoint, original_deadline):
        with endpoint:
            peer = wire._prepare(endpoint)
            req = wire._read(endpoint, peer, original_deadline)
            response = {**req, **Controller().handle("verify", REQUEST, BINDING, None)}
            if changed == "status":
                response["status"]["extra"] = True
            elif changed == "consumed_now":
                response[changed] = True  # Never fresh for verify.
            elif changed == "version":
                response[changed] = True  # Equal to 1 in Python; still invalid wire type.
            else:
                response[changed] = "0" * 32
            wire._send(endpoint, response, original_deadline)

    client, threads, errors = exchange_client(Controller(), serve=serve)
    with pytest.raises(wire.SharedLaunchTransportError):
        client.request("verify", REQUEST, BINDING, deadline=deadline())
    joined(threads[0])
    assert errors == [[]]


def test_fd_ancillary_is_closed_and_denied(tmp_path):
    controller = Controller()
    server, client = pair()
    producer = wire.SharedLaunchServer(controller, expected_broker=broker(), broker_current=current)
    count_before = len(os.listdir("/proc/self/fd"))
    thread, errors = worker(lambda: producer.serve_once(server, deadline=deadline()))
    with client, (tmp_path / "ordinary").open("w") as stream:
        client.sendmsg(
            [wire.encoding.canonical(request()) + b"\n"],
            [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [stream.fileno()]))],
        )
        client.shutdown(socket.SHUT_WR)
    joined(thread)
    assert len(errors) == 1 and not controller.calls
    assert len(os.listdir("/proc/self/fd")) == count_before - 2


def test_sender_child_cannot_inherit_same_uid_socket_authority():
    controller = Controller()
    server, client = pair()
    producer = wire.SharedLaunchServer(controller, expected_broker=broker(), broker_current=current)
    thread, errors = worker(lambda: producer.serve_once(server, deadline=deadline()))
    script = """import socket,sys
s=socket.socket(fileno=int(sys.argv[1]))
s.sendall(bytes.fromhex(sys.argv[2]));s.shutdown(socket.SHUT_WR)
"""
    with client:
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(client.fileno()),
                (wire.encoding.canonical(request()) + b"\n").hex(),
            ],
            pass_fds=(client.fileno(),),
            check=False,
            timeout=3,
        )
    joined(thread)
    assert child.returncode == 0
    assert len(errors) == 1 and not controller.calls


def test_client_pins_kernel_broker_pid_and_current_generation():
    server, endpoint = pair()
    try:
        client = wire.SharedLaunchClient(
            lambda _: endpoint,
            expected_broker=broker(pid=os.getpid() + 1),
            broker_current=lambda *_: True,
        )
        with pytest.raises(wire.SharedLaunchTransportError):
            client.request("verify", REQUEST, BINDING, deadline=deadline())
    finally:
        server.close()
    controller = Controller()
    client, threads, errors = exchange_client(controller, checker=lambda *_: False)
    with pytest.raises(wire.SharedLaunchTransportError):
        client.request("verify", REQUEST, BINDING, deadline=deadline())
    joined(threads[0])
    assert len(errors[0]) == 1 and not controller.calls


def test_final_read_current_check_cannot_return_after_generation_drift():
    observations = []

    def checker(*_):
        observations.append(1)
        return len(observations) < 3

    client, threads, errors = exchange_client(Controller(), checker=checker)
    with pytest.raises(wire.SharedLaunchTransportError):
        client.request("verify", REQUEST, BINDING, deadline=deadline())
    joined(threads[0])
    assert len(observations) == 3 and errors == [[]]


def test_original_deadline_includes_no_eof_and_controller_delay():
    controller = Controller()
    server, client = pair()
    producer = wire.SharedLaunchServer(controller, expected_broker=broker(), broker_current=current)
    original = time.clock_gettime(time.CLOCK_BOOTTIME) + 0.05
    thread, errors = worker(lambda: producer.serve_once(server, deadline=original))
    with client:
        client.sendall(wire.encoding.canonical(request()) + b"\n")
        joined(thread)  # No EOF cannot supply a complete request.
    assert len(errors) == 1 and not controller.calls

    class Slow(Controller):
        def handle(self, *args, **kwargs):
            result = super().handle(*args, **kwargs)
            time.sleep(0.1)
            return result

    client, threads, errors = exchange_client(Slow())
    with pytest.raises(wire.SharedLaunchTransportError):
        client.request(
            "verify", REQUEST, BINDING, deadline=time.clock_gettime(time.CLOCK_BOOTTIME) + 0.05
        )
    joined(threads[0])
    assert len(errors[0]) == 1


def test_dropped_claim_ack_stays_consumed_in_real_fixture_ledger(tmp_path):
    rig = Rig(tmp_path)
    grant, marker = rig.grant, intent(rig.grant)
    rig.ledger.issue(grant)

    class LedgerController:
        contract_sha256 = wire.WIRE_SCHEMA_SHA256

        def handle(self, operation, request_id, binding_sha256, intent_sha256, **_context):
            assert request_id == grant.request_id and binding_sha256 == grant.sha256()
            if operation == "claim":
                assert intent_sha256 == marker.intent_sha256
                result = rig.ledger.claim(grant, marker)
                return {"status": asdict(result.status), "consumed_now": result.consumed_now}
            return {"status": asdict(rig.ledger.status(grant)), "consumed_now": False}

    server, endpoint = pair()
    producer = wire.SharedLaunchServer(
        LedgerController(), expected_broker=broker(), broker_current=current
    )
    endpoint.sendall(
        wire.encoding.canonical(
            request(
                "claim",
                request_id=grant.request_id,
                binding_sha256=grant.sha256(),
                intent_sha256=marker.intent_sha256,
            )
        )
        + b"\n"
    )
    endpoint.shutdown(socket.SHUT_WR)
    endpoint.close()  # Crash/lost ACK; no launch adapter may retry consumption automatically.
    with pytest.raises(wire.SharedLaunchTransportError):
        producer.serve_once(server, deadline=deadline())
    assert rig.ledger.status(grant).consumed
    client, threads, errors = exchange_client(LedgerController())
    with pytest.raises(wire.SharedLaunchTransportError):
        client.activation_claimer(
            grant.request_id, grant.sha256(), marker.intent_sha256, deadline=deadline()
        )
    joined(threads[0])
    assert errors == [[]]


def test_truncated_or_extra_ancillary_closes_all_decoded_rights(tmp_path):
    with (tmp_path / "fd").open("w") as source:
        unexpected = os.dup(source.fileno())
        ancillary = [
            (socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [unexpected]).tobytes()),
            (
                socket.SOL_SOCKET,
                socket.SCM_CREDENTIALS,
                struct.pack("3i", os.getpid(), os.getuid(), os.getgid()),
            ),
        ]
        with pytest.raises(wire.SharedLaunchTransportError):
            wire._credentials(ancillary, socket.MSG_CTRUNC)
        with pytest.raises(OSError):
            os.fstat(unexpected)
        with pytest.raises(wire.SharedLaunchTransportError):
            wire._credentials(ancillary[1:] * 2, 0)


def test_client_rejects_reply_sent_by_same_uid_child_with_broker_fd():
    def serve(endpoint, original_deadline):
        with endpoint:
            peer = wire._prepare(endpoint)
            req = wire._read(endpoint, peer, original_deadline)
            response = {**req, **Controller().handle("verify", REQUEST, BINDING, None)}
            script = """import socket,sys
s=socket.socket(fileno=int(sys.argv[1]))
s.sendall(bytes.fromhex(sys.argv[2]));s.shutdown(socket.SHUT_WR)
"""
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(endpoint.fileno()),
                    (wire.encoding.canonical(response) + b"\n").hex(),
                ],
                pass_fds=(endpoint.fileno(),),
                check=False,
                timeout=3,
            )
            assert result.returncode == 0

    client, threads, errors = exchange_client(Controller(), serve=serve)
    with pytest.raises(wire.SharedLaunchTransportError):
        client.request("verify", REQUEST, BINDING, deadline=deadline())
    joined(threads[0])
    assert errors == [[]]
    assert len(threads) == 1  # No connection retry after an unauthenticated reply.


def test_server_denies_authority_with_different_contract_before_dispatch():
    controller = Controller()
    controller.contract_sha256 = "0" * 64
    with pytest.raises(wire.SharedLaunchTransportError):
        wire.SharedLaunchServer(controller, expected_broker=broker(), broker_current=current)
    assert controller.calls == []
