"""Synthetic CPU owner-loop plumbing; not native/GPU acceptance evidence."""

import json
import sqlite3
import threading
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from types import SimpleNamespace

import pytest

from scripts import aos_native_retained_resolution as module


class Denied(Exception):
    pass


@dataclass
class Intent:
    session_id: str = "session"
    runtime_id: str = "runtime"
    owner: str = "AGENT"
    lease_id: str = "lease"
    generation: int = 2
    authorization_context_sha256: str = "a" * 64

    def model_copy(self, *, deep=False):
        return deepcopy(self) if deep else replace(self)

    @classmethod
    def model_validate_json(cls, value, *, strict):
        assert strict
        return cls(**json.loads(value))


@dataclass
class Record:
    request_id: str = "1" * 32
    session_id: str = "session"

    def model_copy(self, *, deep=False):
        return deepcopy(self) if deep else replace(self)


@dataclass
class ResolutionResult:
    request_id: str
    admission_record_sha256: str
    reconcile_control_id: str
    response_sha256: str
    terminal_receipt_sha256: str
    current_binding: Intent
    state: str
    original_intent_unchanged: bool

    @classmethod
    def model_validate(cls, value, *, strict):
        assert strict
        return cls(**value)


class DesktopBinding:
    def create_retained_host(self, current_binding, **options):
        self.options = options
        assert current_binding == self.intent
        self.host = Host(self)
        return self.host


class Host:
    def __init__(self, binding):
        self.store = binding.controller.store
        self.history = binding.admission_history
        self.binding = binding.intent.model_copy(deep=True)
        self.calls = []
        self.acks = {}
        self.fail = None
        self.after_discover = None
        self.resolved_records = {}

    def discover(self, request_id, control_id):
        self.calls.append(("discover", request_id, control_id))
        if self.fail == "discover":
            raise TimeoutError("synthetic lost ACK")
        self.acks[control_id] = self.ack(request_id, control_id, "capability")
        if self.after_discover:
            self.after_discover()

    def reconcile(self, request_id, capability_id, control_id):
        self.calls.append(("reconcile", request_id, capability_id, control_id))
        self.acks[control_id] = self.ack(request_id, control_id, "reconcile")

    @staticmethod
    def ack(request_id, control_id, operation):
        return {
            "control_id": control_id,
            "request": {"target": {"request_id": request_id}, "op": operation},
            "pending": False,
            "response": {"ok": True},
            "response_sha256": "b" * 64,
        }

    def inspect(self, control_id, *, capability_control_id=None):
        return self.acks[control_id]

    def resolve(self, reconcile_id, capability_id, *, response_sha256):
        self.calls.append(("resolve", reconcile_id, capability_id, response_sha256))
        record = {
            "request_id": "1" * 32,
            "admission_record_sha256": "c" * 64,
            "control_id": reconcile_id,
            "response_sha256": response_sha256,
            "terminal_receipt_sha256": "e" * 64,
            "current_binding_json": json.dumps(asdict(self.binding)),
        }
        self.resolved_records[record["request_id"]] = deepcopy(record)
        return record

    def inspect_resolution(self, request_id, capability_id):
        return deepcopy(self.resolved_records[request_id])


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(
        module,
        "_load_aos",
        lambda: SimpleNamespace(
            Record=Record,
            Binding=DesktopBinding,
            IntentBinding=Intent,
            ResolutionResult=ResolutionResult,
            Error=Denied,
        ),
    )
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE scientist_turn_intents "
        "(request_id TEXT,state TEXT,receipt_json TEXT,binding_json TEXT)"
    )
    intent = Intent()
    connection.execute(
        "INSERT INTO scientist_turn_intents VALUES (?,?,?,?)",
        ("1" * 32, "receipt_recorded", "{}", json.dumps(asdict(intent))),
    )
    connection.commit()
    store = SimpleNamespace(connection=connection)
    record = Record()
    history = SimpleNamespace(store=store, record_version="2.0", read=lambda _: (record, "c" * 64))
    state = asdict(intent) | {"status": "running"}
    binding = DesktopBinding()
    binding.intent = intent
    binding.controller = SimpleNamespace(store=store, state=lambda: state.copy())
    binding.admission_history = history
    binding.scheduler = SimpleNamespace(closed=False, restart_quiesced=False)
    rights = {"valid": True, "calls": 0}

    def verify_current(_binding, _intent, _original):
        rights["calls"] += 1
        if not rights["valid"]:
            raise Denied("synthetic revoked rights")

    options = {
        "authenticator": object(),
        "verifier": object(),
        "verify_control": lambda *args: None,
        "verify_resolution": lambda *args: None,
    }
    factory = module.ConfiguredNativeRetainedResolver(
        host_options_factory=lambda *args: options,
        verify_current=verify_current,
        prepare_resolution=lambda *args: None,
    )
    yield SimpleNamespace(
        factory=factory,
        binding=binding,
        intent=intent,
        state=state,
        connection=connection,
        rights=rights,
        options=options,
    )
    connection.close()


