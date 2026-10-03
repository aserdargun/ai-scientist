"""Trusted host adapter for the existing AOS desktop bootstrap hooks.

Load this file in the AOS interpreter, with only stdlib and AOS dependencies.
Construct ``NativeScientistAdmissionFactory`` with three host-owned callbacks:

* expected_binding(request, intent_binding) returns an independently provisioned
  complete ScientistAdmissionBindingV2, never a binding extracted from an ACK.
* verify_current(controller, request, intent_binding, expected_binding) verifies
  current source fingerprints, policy, profile/output/schema pins, runtime and
  caller rights (including revocation). It must complete with None or raise.
* persist_bootstrap_intent(frame, checksum, deadline, broker_peer) durably writes
  the control intent BEFORE send, using host-owned storage outside the original
  controller transaction. It must complete with None or raise.

Pass the instance as serve_desktop.main(scientist_admission_factory=factory,
scientist_bootstrap_expected_peer=factory.expected_peer, ...). Existing required
scientist_confirm_runtime and scientist_output_contract still come from the
trusted host. Neither importing nor constructing this adapter grants admission;
there is no default trust provider, writer, service launch or DB schema here.
"""

from __future__ import annotations

import inspect
import math
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict
from functools import wraps
from pathlib import Path
from types import SimpleNamespace

# Shared only within a verification chain; nested checks cannot restart its budget.
_verification_deadline = ContextVar("native_scientist_verification_deadline", default=None)
_deadline_requires_support = ContextVar("native_scientist_deadline_requires_support", default=False)


def _outer_deadline():
    module = sys.modules.get("lab.llm.aos_gpu_control_store")
    variable = getattr(module, "control_deadline", None)
    return variable.get() if variable is not None else None


def _deadline_check(api, deadline):
    _require(
        api,
        type(deadline) in (int, float) and math.isfinite(deadline) and time.monotonic() < deadline,
        "Native verification deadline expired or invalid",
    )


@contextmanager
def _deadline_scope(api, seconds):
    limits = [time.monotonic() + seconds]
    limits.extend(
        value for value in (_verification_deadline.get(), _outer_deadline()) if value is not None
    )
    for value in limits:
        _deadline_check(api, value)
    deadline = min(limits)
    required_token = _deadline_requires_support.set(
        _deadline_requires_support.get()
        or (_verification_deadline.get() is None and _outer_deadline() is not None)
    )
    token = _verification_deadline.set(deadline)
    module = sys.modules.get("lab.llm.aos_gpu_control_store")
    variable = getattr(module, "control_deadline", None)
    outer_token = variable.set(deadline) if variable is not None else None
    try:
        yield deadline
        _deadline_check(api, deadline)
    finally:
        if outer_token is not None:
            variable.reset(outer_token)
        _verification_deadline.reset(token)
        _deadline_requires_support.reset(required_token)


def _bounded(seconds):
    def decorate(method):
        @wraps(method)
        def checked(self, *args, **kwargs):
            with _deadline_scope(self._api, seconds):
                return method(self, *args, **kwargs)

        return checked

    return decorate


def _supports_deadline(method):
    try:
        parameter = inspect.signature(method).parameters.get("deadline")
        return parameter is not None and parameter.kind in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    except (TypeError, ValueError):
        return False


def _require_deadline_support(api):
    for authenticator, methods in (
        (api.SystemdBrokerAuthenticator, ("authenticate", "still_current")),
        (api.SystemdCallerAuthenticator, ("authenticate",)),
    ):
        for name in methods:
            _require(
                api,
                _supports_deadline(getattr(authenticator, name, None)),
                "Reviewed native authenticator outer deadline support is unavailable",
            )


def _authenticate(api, method, *args, deadline=None):
    limits = [
        value
        for value in (deadline, _verification_deadline.get(), _outer_deadline())
        if value is not None
    ]
    for value in limits:
        _deadline_check(api, value)
    deadline = min(limits) if limits else None
    if _supports_deadline(method):
        result = method(*args, deadline=deadline)
    else:
        # Only historical inert stand-ins may omit the reviewed native capability.
        _require(
            api,
            not getattr(api, "native_deadline_required", False)
            and not _deadline_requires_support.get()
            and (_verification_deadline.get() is not None or _outer_deadline() is None),
            "Reviewed native authenticator outer deadline support is unavailable",
        )
        result = method(*args)
    if deadline is not None:
        _deadline_check(api, deadline)
    return result


class _DeadlineAuthenticator:
    """Keep the native two-argument callback API and forward its current budget."""

    def __init__(self, api, authenticator):
        self._api, self._authenticator = api, authenticator

    def authenticate(self, pid, uid, *, deadline=None):
        with _deadline_scope(self._api, 3.5) as effective:
            if deadline is not None:
                _deadline_check(self._api, deadline)
                effective = min(effective, deadline)
            return _authenticate(
                self._api, self._authenticator.authenticate, pid, uid, deadline=effective
            )

    def still_current(self, peer, *, deadline=None):
        with _deadline_scope(self._api, 3.5) as effective:
            if deadline is not None:
                _deadline_check(self._api, deadline)
                effective = min(effective, deadline)
            return _authenticate(
                self._api, self._authenticator.still_current, peer, deadline=effective
            )


