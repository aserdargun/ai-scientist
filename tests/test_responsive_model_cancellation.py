"""CPU evidence for owner observation and cancellation cleanup; no real GPU."""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from test_native_runtime import _fake_runtime

from lab.director.model_cancellation import DirectorModelObserver, _matches, _worker
from lab.director.ownership import ExecutionOwner
from lab.llm import native_runtime as runtime


def _owner():
    return ExecutionOwner(
        run_id=uuid4(), generation=1, invocation_id="a" * 32, execution_sha256="b" * 64
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "stop_requested"),
        ("stop_requested", True),
        ("mode", "revoked"),
        ("current_generation", 2),
        ("worker_invocation_id", "c" * 32),
        ("execution_sha256", "c" * 64),
        ("contract_sha256", "c" * 64),
    ],
)
def test_owner_observation_rejects_stop_revoke_and_stale_identity(field, value):
    owner = _owner()
    row = {
        "state": "running",
        "stop_requested": False,
        "mode": "active",
        "current_generation": owner.generation,
        "worker_invocation_id": owner.invocation_id,
        "execution_sha256": owner.execution_sha256,
        "contract_sha256": owner.execution_sha256,
    }
    expected = {
        "generation": owner.generation,
        "invocation_id": owner.invocation_id,
        "execution_sha256": owner.execution_sha256,
    }
    assert _matches(row, expected)
    row[field] = value
    assert not _matches(row, expected)
    assert not _matches(None, expected)


def test_pipe_deadline_bounds_stalled_connection_and_kills_only_own_worker(monkeypatch):
    import lab.director.model_cancellation as cancellation

    actual_popen = subprocess.Popen
    children = []

    def stalled(*args, **kwargs):
        assert args[0] == [sys.executable, "-m", "lab.director.model_cancellation"]
        assert "secret-password" not in repr(args)
        child = actual_popen([sys.executable, "-c", "import time; time.sleep(20)"], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(cancellation.subprocess, "Popen", stalled)
    monkeypatch.setattr(cancellation, "OBSERVATION_SECONDS", 0.1)
    engine = create_engine("postgresql+psycopg://u:secret-password@localhost/no_connect")
    observer = DirectorModelObserver(engine, _owner())
    started = time.monotonic()
    with pytest.raises(runtime.ModelTurnCancelled) as error:
        observer()
    assert time.monotonic() - started < 1.0
    assert "secret-password" not in str(error.value)
    assert children[0].poll() is not None
    with pytest.raises(runtime.ModelTurnCancelled):
        observer()
    assert len(children) == 1
    engine.dispose()


def test_worker_connection_error_is_read_only_and_returns_no_credential_details(monkeypatch):
    import lab.director.model_cancellation as cancellation

    observed = []

    def unavailable(url, **kwargs):
        observed.append(kwargs)
        raise RuntimeError("secret-password private SQL failure")

    monkeypatch.setattr(cancellation, "create_engine", unavailable)
    configuration = (
        json.dumps(
            {"url": "postgresql+psycopg://u:secret-password@localhost/db", "owner": {}}
        ).encode()
        + b"\n"
    )
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(configuration)))
    output = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=output))
    assert _worker() == 1
    assert output.getvalue() == b""
    assert observed[0]["connect_args"]["connect_timeout"] == 1
    assert "default_transaction_read_only=on" in observed[0]["connect_args"]["options"]


@pytest.mark.parametrize("phase", ["fair_queue_wait", "model_startup", "model_inference"])
def test_stop_during_queue_startup_or_inference_drains_only_own_turn(tmp_path, monkeypatch, phase):
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    runtime_dir = Path("/tmp") / ("gpu-" + uuid4().hex[:8])
    runtime_dir.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setattr(
        runtime.OwnedVllmRuntime, "_expected_control_group", staticmethod(lambda *_: True)
    )
    try:
        instance, units = _fake_runtime(tmp_path, runtime_dir, slow_completion=False)
        calls = []
        if phase == "fair_queue_wait":
            monkeypatch.setattr(instance.scheduler, "try_acquire", lambda *_: None)

        def observer():
            calls.append(instance._phase)
            if instance._phase == phase:
                if phase == "fair_queue_wait" and len(calls) < 2:
                    return
                raise runtime.ModelTurnCancelled("captured owner stopped")

        instance._cancellation_observer = observer
        with pytest.raises(runtime.ModelTurnCancelled):
            instance.run_turn(
                "lab",
                "cancel-test",
                [{"role": "user", "content": "fixture"}],
                enable_thinking=False,
                max_output_tokens=8,
            )
        assert phase in calls
        assert not units.running
        assert len(units.stop_calls) == (0 if phase == "fair_queue_wait" else 1)
        with sqlite3.connect(tmp_path / "scheduler.sqlite3") as db:
            assert db.execute("select active_owner from gpu_turn_state").fetchone()[0] is None
    finally:
        shutil.rmtree(runtime_dir)


