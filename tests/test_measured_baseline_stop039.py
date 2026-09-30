"""Prepared-cell provenance checks; fixtures do not prove actual PG stop authority."""

from copy import deepcopy
from typing import Any

import pytest

from lab.director.recovery import RecoveryPending
from lab.director.stop_closure import _validate_baseline_cells


def cells() -> dict[str, Any]:
    task = {
        "task_id": "one",
        "dataset_id": "fixture",
        "split_id": "dev",
        "session_id": "sample",
        "profile_sha256": "p" * 64,
        "family": "EVT",
    }
    row = {"experiment_id": "exp_fixture", "candidate_sha256": "c" * 64}
    plans = [
        {
            **{key: task[key] for key in ("task_id", "dataset_id", "split_id", "session_id")},
            **row,
            "evaluation_kind": "baseline",
            "seed": seed,
        }
        for seed in range(3)
    ]
    measurement = {
        **plans[0],
        "profile_sha256": task["profile_sha256"],
        "task_family": "EVT",
        "harness_sha256": "h" * 64,
    }
    return {
        "plans": plans,
        "measurements": [measurement],
        "inputs": {"tasks": [task]},
        "row": row,
        "execution": {"harness_sha256": "h" * 64},
    }


def test_partial_verified_scores_need_no_fabricated_remaining_measurements() -> None:
    payload = cells()
    before = deepcopy(payload)
    _validate_baseline_cells(**payload)
    assert payload == before
    assert len(payload["measurements"]) == 1


@pytest.mark.parametrize(
    "field",
    [
        "experiment_id",
        "evaluation_kind",
        "candidate_sha256",
        "harness_sha256",
        "task_family",
        "dataset_id",
        "split_id",
        "session_id",
        "profile_sha256",
        "task_id",
        "seed",
    ],
)
def test_changed_measured_cell_is_rejected(field: str) -> None:
    payload = cells()
    payload["measurements"][0][field] = 9 if field == "seed" else "foreign"
    with pytest.raises(RecoveryPending):
        _validate_baseline_cells(**payload)


@pytest.mark.parametrize(
    "field",
    [
        "experiment_id",
        "evaluation_kind",
        "candidate_sha256",
        "dataset_id",
        "split_id",
        "session_id",
        "task_id",
        "seed",
    ],
)
def test_changed_prepared_plan_is_rejected(field: str) -> None:
    payload = cells()
    payload["plans"][0][field] = 9 if field == "seed" else "foreign"
    with pytest.raises(RecoveryPending):
        _validate_baseline_cells(**payload)


@pytest.mark.parametrize("kind", ["plans", "measurements"])
def test_duplicate_cells_are_rejected(kind: str) -> None:
    payload = cells()
    payload[kind].append(deepcopy(payload[kind][0]))
    with pytest.raises(RecoveryPending):
        _validate_baseline_cells(**payload)


def test_measured_baseline_requires_original_complete_plan() -> None:
    payload = cells()
    payload["plans"].pop()
    with pytest.raises(RecoveryPending, match="incomplete original task plan"):
        _validate_baseline_cells(**payload)


def test_unmeasured_existing_empty_plan_remains_supported() -> None:
    payload = cells()
    payload["plans"] = []
    payload["measurements"] = []
    _validate_baseline_cells(**payload)


def completed_claim() -> tuple[dict[str, Any], dict[str, Any]]:
    job = {
        "state": "completed",
        "job_id": "job",
        "run_id": "run",
        "experiment_id": "experiment",
        "evaluation_kind": "baseline",
        "task_id": "one",
        "seed": 0,
        "candidate_sha256": "c" * 64,
        "artifact_sha256": "a" * 64,
        "claim_invocation_id": None,
    }
    score = {
        **{
            field: job[field]
            for field in ("run_id", "experiment_id", "evaluation_kind", "task_id", "seed")
        },
        "score_job_id": "job",
        "worker_invocation_id": "f" * 32,
        "claim_token": None,
        "score": {"candidate_sha256": "c" * 64, "candidate_output_sha256": "a" * 64},
    }
    return job, score


def test_completed_claim_uses_committed_invocation_after_live_claim_cleared() -> None:
    from lab.scorer.stop_recovery import _completed_job_invocation

    job, score = completed_claim()
    assert _completed_job_invocation(job, score) == "f" * 32
    assert job["claim_invocation_id"] is None


