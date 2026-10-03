"""P1-only unscored mode replay; candidate model bytes stay opaque on the host."""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.ipc as ipc
from pydantic import Field, model_validator

from harness.contracts import FitContext
from lab.operating_modes.candidate import candidate_source
from lab.operating_modes.contracts import AlarmState, ModeConfig, PredictionBatch, StrictModel
from lab.operating_modes.stream import (
    MAX_CHUNK_BYTES,
    MAX_CHUNK_ROWS,
    MAX_EVALUATION_ROWS,
    MAX_STREAM_OUTPUT_BYTES,
    MAX_TRAIN_BYTES,
    MAX_TRAIN_ROWS,
    canonical_json,
    configuration_sha256,
    strict_json,
    validate_sensors,
)

if TYPE_CHECKING:
    from lab.sandbox.docker_runner import LocalDockerRunner, SandboxResult

MAX_FIT_ARTIFACT_BYTES = 32 * 1024**2
STREAM_PHASES = frozenset({"mode_stream_fit", "mode_stream_predict"})
_DIGEST = r"^[0-9a-f]{64}$"


class StreamPhaseContext(StrictModel):
    """Explicit sandbox request, including the incoming alarm/cursor state."""

    schema_version: Literal["mode-stream-phase.v1"] = "mode-stream-phase.v1"
    input_sha256: str = Field(pattern=_DIGEST)
    configuration: ModeConfig
    configuration_sha256: str = Field(pattern=_DIGEST)
    candidate_sha256: str = Field(pattern=_DIGEST)
    fit_artifact_sha256: str | None = Field(default=None, pattern=_DIGEST)
    model_sha256: str | None = Field(default=None, pattern=_DIGEST)
    row_offset: int = Field(default=0, ge=0, lt=MAX_EVALUATION_ROWS)
    alarm_state: AlarmState = AlarmState()

    @model_validator(mode="after")
    def identities(self) -> StreamPhaseContext:
        if (
            self.configuration_sha256 != configuration_sha256(self.configuration)
            or self.candidate_sha256
            != hashlib.sha256(candidate_source(self.configuration)).hexdigest()
            or (self.fit_artifact_sha256 is None) != (self.model_sha256 is None)
            or self.fit_artifact_sha256 is None
            and (self.row_offset != 0 or self.alarm_state != AlarmState())
        ):
            raise ValueError("invalid stream phase identity")
        validate_alarm_state(self.alarm_state, self.configuration)
        return self


def validate_alarm_state(state: AlarmState, config: ModeConfig) -> None:
    if state.pending >= config.dwell or (state.active and state.pending):
        raise ValueError("invalid stream alarm continuation state")


def _finite(value: Any, *, minimum: float | None = None) -> bool:
    return (
        type(value) in (int, float)
        and math.isfinite(value)
        and (minimum is None or value >= minimum)
    )


