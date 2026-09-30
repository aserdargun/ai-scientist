"""One-at-a-time, fixed allowlist CPU smoke check in a resource-limited scope."""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import threading
from datetime import UTC, datetime
from typing import Any

from fastapi import BackgroundTasks, HTTPException

from console.upstream import ROOT

MAX_OUTPUT_BYTES = 32 * 1024
TIMEOUT_SECONDS = 120
UNIT_NAME = "swapp-ai-scientist-console-check.service"
TESTS = (
    "tests/test_director_strategy.py",
    "tests/test_referee.py",
    "tests/test_alarm.py",
    "tests/test_director_contracts.py",
    "tests/test_api_and_scorer.py",
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _desktop_bus_environment() -> dict[str, str]:
    uid = os.getuid()
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if runtime != f"/run/user/{uid}":
        raise OSError("XDG_RUNTIME_DIR is unavailable for the current user")
    runtime_info = os.stat(runtime, follow_symlinks=False)
    if runtime_info.st_uid != uid or not os.path.isdir(runtime) or os.path.islink(runtime):
        raise OSError("XDG_RUNTIME_DIR is not owned by the current user")
    bus = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    expected_bus = f"unix:path={runtime}/bus"
    if bus != expected_bus:
        raise OSError("user systemd D-Bus address is unavailable")
    return {"XDG_RUNTIME_DIR": runtime, "DBUS_SESSION_BUS_ADDRESS": bus}


def command() -> list[str]:
    binary = shutil.which("systemd-run")
    if binary is None:
        raise OSError("systemd-run is unavailable")
    return [
        binary,
        "--user",
        "--wait",
        "--pipe",
        "--collect",
        "--quiet",
        f"--unit={UNIT_NAME}",
        "--working-directory",
        str(ROOT),
        "--property=MemoryMax=1G",
        "--property=MemorySwapMax=0",
        "--property=CPUQuota=50%",
        "--property=TasksMax=64",
        "--property=RuntimeMaxSec=120",
        "--setenv=OMP_NUM_THREADS=1",
        "--setenv=OPENBLAS_NUM_THREADS=1",
        "--setenv=MKL_NUM_THREADS=1",
        "--setenv=PYTHONPATH=" + str(ROOT),
        str(ROOT / ".venv/bin/python"),
        "-m",
        "pytest",
        "-q",
        *TESTS,
    ]


def _trim_output(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    def decode(value: str | bytes | None) -> bytes:
        if value is None:
            return b""
        return value if isinstance(value, bytes) else value.encode("utf-8", errors="replace")

    data = decode(stdout) + decode(stderr)
    trimmed = data[-MAX_OUTPUT_BYTES:]
    prefix = b"[earlier output truncated]\n" if len(data) > len(trimmed) else b""
    return (prefix + trimmed).decode("utf-8", errors="replace")


class CheckManager:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.items: dict[str, dict[str, Any]] = {}
        self.active_id: str | None = None

    def submit(self, background: BackgroundTasks) -> dict[str, str]:
        with self.lock:
            if self.active_id is not None:
                raise HTTPException(status_code=409, detail="a CPU check is already active")
            check_id = secrets.token_urlsafe(16)
            self.active_id = check_id
            self.items[check_id] = {
                "id": check_id,
                "state": "queued",
                "started_at": None,
                "finished_at": None,
                "exit_code": None,
                "summary": "Queued fixed CPU-only verification.",
                "output": "",
            }
        background.add_task(self._run, check_id)
        return {"id": check_id, "state": "queued"}

    def list(self) -> dict[str, list[dict[str, Any]]]:
        with self.lock:
            return {"items": [dict(row) for row in self.items.values()]}

    def get(self, check_id: str) -> dict[str, Any]:
        with self.lock:
            row = self.items.get(check_id)
            if row is None:
                raise HTTPException(status_code=404, detail="check not found")
            return dict(row)

    def _run(self, check_id: str) -> None:
        with self.lock:
            self.items[check_id].update(
                state="running", started_at=_now(), summary="Running fixed CPU-only verification."
            )
        try:
            desktop_env = _desktop_bus_environment()
            env = {
                **desktop_env,
                "PATH": f"{ROOT / '.venv/bin'}:/usr/bin:/bin",
                "LC_ALL": "C",
            }
            result = subprocess.run(
                command(),
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS + 10,
                env=env,
            )
            output = _trim_output(result.stdout, result.stderr)
            exit_code = result.returncode
            passed = exit_code == 0
            summary = (
                "CPU-only check passed."
                if passed
                else f"CPU-only check failed with exit code {exit_code}."
            )
        except subprocess.TimeoutExpired as exc:
            output = _trim_output(exc.stdout, exc.stderr)
            exit_code, passed = 124, False
            summary = "CPU-only check exceeded its 120 second limit."
        except OSError as exc:
            output, exit_code, passed = "", None, False
            summary = f"CPU-only check unavailable: {str(exc)}"
        with self.lock:
            self.items[check_id].update(
                state="passed" if passed else "failed",
                finished_at=_now(),
                exit_code=exit_code,
                summary=summary,
                output=output,
            )
            self.active_id = None
