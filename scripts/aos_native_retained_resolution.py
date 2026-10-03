"""Configured retained resolution in the original AOS desktop owner thread.

The launch host must supply reviewed host options and independent current rights.
Neither constructing this factory nor attaching a resolver dispatches controls.
Call ``factory(binding, current_binding).resolve_successful(request_id)`` only
from the original owner loop after a recorded successful native inference. AOS
owns every evidence/resolution transaction; this module never commits, allocates,
rearms a client, starts a listener, or changes an original inference intent.
"""

from __future__ import annotations

import re
import threading
import uuid
from types import SimpleNamespace


def _load_aos():
    from aos.scientist_admission_history import ScientistAdmissionRecordV2
    from aos.scientist_desktop import ScientistDesktopBinding
    from aos.scientist_intents import ScientistIntentBinding
    from aos.scientist_successful_resolution import ScientistRetainedResolutionResult
    from aos.scientist_transport import ScientistAdmissionError

    return SimpleNamespace(
        Record=ScientistAdmissionRecordV2,
        Binding=ScientistDesktopBinding,
        IntentBinding=ScientistIntentBinding,
        ResolutionResult=ScientistRetainedResolutionResult,
        Error=ScientistAdmissionError,
    )


def _require(api, condition, reason):
    if not condition:
        raise api.Error(reason)


class ConfiguredNativeRetainedResolver:
    """Trusted owner-loop factory; absence of any provider denies operation.

    ``host_options_factory(binding, current_binding)`` supplies the existing
    create_retained_host keyword arguments: pinned transport/evidence schemas,
    authenticated peer, retained terminal verifier, and explicit control and
    resolution authority. ``verify_current(binding, current_binding, original)``
    independently checks current rights/source/peer/finite bounds and must return
    None or raise. It must preserve the original connection and transaction.
    ``prepare_resolution(host, original, capability_id, capability_response_sha256,
    reconcile_id, reconcile_response_sha256)`` binds independently gated proof
    callbacks after both durable ACKs; it must return None or raise without
    changing those ACKs, original ownership, or transaction ownership. It runs
    once per sequence; completed retries retain the exact prepared closure.
    These callbacks are host configuration, never model or HTTP input.
    """

    def __init__(self, *, host_options_factory=None, verify_current=None, prepare_resolution=None):
        self.host_options_factory = host_options_factory
        self.verify_current = verify_current
        self.prepare_resolution = prepare_resolution

    def __call__(self, desktop_binding, current_binding):
        api = _load_aos()
        _require(
            api,
            callable(self.host_options_factory)
            and callable(self.verify_current)
            and callable(self.prepare_resolution),
            "Native retained resolution requires explicit trusted host providers",
        )
        _require(
            api,
            isinstance(desktop_binding, api.Binding)
            and isinstance(current_binding, api.IntentBinding),
            "Native retained resolution requires the original typed desktop binding",
        )
        resolver = NativeRetainedOwnerResolver(
            desktop_binding, current_binding, self.verify_current, self.prepare_resolution, api=api
        )
        resolver._current()
        options = self.host_options_factory(
            desktop_binding, resolver.current_binding.model_copy(deep=True)
        )
        resolver._current()
        _require(
            api,
            type(options) is dict
            and all(options.get(name) is not None for name in ("authenticator", "verifier"))
            and all(
                callable(options.get(name)) for name in ("verify_control", "verify_resolution")
            ),
            "Native retained resolution requires peer, proof, control and resolution providers",
        )
        resolver.host = desktop_binding.create_retained_host(resolver.current_binding, **options)
        resolver._current()
        _require(
            api,
            resolver.host.store is resolver.store
            and resolver.host.history is resolver.history
            and resolver.host.binding == resolver.current_binding,
            "Native retained host changed its original store, history or binding",
        )
        return resolver