def _load_aos():
    """Import lazily so source checks do not require Scientist or AOS packages."""
    from aos.desktop_control import DesktopController
    from aos.scientist_admission_history import (
        ScientistAdmissionBindingV2,
        ScientistAdmissionHistory,
    )
    from aos.scientist_bootstrap import (
        CONTROL_DESCRIPTOR_SHA256,
        ScientistBootstrapCapture,
        ScientistBootstrapCodec,
    )
    from aos.scientist_intents import ScientistIntentBinding
    from aos.scientist_protocol import ScientistTurnRequest
    from aos.scientist_transport import (
        BROKER_UNIT,
        BrokerPeer,
        ScientistAdmissionError,
        SystemdBrokerAuthenticator,
        SystemdCallerAuthenticator,
        _process_identity,
    )
    from aos.storage import TrajectoryStore

    api = SimpleNamespace(
        native_deadline_required=True,
        DesktopController=DesktopController,
        ScientistAdmissionBindingV2=ScientistAdmissionBindingV2,
        ScientistAdmissionHistory=ScientistAdmissionHistory,
        CONTROL_DESCRIPTOR_SHA256=CONTROL_DESCRIPTOR_SHA256,
        ScientistBootstrapCapture=ScientistBootstrapCapture,
        ScientistBootstrapCodec=ScientistBootstrapCodec,
        ScientistIntentBinding=ScientistIntentBinding,
        ScientistTurnRequest=ScientistTurnRequest,
        BROKER_UNIT=BROKER_UNIT,
        BrokerPeer=BrokerPeer,
        ScientistAdmissionError=ScientistAdmissionError,
        SystemdBrokerAuthenticator=SystemdBrokerAuthenticator,
        SystemdCallerAuthenticator=SystemdCallerAuthenticator,
        _process_identity=_process_identity,
        TrajectoryStore=TrajectoryStore,
    )
    _require_deadline_support(api)
    return api


def _require(api, condition, message):
    if not condition:
        raise api.ScientistAdmissionError(message)


def _verify_caller_service(api, generation):
    """Use the reviewed AOS exact-unit, bounded caller-generation verifier.

    This executes only inside the original caller service, after configured
    source/dependency/artifact and current authority checks. It adds no grant.
    Missing native support fails closed; descendant cgroups are not accepted.
    """
    authenticator = getattr(api, "SystemdCallerAuthenticator", None)
    _require(api, authenticator is not None, "Reviewed native caller authenticator is unavailable")
    verified = _authenticate(api, authenticator().authenticate, generation)
    _require(api, verified == generation, "Native caller service generation differs")


