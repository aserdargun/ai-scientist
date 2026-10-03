"""CPU contract fixtures for stream phases; no Docker/runtime acceptance claimed."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, replace
from io import BytesIO
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pyarrow.ipc as ipc
import pytest

from harness.contracts import FitContext
from lab.operating_modes import AlarmState, ModeConfig, OperatingModeCandidate, candidate_source
from lab.operating_modes.model import fit_model
from lab.operating_modes.stream import MAX_STREAM_OUTPUT_BYTES, canonical_json, configuration_sha256
from lab.sandbox import candidate_entrypoint, mode_stream
from lab.sandbox.docker_runner import (
    DEFAULT_SANDBOX_IMAGE,
    LocalDockerRunner,
    OutputArtifact,
    SandboxProfile,
    SandboxResult,
    SandboxRunError,
)
from lab.scorer.service import parse_candidate_score

INPUT_SHA = "a" * 64


@pytest.fixture
def fixture_model():
    config = ModeConfig(lsh_width=10, lsh_projections=1, lsh_tables=1, lsh_merge_tables=1, dwell=3)
    x = np.linspace(0, 1, 32)
    frame = pd.DataFrame({"a": x, "b": x * 0.5, "constant": 10.0})
    context = FitContext(
        seed=0, signals=tuple(frame.columns), regime_signals=(), sampling_s=1, time_budget_s=60.0
    )
    return config, frame, context, fit_model(frame, config)


def fit_document(config, model, artifact=b"opaque-model-not-a-pickle", *, sampling=1):
    return canonical_json(
        {
            "schema_version": "mode-stream-fit.v1",
            "scoring_available": False,
            "input_sha256": INPUT_SHA,
            "candidate_sha256": hashlib.sha256(candidate_source(config)).hexdigest(),
            "configuration": config.model_dump(mode="json"),
            "configuration_sha256": configuration_sha256(config),
            "repetition_seed": 0,
            "sampling_seconds": sampling,
            "fit_artifact_sha256": hashlib.sha256(artifact).hexdigest(),
            "model": model.summary(),
        }
    )


def output(name, content):
    return OutputArtifact(name, hashlib.sha256(content).hexdigest(), len(content), content)


class FixtureRunner:
    """Return model-derived fixture JSON; this does not execute a sandbox."""

    profile = SandboxProfile(timeout_seconds=60)

    def __init__(self, config, model):
        self.config, self.model = config, model
        self.calls = []
        self.alter = lambda value: None

    def run_phase(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["phase"] == "mode_stream_fit":
            artifacts = (
                output("model.bin", b"opaque-model-not-a-pickle"),
                output(
                    "stream-fit.json",
                    fit_document(
                        self.config, self.model, sampling=kwargs["contract_context"].sampling_s
                    ),
                ),
            )
        else:
            request = mode_stream.StreamPhaseContext.model_validate(kwargs["stream_context"])
            frame = ipc.open_stream(kwargs["arrow_input"]).read_all().to_pandas()
            prediction = self.model.predict(
                frame, row_offset=request.row_offset, alarm_state=request.alarm_state
            )
            value = {
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
            self.alter(value)
            artifacts = (output("stream-prediction.json", canonical_json(value)),)
        return SandboxResult(kwargs["phase"], 0, b"", b"", artifacts, 0.01, "fixture-only")


def run_fit(fixture_model):
    config, train, context, model = fixture_model
    runner = FixtureRunner(config, model)
    fit = mode_stream.run_stream_fit(
        runner,
        config=config,
        train=train,
        context=context,
        input_sha256=INPUT_SHA,
        deadline=time.monotonic() + 50,
    )
    return runner, fit


def chunk(runner, fit, config, frame, context, **kwargs):
    return mode_stream.run_stream_chunk(
        runner,
        config=config,
        frame=frame,
        context=context,
        input_sha256=INPUT_SHA,
        fit=fit,
        row_offset=kwargs.pop("row_offset", 0),
        alarm_state=kwargs.pop("alarm_state", AlarmState()),
        deadline=kwargs.pop("deadline", time.monotonic() + 50),
        **kwargs,
    )


def test_host_never_deserializes_fit_and_continuation_matches_whole_prediction(
    fixture_model, monkeypatch
):
    import pickle

    monkeypatch.setattr(pickle, "loads", lambda *_a, **_k: pytest.fail("host unpickle"))
    config, train, context, model = fixture_model
    runner, fit = run_fit(fixture_model)
    # Exercise dwell across chunk boundaries plus null OMR preserving active alarm state.
    frame = pd.concat([train.iloc[[0]].assign(a=3.0)] * 8, ignore_index=True)
    frame.loc[4, "a"] = np.nan
    whole = model.predict(frame)
    state = AlarmState()
    rows = []
    for offset in range(0, len(frame), 2):
        predicted = chunk(
            runner,
            fit,
            config,
            frame.iloc[offset : offset + 2],
            context,
            row_offset=offset,
            alarm_state=state,
        )
        rows.extend(predicted.rows)
        state = predicted.alarm_state
    assert tuple(rows) == whole.rows
    assert state == whole.alarm_state
    assert rows[4].omr_percent is None and rows[4].alarm is None
    assert mode_stream.StreamFit(fit.fit_artifact, fit.document).model_sha256 == model.model_sha256
    assert all(call["fit_artifact"] == fit.fit_artifact for call in runner.calls[1:])


@pytest.mark.parametrize(
    "mutation",
    [
        lambda v: v.update(input_sha256="b" * 64),
        lambda v: v.update(fit_artifact_sha256="b" * 64),
        lambda v: v.update(model_sha256="b" * 64),
        lambda v: v.update(configuration_sha256="b" * 64),
        lambda v: v.update(row_offset=1),
        lambda v: v.update(alarm_in={"active": True, "pending": 0}),
        lambda v: v["prediction"].update(model_sha256="b" * 64),
        lambda v: v["prediction"]["rows"][0].update(index=1),
        lambda v: v["prediction"]["rows"][0].update(alarm=True),
        lambda v: v["prediction"]["rows"][0]["residuals"][0].update(sensor="other"),
        lambda v: v["prediction"]["rows"][0]["residuals"][0].update(actual=999),
        lambda v: v["prediction"].update(alarm_state={"active": True, "pending": 0}),
        lambda v: v["prediction"]["rows"].append(v["prediction"]["rows"][0]),
    ],
)
def test_predictions_reject_changed_binding_offsets_sensor_values_and_alarm_state(
    fixture_model, mutation
):
    config, train, context, _model = fixture_model
    runner, fit = run_fit(fixture_model)
    runner.alter = mutation
    with pytest.raises(ValueError):
        chunk(runner, fit, config, train.iloc[:2], context)


def test_exact_encoded_output_limit_and_changed_fit_are_rejected(fixture_model):
    config, train, context, _model = fixture_model
    runner, fit = run_fit(fixture_model)
    runner.alter = lambda value: value["prediction"]["rows"][0].update(
        reason="x" * MAX_STREAM_OUTPUT_BYTES
    )
    with pytest.raises(ValueError, match="byte bound"):
        chunk(runner, fit, config, train.iloc[:2], context)
    with pytest.raises(ValueError, match="hash"):
        mode_stream.StreamFit(b"changed", fit.document)
    with pytest.raises(ValueError, match="32 MiB"):
        mode_stream.StreamFit(b"x" * (mode_stream.MAX_FIT_ARTIFACT_BYTES + 1), fit.document)


def test_actual_chunk_and_alarm_cursor_bounds_are_checked_before_runner(fixture_model):
    config, train, context, _model = fixture_model
    runner, fit = run_fit(fixture_model)
    for frame, kwargs in [
        (pd.concat([train] * 3), {}),
        (train.iloc[:2], {"row_offset": 65535}),
        (train.iloc[:2], {"alarm_state": AlarmState(pending=3)}),
        (train.iloc[:2], {"alarm_state": AlarmState(active=True, pending=1)}),
    ]:
        with pytest.raises(ValueError):
            chunk(runner, fit, config, frame, context, **kwargs)
    assert len(runner.calls) == 1


@pytest.mark.parametrize("sampling", [2, None])
def test_sampling_context_is_preserved_and_continuation_cannot_change_it(fixture_model, sampling):
    config, train, context, model = fixture_model
    context = replace(context, sampling_s=sampling)
    runner, fit = run_fit((config, train, context, model))
    assert fit.metadata["sampling_seconds"] == sampling
    before = chunk(runner, fit, config, train.iloc[:2], context)
    after = chunk(
        runner,
        fit,
        config,
        train.iloc[2:5],
        context,
        row_offset=2,
        alarm_state=before.alarm_state,
    )
    whole = model.predict(train.iloc[:5])
    assert before.rows + after.rows == whole.rows
    assert after.alarm_state == whole.alarm_state
    with pytest.raises(ValueError, match="context"):
        chunk(runner, fit, config, train.iloc[:2], replace(context, sampling_s=1))


@pytest.mark.parametrize("sampling", [True, 0, -1, 2.5, "2"])
def test_invalid_sampling_is_rejected_in_arrow_and_fit_metadata(fixture_model, sampling):
    config, train, context, model = fixture_model
    with pytest.raises(ValueError):
        mode_stream._arrow(train, replace(context, sampling_s=sampling), training=True)
    with pytest.raises(ValueError):
        mode_stream.StreamFit(
            b"opaque-model-not-a-pickle", fit_document(config, model, sampling=sampling)
        )


def test_admission_checks_and_original_deadline_cover_returned_output(fixture_model, monkeypatch):
    config, train, context, _model = fixture_model
    runner, fit = run_fit(fixture_model)
    seen = []
    original_deadline = time.monotonic() + 30
    chunk(
        runner,
        fit,
        config,
        train.iloc[:2],
        context,
        deadline=original_deadline,
        admission_check=lambda: seen.append("admitted"),
    )
    assert seen == ["admitted", "admitted"]
    assert runner.calls[-1]["deadline"] == original_deadline
    assert callable(runner.calls[-1]["admission_check"])
    with pytest.raises(SandboxRunError, match="timed out"):
        chunk(runner, fit, config, train.iloc[:2], context, deadline=time.monotonic() - 1)
    original = runner.run_phase

    def late(**kwargs):
        result = original(**kwargs)
        monkeypatch.setattr(mode_stream.time, "monotonic", lambda: original_deadline + 1)
        return result

    runner.run_phase = late
    with pytest.raises(SandboxRunError, match="timed out"):
        chunk(runner, fit, config, train.iloc[:2], context, deadline=original_deadline)


def test_nullable_stream_output_cannot_enter_ordinary_candidate_scores():
    payload = canonical_json(
        {
            "schema": "candidate-scores.v1",
            "sample_indices": [0],
            "scores": [None],
            "alarm_policy": None,
        }
    )
    with pytest.raises(ValueError):
        parse_candidate_score(payload)


def test_uncalibrated_model_fits_and_returns_explicit_unavailable_diagnostics(fixture_model):
    config, train, context, _model = fixture_model
    config = config.model_copy(update={"min_support": 512})
    model = fit_model(train, config)
    runner, fit = run_fit((config, train, context, model))
    assert fit.model_summary["alarm_threshold"] is None
    result = chunk(runner, fit, config, train.iloc[:3], context)
    assert all(row.omr_percent is None and row.alarm is None for row in result.rows)
    assert all(row.reason == "no_supported_modes" for row in result.rows)


def test_container_stream_contract_import_does_not_require_host_runner_or_image_lock():
    import subprocess
    import sys

    script = """
