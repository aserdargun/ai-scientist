"""Disposable genuine-role SQL fixtures; no candidate, process or GPU acceptance."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_postgres_holdout030_recovery import _engines

from lab.director.ledger import register_experiment
from lab.director.ownership import (
    ExecutionContract,
    OwnerProcessIdentity,
    assert_execution_owner_transaction,
    bind_execution_owner,
    canonical_execution_bytes,
    claim_initial_execution,
    reset_execution_owner,
)

pytestmark = pytest.mark.live
EMPTY = hashlib.sha256(b"[]").hexdigest()
EVIDENCE = text("SELECT lab.mode_stream_report_evidence(:run,:generation,:sha,:empty)")


@pytest.fixture
def engines():
    values = _engines()
    try:
        yield values
    finally:
        for engine in values:
            engine.dispose()


def _seed(engines, *, owned=True, changed=None):
    request = {
        "purpose": "mode-stream", "provider": "mode-stream", "track": "mode",
        "program_version": "mode-stream.v1", "suite": "synthetic.mode-stream043",
        "suite_manifest_sha256": "a" * 64, "proposal_limit": 0,
        "budget": {"experiments": 0, "model_tokens": 0, "wall_seconds": 600},
        **(changed or {}),
    }
    run = uuid4()
    payload = hashlib.sha256(canonical_execution_bytes(request)).hexdigest()
    with engines[0].begin() as connection:
        connection.execute(text(
            "INSERT INTO lab.runs(run_id,origin,owner_id,idempotency_key,payload_sha256,"
            "request_json,state) VALUES(:run,'local','fixture:mode-stream043',:key,:sha,"
            "CAST(:request AS jsonb),'queued')"
        ), {"run": run, "key": run.hex, "sha": payload, "request": json.dumps(request)})
    process = owner = None
    if owned:
        unit = f"swapp-ai-scientist-director-dispatch-{run.hex}.service"
        process = OwnerProcessIdentity(
            payload, 101, 123, str(uuid4()), unit, uuid4().hex, "/user.slice/fixture/" + unit
        )
        contract = ExecutionContract.build(
            run_id=run, request=request, request_sha256=payload, suite_id=request["suite"],
            suite_version=1, suite_manifest_sha256="a" * 64,
            registry_entry_sha256="b" * 64, harness_sha256="c" * 64, image_sha256="d" * 64,
        )
        owner = claim_initial_execution(engines[1], contract=contract, process=process)
    return run, payload, owner, process


def _seal(engines, run, owner):
    with engines[2].begin() as connection:
        assert_execution_owner_transaction(connection, owner, run_id=run)
        connection.execute(text(
            "UPDATE lab.runs SET task_plan_sha256=:sha,task_plan_count=0 WHERE run_id=:run"
        ), {"run": run, "sha": EMPTY})


def _stop(engines, run):
    with engines[1].begin() as connection:
        connection.execute(text(
            "SELECT lab.request_director_run_stop(:run,'fixture:mode-stream043','local')"
        ), {"run": run})


def _arguments(run, owner):
    return {"run": run, "generation": owner.generation if owner else None,
            "sha": owner.execution_sha256 if owner else None, "empty": owner is None}


def _prepare(connection, run, payload):
    connection.execute(text("SELECT lab.prepare_unstarted_baseline_stop(:run,:sha)"),
                       {"run": run, "sha": payload})


def _report(connection, run, purpose, *, extra=None):
    document = {"schema": f"lab.{purpose}-report.v1", "run_id": str(run),
                "purpose": purpose, "status": "stopped", **(extra or {})}
    if purpose == "mode-stream":
        document["program_version"] = "mode-stream.v1"
    digest = hashlib.sha256(canonical_execution_bytes(document)).hexdigest()
    connection.execute(text(
        "INSERT INTO lab.reports(run_id,report_sha256,report_json,verified_at)"
        " VALUES(:run,:sha,CAST(:document AS jsonb),clock_timestamp())"
    ), {"run": run, "sha": digest, "document": json.dumps(document)})
    return digest


def test_admitted_stream_seals_empty_and_returns_only_ordered_bound_evidence(engines):
    run, _, owner, process = _seed(engines)
    _seal(engines, run, owner)
    with engines[1].begin() as connection:
        assert_execution_owner_transaction(connection, owner, run_id=run)
        connection.execute(text(
            "INSERT INTO lab.run_events(event_id,run_id,event_type,event_json,created_at)"
            " VALUES(:event,:run,'director.checkpoint',CAST(:receipt AS jsonb),clock_timestamp())"
        ), [{"event": uuid4(), "run": run, "receipt": json.dumps({"sequence": n})}
            for n in (1, 0)])
    with engines[3].begin() as connection:
        evidence = connection.execute(EVIDENCE, _arguments(run, owner)).scalar_one()
    assert evidence == {
        "run_id": str(run), "checkpoints": [{"sequence": 0}, {"sequence": 1}],
        "owners": [{"generation": 1, "execution_sha256": owner.execution_sha256}],
        "owner": asdict(process),
    }
    with pytest.raises(DBAPIError, match="permission denied"), engines[3].begin() as connection:
        connection.execute(text("SELECT event_json FROM lab.run_events WHERE run_id=:run"),
                           {"run": run})


@pytest.mark.parametrize("changed", [
    {"purpose": "research"}, {"provider": "fake-json"}, {"program_version": "mode-stream.v2"},
    {"budget": {"experiments": 1, "model_tokens": 0, "wall_seconds": 600}},
    {"budget": {"experiments": 0, "model_tokens": "0", "wall_seconds": 600}},
])
def test_active_empty_seal_rejects_other_or_malformed_intents(engines, changed):
    run, _, owner, _ = _seed(engines, changed=changed)
    with pytest.raises(DBAPIError, match="empty task plans"):
        _seal(engines, run, owner)


@pytest.mark.parametrize("role", [0, 1, 2])
def test_evidence_rpc_rejects_non_scorer_sessions(engines, role):
    run, _, owner, _ = _seed(engines)
    _seal(engines, run, owner)
    with pytest.raises(DBAPIError), engines[role].begin() as connection:
        connection.execute(EVIDENCE, _arguments(run, owner))


@pytest.mark.parametrize("changed", [
    {"generation": 2}, {"sha": "f" * 64}, {"generation": None},
    {"empty": True}, {"empty": None},
])
def test_evidence_rejects_stale_or_ambiguous_execution_pairs(engines, changed):
    run, _, owner, _ = _seed(engines)
    _seal(engines, run, owner)
    with pytest.raises(DBAPIError), engines[3].begin() as connection:
        connection.execute(EVIDENCE, {**_arguments(run, owner), **changed})


@pytest.mark.parametrize("purpose", ["baseline", "mode-stream"])
def test_unstarted_stop_preserves_existing_baseline_path_and_atomic_publication(engines, purpose):
    run, payload, _, _ = _seed(engines, owned=False, changed={"purpose": purpose})
    _stop(engines, run)
    with engines[3].begin() as connection:
        _prepare(connection, run, payload)
        if purpose == "mode-stream":
            assert connection.execute(EVIDENCE, _arguments(run, None)).scalar_one() == {
                "run_id": str(run), "checkpoints": [], "owners": [], "owner": None,
            }
        digest = _report(connection, run, purpose)
        connection.execute(text(
            "UPDATE lab.runs SET state='stopped',stop_requested=false,report_sha256=:sha"
            " WHERE run_id=:run"
        ), {"run": run, "sha": digest})
    with engines[1].connect() as connection:
        assert connection.execute(text("SELECT state FROM lab.runs WHERE run_id=:run"),
                                  {"run": run}).scalar_one() == "stopped"


@pytest.mark.parametrize("purpose,report_purpose,extra", [
    ("baseline", "mode-stream", {}), ("mode-stream", "baseline", {}),
    ("mode-stream", "mode-stream", {"admitted_generation": None, "execution_sha256": None}),
])
def test_ownerless_reports_must_match_immutable_intent_and_omit_execution_pair(
    engines, purpose, report_purpose, extra,
):
    run, payload, _, _ = _seed(engines, owned=False, changed={"purpose": purpose})
    _stop(engines, run)
    with pytest.raises(DBAPIError, match="ownerless baseline stop report"), \
            engines[3].begin() as connection:
        _prepare(connection, run, payload)
        _report(connection, run, report_purpose, extra=extra)


@pytest.mark.parametrize(
    "table", ["scorer.run_tasks", "lab.holdout_reservations"],
)
def test_stream_rejects_research_rows_even_before_its_empty_seal(engines, table):
    run, _, owner, _ = _seed(engines)
    # The reservation table's migrator owner can exercise its row guard directly.
    # run_tasks has an actual Planner INSERT grant and keeps its owner context.
    engine = engines[2 if table == "scorer.run_tasks" else 0]
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT has_table_privilege(current_user,:table,'INSERT')"), {"table": table},
        ).scalar_one()
    # Before constraints, the existing intent trigger rejects every row for this
    # run. The test intentionally omits unrelated required research fields.
    with pytest.raises(DBAPIError, match="mode stream intent rejects"), \
            engine.begin() as connection:
        if table == "scorer.run_tasks":
            assert_execution_owner_transaction(connection, owner, run_id=run)
        connection.execute(text(f"INSERT INTO {table}(run_id) VALUES(:run)"), {"run": run})


def test_stream_rejects_director_experiment_registration_before_its_empty_seal(engines):
    run, _, owner, _ = _seed(engines)
    token = bind_execution_owner(owner)
    try:
        # The first row trigger requires a Director session and a valid baseline
        # before calibration freeze. Use the real granted SECURITY DEFINER RPC
        # with every required proposal field to reach the subsequent intent guard.
        with pytest.raises(DBAPIError, match="mode stream intent rejects"):
            register_experiment(
                engines[1], experiment_id=f"exp_{uuid4().hex}", run_id=str(run),
                sequence=0, experiment_number=None, kind="baseline", baseline_name="robust_z",
                parent_experiment_id=None, candidate_sha256="e" * 64,
                candidate_blob_sha256="e" * 64, inputs_sha256="f" * 64,
                move_type="detector", system="S1", predicted_delta=None,
                hypothesis="synthetic mode stream experiment rejection fixture",
                proposal={"schema": "baseline.registration.v1", "fixture_only": True},
            )
    finally:
        reset_execution_owner(token)
    with engines[1].connect() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM lab.experiments WHERE run_id=:run"), {"run": run},
        ).scalar_one() == 0


def test_oversized_checkpoint_prefix_fails_without_returning_truncated_evidence(engines):
    run, _, owner, _ = _seed(engines)
    _seal(engines, run, owner)
    with engines[1].begin() as connection:
        assert_execution_owner_transaction(connection, owner, run_id=run)
        connection.execute(text(
            "INSERT INTO lab.run_events(event_id,run_id,event_type,event_json,created_at) "
            "SELECT gen_random_uuid(),:run,'director.checkpoint',jsonb_build_object('sequence',n),"
            "clock_timestamp() FROM generate_series(0,1026) n"
        ), {"run": run})
    with pytest.raises(DBAPIError, match="checkpoint count"), engines[3].begin() as connection:
        connection.execute(EVIDENCE, _arguments(run, owner))


def test_paired_stop_uses_first_stop_event_and_never_renews_cleanup_time(engines):
    run, _, owner, _ = _seed(engines)
    _seal(engines, run, owner)
    with engines[1].begin() as connection:
        connection.execute(text(
            "INSERT INTO lab.run_events(event_id,run_id,event_type,event_json,created_at) "
            "VALUES(gen_random_uuid(),:run,'run.stop_requested','{}',"
            "clock_timestamp()-interval '121 seconds')"
        ), {"run": run})
    _stop(engines, run)
    with pytest.raises(DBAPIError, match="original deadline expired"), \
            engines[3].begin() as connection:
        connection.execute(EVIDENCE, _arguments(run, owner))


def test_paired_stop_stays_inside_original_execution_deadline(engines):
    run, _, owner, _ = _seed(engines, changed={
        "budget": {"experiments": 0, "model_tokens": 0, "wall_seconds": 1},
    })
    _seal(engines, run, owner)
    _stop(engines, run)
    time.sleep(1.1)
    with pytest.raises(DBAPIError, match="original deadline expired"), \
            engines[3].begin() as connection:
        connection.execute(EVIDENCE, _arguments(run, owner))


def test_stopped_claimed_stream_cannot_use_ownerless_assertion(engines):
    run, payload, owner, _ = _seed(engines)
    _seal(engines, run, owner)
    _stop(engines, run)
    with pytest.raises(DBAPIError, match="execution evidence"), engines[3].begin() as connection:
        _prepare(connection, run, payload)
    with engines[3].begin() as connection:
        evidence = connection.execute(EVIDENCE, _arguments(run, owner)).scalar_one()
    assert evidence["owners"] == [{"generation": 1, "execution_sha256": owner.execution_sha256}]


@pytest.mark.parametrize("expired", ["first_stop", "original_run"])
def test_unstarted_stream_retries_preserve_original_stop_and_run_deadlines(engines, expired):
    changed = {"budget": {"experiments": 0, "model_tokens": 0, "wall_seconds": 1}}
    run, payload, _, _ = _seed(
        engines, owned=False, changed=changed if expired == "original_run" else None,
    )
    if expired == "first_stop":
        with engines[1].begin() as connection:
            connection.execute(text(
                "INSERT INTO lab.run_events(event_id,run_id,event_type,event_json,created_at) "
                "VALUES(gen_random_uuid(),:run,'run.stop_requested','{}',"
                "clock_timestamp()-interval '121 seconds')"
            ), {"run": run})
    _stop(engines, run)
    if expired == "original_run":
        time.sleep(1.1)
    with pytest.raises(DBAPIError, match="original deadline expired"), \
            engines[3].begin() as connection:
        _prepare(connection, run, payload)
        connection.execute(EVIDENCE, _arguments(run, None))
