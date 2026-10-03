"""Reviewed retained proof composition inside the original native AOS owner.

This adapter opens only the broker's authenticated readback channel, after both
original control ACKs are durable. Native AOS journals own every transaction.
Configuration and ACKs confer no admission, allocation, renewal or rearm rights.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import stat
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace

from scripts.aos_configured_runtime_factory import ConfiguredScientistAdmissionFactory
from scripts.aos_native_admission_factory import _deadline_scope, _verification_deadline
from scripts.aos_native_artifact_receipts import artifact_verification_scope
from scripts.aos_native_retained_channel import DESCRIPTOR_SHA256, open_retained_provider_channel
from scripts.aos_native_retained_resolution import ConfiguredNativeRetainedResolver

SCHEMA = "scientist.native-retained-factory-review.v1"
_control_codec = ContextVar("native_retained_control_codec", default=None)
SCIENTIST_SOURCES = (
    "scripts/aos_native_retained_factory.py",
    "scripts/aos_native_retained_resolution.py",
    "scripts/aos_native_retained_channel.py",
    "scripts/aos_configured_runtime_factory.py",
    "scripts/aos_native_admission_factory.py",
    "scripts/aos_native_artifact_receipts.py",
    "lab/llm/aos_retained_channel.py",
    "lab/llm/aos_retained_provider.py",
    "lab/llm/aos_physical_readback.py",
    "lab/llm/aos_retained_evidence_transport.py",
    "lab/llm/aos_evidence_transport.py",
    "lab/llm/native_runtime.py",
    "lab/llm/contracts/retained_channel_v1/descriptor.json",
)
AOS_SOURCES = tuple(
    "src/aos/" + name + ".py"
    for name in (
        "scientist_async",
        "scientist_desktop",
        "scientist_intents",
        "scientist_admission_history",
        "scientist_budget_witness",
        "scientist_retained_provider",
        "scientist_retained_host",
        "scientist_retained_evidence_transport",
        "scientist_evidence_transport",
        "scientist_evidence_client",
        "scientist_evidence_journal",
        "scientist_resolution",
        "scientist_successful_resolution",
        "scientist_release_proof",
        "scientist_terminal",
        "scientist_profile_output",
        "scientist_protocol",
        "scientist_transport",
        "scientist_source_authority",
    )
)


def _load_aos():
    from aos.contracts import digest
    from aos.scientist_admission_history import ScientistAdmissionRecordV2
    from aos.scientist_async import ScientistAsyncTurnClient
    from aos.scientist_budget_witness import (
        ScientistBudgetWitnessVerifier,
        ScientistRetainedTerminalVerifier,
        budget_witness_schema,
    )
    from aos.scientist_desktop import ScientistDesktopBinding
    from aos.scientist_intents import ScientistIntentBinding
    from aos.scientist_profile_output import AdmissionProfileOutputValidator
    from aos.scientist_retained_evidence_transport import ScientistRetainedEvidenceCodec
    from aos.scientist_retained_host import ScientistRetainedHost
    from aos.scientist_retained_provider import (
        ScientistRetainedProviderAdapter,
        ScientistRetainedProviderClient,
    )
    from aos.scientist_terminal import TERMINAL_DESCRIPTOR_SHA256, terminal_schema_sha256
    from aos.scientist_transport import BROKER_UNIT, BrokerPeer, ScientistAdmissionError

    return SimpleNamespace(
        digest=digest,
        Record=ScientistAdmissionRecordV2,
        AsyncClient=ScientistAsyncTurnClient,
        Binding=ScientistDesktopBinding,
        Intent=ScientistIntentBinding,
        OutputValidator=AdmissionProfileOutputValidator,
        Host=ScientistRetainedHost,
        BudgetVerifier=ScientistBudgetWitnessVerifier,
        TerminalVerifier=ScientistRetainedTerminalVerifier,
        Codec=ScientistRetainedEvidenceCodec,
        Adapter=ScientistRetainedProviderAdapter,
        Client=ScientistRetainedProviderClient,
        Peer=BrokerPeer,
        budget_schema=budget_witness_schema,
        terminal_schema_sha256=terminal_schema_sha256,
        terminal_descriptor_sha256=TERMINAL_DESCRIPTOR_SHA256,
        broker_unit=BROKER_UNIT,
        ScientistAdmissionError=ScientistAdmissionError,
    )


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _finite(value, maximum):
    return type(value) in (int, float) and math.isfinite(value) and 0 < value <= maximum


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate reviewed JSON member")
        result[key] = value
    return result


class ConfiguredNativeRetainedFactory:
    """Opt-in, finite original-target authority with sticky owner resolver cache."""

    def __init__(self, admission_factory, review):
        if not isinstance(admission_factory, ConfiguredScientistAdmissionFactory):
            raise TypeError("The configured native admission factory is required")
        self._admission = admission_factory
        self._api = _load_aos()
        self._review = json.loads(_canonical(review), object_pairs_hook=_unique)
        self._closed = False
        self._owner_thread = threading.get_ident()
        self._resolvers = {}
        self._targets = set()
        self._gates = {}
        self._client = None
        self._channel_attempted = False
        self._active = None
        self._call_deadlines = {}
        self._validate_review()

    def _require(self, condition, reason):
        if not condition:
            raise self._api.ScientistAdmissionError(reason)

    def _validate_review(self):
        review = self._review
        self._require(
            type(review) is dict
            and set(review)
            == {
                "schema",
                "enabled",
                "authority",
                "bounds",
                "schemas",
                "source_files",
                "config_files",
            }
            and review["schema"] == SCHEMA
            and review["enabled"] is True,
            "Explicit reviewed retained configuration is required",
        )
        authority, bounds, schemas = review["authority"], review["bounds"], review["schemas"]
        self._require(
            type(authority) is dict
            and set(authority)
            == {
                "owner",
                "caller_unit",
                "broker_unit",
                "allowed_profiles",
                "control_operations",
                "provider_operations",
                "resolution",
                "boot_id",
                "issued_boottime",
                "expires_boottime",
                "max_targets",
            },
            "Retained authority fields differ",
        )
        self._require(
            authority["owner"] == "AGENT"
            and authority["broker_unit"] == self._api.broker_unit
            and authority["control_operations"] == ["capability", "reconcile"]
            and authority["provider_operations"] == ["read_budget", "verify_physical"]
            and authority["resolution"] == "recorded_successful_original_only"
            and type(authority["allowed_profiles"]) is list
            and 1 <= len(authority["allowed_profiles"]) <= 3
            and len(set(authority["allowed_profiles"])) == len(authority["allowed_profiles"])
            and set(authority["allowed_profiles"]).issubset(self._admission._reviewed)
            and type(authority["max_targets"]) is int
            and 1 <= authority["max_targets"] <= 16
            and _finite(authority["issued_boottime"], 2**53 - 1)
            and _finite(authority["expires_boottime"], 2**53 - 1)
            and 0 < authority["expires_boottime"] - authority["issued_boottime"] <= 3600,
            "Retained rights are absent, broad or unbounded",
        )
        self._require(
            type(bounds) is dict
            and set(bounds) == {"control_seconds", "provider_seconds", "resolution_seconds"}
            and _finite(bounds["control_seconds"], 3)
            and _finite(bounds["provider_seconds"], 3)
            and _finite(bounds["resolution_seconds"], 30),
            "Retained operation bounds differ",
        )
        self._require(
            type(schemas) is dict
            and set(schemas)
            == {
                "retained_transport",
                "evidence",
                "budget_witness_sha256",
                "terminal_schema_sha256",
                "terminal_descriptor_sha256",
                "channel_descriptor_sha256",
            },
            "Retained schema fields differ",
        )
        for name in ("retained_transport", "evidence"):
            self._require(
                type(schemas[name]) is dict and set(schemas[name]) == {"path", "sha256"},
                "Reviewed schema file receipt differs",
            )
            self._path(schemas[name]["path"])
            self._fingerprint(schemas[name]["sha256"])
        for name in set(schemas) - {"retained_transport", "evidence"}:
            self._fingerprint(schemas[name])
        self._require(
            schemas["channel_descriptor_sha256"] == DESCRIPTOR_SHA256
            and schemas["terminal_descriptor_sha256"] == self._api.terminal_descriptor_sha256
            and schemas["terminal_schema_sha256"] == self._api.terminal_schema_sha256()
            and schemas["budget_witness_sha256"] == self._api.digest(self._api.budget_schema()),
            "Native retained schema identity differs",
        )
        for name in ("source_files", "config_files"):
            self._require(
                type(review[name]) is dict and review[name], "Retained file closure is absent"
            )
            for path, fingerprint in review[name].items():
                self._path(path)
                self._fingerprint(fingerprint)
        required = [self._admission._roots["scientist"] / name for name in SCIENTIST_SOURCES]
        required.extend(self._admission._roots["aos"] / name for name in AOS_SOURCES)
        self._require(
            all(str(path) in review["source_files"] for path in required),
            "Retained provider, observer and native proof source closure is incomplete",
        )
        configs = dict(self._admission._configs)
        configs[self._admission._policy] = self._admission._policy_hash
        self._require(
            all(
                review["config_files"].get(str(path)) == digest for path, digest in configs.items()
            ),
            "Original configured file receipts differ",
        )

    def _fingerprint(self, value):
        self._require(
            type(value) is str and re.fullmatch(r"[a-f0-9]{64}", value),
            "An explicit SHA-256 receipt is required",
        )

    def _path(self, value):
        self._require(
            type(value) is str and Path(value).is_absolute() and str(Path(value)) == value,
            "An exact absolute reviewed path is required",
        )
        return Path(value)

    def _read(self, path, expected):
        self._check_deadline()
        descriptor = os.open(
            self._path(path), os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        )
        try:
            self._require(
                stat.S_ISREG(os.fstat(descriptor).st_mode), "Reviewed file is not regular"
            )
            data = bytearray()
            while chunk := os.read(descriptor, 4 * 1024 * 1024 + 1 - len(data)):
                data.extend(chunk)
                self._require(len(data) <= 4 * 1024 * 1024, "Reviewed file exceeds its bound")
                self._check_deadline()
            self._require(
                hashlib.sha256(data).hexdigest() == expected, "Reviewed retained file changed"
            )
            return bytes(data)
        finally:
            os.close(descriptor)

    def _check_deadline(self):
        deadline = _verification_deadline.get()
        self._require(
            deadline is not None and time.monotonic() < deadline,
            "Original retained verification deadline expired",
        )
        if self._active is not None:
            binding, client, context, request_id = self._active
            self._require(
                binding.engine.client is client
                and client._context is context
                and isinstance(client, self._api.AsyncClient)
                and not client._cancelled
                and client._active is not None
                and not client._active.done()
                and getattr(client, "_active_request_id", None) == request_id
                and asyncio.get_running_loop() is context[0]
                and not context[1].is_set()
                and time.monotonic() < context[2]
                and request_id in self._targets,
                "Original async inference context changed, was cancelled or expired",
            )

    def _rights(self):
        self._check_deadline()
        authority = self._review["authority"]
        self._require(
            not self._closed and threading.get_ident() == self._owner_thread,
            "Original retained owner thread is unavailable",
        )
        self._require(
            Path("/proc/sys/kernel/random/boot_id").read_text().strip() == authority["boot_id"]
            and authority["issued_boottime"]
            <= time.clock_gettime(time.CLOCK_BOOTTIME)
            < authority["expires_boottime"],
            "Retained rights expired or belong to another boot",
        )

    @contextmanager
    def _operation_scope(self, deadline):
        """Keep immutable work within this one existing operation deadline."""
        with artifact_verification_scope(deadline, check_current=self._check_deadline) as operation:
            codec = SimpleNamespace(factory=self, operation=operation, constructor=None, raw=None)
            token = _control_codec.set(codec if operation is not None else None)
            try:
                yield
            finally:
                _control_codec.reset(token)

    def _configuration(self, selected):
        self._rights()
        for name in ("source_files", "config_files"):
            for path, fingerprint in self._review[name].items():
                self._read(path, fingerprint)
        schemas = self._review["schemas"]
        raw = {
            name: self._read(schemas[name]["path"], schemas[name]["sha256"])
            for name in ("retained_transport", "evidence")
        }
        # Every source/config file above and both schemas were freshly read.
        # Only deterministic Codec construction may be shared by this operation;
        # live policy/runtime/owner checks below are never shared.
        codec = _control_codec.get()
        if codec is not None:
            codec.operation.check()
        if (
            codec is not None
            and codec.factory is self
            and codec.constructor is self._api.Codec
            and codec.raw == raw
        ):
            schema_hashes = dict(codec.hashes)
        else:
            parsed = {
                name: json.loads(value, object_pairs_hook=_unique) for name, value in raw.items()
            }
            schema_hashes = {name: self._api.digest(value) for name, value in parsed.items()}
            self._api.Codec(
                raw["retained_transport"],
                transport_schema_sha256=schema_hashes["retained_transport"],
                evidence_schema_sha256=schema_hashes["evidence"],
            )
            if codec is not None and codec.factory is self:
                codec.constructor, codec.raw, codec.hashes = (
                    self._api.Codec,
                    dict(raw),
                    schema_hashes,
                )
        self._require(
            self._admission._verify_source(selected) is None,
            "Current configured source proof must complete or raise",
        )
        self._rights()
        self._schemas = raw
        self._schema_hashes = schema_hashes

    def verify_configuration(self):
        """Read-only preflight; never opens a channel or writes an AOS journal."""
        with _deadline_scope(self._api, 3) as deadline, self._operation_scope(deadline):
            bindings = self._admission._bindings()
            selected = {
                name: bindings[name] for name in self._review["authority"]["allowed_profiles"]
            }
            for binding in selected.values():
                self._generations(binding)
            self._configuration(selected)

    def _generations(self, binding):
        authority = self._review["authority"]
        caller, server = binding.caller_generation, binding.server_generation
        self._require(
            caller.unit == authority["caller_unit"]
            and server.unit == authority["broker_unit"]
            and caller.pid == caller.parent_pid == os.getpid()
            and caller.uid == server.uid == os.getuid()
            and caller.start_ticks == caller.parent_start_ticks
            and caller.boot_id == server.boot_id == authority["boot_id"],
            "Original caller or broker generation differs from finite reviewed rights",
        )
        return self._api.Peer(
            **{key: value for key, value in server.model_dump(mode="json").items() if key != "unit"}
        )

    def _current(self, desktop, intent, original, *, peer=None):
        with _deadline_scope(
            self._api,
            min(
                self._review["bounds"]["control_seconds"],
                self._review["bounds"]["provider_seconds"],
            ),
        ):
            self._rights()
            connection = desktop.controller.store.connection
            transaction = connection.in_transaction
            history = desktop.admission_history
            self._require(
                history.store is desktop.controller.store
                and history.record_version == "2.0"
                and intent.owner == "AGENT"
                and desktop.scheduler is not None
                and not desktop.scheduler.closed
                and not desktop.scheduler.restart_quiesced,
                "Original retained store, history or scheduler changed",
            )
            state = desktop.controller.state()
            self._require(
                state.get("status") == "running"
                and all(
                    state.get(name) == getattr(intent, name)
                    for name in ("session_id", "runtime_id", "owner", "lease_id", "generation")
                ),
                "Original session/runtime/owner/lease/generation changed",
            )
            self._require(
                isinstance(original, self._api.Record)
                and original.session_id == intent.session_id
                and original.request_id == self._active[3]
                and history.read(original.request_id)[0] == original,
                "Original retained admission target changed",
            )
            row = connection.execute(
                "SELECT * FROM scientist_turn_intents WHERE request_id=?", (original.request_id,)
            ).fetchone()
            self._require(
                row is not None
                and row["state"] == "receipt_recorded"
                and row["receipt_json"] is not None
                and self._api.Intent.model_validate_json(row["binding_json"], strict=True)
                == intent,
                "Original recorded successful intent binding differs",
            )
            binding = original.admission_binding
            self._require(
                binding.profile_id in self._review["authority"]["allowed_profiles"]
                and self._admission._bindings()[binding.profile_id] == binding,
                "Original full admission binding differs from current reviewed binding",
            )
            expected = self._generations(binding)
            self._require(
                peer is None or peer == expected, "Retained control authenticated peer differs"
            )
            self._configuration({binding.profile_id: binding})
            self._require(
                desktop.controller.store.connection is connection
                and desktop.admission_history is history
                and connection.in_transaction is transaction,
                "Retained authority callback changed original transaction ownership",
            )
            self._rights()
            return expected

    def __call__(self, desktop_binding, current_binding):
        self._require(
            isinstance(desktop_binding, self._api.Binding)
            and isinstance(current_binding, self._api.Intent),
            "Original typed native binding is required",
        )
        client = desktop_binding.engine.client
        self._require(
            isinstance(client, self._api.AsyncClient)
            and type(client._context) is tuple
            and len(client._context) == 3
            and asyncio.get_running_loop() is client._context[0]
            and not client._context[1].is_set()
            and type(getattr(client, "_active_request_id", None)) is str
            and re.fullmatch(r"[a-f0-9]{32}", client._active_request_id),
            "Original native async inference context is required",
        )
        remaining = client._context[2] - time.monotonic()
        self._require(_finite(remaining, 2**53 - 1), "Original inference deadline expired")
        key = (id(desktop_binding), _canonical(current_binding.model_dump(mode="json")))
        call_key = (key, client._context)
        self._call_deadlines.setdefault(
            call_key,
            min(
                client._context[2], time.monotonic() + self._review["bounds"]["resolution_seconds"]
            ),
        )
        with _deadline_scope(self._api, min(3, self._call_deadlines[call_key] - time.monotonic())):
            self.verify_configuration()
        if key not in self._resolvers:
            self._require(
                not self._closed and len(self._resolvers) < 16,
                "Retained factory is closed or its owner cache is exhausted",
            )
            # Reserve before construction: an interrupted preparation cannot mint new controls.
            self._resolvers[key] = None
            resolver = ConfiguredNativeRetainedResolver(
                host_options_factory=self._host_options,
                verify_current=self._owner_current,
                prepare_resolution=self._prepare,
            )(desktop_binding, current_binding)
            self._resolvers[key] = _BoundResolver(self, resolver)
        self._require(
            self._resolvers[key] is not None,
            "Interrupted owner preparation requires explicit reconciliation",
        )
        return self._resolvers[key]

    def _owner_current(self, desktop, intent, original):
        self._current(desktop, intent, original)
        self._bound_host(self._gate(desktop, intent)["host"])

    def _gate(self, desktop, intent):
        return self._gates[(id(desktop), _canonical(intent.model_dump(mode="json")))]

    def _bound_host(self, host):
        host.timeout_seconds = min(
            self._review["bounds"]["control_seconds"],
            _verification_deadline.get() - time.monotonic(),
        )
        self._require(host.timeout_seconds > 0, "Original retained control deadline expired")

    def _host_options(self, desktop, intent):
        self._require(
            isinstance(desktop.output_validator, self._api.OutputValidator)
            and desktop.output_validator.history is desktop.admission_history,
            "Original native profile output validator is required",
        )
        gate = {
            "desktop": desktop,
            "intent": intent.model_copy(deep=True),
            "adapter": None,
            "adapters": {},
            "host": None,
        }
        self._gates[(id(desktop), _canonical(intent.model_dump(mode="json")))] = gate

        def provider(method, *args):
            self._require(
                gate["adapter"] is not None, "Independent provider is not bound to durable ACKs"
            )
            with _deadline_scope(self._api, self._review["bounds"]["provider_seconds"]):
                self._check_deadline()
                result = getattr(gate["adapter"], method)(*args)
                self._check_deadline()
                return result

        def control(request, original, binding, peer):
            self._require(
                request["op"] in self._review["authority"]["control_operations"]
                and binding == gate["intent"],
                "Unreviewed retained control operation or binding",
            )
            self._current(desktop, binding, original, peer=peer)

        def resolution(original, binding, terminal, peer):
            self._require(
                binding == gate["intent"] and terminal.terminal_state == "completed",
                "Only original recorded successful inference may resolve",
            )
            self._current(desktop, binding, original, peer=peer)

        def validate_result(request, receipt):
            original = desktop.admission_history.read(request.request_id)[0]
            self._current(desktop, gate["intent"], original)
            desktop.output_validator(request, receipt)
            self._current(desktop, gate["intent"], original)

        schemas = self._review["schemas"]
        budget = self._api.BudgetVerifier(
            desktop.admission_history,
            schema_sha256=schemas["budget_witness_sha256"],
            read_source=lambda *args: provider("read_source", *args),
            verify_source=lambda *args: provider("verify_source", *args),
        )
        verifier = self._api.TerminalVerifier(
            budget,
            evidence_schema_bytes=self._schemas["evidence"],
            evidence_schema_sha256=self._schema_hashes["evidence"],
            descriptor_sha256=schemas["terminal_descriptor_sha256"],
            terminal_schema_sha256=schemas["terminal_schema_sha256"],
            validate_result=validate_result,
            verify_resolver=lambda *args: provider("verify_resolver", *args),
            verify_physical=lambda *args: provider("verify_physical", *args),
        )
        return dict(
            socket_path=self._admission._socket,
            reviewed_schema_bytes=self._schemas["retained_transport"],
            transport_schema_sha256=self._schema_hashes["retained_transport"],
            evidence_schema_sha256=self._schema_hashes["evidence"],
            authenticator=self._admission._authenticator,
            verifier=verifier,
            verify_control=control,
            verify_resolution=resolution,
            timeout_seconds=self._review["bounds"]["control_seconds"],
        )

    def _prepare(self, host, original, capability_id, capability_sha, reconcile_id, reconcile_sha):
        desktop = self._active[0]
        gate = self._gate(desktop, host.binding)
        self._require(
            host is gate["host"] and gate["adapter"] is None,
            "Retained provider preparation changed its original host",
        )
        with (
            _deadline_scope(self._api, self._review["bounds"]["provider_seconds"]) as deadline,
            self._operation_scope(deadline),
        ):
            expected = self._current(desktop, gate["intent"], original)
            if self._client is None:
                self._require(
                    not self._channel_attempted, "Uncertain provider bootstrap cannot be retried"
                )
                self._channel_attempted = True
                admission = original.admission_binding
                channel = open_retained_provider_channel(
                    self._admission._socket,
                    authenticator=self._admission._authenticator,
                    expected_peer=expected,
                    caller_generation=admission.caller_generation.model_dump(mode="json"),
                    server_generation=admission.server_generation.model_dump(mode="json"),
                    descriptor_sha256=DESCRIPTOR_SHA256,
                    verify_current=lambda: (
                        self._current(desktop, gate["intent"], original) and None
                    ),
                    timeout_seconds=min(
                        self._review["bounds"]["provider_seconds"],
                        _verification_deadline.get() - time.monotonic(),
                    ),
                )
                try:
                    self._client = _provider_client(self, channel, expected)
                except BaseException:
                    channel.close()
                    raise
            self._require(
                not self._client.poisoned and self._client.expected_peer == expected,
                "Original provider channel is closed or changed generation",
            )
            adapter = self._api.Adapter(
                host,
                self._client,
                capability_control_id=capability_id,
                capability_response_sha256=capability_sha,
                reconcile_control_id=reconcile_id,
                reconcile_response_sha256=reconcile_sha,
                verify_current=lambda operation, record, binding: self._provider_current(
                    gate, operation, record, binding
                ),
            )
            self._current(desktop, gate["intent"], original)
            gate["adapter"] = adapter
            gate["adapters"][original.request_id] = adapter

    def _provider_current(self, gate, operation, original, binding):
        self._require(
            operation in self._review["authority"]["provider_operations"]
            and binding == gate["intent"],
            "Unreviewed provider operation or binding",
        )
        self._current(gate["desktop"], binding, original)

    def close(self):
        """Close this factory's own readback FD; never changes original store/rights."""
        self._closed = True
        if self._client is not None:
            self._client.close()