def test_inference_stop_cleanup_failure_keeps_allocation_and_existing_quarantine(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    runtime_dir = Path("/tmp") / ("gpu-" + uuid4().hex[:8])
    runtime_dir.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setattr(
        runtime.OwnedVllmRuntime, "_expected_control_group", staticmethod(lambda *_: True)
    )
    units = None
    try:
        instance, units = _fake_runtime(tmp_path, runtime_dir, slow_completion=False)
        original_stop = units.stop
        original_release = instance.scheduler.release
        captured = []

        def release(lease):
            captured.append(lease)
            return original_release(lease)

        monkeypatch.setattr(instance.scheduler, "release", release)

        def fail_stop(*_args, **_kwargs):
            raise runtime.ModelRuntimeError("cleanup unverifiable")

        monkeypatch.setattr(units, "stop", fail_stop)

        def observer():
            if instance._phase == "model_inference":
                raise runtime.ModelTurnCancelled("stopped")

        instance._cancellation_observer = observer
        with pytest.raises(runtime.ModelTurnCancelled):
            instance.run_turn(
                "lab",
                "quarantine-test",
                [{"role": "user", "content": "fixture"}],
                enable_thinking=False,
                max_output_tokens=8,
            )
        with sqlite3.connect(tmp_path / "scheduler.sqlite3") as db:
            active, phase = db.execute("select active_owner,phase from gpu_turn_state").fetchone()
            assert active == "lab" and phase == "inference"
        # Existing scheduler expiry alone changes allocation to quarantine.
        monkeypatch.setattr(instance.scheduler, "_clock", lambda: captured[0].total_deadline + 1)
        assert instance.scheduler.heartbeat(captured[0]) is None
        assert instance.scheduler.recover_quarantined(captured[0]) is False
        with sqlite3.connect(tmp_path / "scheduler.sqlite3") as db:
            assert db.execute("select active_owner,phase from gpu_turn_state").fetchone() == (
                "lab",
                "quarantined",
            )
        assert units.running
        # Test fixture cleanup only: no scheduler release or quarantine clearing.
        original_stop(units.unit, 10)
    finally:
        if units is not None and units.server is not None:
            units.server.stop()
        shutil.rmtree(runtime_dir)


def test_stop_preserves_started_attempt_when_completed_checkpoint_is_fenced(monkeypatch):
    from test_local_qwen_provider import (
        _context,
        _FakeRuntime,
        _provider,
        _reset_fake_runtime,
    )

    from lab.director import local_llm

    _reset_fake_runtime.__wrapped__(monkeypatch)

    class CancelRuntime(_FakeRuntime):
        def __init__(self, *args, cancellation_observer, **kwargs):
            super().__init__(*args, **kwargs)
            self.observer = cancellation_observer

        def run_turn(self, *args, **kwargs):
            self.observer()
            raise AssertionError("stopped observer must reject the attempt")

    monkeypatch.setattr(local_llm, "OwnedVllmRuntime", CancelRuntime)
    provider = _provider()

    def cancelled():
        raise runtime.ModelTurnCancelled("captured owner stopped")

    provider.cancellation_observer = cancelled
    persisted = []

    def save(index, state, payload):
        if state == "completed":
            raise RuntimeError("completed checkpoint rejected after stop")
        persisted.append((index, state, payload))

    with pytest.raises(RuntimeError, match="completed checkpoint rejected"):
        provider.propose_bounded(_context(), remaining_wall_seconds=180, attempt_save=save)
    assert len(persisted) == 1 and persisted[0][1] == "started"
    assert persisted[0][2]["receipt"]["failure_type"] == "AttemptOutcomeUnknown"
    assert len(CancelRuntime.calls) == 1
