"""Regression coverage for ready-job and stop-recovery queue separation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, event, insert, update
from sqlalchemy.pool import StaticPool

from lab.cli import _dispatch_one
from lab.db.schema import metadata, run_tasks, runs, score_jobs
from lab.scorer.supervisor import ScorerProcessResult


def _engine() -> Engine:
    raw = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    translated = raw.execution_options(schema_translate_map={"lab": None, "scorer": None})
    metadata.create_all(translated)
    return translated


def _add_job(engine: Engine, *, run_state: str, job_state: str) -> tuple[UUID, UUID]:
    run_id = uuid4()
    job_id = uuid4()
    now = datetime.now(UTC)
    candidate_sha = "a" * 64
    with engine.begin() as connection:
        connection.execute(
            insert(runs).values(
                run_id=run_id,
                origin="local",
                owner_id=f"unit:{run_id}",
                idempotency_key=f"dispatch-{run_id}",
                payload_sha256="b" * 64,
                request_json={},
                state=run_state,
                stop_requested=run_state == "stop_requested",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(run_tasks).values(
                run_id=run_id,
                experiment_id="exp-01",
                evaluation_kind="primary",
                task_id="task-01",
                seed=0,
                candidate_sha256=candidate_sha,
                dataset_id="synthetic",
                split_id="eval",
                session_id="session-01",
            )
        )
        connection.execute(
            insert(score_jobs).values(
                job_id=job_id,
                run_id=run_id,
                experiment_id="exp-01",
                evaluation_kind="primary",
                task_id="task-01",
                seed=0,
                candidate_sha256=candidate_sha,
                artifact_sha256="c" * 64,
                state=job_state,
                attempt=0,
                claimed_by="old-token" if job_state == "running" else None,
                lease_until=now if job_state == "running" else None,
                claim_unit=(f"swapp-ai-scientist-scorer-{job_id.hex}.service"
                            if job_state == "running" else None),
                claim_invocation_id="d" * 32 if job_state == "running" else None,
                admitted_generation=1,
                execution_sha256="e" * 64,
                created_at=now,
                updated_at=now,
            )
        )
    return run_id, job_id


def test_dispatcher_prioritizes_only_running_run_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    import lab.cli

    engine = _engine()
    _add_job(engine, run_state="stop_requested", job_state="queued")
    _, active_job = _add_job(engine, run_state="running", job_state="queued")
    monkeypatch.setattr(
        lab.cli,
        "run_scorer_process",
        lambda job_id, remaining_seconds: ScorerProcessResult(
            job_id, f"swapp-ai-scientist-scorer-{job_id.hex}.service", 0, {"state": "completed"}
        ),
    )
    monkeypatch.setattr(
        lab.cli,
        "run_scorer_recovery_process",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("recovery ran first")),
    )

    result = _dispatch_one(engine, remaining_seconds=60)

    assert result["job_id"] == str(active_job)
    assert result["state"] == "completed"


def test_dispatcher_routes_stopped_queued_jobs_to_cancel_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lab.cli

    engine = _engine()
    stopped_run, stopped_job = _add_job(
        engine, run_state="stop_requested", job_state="queued"
    )
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        lab.cli,
        "cancel_queued_score_job",
        lambda engine, job_id: calls.append(("cancel", job_id)),
    )
    def finalize(run_id: UUID, *, admitted_generation: int, execution_sha256: str,
                 remaining_seconds: int) -> SimpleNamespace:
        assert remaining_seconds == 60
        calls.append(("finalize", (run_id, admitted_generation, execution_sha256)))
        return SimpleNamespace(
            exit_code=0,
            unit=f"swapp-ai-scientist-finalize-{run_id.hex}.service",
            result={"research_status": "stopped"},
        )

    monkeypatch.setattr(lab.cli, "run_scorer_finalize_process", finalize)
    monkeypatch.setattr(
        lab.cli,
        "run_scorer_process",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("normal scorer ran")),
    )

    result = _dispatch_one(engine, remaining_seconds=60)

    assert calls == [("cancel", stopped_job), ("finalize", (stopped_run, 1, "e" * 64))]
    assert result["state"] == "cancelled_queued"
    assert result["research_status"] == "stopped"
    assert str(stopped_run) not in result.values()


def test_dispatcher_prefers_queued_work_over_expired_running_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lab.cli

    engine = _engine()
    _, expired_job = _add_job(engine, run_state="running", job_state="running")
    _, queued_job = _add_job(engine, run_state="running", job_state="queued")
    with engine.begin() as connection:
        connection.execute(
            update(score_jobs)
            .where(score_jobs.c.job_id == expired_job)
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    selected: list[UUID] = []

    def fake_run(job_id: UUID, remaining_seconds: int) -> ScorerProcessResult:
        selected.append(job_id)
        return ScorerProcessResult(
            job_id,
            f"swapp-ai-scientist-scorer-{job_id.hex}.service",
            0,
            {"state": "completed"},
        )

    monkeypatch.setattr(
        lab.cli,
        "run_scorer_process",
        fake_run,
    )

    result = _dispatch_one(engine, remaining_seconds=60)

    assert selected == [queued_job]
    assert result["job_id"] == str(queued_job)


def test_capacity_busy_active_job_falls_through_to_stopped_running_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lab.cli

    engine = _engine()
    _, active_job = _add_job(engine, run_state="running", job_state="queued")
    _, stopped_job = _add_job(engine, run_state="stop_requested", job_state="running")
    called: list[tuple[str, object]] = []
    monkeypatch.setattr(
        lab.cli,
        "run_scorer_process",
        lambda job_id, remaining_seconds: ScorerProcessResult(
            job_id,
            f"swapp-ai-scientist-scorer-{job_id.hex}.service",
            0,
            {"state": "capacity_busy"},
        ),
    )

    def fake_recovery(job_id: UUID, *, expected_claim_invocation_id: str,
                      remaining_seconds: int) -> ScorerProcessResult:
        called.append((str(job_id), expected_claim_invocation_id))
        return ScorerProcessResult(
            job_id,
            f"swapp-ai-scientist-scorer-{job_id.hex}.service",
            0,
            {"state": "cancelled_after_drain", "report_state": "stopped"},
        )

    monkeypatch.setattr(lab.cli, "run_scorer_recovery_process", fake_recovery)

    result = _dispatch_one(engine, remaining_seconds=60)

    assert called == [(str(stopped_job), "d" * 32)]
    assert result["job_id"] == str(stopped_job)
    assert result["state"] == "cancelled_after_drain"
    assert result["research_status"] == "stopped"
    assert str(active_job) != result["job_id"]


def test_capacity_busy_without_stopped_work_is_not_reported_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lab.cli

    engine = _engine()
    selector_calls: list[str] = []

    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def empty_baseline_selector(connection, cursor, statement, parameters, context, executemany):
        # This SQLite queue-routing fixture cannot implement PostgreSQL owner/stop RPCs.
        # Supply only the independently tested selector's empty result at its SQL boundary.
        if statement == "SELECT lab.next_unstarted_baseline_stop()":
            selector_calls.append(statement)
            return "SELECT NULL", ()
        return statement, parameters

    _, active_job = _add_job(engine, run_state="running", job_state="queued")
    monkeypatch.setattr(
        lab.cli,
        "run_scorer_process",
        lambda job_id, remaining_seconds: ScorerProcessResult(
            job_id,
            f"swapp-ai-scientist-scorer-{job_id.hex}.service",
            0,
            {"state": "capacity_busy"},
        ),
    )
    monkeypatch.setattr(
        lab.cli,
        "run_scorer_recovery_process",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no recovery job")),
    )

    result = _dispatch_one(engine, remaining_seconds=60)

    assert result == {"state": "capacity_busy"}
    assert active_job is not None
    assert selector_calls == ["SELECT lab.next_unstarted_baseline_stop()"]
