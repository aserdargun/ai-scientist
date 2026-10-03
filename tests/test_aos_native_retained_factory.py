"""CPU composition/fencing fixtures; these do not establish runtime GPU proof."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import threading
import time
from contextlib import nullcontext
from contextvars import copy_context
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import aos_native_artifact_receipts as artifact_receipts
from scripts import aos_native_retained_factory as module
from scripts.aos_native_admission_factory import _deadline_scope, _verification_deadline


class Denied(RuntimeError):
    pass


class Intent:
    def __init__(self, generation=1):
        self.generation = generation

    def model_dump(self, **_options):
        return {"generation": self.generation}

    def model_copy(self, **_options):
        return deepcopy(self)


class Binding:
    pass


class AsyncClient:
    pass


class OutputValidator:
    def __init__(self, history):
        self.history = history


class Budget:
    def __init__(self, history, **options):
        self.history, self.options = history, options


class Verifier:
    def __init__(self, budget, **options):
        self.budget_verifier, self.options = budget, options


def digest(value):
    return hashlib.sha256(module._canonical(value)).hexdigest()


@pytest.fixture
def configured(tmp_path, monkeypatch):
    roots = {name: tmp_path / name for name in ("aos", "scientist")}
    files = {}
    for name, paths in (("aos", module.AOS_SOURCES), ("scientist", module.SCIENTIST_SOURCES)):
        for relative in paths:
            path = roots[name] / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("CPU source fixture\n")
            files[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    policy = tmp_path / "policy.json"
    policy.write_text("{}")
    schemas = {}
    for name in ("retained_transport", "evidence"):
        path = tmp_path / (name + ".json")
        path.write_text('{\n  "fixture": true\n}\n')
        schemas[name] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    api = SimpleNamespace(
        ScientistAdmissionError=Denied,
        broker_unit="swapp-lab-gpu-broker.service",
        terminal_descriptor_sha256="a" * 64,
        terminal_schema_sha256=lambda: "b" * 64,
        budget_schema=lambda: {},
        digest=digest,
        Binding=Binding,
        Intent=Intent,
        AsyncClient=AsyncClient,
        OutputValidator=OutputValidator,
        BudgetVerifier=Budget,
        TerminalVerifier=Verifier,
        Codec=lambda *args, **kwargs: None,
    )
    admission = object.__new__(module.ConfiguredScientistAdmissionFactory)
    admission._roots = roots
    admission._configs = {}
    admission._policy = policy
    admission._policy_hash = hashlib.sha256(policy.read_bytes()).hexdigest()
    admission._reviewed = {"aos.decider.turn.v1": "{}"}
    admission._socket = tmp_path / "control.sock"
    admission._authenticator = object()
    admission._verify_source = lambda selected: None
    review = {
        "schema": module.SCHEMA,
        "enabled": True,
        "authority": {
            "owner": "AGENT",
            "caller_unit": "aos-original.service",
            "broker_unit": api.broker_unit,
            "allowed_profiles": ["aos.decider.turn.v1"],
            "control_operations": ["capability", "reconcile"],
            "provider_operations": ["read_budget", "verify_physical"],
            "resolution": "recorded_successful_original_only",
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "issued_boottime": time.clock_gettime(time.CLOCK_BOOTTIME) - 1,
            "expires_boottime": time.clock_gettime(time.CLOCK_BOOTTIME) + 300,
            "max_targets": 2,
        },
        "bounds": {"control_seconds": 3, "provider_seconds": 3, "resolution_seconds": 30},
        "schemas": schemas
        | {
            "budget_witness_sha256": digest({}),
            "terminal_schema_sha256": "b" * 64,
            "terminal_descriptor_sha256": "a" * 64,
            "channel_descriptor_sha256": module.DESCRIPTOR_SHA256,
        },
        "source_files": files,
        "config_files": {str(policy): admission._policy_hash},
    }
    monkeypatch.setattr(module, "_load_aos", lambda: api)
    factory = module.ConfiguredNativeRetainedFactory(admission, review)
    with _deadline_scope(api, 3):
        factory._configuration({})
    return SimpleNamespace(factory=factory, review=review, admission=admission, api=api)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.update(extra=True),
        lambda r: r.update(enabled=False),
        lambda r: r["authority"].update(provider_operations=["allocate"]),
        lambda r: r["authority"].update(max_targets=True),
        lambda r: r["authority"].update(expires_boottime=r["authority"]["issued_boottime"] + 3601),
        lambda r: r["bounds"].update(provider_seconds=3.01),
        lambda r: r["bounds"].update(resolution_seconds=31),
        lambda r: r["schemas"].update(channel_descriptor_sha256="f" * 64),
        lambda r: r["source_files"].pop(next(iter(r["source_files"]))),
        lambda r: r["config_files"].clear(),
    ],
)
def test_review_denies_unreviewed_authority(configured, mutation):
    review = deepcopy(configured.review)
    mutation(review)
    with pytest.raises(Denied):
        module.ConfiguredNativeRetainedFactory(configured.admission, review)


def test_current_source_and_revocation_are_rechecked(configured):
    factory = configured.factory
    path = Path(next(iter(configured.review["source_files"])))
    path.write_text("changed")
    with _deadline_scope(configured.api, 3), pytest.raises(Denied, match="changed"):
        factory._configuration({})
    factory.close()
    with _deadline_scope(configured.api, 3), pytest.raises(Denied, match="unavailable"):
        factory._rights()


def test_lazy_proof_callbacks_deny_before_adapter(configured):
    desktop = Binding()
    desktop.admission_history = object()
    desktop.output_validator = OutputValidator(desktop.admission_history)
    options = configured.factory._host_options(desktop, Intent())
    verifier = options["verifier"]
    with pytest.raises(Denied, match="durable ACKs"):
        verifier.budget_verifier.options["read_source"](None, None)
    with pytest.raises(Denied, match="durable ACKs"):
        verifier.options["verify_physical"](None, None, None, None, None)
    assert configured.factory._client is None
    assert not configured.factory._channel_attempted


def owner(configured, loop):
    desktop = Binding()
    desktop.admission_history = object()
    desktop.output_validator = OutputValidator(desktop.admission_history)
    client = AsyncClient()
    client._context = (loop, threading.Event(), time.monotonic() + 100)
    client._cancelled = False
    client._active = asyncio.current_task()
    client._active_request_id = "1" * 32
    desktop.engine = SimpleNamespace(client=client)
    configured.factory.verify_configuration = lambda: None
    return desktop, client


class ResolverFixture:
    """No control effects: exposes factory owner cache and deadline behavior."""

    count = 0
    fail = False

    def __init__(self, **options):
        self.options = options

    def __call__(self, desktop, intent):
        type(self).count += 1
        self.options["host_options_factory"](desktop, intent)
        if type(self).fail:
            raise Denied("interrupted native construction fixture")
        self.desktop_binding, self.current_binding = desktop, intent
        self.host = SimpleNamespace(
            discover=lambda *args: None,
            reconcile=lambda *args: None,
            resolve=lambda *args: None,
        )
        self.attempts = {}
        self.fail_resolution = False
        return self

    def resolve_successful(self, request_id):
        if self.attempts.get(request_id) == "pending":
            raise Denied("interrupted sequence")
        self.attempts[request_id] = "pending"
        if self.fail_resolution:
            raise Denied("uncertain control fixture")
        self.attempts[request_id] = "completed"
        return request_id


def test_owner_cache_preserves_interrupted_sequence_and_binding_gates(configured, monkeypatch):
    monkeypatch.setattr(module, "ConfiguredNativeRetainedResolver", ResolverFixture)
    ResolverFixture.count, ResolverFixture.fail = 0, False

    async def run():
        desktop, _client = owner(configured, asyncio.get_running_loop())
        first = configured.factory(desktop, Intent())
        first.resolver.fail_resolution = True
        with pytest.raises(Denied, match="uncertain"):
            first.resolve_successful("1" * 32)
        assert configured.factory(desktop, Intent()) is first
        with pytest.raises(Denied, match="interrupted sequence"):
            first.resolve_successful("1" * 32)
        first_gate = configured.factory._gate(desktop, Intent())
        second = configured.factory(desktop, Intent(2))
        assert second is not first and ResolverFixture.count == 2
        assert configured.factory._gate(desktop, Intent()) is first_gate
        assert configured.factory._gate(desktop, Intent(2))["host"] is second.resolver.host

    asyncio.run(run())


def test_interrupted_factory_construction_is_sticky(configured, monkeypatch):
    monkeypatch.setattr(module, "ConfiguredNativeRetainedResolver", ResolverFixture)
    ResolverFixture.count, ResolverFixture.fail = 0, True

    async def run():
        desktop, _client = owner(configured, asyncio.get_running_loop())
        with pytest.raises(Denied, match="construction"):
            configured.factory(desktop, Intent())
        ResolverFixture.fail = False
        with pytest.raises(Denied, match="Interrupted owner"):
            configured.factory(desktop, Intent())
        assert ResolverFixture.count == 1

    asyncio.run(run())


@pytest.mark.parametrize("change", ["cancel", "deadline", "context", "request", "closed"])
def test_original_async_context_and_absolute_deadline_are_required(configured, monkeypatch, change):
    monkeypatch.setattr(module, "ConfiguredNativeRetainedResolver", ResolverFixture)
    ResolverFixture.fail = False

    async def run():
        desktop, client = owner(configured, asyncio.get_running_loop())
        bound = configured.factory(desktop, Intent())
        original = bound.resolver.resolve_successful

        def interrupted(request_id):
            result = original(request_id)
            if change == "cancel":
                client._context[1].set()
            elif change == "deadline":
                configured.factory._call_deadlines[
                    ((id(desktop), module._canonical(Intent().model_dump())), client._context)
                ] = time.monotonic() - 1
                # The active verification deadline itself remains the original absolute value.
                token = _verification_deadline.set(time.monotonic() - 1)
                try:
                    configured.factory._check_deadline()
                finally:
                    _verification_deadline.reset(token)
            elif change == "context":
                client._context = tuple([*client._context])
            elif change == "request":
                client._active_request_id = "2" * 32
            else:
                configured.factory.close()
                configured.factory._rights()
            return result

        bound.resolver.resolve_successful = interrupted
        with pytest.raises(Denied):
            bound.resolve_successful("1" * 32)

    asyncio.run(run())


def test_successive_contexts_receive_separate_original_deadlines(configured, monkeypatch):
    monkeypatch.setattr(module, "ConfiguredNativeRetainedResolver", ResolverFixture)
    ResolverFixture.fail = False

    async def run():
        desktop, client = owner(configured, asyncio.get_running_loop())
        first = configured.factory(desktop, Intent())
        assert first.resolve_successful("1" * 32) == "1" * 32
        old_context = client._context
        old_key = ((id(desktop), module._canonical(Intent().model_dump())), old_context)
        configured.factory._call_deadlines[old_key] = time.monotonic() - 1
        client._context = (asyncio.get_running_loop(), threading.Event(), time.monotonic() + 100)
        client._active_request_id = "2" * 32
        assert configured.factory(desktop, Intent()) is first
        assert first.resolve_successful("2" * 32) == "2" * 32
        assert old_key in configured.factory._call_deadlines

    asyncio.run(run())


def test_exact_original_request_is_required_before_control(configured, monkeypatch):
    monkeypatch.setattr(module, "ConfiguredNativeRetainedResolver", ResolverFixture)
    ResolverFixture.fail = False

    async def run():
        desktop, _client = owner(configured, asyncio.get_running_loop())
        bound = configured.factory(desktop, Intent())
        with pytest.raises(Denied, match="async inference"):
            bound.resolve_successful("2" * 32)
        assert not bound.resolver.attempts

    asyncio.run(run())


def test_provider_post_exchange_deadline_failure_closes_owned_channel(configured):
    class NativeClientFixture:
        def __init__(self, channel, **options):
            self.channel, self.options, self.closed = channel, options, False

        def exchange(self, **options):
            return options

        def close(self):
            self.closed = True

    factory = configured.factory
    factory._api.Client = NativeClientFixture
    checks = []

    def current():
        checks.append(True)
        if len(checks) == 2:
            raise Denied("original deadline expired after packet")

    factory._check_deadline = current
    client = module._provider_client(factory, object(), object())
    with pytest.raises(Denied, match="after packet"):
        client.exchange(op="read_budget")
    assert client.closed


def test_retained_control_reuses_immutable_reads_without_renewing_three_seconds(
    configured, tmp_path, monkeypatch
):
    """Replay the observed callback order with injected CPU cost, never real dispatch.

    These ten stages follow ScientistEvidenceClient.exchange and its journal's
    before/after intent/response verifications. Each configured source check
    invokes three attestations, each with two snapshots. The injected cost is a
    causal fixture, not a performance claim about native runtime.
    """
    stages = (
        "pre_intent_authorization",
        "intent_before_insert",
        "intent_after_insert",
        "post_intent_authorization",
        "dispatch_authorization",
        "response_authorization",
        "response_reauthorization",
        "response_before_insert",
        "response_after_insert",
        "post_response_authorization",
    )
    executable = tmp_path / "independently-pinned-interpreter"
    executable.write_bytes(b"bounded synthetic interpreter contents")
    fingerprint = hashlib.sha256(executable.read_bytes()).hexdigest()
    identity = executable.stat()
    now = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    original_read = os.read
    reads, gates, observed, outcomes = [], [], [], {}
    codecs, source_checks = [], []
    configured.api.Codec = lambda *args, **kwargs: codecs.append((args, kwargs))

    def expensive_read(descriptor, length):
        data = original_read(descriptor, length)
        info = os.fstat(descriptor)
        if data and (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
            reads.append(observed[-1])
            now[0] += 0.14
        return data

    monkeypatch.setattr(artifact_receipts.os, "read", expensive_read)

    def source_verification(_selected):
        source_checks.append(observed[-1])
        for _attestation in range(3):
            # Rights/current-generation work remains live on every attestation.
            gates.append((observed[-1], _verification_deadline.get()))
            now[0] += 0.001
            for _snapshot in range(2):
                artifact_receipts._small_file(executable, fingerprint)

    configured.admission._verify_source = source_verification

    def exchange():
        for stage in stages:
            observed.append(stage)
            configured.factory._configuration({})
            if stage == "intent_after_insert":
                outcomes["durable_intent"] = True
            if stage == "dispatch_authorization":
                outcomes["frame_sent"] = True
            if stage == "response_after_insert":
                outcomes["durable_response"] = True
        return "complete synthetic exchange"

    bound = object.__new__(module._BoundResolver)
    bound.factory = configured.factory
    bound.resolver = SimpleNamespace(host=SimpleNamespace(timeout_seconds=None))
    control = bound._control(exchange)
    actual_scope = module.artifact_verification_scope

    # The old uncached behavior exhausts the original scope at the same stage
    # as the real failure, after committing intent but before sending a frame.
    monkeypatch.setattr(
        module, "artifact_verification_scope", lambda deadline, **_options: nullcontext()
    )
    with pytest.raises(Denied, match="deadline"):
        control()
    assert observed == list(stages[:4])
    assert outcomes == {"durable_intent": True}
    assert _verification_deadline.get() is None

    now[0] = 100.0
    reads.clear()
    gates.clear()
    observed.clear()
    outcomes.clear()
    codecs.clear()
    source_checks.clear()
    monkeypatch.setattr(module, "artifact_verification_scope", actual_scope)
    assert control() == "complete synthetic exchange"
    assert observed == list(stages)
    assert outcomes == {"durable_intent": True, "frame_sent": True, "durable_response": True}
    assert reads == [stages[0]]
    assert len(gates) == 30 and all(deadline == 103.0 for _stage, deadline in gates)
    assert len(codecs) == 1 and source_checks == list(stages)
    assert now[0] < 103.0
    assert _verification_deadline.get() is None
    assert module._control_codec.get() is None

    # A different control starts with an empty memo even for identical bytes.
    reads.clear()
    codecs.clear()
    source_checks.clear()
    assert control() == "complete synthetic exchange"
    assert reads == [stages[0]]
    assert len(codecs) == 1 and source_checks == list(stages)


@pytest.mark.parametrize("changed", ["schema", "source"])
def test_control_codec_reuse_requires_fresh_pinned_schema_and_source(configured, changed):
    codecs, source_checks = [], []
    configured.api.Codec = lambda *args, **kwargs: codecs.append(True)
    configured.admission._verify_source = lambda selected: source_checks.append(True)
    path = Path(
        configured.review["schemas"]["evidence"]["path"]
        if changed == "schema"
        else next(iter(configured.review["source_files"]))
    )

    def exchange():
        configured.factory._configuration({})
        path.write_text("changed after the first Codec validation")
        configured.factory._configuration({})

    bound = object.__new__(module._BoundResolver)
    bound.factory = configured.factory
    bound.resolver = SimpleNamespace(host=SimpleNamespace(timeout_seconds=None))
    with pytest.raises(Denied, match="Reviewed retained file changed"):
        bound._control(exchange)()
    assert codecs == source_checks == [True]
    assert module._control_codec.get() is None


def test_control_codec_constructor_change_requires_new_validation(configured):
    codecs = []
    configured.api.Codec = lambda *args, **kwargs: codecs.append("original")

    def exchange():
        configured.factory._configuration({})
        configured.api.Codec = lambda *args, **kwargs: codecs.append("changed")
        configured.factory._configuration({})
        configured.factory._configuration({})

    bound = object.__new__(module._BoundResolver)
    bound.factory = configured.factory
    bound.resolver = SimpleNamespace(host=SimpleNamespace(timeout_seconds=None))
    bound._control(exchange)()
    assert codecs == ["original", "changed"]


@pytest.mark.parametrize("copied_owner", ["closed_scope", "different_task"])
def test_control_codec_cannot_reuse_copied_scope_or_task(configured, copied_owner):
    codecs, source_checks, contexts = [], [], []
    configured.api.Codec = lambda *args, **kwargs: codecs.append(True)
    configured.admission._verify_source = lambda selected: source_checks.append(True)

    async def different_task():
        configured.factory._configuration({})

    def exchange():
        configured.factory._configuration({})
        if copied_owner == "closed_scope":
            contexts.append(copy_context())
        else:
            with pytest.raises(
                artifact_receipts.NativeArtifactReceiptError, match="owner or lifetime"
            ):
                asyncio.run(different_task())

    bound = object.__new__(module._BoundResolver)
    bound.factory = configured.factory
    bound.resolver = SimpleNamespace(host=SimpleNamespace(timeout_seconds=None))
    bound._control(exchange)()
    if contexts:
        with pytest.raises(artifact_receipts.NativeArtifactReceiptError, match="owner or lifetime"):
            contexts[0].run(configured.factory._configuration, {})
    assert codecs == source_checks == [True]
    assert module._control_codec.get() is None


def test_prepare_and_resolve_preserve_separate_original_operation_bounds(
    configured, tmp_path, monkeypatch
):
    """Actual phase wrappers with CPU channel and resolution callback fixtures.

    Prepare adds five channel callbacks to its own two; resolve has seventy.
    Immutable-read cost is injected; no socket, service or native timing claim.
    """
    executable = tmp_path / "pinned-prepare-interpreter"
    executable.write_bytes(b"CPU interpreter identity fixture")
    fingerprint = hashlib.sha256(executable.read_bytes()).hexdigest()
    identity = executable.stat()
    now, reads, codecs, checks, operations, closed = [100.0], [], [], [], [], []
    cancelled, active = [False], [None]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    original_read = os.read

    def expensive_read(descriptor, length):
        data = original_read(descriptor, length)
        info = os.fstat(descriptor)
        if data and (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
            reads.append(True)
            now[0] += 0.14
        return data

    monkeypatch.setattr(artifact_receipts.os, "read", expensive_read)
    configured.api.Codec = lambda *args, **kwargs: codecs.append(True)

    def source_verification(_selected):
        checks.append(_verification_deadline.get())
        operation = artifact_receipts._operation()
        if operation is not None and not operation.workers:
            operations.append(operation)
            operation.workers["CPU-cleanup-fixture"] = SimpleNamespace(
                close=lambda: closed.append(operation)
            )
        for _snapshot in range(6):
            artifact_receipts._small_file(executable, fingerprint)
        if cancelled[0]:
            active[0]._context[1].set()

    configured.admission._verify_source = source_verification
    expected = object()

    def open_channel(*args, verify_current, **kwargs):
        # Current checks before connect, send, receive, after receive and return.
        for _stage in range(5):
            assert verify_current() is None
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(module, "open_retained_provider_channel", open_channel)
    monkeypatch.setattr(
        module,
        "_provider_client",
        lambda *args: SimpleNamespace(poisoned=False, expected_peer=expected),
    )
    configured.api.Adapter = lambda *args, **kwargs: object()

    def original(request_id):
        return SimpleNamespace(
            request_id=request_id,
            admission_binding=SimpleNamespace(
                caller_generation=Intent(), server_generation=Intent()
            ),
        )

    def prepare(factory, host, record):
        return factory._prepare(host, record, "a" * 32, "b" * 64, "c" * 32, "d" * 64)

    async def run():
        def new_owner():
            factory = module.ConfiguredNativeRetainedFactory(
                configured.admission, configured.review
            )
            configured.factory = factory
            desktop, client = owner(configured, asyncio.get_running_loop())
            active[0] = client
            factory._active = desktop, client, client._context, client._active_request_id
            factory._targets.add(client._active_request_id)
            host = SimpleNamespace(binding=Intent(), timeout_seconds=None)
            gate = {"host": host, "intent": host.binding, "adapter": None, "adapters": {}}
            factory._gates[(id(desktop), module._canonical(host.binding.model_dump()))] = gate
            factory._current = lambda *args: factory._configuration({}) or expected
            return factory, host, gate, client

        factory, host, gate, _client = new_owner()
        # The old preparation had only the deadline scope, without immutable reuse.
        factory._operation_scope = lambda deadline: nullcontext()
        with pytest.raises(Denied, match="deadline"):
            prepare(factory, host, original("1" * 32))
        assert len(checks) == 4 and now[0] >= 103.0
        assert gate["adapter"] is None and factory._channel_attempted
        assert _verification_deadline.get() is None

        now[0] = 100.0
        factory, host, gate, client = new_owner()
        bound = object.__new__(module._BoundResolver)
        bound.factory, bound.resolver = factory, SimpleNamespace(host=host)
        bound._control(lambda: factory._configuration({}))()
        reads.clear()
        codecs.clear()
        checks.clear()
        start = now[0]
        assert prepare(factory, host, original("1" * 32)) is None
        assert reads == codecs == [True] and checks == [start + 3] * 7
        assert gate["adapters"]["1" * 32] is gate["adapter"]

        # A different target can reuse its channel, never the previous operation memo.
        gate["adapter"] = None
        client._active_request_id = "2" * 32
        factory._targets.add(client._active_request_id)
        factory._active = factory._active[:3] + (client._active_request_id,)
        reads.clear()
        codecs.clear()
        checks.clear()
        start = now[0]
        prepare(factory, host, original("2" * 32))
        assert reads == codecs == [True] and checks == [start + 3] * 2

        def resolve():
            for _callback in range(70):
                factory._configuration({})
            return "complete CPU resolution"

        # An unscoped resolution also exceeds its partially spent original window.
        checks.clear()
        factory._operation_scope = lambda deadline: nullcontext()
        with (
            pytest.raises(Denied, match="deadline"),
            _deadline_scope(configured.api, 30) as deadline,
        ):
            now[0] += 10
            bound._resolve(resolve)()
        assert 0 < len(checks) < 70 and now[0] >= deadline
        del factory._operation_scope

        reads.clear()
        codecs.clear()
        checks.clear()
        with _deadline_scope(configured.api, 30) as deadline:
            now[0] += 10
            assert bound._resolve(resolve)() == "complete CPU resolution"
            assert checks == [deadline] * 70 and now[0] < deadline
        assert reads == codecs == [True]

        # No fresh timeout may revive an expired outer resolution authority.
        with (
            pytest.raises(Denied, match="deadline"),
            _deadline_scope(configured.api, 30) as deadline,
        ):
            now[0] = deadline
            bound._resolve(lambda: pytest.fail("Expired resolution reached its body"))()

        factory, host, gate, client = new_owner()
        desktop = factory._active[0]
        original_deadline = now[0] + 20  # Ten seconds of the original 30 are already spent.
        key = (id(desktop), module._canonical(host.binding.model_dump()))
        factory._call_deadlines[(key, client._context)] = original_deadline
        factory._active = None

        def outer_resolution(request_id):
            parent = artifact_receipts._operation()
            parent_codec = module._control_codec.get()
            assert parent.deadline == _verification_deadline.get() == original_deadline
            factory._configuration({})
            phases = (
                bound._control(lambda: factory._configuration({})),
                lambda: prepare(factory, host, original(request_id)),
                bound._resolve(resolve),
            )
            for phase in phases:
                phase()
                assert artifact_receipts._operation() is parent and not parent.closed
                assert module._control_codec.get() is parent_codec
                assert _verification_deadline.get() == original_deadline
            factory._configuration({})
            return "complete outer CPU resolution"

        bound.factory = factory
        bound.resolver = SimpleNamespace(
            host=host,
            desktop_binding=desktop,
            current_binding=host.binding,
            resolve_successful=outer_resolution,
        )
        reads.clear()
        codecs.clear()
        checks.clear()
        prior_operations = len(operations)
        assert bound.resolve_successful("1" * 32) == "complete outer CPU resolution"
        # Parent boundary reads reuse their own memo; every nested operation starts fresh.
        assert reads == codecs == [True] * 4
        assert len(checks) == 80 and checks[0] == checks[-1] == original_deadline
        assert len(operations) - prior_operations == 4 and factory._active is None

        for phase in ("prepare", "resolve"):
            factory, host, gate, _client = new_owner()
            bound.factory, bound.resolver = factory, SimpleNamespace(host=host)
            cancelled[0] = True
            with pytest.raises(Denied, match="cancelled"):
                if phase == "prepare":
                    prepare(factory, host, original("1" * 32))
                else:
                    with _deadline_scope(configured.api, 30):
                        bound._resolve(resolve)()
            assert gate["adapter"] is None
        assert len(operations) == len(closed) and set(operations) == set(closed)
        assert all(operation.closed for operation in operations)
        assert module._control_codec.get() is artifact_receipts._operation() is None
        assert _verification_deadline.get() is None

    asyncio.run(run())


def test_actual_native_constructor_and_lazy_proof_composition(tmp_path):
    """Optional checkout integration uses genuine native types, no services/GPU."""
    aos_root = Path(os.environ.get("AOS_SOURCE_ROOT", "/home/cachyos/aos"))
    interpreter = aos_root / ".venv/bin/python"
    if not interpreter.exists():
        pytest.skip("Independent native AOS checkout interpreter is unavailable")
    from lab.llm.aos_retained_evidence_transport import schema_document

    root = Path(__file__).resolve().parents[1]
    transport = tmp_path / "transport.json"
    transport.write_text(json.dumps(schema_document(), indent=2))
    evidence = root / "lab/llm/contracts/control_v2/terminal-evidence.schema.json"
    code = r"""
