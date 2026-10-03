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
import pyarrow as pa
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


def _stream_phase(phase: str) -> int:
    """Separate unscored mode contract; ordinary score/diagnostic v1 stays strict."""
    from lab.operating_modes import OperatingModeCandidate, candidate_source
    from lab.operating_modes.model import OperatingModeModel
    from lab.operating_modes.stream import (
        MAX_CHUNK_BYTES,
        MAX_CHUNK_ROWS,
        MAX_STREAM_OUTPUT_BYTES,
        MAX_TRAIN_BYTES,
        MAX_TRAIN_ROWS,
        canonical_json,
        strict_json,
        validate_sensors,
    )
    from lab.sandbox.mode_stream import MAX_FIT_ARTIFACT_BYTES, StreamFit, StreamPhaseContext

    context = _context()
    validate_sensors(context.signals)
    request = StreamPhaseContext.model_validate_json(
        canonical_json(strict_json(os.environ["SWAPP_STREAM_CONTEXT"].encode(), limit=16384))
    )
    training = phase == "mode_stream_fit"
    if (
        context.seed >= 2**32
        or context.regime_signals
        or (request.fit_artifact_sha256 is None) != training
        or Path("/candidate/candidate.py").read_bytes() != candidate_source(request.configuration)
    ):
        raise ValueError("stream sandbox source/context mismatch")
    random.seed(context.seed)
    np.random.seed(context.seed)
    byte_limit = MAX_TRAIN_BYTES if training else MAX_CHUNK_BYTES
    payload = sys.stdin.buffer.read(byte_limit + 1)
    if len(payload) > byte_limit:
        raise ValueError("stream Arrow byte limit exceeded")
    reader = ipc.open_stream(payload)
    if tuple(reader.schema.names) != context.signals or any(
        not pa.types.is_float64(field.type) for field in reader.schema
    ):
        raise ValueError("stream Arrow requires only approved float64 sensors")
    batches = []
    rows = 0
    for batch in reader:
        rows += batch.num_rows
        if rows > (MAX_TRAIN_ROWS if training else MAX_CHUNK_ROWS):
            raise ValueError("stream Arrow row limit exceeded")
        batches.append(batch)
    if rows < (16 if training else 1) or not training and request.row_offset + rows > 65536:
        raise ValueError("invalid stream row count")
    frame = cast(pd.DataFrame, pa.Table.from_batches(batches, reader.schema).to_pandas())
    if np.isinf(frame.to_numpy(dtype=float)).any():
        raise ValueError("infinite stream input")
    if training:
        module = _candidate_module()
        pipeline = module.build_candidate()
        if type(pipeline) is not OperatingModeCandidate:
            raise ValueError("stream fit requires exact OperatingModeCandidate")
        pipeline.fit(frame, context)
        if type(pipeline.model) is not OperatingModeModel:
            raise ValueError("stream fit did not produce the exact operating mode model")
        artifact = pickle.dumps(pipeline, protocol=5)
        document = canonical_json(
            {
                "schema_version": "mode-stream-fit.v1",
                "scoring_available": False,
                "input_sha256": request.input_sha256,
                "candidate_sha256": request.candidate_sha256,
                "configuration": request.configuration.model_dump(mode="json"),
                "configuration_sha256": request.configuration_sha256,
                "repetition_seed": context.seed,
                "sampling_seconds": context.sampling_s,
                "fit_artifact_sha256": hashlib.sha256(artifact).hexdigest(),
                "model": pipeline.model.summary(),
            }
        )
        StreamFit(artifact, document)
        Path("/output/model.bin").write_bytes(artifact)
        Path("/output/stream-fit.json").write_bytes(document)
        return 0
    with Path("/fit-artifact/model.bin").open("rb") as handle:
        artifact = handle.read(MAX_FIT_ARTIFACT_BYTES + 1)
    if (
        len(artifact) > MAX_FIT_ARTIFACT_BYTES
        or hashlib.sha256(artifact).hexdigest() != request.fit_artifact_sha256
    ):
        raise ValueError("stream frozen artifact identity mismatch")
    # This function runs only in the candidate container, never on the host.
    pipeline = pickle.loads(artifact)  # nosec B301
    if (
        type(pipeline) is not OperatingModeCandidate
        or type(pipeline.model) is not OperatingModeModel
        or pipeline.config != request.configuration
        or pipeline.model.model_sha256 != request.model_sha256
        or pipeline.model.sensors != context.signals
        or pipeline.model.config
        != request.configuration.model_copy(
            update={"seed": (request.configuration.seed + context.seed) % 2**32}
        )
    ):
        raise ValueError("stream frozen model identity mismatch")
    prediction = pipeline.model.predict(
        frame, alarm_state=request.alarm_state, row_offset=request.row_offset
    )
    if pipeline.model.model_sha256 != request.model_sha256:
        raise ValueError("stream model changed during prediction")
    document = canonical_json(
        {
            "schema": "mode-stream-prediction.v1",
            "scoring_available": False,
            "input_sha256": request.input_sha256,
            "candidate_sha256": request.candidate_sha256,
            "configuration_sha256": request.configuration_sha256,
            "fit_artifact_sha256": request.fit_artifact_sha256,
            "model_sha256": request.model_sha256,
            "row_offset": request.row_offset,
            "alarm_in": request.alarm_state.model_dump(mode="json"),
            "prediction": prediction.to_dict(),
        }
    )
    if len(document) > MAX_STREAM_OUTPUT_BYTES:
        raise ValueError("stream prediction exceeds 2 MiB encoded output")
    Path("/output/stream-prediction.json").write_bytes(document)
    return 0


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {
        "fit",
        "score",
        "mode_stream_fit",
        "mode_stream_predict",
    }:
        return 64
    if sys.argv[1] in {"mode_stream_fit", "mode_stream_predict"}:
        return _stream_phase(sys.argv[1])
    module = _candidate_module()
    return _fit(module) if sys.argv[1] == "fit" else _score(module)


if __name__ == "__main__":
    raise SystemExit(main())
