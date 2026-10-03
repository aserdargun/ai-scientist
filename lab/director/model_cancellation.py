"""Bounded read-only owner observation through a private fixed-worker pipe."""

from __future__ import annotations

import atexit
import json
import os
import selectors
import subprocess  # nosec B404
import sys
import time
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from lab.director.ownership import ExecutionOwner
from lab.llm.native_runtime import ModelTurnCancelled

OBSERVATION_SECONDS = 2.0
MAX_MESSAGE_BYTES = 8192
_QUERY = (
    "SELECT r.state,r.stop_requested,c.mode,c.current_generation,"
    "g.worker_invocation_id,g.execution_sha256,e.execution_sha256 AS contract_sha256 "
    "FROM lab.runs r JOIN lab.director_execution_control c ON c.run_id=r.run_id "
    "JOIN lab.director_owner_generations g ON g.run_id=c.run_id "
    "AND g.generation=c.current_generation "
    "JOIN lab.director_execution_contracts e ON e.run_id=r.run_id WHERE r.run_id=:run_id"
)


def _matches(row: Any, owner: dict[str, Any]) -> bool:
    return bool(
        row is not None
        and row["state"] == "running"
        and row["stop_requested"] is False
        and row["mode"] == "active"
        and row["current_generation"] == owner["generation"]
        and row["worker_invocation_id"] == owner["invocation_id"]
        and row["execution_sha256"] == owner["execution_sha256"]
        and row["contract_sha256"] == owner["execution_sha256"]
    )


class DirectorModelObserver:
    """The total pipe RPC deadline includes startup, pool/connect and wire waits.

    This observer can terminate only its own fixed SQL worker. It has no scheduler,
    GPU, release or mutation authority. Every call obtains a new DB snapshot.
    """

    def __init__(self, engine: Any, owner: ExecutionOwner) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("Director model observation requires PostgreSQL")
        self._configuration = {
            "url": engine.url.render_as_string(hide_password=False),
            "owner": {
                "run_id": str(owner.run_id),
                "generation": owner.generation,
                "invocation_id": owner.invocation_id,
                "execution_sha256": owner.execution_sha256,
            },
        }
        self._process: subprocess.Popen[bytes] | None = None
        self._failed = False
        atexit.register(self.close)

    def close(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        for pipe in (process.stdin, process.stdout):
            if pipe is not None:
                pipe.close()
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            # Its process remains in this Director's cgroup; never authorize GPU release here.
            pass

    def _exchange(self, payload: bytes, deadline: float) -> bytes:
        process = self._process
        if process is None or process.stdin is None or process.stdout is None:
            raise RuntimeError("observer worker unavailable")
        response = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdin, selectors.EVENT_WRITE)
            while payload:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError("observer deadline")
                count = os.write(process.stdin.fileno(), payload)
                payload = payload[count:]
            selector.unregister(process.stdin)
            selector.register(process.stdout, selectors.EVENT_READ)
            while b"\n" not in response:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError("observer deadline")
                chunk = os.read(process.stdout.fileno(), 128)
                if not chunk:
                    raise RuntimeError("observer worker closed")
                response.extend(chunk)
                if len(response) > 128:
                    raise RuntimeError("observer response exceeded bound")
        return bytes(response)

    def __call__(self) -> None:
        if self._failed:
            raise ModelTurnCancelled("captured Director owner observation unavailable")
        deadline = time.monotonic() + OBSERVATION_SECONDS
        try:
            if self._process is None:
                configuration = (
                    json.dumps(self._configuration, separators=(",", ":")).encode() + b"\n"
                )
                if len(configuration) > MAX_MESSAGE_BYTES:
                    raise ValueError("observer configuration exceeded bound")
                self._process = subprocess.Popen(  # nosec B603 -- fixed trusted worker, private pipe
                    [sys.executable, "-m", "lab.director.model_cancellation"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    env={**os.environ, "CUDA_VISIBLE_DEVICES": ""},
                )
                if self._process.stdin is None or self._process.stdout is None:
                    raise RuntimeError("observer worker pipes unavailable")
                os.set_blocking(self._process.stdin.fileno(), False)
                os.set_blocking(self._process.stdout.fileno(), False)
                payload = configuration + b"check\n"
            else:
                payload = b"check\n"
            if self._exchange(payload, deadline) != b"active\n":
                raise ModelTurnCancelled("captured Director owner stopped or fenced")
        except BaseException as exc:
            self._failed = True
            self.close()
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            # Credential/wire exception details never cross this trusted boundary.
            raise ModelTurnCancelled(
                "captured Director owner stopped, fenced or unavailable"
            ) from None


def _worker() -> int:
    # No caller input is executed; no SQL mutation or allocation API is available.
    try:
        raw = sys.stdin.buffer.readline(MAX_MESSAGE_BYTES + 1)
        if len(raw) > MAX_MESSAGE_BYTES or not raw.endswith(b"\n"):
            return 1
        configuration = json.loads(raw)
        engine = create_engine(
            configuration["url"],
            poolclass=NullPool,
            connect_args={
                "connect_timeout": 1,
                "options": (
                    "-c default_transaction_read_only=on "
                    "-c statement_timeout=500 -c lock_timeout=100"
                ),
            },
        )
        while sys.stdin.buffer.readline(16) == b"check\n":
            with engine.connect() as connection:
                row = (
                    connection.execute(text(_QUERY), {"run_id": configuration["owner"]["run_id"]})
                    .mappings()
                    .one_or_none()
                )
            active = _matches(row, configuration["owner"])
            sys.stdout.buffer.write(b"active\n" if active else b"cancelled\n")
            sys.stdout.buffer.flush()
            if not active:
                break
        engine.dispose()
        return 0
    except BaseException:
        return 1


if __name__ == "__main__":
    raise SystemExit(_worker())
