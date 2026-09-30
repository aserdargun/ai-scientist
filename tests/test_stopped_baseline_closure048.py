"""Inert stop closure tests; no services, Docker, database, model, or GPU use."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from lab.director import recovery, stop_closure
from lab.director.artifacts import read_director_artifact, store_director_artifact
from lab.director.journal import canonical_bytes
from lab.scorer import holdout_supervisor, stop_recovery


@pytest.fixture(autouse=True)
def fixture_disk_reserve(monkeypatch):
    # Tiny unit artifacts do not exercise the production 20GiB reserve policy.
    monkeypatch.setattr("lab.scorer.jobs.MIN_FREE_DISK_BYTES", 0)


def owner():
    return recovery.OwnerGeneration(
        "a" * 64,
        101,
        900,
        str(uuid4()),
        recovery.OWNER_DRAIN_UNIT,
        "b" * 32,
        "/user.slice/" + recovery.OWNER_DRAIN_UNIT,
    )


def replacement(monkeypatch, old):
    monkeypatch.setattr(
        recovery, "_owner_is_proven_dead", MagicMock(side_effect=recovery.RecoveryPending("active"))
    )
    monkeypatch.setattr(recovery, "_current_boot_id", lambda: old.worker_boot_id)
    monkeypatch.setattr(
        recovery, "_current_process_start_ticks", lambda pid: 999 if pid == 202 else None
    )
    monkeypatch.setattr(stop_closure, "_process_cgroup", lambda pid: old.worker_cgroup)
    props = dict(
        LoadState="loaded",
        ActiveState="active",
        InvocationID="c" * 32,
        MainPID="202",
        ControlGroup=old.worker_cgroup,
    )
    monkeypatch.setattr(recovery, "_systemctl_show", lambda unit: props.copy())
    return props


def test_retired_shared_generation_proves_replacement_without_emptying_cgroup(monkeypatch):
    old = owner()
    replacement(monkeypatch, old)
    empty = MagicMock(side_effect=AssertionError("must not empty replacement cgroup"))
    monkeypatch.setattr(recovery, "_require_cgroup_empty", empty)
    assert stop_closure.prove_stopped_owner_dead(old, uuid4())
    empty.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("InvocationID", "b" * 32),
        ("InvocationID", "invalid"),
        ("MainPID", "0"),
        ("ControlGroup", "/other"),
        ("ActiveState", "activating"),
        ("LoadState", "error"),
    ],
)
def test_ambiguous_shared_replacement_stays_pending(monkeypatch, field, value):
    old = owner()
    props = replacement(monkeypatch, old)
    props[field] = value
    with pytest.raises(recovery.RecoveryPending):
        stop_closure.prove_stopped_owner_dead(old, uuid4())


def test_same_original_process_is_never_recovered(monkeypatch):
    old = owner()
    replacement(monkeypatch, old)
    monkeypatch.setattr(
        recovery, "_current_process_start_ticks", lambda pid: old.worker_start_ticks
    )
    assert stop_closure.prove_stopped_owner_dead(old, uuid4()) is False


def test_replacement_identity_race_stays_pending(monkeypatch):
    old = owner()
    props = replacement(monkeypatch, old)
    monkeypatch.setattr(
        recovery, "_systemctl_show", MagicMock(side_effect=[props, {**props, "MainPID": "303"}])
    )
    with pytest.raises(recovery.RecoveryPending, match="changed"):
        stop_closure.prove_stopped_owner_dead(old, uuid4())


def test_nonshared_unit_cannot_use_retired_exception(monkeypatch):
    old = replace(
        owner(), worker_unit=f"swapp-ai-scientist-director-dispatch-{uuid4().hex}.service"
    )
    replacement(monkeypatch, old)
    with pytest.raises(recovery.RecoveryPending):
        stop_closure.prove_stopped_owner_dead(old, uuid4())


def prepared(tmp_path):
    run = uuid4()
    provenance = dict(
        dataset_id="synthetic",
        split_id="dev",
        session_id="one",
        source_manifest_sha256="b" * 64,
        source_revision="fixture.v1",
        license_id="CC0-1.0",
        attribution="Synthetic fixture",
        access_terms="Generated locally",
        usage_profile="noncommercial_research",
    )
    task = dict(
        task_id="task",
        dataset_id="synthetic",
        split_id="dev",
        session_id="one",
        profile_sha256="c" * 64,
        family="EVT",
        task_weight=1.0,
        independent_family="synthetic",
        label_tier="gold",
        provenance=provenance,
        context={},
        columns=["sensor"],
        train=[[0.0], [1.0]],
        evaluation=[[2.0]],
    )
    suite = canonical_bytes(
        dict(
            schema="director-suite.v1",
            suite_id="fixture",
            suite_version=1,
            tasks=[task],
            weight_policy="single_snapshot_study.v1",
            family_cap=1.0,
            type_shares={"EVT": 1.0},
            family_shares={"synthetic": 1.0},
        )
    )
    execution = dict(
        suite_id="fixture",
        suite_version=1,
        harness_sha256="d" * 64,
        image_sha256="e" * 64,
        suite_manifest_sha256=hashlib.sha256(suite).hexdigest(),
    )
    # Deliberately original historical source, not current regenerated algorithm text.
    source = store_director_artifact(
        b"# original prepared historical baseline\n", artifact_root=tmp_path
    )
    inputs = dict(
        schema="baseline-inputs.v1",
        run_id=str(run),
        baseline_name="robust_z",
        candidate_sha256=source,
        **{k: v for k, v in execution.items() if k != "suite_manifest_sha256"},
        tasks=[
            {
                k: task[k]
                for k in (
                    "task_id",
                    "dataset_id",
                    "split_id",
                    "session_id",
                    "profile_sha256",
                    "family",
                )
            }
        ],
        seeds=[0, 1, 2],
    )
    row = dict(
        kind="baseline",
        status="primary_running",
        baseline_name="robust_z",
        candidate_sha256=source,
        candidate_blob_sha256=source,
        inputs_sha256=store_director_artifact(canonical_bytes(inputs), artifact_root=tmp_path),
        experiment_id="exp_" + uuid4().hex,
        parent_experiment_id=None,
        move_type="detector",
        system="S1",
        hypothesis="Original immutable baseline intent",
        predicted_delta=None,
        sequence=0,
    )
    return dict(
        run_id=run,
        row=row,
        execution=execution,
        artifact_root=tmp_path,
        recovery_id=uuid4(),
        failures=[{"job_id": str(uuid4()), "error_code": "retryable_infrastructure"}],
        suite_manifest=suite,
    )


def test_abandoned_pair_preserves_original_source_and_failure_evidence(tmp_path):
    args = prepared(tmp_path)
    experiment, trajectory = stop_closure._baseline_documents(**args)
    assert experiment.status == "abandoned"
    assert experiment.wall_seconds is None
    assert experiment.candidate_sha256 == args["row"]["candidate_sha256"]
    assert experiment.harness_sha256 == "d" * 64
    assert experiment.per_task == () and experiment.calibration_sha256 is None
    assert (
        experiment.suite_score
        is experiment.decision
        is experiment.fit_seconds
        is experiment.score_seconds
        is None
    )
    assert (
        trajectory.outcome is None
        and trajectory.messages_blob_sha256 == hashlib.sha256(b"[]").hexdigest()
    )
    evidence_sha = trajectory.exclusions[-1].split(": ", 1)[1]
    evidence = json.loads(read_director_artifact(evidence_sha, artifact_root=tmp_path))
    assert evidence["original_jobs"] == args["failures"]
    assert trajectory.source_provenance[0].license_id == "CC0-1.0"
    assert stop_closure._baseline_documents(**args) == (experiment, trajectory)


@pytest.mark.parametrize("mutation", ["source", "suite", "pins", "kind"])
def test_abandonment_rejects_conflicting_original_identity(tmp_path, mutation):
    args = prepared(tmp_path)
    if mutation == "source":
        args["row"]["candidate_sha256"] = "f" * 64
    elif mutation == "suite":
        args["suite_manifest"] += b" "
    elif mutation == "pins":
        args["execution"]["harness_sha256"] = "f" * 64
    else:
        args["row"]["kind"] = "proposal"
    with pytest.raises(ValueError):
        stop_closure._baseline_documents(**args)


def test_fixed_stop_worker_argv_target_and_receipt(monkeypatch):
    stop = uuid4()
    call = MagicMock(return_value=SimpleNamespace(state="drained"))
    monkeypatch.setattr(holdout_supervisor, "_run_holdout_recovery_process", call)
    assert (
        holdout_supervisor.run_stopped_director_recovery_process(stop, remaining_seconds=8).state
        == "drained"
    )
    call.assert_called_once_with(stop, target_kind="stop", remaining_seconds=8)
    assert (
        holdout_supervisor._parse_recovery_status(
            json.dumps({"stop_id": str(stop), "state": "drained"}), stop, target_kind="stop"
        )
        == "drained"
    )
    with pytest.raises(RuntimeError):
        holdout_supervisor._parse_recovery_status(
            json.dumps({"restart_id": str(stop), "state": "drained"}), stop, target_kind="stop"
        )


def test_expired_stop_attempt_makes_no_worker_side_effect(monkeypatch):
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    request = {"run_id": uuid4(), "remaining": 0.0}
    connection.execute.return_value.mappings.return_value.one.return_value = request
    connection.execute.return_value.scalars.return_value.all.return_value = []
    proof = MagicMock(side_effect=AssertionError("no host proof after expiry"))
    monkeypatch.setattr(stop_recovery, "prove_stopped_owner_dead", proof)
    assert (
        stop_recovery.reconcile_stopped_children(engine, uuid4(), recovery_invocation="a" * 32)
        == "pending"
    )
    engine.begin.assert_not_called()
    proof.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        {"status": "crashed"},
        {"status": "scored"},
        {"fit_seconds": 0.1},
        {"score_seconds": 0.1},
        {"suite_score": 0.0},
    ],
)
def test_unknown_wall_only_for_unmeasured_abandoned_baseline(tmp_path, change):
    from lab.director.contracts import ExperimentDocument

    experiment, _ = stop_closure._baseline_documents(**prepared(tmp_path))
    payload = experiment.model_dump(by_alias=True)
    payload.update(change)
    with pytest.raises(ValueError, match="unknown wall time"):
        ExperimentDocument.model_validate(payload, strict=True)


@pytest.mark.parametrize(
    "old_alive,wrong_child,expected",
    [(False, False, "drained"), (True, False, "pending"), (False, True, "pending")],
)
def test_stopped_worker_only_cas_after_independent_exact_generation_proof(
    monkeypatch, old_alive, wrong_child, expected
):
    from contextlib import nullcontext
    from dataclasses import asdict

    old = owner()
    run, job_id, stop = uuid4(), uuid4(), uuid4()
    job = dict(
        job_id=str(job_id),
        run_id=str(run),
        admitted_generation=1,
        execution_sha256="d" * 64,
        claim_invocation_id="f" * 32,
        state="queued",
        error_code="retryable_infrastructure",
    )
    request = dict(
        run_id=run,
        remaining=119.0,
        expected_generation=1,
        execution_sha256="d" * 64,
        owner_json=asdict(old),
    )
    mutations = []
    connection = MagicMock()

    def execute(statement, params):
        sql = str(statement)
        result = MagicMock()
        if "SELECT *,extract" in sql:
            result.mappings.return_value.one.return_value = request
        elif "SELECT job_id" in sql:
            result.scalars.return_value.all.return_value = [job_id]
        elif "to_jsonb" in sql:
            result.scalar_one.return_value = job.copy()
        else:
            mutations.append((sql, params))
            result.scalar_one.return_value = "drained" if "finish_stopped" in sql else "failed"
        return result

    connection.execute.side_effect = execute
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = connection
    engine.begin.return_value.__enter__.return_value = connection
    monkeypatch.setattr(stop_recovery, "prove_stopped_owner_dead", lambda *a: not old_alive)
    monkeypatch.setattr(stop_recovery, "_job_lifecycle_lock", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(stop_recovery, "_expected_unit_cgroup", lambda unit: "/exact/" + unit)
    monkeypatch.setattr(stop_recovery, "_cgroup_is_absent_or_empty", lambda group: True)
    monkeypatch.setattr(
        stop_recovery,
        "_systemctl_show",
        lambda unit: dict(
            LoadState="loaded",
            ActiveState="inactive",
            MainPID="0",
            ControlGroup="",
            InvocationID=("0" if wrong_child else "f") * 32,
        ),
    )
    assert (
        stop_recovery.reconcile_stopped_children(engine, stop, recovery_invocation="a" * 32)
        == expected
    )
    if expected == "pending":
        assert mutations == []
    else:
        assert len(mutations) == 2
        assert "reconcile_stopped_score_job" in mutations[0][0]
        assert json.loads(mutations[0][1]["expected"]) == job
        assert mutations[0][1]["stop"] == stop
        assert "finish_stopped_children" in mutations[1][0]


@pytest.mark.parametrize("remaining,expected", [(None, None), (119.9, 119), (-3.1, 0)])
def test_stop_window_comes_from_original_database_timestamp(remaining, expected):
    engine = MagicMock()
    query = engine.connect.return_value.__enter__.return_value.execute
    query.return_value.scalar_one_or_none.return_value = remaining
    assert stop_closure.remaining_stop_closure_seconds(engine, uuid4()) == expected
    assert "created_at+interval '120 seconds'-clock_timestamp()" in str(query.call_args.args[0])
    engine.begin.assert_not_called()
