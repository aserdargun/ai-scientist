"""Pure CPU protocol/policy tests: no ptrace, containers, services or GPU launched."""

from __future__ import annotations

import errno
import signal
from types import SimpleNamespace

import pytest

from lab.sandbox import syscall_observer as audit

ZERO_ARGS = (0,) * 6
STOP = (signal.SIGSTOP << 8) | 0x7F
SYSCALL_STOP = ((signal.SIGTRAP | 0x80) << 8) | 0x7F


def event_stop(event):
    return (event << 16) | (signal.SIGTRAP << 8) | 0x7F


def syscall(number=0, args=ZERO_ARGS, *, arch=audit.ARCH_X86_64, result=None):
    return SimpleNamespace(
        op=1 if result is None else 2,
        arch=arch,
        body=SimpleNamespace(
            entry=SimpleNamespace(number=number, args=args), exit=SimpleNamespace(result=result)
        ),
    )


@pytest.mark.parametrize("number", sorted(audit.NETWORK_SYSCALLS | audit.TAMPER_SYSCALLS))
def test_network_and_observer_tampering_always_denied(number):
    assert audit.denied_entry(audit.ARCH_X86_64, number, ZERO_ARGS)


@pytest.mark.parametrize(
    "arch,number", [(0x40000003, 102), (0, 0), (audit.ARCH_X86_64, audit.X32_BIT | 41)]
)
def test_compat_socketcall_and_x32_cannot_bypass_policy(arch, number):
    assert audit.denied_entry(arch, number, ZERO_ARGS)


@pytest.mark.parametrize("flags", [audit.CLONE_UNTRACED, 0x10000000, 0x80])
def test_untraced_and_namespace_clone_denied(flags):
    assert audit.denied_entry(audit.ARCH_X86_64, 56, (flags, 0, 0, 0, 0, 0))


@pytest.mark.parametrize("option", [22, 4, 0x59616D61, 2**32 + 22])
def test_prctl_filter_layering_and_protection_changes_denied(option):
    assert audit.denied_entry(audit.ARCH_X86_64, 157, (option, 0, 0, 0, 0, 0))


@pytest.mark.parametrize("number", [0, 1, 2, 3, 9, 13, 14, 56, 57, 58, 59, 60, 202, 231, 435])
def test_normal_syscalls_and_observed_lifecycle_allowed(number):
    assert not audit.denied_entry(audit.ARCH_X86_64, number, ZERO_ARGS)


def drive(monkeypatch, records):
    """Records are kernel wait statuses + fixed metadata, never candidate text."""
    pending = iter(records)
    state = {}
    calls = []
    observer = audit.SyscallObserver(100, 1000)

    def wait():
        pid, status, metadata = next(pending)
        state["metadata"] = metadata
        return pid, status

    def ptrace(request, pid, address=0, data=0):
        calls.append((request, pid, data))
        if request == audit.PTRACE_GETEVENTMSG:
            data._obj.value = state["metadata"]
        return 0

    monkeypatch.setattr(observer, "_wait", wait)
    monkeypatch.setattr(audit, "_ptrace", ptrace)
    monkeypatch.setattr(audit, "_syscall_info", lambda pid: state["metadata"])
    monkeypatch.setattr(audit.os, "kill", lambda *args: None)
    return observer, calls


def test_clean_candidate_lifecycle(monkeypatch):
    observer, calls = drive(
        monkeypatch,
        [
            (100, STOP, None),
            (100, SYSCALL_STOP, syscall(1)),
            (100, SYSCALL_STOP, syscall(result=8)),
            (100, event_stop(6), 0),
        ],
    )
    assert observer.run() == 0
    assert calls[0] == (audit.PTRACE_SETOPTIONS, 100, audit.TRACE_OPTIONS)
    assert not observer.violated


def test_swallowed_errno_never_resumes_network_syscall(monkeypatch):
    observer, calls = drive(
        monkeypatch,
        [
            (100, STOP, None),
            (100, SYSCALL_STOP, syscall(41)),
        ],
    )
    with pytest.raises(audit.ForbiddenAccess, match="^forbidden_access$"):
        observer.run()
    assert observer.violated
    assert sum(request == audit.PTRACE_SYSCALL for request, _, _ in calls) == 1


