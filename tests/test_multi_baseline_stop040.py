"""Inert mixed baseline stop fixtures; no runtime or PostgreSQL acceptance evidence."""

from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from test_stopped_baseline_closure048 import owner, prepared

from lab.director import stop_closure
from lab.director.artifacts import read_director_artifact, store_director_artifact
from lab.director.contracts import PerTaskResult
from lab.director.journal import canonical_bytes
from lab.director.ledger import canonical_json_bytes
from lab.director.recovery import RecoveryPending
from lab.scorer import stop_recovery


@pytest.fixture(autouse=True)
def disk_reserve(monkeypatch):
    monkeypatch.setattr("lab.scorer.jobs.MIN_FREE_DISK_BYTES", 0)


def mixed_fixture(tmp_path, states, counts):
    args = prepared(tmp_path)
    manifest = json.loads(args["suite_manifest"])
    task = manifest["tasks"][0]
    manifest["tasks"] = [{**task, "task_id": f"task-{index}"} for index in range(27)]
    args["suite_manifest"] = canonical_bytes(manifest)
    args["execution"]["suite_manifest_sha256"] = hashlib.sha256(args["suite_manifest"]).hexdigest()
    rows, plans, scores, receipts = [], [], [], {}
    for index, (state, count) in enumerate(zip(states, counts, strict=True)):
        row = {
            **args["row"],
            "baseline_name": ("robust_z", "iforest", "ecod_train_frozen")[index],
            "experiment_id": "exp_" + str(index + 1).zfill(32),
            "sequence": index,
        }
        inputs = {
            "schema": "baseline-inputs.v1",
            "run_id": str(args["run_id"]),
            "baseline_name": row["baseline_name"],
            "candidate_sha256": row["candidate_sha256"],
            **{
                key: value
                for key, value in args["execution"].items()
                if key != "suite_manifest_sha256"
            },
            "tasks": [
                {
                    key: task[key]
                    for key in (
                        "task_id",
                        "dataset_id",
                        "split_id",
                        "session_id",
                        "profile_sha256",
                        "family",
                    )
                }
                for task in manifest["tasks"]
            ],
            "seeds": [0, 1, 2],
        }
        row["inputs_sha256"] = store_director_artifact(
            canonical_bytes(inputs), artifact_root=tmp_path
        )
        original = [
            {
                "experiment_id": row["experiment_id"],
                "evaluation_kind": "baseline",
                "candidate_sha256": row["candidate_sha256"],
                "seed": seed,
                **{key: task[key] for key in ("task_id", "dataset_id", "split_id", "session_id")},
            }
            for seed in range(3)
            for task in manifest["tasks"]
        ]
        plans.extend(original)
        scores.extend(
            {**cell, "profile_sha256": "c" * 64, "task_family": "EVT", "harness_sha256": "d" * 64}
            for cell in original[:count]
        )
        if state in {"scored", "abandoned"}:
            experiment, trajectory = stop_closure._baseline_documents(**{**args, "row": row})
            if state == "scored":
                per_task = tuple(
                    PerTaskResult(
                        task_id=task["task_id"],
                        score_norm=None,
                        task_score=0.5,
                        event_f1=None,
                        fit_seconds=1.0,
                        score_seconds=1.0,
                    )
                    for task in manifest["tasks"]
                )
                experiment = experiment.model_copy(
                    update={
                        "status": "scored",
                        "per_task": per_task,
                        "wall_seconds": 54.0,
                        "fit_seconds": 27.0,
                        "score_seconds": 27.0,
                    }
                )
            receipt = {
                "run_id": str(args["run_id"]),
                "experiment_id": row["experiment_id"],
                "status": state,
            }
            for kind, document in (("experiment", experiment), ("trajectory", trajectory)):
                raw = canonical_json_bytes(document)
                receipt[kind + "_blob_sha256"] = store_director_artifact(
                    raw, artifact_root=tmp_path
                )
                receipt[kind + "_sha256"] = hashlib.sha256(raw).hexdigest()
            receipts[row["experiment_id"]] = receipt
        rows.append({**row, "status": state})
    return {
        key: args[key] for key in ("run_id", "execution", "artifact_root", "suite_manifest")
    } | {"rows": rows, "plans": plans, "measurements": scores}, receipts