import hashlib,json,os,socket,sys,threading,time
from pathlib import Path
from types import SimpleNamespace
from scripts import aos_native_retained_factory as m
from scripts.aos_native_admission_factory import _deadline_scope
from aos.storage import TrajectoryStore
from aos.scientist_admission_history import ScientistAdmissionHistory
from aos.scientist_profile_output import AdmissionProfileOutputValidator
api=m._load_aos()
root,aos,transport,evidence,work=map(Path,sys.argv[1:])
admission=object.__new__(m.ConfiguredScientistAdmissionFactory)
admission._roots={'scientist':root,'aos':aos}
admission._configs={};admission._policy=transport
admission._policy_hash=hashlib.sha256(transport.read_bytes()).hexdigest()
admission._reviewed={'aos.decider.turn.v1':'{}'}
admission._socket=Path('/run/user')/str(os.getuid())/'swapp-gpu/control.sock'
peer=api.Peer(pid=os.getpid(),uid=os.getuid(),start_ticks=1,
 boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
 invocation_id='a'*32,control_group='/cpu-fixture')
admission._authenticator=SimpleNamespace(authenticate=lambda *args:peer,still_current=lambda p:True)
now=time.clock_gettime(time.CLOCK_BOOTTIME)
schemas={name:{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
 for name,path in [('retained_transport',transport),('evidence',evidence)]}
schemas.update(budget_witness_sha256=api.digest(api.budget_schema()),
 terminal_schema_sha256=api.terminal_schema_sha256(),
 terminal_descriptor_sha256=api.terminal_descriptor_sha256,channel_descriptor_sha256=m.DESCRIPTOR_SHA256)
review={'schema':m.SCHEMA,'enabled':True,'authority':{'owner':'AGENT',
 'caller_unit':'cpu-fixture.service','broker_unit':api.broker_unit,
 'allowed_profiles':['aos.decider.turn.v1'],'control_operations':['capability','reconcile'],
 'provider_operations':['read_budget','verify_physical'],'resolution':'recorded_successful_original_only',
 'boot_id':peer.boot_id,'issued_boottime':now-1,'expires_boottime':now+300,'max_targets':2},
 'bounds':{'control_seconds':3,'provider_seconds':3,'resolution_seconds':30},'schemas':schemas,
 'source_files':{str(base/name):hashlib.sha256((base/name).read_bytes()).hexdigest()
 for base,names in [(root,m.SCIENTIST_SOURCES),(aos,m.AOS_SOURCES)] for name in names},
 'config_files':{str(transport):admission._policy_hash}}
factory=m.ConfiguredNativeRetainedFactory(admission,review)
factory._schemas={name:Path(value['path']).read_bytes() for name,value in schemas.items()
 if type(value) is dict}
factory._schema_hashes={name:api.digest(json.loads(raw)) for name,raw in factory._schemas.items()}
store=TrajectoryStore(work/'cpu-native.sqlite3')
history=ScientistAdmissionHistory(store,record_version='2.0')
validator=AdmissionProfileOutputValidator(history,
 {'name':'aos-scientist-profile-output.v2','version':2,'bundle_sha256':'b'*64})
desktop=api.Binding(SimpleNamespace(store=store),admission_history=history,output_validator=validator)
intent=api.Intent(session_id='fixture',runtime_id='fixture',lease_id='fixture',generation=1,
 owner='AGENT',authorization_context_sha256='c'*64)
options=factory._host_options(desktop,intent)
host=api.Host(store,history,intent,**options)
assert isinstance(host.verifier,api.TerminalVerifier)
assert host.verifier.budget_verifier.history is history
try:host.verifier.budget_verifier.read_source(None,None)
except api.ScientistAdmissionError:pass
else:raise AssertionError('unprepared proof allowed')
one,two=socket.socketpair(socket.AF_UNIX,socket.SOCK_SEQPACKET)
with _deadline_scope(api,3):
 client=m._provider_client(factory,one,peer)
 assert isinstance(client,api.Client)
 assert client.expected_peer==peer
 client.close()
two.close();factory.close();store.connection.close()
print('native composition PASS')
"""
    result = subprocess.run(
        [
            str(interpreter),
            "-c",
            code,
            str(root),
            str(aos_root),
            str(transport),
            str(evidence),
            str(tmp_path),
        ],
        env=os.environ
        | {
            "PYTHONPATH": str(root) + os.pathsep + str(aos_root / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "CUDA_VISIBLE_DEVICES": "",
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "native composition PASS"
