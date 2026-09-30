"""Bounded CPU coverage for report-only closure and immutable retry behavior."""

from __future__ import annotations

import ast
from pathlib import Path
from uuid import uuid4

from sqlalchemy import insert, text
from test_api_and_scorer import _engine

from lab.db.schema import runs
from lab.db.task_plan import canonical_task_plan_digest
from lab.scorer.service import IndependentScorer


def test_published_stop_report_retries_identically_through_finalizer() -> None:
    engine = _engine()
    run_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            insert(runs).values(
                run_id=run_id,
                origin="local",
                owner_id="fixture:037",
                idempotency_key=f"terminal-{run_id}",
                payload_sha256="a" * 64,
                request_json={},
                state="stop_requested",
                stop_requested=True,
                task_plan_sha256=canonical_task_plan_digest([]),
                task_plan_count=0,
            )
        )
    scorer = IndependentScorer(engine, harness_sha256="b" * 64)
    first = scorer.finalize_if_ready(run_id=run_id)
    assert first is not None and first[0]["status"] == "stopped"
    assert scorer.finalize_if_ready(run_id=run_id) == first
    assert scorer.complete_run(run_id=run_id) == first
    engine.dispose()


def test_terminal_migration_sql_has_no_accidental_driver_bind_parameters() -> None:
    path = Path(__file__).parents[1] / "lab/db/migrations/versions/0027_terminal_finalization.py"
    checked = 0
    for node in ast.walk(ast.parse(path.read_text())):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            assert not text(node.args[0].value)._bindparams
            checked += 1
    assert checked > 0


def test_baseline_window_commits_before_a_failed_publication() -> None:
    """The real SQL RPC owns validation; this checks its transaction boundary."""
    from contextlib import contextmanager
    from types import SimpleNamespace

    import pytest

    run_id = uuid4()
    captured = []
    committed = []

    class Preparation:
        def execute(self, statement, params=None):
            if params is None:
                return SimpleNamespace(
                    first=lambda: SimpleNamespace(
                        state="running", request_json={"purpose": "baseline"}
                    )
                )
            captured.append((str(statement), params))
            return None

    class Engine:
        dialect = SimpleNamespace(name="postgresql")
        calls = 0

        @contextmanager
        def begin(self):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("simulated publication failure")
            yield Preparation()
            committed.append(True)

    service = IndependentScorer(Engine(), harness_sha256="a" * 64)
    with pytest.raises(RuntimeError, match="simulated publication failure"):
        service.complete_run(run_id=run_id, admitted_generation=7, execution_sha256="b" * 64)
    assert committed == [True]
    assert "prepare_scorer_baseline_finalization" in captured[0][0]
    assert captured[0][1] == {"run": run_id, "generation": 7, "sha": "b" * 64}
