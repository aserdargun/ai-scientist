"""Aggregate native factory budgets use absolute monotonic deadlines, without services."""

from types import SimpleNamespace

import pytest
from test_aos_configured_runtime_factory import configured as configured
from test_aos_native_admission_factory import Denied
from test_aos_native_admission_factory import context as context

from lab.llm.aos_gpu_control_store import control_deadline
from scripts import aos_configured_runtime_factory as configured_adapter
from scripts import aos_native_admission_factory as adapter


@pytest.mark.parametrize("operation", ["expected_peer", "_current", "_verify_capture"])
def test_expired_native_outer_deadline_denies_before_authority_callbacks(
    context, monkeypatch, operation
):
    monkeypatch.setattr(adapter.time, "monotonic", lambda: 100.0)
    token = control_deadline.set(100.0)
    try:
        arguments = [context.request, context.binding]
        if operation != "expected_peer":
            arguments.append(context.peer)
        if operation == "_verify_capture":
            arguments.append(SimpleNamespace(admission_binding=context.expected))
        with pytest.raises(Denied, match="deadline"):
            getattr(context.factory, operation)(*arguments)
    finally:
        control_deadline.reset(token)
    context.reader.assert_not_called()
    context.auth.authenticate.assert_not_called()
    context.current.assert_not_called()


def test_nested_native_verifiers_share_one_budget_and_stop_after_exhaustion(context, monkeypatch):
    now, calls = [100.0], []
    monkeypatch.setattr(adapter.time, "monotonic", lambda: now[0])

    def authenticate(pid, uid, *, deadline):
        calls.append(("authenticate", deadline))
        now[0] += 0.3
        return context.peer

    def still_current(peer, *, deadline):
        calls.append(("still_current", deadline))
        now[0] += 0.3
        return True

    context.auth.authenticate = authenticate
    context.auth.still_current = still_current

    def caller(api, generation):
        calls.append(("caller", adapter._verification_deadline.get()))
        assert control_deadline.get() == 101.0
        now[0] += 0.3

    monkeypatch.setattr(adapter, "_verify_caller_service", caller)
    context.current.side_effect = lambda *_args: now.__setitem__(0, now[0] + 0.3)
    token = control_deadline.set(101.0)
    try:
        with pytest.raises(Denied, match="deadline"):
            context.factory.expected_peer(context.request, context.binding)
        assert control_deadline.get() == 101.0
    finally:
        control_deadline.reset(token)
    assert calls == [("authenticate", 101.0), ("still_current", 101.0), ("caller", 101.0)]
    assert adapter._verification_deadline.get() is None


def test_native_broker_generation_still_denies_with_outer_deadline(context, monkeypatch):
    monkeypatch.setattr(adapter.time, "monotonic", lambda: 100.0)
    context.auth.authenticate = lambda pid, uid, *, deadline: context.peer
    context.auth.still_current = lambda peer, *, deadline: False
    token = control_deadline.set(101.0)
    try:
        with pytest.raises(Denied, match="current systemd"):
            context.factory.expected_peer(context.request, context.binding)
    finally:
        control_deadline.reset(token)
    context.current.assert_not_called()


def test_authenticator_capability_requires_explicit_deadline_parameter():
    class OldBroker:
        def authenticate(self, pid, uid):
            pass

        def still_current(self, peer):
            pass

    api = SimpleNamespace(
        ScientistAdmissionError=Denied,
        SystemdBrokerAuthenticator=OldBroker,
        SystemdCallerAuthenticator=OldBroker,
    )
    with pytest.raises(Denied, match="deadline support"):
        adapter._require_deadline_support(api)


def test_caller_authentication_receives_shared_deadline(monkeypatch):
    monkeypatch.setattr(adapter.time, "monotonic", lambda: 100.0)
    generation, calls = object(), []

    class Caller:
        def authenticate(self, value, *, deadline):
            calls.append(deadline)
            return value

    api = SimpleNamespace(ScientistAdmissionError=Denied, SystemdCallerAuthenticator=Caller)
    with adapter._deadline_scope(api, 3.5):
        adapter._verify_caller_service(api, generation)
    assert calls == [103.5]