@pytest.mark.parametrize("first_stop_before_fork_event", [False, True])
def test_forked_reaped_violation_cannot_escape(monkeypatch, first_stop_before_fork_event):
    child_stop = (101, STOP, None)
    fork_event = (100, event_stop(1), 101)
    lifecycle = (
        [child_stop, fork_event] if first_stop_before_fork_event else [fork_event, child_stop]
    )
    observer, _ = drive(
        monkeypatch,
        [
            (100, STOP, None),
            *lifecycle,
            (101, SYSCALL_STOP, syscall(42)),
        ],
    )
    with pytest.raises(audit.ForbiddenAccess):
        observer.run()
    assert observer.violated
    assert 101 in observer.tracees


def test_root_exit_drains_pending_descendant_violation_before_cleanup(monkeypatch):
    observer, _ = drive(
        monkeypatch,
        [
            (100, STOP, None),
            (100, event_stop(3), 101),
            (101, STOP, None),
            (100, event_stop(6), 0),
            (101, SYSCALL_STOP, syscall(41)),
        ],
    )
    with pytest.raises(audit.ForbiddenAccess):
        observer.run()
    assert observer.root_status == 0
    assert observer.violated


def test_clean_root_exit_quiesces_surviving_child(monkeypatch):
    observer, calls = drive(
        monkeypatch,
        [
            (100, STOP, None),
            (100, event_stop(1), 101),
            (101, STOP, None),
            (100, event_stop(6), 0),
            (101, SYSCALL_STOP, syscall(1)),
        ],
    )
    assert observer.run() == 0
    # Final syscall is inspected but never resumed before wrapper cleanup.
    assert calls[-1][0] == audit.PTRACE_GETEVENTMSG


@pytest.mark.parametrize("number", [2, 89, 257, 267, 437])
@pytest.mark.parametrize("result", [-errno.EPERM, -errno.EACCES])
def test_protected_proc_memory_and_fd_permission_failure_audited(monkeypatch, number, result):
    observer, _ = drive(
        monkeypatch,
        [
            (100, STOP, None),
            (100, SYSCALL_STOP, syscall(number)),
            (100, SYSCALL_STOP, syscall(result=result)),
        ],
    )
    with pytest.raises(audit.ForbiddenAccess):
        observer.run()


def test_missing_file_is_not_a_forbidden_access(monkeypatch):
    observer, _ = drive(
        monkeypatch,
        [
            (100, STOP, None),
            (100, SYSCALL_STOP, syscall(257)),
            (100, SYSCALL_STOP, syscall(result=-errno.ENOENT)),
            (100, event_stop(6), 0),
        ],
    )
    assert observer.run() == 0


def test_clone3_is_suppressed_on_entry_and_errno_emulated_on_exit(monkeypatch):
    observer, _ = drive(
        monkeypatch,
        [
            (100, STOP, None),
            (100, SYSCALL_STOP, syscall(435)),
            (100, SYSCALL_STOP, syscall(result=-errno.ENOSYS)),
            (100, event_stop(6), 0),
        ],
    )
    suppressed = []
    monkeypatch.setattr(
        audit, "_suppress_clone3", lambda pid, *, entering: suppressed.append(entering)
    )
    assert observer.run() == 0
    assert suppressed == [True, False]


def test_failed_traceme_bootstrap_never_accepts_candidate_exit_zero(monkeypatch):
    observer, _ = drive(monkeypatch, [(100, 0, None)])
    with pytest.raises(audit.ObserverError, match="bootstrap"):
        observer.run()


def test_exit_without_lifecycle_observation_fails_closed(monkeypatch):
    observer, _ = drive(monkeypatch, [(100, STOP, None), (100, 0, None)])
    with pytest.raises(audit.ObserverError, match="without observed"):
        observer.run()


