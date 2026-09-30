"""Mode-study closure must retain the live owner, scope and elapsed budget."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

import lab.cli as cli
from lab.director.budget import RunBudget


@pytest.fixture
def closure(monkeypatch):
    clock = [0.0]
    calls = []
    owner = SimpleNamespace(run_id=uuid4(), generation=3, execution_sha256="a" * 64)
    kwargs = dict(
        owner=owner,
        request={"provider": "mode-grid", "track": "mode"},
        manifest=SimpleNamespace(weight_policy="single_snapshot_study.v1"),
        snapshot_sha256="b" * 64,
        holdout_enabled=False,
        loop_status="proposal_limit_reached",
        budget=RunBudget(wall_limit=1000, token_limit=0, monotonic=lambda: clock[0]),
    )
    monkeypatch.setattr(cli, "seal_run_task_plan", lambda *_a, **_k: calls.append("seal"))

    def finalize(run_id, **options):
        assert calls == ["seal"]
        assert run_id == owner.run_id
        calls.append(options)
        return SimpleNamespace(
            exit_code=0,
            result={"state": "finalized", "research_status": "completed"},
            unit="fixture-finalizer",
        )

    monkeypatch.setattr(cli, "run_scorer_finalize_process", finalize)
    return kwargs, clock, calls


def test_completed_mode_study_seals_then_finalizes_same_owner_with_bounded_wall(closure):
    kwargs, clock, calls = closure
    clock[0] = 970.2
    result = cli._finalize_mode_study(object(), **kwargs)
    assert result["state"] == "finalized"
    assert calls[1] == dict(
        admitted_generation=3, execution_sha256="a" * 64, remaining_seconds=29
    )


def test_mode_finalization_never_reserves_more_than_sixty_seconds(closure):
    kwargs, _clock, calls = closure
    cli._finalize_mode_study(object(), **kwargs)
    assert calls[1]["remaining_seconds"] == 60


@pytest.mark.parametrize(
    "override",
    [
        {"holdout_enabled": True},
        {"request": {"provider": "fake-json", "track": "mode"}},
        {"request": {"provider": "mode-grid", "track": "research"}},
        {"manifest": SimpleNamespace(weight_policy="spec-3.2.5-appendix-c-waterfill.v1")},
        {"snapshot_sha256": None},
        {"loop_status": "candidate_crash_limit"},
    ],
)
def test_mode_closure_cannot_bypass_holdout_or_admit_another_flow(closure, override):
    kwargs, _clock, calls = closure
    assert cli._finalize_mode_study(object(), **(kwargs | override)) is None
    assert calls == []


def test_expired_mode_budget_cannot_seal_or_launch(closure):
    kwargs, clock, calls = closure
    clock[0] = 999.5
    result = cli._finalize_mode_study(object(), **kwargs)
    assert result["state"] == "finalization_window_expired"
    assert calls == []


def test_sealing_cost_is_included_in_finalizer_deadline(closure, monkeypatch):
    kwargs, clock, calls = closure

    def seal(*_args, **_kwargs):
        calls.append("seal")
        clock[0] = 1000

    monkeypatch.setattr(cli, "seal_run_task_plan", seal)
    result = cli._finalize_mode_study(object(), **kwargs)
    assert result["state"] == "finalization_window_expired"
    assert calls == ["seal"]


def test_owner_fence_refusal_prevents_finalizer(closure, monkeypatch):
    kwargs, _clock, calls = closure

    def seal(*_args, **_kwargs):
        raise ValueError("stale execution owner")

    monkeypatch.setattr(cli, "seal_run_task_plan", seal)
    with pytest.raises(ValueError, match="stale execution owner"):
        cli._finalize_mode_study(object(), **kwargs)
    assert calls == []
