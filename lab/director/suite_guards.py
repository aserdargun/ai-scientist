"""Complete-suite guards that require trusted family identities and raw scores."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, StrictStr

from harness.alarm import position_bias


class SuitePositionBiasCheck(BaseModel):
    """Measured NRM position guard; incomplete inputs can never pass."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    state: Literal["pending", "not_applicable", "pass", "fail"]
    expected_tasks: StrictInt = Field(ge=0)
    measured_tasks: StrictInt = Field(ge=0)
    violating_tasks: StrictInt = Field(ge=0)
    threshold: StrictFloat
    max_bias: StrictFloat | None
    missing_task_ids: tuple[StrictStr, ...]
    task_bias: dict[StrictStr, StrictFloat]

    @property
    def passed(self) -> bool:
        """Whether a complete NRM guard (or explicitly absent NRM family) is safe."""
        return self.state in {"pass", "not_applicable"}


def _spearman_position_bias(scores: Sequence[float]) -> float:
    try:
        return position_bias(scores)
    except ValueError as exc:
        raise ValueError(
            "NRM score vectors must be finite ordered vectors with at least two rows"
        ) from exc


def check_complete_suite_position_bias(
    scores_by_task: Mapping[str, Sequence[float]],
    families_by_task: Mapping[str, str],
    *,
    threshold: float = 0.8,
) -> SuitePositionBiasCheck:
    """Check absolute Spearman score/time bias across every expected NRM task.

    This API consumes ordered raw candidate score vectors. The caller must load
    and hash-check each immutable artifact and bind it to trusted suite identities.
    The guard waits for all NRM tasks. It fails when at least half of complete NRM
    tasks exceed the threshold; with no NRM tasks it reports not-applicable.
    """
    if not np.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
        raise ValueError("position-bias threshold must be finite and in [0,1]")
    if any(not isinstance(key, str) or not key for key in families_by_task):
        raise ValueError("trusted task-family IDs must be non-empty strings")
    if any(value not in {"EVT", "PDM", "NRM"} for value in families_by_task.values()):
        raise ValueError("trusted task family is unsupported")
    expected = sorted(key for key, family in families_by_task.items() if family == "NRM")
    if not expected:
        if scores_by_task:
            raise ValueError("position-bias scores were supplied without trusted NRM tasks")
        return SuitePositionBiasCheck(
            state="not_applicable",
            expected_tasks=0,
            measured_tasks=0,
            violating_tasks=0,
            threshold=float(threshold),
            max_bias=None,
            missing_task_ids=(),
            task_bias={},
        )
    unexpected = set(scores_by_task) - set(expected)
    if unexpected:
        raise ValueError("position-bias score map contains untrusted or non-NRM task identities")
    bias = {key: _spearman_position_bias(scores_by_task[key]) for key in sorted(scores_by_task)}
    missing = tuple(sorted(set(expected) - set(bias)))
    measured = len(bias)
    violations = sum(value > threshold for value in bias.values())
    if missing:
        state: Literal["pending", "pass", "fail"] = "pending"
    elif violations * 2 >= len(expected):
        state = "fail"
    else:
        state = "pass"
    return SuitePositionBiasCheck(
        state=state,
        expected_tasks=len(expected),
        measured_tasks=measured,
        violating_tasks=violations,
        threshold=float(threshold),
        max_bias=max(bias.values()) if bias else None,
        missing_task_ids=missing,
        task_bias=bias,
    )