def test_descriptor_cleanup_and_tracing_order_before_untrusted_runpy(monkeypatch):
    calls = []
    monkeypatch.setattr(audit.os, "fstat", lambda fd: SimpleNamespace(st_mode=0o010000))
    monkeypatch.setattr(
        audit.ctypes,
        "CDLL",
        lambda *a, **k: SimpleNamespace(prctl=lambda *a: calls.append("child_dumpable") or 0),
    )
    monkeypatch.setattr(audit, "_ptrace", lambda *a: calls.append("traceme"))
    monkeypatch.setattr(audit.os, "kill", lambda *a: calls.append("stop"))
    monkeypatch.setattr(audit.runpy, "run_path", lambda *a, **k: calls.append("candidate"))
    monkeypatch.setattr(audit.sys, "argv", [])
    assert audit.child_main("/candidate/candidate.py", "fit") == 0
    assert calls == ["child_dumpable", "traceme", "stop", "candidate"]


def test_bootstrap_rejects_socket_stdin(monkeypatch):
    monkeypatch.setattr(audit.os, "fstat", lambda fd: SimpleNamespace(st_mode=0o140000))
    with pytest.raises(audit.ObserverError, match="socket"):
        audit.child_main("/candidate/candidate.py", "fit")


def test_timeout_fails_closed(monkeypatch):
    observer = audit.SyscallObserver(100, 0)
    with pytest.raises(TimeoutError):
        observer._wait()


def test_observer_protection_fails_closed_on_unsupported_machine(monkeypatch):
    monkeypatch.setattr(audit.platform, "machine", lambda: "aarch64")
    with pytest.raises(audit.ObserverError, match="architecture"):
        audit.protect_supervisor()


def test_host_accepts_only_exact_trusted_audit_frame():
    from lab.sandbox.docker_runner import LocalDockerRunner, SandboxRunError

    with pytest.raises(SandboxRunError, match="^forbidden_access$") as caught:
        LocalDockerRunner._check_trusted_exit(audit.AUDIT_EXIT, b"", audit.AUDIT_MARKER)
    assert caught.value.exit_code == audit.AUDIT_EXIT


@pytest.mark.parametrize(
    "stdout,stderr",
    [
        (b"spoof", audit.AUDIT_MARKER),
        (b"", b""),
        (b"", audit.AUDIT_MARKER + b"extra"),
        (b"", b"forbidden_access"),
    ],
)
def test_host_rejects_malformed_or_overflowed_audit_frame_as_infrastructure(stdout, stderr):
    from lab.sandbox.docker_runner import LocalDockerRunner, SandboxRunError

    with pytest.raises(SandboxRunError, match="protocol") as caught:
        LocalDockerRunner._check_trusted_exit(audit.AUDIT_EXIT, stdout, stderr)
    assert caught.value.exit_code is None


def test_observer_failure_is_not_a_candidate_reject():
    from lab.sandbox.docker_runner import LocalDockerRunner, SandboxRunError

    with pytest.raises(SandboxRunError, match="supervisor") as caught:
        LocalDockerRunner._check_trusted_exit(audit.OBSERVER_EXIT, b"", b"fixed failure")
    assert caught.value.exit_code is None


def test_verified_violation_maps_to_forbidden_access_guard():
    from harness.contracts import FitContext
    from lab.sandbox.docker_runner import SandboxRunError
    from lab.sandbox.evaluation import CandidateGuardReject, _run_candidate_phase

    def run_phase(**kwargs):
        raise SandboxRunError("forbidden_access", exit_code=audit.AUDIT_EXIT)

    with pytest.raises(CandidateGuardReject, match="forbidden_access"):
        _run_candidate_phase(
            SimpleNamespace(run_phase=run_phase),
            phase="fit",
            candidate_source=b"pass",
            arrow_input=b"",
            fit_artifact=None,
            context=FitContext(
                seed=0, signals=("a",), regime_signals=(), sampling_s=1, time_budget_s=1
            ),
            timeout_seconds=1,
        )