@pytest.mark.parametrize(
    "states,counts",
    [
        (("primary_running",), (18,)),
        (("scored", "primary_running"), (81, 18)),
        (("scored", "scored", "primary_running"), (81, 81, 18)),
        (("scored", "scored", "scored"), (81, 81, 81)),
        (("primary_running", "primary_running", "proposed"), (18, 9, 0)),
        (("scored", "abandoned", "primary_running"), (81, 18, 9)),
    ],
)
def test_mixed_original_cells_and_terminal_pairs_retained(tmp_path, monkeypatch, states, counts):
    payload, receipts = mixed_fixture(tmp_path, states, counts)
    before = deepcopy(payload)
    monkeypatch.setattr(
        stop_closure, "_retained_baseline_receipt", lambda engine, key: receipts[key]
    )
    retained = stop_closure._validate_stopped_baseline_inventory(MagicMock(), **payload)
    unfinished = stop_closure._partition_stopped_baselines(payload["rows"])
    assert retained == receipts
    assert payload == before
    assert len(payload["measurements"]) == sum(counts)
    assert {row["experiment_id"] for row in unfinished}.isdisjoint(retained)
    for row in payload["rows"]:
        if row["status"] == "scored":
            with pytest.raises(ValueError, match="cannot be rewritten"):
                stop_closure._baseline_documents(
                    **{
                        key: payload[key]
                        for key in ("run_id", "execution", "artifact_root", "suite_manifest")
                    },
                    row=row,
                    recovery_id=payload["run_id"],
                    failures=[],
                )


@pytest.mark.parametrize(
    "corruption",
    [
        "foreign_score",
        "foreign_plan",
        "missing_scored_cell",
        "missing_pair",
        "pair_run",
        "pair_digest",
        "changed_task",
    ],
)
def test_mixed_inventory_corruption_fails_closed(tmp_path, monkeypatch, corruption):
    payload, receipts = mixed_fixture(tmp_path, ("scored", "primary_running"), (81, 18))
    key = payload["rows"][0]["experiment_id"]
    if corruption == "foreign_score":
        payload["measurements"][-1]["experiment_id"] = "foreign"
    elif corruption == "foreign_plan":
        payload["plans"][-1]["experiment_id"] = "foreign"
    elif corruption == "missing_scored_cell":
        payload["measurements"].pop(0)
    elif corruption == "missing_pair":
        receipts[key] = None
    elif corruption == "pair_run":
        receipts[key]["run_id"] = "foreign"
    elif corruption == "pair_digest":
        receipts[key]["experiment_sha256"] = "f" * 64
    else:
        payload["measurements"][-1]["profile_sha256"] = "foreign"
    monkeypatch.setattr(
        stop_closure, "_retained_baseline_receipt", lambda engine, key: receipts[key]
    )
    with pytest.raises(RecoveryPending):
        stop_closure._validate_stopped_baseline_inventory(MagicMock(), **payload)


@pytest.mark.parametrize(
    "corruption", ["four", "duplicate_name", "proposal", "unknown_name", "crashed"]
)
def test_existing_baseline_scope_rejects_unsupported_rows(corruption):
    rows = [
        {"experiment_id": "a", "kind": "baseline", "baseline_name": "robust_z", "status": "scored"},
        {
            "experiment_id": "b",
            "kind": "baseline",
            "baseline_name": "iforest",
            "status": "primary_running",
        },
    ]
    if corruption == "four":
        rows.extend([{**rows[0], "experiment_id": str(index)} for index in (2, 3)])
    elif corruption == "duplicate_name":
        rows[1]["baseline_name"] = "robust_z"
    elif corruption == "proposal":
        rows[1]["kind"] = "proposal"
    elif corruption == "unknown_name":
        rows[1]["baseline_name"] = "unknown"
    else:
        rows[1]["status"] = "crashed"
    with pytest.raises(RecoveryPending):
        stop_closure._partition_stopped_baselines(rows)


