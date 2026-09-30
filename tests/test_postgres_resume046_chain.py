"""Opt-in real-role resume/receipt SQL chains with explicitly synthetic drain evidence.

These cases never inspect/kill processes or launch services, workers, models or Docker.
The old owner is this live test process: the injected drain observation is deliberately
synthetic SQL-fixture evidence, NOT an operating-system death/drain acceptance proof.
Synthetic scores/KEEP records test durable authorities, not scientific performance.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_postgres_holdout030_recovery import _engines
from test_postgres_holdout034_chain import _initial_state, _loop
from test_postgres_run_end_unavailable import _seed_scored_candidate_without_intent

from lab.api import registry
from lab.director.budget import BudgetSnapshot, EpisodeReservation, RunBudget
from lab.director.contracts import ExperimentDocument, TrajectoryDocument
from lab.director.evaluation_recovery import recover_abandoned_experiment
from lab.director.holdout import read_holdout_bit, register_run_end_intent, reserve_holdout_check
from lab.director.holdout_replay import append_terminal_holdout_suffix
from lab.director.journal import DirectorRunLease, canonical_bytes
from lab.director.ledger import commit_experiment_record, register_experiment, transition_experiment
from lab.director.loop import _budget_to_json, _strategy_checkpoint_fields, _strategy_seed_for_run
from lab.director.ownership import (
    ExecutionOwner,
    OwnerProcessIdentity,
    assert_execution_owner_transaction,
    owned_execution,
    reset_execution_owner,
)
from lab.director.recovery import RecoveryPending, _stored_owner
from lab.director.resume import ResumeReceipt, verified_checkpoint_manifest
from lab.director.stop_closure import reconcile_stopped_baseline
from lab.director.strategy import (
    initial_strategy_state,
    resolve_strategy_selection,
    select_strategy_move,
)
from lab.director.task_plan import (
    RunTaskAssignment,
    plan_run_tasks,
    read_terminal_holdout_checkpoints,
    seal_run_task_plan,
)
from lab.scorer.jobs import _assert_scorer_job_execution, claim_score_job, enqueue_score_job
from lab.scorer.service import IndependentScorer


def _synthetic_takeover(director, lease, fixture, root):
    """Actual Director-role RPC chain; ONLY OS drain assertions are injected fixtures."""
    run_id = fixture["run_id"]
    with director.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT r.payload_sha256,x.execution_sha256,x.started_at,x.deadline_at,"
                    "c.current_generation,g.worker_pid,g.worker_start_ticks,g.worker_boot_id,"
                    "g.worker_unit,g.worker_invocation_id,g.worker_cgroup FROM lab.runs r "
                    "JOIN lab.director_execution_contracts x USING(run_id) "
                    "JOIN lab.director_execution_control c USING(run_id) "
                    "JOIN lab.director_owner_generations g ON g.run_id=c.run_id "
                    "AND g.generation=c.current_generation WHERE r.run_id=:run"
                ),
                {"run": run_id},
            )
            .mappings()
            .one()
        )
    prior = {
        name: row[name]
        for name in (
            "worker_pid",
            "worker_start_ticks",
            "worker_boot_id",
            "worker_unit",
            "worker_invocation_id",
            "worker_cgroup",
        )
    }
    restart_id = uuid4()
    request = {
        "schema": "director-resume-request.v1",
        "run_id": str(run_id),
        "expected_generation": row["current_generation"],
        "payload_sha256": row["payload_sha256"],
        "execution_sha256": row["execution_sha256"],
        "prior_owner": prior,
    }
    args = {
        "id": restart_id,
        "run": run_id,
        "generation": row["current_generation"],
        "payload": row["payload_sha256"],
        "execution": row["execution_sha256"],
        "request": canonical_bytes(request).decode(),
    }
    with director.begin() as connection:
        first = connection.execute(
            text(
                "SELECT lab.begin_director_resume(:id,:run,:generation,:payload,:execution,"
                ":request)"
            ),
            args,
        ).scalar_one()
    with director.begin() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT lab.begin_director_resume(:id,:run,:generation,:payload,"
                    ":execution,:request)"
                ),
                args,
            ).scalar_one()
            == first
        )
    unit = f"swapp-ai-scientist-director-resume-{run_id.hex}-{restart_id.hex}.service"
    process = OwnerProcessIdentity(
        payload_sha256=row["payload_sha256"],
        worker_pid=prior["worker_pid"],
        worker_start_ticks=prior["worker_start_ticks"],
        worker_boot_id=prior["worker_boot_id"],
        worker_unit=unit,
        worker_invocation_id=uuid4().hex,
        worker_cgroup=f"/user.slice/sql-fixture/{unit}",
    )
    # A syntactically valid claimant cannot CAS before the observation exists.
    with director.begin() as connection, pytest.raises(DBAPIError):
        connection.execute(
            text("SELECT lab.claim_resumed_director(:id,:proof,:process)"),
            {
                "id": restart_id,
                "proof": "f" * 64,
                "process": canonical_bytes(asdict(process)).decode(),
            },
        )
    observation = {
        "schema": "director-resume-drain.v1",
        "restart_id": str(restart_id),
        "run_id": str(run_id),
        "generation": row["current_generation"],
        "execution_sha256": row["execution_sha256"],
        "prior_owner": prior,
        "owner_dead": True,
        "children_drained": True,
        "sandbox_drained": True,
        "checkpoints": verified_checkpoint_manifest(director, lease, artifact_root=root),
    }
    with director.begin() as connection:
        digest = connection.execute(
            text("SELECT lab.record_director_resume_drain(:id,:proof)"),
            {"id": restart_id, "proof": canonical_bytes(observation).decode()},
        ).scalar_one()
    claim_args = {
        "id": restart_id,
        "proof": digest,
        "process": canonical_bytes(asdict(process)).decode(),
    }
    with director.begin() as connection:
        claimed = connection.execute(
            text("SELECT lab.claim_resumed_director(:id,:proof,:process)"), claim_args
        ).scalar_one()
    with director.begin() as connection:
        replayed = connection.execute(
            text("SELECT lab.claim_resumed_director(:id,:proof,:process)"), claim_args
        ).scalar_one()
    assert claimed == replayed
    assert claimed["generation"] == row["current_generation"] + 1
    assert datetime.fromisoformat(claimed["started_at"]) == row["started_at"]
    assert datetime.fromisoformat(claimed["deadline_at"]) == row["deadline_at"]
    owner = ExecutionOwner(
        run_id, claimed["generation"], claimed["worker_invocation_id"], claimed["execution_sha256"]
    )
    receipt = ResumeReceipt(
        owner, restart_id, row["started_at"], row["deadline_at"], claimed["closure_only"], digest
    )
    assert (
        receipt.elapsed_wall_seconds
        >= (datetime.now(UTC) - row["started_at"]).total_seconds() - 0.05
    )
    with (
        owned_execution(fixture["owner"]),
        director.begin() as connection,
        pytest.raises(DBAPIError),
    ):
        assert_execution_owner_transaction(connection, run_id=run_id)
    if receipt.closure_only:
        with owned_execution(owner), director.begin() as connection, pytest.raises(DBAPIError):
            assert_execution_owner_transaction(connection, run_id=run_id)
    return receipt


def _ten_synthetic_keeps(migrator, director, planner, scorer, fixture, root):
    """Ten labelled fixture terminal documents, registered/committed as Director."""
    with migrator.connect() as connection:
        baseline = connection.execute(
            text("SELECT experiment_json FROM lab.experiment_records WHERE experiment_id=:id"),
            {"id": fixture["experiment_id"]},
        ).scalar_one()
        trajectory = connection.execute(
            text("SELECT trajectory_json FROM lab.trajectory_records WHERE experiment_id=:id"),
            {"id": fixture["experiment_id"]},
        ).scalar_one()
        planned = (
            connection.execute(
                text(
                    "SELECT task_id,dataset_id,split_id,session_id FROM scorer.run_tasks "
                    "WHERE experiment_id=:id"
                ),
                {"id": fixture["experiment_id"]},
            )
            .mappings()
            .one()
        )
    parent = fixture["experiment_id"]
    for ordinal in range(1, 11):
        candidate = f"exp_{uuid4().hex}"
        hypothesis = "Synthetic SQL KEEP fixture; no model/measurement execution"
        register_experiment(
            director,
            experiment_id=candidate,
            run_id=str(fixture["run_id"]),
            sequence=ordinal,
            experiment_number=ordinal,
            kind="proposal",
            baseline_name=None,
            parent_experiment_id=parent,
            candidate_sha256=fixture["candidate_sha"],
            candidate_blob_sha256=fixture["candidate_blob_sha"],
            inputs_sha256=fixture["inputs_sha"],
            move_type="detector",
            system="S1",
            hypothesis=hypothesis,
            predicted_delta=0.0,
            proposal={"calibration_sha256": fixture["calibration_sha"], "fixture_only": True},
        )
        transition_experiment(director, experiment_id=candidate, status="primary_running")
        plan_run_tasks(
            planner,
            run_id=fixture["run_id"],
            assignments=(
                RunTaskAssignment(
                    experiment_id=candidate,
                    evaluation_kind="primary",
                    task_id=planned["task_id"],
                    seed=0,
                    candidate_sha256=fixture["candidate_sha"],
                    dataset_id=planned["dataset_id"],
                    split_id=planned["split_id"],
                    session_id=planned["session_id"],
                ),
            ),
        )
        candidate_output = (
            b'{"schema":"candidate-scores.v1","sample_indices":[0,1],"scores":[0.1,0.9]}'
        )
        job_id = enqueue_score_job(
            planner,
            run_id=fixture["run_id"],
            experiment_id=candidate,
            evaluation_kind="primary",
            task_id=planned["task_id"],
            seed=0,
            candidate_sha256=fixture["candidate_sha"],
            candidate_output=candidate_output,
            artifact_root=root,
        )
        invocation = uuid4().hex
        claim = claim_score_job(
            scorer,
            job_id=job_id,
            claim_unit=f"swapp-ai-scientist-scorer-{job_id.hex}.service",
            claim_invocation_id=invocation,
        )
        assert claim is not None
        with scorer.begin() as connection:
            asserted = _assert_scorer_job_execution(
                connection,
                job_id=job_id,
                claim_token=claim.claim_token,
                invocation_id=invocation,
            )
            assert asserted == (
                fixture["run_id"],
                claim.admitted_generation,
                claim.execution_sha256,
            )
            connection.execute(
                text(
                    "INSERT INTO scorer.task_scores "
                    "(run_id,experiment_id,evaluation_kind,task_id,seed,score,guard_results,"
                    "score_job_id,claim_token,worker_invocation_id) VALUES "
                    "(:run,:experiment,'primary',:task,0,CAST(:score AS jsonb),"
                    "CAST(:guards AS jsonb),:job,:token,:invocation)"
                ),
                {
                    "run": fixture["run_id"],
                    "experiment": candidate,
                    "task": planned["task_id"],
                    "score": json.dumps(
                        {
                            "candidate_sha256": fixture["candidate_sha"],
                            "candidate_output_sha256": hashlib.sha256(candidate_output).hexdigest(),
                            "vus_pr": 0.5,
                            "vus_roc": 0.5,
                        }
                    ),
                    "guards": json.dumps({"fixture_only": True}),
                    "job": job_id,
                    "token": claim.claim_token,
                    "invocation": invocation,
                },
            )
        decision = {
            "verdict": "KEEP",
            "delta": 0.1,
            "ci_low": 0.1,
            "noise_sd": 0.0,
            "reason": "synthetic_sql_fixture",
        }
        exp = {
            **baseline,
            "experiment_id": candidate,
            "ordinal": ordinal + 1,
            "kind": "proposal",
            "experiment_number": ordinal,
            "baseline_name": None,
            "calibration_sha256": fixture["calibration_sha"],
            "parent_experiment_id": parent,
            "hypothesis": hypothesis,
            "predicted_delta": 0.0,
            "decision": decision,
            "suite_score": 0.5,
        }
        trj = {
            **trajectory,
            "experiment_id": candidate,
            "trajectory_id": f"trj_{uuid4().hex}",
            "kind": "proposal",
            "experiment_number": ordinal,
            "baseline_name": None,
            "calibration_sha256": fixture["calibration_sha"],
            "outcome": decision,
        }
        experiment = ExperimentDocument.model_validate_json(json.dumps(exp), strict=True)
        transcript = TrajectoryDocument.model_validate_json(json.dumps(trj), strict=True)
        from lab.director.artifacts import store_director_artifact

        commit_experiment_record(
            director,
            experiment=experiment,
            trajectory=transcript,
            experiment_blob_sha256=store_director_artifact(
                canonical_bytes(exp), artifact_root=root
            ),
            trajectory_blob_sha256=store_director_artifact(
                canonical_bytes(trj), artifact_root=root
            ),
        )
        parent = candidate
    return parent


def _publish_existing_pass(scorer, fixture, reservation_id):
    owner = fixture["owner"]
    unit = f"swapp-ai-scientist-scorer-{reservation_id.hex}.service"
    params = {
        "id": reservation_id,
        "pid": 31338,
        "ticks": 123456,
        "boot": str(uuid4()),
        "unit": unit,
        "invocation": uuid4().hex,
        "cgroup": f"/user.slice/sql-fixture/{unit}",
        "generation": owner.generation,
        "execution": owner.execution_sha256,
    }
    result = b'{"synthetic_fixture":true}'
    with scorer.begin() as connection:
        connection.execute(
            text(
                "SELECT lab.claim_holdout_reservation(:id,:pid,:ticks,:boot,:unit,"
                ":invocation,:cgroup,:generation,:execution)"
            ),
            params,
        )
        connection.execute(
            text(
                "SELECT lab.publish_holdout_result(:id,0.1,CAST(:result AS jsonb),:sha,"
                ":pid,:ticks,:boot,:unit,:invocation,:cgroup,:generation,:execution)"
            ),
            {**params, "result": result.decode(), "sha": hashlib.sha256(result).hexdigest()},
        )


class _PrefixCrash(RuntimeError):
    pass


@pytest.mark.live
@pytest.mark.parametrize(
    "kind,prefix",
    [
        ("run_end", "none"),
        ("run_end", "application"),
        ("run_end", "state"),
        ("keep_interval", "state"),
    ],
)
def test_expired_historical_positive_suffix_then_seal(tmp_path, monkeypatch, kind, prefix):
    migrator, director, planner, scorer = _engines()
    token = None
    try:
        fixture = _seed_scored_candidate_without_intent(
            migrator,
            director,
            planner,
            scorer,
            tmp_path,
            claim_owner=True,
            wall_seconds=15 if kind == "keep_interval" else 5,
            proposal_limit=10,
        )
        token = fixture["owner_context_token"]
        run_id = fixture["run_id"]
        if kind == "keep_interval":
            fixture["experiment_id"] = _ten_synthetic_keeps(
                migrator, director, planner, scorer, fixture, tmp_path
            )
        reservation = EpisodeReservation(uuid4(), 1, 0)
        snapshot = BudgetSnapshot(reserved_wall_seconds=1.0, reservations=(reservation,))
        with DirectorRunLease(director, run_id) as lease:
            loop = _loop(director, lease, fixture, tmp_path, snapshot)
            loop.budget = RunBudget(
                proposal_limit=10,
                wall_limit=15 if kind == "keep_interval" else 5,
                token_limit=100,
                snapshot=snapshot,
                monotonic=lambda: 0.0,
            )
            state = _initial_state(fixture, BudgetSnapshot()).model_copy(
                update={"proposal_limit": 10}
            )
            if kind == "keep_interval":
                strategy = initial_strategy_state(_strategy_seed_for_run(run_id))
                for ordinal in range(1, 11):
                    strategy, _ = select_strategy_move(strategy, ordinal)
                    strategy = resolve_strategy_selection(strategy, ordinal, "KEEP")
                state = state.model_copy(
                    update={
                        **_strategy_checkpoint_fields(strategy),
                        "completed_proposals": 10,
                        "next_ordinal": 11,
                    }
                ).verify_consistency()
            prior = loop._persist_state(state)
            if kind == "run_end":
                register_run_end_intent(
                    director,
                    lease=lease,
                    loop_result=loop._result(state, prior, "budget_exhausted"),
                    artifact_root=tmp_path,
                )
            index = 10 if kind == "keep_interval" else 1
            lease.append_checkpoint(
                sequence=loop._next_checkpoint_sequence(),
                key=f"holdout-budget-reserved:{reservation.reservation_id}",
                phase="holdout_budget_reserved",
                payload={
                    "run_id": str(run_id),
                    "candidate_experiment_id": fixture["experiment_id"],
                    "reservation_id": str(reservation.reservation_id),
                    "trigger_kind": kind,
                    "trigger_index": index,
                    "wall_seconds": 1,
                    "model_tokens": 0,
                    "admitted_generation": fixture["owner"].generation,
                    "execution_sha256": fixture["owner"].execution_sha256,
                    "budget": _budget_to_json(snapshot),
                },
                artifact_root=tmp_path,
            )
            sql_id = uuid4()
            candidate_key = hashlib.sha256(fixture["experiment_id"].encode()).hexdigest()
            reserve_holdout_check(
                director,
                run_id=run_id,
                candidate_experiment_id=fixture["experiment_id"],
                trigger_kind=kind,
                trigger_index=index,
                request_key=f"{kind}:{index}:{candidate_key}",
                reservation_id=sql_id,
            )
            _publish_existing_pass(scorer, fixture, sql_id)
            receipt = read_holdout_bit(director, run_id=run_id, reservation_id=sql_id)
            assert receipt.state == "passed" and receipt.admitted_generation == 1
            if prefix != "none":
                loop.budget.reconcile_proposal(
                    reservation, measured_wall_seconds=1.0, measured_model_tokens=0
                )
                original = lease.append_checkpoint

                def crash(**kwargs):
                    result = original(**kwargs)
                    if kwargs["phase"] == (
                        "holdout_application" if prefix == "application" else "director_loop_state"
                    ):
                        raise _PrefixCrash()
                    return result

                monkeypatch.setattr(lease, "append_checkpoint", crash)
                with pytest.raises(_PrefixCrash):
                    loop._record_holdout_application(
                        state,
                        prior,
                        receipt,
                        trigger_kind=kind,
                        trigger_index=index,
                        candidate_id=fixture["experiment_id"],
                    )
                monkeypatch.setattr(lease, "append_checkpoint", original)
            with director.connect() as connection:
                remaining = connection.execute(
                    text(
                        "SELECT greatest(0,"
                        "extract(epoch FROM deadline_at-clock_timestamp())) FROM "
                        "lab.director_execution_contracts WHERE run_id=:run"
                    ),
                    {"run": run_id},
                ).scalar_one()
            time.sleep(float(remaining) + 0.03)
            resumed = _synthetic_takeover(director, lease, fixture, tmp_path)
            assert resumed.closure_only
            with owned_execution(resumed.owner):
                final, checkpoint = append_terminal_holdout_suffix(
                    director,
                    lease,
                    receipt=receipt,
                    prior_state_sha256=prior["payload_sha256"],
                    artifact_root=tmp_path,
                )
                again, same = append_terminal_holdout_suffix(
                    director,
                    lease,
                    receipt=receipt,
                    prior_state_sha256=prior["payload_sha256"],
                    artifact_root=tmp_path,
                )
                assert same == checkpoint and again == final
                assert final.budget["wall_seconds"] == 1.0
                assert final.budget["reserved_wall_seconds"] == 0.0
                assert final.holdout_last_status == "passed"
                if kind == "run_end":
                    chain = read_terminal_holdout_checkpoints(
                        director, run_id=run_id, lease=lease, artifact_root=tmp_path
                    )
                    seal_run_task_plan(planner, run_id=run_id, terminal_checkpoints=chain)
                    service = IndependentScorer(
                        scorer, harness_sha256="c" * 64, artifact_root=tmp_path
                    )
                    published = service.finalize_if_ready(
                        run_id=run_id,
                        admitted_generation=resumed.owner.generation,
                        execution_sha256=resumed.owner.execution_sha256,
                    )
                    assert published is not None
                    assert published[0]["admitted_generation"] == 2
                    assert published[0]["receipt_execution_pairs"] == [
                        {
                            "admitted_generation": 1,
                            "execution_sha256": resumed.owner.execution_sha256,
                        }
                    ]
                    assert (
                        service.complete_run(
                            run_id=run_id,
                            admitted_generation=2,
                            execution_sha256=resumed.owner.execution_sha256,
                        )
                        == published
                    )
    finally:
        if token is not None:
            reset_execution_owner(token)
        for engine in (migrator, director, planner, scorer):
            engine.dispose()


@pytest.mark.live
def test_mixed_ancestor_and_current_receipts_require_exact_current_writer(tmp_path):
    migrator, director, planner, scorer = _engines()
    token = None
    try:
        fixture = _seed_scored_candidate_without_intent(
            migrator,
            director,
            planner,
            scorer,
            tmp_path,
            claim_owner=True,
            wall_seconds=120,
            proposal_limit=10,
        )
        token = fixture["owner_context_token"]
        run_id = fixture["run_id"]
        with DirectorRunLease(director, run_id) as lease:
            # The ordinary live path must resolve the six-argument RPC without
            # inventing abandonment when no restart/orphan proof exists.
            assert not recover_abandoned_experiment(
                planner, run_id=run_id, experiment_id=fixture["experiment_id"],
                evidence_engine=director, lease=lease, artifact_root=tmp_path,
            )
            resumed = _synthetic_takeover(director, lease, fixture, tmp_path)
            assert not resumed.closure_only
            with owned_execution(resumed.owner):
                assert not recover_abandoned_experiment(
                    planner, run_id=run_id, experiment_id=fixture["experiment_id"],
                    evidence_engine=director, lease=lease, artifact_root=tmp_path,
                )
                with planner.connect() as connection:
                    assert connection.execute(
                        text(
                            "SELECT count(*) FROM scorer.task_terminal_outcomes WHERE run_id=:run"
                        ),
                        {"run": run_id},
                    ).scalar_one() == 0
                candidate = f"exp_{uuid4().hex}"
                register_experiment(
                    director,
                    experiment_id=candidate,
                    run_id=str(run_id),
                    sequence=1,
                    experiment_number=1,
                    kind="proposal",
                    baseline_name=None,
                    parent_experiment_id=fixture["experiment_id"],
                    candidate_sha256=fixture["candidate_sha"],
                    candidate_blob_sha256=fixture["candidate_blob_sha"],
                    inputs_sha256=fixture["inputs_sha"],
                    move_type="detector",
                    system="S1",
                    hypothesis="synthetic current-generation task",
                    predicted_delta=0.0,
                    proposal={"calibration_sha256": fixture["calibration_sha"]},
                )
                transition_experiment(director, experiment_id=candidate, status="primary_running")
                with planner.connect() as connection:
                    old = (
                        connection.execute(
                            text(
                                "SELECT dataset_id,split_id,session_id,"
                                "task_id FROM scorer.run_tasks WHERE run_id=:run LIMIT 1"
                            ),
                            {"run": run_id},
                        )
                        .mappings()
                        .one()
                    )
                plan_run_tasks(
                    planner,
                    run_id=run_id,
                    assignments=(
                        RunTaskAssignment(
                            experiment_id=candidate,
                            evaluation_kind="primary",
                            task_id=old["task_id"],
                            seed=0,
                            candidate_sha256=fixture["candidate_sha"],
                            dataset_id=old["dataset_id"],
                            split_id=old["split_id"],
                            session_id=old["session_id"],
                        ),
                    ),
                )
                job = enqueue_score_job(
                    planner,
                    run_id=run_id,
                    experiment_id=candidate,
                    evaluation_kind="primary",
                    task_id=old["task_id"],
                    seed=0,
                    candidate_sha256=fixture["candidate_sha"],
                    candidate_output=b'{"schema":"candidate-scores.v1","sample_indices":[0,1],"scores":[0.1,0.9]}',
                    artifact_root=tmp_path,
                )
                invocation = uuid4().hex
                claim = claim_score_job(
                    scorer,
                    job_id=job,
                    claim_unit=f"swapp-ai-scientist-scorer-{job.hex}.service",
                    claim_invocation_id=invocation,
                )
                assert claim is not None
                IndependentScorer(scorer, harness_sha256="c" * 64).record_terminal_outcome(
                    run_id=run_id,
                    experiment_id=candidate,
                    evaluation_kind="primary",
                    task_id=old["task_id"],
                    seed=0,
                    candidate_sha256=fixture["candidate_sha"],
                    outcome_code="scorer_error",
                    score_job_id=job,
                    claim_token=claim.claim_token,
                    worker_invocation_id=invocation,
                    admitted_generation=2,
                    execution_sha256=resumed.owner.execution_sha256,
                )
            args = {"run": run_id, "generation": 2, "sha": resumed.owner.execution_sha256}
            query = text("SELECT lab.assert_scorer_report_receipt_ancestry(:run,:generation,:sha)")
            with scorer.begin() as connection:
                authority = connection.execute(query, args).scalar_one()
            assert authority["receipt_execution_pairs"] == [
                {
                    "admitted_generation": generation,
                    "execution_sha256": resumed.owner.execution_sha256,
                }
                for generation in (1, 2)
            ]
            for changed in (
                {"generation": 1},
                {"generation": 3},
                {"generation": None},
                {"sha": "f" * 64},
                {"sha": None},
            ):
                with scorer.begin() as connection, pytest.raises(DBAPIError):
                    connection.execute(query, {**args, **changed})
            for engine in (migrator, director, planner):
                with engine.begin() as connection, pytest.raises(DBAPIError):
                    connection.execute(query, args)
            # Publication is deliberately not attempted: the new fixture experiment
            # has no terminal document yet, which remains a separate mandatory gate.
    finally:
        if token is not None:
            reset_execution_owner(token)
        for engine in (migrator, director, planner, scorer):
            engine.dispose()


@pytest.mark.live
def test_stopped_first_baseline_closes_orphan_without_new_generation(tmp_path):
    """Synthetic drain authority; real roles close one queued retry and two missing cells."""
    from lab.director.recovery import _ensure_recovery_intent, _request_sha256, _stored_owner
    from lab.scorer.worker import _retry_job

    migrator, director, planner, scorer = _engines()
    token = None
    try:
        fixture = _seed_scored_candidate_without_intent(
            migrator,
            director,
            planner,
            scorer,
            tmp_path,
            claim_owner=True,
            wall_seconds=120,
            stop_before_scoring=True,
        )
        token = fixture["owner_context_token"]
        run_id, experiment_id, job_id = (
            fixture["run_id"],
            fixture["experiment_id"],
            fixture["job_id"],
        )
        plan_run_tasks(
            planner,
            run_id=run_id,
            assignments=tuple(
                RunTaskAssignment(
                    experiment_id=experiment_id,
                    evaluation_kind="baseline",
                    task_id=fixture["task_id"],
                    seed=seed,
                    candidate_sha256=fixture["candidate_sha"],
                    dataset_id=fixture["task_id"],
                    split_id=fixture["split_id"],
                    session_id=fixture["session_id"],
                )
                for seed in (1, 2)
            ),
        )
        claim = claim_score_job(
            scorer,
            job_id=job_id,
            claim_unit=f"swapp-ai-scientist-scorer-{job_id.hex}.service",
            claim_invocation_id=uuid4().hex,
        )
        assert claim is not None and _retry_job(scorer, claim)
        with director.connect() as connection:
            owner_row = (
                connection.execute(
                    text("SELECT * FROM lab.director_run_owners WHERE run_id=:run"), {"run": run_id}
                )
                .mappings()
                .one()
            )
            original = (
                connection.execute(
                    text(
                        "SELECT started_at,deadline_at,execution_json FROM "
                        "lab.director_execution_contracts WHERE run_id=:run"
                    ),
                    {"run": run_id},
                )
                .mappings()
                .one()
            )
        owner = _stored_owner(owner_row)
        with director.begin() as connection:
            principal = (
                connection.execute(
                    text("SELECT owner_id,origin FROM lab.runs WHERE run_id=:run"),
                    {"run": run_id},
                )
                .mappings()
                .one()
            )
            stopped = connection.execute(
                text("SELECT lab.request_director_run_stop(:run,:owner,:origin)"),
                {"run": run_id, "owner": principal["owner_id"], "origin": principal["origin"]},
            ).scalar_one()
            assert stopped["state"] == "stop_requested"
            assert stopped["admitted_generation"] == fixture["owner"].generation
            assert stopped["execution_sha256"] == fixture["owner"].execution_sha256
        recovery_id = uuid4()
        request_sha = _request_sha256(run_id, "stop_and_finalize")
        for recovery in (recovery_id, uuid4()):
            _ensure_recovery_intent(
                director,
                recovery_id=recovery,
                run_id=run_id,
                request_sha256=request_sha,
                observed_state="stop_requested",
                owner=owner,
            )
        with director.begin() as connection:
            first_remaining = connection.execute(
                text("SELECT lab.begin_stopped_baseline_closure(:id)"), {"id": recovery_id}
            ).scalar_one()
        with director.begin() as connection, pytest.raises(DBAPIError):
            connection.execute(
                text("SELECT lab.begin_stopped_baseline_closure(:id)"), {"id": recovery}
            )
        with director.begin() as connection:
            repeated_remaining = connection.execute(
                text("SELECT lab.begin_stopped_baseline_closure(:id)"), {"id": recovery_id}
            ).scalar_one()
        assert 0 < repeated_remaining <= first_remaining <= 120
        with planner.begin() as connection, pytest.raises(DBAPIError):
            connection.execute(
                text("SELECT lab.close_stopped_unattempted_tasks(:id,:exp)"),
                {"id": recovery_id, "exp": experiment_id},
            )
        with scorer.connect() as connection:
            job = connection.execute(
                text("SELECT to_jsonb(j) FROM scorer.score_jobs j WHERE job_id=:id"), {"id": job_id}
            ).scalar_one()
        assert job["error_code"] == "retryable_infrastructure"
        invocation = uuid4().hex
        args = {
            "id": recovery_id,
            "job": job_id,
            "expected": canonical_bytes(job).decode(),
            "invocation": invocation,
        }
        query = text("SELECT lab.reconcile_stopped_score_job(:id,:job,:expected,:invocation)")
        for engine in (director, planner):
            with engine.begin() as connection, pytest.raises(DBAPIError):
                connection.execute(query, args)
        with scorer.begin() as connection, pytest.raises(DBAPIError):
            connection.execute(query, {**args, "invocation": None})
        # Only OS drain is synthetic; the exact immutable job CAS is production SQL.
        with scorer.begin() as connection:
            assert connection.execute(query, args).scalar_one() == "failed"
            assert (
                connection.execute(
                    text("SELECT lab.finish_stopped_children(:id,:invocation)"),
                    {"id": recovery_id, "invocation": invocation},
                ).scalar_one()
                == "drained"
            )
        with planner.begin() as connection:
            for _ in range(2):
                assert connection.execute(
                    text("SELECT lab.close_stopped_unattempted_tasks(:id,:exp)"),
                    {"id": recovery_id, "exp": experiment_id},
                ).scalar_one()
        with director.connect() as connection:
            failures = (
                connection.execute(
                    text(
                        "SELECT original_job FROM lab.director_stop_job_drains "
                        "WHERE recovery_id=:id"
                    ),
                    {"id": recovery_id},
                )
                .scalars()
                .all()
            )
        assert failures == [job]
        # Explicitly synthetic terminal documents compose the real SQL boundary.
        # Production reconstruction from original suite bytes is covered separately.
        from lab.director.artifacts import store_director_artifact
        from lab.director.ledger import canonical_json_bytes

        shared = {
            "run_id": run_id,
            "experiment_id": experiment_id,
            "kind": "baseline",
            "experiment_number": None,
            "baseline_name": "robust_z",
            "calibration_sha256": None,
            "agent_version": "synthetic-stop-sql.v1",
            "inputs_sha256": fixture["inputs_sha"],
            "system": "S1",
        }
        execution = original["execution_json"]
        experiment = ExperimentDocument.model_validate(
            {
                **shared,
                "schema": "experiment.v1",
                "ordinal": 1,
                "parent_experiment_id": None,
                "candidate_sha256": fixture["candidate_sha"],
                "candidate_blob_sha256": fixture["candidate_blob_sha"],
                "move_type": "detector",
                "hypothesis": "synthetic SQL fixture baseline",
                "predicted_delta": None,
                "parent_tree": "e" * 40,
                "child_tree": "e" * 40,
                "harness_sha256": execution["harness_sha256"],
                "image_sha256": execution["image_sha256"],
                "suite_id": fixture["suite_id"],
                "suite_version": 3,
                "per_task": (),
                "suite_score": None,
                "guards": {"fixture_only": "not_run"},
                "decision": None,
                "status": "abandoned",
                "fit_seconds": None,
                "score_seconds": None,
                "llm_input_tokens": 0,
                "llm_output_tokens": 0,
                "wall_seconds": None,
            },
            strict=True,
        )
        trajectory = TrajectoryDocument.model_validate(
            {
                **shared,
                "schema": "trajectory.v1",
                "trajectory_id": f"trj_{uuid4().hex}",
                "model_id": "baseline/no-llm.synthetic-fixture",
                "usage_profile": "noncommercial_research",
                "source_provenance": (),
                "quantization": "none",
                "adapter": "none",
                "thinking": False,
                "temperature": 0.0,
                "top_p": 1.0,
                "context_template": "synthetic.stop.sql.v1",
                "messages_blob_sha256": store_director_artifact(b"[]", artifact_root=tmp_path),
                "tool_calls": 0,
                "outcome": None,
                "quality_tier": "bronze",
                "secrets_scrubbed": False,
                "people_scrubbed": False,
                "raw_values_scrubbed": False,
                "exclusions": (
                    "Synthetic SQL documents; no model, measurements or OS drain proof",
                ),
            },
            strict=True,
        )
        commit_experiment_record(
            director,
            experiment=experiment,
            trajectory=trajectory,
            experiment_blob_sha256=store_director_artifact(
                canonical_json_bytes(experiment), artifact_root=tmp_path
            ),
            trajectory_blob_sha256=store_director_artifact(
                canonical_json_bytes(trajectory), artifact_root=tmp_path
            ),
        )
        seal_run_task_plan(planner, run_id=run_id)
        service = IndependentScorer(
            scorer, harness_sha256=execution["harness_sha256"], artifact_root=tmp_path
        )
        published = service.finalize_if_ready(
            run_id=run_id, admitted_generation=1, execution_sha256=fixture["owner"].execution_sha256
        )
        assert published is not None and published[0]["status"] == "stopped"
        assert (
            service.complete_run(
                run_id=run_id,
                admitted_generation=1,
                execution_sha256=fixture["owner"].execution_sha256,
            )
            == published
        )
        with migrator.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT current_generation FROM lab.director_execution_control "
                        "WHERE run_id=:run"
                    ),
                    {"run": run_id},
                ).scalar_one()
                == 1
            )
            assert connection.execute(
                text(
                    "SELECT started_at,deadline_at FROM lab.director_execution_contracts "
                    "WHERE run_id=:run"
                ),
                {"run": run_id},
            ).one() == (original["started_at"], original["deadline_at"])
            assert (
                connection.execute(
                    text("SELECT count(*) FROM scorer.task_scores WHERE run_id=:run"),
                    {"run": run_id},
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM scorer.task_terminal_outcomes WHERE run_id=:run "
                        "AND outcome_code='infrastructure_unattempted'"
                    ),
                    {"run": run_id},
                ).scalar_one()
                == 2
            )
    finally:
        if token is not None:
            reset_execution_owner(token)
        for engine in (migrator, director, planner, scorer):
            engine.dispose()


class _ReachedRegistryGuard(RuntimeError):
    pass


@pytest.mark.live
@pytest.mark.parametrize("calibrated", [False, True])
def test_actual_stopped_preflight_uses_director_receipt(tmp_path, monkeypatch, calibrated):
    migrator, director, planner, scorer = _engines()
    token = None
    try:
        fixture = _seed_scored_candidate_without_intent(
            migrator,
            director,
            planner,
            scorer,
            tmp_path,
            claim_owner=True,
            wall_seconds=120,
            stop_before_scoring=True,
        )
        token = fixture["owner_context_token"]
        run_id, recovery_id = fixture["run_id"], uuid4()
        with director.connect() as connection:
            assert (
                connection.execute(text("SELECT session_user")).scalar_one() == "swapp_lab_director"
            )
            owner_row = (
                connection.execute(
                    text("SELECT * FROM lab.director_run_owners WHERE run_id=:run"),
                    {"run": run_id},
                )
                .mappings()
                .one()
            )
            principal = (
                connection.execute(
                    text("SELECT owner_id,origin FROM lab.runs WHERE run_id=:run"),
                    {"run": run_id},
                )
                .mappings()
                .one()
            )
        owner = _stored_owner(owner_row)
        assert owner is not None
        # Verify the missing permission remains missing; no widened grants mask this regression.
        with director.connect() as connection, pytest.raises(DBAPIError) as denied:
            connection.execute(
                text("SELECT 1 FROM lab.baseline_calibrations WHERE run_id=:run"),
                {"run": run_id},
            )
        assert denied.value.orig.sqlstate == "42501"
        if calibrated:
            # Deliberately synthetic calibration receipt; no fabricated model measurement.
            with migrator.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO lab.baseline_calibrations "
                        "(run_id,suite_id,suite_version,calibration_sha256,blob_sha256,task_count) "
                        "VALUES (:run,:suite,3,:digest,:digest,1)"
                    ),
                    {"run": run_id, "suite": fixture["suite_id"], "digest": "f" * 64},
                )
        with director.begin() as connection:
            stopped = connection.execute(
                text("SELECT lab.request_director_run_stop(:run,:owner,:origin)"),
                {"run": run_id, "owner": principal["owner_id"], "origin": principal["origin"]},
            ).scalar_one()
            assert stopped["state"] == "stop_requested"
        registry_reads = []

        def reach_registry(*args, **kwargs):
            registry_reads.append(True)
            raise _ReachedRegistryGuard("pre-window registry boundary")

        monkeypatch.setenv("LAB_SUITE_REGISTRY_FILE", str(tmp_path / "unused-registry.json"))
        monkeypatch.setattr(registry, "load_suite_registry", reach_registry)
        error = RecoveryPending if calibrated else _ReachedRegistryGuard
        match = "one unscored interrupted baseline" if calibrated else "pre-window registry"
        with DirectorRunLease(director, run_id) as lease, pytest.raises(error, match=match):
            reconcile_stopped_baseline(
                director,
                planner,
                run_id=run_id,
                recovery_id=recovery_id,
                owner=owner,
                execution_owner=fixture["owner"],
                lease=lease,
                artifact_root=tmp_path,
            )
        assert registry_reads == ([] if calibrated else [True])
        with director.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT count(*) FROM lab.director_stop_closures WHERE run_id=:run"),
                    {"run": run_id},
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    text("SELECT state FROM lab.runs WHERE run_id=:run"),
                    {"run": run_id},
                ).scalar_one()
                == "stop_requested"
            )
    finally:
        if token is not None:
            reset_execution_owner(token)
        for engine in (migrator, director, planner, scorer):
            engine.dispose()


def _calibrated_stop_proposal055(migrator, director, planner, scorer, root):
    """Synthetic nine-cell component fixture; no candidate/OS execution evidence."""
    from lab.director.artifacts import store_director_artifact
    from lab.director.ledger import canonical_json_bytes

    f = _seed_scored_candidate_without_intent(
        migrator,
        director,
        planner,
        scorer,
        root,
        claim_owner=True,
        wall_seconds=300,
        stop_before_scoring=True,
    )
    run = f["run_id"]
    with director.connect() as connection:
        execution = connection.execute(
            text("SELECT execution_json FROM lab.director_execution_contracts WHERE run_id=:run"),
            {"run": run},
        ).scalar_one()
    output = b'{"schema":"candidate-scores.v1","sample_indices":[0,1],"scores":[0.1,0.9]}'
    for index, name in enumerate(("robust_z", "iforest", "ecod_train_frozen")):
        exp = f["experiment_id"] if index == 0 else "exp_" + uuid4().hex
        if index:
            register_experiment(
                director,
                experiment_id=exp,
                run_id=str(run),
                sequence=index,
                experiment_number=None,
                kind="baseline",
                baseline_name=name,
                parent_experiment_id=None,
                candidate_sha256=f["candidate_sha"],
                candidate_blob_sha256=f["candidate_blob_sha"],
                inputs_sha256=f["inputs_sha"],
                move_type="detector",
                system="S1",
                hypothesis="SQL fixture baseline",
                predicted_delta=None,
                proposal={"schema": "baseline.registration.v1"},
            )
            transition_experiment(director, experiment_id=exp, status="primary_running")
        for seed in range(3):
            if index == seed == 0:
                job = f["job_id"]
            else:
                plan_run_tasks(
                    planner,
                    run_id=run,
                    assignments=(
                        RunTaskAssignment(
                            experiment_id=exp,
                            evaluation_kind="baseline",
                            task_id=f["task_id"],
                            seed=seed,
                            candidate_sha256=f["candidate_sha"],
                            dataset_id=f["task_id"],
                            split_id=f["split_id"],
                            session_id=f["session_id"],
                        ),
                    ),
                )
                job = enqueue_score_job(
                    planner,
                    run_id=run,
                    experiment_id=exp,
                    evaluation_kind="baseline",
                    task_id=f["task_id"],
                    seed=seed,
                    candidate_sha256=f["candidate_sha"],
                    candidate_output=output,
                    artifact_root=root,
                )
            invocation = uuid4().hex
            claim = claim_score_job(
                scorer,
                job_id=job,
                claim_unit=f"swapp-ai-scientist-scorer-{job.hex}.service",
                claim_invocation_id=invocation,
            )
            assert claim is not None
            with scorer.begin() as connection:
                _assert_scorer_job_execution(
                    connection, job_id=job, claim_token=claim.claim_token, invocation_id=invocation
                )
                connection.execute(
                    text(
                        "INSERT INTO scorer.task_scores(run_id,ex"
                        "periment_id,evaluation_kind,task_id,seed,"
                        "score,guard_results,score_job_id,claim_token,worker_invocation_id) VALUES "
                        "(:run,:exp,'baseline',:task,:seed,CAST(:sc"
                        "ore AS jsonb),'{}'::jsonb,:job,:token,:inv)"
                    ),
                    dict(
                        run=run,
                        exp=exp,
                        task=f["task_id"],
                        seed=seed,
                        job=job,
                        token=claim.claim_token,
                        inv=invocation,
                        score=json.dumps(
                            {
                                "candidate_sha256": f["candidate_sha"],
                                "candidate_output_sha256": hashlib.sha256(output).hexdigest(),
                                "vus_pr": 0.5,
                                "vus_roc": 0.5,
                            }
                        ),
                    ),
                )
        shared = dict(
            run_id=run,
            experiment_id=exp,
            kind="baseline",
            experiment_number=None,
            baseline_name=name,
            calibration_sha256=None,
            agent_version="synthetic055",
            inputs_sha256=f["inputs_sha"],
        )
        experiment = ExperimentDocument.model_validate(
            {
                **shared,
                "schema": "experiment.v1",
                "ordinal": index + 1,
                "parent_experiment_id": None,
                "candidate_sha256": f["candidate_sha"],
                "candidate_blob_sha256": f["candidate_blob_sha"],
                "move_type": "detector",
                "system": "S1",
                "hypothesis": "SQL fixture baseline" if index else "synthetic SQL fixture baseline",
                "predicted_delta": None,
                "parent_tree": "a" * 40,
                "child_tree": "a" * 40,
                "harness_sha256": execution["harness_sha256"],
                "image_sha256": execution["image_sha256"],
                "suite_id": f["suite_id"],
                "suite_version": 3,
                "per_task": (),
                "suite_score": 0.5,
                "guards": {},
                "decision": None,
                "status": "scored",
                "fit_seconds": 0.0,
                "score_seconds": 0.0,
                "llm_input_tokens": 0,
                "llm_output_tokens": 0,
                "wall_seconds": 0.0,
            },
            strict=True,
        )
        trajectory = TrajectoryDocument.model_validate(
            {
                **shared,
                "schema": "trajectory.v1",
                "trajectory_id": "trj_" + uuid4().hex,
                "model_id": "baseline/synthetic",
                "usage_profile": "noncommercial_research",
                "source_provenance": (),
                "quantization": "none",
                "adapter": "none",
                "system": "S1",
                "thinking": False,
                "temperature": 0.0,
                "top_p": 1.0,
                "context_template": "synthetic",
                "messages_blob_sha256": store_director_artifact(b"[]", artifact_root=root),
                "tool_calls": 0,
                "outcome": None,
                "quality_tier": "bronze",
                "secrets_scrubbed": False,
                "people_scrubbed": False,
                "raw_values_scrubbed": False,
                "exclusions": ("Synthetic SQL fixture",),
            },
            strict=True,
        )
        commit_experiment_record(
            director,
            experiment=experiment,
            trajectory=trajectory,
            experiment_blob_sha256=store_director_artifact(
                canonical_json_bytes(experiment), artifact_root=root
            ),
            trajectory_blob_sha256=store_director_artifact(
                canonical_json_bytes(trajectory), artifact_root=root
            ),
        )
    calibration_sha = hashlib.sha256(b"synthetic complete nine-cell calibration055").hexdigest()
    with director.begin() as connection:
        assert_execution_owner_transaction(connection, run_id=run)
        connection.execute(
            text("SELECT lab.register_baseline_calibration(:run,:suite,3,:sha,:sha,1)"),
            dict(run=run, suite=f["suite_id"], sha=calibration_sha),
        )
    f["calibration_sha"] = calibration_sha
    f["execution"] = execution
    return f


@pytest.mark.live
@pytest.mark.parametrize(
    "negative", [None, "stale_owner", "changed_checkpoint", "planned_cell", "expired_window"]
)
def test_stopped_proposal055_preserves_calibrated_ancestors(tmp_path, negative):
    """Real roles/commit/seal/report; injected SQL drain is NOT host death proof."""
    from lab.director.artifacts import store_director_artifact
    from lab.director.contracts import InfrastructureStopReceipt
    from lab.director.ledger import canonical_json_bytes
    from lab.director.recovery import _current_owner_row, _ensure_recovery_intent, _request_sha256
    from tests.test_stopped_proposal055 import documents_fixture

    migrator, director, planner, scorer = _engines()
    token = None
    try:
        f = _calibrated_stop_proposal055(migrator, director, planner, scorer, tmp_path)
        token = f["owner_context_token"]
        run = f["run_id"]
        args = documents_fixture(tmp_path)
        row, payload = args["row"], dict(args["payload"])
        payload.update(
            run_id=str(run),
            calibration_sha256=f["calibration_sha"],
            suite_id=f["suite_id"],
            suite_version=3,
            harness_sha256=f["execution"]["harness_sha256"],
            image_sha256=f["execution"]["image_sha256"],
            parent_experiment_id=f["experiment_id"],
        )
        exp = row["experiment_id"]
        register_experiment(
            director,
            experiment_id=exp,
            run_id=str(run),
            sequence=3,
            experiment_number=1,
            kind="proposal",
            baseline_name=None,
            parent_experiment_id=f["experiment_id"],
            candidate_sha256=payload["candidate_sha256"],
            candidate_blob_sha256=payload["candidate_blob_sha256"],
            inputs_sha256=payload["inputs_sha256"],
            move_type="hparam",
            system="S1",
            hypothesis=payload["proposal"]["hypothesis"],
            predicted_delta=payload["proposal"]["predicted_delta"],
            proposal={**payload["proposal"], "calibration_sha256": f["calibration_sha"]},
        )
        with DirectorRunLease(director, run) as lease:
            reservation = lease.append_checkpoint(
                sequence=0,
                key=f"proposal-budget-reservation:{run}:1",
                phase="proposal_budget_reserved",
                payload={"fixture_original_charge": 24.8369715},
                artifact_root=tmp_path,
            )
            proposal = lease.append_checkpoint(
                sequence=1,
                key="proposal:1",
                phase="proposal_registered",
                payload=payload,
                artifact_root=tmp_path,
            )
            reconciled = lease.append_checkpoint(
                sequence=2,
                key=f"proposal-budget-reconciled:{run}:1",
                phase="proposal_budget_reconciled",
                payload={"status": "episode_budget_overrun", "measured_wall_seconds": 24.8369715},
                artifact_root=tmp_path,
            )
            resumed = _synthetic_takeover(director, lease, f, tmp_path)
        owner = resumed.owner
        recovery_id = uuid4()
        with director.connect() as connection:
            current = _current_owner_row(connection, run)
            principal = (
                connection.execute(
                    text("SELECT owner_id,origin FROM lab.runs WHERE run_id=:run"), {"run": run}
                )
                .mappings()
                .one()
            )
        assert current["generation"] == 2
        process = _stored_owner(current)
        assert process is not None
        if negative == "stale_owner":
            with director.connect() as connection:
                process = _stored_owner(
                    connection.execute(
                        text("SELECT * FROM lab.director_run_owners WHERE run_id=:run"),
                        {"run": run},
                    )
                    .mappings()
                    .one()
                )
        if negative == "planned_cell":
            with owned_execution(owner):
                plan_run_tasks(
                    planner,
                    run_id=run,
                    assignments=(
                        RunTaskAssignment(
                            experiment_id=exp,
                            evaluation_kind="primary",
                            task_id=f["task_id"],
                            seed=0,
                            candidate_sha256=payload["candidate_sha256"],
                            dataset_id=f["task_id"],
                            split_id=f["split_id"],
                            session_id=f["session_id"],
                        ),
                    ),
                )
        with director.begin() as connection:
            connection.execute(
                text("SELECT lab.request_director_run_stop(:run,:owner,:origin)"),
                dict(run=run, owner=principal["owner_id"], origin=principal["origin"]),
            )
        _ensure_recovery_intent(
            director,
            recovery_id=recovery_id,
            run_id=run,
            request_sha256=_request_sha256(run, "stop_and_finalize"),
            observed_state="stop_requested",
            owner=process,
        )
        proposal_text = canonical_bytes(payload).decode() + (
            " " if negative == "changed_checkpoint" else ""
        )
        if negative in {"stale_owner", "changed_checkpoint", "planned_cell"}:
            with director.begin() as connection, pytest.raises(DBAPIError):
                connection.execute(
                    text("SELECT lab.begin_stopped_proposal_closure(:id,:proposal)"),
                    {"id": recovery_id, "proposal": proposal_text},
                )
            with migrator.connect() as connection:
                assert (
                    connection.execute(
                        text("SELECT count(*) FROM lab.director_stop_closures WHERE run_id=:run"),
                        {"run": run},
                    ).scalar_one()
                    == 0
                )
            return
        with migrator.connect() as connection:
            baseline_rows = (
                connection.execute(
                    text(
                        "SELECT status FROM lab.experiments WHERE run_id=:run AND kind='baseline'"
                    ),
                    {"run": run},
                )
                .scalars()
                .all()
            )
            job_shapes = (
                connection.execute(
                    text(
                        "SELECT j.state,j.evaluation_kind,j.admitted_generation,"
                        "j.execution_sha256=:execution AS exact_execution,"
                        "lab.valid_director_restart_ancestry(:run,j.admitted_generation,2,"
                        ":execution) "
                        "AS valid_ancestor,s.claim_token IS NULL AS score_claim_cleared,"
                        "s.worker_invocation_id ~ '^[0-9a-f]{32}$' AS invocation,"
                        "c.completion_kind FROM scorer.score_jobs j "
                        "LEFT JOIN scorer.task_scores s ON s.score_job_id=j.job_id "
                        "LEFT JOIN scorer.task_completions c ON c.run_id=j.run_id "
                        "AND c.experiment_id=j.experiment_id "
                        "AND c.evaluation_kind=j.evaluation_kind "
                        "AND c.task_id=j.task_id AND c.seed=j.seed WHERE j.run_id=:run"
                    ),
                    {"run": run, "execution": owner.execution_sha256},
                )
                .mappings()
                .all()
            )
        assert baseline_rows == ["scored"] * 3
        assert len(job_shapes) == 9
        assert all(
            j["state"] == "completed"
            and j["evaluation_kind"] == "baseline"
            and j["exact_execution"]
            and j["valid_ancestor"]
            and j["score_claim_cleared"]
            and j["invocation"]
            and j["completion_kind"] == "scored"
            for j in job_shapes
        ), job_shapes
        with director.begin() as connection:
            first = connection.execute(
                text("SELECT lab.begin_stopped_proposal_closure(:id,:proposal)"),
                {"id": recovery_id, "proposal": proposal_text},
            ).scalar_one()
            second = connection.execute(
                text("SELECT lab.begin_stopped_proposal_closure(:id,:proposal)"),
                {"id": recovery_id, "proposal": proposal_text},
            ).scalar_one()
            assert 0 < second <= first <= 120
        if negative == "expired_window":
            with scorer.begin() as connection:
                connection.execute(
                    text("SELECT lab.finish_stopped_proposal_children(:id,:inv)"),
                    {"id": recovery_id, "inv": uuid4().hex},
                )
            # Synthetic elapsed-time fixture in this disposable DB; production rows are immutable.
            with migrator.begin() as connection:
                expired_at = connection.execute(
                    text(
                        "UPDATE lab.director_stop_closures SET created_at="
                        "clock_timestamp()-interval '121 seconds' WHERE recovery_id=:id "
                        "RETURNING created_at"
                    ),
                    {"id": recovery_id},
                ).scalar_one()
            with director.begin() as connection:
                assert (
                    connection.execute(
                        text("SELECT lab.begin_stopped_proposal_closure(:id,:proposal)"),
                        {"id": recovery_id, "proposal": proposal_text},
                    ).scalar_one()
                    == 0
                )
            for statement, params in (
                (
                    "SELECT lab.stopped_proposal_job_receipt(:id,:job)",
                    {"id": recovery_id, "job": f["job_id"]},
                ),
                (
                    "SELECT lab.finish_stopped_proposal_children(:id,:inv)",
                    {"id": recovery_id, "inv": uuid4().hex},
                ),
            ):
                with scorer.begin() as connection, pytest.raises(DBAPIError, match="expired"):
                    connection.execute(text(statement), params)
            expiry_stop = InfrastructureStopReceipt(
                reason="stopped_before_candidate_admission",
                recovery_id=recovery_id,
                run_id=run,
                experiment_id=exp,
                generation=2,
                execution_sha256=owner.execution_sha256,
                proposal_sha256=proposal["payload_sha256"],
                reservation_sha256=reservation["payload_sha256"],
                reconciled_sha256=reconciled["payload_sha256"],
                calibration_sha256=f["calibration_sha"],
            )
            from lab.director.stopped_proposal import _proposal_documents

            stopped_experiment, stopped_trajectory = _proposal_documents(**args)
            shared_updates = dict(
                run_id=run, calibration_sha256=f["calibration_sha"], infrastructure_stop=expiry_stop
            )
            stopped_experiment = stopped_experiment.model_copy(
                update={
                    **shared_updates,
                    "parent_experiment_id": f["experiment_id"],
                    "harness_sha256": f["execution"]["harness_sha256"],
                    "image_sha256": f["execution"]["image_sha256"],
                    "suite_id": f["suite_id"],
                    "suite_version": 3,
                }
            )
            stopped_trajectory = stopped_trajectory.model_copy(update=shared_updates)
            with owned_execution(owner), pytest.raises(DBAPIError, match="expired"):
                commit_experiment_record(
                    director,
                    experiment=stopped_experiment,
                    trajectory=stopped_trajectory,
                    experiment_blob_sha256=store_director_artifact(
                        canonical_json_bytes(stopped_experiment), artifact_root=tmp_path
                    ),
                    trajectory_blob_sha256=store_director_artifact(
                        canonical_json_bytes(stopped_trajectory), artifact_root=tmp_path
                    ),
                )
            with migrator.connect() as connection:
                assert (
                    connection.execute(
                        text(
                            "SELECT created_at FROM lab.director_stop_closures "
                            "WHERE recovery_id=:id"
                        ),
                        {"id": recovery_id},
                    ).scalar_one()
                    == expired_at
                )
                for table, params, condition, expected in (
                    ("lab.experiment_records", {"exp": exp}, "experiment_id=:exp", 0),
                    ("lab.trajectory_records", {"exp": exp}, "experiment_id=:exp", 0),
                    ("lab.reports", {"run": run}, "run_id=:run", 0),
                    ("scorer.task_scores", {"run": run}, "run_id=:run", 9),
                ):
                    assert (
                        connection.execute(
                            text(f"SELECT count(*) FROM {table} WHERE {condition}"), params
                        ).scalar_one()
                        == expected
                    )
            return
        other_recovery = uuid4()
        _ensure_recovery_intent(
            director,
            recovery_id=other_recovery,
            run_id=run,
            request_sha256=_request_sha256(run, "stop_and_finalize"),
            observed_state="stop_requested",
            owner=process,
        )
        with director.begin() as connection, pytest.raises(DBAPIError):
            connection.execute(
                text("SELECT lab.begin_stopped_proposal_closure(:id,:proposal)"),
                {"id": other_recovery, "proposal": proposal_text},
            )
        with planner.begin() as connection, pytest.raises(DBAPIError):
            connection.execute(
                text("SELECT lab.begin_stopped_proposal_closure(:id,:proposal)"),
                {"id": recovery_id, "proposal": proposal_text},
            )
        with scorer.begin() as connection:
            jobs = (
                connection.execute(
                    text("SELECT job_id FROM scorer.score_jobs WHERE run_id=:run"), {"run": run}
                )
                .scalars()
                .all()
            )
            assert len(jobs) == 9
            for job in jobs:
                historical = connection.execute(
                    text("SELECT lab.stopped_proposal_job_receipt(:stop,:job)"),
                    {"stop": recovery_id, "job": job},
                ).scalar_one()
                assert historical["state"] == "completed"
                assert historical["admitted_generation"] == 1
                assert len(historical["claim_invocation_id"]) == 32
            assert (
                connection.execute(
                    text("SELECT lab.finish_stopped_proposal_children(:id,:inv)"),
                    {"id": recovery_id, "inv": uuid4().hex},
                ).scalar_one()
                == "drained"
            )
        stop = InfrastructureStopReceipt(
            reason="stopped_before_candidate_admission",
            recovery_id=recovery_id,
            run_id=run,
            experiment_id=exp,
            generation=2,
            execution_sha256=owner.execution_sha256,
            proposal_sha256=proposal["payload_sha256"],
            reservation_sha256=reservation["payload_sha256"],
            reconciled_sha256=reconciled["payload_sha256"],
            calibration_sha256=f["calibration_sha"],
        )
        # CPU tests validate production builder; this typed component pair exercises strict SQL.
        experiment, trajectory = __import__(
            "lab.director.stopped_proposal", fromlist=["_proposal_documents"]
        )._proposal_documents(**args)
        updates = dict(
            run_id=run, calibration_sha256=f["calibration_sha"], infrastructure_stop=stop
        )
        experiment = experiment.model_copy(
            update={
                **updates,
                "parent_experiment_id": f["experiment_id"],
                "harness_sha256": f["execution"]["harness_sha256"],
                "image_sha256": f["execution"]["image_sha256"],
                "suite_id": f["suite_id"],
                "suite_version": 3,
            }
        )
        trajectory = trajectory.model_copy(update=updates)
        with owned_execution(owner):
            commit_experiment_record(
                director,
                experiment=experiment,
                trajectory=trajectory,
                experiment_blob_sha256=store_director_artifact(
                    canonical_json_bytes(experiment), artifact_root=tmp_path
                ),
                trajectory_blob_sha256=store_director_artifact(
                    canonical_json_bytes(trajectory), artifact_root=tmp_path
                ),
            )
            seal_run_task_plan(planner, run_id=run)
        service = IndependentScorer(
            scorer, artifact_root=tmp_path, harness_sha256=f["execution"]["harness_sha256"]
        )
        report = service.complete_run(
            run_id=run, admitted_generation=2, execution_sha256=owner.execution_sha256
        )
        assert report is not None and report[0]["status"] == "stopped"
        assert report[0]["receipt_execution_pairs"] == [
            {"admitted_generation": 1, "execution_sha256": owner.execution_sha256}
        ]
        assert (
            service.complete_run(
                run_id=run, admitted_generation=2, execution_sha256=owner.execution_sha256
            )
            == report
        )
        with migrator.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT count(*) FROM scorer.task_scores WHERE run_id=:run"), {"run": run}
                ).scalar_one()
                == 9
            )
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM scorer.score_jobs "
                        "WHERE run_id=:run AND experiment_id=:exp"
                    ),
                    {"run": run, "exp": exp},
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    text(
                        "SELECT calibration_sha256 FROM lab.baseline_calibrations WHERE run_id=:run"
                    ),
                    {"run": run},
                ).scalar_one()
                == f["calibration_sha"]
            )
    finally:
        if token is not None:
            reset_execution_owner(token)
        for engine in (migrator, director, planner, scorer):
            engine.dispose()
