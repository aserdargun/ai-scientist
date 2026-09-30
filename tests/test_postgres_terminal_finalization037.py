"""Opt-in role-separated terminal closure on explicitly synthetic score fixtures.

No model, worker, sandbox or service runs. Positive score bytes are synthetic;
production Scorer claim/publication and Director checkpoint RPCs persist them
before the immutable research deadline expires.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import replace
from uuid import uuid4

import pytest
import test_postgres_holdout034_chain as chain
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_postgres_holdout030_recovery import _engines
from test_postgres_run_end_unavailable import _seed_scored_candidate_without_intent

from lab.director.budget import BudgetSnapshot
from lab.director.holdout import read_holdout_bit, register_run_end_intent, reserve_holdout_check
from lab.director.journal import DirectorRunLease
from lab.director.ownership import active_execution_owner, owned_execution, reset_execution_owner
from lab.director.task_plan import (
    read_terminal_holdout_checkpoints,
    seal_run_task_plan,
    terminal_finalization_seconds,
)
from lab.scorer.service import IndependentScorer


def _seal_and_publish(migrator, director, planner, scorer, lease, run_id, root):
    owner = active_execution_owner()
    assert owner is not None
    checkpoints = read_terminal_holdout_checkpoints(
        director, run_id=run_id, lease=lease, artifact_root=root
    )
    for wrong in (
        replace(owner, generation=owner.generation + 1),
        replace(owner, execution_sha256="f" * 64),
    ):
        with owned_execution(wrong), pytest.raises(DBAPIError):
            seal_run_task_plan(planner, run_id=run_id, terminal_checkpoints=checkpoints)
    bad = ({**checkpoints[0], "state": "changed"}, *checkpoints[1:])
    with pytest.raises(DBAPIError):
        seal_run_task_plan(planner, run_id=run_id, terminal_checkpoints=bad)
    seal = seal_run_task_plan(planner, run_id=run_id, terminal_checkpoints=checkpoints)
    with migrator.connect() as connection:
        original = connection.execute(
            text("SELECT started_at,deadline_at FROM lab.terminal_finalizations WHERE run_id=:run"),
            {"run": run_id},
        ).one()
    assert 0 < terminal_finalization_seconds(planner, run_id=run_id) <= 60
    assert seal_run_task_plan(planner, run_id=run_id, terminal_checkpoints=checkpoints) == seal
    with scorer.begin() as connection:
        with pytest.raises(DBAPIError):
            connection.execute(
                text("SELECT lab.assert_scorer_terminal_finalization(:run,NULL,:sha)"),
                {"run": run_id, "sha": owner.execution_sha256},
            )
    service = IndependentScorer(scorer, harness_sha256="c" * 64, artifact_root=root)
    report = service.finalize_if_ready(
        run_id=run_id,
        admitted_generation=owner.generation,
        execution_sha256=owner.execution_sha256,
    )
    assert report is not None
    assert (
        service.finalize_if_ready(
            run_id=run_id,
            admitted_generation=owner.generation,
            execution_sha256=owner.execution_sha256,
        )
        == report
    )
    with migrator.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT started_at,deadline_at FROM lab.terminal_finalizations "
                    "WHERE run_id=:run"
                ),
                {"run": run_id},
            ).one()
            == original
        )
        assert connection.execute(
            text(
                "SELECT deadline_at < clock_timestamp() FROM lab.director_execution_contracts "
                "WHERE run_id=:run"
            ),
            {"run": run_id},
        ).scalar_one()


@pytest.mark.live
def test_expired_failed_chain_seals_and_publishes(tmp_path, monkeypatch):
    engines = _engines()
    migrator, director, planner, scorer = engines
    finalized = []
    original_exit = DirectorRunLease.__exit__

    def close(lease, exc_type, exc, traceback):
        try:
            if (
                exc_type is None
                and "append_holdout_closure_checkpoint" not in lease.__dict__
                and lease.read_checkpoint(
                    key="holdout-state-application:run_end:1", artifact_root=tmp_path
                )
            ):
                _seal_and_publish(
                    migrator, director, planner, scorer, lease, lease.run_id, tmp_path
                )
                finalized.append(lease.run_id)
        finally:
            original_exit(lease, exc_type, exc, traceback)

    monkeypatch.setattr(chain, "_engines", lambda: engines)
    monkeypatch.setattr(DirectorRunLease, "__exit__", close)
    chain.test_expired_run_end_prefix_replays_full_production_chain(
        tmp_path, "failed_marker", monkeypatch
    )
    assert len(finalized) == 1


@pytest.mark.live
def test_already_recorded_passed_chain_finalizes_after_expiry(tmp_path):
    migrator, director, planner, scorer = _engines()
    token = None
    try:
        fixture = _seed_scored_candidate_without_intent(
            migrator, director, planner, scorer, tmp_path, claim_owner=True, wall_seconds=3
        )
        token = fixture["owner_context_token"]
        owner, run_id = fixture["owner"], fixture["run_id"]
        with DirectorRunLease(director, run_id) as lease:
            loop = chain._loop(director, lease, fixture, tmp_path, BudgetSnapshot())
            state = chain._initial_state(fixture, BudgetSnapshot())
            prior = loop._persist_state(state)
            result = loop._result(state, prior, "budget_exhausted")
            register_run_end_intent(
                director, lease=lease, loop_result=result, artifact_root=tmp_path
            )
            rid = uuid4()
            reserve_holdout_check(
                director,
                run_id=run_id,
                candidate_experiment_id=fixture["experiment_id"],
                trigger_kind="run_end",
                trigger_index=1,
                request_key="run_end:1:"
                + hashlib.sha256(fixture["experiment_id"].encode()).hexdigest(),
                reservation_id=rid,
            )
            params = {
                "id": rid,
                "pid": 31338,
                "ticks": 123456,
                "boot": str(uuid4()),
                "unit": f"swapp-ai-scientist-scorer-{rid.hex}.service",
                "invocation": uuid4().hex,
                "cgroup": "/user.slice/swapp-ai-scientist-scorer.slice/"
                f"swapp-ai-scientist-scorer-{rid.hex}.service",
                "generation": owner.generation,
                "execution": owner.execution_sha256,
            }
            result_bytes = b'{"synthetic_fixture":true}'
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
                    {
                        **params,
                        "result": result_bytes.decode(),
                        "sha": hashlib.sha256(result_bytes).hexdigest(),
                    },
                )
            receipt = read_holdout_bit(director, run_id=run_id, reservation_id=rid)
            assert receipt.state == "passed"
            loop._record_holdout_application(
                state,
                prior,
                receipt,
                trigger_kind="run_end",
                trigger_index=1,
                candidate_id=fixture["experiment_id"],
            )
            with director.connect() as connection:
                remaining = connection.execute(
                    text(
                        "SELECT greatest(0,extract(epoch FROM deadline_at-clock_timestamp())) "
                        "FROM lab.director_execution_contracts WHERE run_id=:run"
                    ),
                    {"run": run_id},
                ).scalar_one()
            time.sleep(float(remaining) + 0.02)
            _seal_and_publish(migrator, director, planner, scorer, lease, run_id, tmp_path)
    finally:
        if token is not None:
            reset_execution_owner(token)
        for engine in (migrator, director, planner, scorer):
            engine.dispose()