@pytest.mark.parametrize(
    "outcome,expected",
    [
        (68, 1),
        (70, 1),
        (65, 65),
        (124, 124),
        (audit.ForbiddenAccess(), 68),
        (audit.ObserverError(), 70),
    ],
)
def test_wrapper_reserves_audit_and_supervisor_codes(monkeypatch, outcome, expected):
    import io

    from lab.sandbox import sandbox_wrapper as wrapper

    def run():
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    observer = SimpleNamespace(run=run, terminate=lambda: None)
    monkeypatch.setattr(wrapper, "SyscallObserver", lambda *args: observer)
    monkeypatch.setattr(wrapper, "protect_supervisor", lambda: None)
    monkeypatch.setattr(wrapper, "_become_subreaper", lambda: None)
    monkeypatch.setattr(wrapper, "_kill_descendants", lambda: None)
    popen_args = []

    def popen(*args, **kwargs):
        popen_args.append(kwargs)
        return SimpleNamespace(pid=100, returncode=None)

    monkeypatch.setattr(wrapper.subprocess, "Popen", popen)
    monkeypatch.setattr(wrapper.sys, "argv", ["wrapper", "script", "fit", "2", "1024"])
    stderr = io.TextIOWrapper(io.BytesIO())
    stdin = io.TextIOWrapper(io.BytesIO())
    monkeypatch.setattr(wrapper.sys, "stderr", stderr)
    monkeypatch.setattr(wrapper.sys, "stdin", stdin)
    assert wrapper.main() == expected
    stderr.flush()
    error_bytes = stderr.buffer.getvalue()
    assert (error_bytes == audit.AUDIT_MARKER) == isinstance(outcome, audit.ForbiddenAccess)
    assert popen_args[0]["close_fds"] is True
    assert popen_args[0]["stdout"] == wrapper.subprocess.DEVNULL
    assert popen_args[0]["stderr"] == wrapper.subprocess.DEVNULL


def test_unexpected_bootstrap_stop_retains_child_for_cleanup(monkeypatch):
    observer, _ = drive(monkeypatch, [(100, (signal.SIGTRAP << 8) | 0x7F, None)])
    with pytest.raises(audit.ObserverError, match="bootstrap"):
        observer.run()
    assert observer.tracees == {100}


def test_exit_group_cannot_erase_queued_sibling_network_entry(monkeypatch):
    observer, calls = drive(
        monkeypatch,
        [
            (100, STOP, None),
            (100, event_stop(3), 101),
            (101, STOP, None),
            (100, SYSCALL_STOP, syscall(231)),
            (101, SYSCALL_STOP, syscall(41)),
        ],
    )
    with pytest.raises(audit.ForbiddenAccess):
        observer.run()
    # Exit_group was not resumed before inspecting the sibling entry.
    assert calls[-1][1] == 101
    assert observer.violated


def test_root_thread_exit_group_quiesces_blas_style_threads_and_returns_code(monkeypatch):
    observer, _ = drive(
        monkeypatch,
        [
            (100, STOP, None),
            (100, event_stop(3), 101),
            (101, STOP, None),
            (101, SYSCALL_STOP, syscall(231, (7, 0, 0, 0, 0, 0))),
            (100, SYSCALL_STOP, syscall(202)),
        ],
    )
    monkeypatch.setattr(observer, "_group_id", lambda pid: 100)
    assert observer.run() == 7
    assert observer.tracees == {100, 101}


def test_clean_descendant_exit_group_drains_before_resuming_parent(monkeypatch):
    observer, _ = drive(
        monkeypatch,
        [
            (100, STOP, None),
            (100, event_stop(1), 101),
            (101, STOP, None),
            (101, SYSCALL_STOP, syscall(231)),
            (100, SYSCALL_STOP, syscall(61)),
            (101, event_stop(6), 0),
            (101, 0, None),
            (100, SYSCALL_STOP, syscall(result=101)),
            (100, SYSCALL_STOP, syscall(231)),
        ],
    )
    monkeypatch.setattr(observer, "_group_id", lambda pid: pid)
    assert observer.run() == 0
    assert observer.tracees == {100}


def test_multithreaded_exec_fails_closed_after_stopping_siblings(monkeypatch):
    observer, _ = drive(
        monkeypatch,
        [
            (100, STOP, None),
            (100, event_stop(3), 101),
            (101, STOP, None),
            (100, SYSCALL_STOP, syscall(59)),
            (101, SYSCALL_STOP, syscall(202)),
        ],
    )
    monkeypatch.setattr(observer, "_group_id", lambda pid: 100)
    with pytest.raises(audit.ObserverError, match="multithreaded exec"):
        observer.run()


