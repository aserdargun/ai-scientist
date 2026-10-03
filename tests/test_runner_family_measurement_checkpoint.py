"""Checkpoint resume validates family-specific primary scorer measurements."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from lab.director.runner import _read_measurement


class Checkpoint:
    def __init__(self, measurement: dict[str, Any]) -> None:
        self.measurement = measurement

    def read_checkpoint(self, **_: Any) -> dict[str, Any]:
        return {"payload": {"experiment_id": "exp_" + "a" * 32, "measurement": self.measurement}}


def _measurement(family: str | None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "candidate_sha256": "1" * 64,
        "dataset_id": "dataset",
        "split_id": "dev",
        "session_id": "session",
        "profile_sha256": "2" * 64,
        "harness_sha256": "3" * 64,
        "candidate_output_sha256": "4" * 64,
        "fit_seconds": 0.1,
        "score_seconds": 0.2,
        "guards": {"hardcoding": "pass", "determinism": "pass", "causality": "pass"},
    }
    if family is not None:
        row["task_family"] = family
    if family == "EVT" or family is None:
        row.update(vus_pr=0.4, vus_roc=0.6)
    else:
        row.update(task_score=0.7, fa_per_day=0.5, duty_fraction=0.1)
        if family == "NRM":
            row["position_bias"] = 0.2
    return row


def _read(family: str, row: dict[str, Any]) -> dict[str, Any] | None:
    task = SimpleNamespace(
        family=family,
        dataset_id="dataset",
        split_id="dev",
        session_id="session",
        profile_sha256="2" * 64,
        task_id="task",
    )
    return _read_measurement(
        object(),
        lease=Checkpoint(row),
        run_id=uuid4(),
        experiment_id="exp_" + "a" * 32,
        evaluation_kind="primary",
        seed=0,
        task=task,
        candidate_sha256="1" * 64,
        harness_sha256="3" * 64,
        experiment_number=1,
        artifact_root=Path("."),
    )


@pytest.mark.parametrize("family", ["PDM", "NRM"])
def test_family_checkpoint_accepts_primary_score_without_vus(family: str) -> None:
    assert _read(family, _measurement(family))["task_score"] == 0.7  # type: ignore[index]


def test_legacy_evt_checkpoint_without_family_remains_readable() -> None:
    assert _read("EVT", _measurement(None))["vus_pr"] == 0.4  # type: ignore[index]


@pytest.mark.parametrize(
    ("family", "mutate"),
    [
        ("PDM", lambda row: row.update(task_family="NRM")),
        ("NRM", lambda row: row.update(task_score=float("nan"))),
        ("NRM", lambda row: row.update(position_bias=1.1)),
        ("PDM", lambda row: row.update(duty_fraction=float("inf"))),
        ("PDM", lambda row: row.pop("task_family")),
        ("PDM", lambda row: row.update(fit_seconds=float("nan"))),
        ("NRM", lambda row: row.update(score_seconds=float("inf"))),
    ],
)
def test_checkpoint_rejects_family_mismatch_or_invalid_family_measurement(
    family: str, mutate: Any
) -> None:
    row = _measurement(family)
    mutate(row)
    with pytest.raises(RuntimeError):
        _read(family, row)


@pytest.mark.parametrize("family", ["EVT", "PDM", "NRM"])
def test_optional_wall_timings_survive_checkpoint_replay_without_filling_legacy(family):
    row = _measurement(family)
    old = _read(family, row)
    assert "guarded_wall_seconds" not in old
    assert "scorer_wall_seconds" not in old
    row.update(guarded_wall_seconds=14.0, scorer_wall_seconds=0)
    assert _read(family, row) == row


@pytest.mark.parametrize("field", ["guarded_wall_seconds", "scorer_wall_seconds"])
@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True, None, "0.1", 10**1000])
def test_optional_wall_timings_cannot_bypass_checkpoint_validation(field, value):
    row = _measurement("EVT")
    row[field] = value
    with pytest.raises(RuntimeError, match="timing|provenance"):
        _read("EVT", row)
