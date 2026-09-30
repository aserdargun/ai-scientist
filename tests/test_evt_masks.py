"""Mask-aware EVT scoring and temporal-semantics validation."""

from __future__ import annotations

import hashlib
import json

import numpy as np
import pytest

from harness.metrics import vus_metrics
from lab.scorer.task_semantics import score_masked_vus, validate_evt_semantics


def _semantics_digest(
    *, sampling_s: int | None, evaluation_times: list[int], masks: list[bool]
) -> str:
    payload = {
        "schema": "public-task-semantics.v1",
        "task_family": "EVT",
        "sampling_s": sampling_s,
        "evaluation_times": evaluation_times,
        "masked_samples": masks,
        "failure_windows": [],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
            "ascii"
        )
    ).hexdigest()


def test_vus_removes_masked_sample_and_preserves_fixed_window() -> None:
    labels = np.asarray([False, False, True, False, True, False], dtype=np.bool_)
    scores = np.asarray([0.1, 0.9, 0.8, 0.2, 0.95, 0.3], dtype=np.float64)
    masks = [False, True, False, False, False, False]

    actual, masked_count = score_masked_vus(
        labels, scores, masks=masks, sliding_window=3, threshold_count=16
    )
    retained = [index for index, masked in enumerate(masks) if not masked]
    expected = vus_metrics(
        labels[retained], scores[retained], sliding_window=3, threshold_count=16
    )
    assert masked_count == 1
    assert actual == expected

    with pytest.raises(ValueError, match="shorter than its fixed training window"):
        score_masked_vus(
            labels, scores, masks=[True, True, True, False, False, False],
            sliding_window=4, threshold_count=16
        )
    with pytest.raises(ValueError, match="strict boolean"):
        score_masked_vus(
            labels, scores, masks=[0, False, False, False, False, False],
            sliding_window=2, threshold_count=16
        )


def test_masked_source_clock_reset_is_allowed_but_unmasked_order_is_strict() -> None:
    times = [10_000, 0, 60, 120]
    masks = [False, True, False, False]
    digest = _semantics_digest(sampling_s=60, evaluation_times=times, masks=masks)

    assert validate_evt_semantics(
        sample_count=4,
        sampling_s=60,
        evaluation_times=times,
        masked_samples=masks,
        failure_windows=[],
        semantics_sha256=digest,
    ) == masks

    invalid_times = [10_000, 0, 120, 240]
    invalid_digest = _semantics_digest(
        sampling_s=60, evaluation_times=invalid_times, masks=masks
    )
    with pytest.raises(ValueError, match="ordering/cadence"):
        validate_evt_semantics(
            sample_count=4,
            sampling_s=60,
            evaluation_times=invalid_times,
            masked_samples=masks,
            failure_windows=[],
            semantics_sha256=invalid_digest,
        )


def test_unknown_cadence_requires_order_within_unmasked_segments() -> None:
    times = [10, -100, 15, 20]
    masks = [False, True, False, False]
    digest = _semantics_digest(sampling_s=None, evaluation_times=times, masks=masks)
    assert validate_evt_semantics(
        sample_count=4,
        sampling_s=None,
        evaluation_times=times,
        masked_samples=masks,
        failure_windows=[],
        semantics_sha256=digest,
    ) == masks

    invalid_times = [10, -100, 9, 8]
    invalid_digest = _semantics_digest(
        sampling_s=None, evaluation_times=invalid_times, masks=masks
    )
    with pytest.raises(ValueError, match="ordering/cadence"):
        validate_evt_semantics(
            sample_count=4,
            sampling_s=None,
            evaluation_times=invalid_times,
            masked_samples=masks,
            failure_windows=[],
            semantics_sha256=invalid_digest,
        )


def test_evt_semantics_rejects_bad_mask_and_digest() -> None:
    times = [0, 60, 120]
    masks = [False, False, False]
    digest = _semantics_digest(sampling_s=60, evaluation_times=times, masks=masks)
    with pytest.raises(ValueError, match="malformed or misaligned"):
        validate_evt_semantics(
            sample_count=3,
            sampling_s=60,
            evaluation_times=times,
            masked_samples=[False, 0, False],
            failure_windows=[],
            semantics_sha256=digest,
        )
    with pytest.raises(ValueError, match="digest"):
        validate_evt_semantics(
            sample_count=3,
            sampling_s=60,
            evaluation_times=times,
            masked_samples=masks,
            failure_windows=[],
            semantics_sha256="0" * 64,
        )
