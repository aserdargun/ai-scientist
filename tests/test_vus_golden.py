"""Compare vendored VUS outputs to independent numpy<2 TSB-AD 1.5 fixtures."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from harness.metrics import vus_metrics

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "docs/ai-scientist/review-evidence/tsb-ad-oracle"


def test_oracle_provenance_hashes_are_pinned() -> None:
    provenance = json.loads((FIXTURE_DIR / "provenance.json").read_text())
    golden_bytes = (FIXTURE_DIR / "golden.json").read_bytes()
    inputs_bytes = (FIXTURE_DIR / "inputs.npz").read_bytes()
    assert hashlib.sha256(golden_bytes).hexdigest() == provenance["golden_sha256"]
    assert hashlib.sha256(inputs_bytes).hexdigest() == provenance["inputs_sha256"]


def _fixture_rows() -> list[dict[str, Any]]:
    return json.loads((FIXTURE_DIR / "golden.json").read_text())


@pytest.mark.parametrize("fixture", _fixture_rows(), ids=lambda item: item["id"])
def test_vendored_vus_matches_numpy_below_two_oracle(fixture: dict[str, Any]) -> None:
    with np.load(FIXTURE_DIR / "inputs.npz", allow_pickle=False) as data:
        labels = data[f"{fixture['id']}_labels"]
        scores = data[f"{fixture['id']}_scores"]
    result = vus_metrics(
        labels,
        scores,
        sliding_window=fixture["sliding_window"],
        threshold_count=fixture["threshold_count"],
    )
    assert abs(result.vus_roc - fixture["vus_roc"]) <= 1e-9
    assert abs(result.vus_pr - fixture["vus_pr"]) <= 1e-9


@pytest.mark.parametrize(
    ("labels", "scores", "window"),
    [
        ([0, 1], [0.0], 1),
        ([0, 2], [0.0, 1.0], 1),
        ([0, 1], [0.0, float("nan")], 1),
        ([0, 1], [0.0, 1.0], 3),
    ],
)
def test_vus_rejects_invalid_fixture_inputs(
    labels: list[int], scores: list[float], window: int
) -> None:
    with pytest.raises(ValueError):
        vus_metrics(labels, scores, sliding_window=window)
