"""Pure adapter trust-boundary checks; no AOS service, socket, SQL or GPU launch.

The small AOS stand-ins exercise host adapter decisions and callable wiring.
They do not claim native runtime or the AOS capture's transaction acceptance.
"""

from __future__ import annotations

import ast
import os
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import aos_native_admission_factory as adapter


class Model:
    def __init__(self, **value):
        self.value = deepcopy(value)

    def __getattr__(self, name):
        value = self.value[name]
        return Model(**value) if isinstance(value, dict) else value

    def model_dump(self, **_options):
        return deepcopy(self.value)

    def model_copy(self, **_options):
        return type(self)(**self.value)

    @classmethod
    def model_validate(cls, value, **_options):
        if type(value) is not dict:
            raise ValueError("expected binding must be explicit")
        return cls(**value)

    def __eq__(self, other):
        return isinstance(other, Model) and self.value == other.value


class Intent(Model):
    pass


class Request(Model):
    pass


class Admission(Model):
    pass


class Denied(RuntimeError):
    pass


class Store:
    def __init__(self):
        self.connection = SimpleNamespace(in_transaction=False)


class Controller:
    def __init__(self):
        self.store = Store()
        self.runtime = SimpleNamespace(runtime_id="actual-runtime")
        self.session_id = "actual-session"
        self.desktop = dict(
            session_id=self.session_id,
            runtime_id=self.runtime.runtime_id,
            owner="AGENT",
            lease_id="actual-lease",
            generation=3,
            status="running",
        )

    def state(self):
        return deepcopy(self.desktop)


@dataclass(frozen=True)
class Peer:
    pid: int = 22222
    uid: int = 1000
    start_ticks: int = 17
    boot_id: str = "11111111-1111-1111-1111-111111111111"
    invocation_id: str = "a" * 32
    control_group: str = "/user.slice/swapp-lab-gpu-broker.service"


class Capture:
    def __init__(self, store, socket, **options):
        self.store, self.socket, self.options = store, socket, options


class History:
    def __init__(self, store, **options):
        self.store = store
        self.capture = options["capture"]
        self.verify_current = options["verify_current"]
        self.record_version = options["record_version"]


@pytest.fixture
def context(monkeypatch):
    controller, peer = Controller(), Peer()
    binding = Intent(
        **{key: value for key, value in controller.desktop.items() if key != "status"},
        authorization_context_sha256="e" * 64,
    )
    request = Request(
        request_id="b" * 32, profile_id="aos.decider.turn.v1", deployment_digest="c" * 64
    )
    expected = Admission(
        server_generation={**asdict(peer), "unit": "swapp-lab-gpu-broker.service"},
        caller_generation={"pid": os.getpid()},
        policy_sha256="d" * 64,
        profile_id=request.profile_id,
        profile_pin={"deployment_digest": request.deployment_digest},
        source_fingerprints={"aos": "f" * 64, "scientist": "a" * 64},
    )
    auth = SimpleNamespace(
        authenticate=Mock(return_value=peer), still_current=Mock(return_value=True)
    )
    api = SimpleNamespace(
        DesktopController=Controller,
        TrajectoryStore=Store,
        ScientistIntentBinding=Intent,
        ScientistTurnRequest=Request,
        ScientistAdmissionBindingV2=Admission,
        ScientistAdmissionError=Denied,
        BROKER_UNIT="swapp-lab-gpu-broker.service",
        BrokerPeer=Peer,
        SystemdBrokerAuthenticator=Mock(return_value=auth),
        ScientistBootstrapCapture=Capture,
        ScientistAdmissionHistory=History,
        ScientistBootstrapCodec=Mock(),
        CONTROL_DESCRIPTOR_SHA256="f" * 64,
    )
    reader, current, writer = Mock(return_value=expected), Mock(return_value=None), Mock()
    monkeypatch.setattr(adapter, "_load_aos", lambda: api)
    caller_verifier = Mock()
    monkeypatch.setattr(adapter, "_verify_caller_service", caller_verifier)
    factory = adapter.NativeScientistAdmissionFactory(
        "/private/control.sock",
        expected_binding=reader,
        verify_current=current,
        persist_bootstrap_intent=writer,
    )
    history = factory(controller)
    return SimpleNamespace(**locals())