import importlib.abc
import sys
class BlockHostRunner(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "lab.sandbox.docker_runner":
            raise AssertionError("container contract imported host runner/image lock")
sys.meta_path.insert(0, BlockHostRunner())
from lab.sandbox.mode_stream import StreamFit, StreamPhaseContext
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, check=False, timeout=10
    )
    assert result.returncode == 0, result.stderr.decode()


@pytest.mark.parametrize("sampling", [1, 2, None])
def test_entrypoint_explicit_stream_phases_write_separate_documents(
    fixture_model, tmp_path, monkeypatch, sampling
):
    config, train, context, model = fixture_model
    context = replace(context, sampling_s=sampling)
    for directory in ("output", "candidate", "fit-artifact"):
        (tmp_path / directory).mkdir()
    (tmp_path / "candidate/candidate.py").write_bytes(candidate_source(config))
    monkeypatch.setattr(candidate_entrypoint, "Path", lambda raw: tmp_path / raw.lstrip("/"))
    monkeypatch.setattr(
        candidate_entrypoint,
        "_candidate_module",
        lambda: SimpleNamespace(build_candidate=lambda: OperatingModeCandidate(config)),
    )
    monkeypatch.setenv("SWAPP_FIT_CONTEXT", json.dumps(asdict(context)))
    request = mode_stream.StreamPhaseContext(
        input_sha256=INPUT_SHA,
        configuration=config,
        configuration_sha256=configuration_sha256(config),
        candidate_sha256=hashlib.sha256(candidate_source(config)).hexdigest(),
    )
    monkeypatch.setenv("SWAPP_STREAM_CONTEXT", request.model_dump_json())
    monkeypatch.setattr(
        candidate_entrypoint.sys,
        "stdin",
        SimpleNamespace(buffer=BytesIO(mode_stream._arrow(train, context, training=True))),
    )
    assert candidate_entrypoint._stream_phase("mode_stream_fit") == 0
    assert {p.name for p in (tmp_path / "output").iterdir()} == {"model.bin", "stream-fit.json"}
    artifact = (tmp_path / "output/model.bin").read_bytes()
    fit = mode_stream.StreamFit(artifact, (tmp_path / "output/stream-fit.json").read_bytes())
    assert fit.model_sha256 == model.model_sha256
    assert fit.metadata["sampling_seconds"] == sampling
    (tmp_path / "fit-artifact/model.bin").write_bytes(artifact)
    request = request.model_copy(
        update={
            "fit_artifact_sha256": fit.fit_artifact_sha256,
            "model_sha256": fit.model_sha256,
            "row_offset": 9,
        }
    )
    monkeypatch.setenv("SWAPP_STREAM_CONTEXT", request.model_dump_json())
    frame = train.iloc[:3].copy()
    frame.iloc[1, 0] = np.nan
    monkeypatch.setattr(
        candidate_entrypoint.sys,
        "stdin",
        SimpleNamespace(buffer=BytesIO(mode_stream._arrow(frame, context, training=False))),
    )
    assert candidate_entrypoint._stream_phase("mode_stream_predict") == 0
    document = (tmp_path / "output/stream-prediction.json").read_bytes()
    batch = mode_stream._validate_prediction(document, request=request, fit=fit, frame=frame)
    assert [row.index for row in batch.rows] == [9, 10, 11]
    assert batch.rows[1].omr_percent is None
    assert not (tmp_path / "output/scores.json").exists()