def test_default_denial_and_construction_have_no_dispatch(configured):
    value = configured
    with pytest.raises(Denied, match="explicit trusted"):
        module.ConfiguredNativeRetainedResolver()(value.binding, value.intent)
    resolver = value.factory(value.binding, value.intent)
    assert resolver.host.calls == []
    assert value.rights["calls"] == 0


def test_exact_sequence_and_resolution_retry_preserve_original(configured):
    value = configured
    before = tuple(value.connection.iterdump())
    resolver = value.factory(value.binding, value.intent)
    result = resolver.resolve_successful("1" * 32)
    assert isinstance(result, ResolutionResult)
    assert result.admission_record_sha256 == "c" * 64
    assert result.terminal_receipt_sha256 == "e" * 64
    assert result.current_binding == value.intent
    assert result.original_intent_unchanged is True and result.state == "resolved"
    calls = resolver.host.calls.copy()
    assert [call[0] for call in calls] == ["discover", "reconcile", "resolve"]
    assert calls[0][2] != calls[1][3]
    assert calls[1][2] == calls[0][2]
    assert calls[2][1:3] == (calls[1][3], calls[0][2])
    rights_calls = value.rights["calls"]
    assert resolver.resolve_successful("1" * 32) == result
    assert [call[0] for call in resolver.host.calls] == [
        "discover",
        "reconcile",
        "resolve",
        "resolve",
    ]
    assert value.rights["calls"] > rights_calls
    assert tuple(value.connection.iterdump()) == before
    value.rights["valid"] = False
    with pytest.raises(Denied, match="revoked"):
        resolver.resolve_successful("1" * 32)
    assert len(resolver.host.calls) == 4


def test_lost_ack_is_sticky_and_never_dispatched_twice(configured):
    resolver = configured.factory(configured.binding, configured.intent)
    resolver.host.fail = "discover"
    with pytest.raises(TimeoutError):
        resolver.resolve_successful("1" * 32)
    resolver.host.fail = None
    with pytest.raises(Denied, match="historical reconciliation"):
        resolver.resolve_successful("1" * 32)
    assert len(resolver.host.calls) == 1


@pytest.mark.parametrize(
    "field,new",
    [("lease_id", "other"), ("generation", 3), ("owner", "HUMAN"), ("runtime_id", "other")],
)
def test_owner_change_after_discovery_stops_before_reconcile(configured, field, new):
    resolver = configured.factory(configured.binding, configured.intent)
    resolver.host.after_discover = lambda: configured.state.update({field: new})
    with pytest.raises(Denied, match="changed"):
        resolver.resolve_successful("1" * 32)
    assert [call[0] for call in resolver.host.calls] == ["discover"]


def test_pending_or_different_context_never_dispatches(configured):
    resolver = configured.factory(configured.binding, configured.intent)
    configured.connection.execute("UPDATE scientist_turn_intents SET state='pending'")
    configured.connection.commit()
    with pytest.raises(Denied, match="recorded successful"):
        resolver.resolve_successful("1" * 32)
    configured.connection.execute(
        "UPDATE scientist_turn_intents SET state='receipt_recorded',binding_json=?",
        (json.dumps(asdict(replace(configured.intent, authorization_context_sha256="d" * 64))),),
    )
    configured.connection.commit()
    with pytest.raises(Denied, match="original intent"):
        resolver.resolve_successful("1" * 32)
    assert resolver.host.calls == []


def test_original_owner_thread_and_transaction_required(configured):
    resolver = configured.factory(configured.binding, configured.intent)
    errors = []

    def other_thread():
        try:
            resolver.resolve_successful("1" * 32)
        except Denied as error:
            errors.append(error)

    thread = threading.Thread(target=other_thread)
    thread.start()
    thread.join()
    assert len(errors) == 1 and "owner thread" in str(errors[0])
    configured.connection.execute("BEGIN")
    with pytest.raises(Denied, match="transaction"):
        resolver.resolve_successful("1" * 32)
    configured.connection.rollback()
    assert resolver.host.calls == []