def _provider_client(factory, channel, expected):
    """Native subclass clips each transport/auth window to the original scope."""

    class BoundedProvider(factory._api.Client):
        def verify_peer(self, deadline=None):
            try:
                with _deadline_scope(
                    factory._api, factory._review["bounds"]["provider_seconds"]
                ) as outer:
                    factory._check_deadline()
                    result = super().verify_peer(
                        outer if deadline is None else min(outer, deadline)
                    )
                    factory._check_deadline()
                    return result
            except BaseException:
                self.close()
                raise

        def exchange(self, **options):
            try:
                with _deadline_scope(
                    factory._api, factory._review["bounds"]["provider_seconds"]
                ) as outer:
                    factory._check_deadline()
                    self._timeout = min(
                        factory._review["bounds"]["provider_seconds"], outer - time.monotonic()
                    )
                    result = super().exchange(**options)
                    factory._check_deadline()
                    return result
            except BaseException:
                self.close()
                raise

    return BoundedProvider(
        channel,
        authenticator=factory._admission._authenticator,
        expected_peer=expected,
        timeout_seconds=factory._review["bounds"]["provider_seconds"],
    )


class _BoundResolver:
    def __init__(self, factory, resolver):
        self.factory, self.resolver = factory, resolver
        gate = factory._gate(resolver.desktop_binding, resolver.current_binding)
        gate["host"] = resolver.host
        # Each native control keeps its own three-second cap inside the original call.
        for name in ("discover", "reconcile"):
            original = getattr(resolver.host, name)
            setattr(resolver.host, name, self._control(original))
        resolver.host.resolve = self._resolve(resolver.host.resolve)

    def _control(self, method):
        def bounded(*args, **kwargs):
            with _deadline_scope(
                self.factory._api, self.factory._review["bounds"]["control_seconds"]
            ) as deadline:
                with self.factory._operation_scope(deadline):
                    self.factory._check_deadline()
                    self.factory._bound_host(self.resolver.host)
                    result = method(*args, **kwargs)
                    self.factory._check_deadline()
                    return result

        return bounded

    def _resolve(self, method):
        def bounded(*args, **kwargs):
            self.factory._check_deadline()
            # Controls and provider preparation already consumed part of this
            # original resolution deadline. Only the memo starts fresh here.
            with self.factory._operation_scope(_verification_deadline.get()):
                result = method(*args, **kwargs)
                self.factory._check_deadline()
                return result

        return bounded

    def resolve_successful(self, request_id):
        factory, desktop = self.factory, self.resolver.desktop_binding
        factory._require(factory._active is None, "Retained resolution is already active")
        client = desktop.engine.client
        factory._require(
            isinstance(client, factory._api.AsyncClient)
            and type(client._context) is tuple
            and len(client._context) == 3
            and getattr(client, "_active_request_id", None) == request_id,
            "Original native async inference context is required",
        )
        context = client._context
        factory._require(
            type(request_id) is str and re.fullmatch(r"[a-f0-9]{32}", request_id),
            "An exact original request ID is required",
        )
        factory._require(
            request_id in factory._targets
            or len(factory._targets) < factory._review["authority"]["max_targets"],
            "Retained target authority is exhausted",
        )
        factory._targets.add(request_id)
        gate = factory._gate(desktop, self.resolver.current_binding)
        gate["adapter"] = gate["adapters"].get(request_id)
        factory._active = (desktop, client, context, request_id)
        try:
            key = (id(desktop), _canonical(self.resolver.current_binding.model_dump(mode="json")))
            deadline = factory._call_deadlines[(key, context)]
            remaining = min(context[2], deadline) - time.monotonic()
            factory._require(_finite(remaining, 2**53 - 1), "Original inference deadline expired")
            with (
                _deadline_scope(
                    factory._api, min(factory._review["bounds"]["resolution_seconds"], remaining)
                ) as deadline,
                factory._operation_scope(deadline),
            ):
                factory._check_deadline()
                result = self.resolver.resolve_successful(request_id)
                factory._check_deadline()
                return result
        finally:
            factory._active = None