@pytest.mark.parametrize(
    "field",
    [
        "run_id",
        "experiment_id",
        "evaluation_kind",
        "task_id",
        "seed",
        "score_job_id",
        "worker_invocation_id",
        "claim_token",
    ],
)
def test_completed_claim_identity_mismatch_is_rejected(field: str) -> None:
    from lab.scorer.stop_recovery import _completed_job_invocation

    job, score = completed_claim()
    score[field] = "unexpected-claim" if field == "claim_token" else None
    with pytest.raises(ValueError):
        _completed_job_invocation(job, score)


@pytest.mark.parametrize("field", ["candidate_sha256", "candidate_output_sha256"])
def test_completed_claim_output_mismatch_is_rejected(field: str) -> None:
    from lab.scorer.stop_recovery import _completed_job_invocation

    job, score = completed_claim()
    score["score"][field] = "other"
    with pytest.raises(ValueError):
        _completed_job_invocation(job, score)


@pytest.mark.parametrize(
    "active,invocation,load_state,control_group,expected",
    [
        ("inactive", "f" * 32, "loaded", "", "drained"),
        ("failed", "f" * 32, "loaded", "", "drained"),
        ("inactive", "0" * 32, "loaded", "", "pending"),
        ("active", "f" * 32, "loaded", "", "pending"),
        ("activating", "f" * 32, "loaded", "", "pending"),
        ("inactive", "", "not-found", "", "drained"),
        ("active", "", "not-found", "", "pending"),
        ("inactive", "f" * 32, "not-found", "", "pending"),
        ("inactive", None, "not-found", "", "pending"),
        ("inactive", "", "not-found", "/foreign", "pending"),
        ("inactive", "", "not-found", None, "pending"),
        ("inactive", "f" * 32, "error", "", "pending"),
        ("inactive", "f" * 32, "unknown", "", "pending"),
    ],
)
def test_completed_job_still_requires_actual_dead_unit(
    monkeypatch: pytest.MonkeyPatch,
    active: str,
    invocation: str | None,
    load_state: str,
    control_group: str | None,
    expected: str,
) -> None:
    from contextlib import nullcontext
    from dataclasses import asdict
    from unittest.mock import MagicMock
    from uuid import uuid4

    from lab.director.recovery import OwnerGeneration
    from lab.scorer import stop_recovery

    run, job_id, stop_id = uuid4(), uuid4(), uuid4()
    job, score = completed_claim()
    job.update(
        job_id=str(job_id), run_id=str(run), admitted_generation=1, execution_sha256="d" * 64
    )
    score.update(score_job_id=str(job_id), run_id=str(run))
    owner = OwnerGeneration("a" * 64, 101, 900, str(uuid4()), "unit", "b" * 32, "/group")
    request = dict(
        run_id=run,
        remaining=119.0,
        expected_generation=1,
        execution_sha256="d" * 64,
        owner_json=asdict(owner),
    )
    connection, engine = MagicMock(), MagicMock()

    def execute(statement: Any, _parameters: Any) -> MagicMock:
        sql, result = str(statement), MagicMock()
        if "SELECT *,extract" in sql:
            result.mappings.return_value.one.return_value = request
        elif "SELECT job_id" in sql:
            result.scalars.return_value.all.return_value = [job_id]
        elif "to_jsonb(j)" in sql:
            result.scalar_one.return_value = job
        elif "to_jsonb(s)" in sql:
            result.scalar_one.return_value = score
        else:
            assert "finish_stopped_children" in sql
            result.scalar_one.return_value = "drained"
        return result

    connection.execute.side_effect = execute
    engine.connect.return_value.__enter__.return_value = connection
    engine.begin.return_value.__enter__.return_value = connection
    monkeypatch.setattr(stop_recovery, "prove_stopped_owner_dead", lambda *_: True)
    monkeypatch.setattr(stop_recovery, "_job_lifecycle_lock", lambda *_a, **_k: nullcontext())
    monkeypatch.setattr(stop_recovery, "_expected_unit_cgroup", lambda _: "/exact")
    monkeypatch.setattr(stop_recovery, "_cgroup_is_absent_or_empty", lambda _: True)
    monkeypatch.setattr(
        stop_recovery,
        "_systemctl_show",
        lambda _: dict(
            LoadState=load_state,
            ActiveState=active,
            MainPID="0",
            InvocationID=invocation,
            ControlGroup=control_group,
        ),
    )
    assert (
        stop_recovery.reconcile_stopped_children(engine, stop_id, recovery_invocation="a" * 32)
        == expected
    )
    if expected == "pending":
        engine.begin.assert_not_called()