def test_retained_pair_digest_checked_after_closure(monkeypatch):
    monkeypatch.setattr(
        stop_closure, "_retained_baseline_receipt", lambda engine, key: {"sha": "changed"}
    )
    with pytest.raises(RecoveryPending, match="changed a retained"):
        stop_closure._assert_retained_baseline_receipts(
            MagicMock(), {"original": {"sha": "original"}}
        )


def migration_guard():
    path = Path(__file__).parents[1] / "lab/db/migrations/versions/0034_multi_baseline_stop.py"
    module = ast.parse(path.read_text())
    upgrade = next(
        node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
    )
    sql = upgrade.body[1].value.args[0].value
    start = sql.index("(SELECT count(*) FROM lab.experiments WHERE run_id=r.run_id) NOT BETWEEN")
    end = sql.index(" THEN", start)
    return sql[start:end].replace("r.run_id::text", ":run").replace("r.run_id", ":run")


@pytest.mark.parametrize(
    "corruption",
    [
        None,
        "four",
        "duplicate",
        "unknown",
        "proposal",
        "missing_pair",
        "foreign_pair",
        "partial_scored",
    ],
)
def test_actual_migration_mixed_scope_predicate(corruption):
    connection = sqlite3.connect(":memory:")
    connection.execute("ATTACH DATABASE ':memory:' AS lab")
    connection.execute("ATTACH DATABASE ':memory:' AS scorer")
    connection.execute(
        "CREATE TABLE lab.experiments(run_id,experiment_id,baseline_name,status,kind,"
        "candidate_sha256,inputs_sha256)"
    )
    connection.execute("CREATE TABLE lab.experiment_records(experiment_id,experiment_json)")
    connection.execute("CREATE TABLE lab.trajectory_records(experiment_id,trajectory_json)")
    connection.execute("CREATE TABLE scorer.run_tasks(run_id,experiment_id)")
    connection.execute("CREATE TABLE scorer.task_scores(run_id,experiment_id)")
    for key, name, state, cells in (
        ("a", "robust_z", "scored", 81),
        ("b", "iforest", "primary_running", 18),
    ):
        connection.execute(
            "INSERT INTO lab.experiments VALUES ('run',?,?,?,'baseline','c','i')",
            (key, name, state),
        )
        connection.executemany("INSERT INTO scorer.run_tasks VALUES ('run',?)", [(key,)] * 81)
        connection.executemany("INSERT INTO scorer.task_scores VALUES ('run',?)", [(key,)] * cells)
    document = json.dumps(
        {
            "run_id": "run",
            "kind": "baseline",
            "status": "scored",
            "baseline_name": "robust_z",
            "candidate_sha256": "c",
            "inputs_sha256": "i",
        }
    )
    connection.execute("INSERT INTO lab.experiment_records VALUES ('a',?)", (document,))
    connection.execute("INSERT INTO lab.trajectory_records VALUES ('a',?)", (document,))
    if corruption == "four":
        connection.executemany(
            "INSERT INTO lab.experiments VALUES ('run',?,'ecod_train_frozen',"
            "'proposed','baseline','c','i')",
            [("c",), ("d",)],
        )
    elif corruption in {"duplicate", "unknown", "proposal"}:
        column, value = {
            "duplicate": ("baseline_name", "robust_z"),
            "unknown": ("baseline_name", "unknown"),
            "proposal": ("kind", "proposal"),
        }[corruption]
        connection.execute(
            f"UPDATE lab.experiments SET {column}=? WHERE experiment_id='b'", (value,)
        )
    elif corruption == "missing_pair":
        connection.execute("DELETE FROM lab.trajectory_records")
    elif corruption == "foreign_pair":
        connection.execute(
            "UPDATE lab.experiment_records SET experiment_json=?",
            (document.replace('"run"', '"foreign"'),),
        )
    elif corruption == "partial_scored":
        connection.execute("DELETE FROM scorer.task_scores WHERE rowid=1")
    guard = migration_guard()
    # Existing proposal exclusion is preserved outside this extracted scope clause.
    if corruption == "proposal":
        guard = (
            "EXISTS(SELECT 1 FROM lab.experiments WHERE run_id=:run AND kind<>'baseline') OR "
            + guard
        )
    assert connection.execute("SELECT (" + guard + ")", {"run": "run"}).fetchone()[0] == bool(
        corruption
    )
    connection.close()


class FixtureResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value

    def mappings(self):
        return self

    def scalars(self):
        return self

    def all(self):
        return self.value


def orchestration_fixture(tmp_path, monkeypatch, states, counts):
    payload, receipts = mixed_fixture(tmp_path, states, counts)
    calls = {"close": [], "commit": [], "begin": [], "window": 120.0}
    suite_path = tmp_path.parent / (tmp_path.name + "-original-suite.json")
    suite_path.write_bytes(payload["suite_manifest"])
    payload["execution"]["registry_entry_sha256"] = "r" * 64
    registry = MagicMock()
    registry.entry_sha256.return_value = "r" * 64
    registry.verify_entry.return_value = (suite_path, "unused")
    monkeypatch.setenv("LAB_SUITE_REGISTRY_FILE", str(tmp_path / "original-registry.json"))
    monkeypatch.setattr("lab.api.registry.load_suite_registry", lambda *args: registry)

    def execute(statement, parameters):
        query = str(statement)
        if "execution_json FROM lab.director_execution_contracts" in query:
            value = payload["execution"]
        elif "FROM lab.experiments" in query:
            value = payload["rows"]
        elif "FROM lab.dev_task_results" in query:
            value = payload["measurements"]
        elif "FROM scorer.run_tasks" in query:
            value = payload["plans"]
        elif "experiment_record_receipt" in query:
            value = deepcopy(receipts[parameters["id"]])
        elif "begin_stopped_baseline_closure" in query:
            calls["begin"].append(parameters["id"])
            value = calls["window"]
        elif "close_stopped_unattempted_tasks" in query:
            calls["close"].append(parameters["experiment"])
            value = True
        elif "FROM lab.director_stop_job_drains" in query:
            value = []
        else:
            raise AssertionError("unexpected fixture SQL: " + query)
        return FixtureResult(value)

    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.side_effect = execute
    engine.begin.return_value.__enter__.return_value.execute.side_effect = execute

    def commit(_engine, **arguments):
        experiment = arguments["experiment"]
        trajectory = arguments["trajectory"]
        calls["commit"].append(experiment.experiment_id)
        for row in payload["rows"]:
            if row["experiment_id"] == experiment.experiment_id:
                row["status"] = "abandoned"
        receipts[experiment.experiment_id] = {
            "run_id": str(experiment.run_id),
            "experiment_id": experiment.experiment_id,
            "status": "abandoned",
            "experiment_blob_sha256": arguments["experiment_blob_sha256"],
            "experiment_sha256": hashlib.sha256(canonical_json_bytes(experiment)).hexdigest(),
            "trajectory_blob_sha256": arguments["trajectory_blob_sha256"],
            "trajectory_sha256": hashlib.sha256(canonical_json_bytes(trajectory)).hexdigest(),
        }

    monkeypatch.setattr(stop_closure, "commit_experiment_record", commit)
    monkeypatch.setattr(stop_closure, "_has_baseline_calibration", lambda *args: False)
    monkeypatch.setattr(stop_closure, "prove_stopped_owner_dead", MagicMock(return_value=True))
    monkeypatch.setattr(stop_closure, "_drain_director_sandbox", MagicMock())
    monkeypatch.setattr(
        stop_closure,
        "run_stopped_director_recovery_process",
        MagicMock(return_value=SimpleNamespace(state="drained", exit_code=0)),
    )
    monkeypatch.setattr(stop_closure, "_prove_stopped_terminal_jobs", MagicMock())
    monkeypatch.setattr(
        stop_closure, "remaining_stop_closure_seconds", lambda *args: int(calls["window"])
    )
    monkeypatch.setattr(stop_closure, "owned_execution", lambda *args: nullcontext())
    arguments = {
        "run_id": payload["run_id"],
        "recovery_id": payload["run_id"],
        "owner": owner(),
        "execution_owner": SimpleNamespace(generation=1, execution_sha256="e" * 64),
        "lease": MagicMock(),
        "artifact_root": tmp_path,
    }
    return engine, arguments, payload, receipts, calls


