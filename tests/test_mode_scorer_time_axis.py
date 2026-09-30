"""The registered mode UTC axis reaches both EVT and NRM scoring unchanged."""

from __future__ import annotations

import pytest

from lab.director.public_suite import task_semantics_sha256
from lab.scorer.service import _strict_ordered_times
from lab.scorer.task_semantics import validate_evt_semantics
from tests.test_public_scorer_families import _score_family


def validate(times: list[str], *, digest: str | None = None) -> list[bool]:
    masks = [False] * len(times)
    expected = task_semantics_sha256(
        task_family="EVT",
        sampling_s=1,
        evaluation_times=tuple(times),
        masked_samples=tuple(masks),
        failure_windows=(),
    )
    return validate_evt_semantics(
        sample_count=len(times),
        sampling_s=1,
        evaluation_times=times,
        masked_samples=masks,
        failure_windows=[],
        semantics_sha256=digest or expected,
    )


@pytest.mark.parametrize("suffix", ["", "+00:00"])
def test_evt_and_nrm_accept_original_naive_or_mode_utc_axis(suffix: str) -> None:
    times = [f"2024-01-01T00:03:{second}{suffix}" for second in (22, 23, 24)]
    original = times.copy()
    assert validate(times) == [False] * 3
    assert _strict_ordered_times(times, 1)
    assert times == original
    with pytest.raises(ValueError, match="digest"):
        validate(times, digest="0" * 64)


@pytest.mark.parametrize(
    "times",
    [
        ["2024-01-01T00:03:22", "2024-01-01T00:03:23+00:00"],
        ["2024-01-01T00:03:22+00:00", "2024-01-01T00:03:23"],
        ["2024-01-01T00:03:22+03:00", "2024-01-01T00:03:23+03:00"],
        ["2024-01-01T00:03:22Z", "2024-01-01T00:03:23Z"],
        ["2024-01-01 00:03:22+00:00", "2024-01-01 00:03:23+00:00"],
        ["2024-01-01T00:03:22+00:00", "2024-01-01T00:03:24+00:00"],
    ],
)
def test_evt_and_nrm_reject_mixed_noncanonical_or_wrong_cadence(times: list[str]) -> None:
    with pytest.raises(ValueError):
        validate(times)
    assert not _strict_ordered_times(times, 1)


@pytest.mark.parametrize("family", ["PDM", "NRM"])
def test_actual_family_scorer_accepts_registered_utc_semantics(family: str) -> None:
    metrics = _score_family(family, utc_axis=True)
    assert metrics["task_family"] == family
