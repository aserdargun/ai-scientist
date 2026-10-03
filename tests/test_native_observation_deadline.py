"""One deadline must bound sequential read-only native observation commands."""

from types import SimpleNamespace

import pytest

from lab.llm import native_runtime as runtime


def test_gpu_queries_share_remaining_deadline(monkeypatch):
    now = [100.0]
    timeouts = []
    monkeypatch.setattr(runtime.time, "monotonic", lambda: now[0])

    def run(command, **kwargs):
        timeouts.append(kwargs["timeout"])
        now[0] += 0.75
        output = "" if len(timeouts) == 1 else "0,16376\n"
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(runtime.subprocess, "run", run)
    token = runtime.observation_deadline.set(102.0)
    try:
        assert runtime.NvidiaSmiObserver().snapshot().total_memory_mib == 16376
    finally:
        runtime.observation_deadline.reset(token)
    assert timeouts == [2.0, 1.25]


def test_expired_observation_never_starts_second_query(monkeypatch):
    now = [100.0]
    calls = []
    monkeypatch.setattr(runtime.time, "monotonic", lambda: now[0])

    def run(command, **kwargs):
        calls.append(command)
        now[0] = 103.0
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(runtime.subprocess, "run", run)
    token = runtime.observation_deadline.set(102.0)
    try:
        with pytest.raises(runtime.ModelRuntimeError, match="deadline"):
            runtime.NvidiaSmiObserver().snapshot()
    finally:
        runtime.observation_deadline.reset(token)
    assert len(calls) == 1


def test_systemd_query_uses_same_readback_deadline(monkeypatch):
    monkeypatch.setattr(runtime.time, "monotonic", lambda: 100.0)
    timeouts = []

    def run(command, *, timeout):
        timeouts.append(timeout)
        return SimpleNamespace(returncode=0, stdout="LoadState=not-found\n")

    monkeypatch.setattr(runtime.SystemdUnitManager, "_run", run)
    token = runtime.observation_deadline.set(101.0)
    try:
        assert runtime.SystemdUnitManager._show_properties(
            "swapp-aos-gpu-turn-" + "a" * 32 + ".service", ["LoadState"]
        ) == {"LoadState": "not-found"}
    finally:
        runtime.observation_deadline.reset(token)
    assert timeouts == [1.0]
    runtime.SystemdUnitManager._show_properties(
        "swapp-aos-gpu-turn-" + "a" * 32 + ".service", ["LoadState"]
    )
    assert timeouts == [1.0, 5.0]
