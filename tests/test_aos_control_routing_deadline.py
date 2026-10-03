"""CPU-only routing: original deadline and kernel ancillary data survive."""

import array
import os
import socket
import time

import pytest

from lab.llm.aos_gpu_control import CALL_SECONDS, ControlError, LabAOSControl
from lab.llm.aos_gpu_control_store import control_deadline
from lab.llm.aos_gpu_service import _peek_control_frame


def test_peek_preserves_frame_credentials_and_closes_duplicated_fds():
    receiver, sender = socket.socketpair()
    reader, writer = os.pipe()
    try:
        receiver.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        sender.sendmsg(
            [b'{"schema":"test"}\n'],
            [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [reader]))],
        )
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(3):
            assert _peek_control_frame(receiver, time.monotonic() + 0.5) == b'{"schema":"test"}'
            assert len(os.listdir("/proc/self/fd")) == before
        raw, ancillary, flags, _ = receiver.recvmsg(8192, socket.CMSG_SPACE(12) * 2)
        assert raw == b'{"schema":"test"}\n'
        assert not flags & socket.MSG_CTRUNC
        assert any(kind == socket.SCM_CREDENTIALS for _, kind, _ in ancillary)
        rights = []
        for _, kind, payload in ancillary:
            if kind == socket.SCM_RIGHTS:
                handles = array.array("i")
                handles.frombytes(payload)
                rights.extend(handles)
        try:
            assert len(rights) == 1
        finally:
            for descriptor in rights:
                os.close(descriptor)
    finally:
        receiver.close()
        sender.close()
        os.close(reader)
        os.close(writer)


def test_partial_peek_cannot_restart_original_deadline():
    receiver, sender = socket.socketpair()
    try:
        sender.sendall(b'{"schema":')
        original = time.monotonic() + 0.02
        with pytest.raises(ValueError, match="deadline"):
            _peek_control_frame(receiver, original)
        assert time.monotonic() - original < 0.15
        assert receiver.recv(100) == b'{"schema":'
    finally:
        receiver.close()
        sender.close()


def test_control_uses_earliest_outer_deadline_and_restores_context(monkeypatch):
    control = object.__new__(LabAOSControl)
    original = time.monotonic() + 0.2
    token = control_deadline.set(original)
    observed = []
    monkeypatch.setattr(
        control, "_serve_connection", lambda _: observed.append(control_deadline.get())
    )
    try:
        control.serve_connection(None, deadline=time.monotonic() + CALL_SECONDS - 0.1)
        assert observed == [original]
        assert control_deadline.get() == original
    finally:
        control_deadline.reset(token)


@pytest.mark.parametrize("deadline", [0, True, float("nan"), float("inf")])
def test_invalid_deadline_denies_before_any_handler(monkeypatch, deadline):
    control = object.__new__(LabAOSControl)
    monkeypatch.setattr(control, "_serve_connection", lambda _: pytest.fail("handler dispatched"))
    with pytest.raises(ControlError, match="deadline_exceeded"):
        control.serve_connection(None, deadline=deadline)
