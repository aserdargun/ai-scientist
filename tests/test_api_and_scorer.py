"""Unit coverage for strict run control and independent scoring contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, insert, select, update
from sqlalchemy.pool import StaticPool

from lab.api import app as api_app_module
from lab.api.app import create_app
from lab.api.registry import ApiPrincipal
from lab.db.schema import (
    dataset_labels,
    dataset_profiles,
    experiment_records,
    experiments,
    metadata,
    reports,
    run_tasks,
    runs,
    trajectory_records,
)
from lab.db.task_plan import canonical_task_plan_digest
from lab.scorer.service import IndependentScorer, parse_candidate_score


def _engine():
    raw = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    translated = raw.execution_options(schema_translate_map={"lab": None, "scorer": None})
    metadata.create_all(translated)
    return translated


def _request(key: str = "fixture-idempotency-key-001") -> dict[str, object]:
    return {
        "idempotency_key": key,
        "track": "anomaly",
        "suite": "synthetic.smoke.v1",
        "budget": {"experiments": 2, "wall_seconds": 120, "model_tokens": 500},
        "program_version": "harness-v1",
    }


def test_run_start_is_authenticated_idempotent_and_payload_bound() -> None:
    engine = _engine()
    app = create_app(
        director_engine=engine, director_token="u" * 32, allow_unregistered_suites=True
    )
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {'u' * 32}"}
        assert client.post("/v1/runs", json=_request()).status_code == 401
        first = client.post("/v1/runs", headers=headers, json=_request())
        assert first.status_code == 202
        repeated = client.post("/v1/runs", headers=headers, json=_request())
        assert repeated.status_code == 200
        assert repeated.json()["run_id"] == first.json()["run_id"]
        assert repeated.json()["reused"] is True
        changed = _request()
        changed["suite"] = "different.suite"
        conflict = client.post("/v1/runs", headers=headers, json=changed)
        assert conflict.status_code == 409
        run_id = first.json()["run_id"]
        assert client.get(f"/v1/runs/{run_id}", headers=headers).status_code == 200
        stopped = client.post(f"/v1/runs/{run_id}/stop", headers=headers)
        assert stopped.json()["state"] == "stop_requested"
        repeated_stop = client.post(f"/v1/runs/{run_id}/stop", headers=headers)
        assert repeated_stop.json()["state"] == "stop_requested"
    with engine.connect() as connection:
        events = connection.execute(select(metadata.tables["lab.run_events"].c.event_type)).all()
    assert [row[0] for row in events].count("run.stop_requested") == 1


def test_stop_dispatches_holdout_cleanup_as_background_work(monkeypatch) -> None:
    engine = _engine()
    run_ids = []

    def capture_cleanup(
        director, run_id, purpose, admitted_generation, execution_sha256, deadline_at
    ):
        assert director is engine
        run_ids.append((run_id, purpose, admitted_generation, execution_sha256, deadline_at))

    monkeypatch.setattr(api_app_module, "_recover_holdout_after_stop", capture_cleanup)
    app = create_app(
        director_engine=engine, director_token="u" * 32, allow_unregistered_suites=True
    )
    headers = {"Authorization": f"Bearer {'u' * 32}"}
    with TestClient(app) as client:
        started = client.post("/v1/runs", headers=headers, json=_request())
        run_id = started.json()["run_id"]
        stopped = client.post(f"/v1/runs/{run_id}/stop", headers=headers)
        assert stopped.status_code == 200
        assert stopped.json()["state"] == "stop_requested"
        repeated = client.post(f"/v1/runs/{run_id}/stop", headers=headers)
        assert repeated.status_code == 200
    assert run_ids == [
        (UUID(run_id), None, None, None, None),
        (UUID(run_id), None, None, None, None),
    ]


def test_run_requests_are_bounded_and_strict() -> None:
    engine = _engine()
    app = create_app(
        director_engine=engine, director_token="u" * 32, allow_unregistered_suites=True
    )
    headers = {"Authorization": f"Bearer {'u' * 32}"}
    with TestClient(app) as client:
        too_large = client.post(
            "/v1/runs",
            headers=headers,
            content=b"{" + b" " * (32 * 1024),
        )
        assert too_large.status_code == 413
        extra = _request()
        extra["owner_id"] = "attacker-selected"
        assert client.post("/v1/runs", headers=headers, json=extra).status_code == 422
        malformed_budget = _request()
        malformed_budget["budget"] = {
            "experiments": True,
            "wall_seconds": 120,
            "model_tokens": 500,
        }
        assert client.post("/v1/runs", headers=headers, json=malformed_budget).status_code == 422


def test_authenticated_aos_principal_binds_external_action_once() -> None:
    engine = _engine()
    app = create_app(
        director_engine=engine,
        principals=(
            ApiPrincipal(token="a" * 32, origin="aos", owner_id="aos:service"),
            ApiPrincipal(token="b" * 32, origin="local", owner_id="local:default"),
        ),
        allow_unregistered_suites=True,
    )
    headers = {"Authorization": f"Bearer {'a' * 32}"}
    payload = _request("aos-action-idempotency-001")
    with TestClient(app) as client:
        assert client.post("/v1/runs", headers=headers, json=payload).status_code == 422
        payload.update(
            {
                "external_task_id": "task-" + "1" * 32,
                "external_run_id": "run-" + "2" * 32,
                "external_action_id": "action-" + "3" * 32,
            }
        )
        accepted = client.post("/v1/runs", headers=headers, json=payload)
        assert accepted.status_code == 202
        repeated = client.post("/v1/runs", headers=headers, json=payload)
        assert repeated.status_code == 200
        assert repeated.json()["run_id"] == accepted.json()["run_id"]
        changed_key = dict(payload, idempotency_key="other-aos-action-key-002")
        assert client.post("/v1/runs", headers=headers, json=changed_key).status_code == 409
        foreign = {"Authorization": f"Bearer {'b' * 32}"}
        assert (
            client.get(f"/v1/runs/{accepted.json()['run_id']}", headers=foreign).status_code == 404
        )
    with engine.connect() as connection:
        row = connection.execute(select(runs)).mappings().one()
        assert row["origin"] == "aos"
        assert row["owner_id"] == "aos:service"
        assert row["external_action_id"] == payload["external_action_id"]
        assert row["request_json"]["external_task_id"] == payload["external_task_id"]


def test_candidate_score_json_rejects_duplicate_nan_and_label_fields() -> None:
    with pytest.raises(ValueError, match="invalid"):
        parse_candidate_score(b'{"schema":"candidate-scores.v1","schema":"candidate-scores.v1"}')
    with pytest.raises(ValueError, match="invalid"):
        parse_candidate_score(
            b'{"schema":"candidate-scores.v1","dataset_id":"d","split_id":"s",'
            b'"session_id":"x","sample_indices":[0],"scores":[NaN]}'
        )
    candidate = {
        "schema": "candidate-scores.v1",
        "sample_indices": [0],
        "scores": [1.0],
        "labels": [0],
    }
    with pytest.raises(ValueError, match="invalid"):
        parse_candidate_score(json.dumps(candidate).encode())


@pytest.mark.parametrize(
    "payload",
    [
        b'{"schema":"candidate-scores.v1","sample_indices":[0,1],"scores":[0.0,1e999]}',
        b'{"schema":"candidate-scores.v1","sample_indices":[0,1],"scores":[0.0]}',
        b'{"schema":"candidate-scores.v1","sample_indices":[0,0],"scores":[0.0,1.0]}',
    ],
)
def test_candidate_score_json_rejects_nonfinite_misaligned_or_duplicate_indices(
    payload: bytes,
) -> None:
    with pytest.raises(ValueError, match="invalid"):
        parse_candidate_score(payload)


def test_empty_stopped_plan_publishes_retrievable_zero_task_report() -> None:
    engine = _engine()
    run_id = uuid4()
    now = datetime.now(UTC)
    empty_digest = canonical_task_plan_digest([])
    with engine.begin() as connection:
        connection.execute(
            insert(runs).values(
                run_id=run_id,
                origin="local",
                owner_id="local:default",
                idempotency_key=f"empty-stop-{run_id}",
                payload_sha256="a" * 64,
                request_json={},
                state="stop_requested",
                stop_requested=True,
                task_plan_sha256=empty_digest,
                task_plan_count=0,
                created_at=now,
                updated_at=now,
            )
        )

    scorer = IndependentScorer(engine, harness_sha256="1" * 64)
    report, _ = scorer.complete_run(run_id=run_id)
    assert report["status"] == "stopped"
    assert report["task_scores"] == []
    assert report["task_terminal_outcomes"] == []

    app = create_app(
        director_engine=engine, director_token="u" * 32, allow_unregistered_suites=True
    )
    with TestClient(app) as client:
        response = client.get(
            f"/v1/runs/{run_id}/report", headers={"Authorization": f"Bearer {'u' * 32}"}
        )
    assert response.status_code == 200
    assert response.json()["report"]["status"] == "stopped"


def test_finalizer_waits_for_terminal_experiment_document_pair() -> None:
    engine = _engine()
    run_id = uuid4()
    experiment_id = f"exp_{uuid4().hex}"
    now = datetime.now(UTC)
    empty_digest = canonical_task_plan_digest([])
    with engine.begin() as connection:
        connection.execute(
            insert(runs).values(
                run_id=run_id,
                origin="local",
                owner_id="local:default",
                idempotency_key=f"pending-experiment-{run_id}",
                payload_sha256="a" * 64,
                request_json={},
                state="stop_requested",
                stop_requested=True,
                task_plan_sha256=empty_digest,
                task_plan_count=0,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(experiments).values(
                experiment_id=experiment_id,
                run_id=run_id,
                sequence=0,
                experiment_number=1,
                kind="proposal",
                baseline_name=None,
                parent_experiment_id=None,
                candidate_sha256="b" * 64,
                candidate_blob_sha256="b" * 64,
                inputs_sha256="c" * 64,
                move_type="detector",
                system="S1",
                hypothesis="A bounded synthetic hypothesis.",
                predicted_delta=None,
                proposal_json={},
                status="proposed",
                created_at=now,
                updated_at=now,
            )
        )

    scorer = IndependentScorer(engine, harness_sha256="1" * 64)
    assert scorer.finalize_if_ready(run_id=run_id) is None
    with engine.connect() as connection:
        assert connection.execute(select(reports.c.run_id)).first() is None
        assert connection.execute(select(runs.c.state)).scalar_one() == "stop_requested"

    with engine.begin() as connection:
        connection.execute(
            update(experiments)
            .where(experiments.c.experiment_id == experiment_id)
            .values(status="abandoned")
        )
        connection.execute(
            insert(experiment_records).values(
                experiment_id=experiment_id,
                experiment_json={"schema": "experiment.v1", "status": "abandoned"},
                experiment_sha256="d" * 64,
                experiment_blob_sha256="d" * 64,
                created_at=now,
            )
        )
        connection.execute(
            insert(trajectory_records).values(
                experiment_id=experiment_id,
                trajectory_json={"schema": "trajectory.v1"},
                trajectory_sha256="e" * 64,
                trajectory_blob_sha256="e" * 64,
                messages_blob_sha256="f" * 64,
                created_at=now,
            )
        )

    finalized = scorer.finalize_if_ready(run_id=run_id)
    assert finalized is not None
    assert finalized[0]["status"] == "stopped"


def test_stopped_task_outcome_is_reported_without_fake_metric_score() -> None:
    engine = _engine()
    run_id = uuid4()
    now = datetime.now(UTC)
    assignment = {
        "run_id": run_id,
        "experiment_id": "exp-01",
        "evaluation_kind": "primary",
        "task_id": "task-01",
        "seed": 0,
        "candidate_sha256": "2" * 64,
        "dataset_id": "terminal-fixture",
        "split_id": "eval",
        "session_id": "one",
    }
    with engine.begin() as connection:
        connection.execute(
            insert(runs).values(
                run_id=run_id,
                origin="local",
                owner_id="local:default",
                idempotency_key=f"terminal-stop-{run_id}",
                payload_sha256="a" * 64,
                request_json={},
                state="stop_requested",
                stop_requested=True,
                task_plan_sha256=canonical_task_plan_digest([assignment]),
                task_plan_count=1,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(dataset_profiles).values(
                dataset_id=assignment["dataset_id"],
                split_id=assignment["split_id"],
                session_id=assignment["session_id"],
                sample_count=4,
                sliding_window=2,
                profile_sha256="3" * 64,
            )
        )
        connection.execute(insert(run_tasks).values(**assignment))

    scorer = IndependentScorer(engine, harness_sha256="1" * 64)
    scorer.record_terminal_outcome(
        run_id=run_id,
        experiment_id="exp-01",
        evaluation_kind="primary",
        task_id="task-01",
        seed=0,
        candidate_sha256="2" * 64,
        outcome_code="cancelled",
    )
    # A retry after an uncertain commit reads back the same immutable outcome.
    scorer.record_terminal_outcome(
        run_id=run_id,
        experiment_id="exp-01",
        evaluation_kind="primary",
        task_id="task-01",
        seed=0,
        candidate_sha256="2" * 64,
        outcome_code="cancelled",
    )
    with pytest.raises(ValueError, match="different immutable terminal outcome"):
        scorer.record_terminal_outcome(
            run_id=run_id,
            experiment_id="exp-01",
            evaluation_kind="primary",
            task_id="task-01",
            seed=0,
            candidate_sha256="2" * 64,
            outcome_code="candidate_timeout",
        )
    report, _ = scorer.complete_run(run_id=run_id)
    assert report["status"] == "stopped"
    assert report["task_scores"] == []
    assert report["task_terminal_outcomes"] == []
    assert "task-01" not in json.dumps(report)


def test_scorer_uses_private_labels_and_finalizes_content_hashed_report() -> None:
    engine = _engine()
    run_id = uuid4()
    now = datetime.now(UTC)
    dataset = "synthetic-private-fixture"
    split = "eval-v1"
    session = "session-01"
    profile_json = {"dataset": dataset, "split": split, "session": session, "window": 4}
    profile_hash = hashlib.sha256(
        json.dumps(profile_json, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    labels = [False] * 8 + [True] * 8
    with engine.begin() as connection:
        connection.execute(
            insert(runs).values(
                run_id=run_id,
                origin="local",
                owner_id="local:default",
                idempotency_key=f"scorer-{run_id}",
                payload_sha256="0" * 64,
                request_json={},
                state="running",
                created_at=now,
                updated_at=now,
                stop_requested=False,
            )
        )
        connection.execute(
            insert(dataset_profiles).values(
                dataset_id=dataset,
                split_id=split,
                session_id=session,
                sample_count=len(labels),
                sliding_window=4,
                profile_sha256=profile_hash,
                visibility="dev",
            )
        )
        connection.execute(
            insert(dataset_profiles).values(
                dataset_id="distractor",
                split_id="other",
                session_id="other-session",
                sample_count=16,
                sliding_window=2,
                profile_sha256="4" * 64,
            )
        )
        connection.execute(
            insert(dataset_profiles).values(
                dataset_id="sealed-eval",
                split_id="sealed-v1",
                session_id="hidden-session",
                sample_count=len(labels),
                sliding_window=4,
                profile_sha256="5" * 64,
                visibility="sealed",
            )
        )
        connection.execute(
            insert(run_tasks).values(
                run_id=run_id,
                experiment_id="exp-01",
                evaluation_kind="primary",
                task_id="task-01",
                seed=0,
                candidate_sha256="2" * 64,
                dataset_id=dataset,
                split_id=split,
                session_id=session,
            )
        )
        connection.execute(
            insert(run_tasks).values(
                run_id=run_id,
                experiment_id="exp-sealed",
                evaluation_kind="primary",
                task_id="hidden-task",
                seed=0,
                candidate_sha256="4" * 64,
                dataset_id="sealed-eval",
                split_id="sealed-v1",
                session_id="hidden-session",
            )
        )
        connection.execute(
            insert(run_tasks).values(
                run_id=run_id,
                experiment_id="exp-02",
                evaluation_kind="baseline",
                task_id="task-01",
                seed=0,
                candidate_sha256="3" * 64,
                dataset_id=dataset,
                split_id=split,
                session_id=session,
            )
        )
        planned_rows = (
            connection.execute(
                select(run_tasks)
                .where(run_tasks.c.run_id == run_id)
                .order_by(
                    run_tasks.c.experiment_id,
                    run_tasks.c.evaluation_kind,
                    run_tasks.c.task_id,
                    run_tasks.c.seed,
                )
            )
            .mappings()
            .all()
        )
        connection.execute(
            update(runs)
            .where(runs.c.run_id == run_id)
            .values(
                task_plan_sha256=canonical_task_plan_digest(planned_rows),
                task_plan_count=len(planned_rows),
            )
        )
        connection.execute(
            insert(dataset_labels),
            [
                {
                    "dataset_id": dataset,
                    "split_id": split,
                    "session_id": session,
                    "sample_index": index,
                    "is_anomaly": label,
                }
                for index, label in enumerate(labels)
            ],
        )
        connection.execute(
            insert(dataset_labels),
            [
                {
                    "dataset_id": "sealed-eval",
                    "split_id": "sealed-v1",
                    "session_id": "hidden-session",
                    "sample_index": index,
                    "is_anomaly": label,
                }
                for index, label in enumerate(labels)
            ],
        )
        connection.execute(
            insert(dataset_labels),
            [
                {
                    "dataset_id": "distractor",
                    "split_id": "other",
                    "session_id": "other-session",
                    "sample_index": index,
                    "is_anomaly": False,
                }
                for index in range(16)
            ],
        )
    output = json.dumps(
        {
            "schema": "candidate-scores.v1",
            "sample_indices": list(range(len(labels))),
            "scores": [float(index) for index in range(len(labels))],
        },
        separators=(",", ":"),
    ).encode()
    scorer = IndependentScorer(engine, harness_sha256="1" * 64)
    with pytest.raises(ValueError, match="candidate digest"):
        scorer.score_task(
            run_id=run_id,
            experiment_id="exp-01",
            evaluation_kind="primary",
            task_id="task-01",
            seed=0,
            candidate_sha256="9" * 64,
            candidate_output=output,
        )
    score = scorer.score_task(
        run_id=run_id,
        experiment_id="exp-01",
        evaluation_kind="primary",
        task_id="task-01",
        seed=0,
        candidate_sha256="2" * 64,
        candidate_output=output,
    )
    assert score["sample_count"] == 16
    assert 0.0 <= score["vus_roc"] <= 1.0
    assert 0.0 <= score["vus_pr"] <= 1.0
    assert (
        scorer.score_task(
            run_id=run_id,
            experiment_id="exp-01",
            evaluation_kind="primary",
            task_id="task-01",
            seed=0,
            candidate_sha256="2" * 64,
            candidate_output=output,
        )
        == score
    )
    with pytest.raises(ValueError, match="every trusted task"):
        scorer.complete_run(run_id=run_id)
    second_score = scorer.score_task(
        run_id=run_id,
        experiment_id="exp-02",
        evaluation_kind="baseline",
        task_id="task-01",
        seed=0,
        candidate_sha256="3" * 64,
        candidate_output=output,
    )
    assert second_score["dataset_id"] == dataset
    hidden_score = scorer.score_task(
        run_id=run_id,
        experiment_id="exp-sealed",
        evaluation_kind="primary",
        task_id="hidden-task",
        seed=0,
        candidate_sha256="4" * 64,
        candidate_output=output,
    )
    assert hidden_score["dataset_id"] == "sealed-eval"
    report, report_hash = scorer.complete_run(run_id=run_id)
    assert report["schema"] == "lab.report.v1"
    assert len(report["task_scores"]) == 2
    assert all(row["dataset_id"] != "sealed-eval" for row in report["task_scores"])
    assert "hidden-task" not in json.dumps(report)
    assert (
        hashlib.sha256(
            json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        == report_hash
    )
    with engine.connect() as connection:
        row = connection.execute(select(runs).where(runs.c.run_id == run_id)).mappings().one()
    assert row["state"] == "completed"
    assert row["report_sha256"] == report_hash
    assert scorer.complete_run(run_id=run_id) == (report, report_hash)
    with pytest.raises(ValueError, match="active"):
        scorer.score_task(
            run_id=run_id,
            experiment_id="exp-01",
            evaluation_kind="primary",
            task_id="task-01",
            seed=0,
            candidate_sha256="2" * 64,
            candidate_output=output,
        )


def test_scorer_rejects_incomplete_or_misaligned_sample_indices() -> None:
    engine = _engine()
    scorer = IndependentScorer(engine, harness_sha256="1" * 64)
    run_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            insert(runs).values(
                run_id=run_id,
                origin="local",
                owner_id="local:default",
                idempotency_key=f"incomplete-{run_id}",
                payload_sha256="0" * 64,
                request_json={},
                state="running",
                created_at=now,
                updated_at=now,
                stop_requested=False,
            )
        )
        connection.execute(
            insert(dataset_profiles).values(
                dataset_id="d",
                split_id="s",
                session_id="x",
                sample_count=2,
                sliding_window=1,
                profile_sha256="3" * 64,
            )
        )
        connection.execute(
            insert(run_tasks).values(
                run_id=run_id,
                experiment_id="exp-01",
                evaluation_kind="primary",
                task_id="task-01",
                seed=0,
                candidate_sha256="2" * 64,
                dataset_id="d",
                split_id="s",
                session_id="x",
            )
        )
        connection.execute(
            insert(dataset_labels),
            [
                {
                    "dataset_id": "d",
                    "split_id": "s",
                    "session_id": "x",
                    "sample_index": index,
                    "is_anomaly": bool(index),
                }
                for index in range(2)
            ],
        )
    output = json.dumps(
        {
            "schema": "candidate-scores.v1",
            "sample_indices": [1],
            "scores": [0.5],
        }
    ).encode()
    with pytest.raises(ValueError, match="complete ordered"):
        scorer.score_task(
            run_id=run_id,
            experiment_id="exp-01",
            evaluation_kind="primary",
            task_id="task-01",
            seed=0,
            candidate_sha256="2" * 64,
            candidate_output=output,
        )


def test_owned_baseline_stop_uses_paired_finalizer_without_holdout(monkeypatch) -> None:
    from types import SimpleNamespace

    from lab.scorer import holdout_supervisor, supervisor

    engine = _engine()
    run_id = uuid4()
    owner_generation = 3
    execution_sha256 = "a" * 64
    calls = []

    def finalize(run_id_arg, *, admitted_generation, execution_sha256, remaining_seconds):
        calls.append((run_id_arg, admitted_generation, execution_sha256, remaining_seconds))
        return SimpleNamespace(exit_code=1, result=None)

    def forbidden_holdout(*args, **kwargs):
        raise AssertionError("baseline stop must not launch holdout recovery")

    monkeypatch.setattr(supervisor, "run_scorer_finalize_process", finalize)
    monkeypatch.setattr(holdout_supervisor, "run_holdout_run_recovery_process", forbidden_holdout)

    api_app_module._recover_holdout_after_stop(
        engine,
        run_id,
        "baseline",
        owner_generation,
        execution_sha256,
        datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=90),
    )

    assert calls == [(run_id, owner_generation, execution_sha256, 30)]


def test_stop_callback_does_not_mint_budget_after_deadline(monkeypatch) -> None:
    from lab.scorer import supervisor

    calls = []
    monkeypatch.setattr(
        supervisor,
        "run_scorer_empty_baseline_stop_process",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    api_app_module._recover_holdout_after_stop(
        _engine(),
        uuid4(),
        "baseline",
        None,
        None,
        datetime.now(UTC) - timedelta(seconds=1),
    )
    assert calls == []
