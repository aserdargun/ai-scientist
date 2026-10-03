"""AOS-only callbacks for an inherited, authenticated retained provider channel.

This module imports stdlib and the original AOS package only. The channel is
created by the existing broker; there is no listener, discovery or allocator.
The adapter reads the caller's original admission2.0 history and pinned durable
ACKs. It never dispatches, writes, commits or grants control/resolution rights.
Trusted host verify_control and verify_resolution remain separately required.
Installing callbacks is not a deployed host bootstrap or GPU acceptance.
"""

from __future__ import annotations

import array
import hashlib
import json
import math
import os
import re
import socket
import struct
import threading
import time
from copy import deepcopy

SCHEMA = "aos-scientist-retained-provider.v1"
VERSION = 1
FRAME_LIMIT = 128 * 1024
_RESPONSE_KEYS = {"schema", "version", "sequence", "ok", "data", "reason_code"}
_TARGET_KEYS = {"request_id", "request_sha256", "original_peer_generation_sha256"}


class RetainedProviderError(RuntimeError):
    """Provider identity, retained preimages or independent proof was denied."""


def _require(condition, message):
    if not condition:
        raise RetainedProviderError(message)


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _unique(pairs):
    value = {}
    for key, item in pairs:
        _require(key not in value, "duplicate provider JSON member")
        value[key] = item
    return value


def _constant(_value):
    raise RetainedProviderError("nonfinite provider JSON number")


def decode(raw):
    """Require one bounded canonical JSON object, including integer lexemes."""
    _require(type(raw) is bytes and 0 < len(raw) <= FRAME_LIMIT, "provider frame exceeds bound")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_constant)
        _require(
            type(value) is dict and canonical(value).encode("utf-8") == raw,
            "provider frame is not a canonical object",
        )
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError) as error:
        raise RetainedProviderError("malformed provider JSON") from error


def _hash(value, length=64):
    return type(value) is str and re.fullmatch(r"[a-f0-9]{" + str(length) + r"}", value) is not None


def _target(value):
    _require(
        type(value) is dict
        and set(value) == _TARGET_KEYS
        and _hash(value["request_id"], 32)
        and all(_hash(value[key]) for key in _TARGET_KEYS - {"request_id"}),
        "provider target differs",
    )


def _credentials(ancillary, flags):
    """Close every received FD before rejecting forbidden ancillary messages."""
    credentials, forbidden = [], False
    for level, kind, value in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            handles = array.array("i")
            handles.frombytes(value[: len(value) - len(value) % handles.itemsize])
            for descriptor in handles:
                os.close(descriptor)
            forbidden = True
        elif level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS:
            if len(value) != struct.calcsize("3i"):
                forbidden = True
            else:
                credentials.append(struct.unpack("3i", value))
        else:
            forbidden = True
    _require(
        not forbidden
        and not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)
        and len(credentials) == 1,
        "provider ancillary identity is ambiguous",
    )
    return credentials[0]