def test_p1_path_uses_explicit_phase_and_bounded_output_without_starting_docker(
    fixture_model, tmp_path, monkeypatch
):
    config, train, context, _model = fixture_model
    _fixture_runner, fit = run_fit(fixture_model)
    runner = LocalDockerRunner(
        image=DEFAULT_SANDBOX_IMAGE,
        work_root=tmp_path / "work",
        admission_lock=tmp_path / "p1.lock",
    )
    calls = []
    admitted = []

    class CapturedCreate(Exception):
        pass

    def create(command, **kwargs):
        calls.append((command, kwargs))
        raise CapturedCreate

    monkeypatch.setattr(runner, "_create_container", create)
    monkeypatch.setattr(runner, "_reconcile_owned_containers", lambda: admitted.append("P1"))
    monkeypatch.setattr(runner, "_write_admission_intent", lambda *_a: None)
    monkeypatch.setattr(
        "lab.sandbox.docker_runner.shutil.disk_usage", lambda _p: SimpleNamespace(free=25 * 1024**3)
    )
    with pytest.raises(CapturedCreate):
        chunk(runner, fit, config, train.iloc[:2], context)
    command, kwargs = calls[0]
    assert admitted == ["P1", "P1"]  # Admission plus cleanup of unsuccessful creation.
    assert command[-3] == "mode_stream_predict"
    assert command[-1] == str(MAX_STREAM_OUTPUT_BYTES)
    assert any(arg.startswith("SWAPP_STREAM_CONTEXT=") for arg in command)
    assert "--network=none" in command and "--user=10001:10001" in command
    assert any("dst=/fit-artifact,readonly" in arg for arg in command)
    assert 0 < kwargs["timeout_seconds"] <= 30
    assert not any(arg.startswith("--gpus") for arg in command)