class NativeScientistAdmissionFactory:
    """Bind one original desktop controller to native bootstrap/history2.0."""

    def __init__(
        self,
        control_socket,
        *,
        expected_binding=None,
        verify_current=None,
        persist_bootstrap_intent=None,
    ):
        if not all(
            callable(callback)
            for callback in (expected_binding, verify_current, persist_bootstrap_intent)
        ):
            raise TypeError(
                "Independent expected/current authority and durable writer are required"
            )
        control_socket = Path(control_socket)
        if not control_socket.is_absolute() or ".." in control_socket.parts:
            raise ValueError("Bootstrap requires an explicit absolute control socket")
        self._socket = control_socket
        self._expected_reader = expected_binding
        self._verify_host = verify_current
        self._persist = persist_bootstrap_intent
        self._api = None
        self._controller = None
        self._store = None
        self._runtime = None
        self._desktop = None
        self._history = None
        self._authenticator = None
        self._expected_by_request = {}

    def __call__(self, controller):
        api = self._api or _load_aos()
        _require(
            api,
            isinstance(controller, api.DesktopController)
            and isinstance(controller.store, api.TrajectoryStore),
            "Admission factory requires the actual desktop controller and original Store",
        )
        if self._controller is not None:
            _require(
                api,
                controller is self._controller,
                "Admission factory cannot be rebound to another controller",
            )
            self._check_desktop()
            return self._history
        _require(
            api,
            not controller.store.connection.in_transaction,
            "Admission factory must be installed outside the original transaction",
        )
        desktop = controller.state()
        _require(
            api,
            desktop["session_id"] == controller.session_id
            and desktop["runtime_id"] == controller.runtime.runtime_id
            and desktop["owner"] == "AGENT"
            and desktop["status"] == "running",
            "Admission factory requires a running current AGENT desktop",
        )
        authenticator = api.SystemdBrokerAuthenticator()
        if getattr(api, "native_deadline_required", False):
            authenticator = _DeadlineAuthenticator(api, authenticator)
        capture = api.ScientistBootstrapCapture(
            controller.store,
            self._socket,
            codec=api.ScientistBootstrapCodec(
                control_descriptor_sha256=api.CONTROL_DESCRIPTOR_SHA256
            ),
            verify_current=self._current,
            verify_capture=self._verify_capture,
            persist_intent=self._persist,
            authenticator=authenticator,
        )
        history = api.ScientistAdmissionHistory(
            controller.store,
            capture=capture,
            verify_current=self._verify_history,
            record_version="2.0",
        )
        self._api, self._controller = api, controller
        self._store, self._runtime = controller.store, controller.runtime
        self._desktop = {
            key: desktop[key]
            for key in ("session_id", "runtime_id", "owner", "lease_id", "generation")
        }
        self._authenticator, self._history = authenticator, history
        return self._history

    def _check_desktop(self, binding=None):
        api, controller = self._api, self._controller
        _require(api, controller is not None, "Admission factory has not bound a controller")
        _require(
            api,
            controller.store is self._store
            and controller.runtime is self._runtime
            and controller.session_id == self._desktop["session_id"]
            and self._runtime.runtime_id == self._desktop["runtime_id"],
            "Original controller Store, session or runtime changed",
        )
        state = controller.state()
        _require(
            api,
            state["status"] == "running"
            and all(state[key] == value for key, value in self._desktop.items()),
            "Desktop owner, lease, generation or runtime was revoked",
        )
        if binding is not None:
            _require(
                api,
                isinstance(binding, api.ScientistIntentBinding)
                and all(getattr(binding, key) == value for key, value in self._desktop.items()),
                "Intent does not belong to the current original desktop authority",
            )

    @_bounded(3.5)
    def _expected(self, request, binding):
        api = self._api
        self._check_desktop(binding)
        _require(
            api,
            isinstance(request, api.ScientistTurnRequest),
            "Expected admission requires the actual typed inference request",
        )
        in_transaction = self._store.connection.in_transaction
        value = self._expected_reader(request.model_copy(deep=True), binding.model_copy(deep=True))
        _require(
            api,
            self._store.connection.in_transaction is in_transaction,
            "Expected binding reader changed original transaction ownership",
        )
        if isinstance(value, api.ScientistAdmissionBindingV2):
            value = value.model_dump(mode="json")
        expected = api.ScientistAdmissionBindingV2.model_validate(value, strict=True)
        _require(
            api,
            expected.profile_id == request.profile_id
            and expected.profile_pin.deployment_digest == request.deployment_digest
            and expected.server_generation.unit == api.BROKER_UNIT,
            "Independent expected admission differs from the inference profile or broker",
        )
        frozen = (
            request.model_dump(mode="json"),
            binding.model_dump(mode="json"),
            expected.model_dump(mode="json"),
        )
        previous = self._expected_by_request.get(request.request_id)
        _require(
            api,
            previous is None or previous == frozen,
            "Independent expected request, intent or admission generation changed",
        )
        _require(
            api,
            previous is not None or len(self._expected_by_request) < 256,
            "Admission factory lifetime request bound reached",
        )
        self._expected_by_request[request.request_id] = deepcopy(frozen)
        self._check_desktop(binding)
        return expected

    @_bounded(3.5)
    def _current(self, request, binding, peer):
        api = self._api
        expected = self._expected(request, binding)
        _require(
            api,
            isinstance(peer, api.BrokerPeer)
            and expected.server_generation.model_dump(mode="json")
            == {**asdict(peer), "unit": api.BROKER_UNIT}
            and _authenticate(api, self._authenticator.still_current, peer) is True,
            "Expected inference broker is not the actual current systemd generation",
        )
        _verify_caller_service(api, expected.caller_generation)
        in_transaction = self._store.connection.in_transaction
        _require(
            api,
            self._verify_host(
                self._controller,
                request.model_copy(deep=True),
                binding.model_copy(deep=True),
                expected.model_copy(deep=True),
            )
            is None,
            "Current source/runtime authority must complete or raise",
        )
        _require(
            api,
            self._store.connection.in_transaction is in_transaction,
            "Current host verifier changed original transaction ownership",
        )
        self._check_desktop(binding)
        _require(
            api,
            self._expected(request, binding) == expected
            and _authenticate(api, self._authenticator.still_current, peer) is True,
            "Expected binding or broker generation changed during current verification",
        )
        _verify_caller_service(api, expected.caller_generation)

    @_bounded(3.5)
    def expected_peer(self, request, binding):
        """Independent pre-connect reader for scientist_bootstrap_expected_peer."""
        if self._controller is None:
            raise RuntimeError("Admission factory has not bound a controller")
        expected = self._expected(request, binding)
        server = expected.server_generation
        peer = _authenticate(self._api, self._authenticator.authenticate, server.pid, server.uid)
        self._current(request, binding, peer)
        return peer

    @_bounded(3.5)
    def _verify_capture(self, request, binding, peer, capture):
        self._current(request, binding, peer)
        _require(
            self._api,
            capture.admission_binding == self._expected(request, binding),
            "Bootstrap ACK differs from independently trusted complete admission binding",
        )

    @_bounded(3.5)
    def _verify_history(self, record, request, binding, peer):
        self._current(request, binding, peer)
        _require(
            self._api,
            record.admission_binding == self._expected(request, binding),
            "Historical admission differs from independently trusted current binding",
        )
