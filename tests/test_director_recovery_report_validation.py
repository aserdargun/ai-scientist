"""Report contract regressions for read-only terminal recovery inspection."""

from __future__ import annotations

import hashlib
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from lab.director import recovery
from lab.scorer.baseline_report import validate_baseline_report


def _baseline_report(run_id: UUID, status: str) -> dict[str, Any]:
    completed = status == "completed"
    return {
        "schema": "lab.baseline-report.v1",
        "run_id": str(run_id),
        "purpose": "baseline",
        "status": status,
        "calibration_complete": completed,
        "budget_verified": completed,
        "request_sha256": "a" * 64,
        "suite_id": "fixture.suite",
        "suite_version": 1,
        "suite_manifest_sha256": "b" * 64,
        "harness_sha256": "c" * 64,
        "image_sha256": "d" * 64,
        "task_plan_sha256": "e" * 64,
        "task_plan_count": 9 if completed else 0,
        "calibration_sha256": "f" * 64 if completed else None,
        "algorithms": ["robust_z", "iforest", "ecod_train_frozen"],
        "seeds": [0, 1, 2],
        "task_count": 1 if completed else 0,
        "score_count": 9 if completed else 0,
        "budget_receipt": {},
        "model_usage": {"input_tokens": 0, "output_tokens": 0, "calls": 0},
        "baseline_records": [],
        "experiment_records": [
            {
                "experiment_id": f"exp_{index:032x}",
                "baseline_name": name,
                "status": "scored",
                "experiment_sha256": "1" * 64,
                "trajectory_sha256": "2" * 64,
            }
            for index, name in enumerate(("robust_z", "iforest", "ecod_train_frozen"))
        ] if completed else [],
        "task_terminal_outcomes": [],
    }


def _inspect(
    monkeypatch: pytest.MonkeyPatch,
    run_id: UUID,
    report: dict[str, Any],
    *,
    purpose: str | None = "baseline",
    status: str = "stopped",
    mismatched_hash: str | None = None,
) -> dict[str, Any]:
    digest = hashlib.sha256(recovery.canonical_bytes(report)).hexdigest()
    director = MagicMock()
    connection = director.connect.return_value.__enter__.return_value
    run = {"state": status, "payload_sha256": "a" * 64, "purpose": purpose,
           "report_sha256": "0" * 64 if mismatched_hash == "run" else digest}
    stored_report = {
        "report_json": report,
        "report_sha256": "0" * 64 if mismatched_hash == "report" else digest,
    }

    def execute(statement: Any, parameters: Any) -> MagicMock:
        result = MagicMock()
        sql = str(statement)
        if "FROM lab.runs" in sql:
            assert "request_json->>'purpose' AS purpose" in sql
            result.mappings.return_value.one_or_none.return_value = run
        elif "FROM lab.reports" in sql:
            result.mappings.return_value.one_or_none.return_value = stored_report
        elif "FROM lab.experiments" in sql:
            result.mappings.return_value.all.return_value = []
        else:
            raise AssertionError(f"unexpected Director query: {sql}")
        return result

    connection.execute.side_effect = execute
    monkeypatch.setattr(recovery, "_current_owner_row", lambda *args: None)
    planner = MagicMock()
    planner_connection = planner.connect.return_value.__enter__.return_value
    planner_connection.execute.return_value.mappings.return_value.all.return_value = []
    planner_connection.execute.return_value.scalar_one.return_value = 0
    return recovery.inspect_recovery(director, planner, run_id)


@pytest.mark.parametrize("status", ["completed", "failed", "stopped"])
def test_terminal_baseline_accepts_production_report_contract(
    monkeypatch: pytest.MonkeyPatch, status: str,
) -> None:
    run_id = uuid4()
    report = validate_baseline_report(_baseline_report(run_id, status))
    result = _inspect(monkeypatch, run_id, report, status=status)
    assert result["report_valid"] is True
    assert result["can_finalize"] is True
    assert result["recovery_blockers"] == []


@pytest.mark.parametrize(
    "updates",
    [
        {"schema": "lab.report.v1"},
        {"purpose": "research"},
        {"run_id": str(uuid4())},
        {"status": "failed"},
        {"model_usage": {"input_tokens": 1, "output_tokens": 0, "calls": 1}},
        {"calibration_complete": True},
        {"task_plan_count": 1},
        {"task_plan_sha256": None},
        {"unexpected": True},
    ],
)
def test_terminal_baseline_rejects_hash_bound_invalid_report(
    monkeypatch: pytest.MonkeyPatch, updates: dict[str, Any],
) -> None:
    run_id = uuid4()
    report = {**_baseline_report(run_id, "stopped"), **updates}
    result = _inspect(monkeypatch, run_id, report)
    assert result["report_valid"] is False
    assert result["can_finalize"] is False
    assert result["recovery_blockers"] == ["terminal_report_missing_or_invalid"]


@pytest.mark.parametrize("mismatched_hash", ["report", "run"])
def test_terminal_baseline_rejects_mismatched_stored_hash(
    monkeypatch: pytest.MonkeyPatch, mismatched_hash: str,
) -> None:
    run_id = uuid4()
    result = _inspect(
        monkeypatch, run_id, _baseline_report(run_id, "stopped"),
        mismatched_hash=mismatched_hash,
    )
    assert result["report_valid"] is False
    assert result["can_finalize"] is False


@pytest.mark.parametrize("purpose", ["research", None])
@pytest.mark.parametrize("schema", ["lab.report.v1", "lab.baseline-report.v1"])
def test_research_recovery_retains_research_schema_requirement(
    monkeypatch: pytest.MonkeyPatch, purpose: str | None, schema: str,
) -> None:
    run_id = uuid4()
    report = {"schema": schema, "run_id": str(run_id), "status": "stopped"}
    result = _inspect(monkeypatch, run_id, report, purpose=purpose)
    assert result["report_valid"] is (schema == "lab.report.v1")
    assert result["can_finalize"] is (schema == "lab.report.v1")
