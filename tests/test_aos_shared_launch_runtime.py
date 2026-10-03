"""CPU-only runtime review and local socket checks; no real service/GPU authority."""

from __future__ import annotations

import json
import os
import socket
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from test_shared_launch_ledger import binding as fixture_binding

from lab.llm.shared_launch_ledger import LaunchBinding
from lab.llm.shared_launch_transport import WIRE_SCHEMA_SHA256, SharedLaunchTransportError
from scripts import aos_shared_launch_runtime as runtime

INPUT_SHA = "a" * 64
INTENT_SHA = "b" * 64
OLD_WIRE_SHA = "842fe08b2f7f7dbb1f0d0bcf000a5d3029eb4335114da800324d0e34952478f6"


@pytest.fixture
def case(tmp_path, monkeypatch):
    clock = SimpleNamespace(boot=150.0, mono=50.0)
    monkeypatch.setattr(runtime.time, "clock_gettime", lambda _: clock.boot)
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock.mono)
    original = fixture_binding()
    pins = original.pins.model_copy(
        update={"reviewed_launch_input_sha256": INPUT_SHA, "contract_sha256": WIRE_SCHEMA_SHA256}
    )
    broker = original.broker.model_copy(update={"unit": "swapp-lab-gpu-broker.service"})
    grant = original.model_copy(update={"broker": broker, "pins": pins})
    hint = tmp_path / "binding.json"
    socket_path = tmp_path / "wire.sock"
    config_path = str(tmp_path / "input.json")
    review = {
        "schema": "scientist.shared-launch-runtime.v1",
        "request_id": grant.request_id,
        "binding_path": str(hint),
        "transport_schema_sha256": WIRE_SCHEMA_SHA256,
        "socket_path": str(socket_path),
    }
    cfg = {
        "caller_unit": runtime.SHARED_UNIT,
        "shared_launch_runtime": review,
        "factory_arguments": {
            "source_roots": {"scientist": pins.scientist_root, "aos": pins.aos_root},
            "config_files": {config_path: INPUT_SHA},
        },
        "shared_scope": {
            "plan_sha256": grant.plan_sha256,
            "provision_sha256": grant.provision_sha256,
        },
    }
    verified = {
        name: getattr(grant, name)
        for name in (
            "workspace",
            "workspace_device",
            "workspace_inode",
            "workspace_uid",
            "app_session",
            "boot_id",
            "issued_boottime",
            "expires_boottime",
        )
    }
    verified.update(
        session_directory=str(tmp_path / "session"),
        activation_config_paths=frozenset({config_path}),
        intent_sha256=INTENT_SHA,
    )
    captured = {
        "server_generation": broker.model_dump(mode="json"),
        "caller_generation": {"unit": runtime.SHARED_UNIT, "pid": os.getpid()},
    }
    bindings = {"first": deepcopy(captured), "second": deepcopy(captured)}
    value = SimpleNamespace(
        clock=clock,
        grant=grant,
        hint=hint,
        socket_path=socket_path,
        cfg=cfg,
        verified=verified,
        bindings=bindings,
    )

    def write(document=None):
        hint.write_text(runtime.canonical(document or grant.model_dump(mode="json", by_alias=True)))
        hint.chmod(0o600)

    def build(deadline=52.0):
        return runtime.build_shared_launch_runtime(cfg, bindings, INPUT_SHA, verified, deadline)

    value.write, value.build = write, build
    write()
    return value


def test_acyclic_hint_builds_against_independently_captured_broker(case):
    result = case.build()
    assert result.binding == case.grant
    assert result.review.request_id == case.grant.request_id
    assert result.intent_sha256 == INTENT_SHA
    assert result.client.expected_broker == case.grant.broker
    assert result.client.broker_current is runtime.broker_generation_current
    assert set(case.cfg["shared_launch_runtime"]) == {
        "schema",
        "request_id",
        "binding_path",
        "transport_schema_sha256",
        "socket_path",
    }
    assert str(case.hint) not in case.cfg["factory_arguments"]["config_files"]
    assert str(case.hint) not in case.verified["activation_config_paths"]