def test_pending_committed_ack_never_resolves(configured):
    resolver = configured.factory(configured.binding, configured.intent)
    original_inspect = resolver.host.inspect

    def pending(control_id, **kwargs):
        value = original_inspect(control_id, **kwargs).copy()
        value["pending"] = True
        return value

    resolver.host.inspect = pending
    with pytest.raises(Denied, match="committed successful ACK"):
        resolver.resolve_successful("1" * 32)
    assert [call[0] for call in resolver.host.calls] == ["discover"]


def test_preparation_default_denial_without_host_construction(configured):
    configured.factory.prepare_resolution = None
    with pytest.raises(Denied, match="explicit trusted"):
        configured.factory(configured.binding, configured.intent)
    assert not hasattr(configured.binding, "host")


def test_preparation_receives_exact_committed_ack_closure_once(configured):
    prepared = []

    def prepare(host, original, capability_id, capability_hash, reconcile_id, reconcile_hash):
        assert [call[0] for call in host.calls] == ["discover", "reconcile"]
        assert original == Record()
        assert original is not host.history.read(original.request_id)[0]
        assert host.inspect(capability_id)["response_sha256"] == capability_hash
        assert (
            host.inspect(reconcile_id, capability_control_id=capability_id)["response_sha256"]
            == reconcile_hash
        )
        prepared.append((capability_id, capability_hash, reconcile_id, reconcile_hash))

    configured.factory.prepare_resolution = prepare
    resolver = configured.factory(configured.binding, configured.intent)
    resolver.resolve_successful("1" * 32)
    resolver.resolve_successful("1" * 32)
    assert len(prepared) == 1
    assert resolver.host.calls[-1][1:3] == (prepared[0][2], prepared[0][0])


@pytest.mark.parametrize("failure", ["return", "raise", "lease", "capability", "reconcile"])
def test_preparation_denial_or_mutation_stops_resolution_and_remains_sticky(configured, failure):
    def prepare(host, _original, capability_id, _capability_hash, reconcile_id, _reconcile_hash):
        if failure == "return":
            return True
        if failure == "raise":
            raise Denied("synthetic preparation denied")
        if failure == "lease":
            configured.state["lease_id"] = "other"
        else:
            selected = capability_id if failure == "capability" else reconcile_id
            host.acks[selected]["response_sha256"] = "f" * 64
        return None

    configured.factory.prepare_resolution = prepare
    resolver = configured.factory(configured.binding, configured.intent)
    with pytest.raises(Denied):
        resolver.resolve_successful("1" * 32)
    assert [call[0] for call in resolver.host.calls] == ["discover", "reconcile"]
    configured.state["lease_id"] = "lease"
    with pytest.raises(Denied, match="historical reconciliation"):
        resolver.resolve_successful("1" * 32)
    assert len(resolver.host.calls) == 2


@pytest.mark.parametrize("field", ["admission_record_sha256", "control_id", "response_sha256"])
def test_typed_result_rejects_wrong_durable_original_pins(configured, field):
    resolver = configured.factory(configured.binding, configured.intent)
    resolve = resolver.host.resolve

    def changed(*args, **kwargs):
        result = resolve(*args, **kwargs)
        result[field] = "f" * (32 if field == "control_id" else 64)
        return result

    resolver.host.resolve = changed
    with pytest.raises(Denied, match="durable original target"):
        resolver.resolve_successful("1" * 32)


def test_typed_result_requires_exact_readback_and_unchanged_intent(configured):
    resolver = configured.factory(configured.binding, configured.intent)
    resolver.host.inspect_resolution = lambda *args: {}
    with pytest.raises(Denied, match="durable original target"):
        resolver.resolve_successful("1" * 32)
    resolver = configured.factory(configured.binding, configured.intent)
    resolve = resolver.host.resolve

    def changed(*args, **kwargs):
        result = resolve(*args, **kwargs)
        configured.connection.execute("UPDATE scientist_turn_intents SET receipt_json='changed'")
        configured.connection.commit()
        return result

    resolver.host.resolve = changed
    with pytest.raises(Denied, match="changed its original inference intent"):
        resolver.resolve_successful("1" * 32)
