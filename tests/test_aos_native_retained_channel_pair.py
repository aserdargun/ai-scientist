"""Actual bootstrap/client CPU interoperability; synthetic authority, no GPU.

Only socket ancestry is redirected to a temporary owned listener. Both wire
implementations, descriptor bytes, SCM credentials and FD handoff are real.
Generation metadata and retained budget contents are explicitly synthetic.
"""

import hashlib
import os
import socket
import threading
import time
from dataclasses import asdict
from pathlib import Path

import pytest
import test_aos_retained_provider as fixtures

from lab.llm import aos_evidence_transport, aos_retained_evidence_transport
from lab.llm import aos_retained_channel as server
from lab.llm import aos_retained_provider as provider
from lab.llm.aos_gpu_broker import PeerGeneration
from scripts import aos_native_retained_channel as client

retained_fixture = fixtures.retained_fixture
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def pair(tmp_path, monkeypatch, retained_fixture):
    control = fixtures.FakeControl(retained_fixture)
    caller = PeerGeneration(
        uid=os.getuid(),
        pid=os.getpid(),
        start_ticks=123,
        boot_id="synthetic-pair-boot",
        unit="synthetic-aos.service",
        invocation_id="a" * 32,
        control_group="/synthetic/aos",
        parent_pid=os.getpid(),
        parent_start_ticks=123,
    )
    broker = PeerGeneration(
        uid=os.getuid(),
        pid=os.getpid(),
        start_ticks=456,
        boot_id="synthetic-pair-boot",
        unit=client.BROKER_UNIT,
        invocation_id="b" * 32,
        control_group="/synthetic/broker",
    )
    control.peer = caller
    control.authenticator = fixtures.Authentication(caller)
    control.server_generation = {
        key: value
        for key, value in asdict(broker).items()
        if key not in {"parent_pid", "parent_start_ticks"}
    }
    source_paths = (server.SOURCE_FILE, provider.SOURCE_FILE, "lab/llm/aos_physical_readback.py")
    reviewed_sources = {
        path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in source_paths
    }
    control.policy.config.update(
        caller_unit=caller.unit,
        source_files={"scientist": reviewed_sources.copy()},
        evidence_schema_sha256=aos_evidence_transport.EVIDENCE_SCHEMA_SHA256,
        evidence_transport_schema_sha256=aos_evidence_transport.EVIDENCE_TRANSPORT_SCHEMA_HASH,
        retained_evidence_transport_schema_sha256=aos_retained_evidence_transport.EVIDENCE_TRANSPORT_SCHEMA_HASH,
    )

    def verify_policy():
        # Independent immutable CPU review pins; no enabled runtime policy is
        # written or loaded. Production bootstrap must actually call verify().
        configured = control.policy.config["source_files"]["scientist"]
        if configured != reviewed_sources or any(
            hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != expected
            for path, expected in reviewed_sources.items()
        ):
            raise ValueError("synthetic reviewed source differs")

    control.policy.verify = verify_policy
    control.server_current = lambda: True
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    path = tmp_path / "cpu-pair.sock"
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    listener.bind(str(path))
    listener.listen(1)
    listener.settimeout(0.3)
    os.chmod(path, 0o600)

    def ancestry(selected):
        assert selected == path
        info = path.lstat()
        assert info.st_uid == os.getuid()
        return info.st_dev, info.st_ino

    monkeypatch.setattr(client, "_ancestry", ancestry)
    bootstrap = server.RetainedChannelBootstrap(control, object(), object())
    arguments = dict(
        authenticator=fixtures.Authentication(broker),
        expected_peer=broker,
        caller_generation=asdict(caller),
        server_generation=control.server_generation.copy(),
        descriptor_sha256=client.DESCRIPTOR_SHA256,
        verify_current=lambda: None,
        timeout_seconds=0.3,
    )
    yield control, bootstrap, listener, path, arguments, retained_fixture
    bootstrap.close()
    listener.close()


