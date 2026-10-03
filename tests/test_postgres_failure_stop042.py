"""Genuine-role, disposable SQL failure-stop regression; synthetic owner only.

Uses the prefix-guarded existing PostgreSQL harness. No Director/Scorer worker,
GPU, model, AOS, or historical native run is operated on by these tests.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_postgres_holdout030_recovery import _engines

from lab import cli
from lab.director.ledger import register_experiment
from lab.director.ownership import (
    ExecutionContract,
    OwnerProcessIdentity,
    bind_execution_owner,
    canonical_execution_bytes,
    claim_initial_execution,
    reset_execution_owner,
)

pytestmark = pytest.mark.live
STOP = text(
    "SELECT lab.close_director_run_if_owned(:run,:generation,:invocation,"
    ":execution,'stop_requested',:error,:reason)"
)


@pytest.fixture
def stopped_run():
    engines = _engines()
    migrator, director, _, _ = engines
    run = uuid4()
    request = {
        "purpose": "baseline",
        "track": "anomaly",
        "suite": "synthetic.failure-stop042",
        "suite_manifest_sha256": "a" * 64,
        "provider": "fake-json",
        "proposal_limit": 0,
        "budget": {"experiments": 0, "wall_seconds": 600, "model_tokens": 0},
    }
    request_sha = hashlib.sha256(canonical_execution_bytes(request)).hexdigest()
    with migrator.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO lab.runs(run_id,origin,owner_id,idempotency_key,payload_sha256,"
                "request_json,state) VALUES(:run,'local','fixture:failure-stop042',:key,:sha,"
                "CAST(:request AS jsonb),'queued')"
            ),
            {"run": run, "key": run.hex, "sha": request_sha, "request": json.dumps(request)},
        )
    unit = f"swapp-ai-scientist-director-dispatch-{run.hex}.service"
    process = OwnerProcessIdentity(
        request_sha, 101, 123, str(uuid4()), unit, uuid4().hex, "/user.slice/fixture/" + unit
    )
    contract = ExecutionContract.build(
        run_id=run,
        request=request,
        request_sha256=request_sha,
        suite_id=request["suite"],
        suite_version=1,
        suite_manifest_sha256="a" * 64,
        registry_entry_sha256="b" * 64,
        harness_sha256="c" * 64,
        image_sha256="d" * 64,
    )
    owner = claim_initial_execution(director, contract=contract, process=process)
    token = bind_execution_owner(owner)
    try:
        # An unfinished production registration makes the ordinary failed-state
        # ledger fence reject, exercising the actual exception-to-stop fallback.
        register_experiment(
            director,
            experiment_id=f"exp_{uuid4().hex}",
            run_id=str(run),
            sequence=0,
            experiment_number=None,
            kind="baseline",
            baseline_name="robust_z",
            parent_experiment_id=None,
            candidate_sha256="e" * 64,
            candidate_blob_sha256="e" * 64,
            inputs_sha256="f" * 64,
            move_type="detector",
            system="S1",
            hypothesis="synthetic incomplete-ledger failure-stop fixture",
            predicted_delta=None,
            proposal={"schema": "baseline.registration.v1", "fixture_only": True},
        )
        yield engines, run, owner
    finally:
        reset_execution_owner(token)
        for engine in engines:
            engine.dispose()


def _snapshot(director, run):
    with director.connect() as connection:
        state = connection.execute(
            text("SELECT state,stop_requested,updated_at FROM lab.runs WHERE run_id=:run"),
            {"run": run},
        ).one()
        events = connection.execute(
            text(
                "SELECT event_type,created_at,event_json FROM lab.run_events "
                "WHERE run_id=:run ORDER BY created_at,event_id"
            ),
            {"run": run},
        ).all()
        deadline = connection.execute(
            text("SELECT deadline_at FROM lab.director_execution_contracts WHERE run_id=:run"),
            {"run": run},
        ).scalar_one()
    return state, events, deadline


def _stop_arguments(run, owner):
    return dict(
        run=run,
        generation=owner.generation,
        invocation=owner.invocation_id,
        execution=owner.execution_sha256,
        error="RuntimeError",
        reason="execution_exception",
    )


def _stop(director, run, owner):
    with director.begin() as connection:
        return connection.execute(STOP, _stop_arguments(run, owner)).scalar_one()


def test_running_exception_stop_atomically_records_first_stop(stopped_run):
    engines, run, owner = stopped_run
    director = engines[1]
    before = _snapshot(director, run)
    assert before[0].state == "running"
    result = cli._record_claimed_dispatch_failure(director, run, RuntimeError(), owner=owner)
    assert result["state"] == "stop_requested"
    state, events, deadline = _snapshot(director, run)
    assert state.state == "stop_requested" and state.stop_requested
    failures = [event for event in events if event.event_type == "run.dispatch_recovery_required"]
    stops = [event for event in events if event.event_type == "run.stop_requested"]
    assert len(failures) == 1
    assert failures[0].event_json["reason"] == "execution_exception"
    assert len(stops) == 1, "failure-stop transition lacks canonical first-stop event"
    assert stops[0].created_at == state.updated_at == failures[0].created_at
    assert stops[0].event_json["generation"] == owner.generation
    assert stops[0].event_json["invocation_id"] == owner.invocation_id
    assert stops[0].event_json["execution_sha256"] == owner.execution_sha256
    assert deadline == before[2]


def test_exact_retry_preserves_first_stop_and_execution_deadline(stopped_run):
    engines, run, owner = stopped_run
    director = engines[1]
    first = _stop(director, run, owner)
    before = _snapshot(director, run)
    assert _stop(director, run, owner) == first
    assert _snapshot(director, run) == before


@pytest.mark.parametrize(
    "field,value",
    [("generation", 2), ("invocation", "a" * 32), ("execution", "a" * 64)],
)
def test_replaced_owner_cannot_replay_or_change_first_stop(stopped_run, field, value):
    engines, run, owner = stopped_run
    director = engines[1]
    _stop(director, run, owner)
    before = _snapshot(director, run)
    parameters = _stop_arguments(run, owner)
    parameters[field] = value
    with pytest.raises(DBAPIError, match="owner is stale"), director.begin() as connection:
        connection.execute(STOP, parameters)
    assert _snapshot(director, run) == before


@pytest.mark.parametrize("field,value", [("error", "ValueError"), ("reason", "different")])
def test_changed_failure_is_not_an_exact_retry(stopped_run, field, value):
    engines, run, owner = stopped_run
    director = engines[1]
    _stop(director, run, owner)
    before = _snapshot(director, run)
    parameters = _stop_arguments(run, owner)
    parameters[field] = value
    with pytest.raises(DBAPIError, match="original failure-stop event"), director.begin() as c:
        c.execute(STOP, parameters)
    assert _snapshot(director, run) == before


@pytest.mark.parametrize("role_index", [0, 2, 3])
def test_other_roles_cannot_close_or_write_stop_event(stopped_run, role_index):
    engines, run, owner = stopped_run
    before = _snapshot(engines[1], run)
    with pytest.raises(DBAPIError), engines[role_index].begin() as connection:
        connection.execute(STOP, _stop_arguments(run, owner))
    assert _snapshot(engines[1], run) == before


def test_legacy_missing_event_is_never_repaired_with_a_fresh_deadline(stopped_run):
    engines, run, owner = stopped_run
    migrator, director, _, _ = engines
    with migrator.begin() as connection:
        connection.execute(
            text("UPDATE lab.runs SET state='stop_requested',stop_requested=true WHERE run_id=:r"),
            {"r": run},
        )
    before = _snapshot(director, run)
    with pytest.raises(DBAPIError, match="original failure-stop event"):
        _stop(director, run, owner)
    assert _snapshot(director, run) == before


def test_api_stop_race_preserves_the_existing_first_stop(stopped_run):
    engines, run, owner = stopped_run
    director = engines[1]
    with director.begin() as connection:
        connection.execute(
            text("SELECT lab.request_director_run_stop(:run,'fixture:failure-stop042','local')"),
            {"run": run},
        )
    before = _snapshot(director, run)
    result = cli._record_claimed_dispatch_failure(director, run, RuntimeError(), owner=owner)
    assert result["state"] == "stop_requested"
    assert _snapshot(director, run) == before


def test_transaction_rollback_leaves_no_stop_or_failure_event(stopped_run):
    engines, run, owner = stopped_run
    director = engines[1]
    before = _snapshot(director, run)
    with director.connect() as connection:
        transaction = connection.begin()
        connection.execute(STOP, _stop_arguments(run, owner))
        transaction.rollback()
    assert _snapshot(director, run) == before


def test_concurrent_exact_stops_write_one_immutable_first_stop(stopped_run):
    engines, run, owner = stopped_run
    director = engines[1]
    barrier = Barrier(2)

    def stop_once():
        barrier.wait(timeout=5)
        return _stop(director, run, owner)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = [
            future.result(timeout=10)
            for future in (executor.submit(stop_once), executor.submit(stop_once))
        ]
    assert first == second
    state, events, _ = _snapshot(director, run)
    stops = [event for event in events if event.event_type == "run.stop_requested"]
    failures = [event for event in events if event.event_type == "run.dispatch_recovery_required"]
    assert len(stops) == len(failures) == 1
    assert stops[0].created_at == failures[0].created_at == state.updated_at