@pytest.mark.parametrize("reason", ["stop", "deadline"])
def test_stop_or_expiry_after_create_cleans_owned_id_without_starting(
    fixture_model, tmp_path, monkeypatch, reason
):
    config, train, context, _model = fixture_model
    _fixture_runner, fit = run_fit(fixture_model)
    runner = LocalDockerRunner(
        image=DEFAULT_SANDBOX_IMAGE,
        work_root=tmp_path / "work",
        admission_lock=tmp_path / "p1.lock",
    )
    created = []
    removed = []
    original_deadline = time.monotonic() + 50

    def create(_command, **_kwargs):
        created.append("c" * 64)
        if reason == "deadline":
            monkeypatch.setattr(mode_stream.time, "monotonic", lambda: original_deadline + 1)
        return created[-1]

    def admission():
        if created and reason == "stop":
            raise RuntimeError("fixture stop after creation")

    monkeypatch.setattr(runner, "_create_container", create)
    monkeypatch.setattr(
        runner, "_remove_named_container", lambda identity: removed.append(identity)
    )
    monkeypatch.setattr(
        runner, "_run_bounded_process", lambda *_a, **_k: pytest.fail("started after stop/expiry")
    )
    monkeypatch.setattr(
        "lab.sandbox.docker_runner.shutil.disk_usage", lambda _p: SimpleNamespace(free=25 * 1024**3)
    )
    with pytest.raises((RuntimeError, SandboxRunError), match="stop after creation|timed out"):
        chunk(
            runner,
            fit,
            config,
            train.iloc[:2],
            context,
            deadline=original_deadline,
            admission_check=admission,
        )
    assert created == removed == ["c" * 64]
    assert not runner.admission_marker.exists()
    assert list(runner.work_root.iterdir()) == []
    # The existing process-shared P1 lock is released by the same cleanup path.
    with runner._admit_p1_capacity():
        pass