class RetainedProviderClient:
    """One outstanding request; uncertain exchanges permanently close the channel.

    SCM_CREDENTIALS identifies the sender of each response, even if an inherited
    descriptor is passed onward. SO_PEERCRED alone cannot prove that identity.
    The expected BrokerPeer is supplied by trusted host setup, never by a frame.
    """

    def __init__(self, channel, *, authenticator, expected_peer, timeout_seconds=3):
        from aos.scientist_transport import BrokerPeer

        _require(
            isinstance(channel, socket.socket)
            and channel.family == socket.AF_UNIX
            and channel.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) == socket.SOCK_SEQPACKET,
            "provider requires an inherited Unix sequenced packet channel",
        )
        try:
            channel.getpeername()
            _require(
                channel.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 0,
                "provider requires a connected channel",
            )
        except OSError as error:
            raise RetainedProviderError("provider channel is not connected") from error
        _require(
            isinstance(expected_peer, BrokerPeer)
            and all(
                callable(getattr(authenticator, name, None))
                for name in ("authenticate", "still_current")
            ),
            "provider requires pinned current broker authentication",
        )
        _require(
            type(timeout_seconds) in (int, float)
            and math.isfinite(timeout_seconds)
            and 0 < timeout_seconds <= 3,
            "provider deadline exceeds its local bound",
        )
        self._channel, self.authenticator = channel, authenticator
        self.expected_peer = deepcopy(expected_peer)
        self.timeout_seconds = timeout_seconds
        self._sequence, self._poisoned = 0, False
        self._lock = threading.Lock()
        channel.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        channel.set_inheritable(False)
        self._current()

    @property
    def poisoned(self):
        return self._poisoned

    def _current(self):
        expected = deepcopy(self.expected_peer)
        _require(
            self.authenticator.authenticate(expected.pid, expected.uid) == expected
            and self.authenticator.still_current(expected) is True,
            "pinned provider broker generation changed",
        )

    def _remaining(self, deadline):
        remaining = deadline - time.monotonic()
        _require(remaining > 0, "provider deadline expired")
        self._channel.settimeout(remaining)

    def exchange(
        self,
        *,
        op,
        target,
        profile_id,
        deployment_digest,
        expected_capability_sha256,
        expected_evidence=None,
    ):
        _require(self._lock.acquire(blocking=False), "provider already has an outstanding request")
        try:
            _require(not self.poisoned, "provider channel is permanently uncertain")
            deadline = time.monotonic() + self.timeout_seconds
            _target(target)
            _require(
                op in ("read_budget", "verify_physical")
                and type(profile_id) is str
                and profile_id
                in ("aos.decider.turn.v1", "aos.bonsai.recovery.v1", "aos.bonsai.vision.v1")
                and _hash(deployment_digest)
                and _hash(expected_capability_sha256)
                and (
                    expected_evidence is None
                    if op == "read_budget"
                    else type(expected_evidence) is dict
                ),
                "provider request differs",
            )
            self._sequence += 1
            request = {
                "schema": SCHEMA,
                "version": VERSION,
                "sequence": self._sequence,
                "op": op,
                "target": deepcopy(target),
                "profile_id": profile_id,
                "deployment_digest": deployment_digest,
                "expected_capability_sha256": expected_capability_sha256,
                "expected_evidence": deepcopy(expected_evidence),
            }
            frame = canonical(request).encode("utf-8")
            decode(frame)
            self._current()
            self._remaining(deadline)
            _require(self._channel.send(frame) == len(frame), "provider send is incomplete")
            self._current()
            self._remaining(deadline)
            raw, ancillary, flags, _address = self._channel.recvmsg(
                FRAME_LIMIT + 1,
                socket.CMSG_SPACE(struct.calcsize("3i")) + socket.CMSG_SPACE(64 * 4),
                socket.MSG_CMSG_CLOEXEC,
            )
            pid, uid, _gid = _credentials(ancillary, flags)
            _require(
                (pid, uid) == (self.expected_peer.pid, self.expected_peer.uid)
                and self.authenticator.authenticate(pid, uid) == self.expected_peer,
                "provider response sender generation differs",
            )
            self._current()
            self._remaining(deadline)
            response = decode(raw)
            _require(
                set(response) == _RESPONSE_KEYS
                and response["schema"] == SCHEMA
                and type(response["version"]) is int
                and response["version"] == VERSION
                and type(response["sequence"]) is int
                and response["sequence"] == self._sequence
                and type(response["ok"]) is bool,
                "provider response correlation differs",
            )
            if response["ok"]:
                _require(
                    response["reason_code"] is None and type(response["data"]) is dict,
                    "provider success is malformed",
                )
            else:
                _require(
                    response["data"] is None and response["reason_code"] == "provider_denied",
                    "provider denial is malformed",
                )
                raise RetainedProviderError("independent retained provider denied")
            return deepcopy(response["data"])
        except BaseException:
            self._poisoned = True
            self._channel.close()
            raise
        finally:
            self._lock.release()


def _deny_current(_operation, _original, _binding):
    raise RetainedProviderError("trusted current original target authority is not configured")


