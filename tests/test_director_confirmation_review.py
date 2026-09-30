"""Promotion requires measured confirmation and trustworthy future noise inputs."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from lab.director.baselines import (
    BASELINE_NAMES,
    baseline_candidate_sha256,
    build_task_calibration,
    calibration_sha256,
    freeze_calibration_document,
)
from lab.director.contracts import CandidateProposal
from lab.director.runner import (
    PrimaryEvaluationResult,
    RegisteredProposal,
    _candidate_git_tree,
    commit_primary_terminal_record,
    decide_proposal_from_measurements,
)


def _fixture(tmp_path: Path) -> tuple[dict[str, Any], PrimaryEvaluationResult]:
    """Synthetic measured vectors only; no Docker, Scorer or database evidence."""
    task = build_task_calibration(
        task_id="evt-fixture",
        dataset_id="fixture",
        split_id="dev",
        session_id="session",
        profile_sha256="a" * 64,
        family="EVT",
        task_weight=1.0,
        baseline_seed_scores={name: {0: 0.2, 1: 0.2, 2: 0.2} for name in BASELINE_NAMES},
        scorer_artifact_sha256={
            name: dict.fromkeys((0, 1, 2), "b" * 64) for name in BASELINE_NAMES
        },
        baseline_candidate_sha256={
            name: baseline_candidate_sha256(name) for name in BASELINE_NAMES
        },
    )
    run_id = uuid4()
    calibration = freeze_calibration_document(
        run_id=run_id,
        suite_id="confirmation-fixture",
        suite_version=1,
        harness_sha256="c" * 64,
        image_sha256="d" * 64,
        expected_task_identities=(("fixture", "dev", "session", "evt-fixture"),),
        tasks=(task,),
        champion_experiment_id="exp_" + "1" * 32,
        champion_baseline_name="robust_z",
        champion_suite_scores=dict.fromkeys((0, 1, 2), 0.0),
    )
    source = b"def build_candidate():\n    return None\n"
    source_hash = hashlib.sha256(source).hexdigest()
    proposal = RegisteredProposal(
        experiment_id="exp_" + "2" * 32,
        experiment_number=1,
        proposal=CandidateProposal(
            hypothesis="Confirmation boundary fixture",
            move_type="features",
            candidate_source=source.decode(),
            predicted_delta=0.1,
        ),
        candidate_sha256=source_hash,
        candidate_blob_sha256=source_hash,
        inputs_sha256="f" * 64,
        messages_blob_sha256="0" * 64,
        calibration_sha256=calibration_sha256(calibration),
        parent_experiment_id="exp_" + "1" * 32,
        parent_tree_sha256=_candidate_git_tree(source),
        harness_sha256="c" * 64,
        image_sha256="d" * 64,
        suite_id=calibration.suite_id,
        suite_version=1,
        system="S1",
        input_tokens=1,
        output_tokens=1,
        ledger_sequence=3,
    )
    measurement = {
        "task_id": task.task_id,
        "experiment_id": proposal.experiment_id,
        "evaluation_kind": "primary",
        "seed": 0,
        "dataset_id": task.dataset_id,
        "split_id": task.split_id,
        "session_id": task.session_id,
        "profile_sha256": task.profile_sha256,
        "candidate_sha256": proposal.candidate_sha256,
        "harness_sha256": "c" * 64,
        "candidate_output_sha256": "4" * 64,
        "vus_pr": 0.9,
        "vus_roc": 0.9,
        "fit_seconds": 1.0,
        "score_seconds": 1.0,
        "guards": {name: f"{name}_pass" for name in ("hardcoding", "determinism", "causality")},
    }
    primary = PrimaryEvaluationResult(
        proposal.experiment_id, "primary_measured", (measurement,), None, 3.0
    )
    arguments = {
        "run_id": run_id,
        "proposal": proposal,
        "tasks": (
            SimpleNamespace(
                task_id=task.task_id,
                dataset_id=task.dataset_id,
                split_id=task.split_id,
                session_id=task.session_id,
                profile_sha256=task.profile_sha256,
                family="EVT",
            ),
        ),
        "result": primary,
        "calibration": calibration,
        "parent_source": source,
        "artifact_root": tmp_path,
        "best_suite": 0.0,
    }
    return arguments, primary


def _repeat(primary: PrimaryEvaluationResult, seed: int) -> PrimaryEvaluationResult:
    return replace(
        primary,
        evaluation_kind="confirmation",
        seed=seed,
        measurements=tuple(
            dict(row, evaluation_kind="confirmation", seed=seed) for row in primary.measurements
        ),
    )


def test_provisional_keep_cannot_commit_without_confirmation(tmp_path: Path) -> None:
    arguments, _ = _fixture(tmp_path)
    comparison = decide_proposal_from_measurements(
        **{key: value for key, value in arguments.items() if key not in {"artifact_root", "run_id"}}
    )
    assert comparison.decision.verdict == "KEEP"
    with pytest.raises(ValueError, match="requires confirmation seed one"):
        commit_primary_terminal_record(object(), **arguments)
    assert not list(tmp_path.iterdir())


def test_confirmed_keep_cannot_commit_without_new_champion_noise(tmp_path: Path) -> None:
    arguments, primary = _fixture(tmp_path)
    with pytest.raises(ValueError, match="seed-two champion-noise"):
        commit_primary_terminal_record(
            object(), confirmation_result=_repeat(primary, 1), **arguments
        )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("corruption", ["candidate", "profile", "guard", "task_duplicate"])
def test_new_champion_noise_rejects_unbound_measurements(
    tmp_path: Path,
    corruption: str,
) -> None:
    arguments, primary = _fixture(tmp_path)
    noise = _repeat(primary, 2)
    row = dict(noise.measurements[0])
    if corruption == "candidate":
        row["candidate_sha256"] = "9" * 64
    elif corruption == "profile":
        row["profile_sha256"] = "9" * 64
    elif corruption == "guard":
        row["guards"] = {"hardcoding": "hardcoding_pass"}
    rows = (row, row) if corruption == "task_duplicate" else (row,)
    noise = replace(noise, measurements=rows)
    with pytest.raises(ValueError, match="invalid identity|incomplete guard|incomplete"):
        commit_primary_terminal_record(
            object(),
            confirmation_result=_repeat(primary, 1),
            champion_noise_result=noise,
            **arguments,
        )
    assert not list(tmp_path.iterdir())