def start(bootstrap, listener):
    admitted, errors = [], []

    def serve():
        try:
            with listener.accept()[0] as connection:
                admitted.append(True)
                bootstrap.serve(connection, deadline=time.monotonic() + 0.3)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=serve)
    thread.start()
    return thread, admitted, errors


def join(thread):
    thread.join(1)
    assert not thread.is_alive()


def test_real_bootstrap_native_client_and_successive_provider_reads(pair):
    control, bootstrap, listener, path, arguments, retained = pair
    assert len(arguments["caller_generation"]) == 9
    assert len(arguments["server_generation"]) == 7
    assert (
        hashlib.sha256(server.DESCRIPTOR_PATH.read_bytes()).hexdigest() == client.DESCRIPTOR_SHA256
    )
    thread, admitted, errors = start(bootstrap, listener)
    try:
        with client.open_retained_provider_channel(path, **arguments) as endpoint:
            join(thread)
            assert admitted == [True] and errors == []
            assert not endpoint.get_inheritable()
            packet_client = provider.RetainedProviderClient(
                endpoint,
                authenticator=arguments["authenticator"],
                expected_broker=arguments["expected_peer"],
                timeout_seconds=0.3,
            )
            budget = packet_client.exchange(fixtures._request(retained))
            physical = packet_client.exchange(
                fixtures._request(retained, sequence=2, operation="verify_physical")
            )
            assert budget["data"] == retained[1]["original_budget_witness"]
            assert physical["data"]["arbiter"]["active_owner"] == "lab"
            assert [call[0] for call in control.calls] == ["budget", "physical"]
    finally:
        bootstrap.close()
        join(thread)


@pytest.mark.parametrize("fault", ["disabled", "source_missing", "source_pin", "schema_pin"])
def test_real_pair_denies_unreviewed_policy_source_or_schema(pair, fault):
    control, bootstrap, listener, path, arguments, _retained = pair
    if fault == "disabled":
        control.policy.config["enabled"] = False
    elif fault == "source_missing":
        del control.policy.config["source_files"]["scientist"][server.SOURCE_FILE]
    elif fault == "source_pin":
        control.policy.config["source_files"]["scientist"][server.SOURCE_FILE] = "0" * 64
    else:
        control.policy.config["retained_evidence_transport_schema_sha256"] = "0" * 64
    thread, admitted, errors = start(bootstrap, listener)
    with pytest.raises(client.NativeRetainedChannelError, match="channel denied"):
        client.open_retained_provider_channel(path, **arguments)
    join(thread)
    assert admitted == [True]
    assert len(errors) == 1 and isinstance(errors[0], server.RetainedChannelError)
    assert control.calls == [] and bootstrap._slot is None


def test_real_pair_rejects_mismatched_nine_field_caller_hash(pair):
    control, bootstrap, listener, path, arguments, _retained = pair
    arguments["caller_generation"]["boot_id"] = "different-synthetic-boot"
    thread, admitted, errors = start(bootstrap, listener)
    with pytest.raises(client.NativeRetainedChannelError, match="channel denied"):
        client.open_retained_provider_channel(path, **arguments)
    join(thread)
    assert admitted == [True]
    assert len(errors) == 1 and isinstance(errors[0], server.RetainedChannelError)
    assert control.calls == [] and bootstrap._slot is None


@pytest.mark.parametrize("field,value", [("parent_pid", 999999), ("parent_start_ticks", 124)])
def test_parent_not_main_pid_denied_before_control_connection(pair, field, value):
    control, bootstrap, listener, path, arguments, _retained = pair
    arguments["caller_generation"][field] = value
    with pytest.raises(client.NativeRetainedChannelError, match="generation differs"):
        client.open_retained_provider_channel(path, **arguments)
    listener.settimeout(0.01)
    with pytest.raises(TimeoutError):
        listener.accept()
    assert control.calls == [] and bootstrap._slot is None