@pytest.mark.parametrize(
    "missing", ["expected_binding", "verify_current", "persist_bootstrap_intent"]
)
def test_missing_trust_provider_is_denied_before_aos_import(missing, monkeypatch):
    load = Mock(side_effect=AssertionError("constructor must not import AOS"))
    monkeypatch.setattr(adapter, "_load_aos", load)
    options = {
        name: Mock() for name in ("expected_binding", "verify_current", "persist_bootstrap_intent")
    }
    options[missing] = None
    with pytest.raises(TypeError, match="required"):
        adapter.NativeScientistAdmissionFactory("/private/control.sock", **options)
    load.assert_not_called()


@pytest.mark.parametrize("socket", ["relative.sock", "/private/../control.sock"])
def test_requires_explicit_absolute_control_socket(socket):
    with pytest.raises(ValueError, match="absolute"):
        adapter.NativeScientistAdmissionFactory(
            socket, expected_binding=Mock(), verify_current=Mock(), persist_bootstrap_intent=Mock()
        )


def test_native_callable_wiring_preserves_original_store_and_aos_capture(context):
    ctx = context
    assert ctx.history.store is ctx.controller.store
    assert ctx.history.capture.store is ctx.controller.store
    assert ctx.history.record_version == "2.0"
    assert ctx.history.capture.options["authenticator"] is ctx.auth
    assert ctx.history.capture.options["persist_intent"] is ctx.writer
    assert "clock" not in ctx.history.capture.options
    ctx.api.ScientistBootstrapCodec.assert_called_once_with(
        control_descriptor_sha256=ctx.api.CONTROL_DESCRIPTOR_SHA256
    )
    assert ctx.factory(ctx.controller) is ctx.history
    assert ctx.factory.expected_peer(ctx.request, ctx.binding) == ctx.peer
    ctx.auth.authenticate.assert_called_once_with(ctx.peer.pid, ctx.peer.uid)
    ctx.current.assert_called_once()
    assert ctx.current.call_args.args[0] is ctx.controller
    assert ctx.current.call_args.args[3] == ctx.expected
    assert ctx.caller_verifier.call_count == 2


@pytest.mark.parametrize(
    "key,value",
    [
        ("session_id", "foreign"),
        ("runtime_id", "foreign"),
        ("owner", "HUMAN"),
        ("lease_id", "revoked"),
        ("generation", 4),
        ("status", "paused"),
    ],
)
def test_changed_controller_authority_denies_before_peer_or_host_check(context, key, value):
    context.controller.desktop[key] = value
    with pytest.raises(Denied):
        context.factory.expected_peer(context.request, context.binding)
    context.auth.authenticate.assert_not_called()
    context.current.assert_not_called()


@pytest.mark.parametrize("target", ["store", "runtime", "session_id"])
def test_controller_object_rebinding_denies(context, target):
    setattr(context.controller, target, getattr(Controller(), target))
    if target == "session_id":
        context.controller.session_id = "foreign"
    with pytest.raises(Denied, match="changed"):
        context.factory.expected_peer(context.request, context.binding)


def test_same_factory_cannot_attach_another_controller(context):
    with pytest.raises(Denied, match="another controller"):
        context.factory(Controller())


def test_foreign_intent_and_noncurrent_broker_deny(context):
    context.binding.value["lease_id"] = "foreign"
    with pytest.raises(Denied, match="Intent"):
        context.factory.expected_peer(context.request, context.binding)
    context.binding.value["lease_id"] = "actual-lease"
    context.auth.still_current.return_value = False
    with pytest.raises(Denied, match="current systemd"):
        context.factory.expected_peer(context.request, context.binding)
    context.current.assert_not_called()


