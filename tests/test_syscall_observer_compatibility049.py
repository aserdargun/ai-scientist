"""Small opt-in observer/image compatibility gate; never runs in ordinary CPU tests."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from harness.contracts import FitContext
from lab.operating_modes import ModeConfig, candidate_source
from lab.sandbox.docker_runner import DEFAULT_SANDBOX_IMAGE, LocalDockerRunner, SandboxProfile
from lab.sandbox.evaluation import run_candidate_fit_score
from lab.scorer.mode_diagnostics import validate_mode_diagnostics

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("SWAPP_RUN_SYSCALL_AUDIT_LIVE") != "1",
        reason="requires explicit isolated syscall-audit live opt-in",
    ),
]


def test_observer_keeps_numeric_fit_score_threads_and_mode_diagnostics(tmp_path: Path) -> None:
    """Canonical source preserves pickle, Arrow and diagnostics in fresh phases."""
    assert os.environ.get("SWAPP_SYSCALL_AUDIT_IMAGE") == DEFAULT_SANDBOX_IMAGE
    config = ModeConfig(method="lsh", min_support=2, k=2, lsh_width=1.0)
    # Diagnostics bind this exact canonical source; thread coverage is in the
    # existing raw benign node, preserving the fixed six-container probe.
    source = candidate_source(config)
    x = np.linspace(0.0, 1.0, 24)
    train = pd.DataFrame({"sensor_a": x, "sensor_b": np.sin(x * 3.0)})
    y = np.array([0.05, 0.18, 0.26, 0.49, 0.68, 0.81, 1.15, 1.8])
    evaluation = pd.DataFrame({"sensor_a": y, "sensor_b": np.sin(y * 3.0)})
    runner = LocalDockerRunner(
        image=DEFAULT_SANDBOX_IMAGE,
        work_root=tmp_path,
        profile=SandboxProfile(
            memory_bytes=512 * 1024**2,
            cpus=1.0,
            pids=32,
            timeout_seconds=120,
            output_bytes=1024**2,
        ),
    )
    result = run_candidate_fit_score(
        runner,
        candidate_source=source,
        train=train,
        evaluation=evaluation,
        context=FitContext(
            seed=1, signals=tuple(train.columns), regime_signals=(), sampling_s=1, time_budget_s=75
        ),
        remaining_seconds=120,
        fit_timeout_seconds=75,
        score_timeout_seconds=45,
    )
    document = json.loads(result.score_document)
    diagnostics = validate_mode_diagnostics(
        document["mode_diagnostics"],
        list(result.scores),
        candidate_sha256=hashlib.sha256(source).hexdigest(),
        seed=1,
    )
    assert diagnostics["fit_artifact_sha256"] == result.fit_artifact_sha256
    assert len(result.scores) == len(evaluation)
    assert result.fit_container_name != result.score_container_name
    print(
        json.dumps(
            {
                "schema": "observer-compatibility049.v1",
                "image": DEFAULT_SANDBOX_IMAGE,
                "candidate_sha256": hashlib.sha256(source).hexdigest(),
                "fit_artifact_sha256": result.fit_artifact_sha256,
                "fit_seconds": result.fit_seconds,
                "score_seconds": result.score_seconds,
                "rows": len(result.scores),
                "mode_diagnostics_valid": True,
                "canonical_mode_candidate_source": True,
            },
            sort_keys=True,
        )
    )
