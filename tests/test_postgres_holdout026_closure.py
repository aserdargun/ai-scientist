"""Real-role SQL checks for stopped, owner-bound bitless run-end closure.

These cases use a synthetic scored candidate and synthetic Director process
identity. They test the persisted generation fence and its retry behavior;
they do not establish OS process, unit, cgroup, sandbox-drain, or live scoring
proof.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_postgres_holdout030_recovery import _engines
from test_postgres_run_end_unavailable import (
    _counts,
    _fence_with_generation,
    _seed_scored_candidate_without_intent,
    _sha,
    _write_checkpoints,
)

from lab.director.holdout import (
    fence_run_end_reserved_budget_without_intent,
    fence_run_end_unavailable,
    read_run_end_unavailable,
)
from lab.director.ownership import (
    ExecutionOwner,
    assert_execution_owner_transaction,
    reset_execution_owner,
)


def _write_reserved_budget_prefix(director, *, run_id: UUID, candidate_id: str, owner):
    """Seed exact synthetic checkpoints for a budget reservation before SQL intent."""
    reservation_id = uuid4()
    reservation_key = f"holdout-budget-reserved:{reservation_id}"
    reconciliation_key = f"holdout-budget-reconciled:{reservation_id}"
    state_key = "director-state:synthetic-prior"
    state_sha = _sha(b"synthetic prior state before reserved-budget prefix")
    budget_sha = _sha(b"synthetic frozen budget snapshot after reconciliation")
    reservation_meta = {
        "reservation_id": str(reservation_id),
        "trigger_kind": "run_end",
        "trigger_index": 1,
        "candidate_experiment_id": candidate_id,
        "wall_seconds": 2,
        "model_tokens": 0,
        "admitted_generation": owner.generation,
        "execution_sha256": owner.execution_sha256,
    }
    reconciliation_meta = {
        **{key: value for key, value in reservation_meta.items() if key != "wall_seconds"},
        "budget_snapshot_sha256": budget_sha,
        "reserved_wall_seconds": 2,
        "measured_wall_seconds": 2.0,
        "wall_seconds_before": 10.0,
        "wall_seconds_after": 12.0,
        "proposal_count_before": 1,
        "proposal_count_after": 1,
        "reserved_model_tokens_before": 0,
        "reserved_model_tokens_after": 0,
        "reserved_wall_seconds_before": 2.0,
        "reserved_wall_seconds_after": 0.0,
        "reservation_ids_before": [str(reservation_id)],
        "reservation_ids_after": [],
    }
    events = [
        (
            state_key,
            "director_loop_state",
            10,
            state_sha,
            {},
        ),
        (
            reservation_key,
            "holdout_budget_reserved",
            11,
            _sha(json.dumps(reservation_meta, sort_keys=True, separators=(",", ":")).encode()),
            {"holdout_reservation": reservation_meta},
        ),
        (
            reconciliation_key,
            "holdout_budget_reconciled",
            12,
            _sha(json.dumps(reconciliation_meta, sort_keys=True, separators=(",", ":")).encode()),
            {"holdout_budget_reconciliation": reconciliation_meta},
        ),
    ]
    unavailable_sha = _sha(b"synthetic unavailable intent after budget reconciliation")
    events.append(
        (
            "director-holdout-run-end-unavailable-intent:1",
            "holdout_run_end_unavailable_intent",
            13,
            unavailable_sha,
            {},
        )
    )
    with director.begin() as connection:
        assert_execution_owner_transaction(connection, run_id=run_id)
        for key, phase, sequence, digest, extra in events:
            connection.execute(
                text(
                    "INSERT INTO lab.run_events(event_id,run_id,event_type,event_json) "
                    "VALUES (:id,:run,'director.checkpoint',CAST(:event AS jsonb))"
                ),
                {
                    "id": uuid4(),
                    "run": run_id,
                    "event": json.dumps(
                        {
                            "key": key,
                            "phase": phase,
                            "sequence": sequence,
                            "payload_sha256": digest,
                            **extra,
                        }
                    ),
                },
            )
    return {
        "state_key": state_key,
        "state_sha": state_sha,
        "state_sequence": 10,
        "budget_sha": budget_sha,
        "checkpoint_sha": unavailable_sha,
        "checkpoint_sequence": 13,
        "reservation_id": reservation_id,
        "reservation_key": reservation_key,
        "reservation_sha": events[1][3],
        "reservation_sequence": 11,
        "reconciliation_key": reconciliation_key,
        "reconciliation_sha": events[2][3],
        "reconciliation_sequence": 12,
    }


def _fence_reserved_budget_prefix(director, *, run_id: UUID, candidate_id: str, owner, proof):
    with director.begin() as connection:
        return connection.execute(
            text(
                "SELECT lab.fence_run_end_reserved_budget_without_intent(:run,:candidate,"
                ":state_key,:state_sha,:state_seq,:budget_sha,:checkpoint_sha,"
                ":checkpoint_seq,:reservation_key,:reservation_sha,:reservation_seq,"
                ":reconciliation_key,:reconciliation_sha,:reconciliation_seq,"
                ":generation,:invocation,:execution_sha)"
            ),
            {
                "run": run_id,
                "candidate": candidate_id,
                "state_key": proof["state_key"],
                "state_sha": proof["state_sha"],
                "state_seq": proof["state_sequence"],
                "budget_sha": proof["budget_sha"],
                "checkpoint_sha": proof["checkpoint_sha"],
                "checkpoint_seq": proof["checkpoint_sequence"],
                "reservation_key": proof["reservation_key"],
                "reservation_sha": proof["reservation_sha"],
                "reservation_seq": proof["reservation_sequence"],
                "reconciliation_key": proof["reconciliation_key"],
                "reconciliation_sha": proof["reconciliation_sha"],
                "reconciliation_seq": proof["reconciliation_sequence"],
                "generation": owner.generation,
                "invocation": owner.invocation_id,
                "execution_sha": owner.execution_sha256,
            },
        ).scalar_one()


@pytest.mark.live
@pytest.mark.parametrize(
    ("reason", "intent_checkpoint"),
    [
        ("fresh_wall_budget_unavailable", False),
        ("intent_checkpoint_without_sql_registration", True),
    ],
)
def test_stopped_run_end_unavailable_fence_is_bound_to_captured_generation(
    tmp_path, reason: str, intent_checkpoint: bool
) -> None:
    """Wrong owner tuples cannot fence; the captured owner can retry bitlessly."""
    migrator, director, planner, scorer = _engines()
    owner_token = None
    try:
        fixture = _seed_scored_candidate_without_intent(
            migrator, director, planner, scorer, tmp_path, claim_owner=True
        )
        run_id = fixture["run_id"]
        candidate_id = str(fixture["experiment_id"])
        owner = fixture["owner"]
        owner_token = fixture["owner_context_token"]
        assert isinstance(run_id, UUID)
        assert isinstance(owner, ExecutionOwner)
        assert owner.run_id == run_id

        checkpoints = _write_checkpoints(
            director, run_id=run_id, intent_checkpoint=intent_checkpoint
        )
        before = _counts(migrator, run_id, str(fixture["suite_id"]))
        assert before[0:3] == (0, 0, 0)
        assert before[4] == 0

        # This is the authenticated API stop transition; the later fence uses
        # the immutable owner captured by the initial claim, never a reread.
        with director.begin() as connection:
            stop_receipt = connection.execute(
                text("SELECT lab.request_director_run_stop(:run,:owner,'local')"),
                {"run": run_id, "owner": "fixture:runend-unavailable"},
            ).scalar_one()
        assert stop_receipt["changed"] is True
        assert stop_receipt["state"] == "stop_requested"
        assert stop_receipt["admitted_generation"] == owner.generation
        assert stop_receipt["execution_sha256"] == owner.execution_sha256

        fence_values = {
            "run_id": run_id,
            "candidate_experiment_id": candidate_id,
            "reason": reason,
            "prior_state_key": checkpoints["state_key"],
            "prior_state_sha256": checkpoints["state_sha"],
            "prior_state_sequence": checkpoints["state_sequence"],
            "budget_snapshot_sha256": checkpoints["budget_sha"],
            "unavailable_checkpoint_sha256": checkpoints["checkpoint_sha"],
            "unavailable_checkpoint_sequence": checkpoints["checkpoint_sequence"],
            "remaining_wall_seconds": (0.25 if reason == "fresh_wall_budget_unavailable" else 5.0),
            "original_intent_checkpoint_sha256": checkpoints["intent_sha"],
            "original_intent_checkpoint_sequence": checkpoints["intent_sequence"],
        }

        # The pre-0026 ownerless overload is revoked; all writes must carry the
        # captured generation tuple through the replacement adapter.
        with pytest.raises(DBAPIError) as legacy_error:
            with director.begin() as connection:
                connection.execute(
                    text(
                        "SELECT lab.fence_run_end_unavailable(:run,:candidate,:reason,"
                        ":state_key,:state_sha,:state_seq,:budget_sha,:checkpoint_sha,"
                        ":checkpoint_seq,:remaining_wall,:intent_sha,:intent_seq)"
                    ),
                    {
                        "run": run_id,
                        "candidate": candidate_id,
                        "reason": reason,
                        "state_key": checkpoints["state_key"],
                        "state_sha": checkpoints["state_sha"],
                        "state_seq": checkpoints["state_sequence"],
                        "budget_sha": checkpoints["budget_sha"],
                        "checkpoint_sha": checkpoints["checkpoint_sha"],
                        "checkpoint_seq": checkpoints["checkpoint_sequence"],
                        "remaining_wall": fence_values["remaining_wall_seconds"],
                        "intent_sha": checkpoints["intent_sha"],
                        "intent_seq": checkpoints["intent_sequence"],
                    },
                )
        assert getattr(legacy_error.value.orig, "sqlstate", None) == "42501"

        # A stale generation, invocation, or execution digest must fail
        # atomically before a fence row exists.
        wrong_owner_pairs = (
            (owner.generation + 1, owner.invocation_id, owner.execution_sha256),
            (owner.generation, "0" * 32, owner.execution_sha256),
            (owner.generation, owner.invocation_id, "f" * 64),
        )
        for generation, invocation_id, execution_sha256 in wrong_owner_pairs:
            with pytest.raises(
                DBAPIError, match="closure owner is no longer current"
            ) as owner_error:
                _fence_with_generation(
                    director,
                    run_id=run_id,
                    candidate_id=candidate_id,
                    reason=reason,
                    proof=checkpoints,
                    generation=generation,
                    invocation_id=invocation_id,
                    execution_sha256=execution_sha256,
                )
            assert getattr(owner_error.value.orig, "sqlstate", None) == "P0001"
            with director.connect() as connection:
                assert (
                    connection.execute(
                        text("SELECT lab.read_run_end_unavailable_v2(:run)"), {"run": run_id}
                    ).scalar_one()
                    is None
                )
            assert _counts(migrator, run_id, str(fixture["suite_id"])) == before

        malformed_owner_pairs = (
            (None, owner.invocation_id, owner.execution_sha256),
            (owner.generation, None, owner.execution_sha256),
            (owner.generation, owner.invocation_id, None),
        )
        for generation, invocation_id, execution_sha256 in malformed_owner_pairs:
            with pytest.raises(
                DBAPIError, match="Director holdout closure identity is malformed"
            ) as malformed_error:
                _fence_with_generation(
                    director,
                    run_id=run_id,
                    candidate_id=candidate_id,
                    reason=reason,
                    proof=checkpoints,
                    generation=generation,
                    invocation_id=invocation_id,
                    execution_sha256=execution_sha256,
                )
            assert getattr(malformed_error.value.orig, "sqlstate", None) == "P0001"
            with director.connect() as connection:
                assert (
                    connection.execute(
                        text("SELECT lab.read_run_end_unavailable_v2(:run)"), {"run": run_id}
                    ).scalar_one()
                    is None
                )
            assert _counts(migrator, run_id, str(fixture["suite_id"])) == before

        first = fence_run_end_unavailable(director, **fence_values)
        retry = fence_run_end_unavailable(director, **fence_values)
        readback = read_run_end_unavailable(director, run_id=run_id)
        assert first == retry == readback
        assert first.state == "failed" and first.bit is None
        assert first.reason == reason
        assert first.candidate_experiment_id == candidate_id
        assert first.admitted_generation == owner.generation
        assert first.execution_sha256 == owner.execution_sha256
        after = _counts(migrator, run_id, str(fixture["suite_id"]))
        assert after == before[:4] + (before[4] + 1, before[5])
    finally:
        if owner_token is not None:
            reset_execution_owner(owner_token)
        for engine in (migrator, director, planner, scorer):
            engine.dispose()


@pytest.mark.live
def test_stopped_run_end_reserved_budget_prefix_is_fenced_bitlessly(tmp_path) -> None:
    """A durable budget reservation without SQL intent cannot become a holdout query."""
    migrator, director, planner, scorer = _engines()
    owner_token = None
    try:
        fixture = _seed_scored_candidate_without_intent(
            migrator, director, planner, scorer, tmp_path, claim_owner=True
        )
        run_id = fixture["run_id"]
        candidate_id = str(fixture["experiment_id"])
        owner = fixture["owner"]
        owner_token = fixture["owner_context_token"]
        assert isinstance(owner, ExecutionOwner)
        checkpoints = _write_reserved_budget_prefix(
            director, run_id=run_id, candidate_id=candidate_id, owner=owner
        )
        before = _counts(migrator, run_id, str(fixture["suite_id"]))

        with director.begin() as connection:
            stop_receipt = connection.execute(
                text("SELECT lab.request_director_run_stop(:run,:owner,'local')"),
                {"run": run_id, "owner": "fixture:runend-unavailable"},
            ).scalar_one()
        assert stop_receipt["state"] == "stop_requested"
        assert stop_receipt["admitted_generation"] == owner.generation

        # SQL CHECK constraints treat NULL predicates as UNKNOWN, so exercise
        # the RPC-level completeness guard with key/sequence present but the
        # reservation checkpoint digest absent.
        with pytest.raises(
            DBAPIError, match="reserved-budget closure receipts differ"
        ) as provenance_error:
            _fence_reserved_budget_prefix(
                director,
                run_id=run_id,
                candidate_id=candidate_id,
                owner=owner,
                proof={**checkpoints, "reservation_sha": None},
            )
        assert getattr(provenance_error.value.orig, "sqlstate", None) == "P0001"
        assert _counts(migrator, run_id, str(fixture["suite_id"])) == before

        # Every identity component is required and checked before the bitless
        # terminal row can be inserted.
        invalid_owners = (
            (
                SimpleNamespace(
                    generation=owner.generation + 1,
                    invocation_id=owner.invocation_id,
                    execution_sha256=owner.execution_sha256,
                ),
                "closure owner is no longer current",
            ),
            (
                replace(owner, invocation_id="0" * 32),
                "closure owner is no longer current",
            ),
            (
                replace(owner, execution_sha256="f" * 64),
                "closure owner is no longer current",
            ),
            (
                SimpleNamespace(
                    generation=None,
                    invocation_id=owner.invocation_id,
                    execution_sha256=owner.execution_sha256,
                ),
                "Director holdout closure identity is malformed",
            ),
            (
                SimpleNamespace(
                    generation=owner.generation,
                    invocation_id=None,
                    execution_sha256=owner.execution_sha256,
                ),
                "Director holdout closure identity is malformed",
            ),
            (
                SimpleNamespace(
                    generation=owner.generation,
                    invocation_id=owner.invocation_id,
                    execution_sha256=None,
                ),
                "Director holdout closure identity is malformed",
            ),
        )
        for invalid_owner, message in invalid_owners:
            with pytest.raises(DBAPIError, match=message) as identity_error:
                _fence_reserved_budget_prefix(
                    director,
                    run_id=run_id,
                    candidate_id=candidate_id,
                    owner=invalid_owner,
                    proof=checkpoints,
                )
            assert getattr(identity_error.value.orig, "sqlstate", None) == "P0001"
            with director.connect() as connection:
                assert connection.execute(
                    text("SELECT lab.read_run_end_unavailable_v2(:run)"), {"run": run_id}
                ).scalar_one() is None
            assert _counts(migrator, run_id, str(fixture["suite_id"])) == before

        fence_kwargs = {
            "run_id": run_id,
            "candidate_experiment_id": candidate_id,
            "prior_state_key": checkpoints["state_key"],
            "prior_state_sha256": checkpoints["state_sha"],
            "prior_state_sequence": checkpoints["state_sequence"],
            "budget_snapshot_sha256": checkpoints["budget_sha"],
            "unavailable_checkpoint_sha256": checkpoints["checkpoint_sha"],
            "unavailable_checkpoint_sequence": checkpoints["checkpoint_sequence"],
            "reservation_checkpoint_key": checkpoints["reservation_key"],
            "reservation_checkpoint_sha256": checkpoints["reservation_sha"],
            "reservation_checkpoint_sequence": checkpoints["reservation_sequence"],
            "reconciliation_checkpoint_key": checkpoints["reconciliation_key"],
            "reconciliation_checkpoint_sha256": checkpoints["reconciliation_sha"],
            "reconciliation_checkpoint_sequence": checkpoints["reconciliation_sequence"],
        }
        receipt = fence_run_end_reserved_budget_without_intent(director, **fence_kwargs)
        retry = fence_run_end_reserved_budget_without_intent(director, **fence_kwargs)
        assert receipt == retry
        assert receipt.state == "failed" and receipt.bit is None
        assert receipt.reason == "reservation_checkpoint_without_sql_intent"
        assert receipt.reservation_checkpoint_key == checkpoints["reservation_key"]
        assert receipt.reservation_checkpoint_sha256 == checkpoints["reservation_sha"]
        assert receipt.reservation_checkpoint_sequence == checkpoints["reservation_sequence"]
        assert receipt.admitted_generation == owner.generation
        assert receipt.execution_sha256 == owner.execution_sha256
        readback = read_run_end_unavailable(director, run_id=run_id)
        assert readback == receipt

        # Raw SQL below remains a negative-only adapter probe for a stale
        # invocation. The successful transition uses the public typed API.
        with director.connect() as connection:
            assert connection.execute(
                text("SELECT lab.read_run_end_unavailable_v2(:run)"), {"run": run_id}
            ).scalar_one() == {
                "run_id": str(run_id),
                "candidate_experiment_id": candidate_id,
                "reason": "reservation_checkpoint_without_sql_intent",
                "prior_state_key": checkpoints["state_key"],
                "prior_state_sha256": checkpoints["state_sha"],
                "prior_state_sequence": checkpoints["state_sequence"],
                "budget_snapshot_sha256": checkpoints["budget_sha"],
                "unavailable_checkpoint_sha256": checkpoints["checkpoint_sha"],
                "unavailable_checkpoint_sequence": checkpoints["checkpoint_sequence"],
                "original_intent_checkpoint_sha256": None,
                "original_intent_checkpoint_sequence": None,
                "reservation_checkpoint_key": checkpoints["reservation_key"],
                "reservation_checkpoint_sha256": checkpoints["reservation_sha"],
                "reservation_checkpoint_sequence": checkpoints["reservation_sequence"],
                "state": "failed",
                "bit": None,
                "admitted_generation": owner.generation,
                "execution_sha256": owner.execution_sha256,
            }
        after = _counts(migrator, run_id, str(fixture["suite_id"]))
        assert after == before[:4] + (before[4] + 1, before[5])
    finally:
        if owner_token is not None:
            reset_execution_owner(owner_token)
        for engine in (migrator, director, planner, scorer):
            engine.dispose()


@pytest.mark.live
def test_director_owner_gucs_reset_after_commit_and_on_fresh_connection(tmp_path) -> None:
    """Owner assertions set transaction-local GUCs, never pooled-session identity."""
    migrator, director, planner, scorer = _engines()
    owner_token = None
    try:
        fixture = _seed_scored_candidate_without_intent(
            migrator, director, planner, scorer, tmp_path, claim_owner=True
        )
        owner = fixture["owner"]
        owner_token = fixture["owner_context_token"]
        run_id = fixture["run_id"]
        assert isinstance(owner, ExecutionOwner)
        guc_names = (
            "lab.owner_run_id",
            "lab.owner_generation",
            "lab.owner_invocation_id",
            "lab.owner_execution_sha256",
            "lab.holdout_run_id",
            "lab.holdout_generation",
            "lab.holdout_execution_sha256",
            "lab.holdout_closure_run_id",
            "lab.holdout_closure_generation",
            "lab.holdout_closure_execution_sha256",
        )
        settings_sql = text(
            "SELECT "
            + ",".join(f"current_setting(:guc_{index},true)" for index in range(len(guc_names)))
        )
        params = {f"guc_{index}": name for index, name in enumerate(guc_names)}

        with director.connect() as connection:
            with connection.begin():
                before = connection.execute(settings_sql, params).one()
                assert all(value in (None, "") for value in before)
                assert_execution_owner_transaction(connection, owner, run_id=run_id)
                connection.execute(
                    text(
                        "SELECT lab.assert_director_holdout_closure("
                        ":run_id,:generation,:invocation,:execution_sha)"
                    ),
                    {
                        "run_id": run_id,
                        "generation": owner.generation,
                        "invocation": owner.invocation_id,
                        "execution_sha": owner.execution_sha256,
                    },
                )
                during = connection.execute(settings_sql, params).one()
                assert during == (
                    str(run_id),
                    str(owner.generation),
                    owner.invocation_id,
                    owner.execution_sha256,
                    str(run_id),
                    str(owner.generation),
                    owner.execution_sha256,
                    str(run_id),
                    str(owner.generation),
                    owner.execution_sha256,
                )
            # The same checked-out PostgreSQL session must have no owner context
            # after commit, even though SQLAlchemy reuses this connection object.
            with connection.begin():
                after_commit = connection.execute(settings_sql, params).one()
                assert all(value in (None, "") for value in after_commit)

        # A newly checked-out connection cannot inherit the prior transaction's
        # owner tuple either (the pool may return the same physical session).
        with director.connect() as fresh_connection:
            with fresh_connection.begin():
                fresh = fresh_connection.execute(settings_sql, params).one()
                assert all(value in (None, "") for value in fresh)
    finally:
        if owner_token is not None:
            reset_execution_owner(owner_token)
        for engine in (migrator, director, planner, scorer):
            engine.dispose()