def _count(value: Any, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _summary(value: dict[str, Any], config: ModeConfig, seed: int) -> None:
    expected_keys = {
        "contract_version",
        "model_sha256",
        "config",
        "sensors",
        "training_rows",
        "reference_rows",
        "dropped_rows",
        "training_minimum",
        "training_ranges",
        "excluded_sensors",
        "noise_rows",
        "alarm_threshold",
        "alarm_release",
        "calibration_count",
        "modes",
        "diagnostics",
    }
    if set(value) != expected_keys or value["contract_version"] != "operating-modes.omr.v1":
        raise ValueError("invalid stream model summary")
    sensors = value["sensors"]
    if not isinstance(sensors, list) or any(not isinstance(v, str) for v in sensors):
        raise ValueError("invalid model sensors")
    validate_sensors(tuple(sensors))
    if (
        value["config"]
        != config.model_copy(update={"seed": (config.seed + seed) % 2**32}).model_dump(mode="json")
        or not _count(value["training_rows"], 16, MAX_TRAIN_ROWS)
        or not _count(value["reference_rows"], 2, MAX_TRAIN_ROWS)
        or not _count(value["dropped_rows"], 0, MAX_TRAIN_ROWS)
        or not _count(value["noise_rows"], 0, value["reference_rows"])
        or not _count(value["calibration_count"], 0, MAX_TRAIN_ROWS)
        or not isinstance(value["model_sha256"], str)
        or len(value["model_sha256"]) != 64
        or any(char not in "0123456789abcdef" for char in value["model_sha256"])
        or not isinstance(value["diagnostics"], dict)
    ):
        raise ValueError("stream model identity/count mismatch")
    for key in ("training_minimum", "training_ranges"):
        if (
            not isinstance(value[key], list)
            or len(value[key]) != len(sensors)
            or any(
                not _finite(v, minimum=0 if key == "training_ranges" else None) for v in value[key]
            )
        ):
            raise ValueError("invalid stream model ranges")
    threshold, release = value["alarm_threshold"], value["alarm_release"]
    if threshold is None:
        if release is not None:
            raise ValueError("invalid uncalibrated model alarm")
    elif not _finite(threshold, minimum=0) or release != threshold * config.release_ratio:
        raise ValueError("invalid stream model alarm")
    modes = value["modes"]
    if not isinstance(modes, list) or len(modes) > MAX_TRAIN_ROWS // config.min_support:
        raise ValueError("invalid stream mode count")
    support = value["noise_rows"]
    for index, mode in enumerate(modes):
        if (
            not isinstance(mode, dict)
            or set(mode) != {"mode_id", "support", "center", "tolerance"}
            or type(mode["mode_id"]) is not int
            or mode["mode_id"] != index
            or not _count(mode["support"], config.min_support, MAX_TRAIN_ROWS)
            or not isinstance(mode["center"], list)
            or len(mode["center"]) != len(sensors)
            or any(not _finite(v) for v in mode["center"])
            or not _finite(mode["tolerance"], minimum=0)
        ):
            raise ValueError("invalid stream mode summary")
        support += mode["support"]
    if support != value["reference_rows"]:
        raise ValueError("stream reference support mismatch")


class _FitDocument(StrictModel):
    schema_version: Literal["mode-stream-fit.v1"] = "mode-stream-fit.v1"
    scoring_available: Literal[False] = False
    input_sha256: str = Field(pattern=_DIGEST)
    candidate_sha256: str = Field(pattern=_DIGEST)
    configuration: ModeConfig
    configuration_sha256: str = Field(pattern=_DIGEST)
    repetition_seed: int = Field(ge=0, le=2**32 - 1)
    sampling_seconds: int | None = Field(default=1, ge=1)
    fit_artifact_sha256: str = Field(pattern=_DIGEST)
    model: dict[str, Any]

    @model_validator(mode="after")
    def identities(self) -> _FitDocument:
        if (
            self.configuration_sha256 != configuration_sha256(self.configuration)
            or self.candidate_sha256
            != hashlib.sha256(candidate_source(self.configuration)).hexdigest()
        ):
            raise ValueError("stream fit source/configuration mismatch")
        _summary(self.model, self.configuration, self.repetition_seed)
        return self


@dataclass(frozen=True, slots=True)
class StreamFit:
    """Reconstructable from safe JSON plus opaque bytes, never a host pickle load."""

    fit_artifact: bytes
    document: bytes

    def __post_init__(self) -> None:
        if (
            type(self.fit_artifact) is not bytes
            or not 1 <= len(self.fit_artifact) <= MAX_FIT_ARTIFACT_BYTES
        ):
            raise ValueError("stream fit artifact exceeds 32 MiB or is empty")
        value = strict_json(self.document, limit=MAX_STREAM_OUTPUT_BYTES)
        metadata = _FitDocument.model_validate_json(canonical_json(value))
        if metadata.fit_artifact_sha256 != hashlib.sha256(self.fit_artifact).hexdigest():
            raise ValueError("stream fit artifact hash mismatch")
        if canonical_json(metadata.model_dump(mode="json")) != self.document:
            raise ValueError("stream fit document is not canonical")

    @property
    def metadata(self) -> dict[str, Any]:
        return cast(dict[str, Any], strict_json(self.document, limit=MAX_STREAM_OUTPUT_BYTES))

    @property
    def fit_artifact_sha256(self) -> str:
        return cast(str, self.metadata["fit_artifact_sha256"])

    @property
    def model_summary(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.metadata["model"])

    @property
    def model_sha256(self) -> str:
        return cast(str, self.model_summary["model_sha256"])

    @property
    def candidate_sha256(self) -> str:
        return cast(str, self.metadata["candidate_sha256"])

    @property
    def configuration_sha256(self) -> str:
        return cast(str, self.metadata["configuration_sha256"])

    @property
    def input_sha256(self) -> str:
        return cast(str, self.metadata["input_sha256"])


def _arrow(frame: pd.DataFrame, context: FitContext, *, training: bool) -> bytes:
    validate_sensors(context.signals)
    if (
        type(context.seed) is not int
        or not 0 <= context.seed < 2**32
        or context.sampling_s is not None
        and (type(context.sampling_s) is not int or context.sampling_s < 1)
        or context.regime_signals
        or not _finite(context.time_budget_s, minimum=0)
        or context.time_budget_s <= 0
        or not isinstance(frame, pd.DataFrame)
        or tuple(frame.columns) != context.signals
        or not (16 if training else 1)
        <= len(frame)
        <= (MAX_TRAIN_ROWS if training else MAX_CHUNK_ROWS)
    ):
        raise ValueError("invalid bounded stream frame/context")
    values = frame.to_numpy(dtype=np.float64, copy=True)
    if np.isinf(values).any():
        raise ValueError("infinite stream input")
    table = pa.Table.from_pandas(
        pd.DataFrame(values, columns=context.signals), preserve_index=False
    ).replace_schema_metadata(None)
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    payload = cast(bytes, sink.getvalue().to_pybytes())
    if len(payload) > (MAX_TRAIN_BYTES if training else MAX_CHUNK_BYTES):
        raise ValueError("stream input byte bound exceeded")
    return payload


def _remaining(deadline: float, runner: LocalDockerRunner) -> int:
    from lab.sandbox.docker_runner import SandboxRunError

    if not _finite(deadline):
        raise ValueError("invalid original stream phase deadline")
    remaining = math.floor(deadline - time.monotonic())
    if remaining < 1:
        raise SandboxRunError("stream phase timed out")
    return min(remaining, runner.profile.timeout_seconds)


def _phase(
    runner: LocalDockerRunner,
    *,
    context: FitContext,
    request: StreamPhaseContext,
    arrow: bytes,
    fit: bytes | None,
    deadline: float,
    admission_check: Callable[[], None] | None,
) -> SandboxResult:
    _remaining(deadline, runner)
    if admission_check is not None:
        admission_check()
    result = runner.run_phase(
        phase="mode_stream_fit" if fit is None else "mode_stream_predict",
        candidate_source=candidate_source(request.configuration),
        arrow_input=arrow,
        fit_artifact=fit,
        contract_context=context,
        stream_context=request.model_dump(mode="json"),
        timeout_seconds=_remaining(deadline, runner),
        deadline=deadline,
        admission_check=admission_check,
    )
    if admission_check is not None:
        admission_check()
    _remaining(deadline, runner)
    if result.exit_code != 0:
        from lab.sandbox.docker_runner import SandboxRunError

        raise SandboxRunError("stream sandbox returned nonzero", exit_code=result.exit_code)
    return result


def _artifacts(result: SandboxResult, names: set[str]) -> dict[str, bytes]:
    if len(result.artifacts) != len(names) or {v.name for v in result.artifacts} != names:
        raise ValueError("unexpected stream sandbox artifacts")
    output = {}
    for artifact in result.artifacts:
        bound = MAX_FIT_ARTIFACT_BYTES if artifact.name == "model.bin" else MAX_STREAM_OUTPUT_BYTES
        if (
            not 1 <= len(artifact.content) <= bound
            or artifact.size_bytes != len(artifact.content)
            or hashlib.sha256(artifact.content).hexdigest() != artifact.sha256
        ):
            raise ValueError("invalid stream artifact checksum/byte bound")
        output[artifact.name] = artifact.content
    return output


def _assert_fit(
    fit: StreamFit,
    config: ModeConfig,
    context: FitContext,
    input_sha256: str,
) -> None:
    metadata = fit.metadata
    if (
        fit.input_sha256 != input_sha256
        or fit.configuration_sha256 != configuration_sha256(config)
        or metadata["repetition_seed"] != context.seed
        or metadata["sampling_seconds"] != context.sampling_s
        or fit.model_summary["sensors"] != list(context.signals)
    ):
        raise ValueError("frozen stream fit does not match requested source/config/context")


def run_stream_fit(
    runner: LocalDockerRunner,
    *,
    config: ModeConfig,
    train: pd.DataFrame,
    context: FitContext,
    input_sha256: str,
    deadline: float,
    admission_check: Callable[[], None] | None = None,
) -> StreamFit:
    """Fit exactly once in the existing P1 sandbox; export candidate-derived metadata."""
    request = StreamPhaseContext(
        input_sha256=input_sha256,
        configuration=config,
        configuration_sha256=configuration_sha256(config),
        candidate_sha256=hashlib.sha256(candidate_source(config)).hexdigest(),
    )
    result = _phase(
        runner,
        context=context,
        request=request,
        arrow=_arrow(train, context, training=True),
        fit=None,
        deadline=deadline,
        admission_check=admission_check,
    )
    artifacts = _artifacts(result, {"model.bin", "stream-fit.json"})
    fit = StreamFit(artifacts["model.bin"], artifacts["stream-fit.json"])
    _assert_fit(fit, config, context, input_sha256)
    if fit.model_summary["training_rows"] != len(train):
        raise ValueError("stream fit training count mismatch")
    _remaining(deadline, runner)
    return fit


def _validate_prediction(
    document: bytes,
    *,
    request: StreamPhaseContext,
    fit: StreamFit,
    frame: pd.DataFrame,
) -> PredictionBatch:
    value = strict_json(document, limit=MAX_STREAM_OUTPUT_BYTES)
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "scoring_available",
        "input_sha256",
        "candidate_sha256",
        "configuration_sha256",
        "fit_artifact_sha256",
        "model_sha256",
        "row_offset",
        "alarm_in",
        "prediction",
    }:
        raise ValueError("invalid stream prediction envelope")
    bindings = {
        "schema": "mode-stream-prediction.v1",
        "scoring_available": False,
        "input_sha256": request.input_sha256,
        "candidate_sha256": request.candidate_sha256,
        "configuration_sha256": request.configuration_sha256,
        "fit_artifact_sha256": request.fit_artifact_sha256,
        "model_sha256": request.model_sha256,
        "row_offset": request.row_offset,
        "alarm_in": request.alarm_state.model_dump(mode="json"),
    }
    if (
        any(value[key] != expected for key, expected in bindings.items())
        or type(value["row_offset"]) is not int
        or value["scoring_available"] is not False
    ):
        raise ValueError("stream prediction cursor/source/fit binding mismatch")
    raw = value["prediction"]
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("rows"), list)
        or len(raw["rows"]) != len(frame)
        or not 1 <= len(raw["rows"]) <= MAX_CHUNK_ROWS
        or any(
            not isinstance(row, dict)
            or not isinstance(row.get("residuals"), list)
            or len(row["residuals"]) != len(frame.columns)
            for row in raw["rows"]
        )
    ):
        raise ValueError("invalid stream prediction dimensions")
    batch = PredictionBatch.model_validate_json(canonical_json(raw))
    validate_prediction_batch(
        batch, fit=fit, frame=frame, row_offset=request.row_offset, alarm_state=request.alarm_state
    )
    return batch


