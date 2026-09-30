"""Trusted Linux x86-64 syscall-entry supervision of the complete candidate tree.

ptrace stops precede syscall execution and outer seccomp decisions. Candidate
signals, errno handling and child reaping cannot erase an observed entry. No
candidate memory, path, argument string or output is an audit authority.
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import runpy
import signal
import stat
import sys
import time
from dataclasses import dataclass, field

AUDIT_EXIT = 68
AUDIT_MARKER = b"SWAPP-SANDBOX-AUDIT-V1 forbidden_access\n"
OBSERVER_EXIT = 70
ARCH_X86_64 = 0xC000003E
X32_BIT = 0x40000000
NETWORK_SYSCALLS = frozenset((*range(41, 56), 288, 299, 307))
# Signals, ptrace, process_vm, seccomp, pidfd, io_uring and namespace manipulation.
TAMPER_SYSCALLS = frozenset(
    (62, 101, 129, 200, 234, 272, 297, 308, 310, 311, 317, 424, 425, 426, 427, 434, 438)
)
# Kernel access-denied results are audited without inspecting mutable path buffers.
FILE_SYSCALLS = frozenset(
    (
        2,
        4,
        6,
        21,
        76,
        77,
        82,
        83,
        84,
        85,
        86,
        87,
        88,
        89,
        90,
        91,
        92,
        93,
        94,
        257,
        258,
        259,
        260,
        262,
        263,
        264,
        265,
        266,
        267,
        268,
        269,
        280,
        316,
        332,
        437,
        439,
    )
)
CLONE_UNTRACED = 0x00800000
CLONE_NAMESPACES = 0x7E020080
PTRACE_TRACEME = 0
PTRACE_CONT = 7
PTRACE_GETREGS = 12
PTRACE_SETREGS = 13
PTRACE_SYSCALL = 24
PTRACE_SETOPTIONS = 0x4200
PTRACE_GETEVENTMSG = 0x4201
PTRACE_GET_SYSCALL_INFO = 0x420E
TRACE_OPTIONS = 1 | 2 | 4 | 8 | 16 | 32 | 64 | 0x100000
WAIT_ALL = 0x40000000
MAX_TRACEES = 128


class ObserverError(RuntimeError):
    """The supervisor cannot establish or retain its trusted boundary."""


class ForbiddenAccess(RuntimeError):
    """A kernel-observed operation violated policy; retain no untrusted detail."""


class _Entry(ctypes.Structure):
    _fields_ = [("number", ctypes.c_ulonglong), ("args", ctypes.c_ulonglong * 6)]


class _Exit(ctypes.Structure):
    _fields_ = [("result", ctypes.c_longlong), ("is_error", ctypes.c_ubyte)]


class _InfoBody(ctypes.Union):
    _fields_ = [("entry", _Entry), ("exit", _Exit), ("padding", ctypes.c_ubyte * 64)]


class _SyscallInfo(ctypes.Structure):
    _fields_ = [
        ("op", ctypes.c_ubyte),
        ("pad", ctypes.c_ubyte * 3),
        ("arch", ctypes.c_uint),
        ("ip", ctypes.c_ulonglong),
        ("sp", ctypes.c_ulonglong),
        ("body", _InfoBody),
    ]


class _Registers(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in (
            "r15",
            "r14",
            "r13",
            "r12",
            "rbp",
            "rbx",
            "r11",
            "r10",
            "r9",
            "r8",
            "rax",
            "rcx",
            "rdx",
            "rsi",
            "rdi",
            "orig_rax",
            "rip",
            "cs",
            "eflags",
            "rsp",
            "ss",
            "fs_base",
            "gs_base",
            "ds",
            "es",
            "fs",
            "gs",
        )
    ]


def _ptrace(request: int, pid: int, address: int = 0, data: object = 0) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.ptrace.restype = ctypes.c_long
    result = int(libc.ptrace(request, pid, ctypes.c_void_p(address), data))
    if result == -1:
        raise ObserverError("ptrace operation failed")
    return result


def protect_supervisor() -> None:
    """Prevent same-UID access to supervisor memory and /proc/PID/fd."""
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ObserverError("unsupported observer architecture")
    if ctypes.sizeof(ctypes.c_void_p) != 8 or sys.byteorder != "little":
        raise ObserverError("unsupported observer ABI")
    # The signal notification wait belongs to this dedicated single-threaded
    # wrapper. Another unblocked thread could otherwise consume SIGCHLD.
    if len(os.listdir("/proc/self/task")) != 1:
        raise ObserverError("multithreaded supervisor is unsupported")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(4, 0, 0, 0, 0) != 0 or libc.prctl(3, 0, 0, 0, 0) != 0:
        raise ObserverError("supervisor protection failed")


def denied_entry(arch: int, number: int, args: tuple[int, ...]) -> bool:
    """Policy uses only immutable scalar syscall metadata; never dereferences."""
    if arch != ARCH_X86_64 or number & X32_BIT or len(args) != 6:
        return True
    if number in NETWORK_SYSCALLS | TAMPER_SYSCALLS:
        return True
    if number == 56 and args[0] & (CLONE_UNTRACED | CLONE_NAMESPACES):
        return True
    # prctl(PR_SET_SECCOMP), PR_SET_PTRACER, PR_SET_DUMPABLE.
    return number == 157 and (args[0] & 0xFFFFFFFF) in (22, 0x59616D61, 4)


def _syscall_info(pid: int) -> _SyscallInfo:
    info = _SyscallInfo()
    size = _ptrace(PTRACE_GET_SYSCALL_INFO, pid, ctypes.sizeof(info), ctypes.byref(info))
    minimum = {1: 80, 2: 33}.get(info.op)
    if minimum is None or size < minimum:
        raise ObserverError("invalid syscall stop metadata")
    return info


def _suppress_clone3(pid: int, *, entering: bool) -> None:
    """Emulate ENOSYS so libc retries traced clone; never inspect clone_args."""
    registers = _Registers()
    _ptrace(PTRACE_GETREGS, pid, data=ctypes.byref(registers))
    if entering:
        registers.orig_rax = 2**64 - 1
    else:
        registers.rax = 2**64 - errno.ENOSYS
    _ptrace(PTRACE_SETREGS, pid, data=ctypes.byref(registers))


@dataclass
class SyscallObserver:
    """Bounded per-thread state, inherited tracing, and sticky violation state."""

    root_pid: int
    deadline: float
    tracees: set[int] = field(default_factory=set)
    entering: dict[int, int] = field(default_factory=dict)
    newborn: set[int] = field(default_factory=set)
    violated: bool = False
    root_status: int | None = None
    deferred: dict[int, int] = field(default_factory=dict)
    group_exits: dict[int, int] = field(default_factory=dict)
    exec_entries: set[int] = field(default_factory=set)

    def _entry_or_exit(self, pid: int) -> None:
        info = _syscall_info(pid)
        if info.op == 1:
            number = int(info.body.entry.number)
            args = tuple(info.body.entry.args)
            if denied_entry(info.arch, number, args):
                self.violated = True
                raise ForbiddenAccess("forbidden_access")
            self.entering[pid] = number
            if number == 231:
                self.group_exits[pid] = args[0] & 0xFF
            if number in (59, 322):
                self.exec_entries.add(pid)
            if number == 435:
                _suppress_clone3(pid, entering=True)
        else:
            if info.arch != ARCH_X86_64:
                self.violated = True
                raise ForbiddenAccess("forbidden_access")
            previous_number = self.entering.pop(pid, None)
            # A freshly traced fork child may start with its parent's syscall exit.
            if previous_number == 435:
                _suppress_clone3(pid, entering=False)
            if previous_number in FILE_SYSCALLS and info.body.exit.result in (
                -errno.EACCES,
                -errno.EPERM,
            ):
                self.violated = True
                raise ForbiddenAccess("forbidden_access")

    def _event(self, pid: int, event: int) -> None:
        value = ctypes.c_ulonglong(0)
        _ptrace(PTRACE_GETEVENTMSG, pid, data=ctypes.byref(value))
        if event in (1, 2, 3):  # FORK, VFORK, CLONE: inherited attachment, before child runs.
            child = int(value.value)
            if child < 1 or child in self.tracees or len(self.tracees) >= MAX_TRACEES:
                raise ObserverError("invalid traced descendant")
            self.tracees.add(child)
            self.newborn.add(child)
        elif event == 4:  # EXEC may replace the thread-group leader's PID.
            old = int(value.value)
            if old != pid:
                if old not in self.tracees:
                    raise ObserverError("untracked exec thread")
                self.tracees.remove(old)
                self.entering.pop(old, None)
                self.newborn.discard(old)
            self.entering.pop(pid, None)
        elif event == 6:
            status = int(value.value)
            if os.WIFSIGNALED(status):
                raise ObserverError("unexpected candidate signal termination")
            if pid == self.root_pid:
                self.root_status = status
        elif event != 5:
            raise ObserverError("unexpected ptrace lifecycle event")

    def _stop(self, pid: int, status: int) -> int:
        """Classify a stop before deciding whether it may resume."""
        if pid not in self.tracees or not os.WIFSTOPPED(status):
            raise ObserverError("untracked candidate stop")
        stop_signal = os.WSTOPSIG(status)
        event = status >> 16
        if event:
            self._event(pid, event)
            return 0
        if stop_signal == signal.SIGTRAP | 0x80:
            self._entry_or_exit(pid)
            return 0
        if pid in self.newborn and stop_signal == signal.SIGSTOP:
            self.newborn.remove(pid)
            return 0
        # Delivering a fatal signal could tear down sibling threads and erase
        # pending stops. Fail the phase before delivery; no artifact is accepted.
        if stop_signal not in (signal.SIGCHLD, signal.SIGSTOP):
            raise ObserverError("unsupported candidate signal delivery")
        return stop_signal

    def run(self) -> int:
        """Block SIGCHLD before checking stops, then restore the caller's mask."""
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGCHLD})
        try:
            return self._run()
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)

    def _run(self) -> int:
        """Handshake before untrusted execution; observe until root exit begins."""
        self.tracees.add(self.root_pid)
        while True:
            pid, status = self._wait()
            if pid == self.root_pid:
                break
            raise ObserverError("invalid observer handshake")
        if not os.WIFSTOPPED(status) or os.WSTOPSIG(status) != signal.SIGSTOP:
            if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                self.tracees.discard(self.root_pid)
            raise ObserverError("observer bootstrap failed")
        _ptrace(PTRACE_SETOPTIONS, pid, data=TRACE_OPTIONS)
        _ptrace(PTRACE_SYSCALL, pid)
        fully_stopped = False
        while self.root_status is None:
            pid, status = self._next_tracked_stop()
            if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                if os.WIFSIGNALED(status):
                    raise ObserverError("unexpected candidate signal termination")
                # Every normal traced exit must have produced TRACEEXIT first.
                if pid == self.root_pid:
                    raise ObserverError("root exited without observed exit event")
                self.tracees.discard(pid)
                self.entering.pop(pid, None)
                self.newborn.discard(pid)
                continue
            forward = self._stop(pid, status)
            if self.group_exits or self.exec_entries:
                stopped = self._quiesce({pid: forward})
                self._resume_after_lifecycle(stopped)
                fully_stopped = self.root_status is not None
            elif self.root_status is None:
                _ptrace(PTRACE_SYSCALL, pid, data=forward)
        if not fully_stopped:
            self._quiesce()
        return os.waitstatus_to_exitcode(self.root_status)

    def _next_tracked_stop(self) -> tuple[int, int]:
        # Linux may report a newborn's initial stop before its parent's fork
        # event. Keep it stopped until GETEVENTMSG establishes its ownership.
        while True:
            for pid in self.deferred.keys() & self.tracees:
                return pid, self.deferred.pop(pid)
            pid, status = self._wait()
            if pid in self.tracees:
                return pid, status
            if (
                not os.WIFSTOPPED(status)
                or os.WSTOPSIG(status) != signal.SIGSTOP
                or pid in self.deferred
                or len(self.deferred) >= MAX_TRACEES
            ):
                raise ObserverError("untracked candidate event")
            self.deferred[pid] = status

    def _wait(self) -> tuple[int, int]:
        while time.monotonic() < self.deadline:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG | WAIT_ALL)
            except ChildProcessError as exc:
                raise ObserverError("candidate tracing state lost") from exc
            if pid:
                return pid, status
            # SIGCHLD is blocked by run(): a stop between waitpid and this
            # wait remains pending, so there is no missed-wakeup race. Kernel
            # notification avoids a fixed sleep for every candidate syscall.
            remaining = self.deadline - time.monotonic()
            if remaining > 0:
                signal.sigtimedwait({signal.SIGCHLD}, remaining)
        raise TimeoutError("candidate phase timed out")

    def _quiesce(self, stopped: dict[int, int] | None = None) -> dict[int, int]:
        """Stop and inspect every survivor before cleanup can erase pending stops."""
        stopped = {self.root_pid: 0} if stopped is None else stopped
        for pid in self.tracees - stopped.keys():
            try:
                os.kill(pid, signal.SIGSTOP)
            except ProcessLookupError:
                pass
        while self.tracees - stopped.keys():
            pid, status = self._next_tracked_stop()
            if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                if os.WIFSIGNALED(status):
                    raise ObserverError("unexpected candidate signal termination")
                self.tracees.discard(pid)
                continue
            forward = self._stop(pid, status)
            stopped[pid] = 0 if forward == signal.SIGSTOP else forward
        if self.deferred:
            raise ObserverError("unclaimed traced descendant")
        # All remaining tasks are now in inspected ptrace stops. No candidate
        # instruction can execute while the wrapper drains and exports output.
        return stopped

    @staticmethod
    def _group_id(pid: int) -> int:
        """Read kernel thread identity only while its ptrace stop pins the PID."""
        with open(f"/proc/{pid}/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("Tgid:"):
                    return int(line.split()[1])
        raise ObserverError("traced thread group identity unavailable")

    def _resume_after_lifecycle(self, stopped: dict[int, int]) -> None:
        """No group teardown may erase a sibling's uninspected syscall stop."""
        groups = {pid: self._group_id(pid) for pid in self.tracees}
        # exec kills sibling threads before TRACEEXEC. Keep this uncommon case
        # closed until its PID-replacement/teardown protocol is live-validated.
        for pid in self.exec_entries:
            if sum(group == groups[pid] for group in groups.values()) != 1:
                raise ObserverError("multithreaded exec is unsupported")
        self.exec_entries.clear()
        for pid, code in self.group_exits.items():
            if groups[pid] == self.root_pid:
                self.root_status = code << 8
                return  # All tasks remain stopped; wrapper safely kills/reaps.
        while self.group_exits:
            pid = next(iter(self.group_exits))
            exiting = {tid for tid, group in groups.items() if group == groups[pid]}
            _ptrace(PTRACE_SYSCALL, pid)
            while self.tracees & exiting:
                tid, status = self._wait()
                if tid not in exiting:
                    raise ObserverError("unexpected event during group teardown")
                if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                    self.tracees.discard(tid)
                    self.entering.pop(tid, None)
                    self.newborn.discard(tid)
                    self.group_exits.pop(tid, None)
                    stopped.pop(tid, None)
                elif os.WIFSTOPPED(status) and status >> 16 == 6:
                    self._event(tid, 6)
                    _ptrace(PTRACE_CONT, tid)
                else:
                    raise ObserverError("unexpected group teardown stop")
        if self.root_status is None:
            for pid, forward in stopped.items():
                if pid in self.tracees:
                    _ptrace(PTRACE_SYSCALL, pid, data=forward)

    def terminate(self) -> None:
        """Drain only this dedicated wrapper's traced descendants after SIGKILL."""
        deadline = time.monotonic() + 2.0
        while self.tracees and time.monotonic() < deadline:
            for pid in tuple(self.tracees):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    _ptrace(PTRACE_CONT, pid, data=signal.SIGKILL)
                except ObserverError:
                    pass  # A running/dying tracee is handled by waitpid below.
            while True:
                try:
                    pid, status = os.waitpid(-1, os.WNOHANG | WAIT_ALL)
                except ChildProcessError:
                    self.tracees.clear()
                    break
                if not pid:
                    break
                if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                    self.tracees.discard(pid)
                else:
                    # A fork event can arrive while rejection cleanup starts.
                    self.tracees.add(pid)
                    if status >> 16 in (1, 2, 3):
                        self._event(pid, status >> 16)
            time.sleep(0.001)
        if self.tracees:
            raise ObserverError("traced candidate cleanup failed")


def child_main(script: str, phase: str) -> int:
    """Run only immutable bootstrap code until the supervisor attaches."""
    for descriptor in (0, 1, 2):
        if stat.S_ISSOCK(os.fstat(descriptor).st_mode):
            raise ObserverError("standard descriptor must not be a socket")
    # Popen(close_fds=True) removes all supervisor/control descriptors at exec.
    # exec normally restores dumpability; enforce it here for the bootstrap.
    # This affects the child only; the trusted parent stays non-dumpable.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(4, 1, 0, 0, 0) != 0:
        raise ObserverError("candidate tracing bootstrap failed")
    _ptrace(PTRACE_TRACEME, 0)
    os.kill(os.getpid(), signal.SIGSTOP)
    sys.argv = [script, phase]
    runpy.run_path(script, run_name="__main__")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(child_main(sys.argv[1], sys.argv[2]))
    except Exception:  # Never export candidate exception text.
        raise SystemExit(1) from None
