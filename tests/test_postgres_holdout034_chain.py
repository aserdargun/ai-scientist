"""Real DirectorLease crash-prefix closure on disposable synthetic SQL runs.

No worker, model, sandbox, or live holdout evaluation runs. The scored baseline
is a synthetic SQL fixture; every checkpoint and closure is a production write.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import text
from test_holdout_loop_recovery import _minimal_loop_state
from test_postgres_holdout030_recovery import _engines
from test_postgres_run_end_unavailable import _seed_scored_candidate_without_intent

from lab.director.budget import BudgetSnapshot, EpisodeReservation, RunBudget
from lab.director.holdout import register_run_end_intent, reserve_holdout_check
from lab.director.journal import DirectorRunLease
from lab.director.loop import (
    DirectorLoop,
    DirectorLoopState,
    HoldoutApprovedSnapshot,
    _budget_from_json,
    _budget_to_json,
    _strategy_checkpoint_fields,
    _strategy_seed_for_run,
)
from lab.director.ownership import reset_execution_owner
from lab.director.strategy import initial_strategy_state
from lab.scorer.holdout import (
    HoldoutRecoveryResult,
    _failed_holdout_recovery_cas,
    _failed_unclaimed_holdout_recovery_cas,
)


class PrefixCrash(RuntimeError):
    """A simulated crash after one durable production checkpoint commit."""


def _loop(director, lease, fixture, artifact_root, snapshot):
    loop = object.__new__(DirectorLoop)
    loop.run_id = fixture["run_id"]
    loop.director_engine = director
    loop.lease = lease
    loop.artifact_root = artifact_root
    loop.holdout_enabled = True
    loop.budget = RunBudget(wall_limit=3, token_limit=100, snapshot=snapshot)
    return loop


def _initial_state(fixture, snapshot):
    state = _minimal_loop_state(fixture["run_id"], fixture["experiment_id"])
    # Distinct durable approved snapshot proves rollback restores all champion fields.
    approved = HoldoutApprovedSnapshot.from_state(
        state.model_copy(update={"champion_experiment_id": "approved-prior", "best_suite": 0.05}),
        keep_count=0,
    )
    payload = {
        **state.model_dump(mode="json", by_alias=True),
        **_strategy_checkpoint_fields(initial_strategy_state(_strategy_seed_for_run(state.run_id))),
        "suite_id": fixture["suite_id"],
        "suite_version": 3,
        "suite_manifest_sha256": fixture["suite_manifest_sha"],
        "calibration_sha256": fixture["calibration_sha"],
        "champion_source_sha256": fixture["candidate_sha"],
        "champion_source_blob_sha256": fixture["candidate_blob_sha"],
        "holdout_approved_snapshot": approved.model_dump(mode="json"),
        "budget": _budget_to_json(snapshot),
    }
    return DirectorLoopState.model_validate_json(json.dumps(payload)).verify_consistency()


def _crash_after(lease, key):
    original = lease.append_holdout_closure_checkpoint

    def append(**kwargs):
        receipt = original(**kwargs)
        if kwargs["key"] == key:
            raise PrefixCrash(key)
        return receipt

    lease.append_holdout_closure_checkpoint = append


@pytest.mark.live
@pytest.mark.parametrize(
    "prefix",
    [
        "reservation",
        "reserved_state",
        "intent_checkpoint",
        "sql_intent",
        "reconciliation",
        "freeze",
        "application",
        "state",
        "marker",
        "existing_reserved",
        "existing_running",
        "failed_receipt",
        "failed_application",
        "failed_state",
        "failed_marker",
    ],
)
def test_expired_run_end_prefix_replays_full_production_chain(tmp_path, prefix, monkeypatch):
    """Expiry closes each committed prefix without re-admission or budget refund."""
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
            wall_seconds=3,
            model_tokens=100,
        )
        token = fixture["owner_context_token"]
        owner = fixture["owner"]
        run_id = fixture["run_id"]
        with director.connect() as connection:
            timing = connection.execute(
                text(
                    "SELECT started_at,deadline_at FROM lab.director_execution_contracts "
                    "WHERE run_id=:run"
                ),
                {"run": run_id},
            ).one()
        reservation = EpisodeReservation(uuid4(), 1, 0)
        # An unrelated reservation and prior consumption must survive exact reconciliation.
        unrelated = EpisodeReservation(uuid4(), 1, 2)
        before = BudgetSnapshot(
            wall_seconds=0.25,
            model_tokens=7,
            reserved_wall_seconds=2,
            reserved_model_tokens=2,
            reservations=tuple(
                sorted((reservation, unrelated), key=lambda item: item.reservation_id.hex)
            ),
        )
        state = _initial_state(
            fixture,
            replace(before, reserved_wall_seconds=0, reserved_model_tokens=0, reservations=()),
        )
        existing_prefix = prefix.startswith("existing_") or prefix.startswith("failed_")
        sql_reservation_id = uuid4()
        worker_identity = {
            "worker_pid": 31338,
            "worker_start_ticks": 234567,
            "worker_boot_id": str(uuid4()),
            "worker_unit": f"swapp-ai-scientist-scorer-{sql_reservation_id.hex}.service",
            "worker_invocation_id": uuid4().hex,
            "worker_cgroup": "/user.slice/swapp-ai-scientist-scorer.slice/"
            f"swapp-ai-scientist-scorer-{sql_reservation_id.hex}.service",
        }
        recovery_calls = []

        def synthetic_drain(chosen_id, *, remaining_seconds):
            # Only the OS drain is synthetic; the production Scorer CAS sees the real row.
            assert chosen_id == sql_reservation_id
            assert 1 <= remaining_seconds <= 30
            recovery_calls.append(chosen_id)
            if prefix == "existing_running":
                recovered = _failed_holdout_recovery_cas(
                    scorer,
                    chosen_id,
                    run_id,
                    worker_identity,
                    admitted_generation=owner.generation,
                    execution_sha256=owner.execution_sha256,
                )
            else:
                recovered = _failed_unclaimed_holdout_recovery_cas(
                    scorer,
                    chosen_id,
                    run_id,
                    admitted_generation=owner.generation,
                    execution_sha256=owner.execution_sha256,
                )
            return HoldoutRecoveryResult(chosen_id, recovered)

        monkeypatch.setattr(
            "lab.scorer.holdout_supervisor.run_holdout_recovery_process", synthetic_drain
        )
        with DirectorRunLease(director, run_id) as lease:
            loop = _loop(director, lease, fixture, tmp_path, before)
            prior = loop._persist_state(state)
            result = loop._result(state, prior, "budget_exhausted")
            lease.append_checkpoint(
                sequence=loop._next_checkpoint_sequence(),
                key=f"holdout-budget-reserved:{reservation.reservation_id}",
                phase="holdout_budget_reserved",
                payload={
                    "run_id": str(run_id),
                    "trigger_kind": "run_end",
                    "trigger_index": 1,
                    "candidate_experiment_id": fixture["experiment_id"],
                    "reservation_id": str(reservation.reservation_id),
                    "wall_seconds": 1,
                    "model_tokens": 0,
                    "admitted_generation": owner.generation,
                    "execution_sha256": owner.execution_sha256,
                    "budget": _budget_to_json(before),
                },
                artifact_root=tmp_path,
            )
            if prefix in {"reserved_state", "intent_checkpoint", "sql_intent"} or existing_prefix:
                state = state.model_copy(update={"budget": _budget_to_json(before)})
                prior = loop._persist_state(state, checkpoint_tag="run_end_budget_reserved")
                result = loop._result(state, prior, "budget_exhausted")
            if prefix == "intent_checkpoint":
                original = lease.append_checkpoint

                def append_intent(**kwargs):
                    receipt = original(**kwargs)
                    if kwargs["key"] == "director-holdout-run-end-intent":
                        raise PrefixCrash("intent_checkpoint")
                    return receipt

                lease.append_checkpoint = append_intent
                with pytest.raises(PrefixCrash):
                    register_run_end_intent(
                        director,
                        lease=lease,
                        loop_result=result,
                        artifact_root=tmp_path,
                    )
            elif prefix == "sql_intent" or existing_prefix:
                register_run_end_intent(
                    director, lease=lease, loop_result=result, artifact_root=tmp_path
                )

            if existing_prefix:
                candidate_key = hashlib.sha256(fixture["experiment_id"].encode()).hexdigest()
                admitted = reserve_holdout_check(
                    director,
                    run_id=run_id,
                    candidate_experiment_id=fixture["experiment_id"],
                    trigger_kind="run_end",
                    trigger_index=1,
                    request_key=f"run_end:1:{candidate_key}",
                    reservation_id=sql_reservation_id,
                )
                assert admitted.state == "reserved"
                if prefix == "existing_running":
                    with scorer.begin() as connection:
                        claimed = connection.execute(
                            text(
                                "SELECT lab.claim_holdout_reservation(:id,:worker_pid,"
                                ":worker_start_ticks,"
                                ":worker_boot_id,:worker_unit,:worker_invocation_id,:worker_cgroup,"
                                ":generation,:execution_sha256)"
                            ),
                            {
                                "id": sql_reservation_id,
                                **worker_identity,
                                "generation": owner.generation,
                                "execution_sha256": owner.execution_sha256,
                            },
                        ).scalar_one()
                        assert claimed["reservation_id"] == str(sql_reservation_id)
                        assert claimed["run_id"] == str(run_id)
                        assert claimed["admitted_generation"] == owner.generation
                        assert claimed["execution_sha256"] == owner.execution_sha256
            with migrator.connect() as connection:
                if prefix == "existing_running":
                    assert connection.execute(
                        text(
                            "SELECT state FROM lab.holdout_reservations "
                            "WHERE reservation_id=:id"
                        ),
                        {"id": sql_reservation_id},
                    ).scalar_one() == "running"
                quota_before = connection.execute(
                    text(
                        "SELECT coalesce((SELECT used FROM lab.holdout_run_quotas "
                        "WHERE run_id=:run),0),coalesce((SELECT used "
                        "FROM lab.holdout_suite_quotas "
                        "WHERE suite_id=:suite AND suite_version=3),0)"
                    ),
                    {"run": run_id, "suite": fixture["suite_id"]},
                ).one()

        # The original immutable deadline expires; no migration/clock mutation is used.
        with director.connect() as connection:
            remaining = connection.execute(
                text(
                    "SELECT greatest(0,extract(epoch FROM deadline_at-clock_timestamp())) "
                    "FROM lab.director_execution_contracts WHERE run_id=:run"
                ),
                {"run": run_id},
            ).scalar_one()
        time.sleep(float(remaining) + 0.02)
        if prefix.startswith("failed_"):
            assert synthetic_drain(sql_reservation_id, remaining_seconds=30).state == "failed"
            recovery_calls.clear()
        crashes = {
            "reconciliation": f"holdout-budget-reconciled:{reservation.reservation_id}",
            "freeze": "director-holdout-run-end-unavailable-intent:1",
            "application": "holdout-run-end-unavailable:1",
            "state": "director-state:0:run_end",
            "marker": "holdout-state-application:run_end:1",
            "failed_application": "holdout-application:run_end:1",
            "failed_state": "director-state:0:run_end",
            "failed_marker": "holdout-state-application:run_end:1",
        }
        if prefix in crashes:
            with DirectorRunLease(director, run_id) as lease:
                loop = _loop(director, lease, fixture, tmp_path, before)
                _crash_after(lease, crashes[prefix])
                with pytest.raises(PrefixCrash):
                    loop.check_run_end_holdout(result)

        with DirectorRunLease(director, run_id) as lease:
            loop = _loop(director, lease, fixture, tmp_path, BudgetSnapshot())
            latest = lease.read_checkpoint(key=loop._latest_state_key(), artifact_root=tmp_path)
            durable_state = DirectorLoopState.model_validate_json(
                json.dumps(latest["payload"])
            ).verify_consistency()
            loop._restore_budget(durable_state.budget, after_sequence=latest["receipt"]["sequence"])
            final = loop._pending_run_end_terminal_result(durable_state, latest["receipt"])
            assert final is not None
            assert final.champion_experiment_id == "approved-prior"
            assert final.best_suite == 0.05
            final_checkpoint = lease.read_checkpoint(
                key=loop._state_key_for_digest(final.checkpoint_sha256), artifact_root=tmp_path
            )
            final_state = DirectorLoopState.model_validate_json(
                json.dumps(final_checkpoint["payload"])
            )
            final_budget = _budget_from_json(final_state.budget)
            assert final_state.holdout_last_status == "failed"
            assert final_budget.wall_seconds == 1.25
            assert final_budget.reservations == (unrelated,)
            assert final_budget.reserved_wall_seconds == 1
            assert final_budget.reserved_model_tokens == 2
            assert final_budget.model_tokens == 7
            assert final_budget.elapsed_wall_seconds >= 3
            marker = lease.read_checkpoint(
                key="holdout-state-application:run_end:1", artifact_root=tmp_path
            )
            assert marker["payload"]["state_sha256"] == final.checkpoint_sha256
            with director.connect() as connection:
                event_count = connection.execute(
                    text(
                        "SELECT count(*) FROM lab.run_events "
                        "WHERE run_id=:run AND event_type='director.checkpoint'"
                    ),
                    {"run": run_id},
                ).scalar_one()
            repeated = loop.check_run_end_holdout(final)
            assert repeated == final
            assert (
                lease.read_checkpoint(
                    key="holdout-state-application:run_end:1", artifact_root=tmp_path
                )
                == marker
            )
            with director.connect() as connection:
                assert (
                    connection.execute(
                        text(
                            "SELECT count(*) FROM lab.run_events "
                            "WHERE run_id=:run AND event_type='director.checkpoint'"
                        ),
                        {"run": run_id},
                    ).scalar_one()
                    == event_count
                )
                assert (
                    connection.execute(
                        text(
                            "SELECT started_at,deadline_at "
                            "FROM lab.director_execution_contracts WHERE run_id=:run"
                        ),
                        {"run": run_id},
                    ).one()
                    == timing
                )
            with migrator.connect() as connection:
                assert connection.execute(
                    text("SELECT count(*) FROM lab.holdout_reservations WHERE run_id=:run"),
                    {"run": run_id},
                ).scalar_one() == int(existing_prefix)
                assert (
                    connection.execute(
                        text(
                            "SELECT coalesce((SELECT used FROM lab.holdout_run_quotas "
                            "WHERE run_id=:run),0),coalesce((SELECT used "
                            "FROM lab.holdout_suite_quotas "
                            "WHERE suite_id=:suite AND suite_version=3),0)"
                        ),
                        {"run": run_id, "suite": fixture["suite_id"]},
                    ).one()
                    == quota_before
                )
                assert (
                    connection.execute(
                        text(
                            "SELECT count(*) FROM scorer.holdout_results result "
                            "JOIN lab.holdout_reservations reservation USING(reservation_id) "
                            "WHERE reservation.run_id=:run"
                        ),
                        {"run": run_id},
                    ).scalar_one()
                    == 0
                )
            assert recovery_calls == (
                [sql_reservation_id] if prefix.startswith("existing_") else []
            )
    finally:
        if token is not None:
            reset_execution_owner(token)
        for engine in (migrator, director, planner, scorer):
            engine.dispose()
