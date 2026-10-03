"""Canceled provider evidence never fabricates consumption or refunds budget."""

from copy import deepcopy
from hashlib import sha256
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from lab.director import recovery
from lab.director.cancelled_attempt_audit import cancelled_attempt_audit
from lab.director.journal import canonical_bytes
from lab.director.ownership import ExecutionOwner


def setup_audit():
    owner = ExecutionOwner(uuid4(), 1, "a" * 32, "b" * 64)
    reservation_key = f"proposal-budget-reservation:{owner.run_id}:0"
    started_key = "provider-attempt:0:0:started"
    payload = dict(
        run_id=str(owner.run_id), ordinal=0, reservation_id=str(uuid4()),
        model_tokens=18432, wall_seconds=180,
    )
    checkpoints = {}
    for sequence, (key, phase, document) in enumerate([
        (reservation_key, "proposal_budget_reserved", payload),
        (started_key, "provider_attempt", dict(
            run_id=str(owner.run_id), ordinal=0, attempt_index=0, status="started",
        )),
    ]):
        digest = sha256(canonical_bytes(document)).hexdigest()
        checkpoints[key] = dict(
            receipt=dict(key=key, phase=phase, sequence=sequence,
                         payload_sha256=digest, blob_sha256=digest),
            payload=document,
        )
    lease = MagicMock()
    lease.read_checkpoint.side_effect = lambda *, key, **_kwargs: checkpoints.get(key)
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value

    def execute(statement, _parameters):
        result = MagicMock()
        result.scalars.return_value.all.return_value = (
            [reservation_key] if "proposal_budget_reserved" in str(statement) else [started_key]
        )
        return result

    connection.execute.side_effect = execute
    arguments = dict(lease=lease, owner=owner,
                     request={"budget": {"model_tokens": 30000, "wall_seconds": 1800}})
    return engine, arguments, checkpoints


def test_unknown_canceled_usage_retains_exact_reservation_and_checkpoint_provenance(tmp_path):
    engine, arguments, checkpoints = setup_audit()
    before = deepcopy(checkpoints)
    first = cancelled_attempt_audit(engine, **arguments, artifact_root=tmp_path)
    retry = cancelled_attempt_audit(engine, **arguments, artifact_root=tmp_path)
    assert first == retry
    assert checkpoints == before
    assert first["state"] == "recorded"
    assert first["actual_model_tokens"] is first["actual_model_wall_seconds"] is None
    assert not first["conservation_verified"] and not first["accounting_mutated"]
    assert first["refund_model_tokens"] == first["refund_wall_seconds"] == 0
    retained = first["retained_reservations"][0]
    assert retained["model_tokens"] == 18432 and retained["wall_seconds"] == 180
    assert retained["disposition"] == "retained_unknown_consumption"
    assert retained["provider_checkpoints"][0]["payload_sha256"] == (
        checkpoints["provider-attempt:0:0:started"]["receipt"]["payload_sha256"]
    )
    engine.begin.assert_not_called()


@pytest.mark.parametrize("corruption", ["wrong_run", "wrong_ordinal", "bad_hash", "over_cap"])
def test_corrupt_original_evidence_does_not_claim_accounting(tmp_path, corruption):
    engine, arguments, checkpoints = setup_audit()
    reservation = next(iter(checkpoints.values()))
    if corruption == "wrong_run":
        reservation["payload"]["run_id"] = str(uuid4())
    elif corruption == "wrong_ordinal":
        reservation["payload"]["ordinal"] = 1
    elif corruption == "bad_hash":
        reservation["receipt"]["blob_sha256"] = "c" * 64
    else:
        arguments["request"]["budget"]["model_tokens"] = 100
    result = cancelled_attempt_audit(engine, **arguments, artifact_root=tmp_path)
    assert result["state"] == "unavailable"
    assert result["retained_reservations"] == []
    assert result["actual_model_tokens"] is None
    engine.begin.assert_not_called()


def receipt_fixture():
    owner = ExecutionOwner(uuid4(), 1, "a" * 32, "b" * 64)
    identity = dict(state="stopped", report_sha256="c" * 64, current_generation=1,
                    worker_invocation_id=owner.invocation_id,
                    execution_sha256=owner.execution_sha256)
    existing = dict(run_id=owner.run_id, request_sha256="d" * 64,
                    action="stop_and_finalize", state="started",
                    **{name: None for name in (
                        "owner_pid", "owner_start_ticks", "owner_boot_id", "owner_unit",
                        "owner_invocation_id", "owner_cgroup",
                    )})
    engine = MagicMock()
    connection = engine.begin.return_value.__enter__.return_value

    def execute(statement, _parameters):
        reply = MagicMock()
        reply.mappings.return_value.one_or_none.return_value = (
            existing if "SELECT * FROM lab.director_recoveries" in str(statement) else identity
        )
        return reply

    connection.execute.side_effect = execute
    kwargs = dict(recovery_id=uuid4(), run_id=owner.run_id, request_sha256="d" * 64,
                  observed_state="stop_requested", owner=None, state="completed",
                  execution_owner=owner, result={"report_sha256": "c" * 64,
                    "cancelled_model_budget_audit": {"actual_model_tokens": None}})
    return engine, connection, kwargs, identity, existing


@pytest.mark.parametrize("field", ["current_generation", "worker_invocation_id",
                                    "execution_sha256", "report_sha256", "state"])
def test_stale_worker_cannot_write_terminal_cancellation_audit(field):
    engine, connection, kwargs, identity, _existing = receipt_fixture()
    identity[field] = 2 if field == "current_generation" else "changed"
    with pytest.raises(ValueError, match="execution generation changed"):
        recovery._persist_receipt(engine, **kwargs)
    assert not any(
        str(call.args[0]).startswith("UPDATE ") for call in connection.execute.call_args_list
    )


def test_terminal_audit_is_saved_inside_existing_owner_bound_recovery_receipt():
    engine, connection, kwargs, _identity, _existing = receipt_fixture()
    result = recovery._persist_receipt(engine, **kwargs)
    assert result["recovery_state"] == "completed"
    assert result["cancelled_model_budget_audit"]["actual_model_tokens"] is None
    queries = [str(call.args[0]) for call in connection.execute.call_args_list]
    assert "FOR UPDATE OF r" in queries[-2]
    assert queries[-1].startswith("UPDATE lab.director_recoveries SET")


def test_completed_historical_receipt_retry_is_not_rewritten():
    engine, connection, kwargs, identity, existing = receipt_fixture()
    original = {"action": "finalized_stopped", "historical": True}
    existing.update(state="completed", result_json=original,
                    result_sha256=sha256(canonical_bytes(original)).hexdigest())
    identity["current_generation"] = 2
    assert recovery._persist_receipt(engine, **kwargs) == {
        **original, "recovery_state": "completed", "replayed": True,
    }
    assert connection.execute.call_count == 1
