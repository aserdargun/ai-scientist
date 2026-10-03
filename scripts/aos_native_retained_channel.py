"""Acquire one broker-created readback FD over its existing control socket.

Stdlib only: AOS setup may import this without Scientist's ML dependencies.
The descriptor transfers a readback channel, never inference or resolution
rights. The caller supplies independently reviewed generations and a current
authority verifier. There is no retry after an uncertain exchange.
"""

from __future__ import annotations

import array
import hashlib
import json
import math
import os
import re
import socket
import stat
import struct
import time
import uuid
from copy import deepcopy
from pathlib import Path

SCHEMA = "aos-scientist-retained-channel.v1"
DESCRIPTOR_SHA256 = "cd61387214a62ddf8fbac9f063ee2154035d9848d2c15412531f850f2ddf2600"
FRAME_LIMIT = 8192
BROKER_UNIT = "swapp-lab-gpu-broker.service"
_GENERATION_KEYS = {
    "pid",
    "uid",
    "start_ticks",
    "boot_id",
    "unit",
    "invocation_id",
    "control_group",
}


class NativeRetainedChannelError(RuntimeError):
    """Unconfirmed channel creation; no automatic retry is authorized."""


def _require(condition, reason):
    if not condition:
        raise NativeRetainedChannelError(reason)


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _hash(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _unique(pairs):
    value = {}
    for key, item in pairs:
        _require(key not in value, "duplicate response member")
        value[key] = item
    return value


def _decode(raw):
    def reject(_value):
        raise NativeRetainedChannelError("nonfinite response")

    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=reject)
    _require(type(value) is dict and _canonical(value) == raw, "noncanonical response")
    return value


def _deny_current():
    raise NativeRetainedChannelError("trusted current authority is not configured")


def _ancestry(path):
    runtime = Path("/run/user") / str(os.getuid())
    _require(
        path.is_absolute()
        and path.resolve(strict=True) == path
        and path.parent == runtime / "swapp-gpu"
        and re.fullmatch(r"[a-z0-9-]+\.sock", path.name),
        "unreviewed control socket path",
    )
    for directory in (runtime, path.parent):
        info = directory.lstat()
        _require(
            stat.S_ISDIR(info.st_mode)
            and info.st_uid == os.getuid()
            and not stat.S_IMODE(info.st_mode) & 0o077,
            "untrusted runtime directory",
        )
    info = path.lstat()
    _require(
        stat.S_ISSOCK(info.st_mode)
        and info.st_uid == os.getuid()
        and not stat.S_IMODE(info.st_mode) & 0o077,
        "untrusted control socket",
    )
    return info.st_dev, info.st_ino