@pytest.mark.parametrize("field", ["binding_sha256", "config_map_sha256", "generation"])
def test_review_cannot_introduce_circular_hash_or_wire_identity(case, field):
    case.cfg["shared_launch_runtime"][field] = "c" * 64
    with pytest.raises(ValidationError):
        case.build()


@pytest.mark.parametrize("change", ["relative", "traversal", "control", "v1_hash"])
def test_review_rejects_noncanonical_paths_or_old_transport(case, change):
    review = case.cfg["shared_launch_runtime"]
    if change == "relative":
        review["binding_path"] = "binding.json"
    elif change == "traversal":
        review["socket_path"] = "/tmp/../tmp/wire.sock"
    elif change == "control":
        review["binding_path"] += "\n"
    else:
        review["transport_schema_sha256"] = OLD_WIRE_SHA
    with pytest.raises(ValidationError):
        case.build()


@pytest.mark.parametrize("closure", ["factory_config", "activation_config", "session"])
def test_hint_cannot_be_in_upstream_config_or_session(case, closure):
    if closure == "factory_config":
        case.cfg["factory_arguments"]["config_files"][str(case.hint)] = "c" * 64
    elif closure == "activation_config":
        case.verified["activation_config_paths"] |= {str(case.hint)}
    else:
        case.verified["session_directory"] = str(case.hint.parent)
    with pytest.raises(ValueError, match="outside its upstream"):
        case.build()


@pytest.mark.parametrize("change", ["missing_review", "caller_unit"])
def test_shared_review_is_required_before_hint_read(case, monkeypatch, change):
    read = Mock(side_effect=AssertionError("invalid caller must not read the hint"))
    monkeypatch.setattr(runtime, "_read", read)
    if change == "missing_review":
        del case.cfg["shared_launch_runtime"]
    else:
        case.cfg["caller_unit"] = "swapp-aos-gpu-joint-acceptance.service"
    with pytest.raises(ValueError, match="reviewed consumed-launch binding"):
        case.build()
    read.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        "empty",
        "broker_pid",
        "broker_invocation",
        "hint_selected_broker",
        "caller_pid",
        "caller_unit",
    ],
)
def test_hint_cannot_select_broker_or_other_runtime_identity(case, change):
    if change == "empty":
        case.bindings.clear()
    elif change == "broker_pid":
        case.bindings["second"]["server_generation"]["pid"] += 1
    elif change == "broker_invocation":
        case.bindings["second"]["server_generation"]["invocation_id"] = "e" * 32
    elif change == "hint_selected_broker":
        document = case.grant.model_dump(mode="json", by_alias=True)
        document["broker"]["pid"] += 1
        case.write(document)
    elif change == "caller_pid":
        case.bindings["second"]["caller_generation"]["pid"] += 1
    else:
        case.bindings["second"]["caller_generation"]["unit"] = "other.service"
    with pytest.raises(ValueError, match="captured bindings|independently reviewed scope"):
        case.build()


@pytest.mark.parametrize(
    "field",
    [
        "request_id",
        "plan_sha256",
        "provision_sha256",
        "workspace",
        "app_session",
        "issued_boottime",
        "expires_boottime",
        "workspace_device",
        "workspace_inode",
        "scientist_root",
        "aos_root",
        "reviewed_launch_input_sha256",
        "contract_sha256",
    ],
)
def test_independent_review_rejects_binding_scope_and_source_mismatch(case, field):
    document = case.grant.model_dump(mode="json", by_alias=True)
    if field in document["pins"]:
        document["pins"][field] = "/different/source" if field.endswith("root") else "f" * 64
    elif field in {"issued_boottime", "expires_boottime", "workspace_device", "workspace_inode"}:
        document[field] += 1
    elif field == "workspace":
        document[field] = "/different/workspace"
    elif field == "app_session":
        document[field] = "app-" + "f" * 32
    else:
        document[field] = "f" * len(document[field])
    # All changed hints still satisfy the closed binding schema; independent review must deny.
    LaunchBinding.model_validate(document)
    case.write(document)
    with pytest.raises(ValueError, match="independently reviewed scope"):
        case.build()


