"""CPU-only contracts for immutable Director execution generations."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from lab.director.ownership import (
    ExecutionContract,
    ExecutionOwner,
    OwnerProcessIdentity,
    active_execution_owner,
    assert_execution_owner_transaction,
    canonical_execution_bytes,
    claim_initial_execution,
    owned_execution,
)


def _request(*, wall_seconds: int = 120) -> dict[str, object]:
    return {
        "idempotency_key": "private-key-is-not-execution-input",
        "track": "anomaly",
        "suite": "trusted-suite.v1",
        "budget": {"experiments": 3, "wall_seconds": wall_seconds, "model_tokens": 0},
        "provider": "fake-json",
        "provider_config_sha256": None,
        "scenario_sha256": "b" * 64,
    }


def _contract(*, request: dict[str, object] | None = None) -> ExecutionContract:
    selected = request or _request()
    return ExecutionContract.build(
        run_id=uuid4(),
        request=selected,
        request_sha256="a" * 64,
        suite_id="trusted-suite.v1",
        suite_version=1,
        suite_manifest_sha256="c" * 64,
        registry_entry_sha256="d" * 64,
        harness_sha256="e" * 64,
        image_sha256="f" * 64,
    )


def _process(contract: ExecutionContract) -> OwnerProcessIdentity:
    return OwnerProcessIdentity(
        payload_sha256=contract.request_sha256,
        worker_pid=2345,
        worker_start_ticks=987654,
        worker_boot_id="12345678-1234-1234-1234-123456789abc",
        worker_unit=f"swapp-ai-scientist-director-dispatch-{contract.run_id.hex}.service",
        worker_invocation_id="1" * 32,
        worker_cgroup=(
            "/user.slice/user-1000.slice/user@1000.service/app.slice/"
            f"swapp-ai-scientist-director-dispatch-{contract.run_id.hex}.service"
        ),
    )


def test_execution_contract_is_canonical_and_excludes_idempotency_key() -> None:
    contract = _contract()
    expected = dict(contract.execution_json)
    assert "idempotency_key" not in expected["request"]
    import hashlib

    assert (
        contract.execution_sha256 == hashlib.sha256(canonical_execution_bytes(expected)).hexdigest()
    )
    assert json.loads(canonical_execution_bytes(expected)) == expected
    assert contract.wall_seconds == 120


@pytest.mark.parametrize("wall", [True, 0, -1, 14_401, "120"])
def test_execution_contract_rejects_invalid_wall_budget(wall: object) -> None:
    with pytest.raises(ValueError, match="wall budget"):
        _contract(request={**_request(), "budget": {"wall_seconds": wall}})


def test_execution_contract_rejects_malformed_verified_pins() -> None:
    with pytest.raises(ValueError, match="pin"):
        ExecutionContract.build(
            run_id=uuid4(),
            request=_request(),
            request_sha256="a" * 64,
            suite_id="trusted-suite.v1",
            suite_version=1,
            suite_manifest_sha256="bad",
            registry_entry_sha256="d" * 64,
            harness_sha256="e" * 64,
            image_sha256="f" * 64,
        )


def test_owner_generation_is_frozen_and_context_restores() -> None:
    owner = ExecutionOwner(uuid4(), 1, "1" * 32, "a" * 64)
    assert active_execution_owner() is None
    with owned_execution(owner) as bound:
        assert bound is owner
        assert active_execution_owner() is owner
    assert active_execution_owner() is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"generation": True},
        {"generation": 0},
        {"invocation_id": "bad"},
        {"execution_sha256": "z" * 64},
    ],
)
def test_owner_rejects_invalid_generation_identity(kwargs: dict[str, object]) -> None:
    values: dict[str, object] = {
        "run_id": uuid4(),
        "generation": 1,
        "invocation_id": "1" * 32,
        "execution_sha256": "a" * 64,
    }
    values.update(kwargs)
    with pytest.raises(ValueError):
        ExecutionOwner(**values)  # type: ignore[arg-type]


def test_process_identity_binds_exact_unit_and_cgroup() -> None:
    contract = _contract()
    with pytest.raises(ValueError, match="cgroup"):
        OwnerProcessIdentity(
            payload_sha256=contract.request_sha256,
            worker_pid=2345,
            worker_start_ticks=987654,
            worker_boot_id="12345678-1234-1234-1234-123456789abc",
            worker_unit=f"swapp-ai-scientist-director-dispatch-{contract.run_id.hex}.service",
            worker_invocation_id="1" * 32,
            worker_cgroup="/system.slice/unrelated.service",
        )


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one(self) -> object:
        return self.value


class _FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(self, statement, parameters=None):
        self.calls.append((str(statement), dict(parameters or {})))
        if "claim_initial_director_execution" in str(statement):
            args = dict(parameters or {})
            return _ScalarResult(
                {
                    "state": "running",
                    "newly_claimed": True,
                    "generation": 1,
                    "worker_invocation_id": args["worker_invocation_id"],
                }
            )
        return _ScalarResult(None)


class _FakeEngine:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    @contextmanager
    def begin(self):
        yield self.connection


def test_initial_claim_passes_contract_and_process_as_one_rpc() -> None:
    contract = _contract()
    process = _process(contract)
    connection = _FakeConnection()
    owner = claim_initial_execution(_FakeEngine(connection), contract=contract, process=process)
    assert owner == ExecutionOwner(
        contract.run_id, 1, process.worker_invocation_id, contract.execution_sha256
    )
    assert len(connection.calls) == 1
    statement, params = connection.calls[0]
    assert "claim_initial_director_execution" in statement
    assert params["run_id"] == contract.run_id
    assert params["execution_sha256"] == contract.execution_sha256
    assert json.loads(params["execution_json"]) == contract.execution_json
    assert params["worker_invocation_id"] == process.worker_invocation_id


def test_initial_claim_rejects_process_bound_to_different_request() -> None:
    contract = _contract()
    connection = _FakeConnection()
    process = replace(_process(contract), payload_sha256="0" * 64)
    with pytest.raises(ValueError, match="request digest"):
        claim_initial_execution(_FakeEngine(connection), contract=contract, process=process)
    assert connection.calls == []


def test_initial_claim_rejects_inflated_execution_wall_without_rpc() -> None:
    contract = _contract()
    inflated_document = {**contract.execution_json, "wall_seconds": contract.wall_seconds + 1}
    inflated = replace(
        contract,
        execution_json=inflated_document,
        execution_sha256=__import__("hashlib")
        .sha256(canonical_execution_bytes(inflated_document))
        .hexdigest(),
        wall_seconds=contract.wall_seconds + 1,
    )
    connection = _FakeConnection()
    with pytest.raises(ValueError, match="immutable request"):
        claim_initial_execution(
            _FakeEngine(connection), contract=inflated, process=_process(contract)
        )
    assert connection.calls == []


def test_owner_context_rejects_mutator_targeting_another_run_before_db_calls() -> None:
    owner = ExecutionOwner(uuid4(), 1, "3" * 32, "a" * 64)
    connection = _FakeConnection()
    with pytest.raises(RuntimeError, match="another run"):
        assert_execution_owner_transaction(connection, owner, run_id=uuid4())
    assert connection.calls == []


def test_migration_binds_run_plan_and_row_fences_to_mutation_target() -> None:
    migration = (
        Path(__file__).parents[1] / "lab/db/migrations/versions/0025_director_generations.py"
    ).read_text(encoding="utf-8")
    assert "owner_run IS NOT NULL AND owner_run IS DISTINCT FROM p_run_id" in migration
    assert "PERFORM lab.assert_director_owner_context(target_run);" in migration
    assert "request_wall_seconds IS DISTINCT FROM p_wall_seconds" in migration
    assert "execution_wall_seconds IS DISTINCT FROM request_wall_seconds" in migration
    assert "current_run.request_json->'budget'->'wall_seconds'" in migration


def test_migration_preserves_stopped_closure_and_baseline_rpc_boundaries() -> None:
    migration = (
        Path(__file__).parents[1] / "lab/db/migrations/versions/0025_director_generations.py"
    ).read_text(encoding="utf-8")
    statement_guard = migration.split(
        "CREATE FUNCTION lab.guard_scorer_owned_statement()", maxsplit=1
    )[1].split("CREATE FUNCTION lab.guard_scorer_owned_row()", maxsplit=1)[0]
    row_guard = migration.split(
        "CREATE FUNCTION lab.guard_scorer_owned_row()", maxsplit=1
    )[1].split("for table, operation in (", maxsplit=1)[0]
    assert (
        "'score_jobs','task_terminal_outcomes'," in statement_guard
        and "'task_completions') AND" in statement_guard
    )
    assert "lab.assert_director_generation_identity(" in statement_guard
    assert "Planner cannot enqueue a score job after stop" in row_guard
    assert "NEW.error_code IS NOT NULL" in row_guard
    assert "'claim_invocation_id','error_code','updated_at'" in row_guard
    assert "OLD.state='stop_requested' AND NEW.state='stop_requested'" in migration
    assert "NEW.task_plan_sha256=" in migration
    assert "ALTER FUNCTION lab.register_baseline_operation(uuid,jsonb)" in migration
    assert "PERFORM lab.assert_director_owner_context(p_run_id)" in migration
    assert "ALTER FUNCTION lab.prepare_unstarted_baseline_stop(uuid,text)" in migration
    assert "PERFORM lab.assert_scorer_empty_baseline_stop(p_run_id)" in migration
    assert "request_director_run_stop(uuid,text,text)" in migration
    assert "p_owner_id text" in migration


def test_transaction_fence_sets_all_owner_context_on_same_connection() -> None:
    owner = ExecutionOwner(uuid4(), 4, "2" * 32, "f" * 64)
    connection = _FakeConnection()
    with owned_execution(owner):
        assert_execution_owner_transaction(connection)
    assert len(connection.calls) == 5
    config_calls = connection.calls[:4]
    assert all("set_config" in statement for statement, _ in config_calls)
    assert {parameters["key"] for _, parameters in config_calls} == {
        "lab.owner_run_id",
        "lab.owner_generation",
        "lab.owner_invocation_id",
        "lab.owner_execution_sha256",
    }
    assert "assert_director_owner_context" in connection.calls[4][0]
    assert connection.calls[4][1]["run_id"] == owner.run_id


def test_transaction_fence_rejects_missing_captured_owner() -> None:
    with pytest.raises(RuntimeError, match="captured execution owner"):
        assert_execution_owner_transaction(_FakeConnection())