@pytest.mark.parametrize(
    "states,counts",
    [
        (("scored", "primary_running"), (81, 18)),
        (("scored", "scored", "primary_running"), (81, 81, 18)),
    ],
)
def test_actual_reconcile_closes_only_unfinished_and_retry_preserves_pairs(
    tmp_path,
    monkeypatch,
    states,
    counts,
):
    engine, arguments, payload, receipts, calls = orchestration_fixture(
        tmp_path,
        monkeypatch,
        states,
        counts,
    )
    original_receipts = deepcopy(receipts)
    original_cells = deepcopy((payload["plans"], payload["measurements"]))
    original_execution = deepcopy(payload["execution"])
    artifact_bytes = {
        digest: read_director_artifact(digest, artifact_root=tmp_path)
        for receipt in receipts.values()
        for key, digest in receipt.items()
        if key.endswith("_blob_sha256")
    }
    unfinished = [
        row["experiment_id"] for row in payload["rows"] if row["status"] == "primary_running"
    ]
    stop_closure.reconcile_stopped_baseline(engine, engine, **arguments)
    assert calls["close"] == unfinished
    assert calls["commit"] == unfinished
    assert payload["plans"] == original_cells[0]
    assert payload["measurements"] == original_cells[1]
    assert payload["execution"] == original_execution
    assert all(receipts[key] == receipt for key, receipt in original_receipts.items())
    assert all(
        read_director_artifact(digest, artifact_root=tmp_path) == raw
        for digest, raw in artifact_bytes.items()
    )
    after_first = deepcopy(receipts)
    calls["window"] = 88.0  # Retry receives only the aged original stop allowance.
    stop_closure.reconcile_stopped_baseline(engine, engine, **arguments)
    assert receipts == after_first
    assert calls["close"] == unfinished
    assert calls["commit"] == unfinished
    assert calls["begin"] == [arguments["recovery_id"]] * 2
    assert calls["window"] == 88.0


@pytest.mark.parametrize("failure", ["expired_begin", "expired_commit", "live_child"])
def test_actual_reconcile_expired_authority_or_live_child_blocks_commit(
    tmp_path,
    monkeypatch,
    failure,
):
    engine, arguments, payload, receipts, calls = orchestration_fixture(
        tmp_path,
        monkeypatch,
        ("scored", "primary_running"),
        (81, 18),
    )
    before = deepcopy((payload, receipts))
    if failure == "expired_begin":
        calls["window"] = 0.0
    elif failure == "expired_commit":
        monkeypatch.setattr(stop_closure, "remaining_stop_closure_seconds", lambda *args: 0)
    else:
        monkeypatch.setattr(
            stop_closure,
            "_prove_stopped_terminal_jobs",
            MagicMock(side_effect=RecoveryPending("exact child remains live")),
        )
    with pytest.raises(RecoveryPending):
        stop_closure.reconcile_stopped_baseline(engine, engine, **arguments)
    assert calls["commit"] == []
    assert (payload, receipts) == before
    if failure in {"expired_begin", "live_child"}:
        assert calls["close"] == []


@pytest.mark.parametrize(
    "proposal,count,limit,owner_check",
    [
        (False, 4097, 9217, True),
        (False, 9217, 9217, False),
        (True, 4096, 4097, True),
        (True, 4097, 4097, False),
    ],
)
def test_scorer_inventory_expands_only_baseline_scope(
    monkeypatch, proposal, count, limit, owner_check
):
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    request = {"run_id": uuid4(), "remaining": 120, "proposal_closure": proposal, "owner_json": {}}
    request_result = MagicMock()
    request_result.mappings.return_value.one.return_value = request
    jobs_result = FixtureResult([uuid4()] * count)
    connection.execute.side_effect = [request_result, jobs_result]
    monkeypatch.setattr(stop_recovery, "_stored_owner", lambda value: object())
    proof = MagicMock(return_value=False)
    monkeypatch.setattr(stop_recovery, "prove_stopped_owner_dead", proof)
    assert (
        stop_recovery.reconcile_stopped_children(
            engine,
            uuid4(),
            recovery_invocation="a" * 32,
        )
        == "pending"
    )
    assert connection.execute.call_args_list[1].args[1]["inventory_limit"] == limit
    assert proof.called is owner_check
