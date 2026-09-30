"""Test-only pidfd observer for the baseline score/checkpoint interruption proof.

No signal is sent on import. The caller supplies a DB-captured Director identity
and the original operation deadline. This verifies that identity against Linux
and systemd, pauses only that process, and always resumes it on scope exit.
It does not establish that a score committed or a checkpoint is absent: the
caller must query both facts AFTER the pause and label a lost race inconclusive.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from typing import Iterator
from uuid import UUID


SYSTEMCTL = "/usr/bin/systemctl"


@dataclass(frozen=True)
class DirectorIdentity:
    run_id: UUID
    worker_pid: int
    worker_start_ticks: int
    worker_boot_id: str
    worker_unit: str
    worker_invocation_id: str
    worker_cgroup: str

    def validate(self) -> None:
        expected = f"swapp-ai-scientist-director-dispatch-{self.run_id.hex}.service"
        if (
            self.worker_unit != expected
            or isinstance(self.worker_pid, bool)
            or self.worker_pid <= 1
            or self.worker_pid == os.getpid()
            or isinstance(self.worker_start_ticks, bool)
            or self.worker_start_ticks <= 0
            or str(UUID(self.worker_boot_id)) != self.worker_boot_id
            or re.fullmatch(r"[0-9a-f]{32}", self.worker_invocation_id) is None
            or not self.worker_cgroup.startswith("/")
            or ".." in Path(self.worker_cgroup).parts
            or Path(self.worker_cgroup).name != expected
        ):
            raise ValueError("observer requires one exact run-bound Director identity")


def remaining(deadline: float, maximum: float = 2.0) -> float:
    available = deadline - time.monotonic()
    if available <= 0:
        raise TimeoutError("original observation deadline expired")
    return min(available, maximum)


def process_state(pid: int) -> tuple[str, int]:
    fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
    return fields[0], int(fields[19])


def process_cgroup(pid: int) -> str:
    rows = Path(f"/proc/{pid}/cgroup").read_text(encoding="ascii").splitlines()
    unified = [row[3:] for row in rows if row.startswith("0::")]
    if len(unified) != 1:
        raise RuntimeError("process does not have one unified cgroup")
    return unified[0]


def unit_properties(unit: str, deadline: float) -> dict[str, str]:
    if re.fullmatch(r"swapp-ai-scientist-director-dispatch-[0-9a-f]{32}[.]service", unit) is None:
        raise ValueError("observer cannot query an unrelated service")
    result = subprocess.run(
        [SYSTEMCTL, "--user", "show", "--no-pager", "--property=LoadState",
         "--property=ActiveState", "--property=MainPID", "--property=InvocationID",
         "--property=ControlGroup", unit],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
        timeout=remaining(deadline), check=False,
    )
    if result.returncode or len(result.stdout) > 16_384 or len(result.stderr) > 16_384:
        raise RuntimeError("exact Director unit inspection failed")
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def verify_identity(
    identity: DirectorIdentity, deadline: float, *, expect_paused: bool = False
) -> dict[str, str]:
    identity.validate()
    remaining(deadline)
    boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    state, start = process_state(identity.worker_pid)
    properties = unit_properties(identity.worker_unit, deadline)
    if (
        boot != identity.worker_boot_id
        or start != identity.worker_start_ticks
        or state in {"Z", "X", "x"}
        or (expect_paused and state != "T")
        or (state in {"T", "t"} and not expect_paused)
        or process_cgroup(identity.worker_pid) != identity.worker_cgroup
        or properties.get("LoadState") != "loaded"
        or properties.get("ActiveState") != "active"
        or properties.get("MainPID") != str(identity.worker_pid)
        or properties.get("InvocationID") != identity.worker_invocation_id
        or properties.get("ControlGroup") != identity.worker_cgroup
    ):
        raise RuntimeError("captured Director identity no longer matches the live process")
    # Recheck after the slower service query. An already-open pidfd still binds
    # the signal target even if this PID exits and is reused after this read.
    final_state, final_start = process_state(identity.worker_pid)
    if (
        final_start != identity.worker_start_ticks
        or final_state in {"Z", "X", "x"}
        or (expect_paused and final_state != "T")
        or (final_state in {"T", "t"} and not expect_paused)
    ):
        raise RuntimeError("Director process changed during identity verification")
    return properties


@contextmanager
def paused_director(
    identity: DirectorIdentity, *, deadline: float, trace: list[dict[str, object]]
) -> Iterator[None]:
    """Pause exactly one owned main process; keep its pidfd through SIGCONT.

The Director operation budget keeps running. SIGCONT is mandatory cleanup and
is attempted even when that deadline expires or the caller raises. No process
    group, service, cgroup, child worker or Docker container receives a signal.
    """
    if not callable(getattr(os, "pidfd_open", None)) or not callable(
        getattr(signal, "pidfd_send_signal", None)
    ):
        raise RuntimeError("observer interpreter requires both pidfd Python APIs")
    identity.validate()
    descriptor = os.pidfd_open(identity.worker_pid, 0)
    paused = False
    primary_error: BaseException | None = None
    try:
        verify_identity(identity, deadline)
        remaining(deadline)
        # Arm cleanup BEFORE the syscall: a Python signal handler may raise
        # immediately after a successful STOP and before the next statement.
        paused = True
        signal.pidfd_send_signal(descriptor, signal.SIGSTOP)
        trace.append({"event": "sigstop_sent", "monotonic": time.monotonic(),
                      "pid": identity.worker_pid})
        while True:
            state, start = process_state(identity.worker_pid)
            if start != identity.worker_start_ticks:
                raise RuntimeError("Director PID changed after pidfd pause")
            if state == "T":
                break
            if state in {"Z", "X", "x"}:
                raise RuntimeError("Director exited before its pause was observed")
            time.sleep(remaining(deadline, 0.01))
        verify_identity(identity, deadline, expect_paused=True)
        trace.append({"event": "pause_observed", "monotonic": time.monotonic(),
                      "pid": identity.worker_pid, "state": state})
        yield
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            if paused:
                try:
                    signal.pidfd_send_signal(descriptor, signal.SIGCONT)
                except ProcessLookupError:
                    trace.append({"event": "owned_process_exited_before_resume",
                                  "monotonic": time.monotonic(), "pid": identity.worker_pid})
                else:
                    trace.append({"event": "sigcont_sent", "monotonic": time.monotonic(),
                                  "pid": identity.worker_pid})
        except BaseException as cleanup_error:
            trace.append({"event": "resume_failed", "type": type(cleanup_error).__name__})
            if primary_error is None:
                raise
            primary_error.add_note(f"pidfd resume failed: {type(cleanup_error).__name__}")
        finally:
            os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit("Import-only proof helper; no execution or acceptance claim.")
