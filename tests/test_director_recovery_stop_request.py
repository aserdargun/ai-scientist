"""Recovery SQL routing; actual PostgreSQL fences are covered by live probes."""

from __future__ import annotations

from dataclasses import asdict
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from lab.director import recovery


@pytest.mark.parametrize("initial_state", ["running", "stop_requested"])
@pytest.mark.parametrize("children_blocked", [True, False])
def test_recovery_stop_preserves_principal_and_returns_pending(
    monkeypatch: pytest.MonkeyPatch, initial_state: str, children_blocked: bool
) -> None:
    """Exercise both recovery branches without a current execution-owner context."""
    run_id = uuid4()
    principal = f"recovery-regression:{run_id.hex}"
    owner = recovery.OwnerGeneration(
        payload_sha256="c" * 64,
        worker_pid=4321,
        worker_start_ticks=12345,
        worker_boot_id=str(uuid4()),
        worker_unit=recovery.OWNER_DRAIN_UNIT,
        worker_invocation_id=uuid4().hex,
        worker_cgroup=f"/synthetic-fixture/{recovery.OWNER_DRAIN_UNIT}",
    )
    run = {
        "state": initial_state, "payload_sha256": owner.payload_sha256,
        "report_sha256": None, "request_json": {}, "owner_id": principal, "origin": "aos",
    }
    lease = MagicMock()
    death_proofs = []
    stop_calls = []

    def prove_dead(*args):
        death_proofs.append(args)
        return True

    def execute(statement, parameters):
        query = str(statement)
        result = MagicMock()
        if query.startswith("SELECT state,payload_sha256"):
            result.mappings.return_value.one_or_none.return_value = run.copy()
        elif "SELECT generation.generation" in query:
            result.mappings.return_value.one_or_none.return_value = None
        elif query.startswith("SELECT request_json"):
            result.scalar_one.return_value = run["request_json"]
        elif query.startswith("SELECT state FROM"):
            result.scalar_one.return_value = run["state"]
        elif query == "SELECT lab.request_director_run_stop(:id,:owner_id,:origin)":
            assert parameters == {"id": run_id, "owner_id": principal, "origin": "aos"}
            assert death_proofs == [(owner, run_id)]
            lease.__enter__.assert_called_once_with()
            stop_calls.append(parameters)
            run["state"] = "stop_requested"
        else:
            # PostgreSQL's statement trigger rejects even zero-row raw UPDATEs.
            raise AssertionError(f"unexpected unowned SQL: {query}")
        return result

    connection = MagicMock()
    connection.execute.side_effect = execute
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = connection
    engine.begin.return_value.__enter__.return_value = connection
    monkeypatch.setattr(recovery, "DirectorRunLease", lambda *args: lease)
    monkeypatch.setattr(recovery, "_current_owner_row", lambda *args: asdict(owner))
    monkeypatch.setattr(recovery, "_owner_is_proven_dead", prove_dead)
    monkeypatch.setattr(recovery, "_ensure_recovery_intent", lambda *args, **kwargs: None)
    monkeypatch.setattr(recovery, "_insert_event", lambda *args: None)
    monkeypatch.setattr(
        recovery, "_persist_receipt",
        lambda *args, **kwargs: {**kwargs["result"], "recovery_state": kwargs["state"]},
    )
    monkeypatch.setattr(
        recovery, "inspect_recovery",
        lambda *args, **kwargs: {
            "owner_proven_dead": True, "recovery_blockers": ["synthetic_ledger_pending"]
        },
    )
    monkeypatch.setattr(
        recovery, "_child_blockers",
        lambda *args, **kwargs: ["gpu_runtime_state_unavailable"] if children_blocked else [],
    )
    recovery_id = recovery._recovery_id(
        run_id, recovery._request_sha256(run_id, "stop_and_finalize")
    )
    result = recovery.apply_stop_and_finalize(
        engine, engine, run_id=run_id, recovery_id=recovery_id
    )
    assert result["recovery_state"] == "pending"
    assert result["reason"] == (
        "unresolved_children" if children_blocked else "ledger_not_terminal"
    )
    if children_blocked:
        assert result["child_blockers"] == ["gpu_runtime_state_unavailable"]
    assert run["state"] == "stop_requested"
    assert len(stop_calls) == int(children_blocked or initial_state == "running")
    lease.__exit__.assert_called_once()
