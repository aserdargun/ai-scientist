"""Trusted typed fit/score entry point executed only inside a candidate container."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import pickle  # nosec B403
import random
import sys
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd
import pyarrow.ipc as ipc

sys.path.insert(0, "/opt/swapp-ai-scientist")

from harness.contracts import ADPipeline, AlarmPolicy, FitContext

FORBIDDEN_COLUMNS = frozenset(
    {"label", "labels", "anomaly", "is_anomaly", "changepoint", "timestamp", "datetime"}
)


def _candidate_module() -> Any:
    spec = importlib.util.spec_from_file_location("candidate_module", "/candidate/candidate.py")
    if spec is None or spec.loader is None:
        raise ValueError("candidate module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _read_frame(context: FitContext) -> pd.DataFrame:
    table = ipc.open_stream(sys.stdin.buffer).read_all()
    if tuple(table.column_names) != context.signals or any(
        name.lower() in FORBIDDEN_COLUMNS for name in context.signals
    ):
        raise ValueError("Arrow input must contain sensor columns only")
    frame = cast(pd.DataFrame, table.to_pandas())
    values = frame.to_numpy(dtype=np.float64)
    if values.ndim != 2 or not len(frame) or not np.isfinite(values).all():
        raise ValueError("Arrow input must be a non-empty finite numeric frame")
    return frame


def _context() -> FitContext:
    payload = json.loads(os.environ["SWAPP_FIT_CONTEXT"])
    if not isinstance(payload, dict) or set(payload) != {
        "seed",
        "signals",
        "regime_signals",
        "sampling_s",
        "time_budget_s",
    }:
        raise ValueError("fit context shape is invalid")
    signals = payload["signals"]
    regimes = payload["regime_signals"]
    if (
        not isinstance(payload["seed"], int)
        or isinstance(payload["seed"], bool)
        or payload["seed"] < 0
        or not isinstance(signals, list)
        or not signals
        or any(not isinstance(name, str) or not name for name in signals)
        or len(signals) != len(set(signals))
        or not isinstance(regimes, list)
        or any(not isinstance(name, str) or name not in signals for name in regimes)
        or (
            payload["sampling_s"] is not None
            and (
                not isinstance(payload["sampling_s"], int)
                or isinstance(payload["sampling_s"], bool)
                or payload["sampling_s"] < 1
            )
        )
        or isinstance(payload["time_budget_s"], bool)
        or not isinstance(payload["time_budget_s"], (int, float))
        or not math.isfinite(float(payload["time_budget_s"]))
        or float(payload["time_budget_s"]) <= 0
    ):
        raise ValueError("fit context values are invalid")
    return FitContext(
        seed=payload["seed"],
        signals=tuple(signals),
        regime_signals=tuple(regimes),
        sampling_s=payload["sampling_s"],
        time_budget_s=float(payload["time_budget_s"]),
    )


def _fit(module: Any) -> int:
    context = _context()
    random.seed(context.seed)
    np.random.seed(context.seed)
    frame = _read_frame(context)
    factory = getattr(module, "build_candidate", None)
    if not callable(factory):
        raise ValueError("candidate must define build_candidate()")
    pipeline = factory()
    if not isinstance(pipeline, ADPipeline):
        raise ValueError("candidate does not implement the typed ADPipeline contract")
    pipeline.fit(frame, context)
    train_scores = np.asarray(pipeline.score(frame), dtype=np.float64)
    if train_scores.shape != (len(frame),):
        return 65
    if not np.isfinite(train_scores).all():
        return 66
    policy = pipeline.alarm_policy(train_scores.copy())
    if not isinstance(policy, AlarmPolicy):
        raise ValueError("alarm_policy must return a validated AlarmPolicy")
    Path("/output/model.bin").write_bytes(pickle.dumps((pipeline, policy), protocol=5))
    Path("/output/policy.json").write_text(
        json.dumps(
            {"threshold": policy.threshold, "release": policy.release, "dwell": policy.dwell},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ),
        encoding="ascii",
    )
    return 0


def _score(module: Any) -> int:
    context = _context()
    random.seed(context.seed)
    np.random.seed(context.seed)
    frame = _read_frame(context)
    # Host and Scorer never deserialize the candidate artifact.
    pipeline, policy = pickle.loads(  # nosec B301
        Path("/fit-artifact/model.bin").read_bytes()
    )
    if not isinstance(pipeline, ADPipeline) or not isinstance(policy, AlarmPolicy):
        raise ValueError("frozen candidate artifact violates the typed pipeline contract")
    scores = np.asarray(pipeline.score(frame), dtype=np.float64)
    if scores.shape != (len(frame),):
        return 65
    if not np.isfinite(scores).all():
        return 66
    if np.all(scores == scores[0]) and os.environ.get("SWAPP_TRUSTED_BASELINE") != "1":
        return 67
    output = {
        "schema": "candidate-scores.v1",
        "sample_indices": list(range(len(frame))),
        "scores": [float(value) for value in scores],
        "alarm_policy": {
            "threshold": policy.threshold,
            "release": policy.release,
            "dwell": policy.dwell,
        },
    }
    from lab.operating_modes import OperatingModeCandidate

    if type(pipeline) is OperatingModeCandidate and pipeline.model is not None:
        config = pipeline.config.model_dump(mode="json")
        config_bytes = json.dumps(
            config, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        output["mode_diagnostics"] = {
            "schema": "candidate-mode-diagnostics.v1",
            "candidate_sha256": hashlib.sha256(
                Path("/candidate/candidate.py").read_bytes()
            ).hexdigest(),
            "configuration": config,
            "configuration_sha256": hashlib.sha256(config_bytes).hexdigest(),
            "repetition_seed": context.seed,
            "fit_artifact_sha256": hashlib.sha256(
                Path("/fit-artifact/model.bin").read_bytes()
            ).hexdigest(),
            "model": pipeline.model.summary(),
            "prediction": pipeline.model.predict(frame).to_dict(),
        }
    Path("/output/scores.json").write_text(
        json.dumps(output, sort_keys=True, separators=(",", ":"), allow_nan=False),
        encoding="ascii",
    )
    return 0


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"fit", "score"}:
        return 64
    module = _candidate_module()
    return _fit(module) if sys.argv[1] == "fit" else _score(module)


if __name__ == "__main__":
    raise SystemExit(main())
