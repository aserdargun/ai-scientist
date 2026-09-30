"""Promoted checkpoints persist a metric that matches each task family."""

import pytest

from lab.director.loop import _required_promoted_task_score


def test_promoted_public_family_score_uses_task_score_without_vus() -> None:
    assert _required_promoted_task_score({"task_family": "PDM", "task_score": 0.7}) == 0.7
    assert _required_promoted_task_score({"task_family": "NRM", "task_score": 0.2}) == 0.2


def test_promoted_non_evt_cannot_fall_back_to_vus() -> None:
    with pytest.raises(RuntimeError, match="task_score"):
        _required_promoted_task_score({"task_family": "PDM", "vus_pr": 0.9})


def test_promoted_evt_prefers_common_score_and_reads_legacy_rows() -> None:
    assert (
        _required_promoted_task_score({"task_family": "EVT", "task_score": 0.6, "vus_pr": 0.5})
        == 0.6
    )
    assert _required_promoted_task_score({"vus_pr": 0.5}) == 0.5