def test_configured_expired_deadline_denies_before_file_reads(configured, monkeypatch):
    monkeypatch.setattr(adapter.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(
        configured.factory,
        "_json",
        lambda *_args, **_kwargs: pytest.fail("expired deadline reached a configured file read"),
    )
    token = control_deadline.set(100.0)
    try:
        with pytest.raises(configured.api.ScientistAdmissionError, match="deadline"):
            configured.factory._verify_runtime(configured.selected)
    finally:
        control_deadline.reset(token)


def test_configured_native_two_argument_authenticator_retains_context_deadline(monkeypatch):
    now, deadlines = [100.0], []
    monkeypatch.setattr(adapter.time, "monotonic", lambda: now[0])
    api = SimpleNamespace(ScientistAdmissionError=Denied)

    class Broker:
        def authenticate(self, pid, uid, *, deadline):
            deadlines.append(deadline)
            now[0] += 0.6
            return "peer"

        def still_current(self, peer, *, deadline):
            deadlines.append(deadline)
            now[0] += 0.6
            return True

    broker = adapter._DeadlineAuthenticator(api, Broker())
    token = control_deadline.set(101.0)
    try:
        with pytest.raises(Denied, match="deadline"):
            with adapter._deadline_scope(api, 3.5):
                assert broker.authenticate(12, 34) == "peer"
                broker.still_current("peer")
    finally:
        control_deadline.reset(token)
    assert deadlines == [101.0, 101.0]
    assert configured_adapter._verification_deadline.get() is None


def test_configured_runtime_authentication_and_attestation_share_outer_budget(
    configured, monkeypatch
):
    now, deadlines = [100.0], []
    monkeypatch.setattr(adapter.time, "monotonic", lambda: now[0])

    def authenticate(pid, uid, *, deadline):
        deadlines.append(deadline)
        now[0] += 0.3
        return configured.peer

    def still_current(peer, *, deadline):
        deadlines.append(deadline)
        now[0] += 0.3
        return True

    def caller(api, generation):
        deadlines.append(adapter._verification_deadline.get())
        now[0] += 0.3

    def attest(*args):
        assert control_deadline.get() == 101.0
        now[0] += 0.3

    configured.auth.authenticate = authenticate
    configured.auth.still_current = still_current
    monkeypatch.setattr(configured_adapter, "_verify_caller_service", caller)
    configured.attestation.side_effect = attest
    token = control_deadline.set(101.0)
    try:
        with pytest.raises(configured.api.ScientistAdmissionError, match="deadline"):
            configured.factory._verify_runtime(configured.selected)
    finally:
        control_deadline.reset(token)
    assert deadlines == [101.0, 101.0, 101.0]
    configured.attestation.assert_called_once()


def test_configured_callback_gets_shared_context_when_original_deadline_is_none(
    configured, monkeypatch
):
    now, observed = [100.0], []
    monkeypatch.setattr(adapter.time, "monotonic", lambda: now[0])

    def authenticate(pid, uid, *, deadline):
        observed.append(deadline)
        now[0] += 0.3
        return configured.peer

    def still_current(peer, *, deadline):
        observed.append(deadline)
        now[0] += 0.3
        return True

    def attest(*args):
        observed.append(control_deadline.get())
        assert control_deadline.get() == 104.0
        assert control_deadline.get() - now[0] < 4.0

    configured.auth.authenticate = authenticate
    configured.auth.still_current = still_current
    configured.attestation.side_effect = attest
    token = control_deadline.set(None)
    try:
        configured.factory._verify_runtime(configured.selected)
        assert control_deadline.get() is None
    finally:
        control_deadline.reset(token)
    assert observed == [104.0] * 4
    assert adapter._verification_deadline.get() is None


def test_native_source_receives_one_outer_deadline_and_runtime_does_not_reset_it(
    configured, monkeypatch
):
    now, observed = [100.0], []
    monkeypatch.setattr(adapter.time, "monotonic", lambda: now[0])
    configured.api.native_deadline_required = True
    configured.auth.authenticate = lambda pid, uid, *, deadline: configured.peer
    configured.auth.still_current = lambda peer, *, deadline: True
    original_json = configured.factory._json

    def read(path, deadline, **kwargs):
        observed.append(deadline)
        return original_json(path, deadline, **kwargs)

    class Source:
        def __call__(self, selected, *, deadline=None):
            observed.append(deadline)
            now[0] += 0.1
            configured.factory._verify_runtime(selected)
            observed.append(control_deadline.get())
            return None

    configured.factory._source = Source()
    monkeypatch.setattr(configured.factory, "_json", read)
    token = control_deadline.set(101.0)
    try:
        configured.factory._verify_source(configured.selected)
        assert control_deadline.get() == 101.0
    finally:
        control_deadline.reset(token)
    assert len(observed) > 3
    assert set(observed) == {101.0}
    assert adapter._verification_deadline.get() is None


def test_expired_native_source_outer_deadline_denies_before_source_read(configured, monkeypatch):
    monkeypatch.setattr(adapter.time, "monotonic", lambda: 100.0)
    configured.api.native_deadline_required = True

    class Source:
        def __call__(self, selected, *, deadline=None):
            pytest.fail("Expired outer deadline reached a source read")

    configured.factory._source = Source()
    token = control_deadline.set(100.0)
    try:
        with pytest.raises(configured.api.ScientistAdmissionError, match="deadline"):
            configured.factory._verify_source(configured.selected)
    finally:
        control_deadline.reset(token)


def test_actual_native_source_without_deadline_support_denies_before_desktop(
    configured, monkeypatch
):
    class OldSource:
        def __call__(self, selected):
            pytest.fail("Incompatible native source verifier was called")

    configured.api.native_deadline_required = True
    configured.api.ScientistConfiguredSourceVerifier = OldSource
    configured.factory._source = OldSource()
    monkeypatch.setattr(
        configured.api,
        "ScientistBootstrapAdmissionFactory",
        lambda *_args, **_kwargs: pytest.fail("Incompatible source API reached native bootstrap"),
    )
    with pytest.raises(
        configured.api.ScientistAdmissionError, match="source verifier outer deadline"
    ):
        configured_adapter.ConfiguredScientistAdmissionFactory(**configured.arguments)
    with pytest.raises(
        configured.api.ScientistAdmissionError, match="source verifier outer deadline"
    ):
        configured_adapter._require_source_deadline_support(configured.api)
    with pytest.raises(
        configured.api.ScientistAdmissionError, match="source verifier outer deadline"
    ):
        configured.factory._verify_source(configured.selected)


def test_native_source_deadline_api_has_explicit_keyword_capability():
    class Source:
        def __call__(self, selected, *, deadline=None):
            return None

    api = SimpleNamespace(ScientistAdmissionError=Denied, ScientistConfiguredSourceVerifier=Source)
    configured_adapter._require_source_deadline_support(api)