class RetainedProviderAdapter:
    """Read-only callbacks over the exact original Store/history and retained host.

    verify_current(operation, original, current_binding) is mandatory current
    host authority and must return None or raise. It must independently check
    current source/target rights; successful ACKs and matching hashes cannot do
    that. The adapter separately checks current session, runtime, owner, lease,
    generation and pinned broker identity before and after each callback.
    """

    def __init__(
        self,
        host,
        client,
        *,
        capability_control_id,
        capability_response_sha256,
        reconcile_control_id,
        reconcile_response_sha256,
        verify_current=_deny_current,
    ):
        from aos.scientist_retained_host import ScientistRetainedHost

        _require(
            isinstance(host, ScientistRetainedHost)
            and isinstance(client, RetainedProviderClient)
            and host.history.store is host.store
            and host.history.record_version == "2.0",
            "provider requires the original retained host and admission history2.0",
        )
        _require(
            _hash(capability_control_id, 32)
            and _hash(reconcile_control_id, 32)
            and capability_control_id != reconcile_control_id
            and _hash(capability_response_sha256)
            and _hash(reconcile_response_sha256)
            and callable(verify_current),
            "provider pinned ACK closure differs",
        )
        self.host, self.client, self.verify_current = host, client, verify_current
        self.capability_control_id, self.reconcile_control_id = (
            capability_control_id,
            reconcile_control_id,
        )
        self.capability_response_sha256 = capability_response_sha256
        self.reconcile_response_sha256 = reconcile_response_sha256

    def _authority(self, operation, original):
        from aos.contracts import digest as intent_digest
        from aos.scientist_admission_history import ScientistAdmissionRecordV2
        from aos.scientist_intents import ScientistIntentBinding

        _require(
            isinstance(original, ScientistAdmissionRecordV2),
            "legacy original admission is unsupported",
        )
        connection = self.host.store.connection
        transaction = connection.in_transaction
        retained, _checksum = self.host.history.read(original.request_id)
        _require(
            retained == original and original.session_id == self.host.binding.session_id,
            "provider original admission changed",
        )
        session = connection.execute(
            "SELECT * FROM desktop_sessions WHERE session_id=?", (self.host.binding.session_id,)
        ).fetchone()
        _require(
            session is not None
            and session["status"] == "running"
            and all(
                session[name] == getattr(self.host.binding, name)
                for name in ("runtime_id", "owner", "lease_id", "generation")
            ),
            "provider current session authority changed",
        )
        intent = connection.execute(
            "SELECT binding_json FROM scientist_turn_intents WHERE request_id=?",
            (original.request_id,),
        ).fetchone()
        _require(intent is not None, "provider original intent is missing")
        binding = ScientistIntentBinding.model_validate_json(intent["binding_json"], strict=True)
        _require(
            binding.session_id == original.session_id
            and binding.runtime_id == self.host.binding.runtime_id
            and intent_digest(binding.model_dump(mode="json")) == original.intent_binding_sha256,
            "provider original runtime or intent differs",
        )
        server, peer = original.admission_binding.server_generation, self.client.expected_peer
        _require(
            all(
                getattr(server, name) == getattr(peer, name)
                for name in (
                    "pid",
                    "uid",
                    "start_ticks",
                    "boot_id",
                    "invocation_id",
                    "control_group",
                )
            ),
            "provider broker differs from original admission server",
        )
        self.client._current()
        _require(
            self.verify_current(
                operation, original.model_copy(deep=True), self.host.binding.model_copy(deep=True)
            )
            is None,
            "provider current authority must complete or raise",
        )
        _require(
            connection.in_transaction is transaction,
            "provider callback changed transaction ownership",
        )

    @staticmethod
    def _bindings(original, request=None):
        from aos.scientist_protocol import scientist_request_sha256

        binding = original.admission_binding
        target = {
            "request_id": original.request_id,
            "request_sha256": original.request_sha256,
            "original_peer_generation_sha256": digest(
                binding.caller_generation.model_dump(mode="json")
            ),
        }
        if request is not None:
            _require(
                request.request_id == original.request_id
                and scientist_request_sha256(request) == original.request_sha256
                and request.profile_id == binding.profile_id
                and request.deployment_digest == binding.profile_pin.deployment_digest,
                "provider request differs from original admission",
            )
        return {
            "target": target,
            "profile_id": binding.profile_id,
            "deployment_digest": binding.profile_pin.deployment_digest,
        }

    def _capability(self, original):
        inspected = self.host.inspect(self.capability_control_id)
        _require(
            inspected["pending"] is False
            and inspected["control_id"] == self.capability_control_id
            and inspected["response_sha256"] == self.capability_response_sha256
            and digest(inspected["response"]) == self.capability_response_sha256,
            "provider capability ACK differs from pinned response",
        )
        response, request = inspected["response"], inspected["request"]
        _require(
            request["op"] == response["op"] == "capability"
            and response["ok"] is True
            and response["control_id"] == self.capability_control_id
            and all(request[name] == value for name, value in self._bindings(original).items())
            and inspected["admission_record_sha256"]
            == self.host.history.read(original.request_id)[1],
            "provider capability ACK differs from original target",
        )
        capability = response["data"]["capability"]
        _require(
            digest(capability) == response["capability_sha256"],
            "provider complete capability hash differs",
        )
        return deepcopy(capability)

    def _reconcile(self, original):
        capability = self._capability(original)
        inspected = self.host.inspect(
            self.reconcile_control_id, capability_control_id=self.capability_control_id
        )
        response, request = inspected["response"], inspected["request"]
        _require(
            inspected["pending"] is False
            and inspected["control_id"] == self.reconcile_control_id
            and inspected["response_sha256"] == self.reconcile_response_sha256
            and digest(response) == self.reconcile_response_sha256
            and response["ok"] is True
            and request["op"] == response["op"] == "reconcile"
            and response["control_id"] == self.reconcile_control_id
            and request["expected_capability_sha256"]
            == response["capability_sha256"]
            == digest(capability)
            and all(request[name] == value for name, value in self._bindings(original).items())
            and inspected["admission_record_sha256"]
            == self.host.history.read(original.request_id)[1],
            "provider reconcile ACK differs from pinned original closure",
        )
        evidence = response["data"]["evidence"]
        _require(
            evidence["target"] == self._bindings(original)["target"],
            "provider evidence target differs",
        )
        return deepcopy(evidence), digest(capability)

    def _witness(self, original, value):
        from aos.scientist_budget_witness import ScientistOriginalBudgetWitness
        from aos.scientist_terminal import ScientistTerminalBudget

        witness = ScientistOriginalBudgetWitness.model_validate(value, strict=True)
        _require(
            witness.model_dump(mode="json", by_alias=True) == value,
            "provider witness is not complete canonical typed data",
        )
        binding = original.admission_binding
        _require(
            all(value[name] == expected for name, expected in self._bindings(original).items())
            and witness.profile_config_sha256 == binding.profile_pin.config_sha256
            and witness.response_schema_sha256 == binding.profile_pin.response_schema_sha256
            and witness.original_admission_binding_sha256 == original.admission_binding_sha256
            and witness.original_cleanup_authorization_sha256 is None,
            "provider witness differs from exact original admission",
        )
        budget = decode(witness.budget_canonical.encode("utf-8"))
        typed = ScientistTerminalBudget.model_validate(budget, strict=True)
        _require(
            typed.model_dump(mode="json") == budget
            and digest(budget) == witness.budget_sha256
            and typed.boot_id == binding.server_generation.boot_id,
            "provider budget preimage or boot differs",
        )
        return deepcopy(value)

    def read_source(self, request, original):
        self._authority("read_budget", original)
        bindings = self._bindings(original, request)
        capability = self._capability(original)
        value = self.client.exchange(
            op="read_budget", **bindings, expected_capability_sha256=digest(capability)
        )
        result = self._witness(original, value)
        self._authority("read_budget", original)
        self._capability(original)
        return result

    def verify_source(self, request, original, witness):
        # Candidate consistency only: independent reading occurs in read_source
        # and never accepts or derives its retained budget from this candidate.
        self._authority("read_budget", original)
        self._bindings(original, request)
        self._capability(original)
        self._witness(original, witness.model_dump(mode="json", by_alias=True))
        self._authority("read_budget", original)

    def verify_resolver(self, original, terminal):
        self._authority("verify_physical", original)
        evidence, _capability_sha256 = self._reconcile(original)
        _require(
            canonical(terminal.model_dump(mode="json", by_alias=True))
            == evidence["terminal_canonical"],
            "provider resolver terminal differs from pinned original ACK",
        )
        self._authority("verify_physical", original)

    def verify_physical(self, original, terminal, allocation, drain, no_admission):
        self._authority("verify_physical", original)
        evidence, capability_sha256 = self._reconcile(original)
        for name, value in (
            ("terminal", terminal.model_dump(mode="json", by_alias=True)),
            ("allocation", allocation),
            ("drain", drain),
            ("no_admission", no_admission),
        ):
            _require(
                (None if value is None else canonical(value)) == evidence[name + "_canonical"],
                "provider physical arguments differ from retained canonical preimages",
            )
        # Include result_canonical unchanged: removing it would change the full
        # durable ACK closure and create a different physical source request.
        snapshot = self.client.exchange(
            op="verify_physical",
            **self._bindings(original),
            expected_capability_sha256=capability_sha256,
            expected_evidence=evidence,
        )
        _require(
            snapshot.get("schema") == "aos-scientist-physical-snapshot.v1"
            and type(snapshot.get("version")) is int
            and snapshot["version"] == 1
            and snapshot.get("evidence") == evidence,
            "provider physical snapshot differs from exact retained evidence",
        )
        self._authority("verify_physical", original)
        _require(
            self._reconcile(original)[0] == evidence,
            "provider ACK closure changed during physical observation",
        )