@pytest.mark.parametrize("quiescent", [True, False])
def test_final_stop_proof_uses_committed_invocation_for_loaded_inactive_job(
    monkeypatch: pytest.MonkeyPatch, quiescent: bool
) -> None:
    import time
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from uuid import uuid4

    from lab.director import stop_closure

    run, job_id, recovery_id = uuid4(), uuid4(), uuid4()
    owner = SimpleNamespace(generation=1, execution_sha256="d" * 64)
    job = dict(
        job_id=job_id,
        state="completed",
        claim_invocation_id=None,
        admitted_generation=1,
        execution_sha256="d" * 64,
    )
    planner, director = MagicMock(), MagicMock()
    planned = planner.connect.return_value.__enter__.return_value.execute
    planned.return_value.mappings.return_value.all.return_value = [job]
    receipt = director.connect.return_value.__enter__.return_value.execute
    receipt.return_value.scalar_one.return_value = "f" * 32
    proof = MagicMock(return_value=quiescent)
    monkeypatch.setattr(stop_closure, "scorer_job_unit_is_quiescent", proof)
    if quiescent:
        stop_closure._prove_stopped_terminal_jobs(
            director, planner, run, time.monotonic() + 120, owner, recovery_id
        )
    else:
        with pytest.raises(RecoveryPending, match="not quiescent"):
            stop_closure._prove_stopped_terminal_jobs(
                director, planner, run, time.monotonic() + 120, owner, recovery_id
            )
    assert proof.call_args.kwargs["expected_invocation_id"] == "f" * 32
    statement, params = receipt.call_args.args
    assert str(statement) == "SELECT lab.stopped_baseline_score_invocation(:stop,:job)"
    assert params == {"stop": recovery_id, "job": job_id}


@pytest.mark.parametrize(
    "corruption", [None, "cross_run", "foreign_task", "live_token", "terminal_completion"]
)
def test_rpc_final_lookup_rejects_corrupt_score_job_link(corruption: str | None) -> None:
    """Execute the actual final lookup on an inert SQL fixture with a job-id-only link."""
    import ast
    import re
    import sqlite3
    from pathlib import Path

    migration = (
        Path(__file__).resolve().parents[1]
        / "lab/db/migrations/versions/0033_measured_baseline_stop.py"
    )
    tree = ast.parse(migration.read_text())
    upgrade = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
    )
    statements = [
        ast.literal_eval(node.value.args[0])
        for node in upgrade.body
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
    ]
    rpc = next(
        sql for sql in statements if "CREATE FUNCTION lab.stopped_baseline_score_invocation" in sql
    )
    lookup = re.search(r"SELECT s.worker_invocation_id INTO invocation FROM.*?;", rpc, re.S)
    assert lookup is not None
    sql = lookup.group().replace(" INTO invocation", "").replace("a.run_id", ":run")
    sql = sql.replace("a.expected_generation", ":generation").replace(
        "a.execution_sha256", ":execution"
    )
    sql = sql.replace("p_job", ":job")
    identity = ["run", "experiment", "baseline", "task", 0]
    score_identity = identity.copy()
    if corruption == "cross_run":
        score_identity[0] = "foreign"
    if corruption == "foreign_task":
        score_identity[3] = "foreign"
    with sqlite3.connect(":memory:") as connection:
        connection.execute("ATTACH DATABASE ':memory:' AS scorer")
        connection.execute(
            "CREATE TABLE scorer.task_scores(run_id,experiment_id,evaluation_kind,task_id,seed,"
            "score_job_id,worker_invocation_id,claim_token)"
        )
        connection.execute(
            "CREATE TABLE scorer.score_jobs(run_id,experiment_id,evaluation_kind,task_id,seed,"
            "job_id,state,admitted_generation,execution_sha256)"
        )
        connection.execute(
            "CREATE TABLE scorer.task_completions(run_id,experiment_id,evaluation_kind,"
            "task_id,seed,completion_kind)"
        )
        connection.execute(
            "INSERT INTO scorer.score_jobs VALUES(?,?,?,?,?,?,?,?,?)",
            (*identity, "job", "completed", 1, "digest"),
        )
        connection.execute(
            "INSERT INTO scorer.task_scores VALUES(?,?,?,?,?,?,?,?)",
            (*score_identity, "job", "f" * 32, "live" if corruption == "live_token" else None),
        )
        connection.execute(
            "INSERT INTO scorer.task_completions VALUES(?,?,?,?,?,?)",
            (*score_identity, "terminal" if corruption == "terminal_completion" else "scored"),
        )
        result = connection.execute(
            sql, dict(run="run", generation=1, execution="digest", job="job")
        ).fetchone()
    assert result == (("f" * 32,) if corruption is None else None)
