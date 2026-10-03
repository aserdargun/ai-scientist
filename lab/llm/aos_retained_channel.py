"""Bounded FD handoff on the existing authenticated broker control socket.

This bootstrap creates one read-only retained provider channel. It never opens
a listener, allocates GPU capacity, mints a capability, or resolves a journal.
The original stream request must remain unread until ``serve`` receives its
per-message kernel credentials. The control listener must enable SO_PASSCRED
before accepting connections; a zero-PID queued credential is denied.
"""

from __future__ import annotations

import array
import hashlib
import math
import os
import select
import socket
import stat
import struct
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from lab.llm import aos_control_contract_v2 as metadata
from lab.llm import aos_evidence_transport, aos_retained_evidence_transport
from lab.llm.aos_gpu_broker import PeerGeneration
from lab.llm.aos_gpu_control import LabAOSControl
from lab.llm.aos_gpu_control_store import control_deadline
from lab.llm.aos_retained_provider import RetainedProviderServer, prepare_channel
from lab.llm.native_runtime import GpuObserver, UnitManager

SCHEMA = "aos-scientist-retained-channel.v1"
DESCRIPTOR_SHA256 = "cd61387214a62ddf8fbac9f063ee2154035d9848d2c15412531f850f2ddf2600"
DESCRIPTOR_PATH = Path(__file__).parent / "contracts/retained_channel_v1/descriptor.json"
SOURCE_FILE = "lab/llm/aos_retained_channel.py"
MAX_BYTES = 8192
MAX_SECONDS = 3.0
CHANNEL_SECONDS = 600.0
IDLE_SECONDS = 1.0
_CREDENTIALS = struct.Struct("3i")
_ANCILLARY_BYTES = socket.CMSG_SPACE(_CREDENTIALS.size) + socket.CMSG_SPACE(256 * 4)
_REQUEST_KEYS = frozenset(
    {
        "schema",
        "version",
        "op",
        "nonce",
        "descriptor_sha256",
        "caller_generation_sha256",
        "server_generation_sha256",
    }
)


class RetainedChannelError(ValueError):
    """Handoff failed without granting rights or exposing private details."""


def _require(condition: bool) -> None:
    if not condition:
        raise RetainedChannelError("provider_denied")


def _remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    _require(value > 0)
    return value


def _hex(value: Any, length: int = 64) -> bool:
    return (
        type(value) is str
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _decode(line: bytes) -> dict[str, Any]:
    try:
        value = metadata.decode(line, limit=MAX_BYTES)
        _require(set(value) == _REQUEST_KEYS and value["schema"] == SCHEMA)
        _require(type(value["version"]) is int and value["version"] == 1)
        _require(value["op"] == "open" and _hex(value["nonce"], 32))
        _require(all(_hex(value[key]) for key in _REQUEST_KEYS if key.endswith("sha256")))
        return value
    except (metadata.ContractError, KeyError, TypeError, RecursionError):
        raise RetainedChannelError("provider_denied") from None


def is_request(line: bytes) -> bool:
    """Identify this wire schema from a pure canonical, bounded peek only."""
    try:
        if line.endswith(b"\n"):
            line = line[:-1]
        return metadata.decode(line, limit=MAX_BYTES).get("schema") == SCHEMA
    except (metadata.ContractError, AttributeError, TypeError, RecursionError):
        return False


def _credentials(ancillary: list[tuple[int, int, bytes]], flags: int) -> tuple[int, int]:
    identities = []
    forbidden = bool(flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC))
    for level, kind, payload in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            handles = array.array("i")
            handles.frombytes(payload[: len(payload) - len(payload) % handles.itemsize])
            for handle in handles:
                try:
                    os.close(handle)
                except OSError:
                    pass
            forbidden = True
        elif level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS:
            if len(payload) == _CREDENTIALS.size:
                identities.append(_CREDENTIALS.unpack(payload))
            else:
                forbidden = True
        else:
            forbidden = True
    _require(not forbidden and len(identities) == 1)
    pid, uid, _gid = identities[0]
    _require(pid > 1 and uid >= 0)
    return pid, uid