def validate_prediction_batch(
    prediction: PredictionBatch,
    *,
    fit: StreamFit,
    frame: pd.DataFrame,
    row_offset: int,
    alarm_state: AlarmState,
) -> None:
    """Verify persisted diagnostics and alarm continuity using only safe fit metadata."""
    batch = prediction
    if len(canonical_json(batch.to_dict())) > MAX_STREAM_OUTPUT_BYTES:
        raise ValueError("stream prediction exceeds encoded byte bound")
    summary = fit.model_summary
    config = ModeConfig.model_validate(fit.metadata["configuration"])
    validate_alarm_state(alarm_state, config)
    if (
        not _count(row_offset, 0, MAX_EVALUATION_ROWS - 1)
        or not 1 <= len(frame) <= MAX_CHUNK_ROWS
        or len(batch.rows) != len(frame)
        or row_offset + len(frame) > MAX_EVALUATION_ROWS
        or list(frame.columns) != summary["sensors"]
        or row_offset == 0
        and alarm_state != AlarmState()
    ):
        raise ValueError("invalid stream prediction frame/cursor")
    if batch.model_sha256 != fit.model_sha256:
        raise ValueError("stream model changed across prediction")
    state = alarm_state
    threshold, release = summary["alarm_threshold"], summary["alarm_release"]
    values = frame.to_numpy(dtype=float)
    for index, row in enumerate(batch.rows):
        if row.index != row_offset + index or tuple(r.sensor for r in row.residuals) != tuple(
            frame.columns
        ):
            raise ValueError("stream prediction offset or sensor order mismatch")
        for sensor_index, residual in enumerate(row.residuals):
            actual = values[index, sensor_index]
            if (
                residual.actual != (float(actual) if np.isfinite(actual) else None)
                or residual.training_range != summary["training_ranges"][sensor_index]
            ):
                raise ValueError("stream residual input/range mismatch")
        alarm = None
        if row.omr_percent is None or threshold is None:
            state = AlarmState(active=state.active, pending=0)
        elif state.active:
            state = AlarmState(active=row.omr_percent > release)
            alarm = state.active
        else:
            pending = state.pending + 1 if row.omr_percent > threshold else 0
            state = AlarmState(
                active=pending >= config.dwell, pending=0 if pending >= config.dwell else pending
            )
            alarm = state.active
        if row.alarm != alarm:
            raise ValueError("stream alarm continuity mismatch")
    if batch.alarm_state != state:
        raise ValueError("stream final alarm state mismatch")