def test_multithreaded_exec_cannot_erase_pending_sibling_violation(monkeypatch):
    observer, _ = drive(
        monkeypatch,
        [
            (100, STOP, None),
            (100, event_stop(3), 101),
            (101, STOP, None),
            (100, SYSCALL_STOP, syscall(59)),
            (101, SYSCALL_STOP, syscall(41)),
        ],
    )
    with pytest.raises(audit.ForbiddenAccess):
        observer.run()


def test_single_thread_exec_preserves_tracing(monkeypatch):
    observer, _ = drive(
        monkeypatch,
        [
            (100, STOP, None),
            (100, SYSCALL_STOP, syscall(59)),
            (100, event_stop(4), 100),
            (100, SYSCALL_STOP, syscall(result=0)),
            (100, SYSCALL_STOP, syscall(231)),
        ],
    )
    monkeypatch.setattr(observer, "_group_id", lambda pid: pid)
    assert observer.run() == 0


@pytest.mark.parametrize("stop_signal", [signal.SIGSEGV, signal.SIGBUS, signal.SIGILL])
def test_descendant_fatal_signal_cannot_teardown_then_be_silently_reaped(monkeypatch, stop_signal):
    observer, _ = drive(
        monkeypatch,
        [
            (100, STOP, None),
            (100, event_stop(1), 101),
            (101, STOP, None),
            (101, (stop_signal << 8) | 0x7F, None),
        ],
    )
    with pytest.raises(audit.ObserverError, match="signal delivery"):
        observer.run()


def test_descendant_forced_signal_exit_fails_entire_phase(monkeypatch):
    observer, _ = drive(
        monkeypatch,
        [
            (100, STOP, None),
            (100, event_stop(1), 101),
            (101, STOP, None),
            (101, event_stop(6), signal.SIGSYS),
        ],
    )
    with pytest.raises(audit.ObserverError, match="signal termination"):
        observer.run()


@pytest.mark.parametrize("blocked_stage", ["dumpability", "traceme"])
def test_blocked_tracing_bootstrap_never_runs_candidate(monkeypatch, blocked_stage):
    """Outer policy failure cannot silently execute an unobserved candidate."""
    candidate_calls = []
    monkeypatch.setattr(audit.os, "fstat", lambda fd: SimpleNamespace(st_mode=0o010000))
    monkeypatch.setattr(
        audit.ctypes,
        "CDLL",
        lambda *args, **kwargs: SimpleNamespace(
            prctl=lambda *args: -1 if blocked_stage == "dumpability" else 0
        ),
    )

    def denied_ptrace(*args):
        raise audit.ObserverError("ptrace operation failed")

    monkeypatch.setattr(audit, "_ptrace", denied_ptrace)
    monkeypatch.setattr(audit.runpy, "run_path", lambda *args, **kwargs: candidate_calls.append(1))
    with pytest.raises(audit.ObserverError):
        audit.child_main("/candidate/candidate.py", "fit")
    assert candidate_calls == []


def test_observer_container_keeps_default_security_profile(tmp_path, monkeypatch):
    """Capture create arguments without starting Docker or relaxing its policy."""
    from lab.sandbox.docker_runner import DEFAULT_SANDBOX_IMAGE, LocalDockerRunner

    runner = LocalDockerRunner(image=DEFAULT_SANDBOX_IMAGE, work_root=tmp_path)
    commands = []

    class CapturedCreate(Exception):
        pass

    def capture(command):
        commands.append(command)
        raise CapturedCreate

    monkeypatch.setattr(runner, "_create_container", capture)
    monkeypatch.setattr(runner, "_write_admission_intent", lambda *args: None)
    monkeypatch.setattr(runner, "_reconcile_owned_containers", lambda: None)
    with pytest.raises(CapturedCreate):
        runner._invoke(
            "fit", b"pass", b"", None, contract_context=None,
            trusted_baseline_name=None, timeout_seconds=1,
        )
    assert len(commands) == 1
    command = commands[0]
    assert [arg for arg in command if arg.startswith("--security-opt")] == [
        "--security-opt=no-new-privileges"
    ]
    assert "--cap-drop=ALL" in command
    assert "--user=10001:10001" in command
    assert "--network=none" in command
    assert "--read-only" in command
    assert not any(
        "unconfined" in arg or arg.startswith(("--privileged", "--cap-add")) for arg in command
    )