def _receive(connection: socket.socket, deadline: float) -> tuple[dict[str, Any], int, int]:
    _require(
        connection.family == socket.AF_UNIX
        and connection.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) == socket.SOCK_STREAM
        and connection.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 0
    )
    connection.getpeername()
    connection.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    data = bytearray()
    identity = None
    while b"\n" not in data:
        connection.settimeout(_remaining(deadline))
        raw, ancillary, flags, _address = connection.recvmsg(
            MAX_BYTES + 2 - len(data), _ANCILLARY_BYTES, socket.MSG_CMSG_CLOEXEC
        )
        observed = _credentials(ancillary, flags)
        _require(identity is None or observed == identity)
        identity = observed
        _require(bool(raw))
        data.extend(raw)
        _require(len(data) <= MAX_BYTES + 1)
    line, separator, trailing = data.partition(b"\n")
    _require(bool(separator) and not trailing and identity is not None)
    request = _decode(bytes(line))
    _remaining(deadline)
    if identity is None:
        raise RetainedChannelError("provider_denied")
    return request, identity[0], identity[1]


@dataclass(eq=False)
class _Channel:
    socket: socket.socket
    peer: PeerGeneration
    expires: float
    stopping: threading.Event
    thread: threading.Thread | None = None


class RetainedChannelBootstrap:
    """One atomic channel slot on this exact existing broker control facade."""

    is_request = staticmethod(is_request)

    def __init__(self, control: LabAOSControl, units: UnitManager, gpu: GpuObserver) -> None:
        self.control, self.units, self.gpu = control, units, gpu
        self._lock = threading.Lock()
        self._closed = False
        self._slot: _Channel | None = None

    def verify_configuration(self) -> None:
        """Require the literal descriptor and all executable source/policy pins."""
        descriptor = os.open(
            DESCRIPTOR_PATH, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        )
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            _require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= MAX_BYTES)
            raw = stream.read(MAX_BYTES + 1)
        _require(hashlib.sha256(raw).hexdigest() == DESCRIPTOR_SHA256)
        config = self.control.policy.config
        if config is None or type(config) is not dict:
            raise RetainedChannelError("provider_denied")
        _require(config.get("enabled") is True)
        required = {
            SOURCE_FILE,
            "lab/llm/aos_retained_provider.py",
            "lab/llm/aos_physical_readback.py",
        }
        _require(required <= set(config.get("source_files", {}).get("scientist", {})))
        _require(
            config.get("evidence_schema_sha256") == aos_evidence_transport.EVIDENCE_SCHEMA_SHA256
            and config.get("evidence_transport_schema_sha256")
            == aos_evidence_transport.EVIDENCE_TRANSPORT_SCHEMA_HASH
            and config.get("retained_evidence_transport_schema_sha256")
            == aos_retained_evidence_transport.EVIDENCE_TRANSPORT_SCHEMA_HASH
            and callable(self.control.server_current)
        )
        self.control.policy.verify()

    def _current(self, peer: PeerGeneration, deadline: float) -> None:
        _remaining(deadline)
        _require(
            isinstance(peer, PeerGeneration)
            and peer.pid == peer.parent_pid
            and peer.start_ticks == peer.parent_start_ticks
            and peer.uid == os.getuid()
            and self.control.policy.config is not None
            and self.control.policy.config.get("caller_unit") == peer.unit
            and self.control.server_generation.get("pid") == os.getpid()
            and self.control.server_generation.get("uid") == os.getuid()
        )
        self.control._current(peer)
        _require(self.control.authenticator.authenticate(peer.pid, peer.uid) == peer)
        self.verify_configuration()
        self.control._current(peer)
        _remaining(deadline)

    def _release(self, selected: _Channel) -> None:
        selected.stopping.set()
        try:
            selected.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        selected.socket.close()
        with self._lock:
            if self._slot is selected:
                self._slot = None

    def _worker(self, selected: _Channel) -> None:
        provider = RetainedProviderServer(
            self.control, units=self.units, gpu=self.gpu, expected_peer_generation=selected.peer
        )
        sequence = 1
        try:
            while not selected.stopping.is_set():
                deadline = min(time.monotonic() + MAX_SECONDS, selected.expires)
                token = control_deadline.set(deadline)
                try:
                    self._current(selected.peer, deadline)
                    ready, _, _ = select.select(
                        [selected.socket], [], [], min(IDLE_SECONDS, _remaining(deadline))
                    )
                    if not ready:
                        continue
                    provider.timeout_seconds = min(MAX_SECONDS, _remaining(selected.expires))
                    provider.serve_once(selected.socket, sequence)
                    sequence += 1
                finally:
                    control_deadline.reset(token)
        except (OSError, ValueError, RuntimeError, EOFError):
            pass
        finally:
            self._release(selected)

    @staticmethod
    def _send(
        connection: socket.socket,
        request: dict[str, Any],
        *,
        deadline: float,
        channel: socket.socket | None = None,
        reason: str | None = None,
    ) -> None:
        response = {**request, "ok": channel is not None, "reason_code": reason}
        raw = metadata.canonical(response) + b"\n"
        _require(len(raw) <= MAX_BYTES and (reason is None) == (channel is not None))
        ancillary = (
            []
            if channel is None
            else [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [channel.fileno()]))]
        )
        connection.settimeout(_remaining(deadline))
        _require(connection.sendmsg([raw], ancillary) == len(raw))
        _remaining(deadline)

    def serve(self, connection: socket.socket, *, deadline: float) -> None:
        """Receive the original frame and hand one FD to its exact current sender."""
        _require(type(deadline) in (int, float) and math.isfinite(deadline))
        deadline = min(deadline, time.monotonic() + MAX_SECONDS)
        previous = control_deadline.get()
        if previous is not None:
            deadline = min(deadline, previous)
        token = control_deadline.set(deadline)
        selected = None
        client = None
        request = None
        transfer_attempted = False
        activated = False
        try:
            request, pid, uid = _receive(connection, deadline)
            peer = self.control.authenticator.authenticate(pid, uid)
            self._current(peer, deadline)
            _require(
                request["descriptor_sha256"] == DESCRIPTOR_SHA256
                and request["caller_generation_sha256"] == metadata.digest(asdict(peer))
                and request["server_generation_sha256"]
                == metadata.digest(self.control.server_generation)
            )
            with self._lock:
                _require(not self._closed)
                busy = self._slot is not None
                if not busy:
                    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                    try:
                        prepare_channel(server)
                        prepare_channel(client)
                        server.set_inheritable(False)
                        client.set_inheritable(False)
                    except BaseException:
                        server.close()
                        client.close()
                        raise
                    selected = _Channel(
                        server, peer, time.monotonic() + CHANNEL_SECONDS, threading.Event()
                    )
                    self._slot = selected
            if busy:
                self._current(peer, deadline)
                transfer_attempted = True
                self._send(connection, request, deadline=deadline, reason="busy")
                self._current(peer, deadline)
                return
            if selected is None or client is None:
                raise RetainedChannelError("provider_denied")
            self._current(peer, deadline)
            transfer_attempted = True
            self._send(connection, request, deadline=deadline, channel=client)
            self._current(peer, deadline)
            with self._lock:
                _require(not self._closed and self._slot is selected and selected is not None)
                selected.thread = threading.Thread(
                    target=self._worker, args=(selected,), name="gpu-retained-readback", daemon=True
                )
                selected.thread.start()
                activated = True
        except (OSError, ValueError, RuntimeError) as error:
            if request is not None and not transfer_attempted:
                try:
                    self._send(connection, request, deadline=deadline, reason="provider_denied")
                except (OSError, ValueError):
                    pass
            raise RetainedChannelError("provider_denied") from error
        finally:
            if client is not None:
                client.close()
            if selected is not None and not activated:
                self._release(selected)
            control_deadline.reset(token)

    def close(self) -> None:
        """Retire this bootstrap and its owned channel without touching services."""
        with self._lock:
            self._closed = True
            selected = self._slot
        if selected is not None:
            self._release(selected)
            if selected.thread is not None and selected.thread is not threading.current_thread():
                selected.thread.join(timeout=MAX_SECONDS + IDLE_SECONDS)
