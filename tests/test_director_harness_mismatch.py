"""A trusted-source mutation is terminally rejected before sandbox or Scorer work."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from harness.fingerprint import compute_harness_hash
from lab.director import evaluation_recovery, executor, runner
from lab.director.budget import RunBudget
from lab.director.contracts import CandidateProposal


class _MemoryLease:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}

    def heartbeat(self) -> None:
        pass

    def require_run_active(self) -> None:
        pass

    def read_checkpoint(self, *, key: str, **_: Any) -> dict[str, Any] | None:
        return self.records.get(key)

    def append_checkpoint(
        self, *, key: str, phase: str, payload: dict[str, Any], **_: Any
    ) -> None:
        self.records[key] = {"phase": phase, "payload": payload}


def _proposal(number: int, harness_sha256: str) -> runner.RegisteredProposal:
    source = "def build_candidate():\n    return None\n"
    digest = hashlib.sha256(source.encode()).hexdigest()
    return runner.RegisteredProposal(
        experiment_id=f"exp_{number:032x}",
        experiment_number=number,
        proposal=CandidateProposal(
            hypothesis="Fingerprint mutation must fail closed",
            move_type="features",
            candidate_source=source,
            predicted_delta=0.01,
        ),
        candidate_sha256=digest,
        candidate_blob_sha256=digest,
        inputs_sha256="a" * 64,
        messages_blob_sha256="b" * 64,
        calibration_sha256="c" * 64,
        parent_experiment_id="exp_" + "d" * 32,
        parent_tree_sha256="e" * 40,
        harness_sha256=harness_sha256,
        image_sha256=executor.DEFAULT_SANDBOX_IMAGE.removeprefix("sha256:").split("@sha256:")[-1],
        suite_id="fingerprint-mutation-fixture.v1",
        suite_version=1,
        system="S1",
        input_tokens=1,
        output_tokens=1,
    )


def test_mutated_trusted_byte_rejects_each_next_proposal_without_execution(
    monkeypatch: Any, tmp_path: Path
) -> None:
    source_root = Path(__file__).resolve().parents[1]
    live_pin = compute_harness_hash(source_root).sha256
    root = tmp_path / "trusted-copy"
    for relative in (
        "harness",
        "vendor",
        "lab/sandbox",
        "lab/scorer",
        "lab/referee",
        "lab/director",
        "lab/llm",
        "lab/api",
        "lab/training",
        "lab/operating_modes",
        "lab/analytics",
    ):
        shutil.copytree(source_root / relative, root / relative, ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", ".*"
        ))
    for relative in (
        "Dockerfile.sandbox",
        "docker/sandbox/requirements.txt",
        "ops/sandbox-image.lock",
        "pyproject.toml",
        "uv.lock",
        "lab/cli.py",
        "lab/suite_limits.py",
        "lab/reporting.py",
        "lab/replay.py",
    ):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_root / relative, destination)
    monkeypatch.setattr(executor, "PROJECT_ROOT", root)
    original_hash = compute_harness_hash(root).sha256
    version_file = root / "harness/VERSION"
    original_bytes = version_file.read_bytes()
    lease = _MemoryLease()
    terminal_outcomes: list[str] = []
    forbidden_dispatch: list[str] = []
    cache_lookups: list[str] = []
    task = SimpleNamespace(
        task_id="fixture-task", dataset_id="fixture", split_id="dev", session_id="session"
    )

    # This control-flow fixture has no persisted restart/orphan evidence.
    monkeypatch.setattr(
        evaluation_recovery, "recover_abandoned_experiment", lambda *args, **kwargs: False
    )
    monkeypatch.setattr(runner, "plan_run_tasks", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_advance_experiment_if_needed", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_read_measurement", lambda *args, **kwargs: None)
    def read_cached_seed(*args: Any, **kwargs: Any) -> runner.PrimaryEvaluationResult | None:
        proposal = kwargs["proposal"]
        cache_lookups.append(proposal.experiment_id)
        if proposal.experiment_number == 2:
            return runner.PrimaryEvaluationResult(
                proposal.experiment_id,
                "primary_measured",
                ({"task_id": task.task_id},),
                None,
            )
        return None

    monkeypatch.setattr(runner, "_read_completed_seed", read_cached_seed)
    monkeypatch.setattr(runner, "_task_has_terminal_outcome", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        runner,
        "record_planner_terminal_outcome",
        lambda *args, **kwargs: terminal_outcomes.append(kwargs["outcome_code"]),
    )
    monkeypatch.setattr(
        runner,
        "_append_next_checkpoint",
        lambda _lease, _engine, *, key, phase, payload, **kwargs: lease.append_checkpoint(
            key=key, phase=phase, payload=payload
        ),
    )
    monkeypatch.setattr(
        executor,
        "run_guarded_seed_evaluation",
        lambda *args, **kwargs: forbidden_dispatch.append("sandbox"),
    )
    monkeypatch.setattr(
        executor,
        "enqueue_score_job",
        lambda *args, **kwargs: forbidden_dispatch.append("scorer"),
    )

    def mutate_after_first_boundary_check(*args: Any, **kwargs: Any) -> None:
        executor.verify_execution_identity(*args, **kwargs)
        if not version_file.read_bytes().endswith(b"x"):
            version_file.write_bytes(original_bytes + b"x")

    monkeypatch.setattr(runner, "verify_execution_identity", mutate_after_first_boundary_check)
    results = []
    for ordinal in (1, 2):
        proposal = _proposal(ordinal, original_hash)
        budget = RunBudget(wall_limit=1200, token_limit=0)
        result = runner.execute_candidate_seed(
            object(),
            object(),
            lease=lease,
            runner=SimpleNamespace(image=executor.DEFAULT_SANDBOX_IMAGE),
            run_id=uuid4(),
            proposal=proposal,
            tasks=(task,),
            harness_sha256=original_hash,
            image_sha256=proposal.image_sha256,
            budget=budget,
        )
        results.append(result)

    assert [item.status for item in results] == ["rejected", "rejected"]
    assert [item.terminal_code for item in results] == [
        "harness_hash_mismatch",
        "harness_hash_mismatch",
    ]
    assert compute_harness_hash(root).sha256 != original_hash
    assert terminal_outcomes == ["guard_rejected"]
    assert forbidden_dispatch == []
    assert cache_lookups == ["exp_" + f"{1:032x}"]
    for proposal_number in (1, 2):
        if proposal_number == 1:
            event = lease.records[
                f"seed-complete:exp_{proposal_number:032x}:primary:0"
            ]
            assert event["payload"]["terminal_code"] == "harness_hash_mismatch"
        else:
            assert f"seed-complete:exp_{proposal_number:032x}:primary:0" not in lease.records

    assert version_file.read_bytes() == original_bytes + b"x"
    assert compute_harness_hash(source_root).sha256 == live_pin


def test_harness_mismatch_surfaces_as_exact_referee_reject(tmp_path: Path) -> None:
    from test_director_confirmation_review import _fixture

    arguments, _ = _fixture(tmp_path)
    result = runner.PrimaryEvaluationResult(
        experiment_id=arguments["proposal"].experiment_id,
        status="rejected",
        measurements=(),
        terminal_code="harness_hash_mismatch",
    )
    referee = runner.decide_proposal_from_measurements(
        proposal=arguments["proposal"],
        result=result,
        tasks=arguments["tasks"],
        calibration=arguments["calibration"],
        parent_source=arguments["parent_source"],
        best_suite=arguments["best_suite"],
    )

    assert referee.decision.verdict == "REJECT"
    assert referee.decision.reason == "harness_hash_mismatch"