def test_empty_wait_uses_kernel_notification_without_per_syscall_sleep(monkeypatch):
    waits = iter([(0, 0), (100, STOP)])
    monkeypatch.setattr(audit.os, "waitpid", lambda *args: next(waits))
    monkeypatch.setattr(audit.time, "monotonic", lambda: 1.0)
    monkeypatch.setattr(audit.time, "sleep", lambda seconds: pytest.fail("polling sleep"))
    notifications = []
    monkeypatch.setattr(
        audit.signal, "sigtimedwait",
        lambda signals, timeout: notifications.append((signals, timeout)),
    )
    assert audit.SyscallObserver(100, 5.0)._wait() == (100, STOP)
    assert notifications == [({signal.SIGCHLD}, 4.0)]


def test_notification_mask_is_restored_after_bootstrap_failure(monkeypatch):
    masks = []

    def change_mask(operation, signals):
        masks.append((operation, signals))
        return {signal.SIGUSR1}

    monkeypatch.setattr(audit.signal, "pthread_sigmask", change_mask)
    observer = audit.SyscallObserver(100, 5.0)

    def fail():
        raise audit.ObserverError("bootstrap")

    monkeypatch.setattr(observer, "_run", fail)
    with pytest.raises(audit.ObserverError, match="bootstrap"):
        observer.run()
    assert masks == [
        (signal.SIG_BLOCK, {signal.SIGCHLD}),
        (signal.SIG_SETMASK, {signal.SIGUSR1}),
    ]


def test_supervisor_rejects_other_threads_before_signal_wait_setup(monkeypatch):
    monkeypatch.setattr(audit.platform, "system", lambda: "Linux")
    monkeypatch.setattr(audit.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(audit.os, "listdir", lambda path: ["100", "101"])
    with pytest.raises(audit.ObserverError, match="multithreaded supervisor"):
        audit.protect_supervisor()


def test_spurious_or_coalesced_notifications_do_not_renew_deadline(monkeypatch):
    clock = iter([1.0, 1.0, 2.0, 2.0, 5.0])
    monkeypatch.setattr(audit.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(audit.os, "waitpid", lambda *args: (0, 0))
    notifications = []

    def notification(signals, timeout):
        notifications.append((signals, timeout))
        return SimpleNamespace(si_signo=signal.SIGCHLD) if len(notifications) == 1 else None

    monkeypatch.setattr(audit.signal, "sigtimedwait", notification)
    with pytest.raises(TimeoutError, match="timed out"):
        audit.SyscallObserver(100, 5.0)._wait()
    assert notifications == [({signal.SIGCHLD}, 4.0), ({signal.SIGCHLD}, 3.0)]


def test_wrapper_creates_child_before_observer_changes_signal_mask(monkeypatch):
    import io

    from lab.sandbox import sandbox_wrapper as wrapper

    events = []

    def mask(operation, signals):
        events.append(("mask", operation, signals))
        return {signal.SIGUSR1}

    class Observer(audit.SyscallObserver):
        def _run(self):
            events.append(("observed",))
            return 65

        def terminate(self):
            pass

    def child(*args, **kwargs):
        events.append(("child_created",))
        return SimpleNamespace(pid=100, returncode=None)

    monkeypatch.setattr(audit.signal, "pthread_sigmask", mask)
    monkeypatch.setattr(wrapper, "SyscallObserver", Observer)
    monkeypatch.setattr(wrapper, "protect_supervisor", lambda: None)
    monkeypatch.setattr(wrapper, "_become_subreaper", lambda: None)
    monkeypatch.setattr(wrapper, "_kill_descendants", lambda: None)
    monkeypatch.setattr(wrapper.subprocess, "Popen", child)
    monkeypatch.setattr(wrapper.sys, "argv", ["wrapper", "script", "fit", "2", "1024"])
    monkeypatch.setattr(wrapper.sys, "stderr", io.TextIOWrapper(io.BytesIO()))
    monkeypatch.setattr(wrapper.sys, "stdin", io.TextIOWrapper(io.BytesIO()))
    assert wrapper.main() == 65
    assert events == [
        ("child_created",),
        ("mask", signal.SIG_BLOCK, {signal.SIGCHLD}),
        ("observed",),
        ("mask", signal.SIG_SETMASK, {signal.SIGUSR1}),
    ]
