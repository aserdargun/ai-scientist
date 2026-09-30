from __future__ import annotations

import subprocess
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest

from lab.scorer import supervisor, worker


def test_supervised_finalizer_transports_captured_pair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_id = uuid4()
    digest = "a" * 64
    captured: dict[str, object] = {}
    artifact_root = tmp_path / "data/runtime/owned-run"
    artifact_root.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(supervisor, "PROJECT_ROOT", tmp_path)

    monkeypatch.setattr(supervisor, "_ensure_aggregate_slice", lambda: None)

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, '{"state":"finalized"}', "")

    monkeypatch.setattr(supervisor.subprocess, "run", fake_run)
    result = supervisor.run_scorer_finalize_process(
        run_id,
        admitted_generation=7,
        execution_sha256=digest,
        remaining_seconds=30,
        artifact_root=artifact_root,
    )
    command = captured["command"]
    assert isinstance(command, list)
    assert command[command.index("--finalize-admitted-generation") + 1] == "7"
    assert command[command.index("--finalize-execution-sha256") + 1] == digest
    assert "--finalize-empty-baseline-stop" not in command
    assert f"--setenv=LAB_ARTIFACT_ROOT={artifact_root}" in command
    assert result.result == {"state": "finalized"}


def test_empty_baseline_stop_uses_explicit_no_pair_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = uuid4()
    captured: dict[str, object] = {}
    monkeypatch.setattr(supervisor, "_ensure_aggregate_slice", lambda: None)

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, '{"state":"finalized"}', "")

    monkeypatch.setattr(supervisor.subprocess, "run", fake_run)
    result = supervisor.run_scorer_empty_baseline_stop_process(run_id, remaining_seconds=30)
    command = captured["command"]
    assert isinstance(command, list)
    assert "--finalize-empty-baseline-stop" in command
    assert "--finalize-admitted-generation" not in command
    assert "--finalize-execution-sha256" not in command
    assert result.result == {"state": "finalized"}


def test_finalizer_refuses_unbound_or_ambiguous_identity() -> None:
    run_id = uuid4()
    with pytest.raises(TypeError):
        supervisor.run_scorer_finalize_process(run_id)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="captured generation/hash pair"):
        supervisor.run_scorer_finalize_process(
            run_id,
            admitted_generation=0,
            execution_sha256="a" * 64,
        )
    with pytest.raises(ValueError, match="captured generation/hash pair"):
        supervisor.run_scorer_finalize_process(
            run_id,
            admitted_generation=1,
            execution_sha256="A" * 64,
        )


def test_worker_finalizer_requires_exactly_one_mode() -> None:
    class NeverConnect:
        def connect(self) -> None:
            raise AssertionError("invalid identity must fail before opening a database")

    run_id = uuid4()
    invocation = worker.ScorerInvocation(
        unit=f"swapp-ai-scientist-finalize-{run_id.hex}.service",
        invocation_id="0" * 32,
        control_group="/user.slice/swapp-ai-scientist.slice/finalize.service",
    )
    with pytest.raises(ValueError, match="either a captured pair or explicit empty-stop mode"):
        worker.process_finalize(
            NeverConnect(),
            run_id=run_id,
            invocation=invocation,
            admitted_generation=None,
            execution_sha256=None,
        )
    with pytest.raises(ValueError, match="either a captured pair or explicit empty-stop mode"):
        worker.process_finalize(
            NeverConnect(),
            run_id=run_id,
            invocation=invocation,
            admitted_generation=1,
            execution_sha256="a" * 64,
            empty_baseline_stop=True,
        )


def test_worker_finalizer_passes_pair_into_report_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    expected = {"run_id": run_id, "admitted_generation": 8, "execution_sha256": "b" * 64}
    calls: list[dict[str, object]] = []

    class Connection:
        def execute(self, statement: object, params: object) -> object:
            class Result:
                @staticmethod
                def scalar_one() -> bool:
                    return True

            return Result()

    class Engine:
        @staticmethod
        @contextmanager
        def connect():
            yield Connection()

    class Scorer:
        def __init__(self, _engine: object, *, harness_sha256: str, artifact_root: Path) -> None:
            assert artifact_root == worker.DEFAULT_ARTIFACT_ROOT
            assert harness_sha256 == "c" * 64

        def finalize_if_ready(self, **kwargs: object) -> tuple[dict[str, str], str]:
            calls.append(kwargs)
            return {"status": "completed"}, "d" * 64

    monkeypatch.setattr(
        "harness.fingerprint.compute_harness_hash",
        lambda _path: type("Hash", (), {"sha256": "c" * 64})(),
    )
    monkeypatch.setattr("lab.scorer.service.IndependentScorer", Scorer)
    invocation = worker.ScorerInvocation(
        unit=f"swapp-ai-scientist-finalize-{run_id.hex}.service",
        invocation_id="0" * 32,
        control_group="/user.slice/swapp-ai-scientist.slice/finalize.service",
    )
    result = worker.process_finalize(
        Engine(),
        run_id=run_id,
        invocation=invocation,
        admitted_generation=int(expected["admitted_generation"]),
        execution_sha256=str(expected["execution_sha256"]),
    )
    assert calls == [{**expected, "empty_baseline_stop": False}]
    assert result["research_status"] == "completed"