def run_stream_chunk(
    runner: LocalDockerRunner,
    *,
    config: ModeConfig,
    frame: pd.DataFrame,
    context: FitContext,
    input_sha256: str,
    fit: StreamFit,
    row_offset: int,
    alarm_state: AlarmState,
    deadline: float,
    admission_check: Callable[[], None] | None = None,
) -> PredictionBatch:
    """Run one bounded diagnostic-only prediction from the same immutable fit."""
    _assert_fit(fit, config, context, input_sha256)
    request = StreamPhaseContext(
        input_sha256=input_sha256,
        configuration=config,
        configuration_sha256=fit.configuration_sha256,
        candidate_sha256=fit.candidate_sha256,
        fit_artifact_sha256=fit.fit_artifact_sha256,
        model_sha256=fit.model_sha256,
        row_offset=row_offset,
        alarm_state=alarm_state,
    )
    if row_offset + len(frame) > MAX_EVALUATION_ROWS:
        raise ValueError("stream prediction exceeds total evaluation rows")
    arrow = _arrow(frame, context, training=False)
    # Validate against the same frozen values sent to the sandbox, not a mutable caller frame.
    frozen = cast(pd.DataFrame, ipc.open_stream(arrow).read_all().to_pandas())
    result = _phase(
        runner,
        context=context,
        request=request,
        arrow=arrow,
        fit=fit.fit_artifact,
        deadline=deadline,
        admission_check=admission_check,
    )
    document = _artifacts(result, {"stream-prediction.json"})["stream-prediction.json"]
    prediction = _validate_prediction(document, request=request, fit=fit, frame=frozen)
    _remaining(deadline, runner)
    return prediction