class NativeRetainedOwnerResolver:
    """One bounded discover/reconcile/resolve sequence per original target.

    An interrupted sequence is retained and never automatically dispatched
    again. A completed sequence may only repeat its exact resolution, which
    rechecks current rights and independent proof through the AOS journal.
    """

    def __init__(
        self, desktop_binding, current_binding, verify_current, prepare_resolution, *, api
    ):
        self.api = api
        self.desktop_binding = desktop_binding
        self.controller = desktop_binding.controller
        self.store = self.controller.store
        self.connection = self.store.connection
        self.history = desktop_binding.admission_history
        self.scheduler = desktop_binding.scheduler
        self.current_binding = current_binding.model_copy(deep=True)
        self.verify_current = verify_current
        self.prepare_resolution = prepare_resolution
        self.owner_thread = threading.get_ident()
        self.host = None
        self.attempts = {}
        self.busy = False

    def _current(self, original=None):
        _require(
            self.api,
            threading.get_ident() == self.owner_thread,
            "Native retained resolution must run on its original owner thread",
        )
        _require(
            self.api,
            self.desktop_binding.controller is self.controller
            and self.controller.store is self.store
            and self.store.connection is self.connection
            and not self.connection.in_transaction
            and self.desktop_binding.admission_history is self.history
            and self.history is not None
            and self.history.store is self.store
            and self.history.record_version == "2.0"
            and self.desktop_binding.scheduler is self.scheduler
            and self.scheduler is not None
            and not self.scheduler.closed
            and not self.scheduler.restart_quiesced,
            "Native retained original owner/store/history/transaction is unavailable",
        )
        state = self.controller.state()
        if self.host is not None:
            _require(
                self.api,
                self.host.store is self.store
                and self.host.history is self.history
                and self.host.binding == self.current_binding,
                "Native retained host changed its original store, history or binding",
            )
        _require(
            self.api,
            self.current_binding.owner == "AGENT"
            and state.get("status") == "running"
            and all(
                state.get(name) == getattr(self.current_binding, name)
                for name in ("session_id", "runtime_id", "owner", "lease_id", "generation")
            ),
            "Native retained current session/runtime/owner/lease/generation changed",
        )
        if original is not None:
            result = self.verify_current(
                self.desktop_binding,
                self.current_binding.model_copy(deep=True),
                original.model_copy(deep=True),
            )
            _require(self.api, result is None, "Native retained rights must complete or raise")
            self._current()

    def _ack(self, control_id, request_id, operation, capability_id=None):
        value = self.host.inspect(control_id, capability_control_id=capability_id)
        _require(
            self.api,
            value["control_id"] == control_id
            and value["request"]["target"]["request_id"] == request_id
            and value["request"]["op"] == operation
            and value["pending"] is False
            and value["response"]["ok"] is True
            and type(value["response_sha256"]) is str
            and re.fullmatch(r"[a-f0-9]{64}", value["response_sha256"]) is not None,
            "Native retained resolution requires the exact committed successful ACK",
        )
        return value["response_sha256"]

    def resolve_successful(self, request_id):
        self._current()
        _require(self.api, not self.busy, "Native retained resolution is already active")
        _require(
            self.api,
            type(request_id) is str and re.fullmatch(r"[a-f0-9]{32}", request_id),
            "Native retained resolution requires an exact original request ID",
        )
        original, checksum = self.history.read(request_id)
        _require(self.api, isinstance(original, self.api.Record), "Legacy admission is unsupported")
        row = self.connection.execute(
            "SELECT * FROM scientist_turn_intents WHERE request_id=?",
            (request_id,),
        ).fetchone()
        _require(
            self.api,
            row is not None
            and row["state"] == "receipt_recorded"
            and row["receipt_json"] is not None,
            "Only an original recorded successful inference can use native resolution",
        )
        intent_binding = self.api.IntentBinding.model_validate_json(
            row["binding_json"], strict=True
        )
        original_intent = dict(row)
        _require(
            self.api,
            intent_binding == self.current_binding
            and original.session_id == self.current_binding.session_id,
            "Native retained target differs from its original intent binding",
        )
        self._current(original)
        prior = self.attempts.get(request_id)
        _require(
            self.api,
            prior is None or prior["completed"],
            "Interrupted retained controls require explicit historical reconciliation",
        )
        if prior is None:
            prior = {
                "capability_id": uuid.uuid4().hex,
                "reconcile_id": uuid.uuid4().hex,
                "completed": False,
                "capability_response_sha256": None,
                "response_sha256": None,
            }
            self.attempts[request_id] = prior
        self.busy = True
        try:
            if not prior["completed"]:
                self.host.discover(request_id, prior["capability_id"])
                self._current(original)
                prior["capability_response_sha256"] = self._ack(
                    prior["capability_id"], request_id, "capability"
                )
                self.host.reconcile(request_id, prior["capability_id"], prior["reconcile_id"])
                self._current(original)
                prior["response_sha256"] = self._ack(
                    prior["reconcile_id"], request_id, "reconcile", prior["capability_id"]
                )
                self._current(original)
                _require(
                    self.api,
                    self._ack(prior["capability_id"], request_id, "capability")
                    == prior["capability_response_sha256"],
                    "Native retained preparation requires its original durable capability ACK",
                )
                prepared = self.prepare_resolution(
                    self.host,
                    original.model_copy(deep=True),
                    prior["capability_id"],
                    prior["capability_response_sha256"],
                    prior["reconcile_id"],
                    prior["response_sha256"],
                )
                _require(
                    self.api, prepared is None, "Native retained preparation must complete or raise"
                )
                self._current(original)
                _require(
                    self.api,
                    self._ack(prior["capability_id"], request_id, "capability")
                    == prior["capability_response_sha256"]
                    and self._ack(
                        prior["reconcile_id"], request_id, "reconcile", prior["capability_id"]
                    )
                    == prior["response_sha256"],
                    "Native retained preparation changed its exact durable ACK hashes",
                )
            self._current(original)
            _require(
                self.api,
                self.history.read(request_id)[1] == checksum,
                "Original retained admission changed during resolution",
            )
            result = self.host.resolve(
                prior["reconcile_id"],
                prior["capability_id"],
                response_sha256=prior["response_sha256"],
            )
            self._current(original)
            resolved_binding = self.api.IntentBinding.model_validate_json(
                result["current_binding_json"], strict=True
            )
            _require(
                self.api,
                result["request_id"] == request_id
                and result["admission_record_sha256"] == checksum
                and result["control_id"] == prior["reconcile_id"]
                and result["response_sha256"] == prior["response_sha256"]
                and resolved_binding == self.current_binding
                and self.host.inspect_resolution(request_id, prior["capability_id"]) == result,
                "Native retained resolution differs from its exact durable original target",
            )
            after = self.connection.execute(
                "SELECT * FROM scientist_turn_intents WHERE request_id=?", (request_id,)
            ).fetchone()
            _require(
                self.api,
                after is not None and dict(after) == original_intent,
                "Native retained resolution changed its original inference intent",
            )
            self._current(original)
            typed_result = self.api.ResolutionResult.model_validate(
                {
                    "request_id": request_id,
                    "admission_record_sha256": checksum,
                    "reconcile_control_id": prior["reconcile_id"],
                    "response_sha256": prior["response_sha256"],
                    "terminal_receipt_sha256": result["terminal_receipt_sha256"],
                    "current_binding": resolved_binding,
                    "state": "resolved",
                    "original_intent_unchanged": True,
                },
                strict=True,
            )
            prior["completed"] = True
            return typed_result
        finally:
            self.busy = False