@pytest.mark.parametrize(
    "field", ["workspace_device", "workspace_inode", "workspace_uid", "boot_id"]
)
def test_independent_workspace_and_boot_identity_cannot_drift(case, field):
    case.verified[field] = (
        "00000000-0000-0000-0000-000000000002" if field == "boot_id" else case.verified[field] + 1
    )
    with pytest.raises(ValueError, match="independently reviewed scope"):
        case.build()


def test_old_contract_hint_is_denied_before_client_or_socket_construction(case, monkeypatch):
    document = case.grant.model_dump(mode="json", by_alias=True)
    document["pins"]["contract_sha256"] = OLD_WIRE_SHA
    case.write(document)
    client = Mock(side_effect=AssertionError("old contract must not construct a client"))
    monkeypatch.setattr(runtime, "SharedLaunchClient", client)
    with pytest.raises(ValueError, match="independently reviewed scope"):
        case.build()
    client.assert_not_called()


@pytest.mark.parametrize("change", ["mode", "hardlink", "symlink", "whitespace", "duplicate"])
def test_binding_hint_must_be_private_regular_and_canonical(case, change):
    if change == "mode":
        case.hint.chmod(0o644)
    elif change == "hardlink":
        os.link(case.hint, case.hint.with_suffix(".alias"))
    elif change == "symlink":
        target = case.hint.with_suffix(".target")
        case.hint.rename(target)
        case.hint.symlink_to(target)
    elif change == "whitespace":
        case.hint.write_text(
            json.dumps(case.grant.model_dump(mode="json", by_alias=True), indent=2)
        )
    else:
        text = case.hint.read_text()
        case.hint.write_text('{"request_id":"' + case.grant.request_id + '",' + text[1:])
    with pytest.raises((ValueError, RuntimeError, OSError)):
        case.build()


def test_build_expired_deadline_does_not_read_or_renew(case, monkeypatch):
    read = Mock(side_effect=AssertionError("expired review must not read"))
    monkeypatch.setattr(runtime, "_read", read)
    with pytest.raises(ValueError, match="deadline expired"):
        case.build(deadline=case.clock.mono)
    read.assert_not_called()


@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), float("-inf")])
def test_build_nonfinite_original_deadline_is_denied_before_read(case, monkeypatch, deadline):
    read = Mock(side_effect=AssertionError("nonfinite original deadline must not read"))
    monkeypatch.setattr(runtime, "_read", read)
    with pytest.raises(ValueError, match="deadline"):
        case.build(deadline=deadline)
    read.assert_not_called()


def test_build_does_not_return_client_after_original_deadline_elapses_in_decode(case, monkeypatch):
    decode = runtime.strict_json

    def late_decode(raw):
        result = decode(raw)
        case.clock.mono = 52.0
        case.clock.boot = 152.0
        return result

    client = Mock(side_effect=AssertionError("late review must not construct a client"))
    monkeypatch.setattr(runtime, "strict_json", late_decode)
    monkeypatch.setattr(runtime, "SharedLaunchClient", client)
    with pytest.raises(ValueError, match="deadline"):
        case.build(deadline=52.0)
    client.assert_not_called()


@pytest.mark.parametrize("operation", ["enter", "verify"])
@pytest.mark.parametrize("deadline,end", [(51.0, 151.0), (80.0, 153.0), (52.0, 151.0)])
def test_runtime_converts_original_deadline_and_caps_at_authority_expiry(
    case, operation, deadline, end
):
    if deadline == 52.0:
        document = case.grant.model_dump(mode="json", by_alias=True)
        document["expires_boottime"] = 151.0
        case.write(document)
        case.verified["expires_boottime"] = 151.0
    result = case.build()
    result.client = Mock()
    assert getattr(result, operation)(deadline) is None
    result.client.request.assert_called_once_with(
        "enter" if operation == "enter" else "verify_runtime",
        case.grant.request_id,
        result.binding.sha256(),
        INTENT_SHA,
        deadline=end,
    )


