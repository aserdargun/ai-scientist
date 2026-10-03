"""Canonical finalizer callers share one lifecycle and consume the original budget."""

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import Mock
from uuid import uuid4

import pytest

from lab.scorer import supervisor


def test_concurrent_finalizer_callers_serialize_with_the_same_pair(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    entered = threading.Event()
    release = threading.Event()
    finished = []
    run, digest = uuid4(), "a" * 64

    def worker(selected, **kwargs):
        assert selected == run
        assert kwargs["admitted_generation"] == 3
        assert kwargs["execution_sha256"] == digest
        assert 0 < kwargs["remaining_seconds"] < 20
        finished.append("entered")
        entered.set()
        assert release.wait(timeout=2)
        return supervisor.ScorerProcessResult(run, "canonical-unit", 0, {"state": "finalized"})

    monkeypatch.setattr(supervisor, "_run_scorer_finalize_process_locked", worker)

    def invoke():
        return supervisor.run_scorer_finalize_process(
            run, admitted_generation=3, execution_sha256=digest, remaining_seconds=20
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(invoke)
        assert entered.wait(timeout=2)
        second = pool.submit(invoke)
        assert finished == ["entered"]
        release.set()
        assert first.result(timeout=3).exit_code == second.result(timeout=3).exit_code == 0
    assert finished == ["entered", "entered"]


@pytest.mark.parametrize("spent", [4.2, 10.0])
def test_lifecycle_wait_consumes_original_finalizer_budget(monkeypatch, spent):
    now = [100.0]
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: now[0])

    @contextmanager
    def lock(selected, *, timeout_seconds):
        assert timeout_seconds == 10
        now[0] += spent
        yield

    monkeypatch.setattr(supervisor, "_job_lifecycle_lock", lock)
    worker = Mock(return_value=None)
    monkeypatch.setattr(supervisor, "_run_scorer_finalize_process_locked", worker)
    if spent == 10.0:
        with pytest.raises(TimeoutError, match="finalizer lifecycle deadline"):
            supervisor.run_scorer_finalize_process(
                uuid4(), admitted_generation=1, execution_sha256="a" * 64, remaining_seconds=10
            )
        worker.assert_not_called()
    else:
        supervisor.run_scorer_finalize_process(
            uuid4(), admitted_generation=1, execution_sha256="a" * 64, remaining_seconds=10
        )
        assert worker.call_args.kwargs["remaining_seconds"] == 5