def test_actual_peer_generation_must_match_independent_expectation(context):
    context.auth.authenticate.return_value = replace(context.peer, start_ticks=99)
    with pytest.raises(Denied, match="current systemd"):
        context.factory.expected_peer(context.request, context.binding)


def test_expected_binding_cannot_change_after_first_preconnect_read(context):
    context.factory.expected_peer(context.request, context.binding)
    changed = context.expected.model_dump()
    changed["policy_sha256"] = "9" * 64
    context.reader.return_value = Admission(**changed)
    with pytest.raises(Denied, match="Independent expected"):
        context.factory.expected_peer(context.request, context.binding)


@pytest.mark.parametrize("callback", ["reader", "current"])
def test_trust_callback_cannot_change_original_transaction(context, callback):
    def change_transaction(*_arguments):
        context.controller.store.connection.in_transaction = True
        return context.expected if callback == "reader" else None

    getattr(context, callback).side_effect = change_transaction
    with pytest.raises(Denied, match="transaction ownership"):
        context.factory.expected_peer(context.request, context.binding)


def test_host_rights_and_source_revocation_denies(context):
    context.current.side_effect = Denied("source/runtime revoked")
    with pytest.raises(Denied, match="revoked"):
        context.factory.expected_peer(context.request, context.binding)


def test_boolean_host_approval_cannot_replace_complete_current_verification(context):
    context.current.return_value = True
    with pytest.raises(Denied, match="complete or raise"):
        context.factory.expected_peer(context.request, context.binding)


@pytest.mark.parametrize("method", ["_verify_capture", "_verify_history"])
def test_self_consistent_ack_or_history_cannot_supply_expected_trust(context, method):
    changed = context.expected.model_dump()
    changed["source_fingerprints"]["scientist"] = "9" * 64
    untrusted = SimpleNamespace(admission_binding=Admission(**changed))
    with pytest.raises(Denied, match="independently trusted"):
        callback = getattr(context.factory, method)
        if method == "_verify_capture":
            callback(context.request, context.binding, context.peer, untrusted)
        else:
            callback(untrusted, context.request, context.binding, context.peer)
    assert all(len(call.args) == 2 for call in context.reader.call_args_list)


def test_module_imports_only_stdlib_and_lazy_aos():
    tree = ast.parse(Path(adapter.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module.split(".")[0] in {
                "__future__",
                "copy",
                "contextlib",
                "contextvars",
                "functools",
                "dataclasses",
                "pathlib",
                "types",
                "aos",
            }
        elif isinstance(node, ast.Import):
            assert all(
                alias.name in {"os", "re", "subprocess", "inspect", "math", "sys", "time"}
                for alias in node.names
            )


@pytest.mark.parametrize("outcome", ["current", "rejected", "changed", "missing"])
def test_native_caller_verification_uses_reviewed_exact_unit_authenticator(outcome):
    generation = Model(pid=os.getpid(), parent_pid=os.getpid() + 100, control_group="/unit")
    authenticate = Mock(return_value=generation)
    if outcome == "rejected":
        authenticate.side_effect = Denied("exact unit cgroup or bounded evidence rejected")
    elif outcome == "changed":
        authenticate.return_value = Model(pid=os.getpid() + 1)
    factory = Mock(return_value=SimpleNamespace(authenticate=authenticate))
    api = SimpleNamespace(ScientistAdmissionError=Denied)
    if outcome != "missing":
        api.SystemdCallerAuthenticator = factory
    if outcome == "current":
        adapter._verify_caller_service(api, generation)
    else:
        with pytest.raises(Denied):
            adapter._verify_caller_service(api, generation)
    if outcome != "missing":
        factory.assert_called_once_with()
        authenticate.assert_called_once_with(generation)