@pytest.mark.parametrize("change", ["expired_caller", "before_issue", "authority_expired", "nan"])
def test_runtime_expired_or_invalid_windows_fail_before_transport(case, change):
    result = case.build()
    result.client = Mock()
    deadline = 52.0
    if change == "expired_caller":
        deadline = case.clock.mono
    elif change == "before_issue":
        case.clock.boot = 99.0
    elif change == "authority_expired":
        case.clock.boot = result.binding.expires_boottime
    else:
        deadline = float("nan")
    with pytest.raises(ValueError, match="window expired"):
        result.enter(deadline)
    result.client.request.assert_not_called()


@pytest.mark.parametrize("late_clock", ["mono", "boot"])
def test_late_response_cannot_renew_original_request(case, late_clock):
    result = case.build()

    def late(*_args, **_kwargs):
        setattr(case.clock, late_clock, 52.0 if late_clock == "mono" else 152.0)

    result.client = Mock()
    result.client.request.side_effect = late
    with pytest.raises(ValueError, match="response arrived after"):
        result.enter(52.0)
    assert result.client.request.call_count == 1


def test_lost_ack_has_no_runtime_retry(case):
    result = case.build()
    result.client = Mock()
    result.client.request.side_effect = SharedLaunchTransportError("launch_denied")
    with pytest.raises(SharedLaunchTransportError):
        result.enter(52.0)
    assert result.client.request.call_count == 1


def test_real_local_private_socket_connects_with_bounded_timeout(case):
    result = case.build()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(case.socket_path))
        case.socket_path.chmod(0o600)
        server.listen(1)
        with result._connect(151.0) as client:
            assert client.gettimeout() == 1.0
            with server.accept()[0]:
                assert client.getpeername() == str(case.socket_path)


@pytest.mark.parametrize(
    "change", ["parent_mode", "socket_mode", "regular_file", "symlink", "expired"]
)
def test_private_socket_checks_deny_before_connect(case, monkeypatch, change):
    result = case.build()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(case.socket_path))
        case.socket_path.chmod(0o600)
        server.listen(1)
        if change == "parent_mode":
            case.socket_path.parent.chmod(0o755)
        elif change == "socket_mode":
            case.socket_path.chmod(0o666)
        elif change == "regular_file":
            case.socket_path.unlink()
            case.socket_path.write_text("not a socket")
            case.socket_path.chmod(0o600)
        elif change == "symlink":
            target = case.socket_path.with_suffix(".target")
            case.socket_path.rename(target)
            case.socket_path.symlink_to(target)
        factory = Mock(side_effect=AssertionError("invalid path must not create socket"))
        monkeypatch.setattr(runtime.socket, "socket", factory)
        with pytest.raises(ValueError):
            result._connect(150.0 if change == "expired" else 151.0)
        factory.assert_not_called()


def test_socket_replacement_during_connect_closes_channel(case, monkeypatch):
    result = case.build()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(case.socket_path))
        case.socket_path.chmod(0o600)
        channel = Mock()

        def replace(_path):
            case.socket_path.unlink()
            case.socket_path.write_text("replacement")
            case.socket_path.chmod(0o600)

        channel.connect.side_effect = replace
        monkeypatch.setattr(runtime.socket, "socket", Mock(return_value=channel))
        with pytest.raises(ValueError, match="changed during connection"):
            result._connect(180.0)
        channel.settimeout.assert_called_once_with(3.0)
        channel.close.assert_called_once()


@pytest.mark.parametrize("change", ["parent_permissions", "parent_symlink"])
def test_parent_identity_or_privacy_drift_during_connect_closes_channel(case, monkeypatch, change):
    parent = case.socket_path.parent / "private"
    parent.mkdir(mode=0o700)
    path = parent / "wire.sock"
    case.cfg["shared_launch_runtime"]["socket_path"] = str(path)
    result = case.build()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        path.chmod(0o600)
        channel = Mock()

        def change_parent(_path):
            if change == "parent_permissions":
                parent.chmod(0o755)
            else:
                moved = parent.with_name("moved")
                parent.rename(moved)
                parent.symlink_to(moved, target_is_directory=True)

        channel.connect.side_effect = change_parent
        monkeypatch.setattr(runtime.socket, "socket", Mock(return_value=channel))
        with pytest.raises(ValueError, match="changed during connection"):
            result._connect(151.0)
        channel.close.assert_called_once()