def open_retained_provider_channel(
    socket_path,
    *,
    authenticator,
    expected_peer,
    caller_generation,
    server_generation,
    descriptor_sha256,
    verify_current=_deny_current,
    timeout_seconds=3,
):
    """Return one connected noninheritable SOCK_SEQPACKET FD, or close all FDs.

    The provider still authenticates every packet against this channel's birth
    generation and independently checks original retained-target authority.
    The bootstrap must run inside the exact current AOS MainPID, not a sidecar.
    """
    _require(
        type(timeout_seconds) in (float, int)
        and math.isfinite(timeout_seconds)
        and 0 < timeout_seconds <= 3,
        "invalid original channel deadline",
    )
    _require(
        descriptor_sha256 == DESCRIPTOR_SHA256
        and callable(verify_current)
        and all(
            callable(getattr(authenticator, k, None)) for k in ("authenticate", "still_current")
        ),
        "unconfirmed channel configuration",
    )
    caller, server, peer = (
        deepcopy(caller_generation),
        deepcopy(server_generation),
        deepcopy(expected_peer),
    )
    _require(
        type(caller) is dict
        and type(server) is dict
        and set(server) == _GENERATION_KEYS
        and set(caller) == _GENERATION_KEYS | {"parent_pid", "parent_start_ticks"}
        and caller["pid"] == os.getpid()
        and caller["uid"] == os.getuid()
        and caller["parent_pid"] == caller["pid"]
        and caller["parent_start_ticks"] == caller["start_ticks"]
        and server["unit"] == BROKER_UNIT
        and all(server[k] == getattr(peer, k) for k in _GENERATION_KEYS - {"unit"}),
        "reviewed caller or broker generation differs",
    )
    descriptor = (
        Path(__file__).resolve().parents[1]
        / "lab/llm/contracts/retained_channel_v1/descriptor.json"
    )
    _require(
        not descriptor.is_symlink()
        and hashlib.sha256(descriptor.read_bytes()).hexdigest() == DESCRIPTOR_SHA256,
        "channel descriptor changed",
    )
    path = Path(socket_path)
    deadline = time.monotonic() + timeout_seconds

    def current():
        _require(time.monotonic() < deadline, "original channel deadline expired")
        _require(verify_current() is None, "current verifier must complete or raise")
        _require(
            authenticator.authenticate(peer.pid, peer.uid) == peer
            and authenticator.still_current(peer) is True,
            "broker generation changed",
        )
        _require(time.monotonic() < deadline, "original channel deadline expired")

    current()
    identity = _ancestry(path)
    request = {
        "schema": SCHEMA,
        "version": 1,
        "op": "open",
        "nonce": uuid.uuid4().hex,
        "descriptor_sha256": DESCRIPTOR_SHA256,
        "caller_generation_sha256": _hash(caller),
        "server_generation_sha256": _hash(server),
    }
    descriptors = []
    channel = None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            connection.settimeout(max(0, deadline - time.monotonic()))
            connection.connect(str(path))
            _require(_ancestry(path) == identity, "control socket replaced during connect")
            credentials = struct.unpack(
                "3i",
                connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")),
            )
            _require(credentials[:2] == (peer.pid, peer.uid), "control socket creator differs")
            current()
            connection.settimeout(max(0, deadline - time.monotonic()))
            connection.sendall(_canonical(request) + b"\n")
            raw = bytearray()
            while b"\n" not in raw:
                current()
                connection.settimeout(max(0, deadline - time.monotonic()))
                data, ancillary, flags, _address = connection.recvmsg(
                    FRAME_LIMIT + 1 - len(raw),
                    socket.CMSG_SPACE(4 * 253) + socket.CMSG_SPACE(12),
                    socket.MSG_CMSG_CLOEXEC,
                )
                credentials, forbidden = [], False
                for level, kind, payload in ancillary:
                    if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                        handles = array.array("i")
                        handles.frombytes(payload[: len(payload) - len(payload) % handles.itemsize])
                        descriptors.extend(handles)
                        forbidden = forbidden or len(payload) % handles.itemsize != 0
                    elif (
                        level == socket.SOL_SOCKET
                        and kind == socket.SCM_CREDENTIALS
                        and len(payload) == 12
                    ):
                        credentials.append(struct.unpack("3i", payload))
                    else:
                        forbidden = True
                _require(
                    not forbidden
                    and not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)
                    and len(credentials) == 1
                    and credentials[0][:2] == (peer.pid, peer.uid),
                    "ambiguous response sender or descriptors",
                )
                current()
                _require(
                    data and len(raw) + len(data) <= FRAME_LIMIT, "response framing exceeds bound"
                )
                raw.extend(data)
            line, _, trailing = bytes(raw).partition(b"\n")
            _require(not trailing, "multiple response frames")
            response = _decode(line)
            _require(
                set(response) == set(request) | {"ok", "reason_code"}
                and all(
                    response[k] == v and type(response[k]) is type(v) for k, v in request.items()
                )
                and type(response["ok"]) is bool,
                "channel response correlation differs",
            )
            if not response["ok"]:
                _require(
                    not descriptors and response["reason_code"] in ("provider_denied", "busy"),
                    "malformed channel denial",
                )
                raise NativeRetainedChannelError("channel denied; do not retry uncertain exchanges")
            _require(
                response["reason_code"] is None and len(descriptors) == 1,
                "channel requires exactly one descriptor",
            )
            descriptor_fd = descriptors[0]
            channel = socket.socket(fileno=descriptor_fd)
            descriptors.clear()
            channel.set_inheritable(False)
            _require(
                channel.family == socket.AF_UNIX
                and channel.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) == socket.SOCK_SEQPACKET
                and channel.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 0,
                "transferred descriptor is not a connected provider socket",
            )
            channel.getpeername()
            credentials = struct.unpack(
                "3i",
                channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")),
            )
            _require(credentials[:2] == (peer.pid, peer.uid), "transferred socket creator differs")
            channel.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            current()
            return channel
    except BaseException:
        if channel is not None:
            channel.close()
        raise
    finally:
        for descriptor_fd in descriptors:
            os.close(descriptor_fd)
