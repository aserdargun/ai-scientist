"""Exercise the baseline bootstrap's complete typed-record path before live work."""

from __future__ import annotations

import copy
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pandas as pd
import pytest
from test_director_seed_resume_review import MemoryCheckpoints

from harness.contracts import FitContext
from lab.director import baseline_runner as module
from lab.director.budget import RunBudget
from lab.director.contracts import SourceProvenance
from lab.director.suite import SuiteTask


@pytest.mark.parametrize(
    ("task_count", "per_task_seconds", "wall_limit", "task_elapsed", "restored_seconds"),
    [
        (2, 90, 14_400, 1, None),
        (1, 600, 14_400, 1, None),
        (27, 600, 14_400, 35, None),
        (27, 600, 20, 1, None),
        (27, 600, 14_400, 35, 600),
    ],
)
def test_baseline_bootstrap_commits_matching_typed_pairs_with_whole_seed_reservations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    task_count: int,
    per_task_seconds: int,
    wall_limit: int,
    task_elapsed: int,
    restored_seconds: int | None,
) -> None:
    """SQL/Docker replaced; real typed documents, budgets and physical blob store."""
    # pytest's small temporary filesystem does not model the host disk reserve.
    monkeypatch.setattr("lab.scorer.jobs.MIN_FREE_DISK_BYTES", 0)
    clock = [0.0]

    class ClockDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 30, tzinfo=UTC) + timedelta(seconds=clock[0])

    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module, "datetime", ClockDateTime)
    tasks = []
    for index in range(task_count):
        identity = {
            "dataset_id": f"baseline-fixture-{index}",
            "split_id": "dev",
            "session_id": "local",
        }
        tasks.append(
            SuiteTask.from_frames(
                **identity,
                task_id=f"task-{index}",
                profile_sha256="a" * 64,
                family="EVT",
                task_weight=1 / task_count,
                provenance=SourceProvenance(
                    **identity,
                    source_manifest_sha256="a" * 64,
                    source_revision="fixture-v1",
                    license_id="CC0-1.0",
                    attribution="Local test",
                    access_terms="Local test",
                    usage_profile="noncommercial_research",
                ),
                context=FitContext(0, ("sensor",), (), 60, 90.0),
                train=pd.DataFrame({"sensor": [0.0, 1.0]}),
                evaluation=pd.DataFrame({"sensor": [2.0, 3.0]}),
            )
        )
    journal = MemoryCheckpoints()
    budget = RunBudget(token_limit=0, wall_limit=wall_limit, monotonic=lambda: clock[0])
    run_id = uuid4()
    reservation_starts = {}
    original_reservation = None
    if restored_seconds is not None:
        name = module.BASELINE_NAMES[0]
        reservation = budget.reserve_work(wall_seconds=restored_seconds, model_tokens=0)
        journal.append(
            key=f"baseline-seed-reservation:{name}:0",
            phase="baseline_seed_reserved",
            payload={
                "experiment_id": module._baseline_experiment_id(run_id, name),
                "seed": 0,
                "name": name,
                "reservation_id": str(reservation.reservation_id),
                "wall_seconds": reservation.wall_seconds,
                "model_tokens": 0,
                "started_at": ClockDateTime.now(UTC).isoformat(),
                "budget": module._budget_json(budget),
            },
        )
        original_reservation = copy.deepcopy(journal.records)
        # Resume an already aged reservation: its 120 seconds cannot be renewed.
        clock[0] = 120.0
    registrations, commits, measurements = {}, [], []
    calibration = object()
    connection = SimpleNamespace(
        execute=lambda *_args, **_kwargs: SimpleNamespace(scalar_one_or_none=lambda: None)
    )
    engine = SimpleNamespace(connect=lambda: nullcontext(connection))

    def register(*_: Any, **kwargs: Any):
        registrations[kwargs["experiment_id"]] = kwargs
        return {"status": "proposed"}

    def evaluate(*_: Any, **kwargs: Any):
        task = kwargs["task"]
        active = budget.snapshot().reservations
        assert len(active) == 1
        if active[0].reservation_id not in reservation_starts:
            reservation_starts[active[0].reservation_id] = clock[0]
        expected = (
            restored_seconds
            if restored_seconds is not None
            else min(
                task_count * per_task_seconds,
                int(wall_limit - reservation_starts[active[0].reservation_id]),
            )
        )
        assert active[0].wall_seconds == expected
        assert 1 <= kwargs["remaining_seconds"] <= per_task_seconds <= 600
        clock[0] += min(task_elapsed, kwargs["remaining_seconds"])
        measurements.append((kwargs["experiment_id"], kwargs["seed"], active[0].reservation_id))
        return {
            "experiment_id": kwargs["experiment_id"],
            "evaluation_kind": "baseline",
            "seed": kwargs["seed"],
            "candidate_sha256": kwargs["candidate_sha256"],
            "task_id": task.task_id,
            "dataset_id": task.dataset_id,
            "split_id": task.split_id,
            "session_id": task.session_id,
            "profile_sha256": task.profile_sha256,
            "harness_sha256": "b" * 64,
            "candidate_output_sha256": "c" * 64,
            "vus_pr": 0.2,
            "vus_roc": 0.5,
            "fit_seconds": 0.001,
            "score_seconds": 0.001,
            "guards": {key: f"{key}_pass" for key in ("hardcoding", "determinism", "causality")},
        }

    def commit(*_: Any, **kwargs: Any):
        experiment, trajectory = kwargs["experiment"], kwargs["trajectory"]
        registered = registrations[experiment.experiment_id]
        assert experiment.hypothesis == registered["hypothesis"]
        assert experiment.inputs_sha256 == registered["inputs_sha256"]
        assert experiment.candidate_sha256 == registered["candidate_sha256"]
        assert experiment.ordinal == registered["sequence"] + 1
        assert experiment.kind == trajectory.kind == "baseline"
        assert experiment.system == trajectory.system == "S1"
        assert experiment.run_id == trajectory.run_id
        assert experiment.decision is trajectory.outcome is None
        assert experiment.llm_input_tokens == experiment.llm_output_tokens == 0
        commits.append(kwargs)
        return {"status": "committed"}

    monkeypatch.setattr(module, "compute_harness_hash", lambda _: SimpleNamespace(sha256="b" * 64))
    monkeypatch.setattr(module, "register_experiment", register)
    monkeypatch.setattr(module, "transition_experiment", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "plan_run_tasks", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "_append_next_checkpoint", journal.append)
    monkeypatch.setattr(module, "evaluate_and_score_seed", evaluate)
    monkeypatch.setattr(module, "commit_experiment_record", commit)
    monkeypatch.setattr(module, "freeze_calibration_from_database", lambda *_a, **_k: calibration)
    monkeypatch.setattr(module, "register_frozen_calibration", lambda *_a, **_k: None)
    monkeypatch.setattr(module, "read_registered_calibration", lambda *_a, **_k: calibration)

    def run():
        return module.run_baseline_suite(
            engine,
            object(),
            run_id=run_id,
            tasks=tuple(tasks),
            lease=journal,
            runner=SimpleNamespace(image=module.DEFAULT_SANDBOX_IMAGE),
            suite_id="baseline-record-fixture",
            suite_version=1,
            harness_sha256="b" * 64,
            image_sha256=module.DEFAULT_SANDBOX_IMAGE.removeprefix("sha256:"),
            budget=budget,
            artifact_root=tmp_path,
            seed_wall_seconds=per_task_seconds,
        )

    if restored_seconds is not None or wall_limit < task_count * task_elapsed:
        with pytest.raises(RuntimeError, match="whole_suite_seed_budget_exhausted"):
            run()
        assert not commits
        assert len(budget.snapshot().reservations) == 1
        active = budget.snapshot().reservations[0]
        assert active.wall_seconds == (restored_seconds or wall_limit)
        if original_reservation is not None:
            for key, record in original_reservation.items():
                assert journal.records[key] == record
            assert len(reservation_starts) == 1
            # 480 remaining seconds allow 13 full tasks and one bounded tail.
            assert len(measurements) == 14
            assert clock[0] == 600.0
        return
    result = run()
    assert result is calibration
    assert len(commits) == 3 and len(measurements) == task_count * 9
    assert len({reservation_id for _, _, reservation_id in measurements}) == 9
    assert budget.proposal_count == budget.model_tokens == 0
    assert not budget.snapshot().reservations
