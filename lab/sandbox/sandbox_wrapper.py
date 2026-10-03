"""Trusted in-container launcher and bounded artifact transport."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import signal
import subprocess  # nosec B404
import sys
import time
from pathlib import Path

MAGIC = b"SWAPP-SANDBOX-ARTIFACTS-V1\n"
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MAX_ITEMS = 1024

# The wrapper is invoked with -I; only the immutable image package is added.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lab.sandbox.syscall_observer import (  # noqa: E402
    AUDIT_EXIT,
    AUDIT_MARKER,
    ForbiddenAccess,
    SyscallObserver,
    protect_supervisor,
)


def _become_subreaper() -> None:
    """Adopt candidate descendants so they can be killed before export."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise RuntimeError("candidate subreaper setup failed")


def _child_pids(parent_pid: int) -> set[int]:
    descendants: set[int] = set()
    children: dict[int, list[int]] = {}
    for proc_entry in Path("/proc").iterdir():
        if not proc_entry.name.isdigit():
            continue
        try:
            fields = (proc_entry / "stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
            pid = int(proc_entry.name)
            ppid = int(fields[1])
        except (FileNotFoundError, PermissionError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(pid)
    pending = list(children.get(parent_pid, []))
    while pending:
        pid = pending.pop()
        if pid not in descendants:
            descendants.add(pid)
            pending.extend(children.get(pid, []))
    return descendants


def _kill_descendants() -> None:
    """Kill the candidate tree, including children that called setsid()."""
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        descendants = _child_pids(os.getpid())
        if not descendants:
            return
        for pid in descendants:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        while True:
            try:
                reaped, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if reaped == 0:
                break
        time.sleep(0.01)
    if _child_pids(os.getpid()):
        raise RuntimeError("candidate child process could not be drained")


def _collect_outputs(output: Path, max_bytes: int) -> tuple[dict[str, object], bytes]:
    items: list[dict[str, object]] = []
    content = bytearray()
    with os.scandir(output) as entries:
        for entry in entries:
            if not SAFE_NAME.fullmatch(entry.name) or not entry.is_file(follow_symlinks=False):
                raise RuntimeError("candidate output must use safe flat regular files")
            info = entry.stat(follow_symlinks=False)
            if info.st_nlink != 1:
                raise RuntimeError("candidate output hard links are forbidden")
            if len(items) >= MAX_ITEMS or len(content) + info.st_size > max_bytes:
                raise RuntimeError("candidate output byte/item budget exceeded")
            fd = os.open(entry.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as handle:
                blob = handle.read(max_bytes + 1)
            if len(blob) != info.st_size or len(content) + len(blob) > max_bytes:
                raise RuntimeError("candidate output changed during collection")
            items.append(
                {
                    "name": entry.name,
                    "size_bytes": len(blob),
                    "sha256": hashlib.sha256(blob).hexdigest(),
                }
            )
            content.extend(blob)
    manifest: dict[str, object] = {"schema": "sandbox-artifacts.v1", "items": items}
    return manifest, bytes(content)


def main() -> int:
    if len(sys.argv) != 5:
        print("invalid trusted sandbox arguments", file=sys.stderr)
        return 64
    script = Path(sys.argv[1])
    phase = sys.argv[2]
    timeout = int(sys.argv[3])
    output_limit = int(sys.argv[4])
    if (
        phase not in {"fit", "score", "guard", "mode_stream_fit", "mode_stream_predict"}
        or not 1 <= timeout <= 600
    ):
        print("invalid trusted sandbox phase/limits", file=sys.stderr)
        return 64
    try:
        _become_subreaper()
        protect_supervisor()
        child = subprocess.Popen(  # nosec B603
            [
                sys.executable,
                "-I",
                str(Path(__file__).with_name("syscall_observer.py")),
                str(script),
                phase,
            ],
            stdin=sys.stdin.buffer,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        observer = SyscallObserver(child.pid, time.monotonic() + timeout)
        try:
            exit_code = observer.run()
        except ForbiddenAccess:
            # Only this protected process writes the audit channel. A child's
            # exit code/stdout/stderr never becomes kernel audit evidence.
            sys.stderr.buffer.write(AUDIT_MARKER)
            sys.stderr.buffer.flush()
            return AUDIT_EXIT
        except TimeoutError:
            print("candidate phase timed out", file=sys.stderr)
            return 124
        finally:
            observer.terminate()
            child.returncode = 0  # Reaped by observer, not subprocess.wait().
            _kill_descendants()
        if exit_code == 124:
            print("candidate phase timed out", file=sys.stderr)
            return 124
        if exit_code in {65, 66, 67}:
            errors = {
                65: "candidate score shape is invalid",
                66: "candidate score contains non-finite values",
                67: "candidate score is degenerate",
            }
            print(errors[exit_code], file=sys.stderr)
            return exit_code
        if exit_code != 0:
            print("candidate phase returned non-zero", file=sys.stderr)
            return 1
        output = Path("/output")
        os.chmod(output, 0o700)
        manifest, blobs = _collect_outputs(output, output_limit)
        encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        if len(encoded) > 64 * 1024:
            raise RuntimeError("candidate output manifest is too large")
        sys.stdout.buffer.write(MAGIC)
        sys.stdout.buffer.write(len(encoded).to_bytes(4, "big"))
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.write(blobs)
        sys.stdout.buffer.flush()
        return 0
    except Exception:  # fixed trusted wrapper emits no candidate-controlled text
        print("sandbox trusted supervisor failed", file=sys.stderr)
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
