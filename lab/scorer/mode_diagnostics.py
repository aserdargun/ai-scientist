"""Bounded candidate-derived diagnostics; trusted performance remains Scorer-owned."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from lab.operating_modes import ModeConfig, PredictionBatch, candidate_source

MAX_DIAGNOSTIC_BYTES = 2 * 1024 * 1024


def validate_mode_diagnostics(
    value: dict[str, Any],
    scores: list[float],
    *,
    candidate_sha256: str | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if (
        len(encoded) > MAX_DIAGNOSTIC_BYTES
        or set(value)
        != {
            "schema",
            "candidate_sha256",
            "configuration",
            "configuration_sha256",
            "repetition_seed",
            "fit_artifact_sha256",
            "model",
            "prediction",
        }
        or value.get("schema") != "candidate-mode-diagnostics.v1"
    ):
        raise ValueError("invalid bounded mode diagnostics")
    config = ModeConfig.model_validate(value["configuration"], strict=True)
    config_bytes = json.dumps(
        config.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    source_sha = hashlib.sha256(candidate_source(config)).hexdigest()
    if (
        value["candidate_sha256"] != source_sha
        or candidate_sha256 is not None
        and candidate_sha256 != source_sha
        or value["configuration_sha256"] != hashlib.sha256(config_bytes).hexdigest()
        or seed is not None
        and value["repetition_seed"] != seed
    ):
        raise ValueError("mode diagnostics source/configuration/seed identity mismatch")
    repetition = value["repetition_seed"]
    if isinstance(repetition, bool) or not isinstance(repetition, int) or repetition < 0:
        raise ValueError("invalid diagnostics seed")
    raw_prediction = value.get("prediction")
    raw_rows = raw_prediction.get("rows") if isinstance(raw_prediction, dict) else None
    if (
        not isinstance(raw_rows, list)
        or not 1 <= len(raw_rows) <= 4096
        or any(
            not isinstance(row, dict)
            or not isinstance(row.get("residuals"), list)
            or not 1 <= len(row["residuals"]) <= 50
            for row in raw_rows
        )
    ):
        raise ValueError("mode diagnostics exceed row or sensor bounds")
    prediction = PredictionBatch.model_validate_json(json.dumps(value["prediction"]), strict=True)
    model = value["model"]
    if (
        not isinstance(model, dict)
        or model.get("model_sha256") != prediction.model_sha256
        or model.get("config")
        != config.model_copy(update={"seed": (config.seed + repetition) % 2**32}).model_dump(
            mode="json"
        )
        or len(prediction.rows) != len(scores)
        or len(scores) > 4096
    ):
        raise ValueError("mode diagnostics model identity or row count mismatch")
    for digest in (source_sha, prediction.model_sha256, value["fit_artifact_sha256"]):
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise ValueError("invalid diagnostic content identity")
    sensors = model.get("sensors")
    if (
        not isinstance(sensors, list)
        or not 1 <= len(sensors) <= 50
        or len(set(sensors)) != len(sensors)
    ):
        raise ValueError("invalid diagnostic sensor shape")
    for index, (row, score) in enumerate(zip(prediction.rows, scores, strict=True)):
        if (
            row.index != index
            or row.omr_percent is None
            or not math.isfinite(score)
            or row.omr_percent != score
            or [item.sensor for item in row.residuals] != sensors
        ):
            raise ValueError("mode diagnostics differ from scalar score output")
    return value
