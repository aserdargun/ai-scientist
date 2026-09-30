"""An unconfirmed rejection must retain the seed-zero comparison in its record."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from test_director_confirmation_review import _fixture

from lab.director import runner as module
from lab.director.budget import RunBudget


def test_primary_discard_records_the_screened_parent_seed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Real Referee arithmetic with synthetic scores; persistence/Docker are replaced."""
    arguments, primary = _fixture(tmp_path)
    proposal = arguments["proposal"]
    primary = replace(
        primary,
        measurements=tuple(dict(row, vus_pr=0.245) for row in primary.measurements),
    )
    # Normalized parent seeds are [1, 0, 0]: first screen and confirmation mean differ.
    screen_parent = {"evt-fixture": 0.25}
    confirmation_parent = {"evt-fixture": 0.225}
    noise = float(np.std([1.0, 0.0, 0.0], ddof=0))
    expected = module.decide_proposal_from_measurements(
        proposal=proposal,
        result=primary,
        tasks=arguments["tasks"],
        calibration=arguments["calibration"],
        parent_source=arguments["parent_source"],
        champion_scores_by_task=screen_parent,
        champion_noise_sd=noise,
        best_suite=0.5,
    )
    assert expected.decision.verdict == "DISCARD"
    assert expected.decision.delta < 0
    seeds = []

    def execute(*_: Any, **kwargs: Any):
        seeds.append(kwargs["seed"])
        return primary

    def terminal(*_: Any, **kwargs: Any):
        # Reuse the production terminal's actual numerical decision path.
        result = module.decide_proposal_from_measurements(
            **{
                name: kwargs[name]
                for name in (
                    "proposal",
                    "result",
                    "tasks",
                    "calibration",
                    "parent_source",
                    "best_suite",
                    "champion_scores_by_task",
                    "champion_noise_sd",
                )
            }
        )
        return result.decision.model_dump(mode="json")

    lease = SimpleNamespace(
        heartbeat=lambda: None,
        require_run_active=lambda: None,
        read_checkpoint=lambda **_: {"payload": {}},
    )
    monkeypatch.setattr(module, "register_proposal_before_execution", lambda *_, **__: proposal)
    monkeypatch.setattr(module, "execute_candidate_seed", execute)
    monkeypatch.setattr(module, "commit_primary_terminal_record", terminal)
    recorded = module.run_one_proposal(
        object(),
        object(),
        lease=lease,
        runner=object(),
        run_id=arguments["run_id"],
        ordinal=1,
        parent_experiment_id=proposal.parent_experiment_id,
        parent_tree_sha256=proposal.parent_tree_sha256,
        parent_source=arguments["parent_source"],
        suite_id=proposal.suite_id,
        suite_version=proposal.suite_version,
        calibration=arguments["calibration"],
        harness_sha256=proposal.harness_sha256,
        image_sha256=proposal.image_sha256,
        system="S1",
        context=object(),
        provider=object(),
        tasks=arguments["tasks"],
        budget=RunBudget(token_limit=40_000),
        artifact_root=tmp_path,
        best_suite=0.5,
        screen_champion_scores_by_task=screen_parent,
        champion_scores_by_task=confirmation_parent,
        champion_noise=noise,
    )
    assert seeds == [0]
    assert recorded == expected.decision.model_dump(mode="json")
