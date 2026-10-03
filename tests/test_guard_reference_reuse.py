"""Independent CPU phase recorder; no Docker, dataset or performance acceptance."""

import hashlib
import json
from dataclasses import asdict, replace
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pyarrow.ipc as ipc
import pytest

from harness.contracts import FitContext
from lab.sandbox import evaluation as guards
from lab.sandbox.docker_runner import OutputArtifact, SandboxResult, SandboxRunError


def artifact(name, content):
    return OutputArtifact(name, hashlib.sha256(content).hexdigest(), len(content), content)


class PhaseRecorder:
    """Model isolated score restoration with opaque, distinct fit bytes."""

    def __init__(self, mode="causal", clock=None):
        self.mode = mode
        self.clock = clock
        self.calls = []
        self.closed = []
        self.models = {}
        self.score_count = 0
        self.after = None
        self.failure = None
        self.policy = {"threshold": 2.0, "release": 1.0, "dwell": 1}

    def run_phase(self, **arguments):
        phase = arguments["phase"]
        name = f"fixture-{phase}-{len(self.calls)}"
        data = ipc.open_stream(arguments["arrow_input"]).read_all().to_pandas()
        context = arguments["contract_context"]
        self.calls.append({**arguments, "context_snapshot": asdict(context), "name": name})
        try:
            if self.failure is not None:
                raise self.failure
            if phase == "fit":
                model = f"opaque-fit-{len(self.models)}".encode()
                offset = float(len(self.models)) if self.mode == "fit_rng" else 0.0
                if self.mode == "seeded":
                    offset = float(np.random.default_rng(context.seed).normal())
                self.models[model] = offset
                result = (
                    artifact("model.bin", model),
                    artifact("policy.json", json.dumps(self.policy).encode()),
                )
            else:
                self.score_count += 1
                model = arguments["fit_artifact"]
                scores = data["sensor"].to_numpy(dtype=float) - self.models[model]
                if self.mode == "score_rng":
                    scores += self.score_count
                elif self.mode == "initial_only" and self.score_count == 1:
                    scores += 10.0
                elif self.mode == "noncausal":
                    scores -= scores.mean()
                elif self.mode == "tiny_noncausal":
                    scores = (scores - scores.mean()) * 1e-12
                elif self.mode == "stateful":
                    # Every fresh score reconstructs state zero from the fit.
                    scores = np.cumsum(scores)
                elif self.mode == "bad_f0" and model == b"opaque-fit-0" and self.score_count > 2:
                    scores -= scores.mean()
                policy = dict(self.policy)
                indices = list(range(len(scores)))
                if self.score_count >= 3:
                    if self.mode == "bad_policy":
                        policy["threshold"] = 3.0
                    elif self.mode == "bad_index":
                        indices = list(range(1, len(scores) + 1))
                    elif self.mode == "nan":
                        scores[0] = float("nan")
                payload = json.dumps(
                    {
                        "schema": "candidate-scores.v1",
                        "sample_indices": indices,
                        "scores": scores.tolist(),
                        "alarm_policy": policy,
                    }
                ).encode()
                result = (artifact("scores.json", payload),)
            if self.after is not None:
                self.after(arguments)
            if self.clock is not None:
                self.clock.now += self.clock.step
            return SandboxResult(phase, 0, b"", b"", result, 2.0, name)
        finally:
            self.closed.append(name)


@pytest.fixture
def inputs():
    train = pd.DataFrame({"sensor": np.arange(20, dtype=float)})
    evaluation = pd.DataFrame({"sensor": np.arange(1, 21, dtype=float)})
    train.attrs["private_label_canary"] = "PRIVATE_LABEL"
    evaluation.index = pd.date_range("2020-01-01", periods=20)
    context = FitContext(7, ("sensor",), ("sensor",), 60, 600.0)
    return dict(
        candidate_source=b"# synthetic candidate\n",
        train=train,
        evaluation=evaluation,
        context=context,
    )


def guarded(runner, inputs, **options):
    return guards.run_guarded_seed_evaluation(
        runner,
        **inputs,
        task_ids=frozenset(),
        evaluation_instants=frozenset(),
        **options,
    )


@pytest.mark.parametrize("mode", ["causal", "seeded", "stateful"])
def test_seven_independent_phases_guard_original_result_and_exact_frozen_fit(inputs, mode):
    runner = PhaseRecorder(mode)
    before = inputs["evaluation"].copy(deep=True)
    admissions = []
    result = guarded(runner, inputs, admission_check=lambda: admissions.append(len(runner.calls)))
    assert [call["phase"] for call in runner.calls] == [
        "fit",
        "score",
        "fit",
        "score",
        "score",
        "score",
        "score",
    ]
    assert admissions == list(range(7))
    assert len({call["name"] for call in runner.calls}) == 7
    assert runner.closed == [call["name"] for call in runner.calls]
    assert result.evaluation.fit_container_name == runner.calls[0]["name"]
    assert result.evaluation.score_container_name == runner.calls[1]["name"]
    assert runner.calls[3]["fit_artifact"] != runner.calls[1]["fit_artifact"]
    assert all(call["fit_artifact"] == result.evaluation.fit_artifact for call in runner.calls[4:])
    assert runner.calls[1]["arrow_input"] == runner.calls[3]["arrow_input"]
    assert runner.calls[0]["arrow_input"] == runner.calls[2]["arrow_input"]
    assert all(call["context_snapshot"] == asdict(inputs["context"]) for call in runner.calls)
    for call, fraction in zip(runner.calls[4:], (0.5, 0.7, 0.85), strict=True):
        changed = ipc.open_stream(call["arrow_input"]).read_all().to_pandas()
        cut = int(len(before) * fraction)
        assert np.array_equal(changed["sensor"][:cut], before["sensor"][:cut])
        assert not np.array_equal(changed["sensor"][cut:], before["sensor"][cut:])
    for call in runner.calls:
        assert b"PRIVATE_LABEL" not in call["arrow_input"]
        assert ipc.open_stream(call["arrow_input"]).schema.metadata is None
    pd.testing.assert_frame_equal(inputs["evaluation"], before)


def test_original_only_mismatch_previously_outside_determinism_is_now_rejected(inputs):
    old_pair = PhaseRecorder("initial_only")
    guards.run_candidate_fit_score(old_pair, **inputs)
    assert guards.check_determinism(old_pair, **inputs).passed
    runner = PhaseRecorder("initial_only")
    with pytest.raises(guards.CandidateGuardReject, match="^determinism$"):
        guarded(runner, inputs)
    assert len(runner.calls) == 4


@pytest.mark.parametrize("mode", ["fit_rng", "score_rng"])
def test_independent_fit_and_score_randomness_is_still_rejected(inputs, mode):
    runner = PhaseRecorder(mode)
    with pytest.raises(guards.CandidateGuardReject, match="^determinism$"):
        guarded(runner, inputs)
    assert len(runner.calls) == 4


@pytest.mark.parametrize("mode", ["noncausal", "tiny_noncausal", "bad_f0"])
def test_noncausal_actual_f0_fails_all_unchanged_prefix_comparisons(inputs, mode):
    runner = PhaseRecorder(mode)
    with pytest.raises(guards.CandidateGuardReject, match="^causality$"):
        guarded(runner, inputs)
    assert len(runner.calls) == 7
    assert all(call["fit_artifact"] == b"opaque-fit-0" for call in runner.calls[4:])


@pytest.mark.parametrize(
    "mode,code",
    [
        ("bad_policy", "score_policy_differs_from_fitted_policy"),
        ("bad_index", "score_length_or_index_mismatch"),
        ("nan", "invalid_score_artifact"),
    ],
)
def test_perturbed_score_validation_cannot_be_skipped(inputs, mode, code):
    runner = PhaseRecorder(mode)
    with pytest.raises(guards.CandidateGuardReject, match=code):
        guarded(runner, inputs)
    assert len(runner.calls) == 5


def test_mutation_of_callers_frames_context_and_source_cannot_change_snapshot(inputs):
    runner = PhaseRecorder()
    original_context = asdict(inputs["context"])
    reference = inputs["evaluation"]["sensor"].to_numpy(copy=True)
    mutable_source = bytearray(inputs["candidate_source"])
    inputs["candidate_source"] = mutable_source

    def alter_external_inputs():
        inputs["train"].iloc[:] = 999.0
        inputs["evaluation"].iloc[:] = 888.0
        object.__setattr__(inputs["context"], "seed", 99)
        mutable_source[:] = b"changed external source"

    result = guarded(runner, inputs, admission_check=alter_external_inputs)
    assert result.evaluation.scores == tuple(reference)
    assert all(call["candidate_source"] == b"# synthetic candidate\n" for call in runner.calls)
    assert all(call["context_snapshot"] == original_context for call in runner.calls)


def test_internal_context_drift_is_denied_before_reuse(inputs):
    runner = PhaseRecorder()
    runner.after = lambda arguments: object.__setattr__(arguments["contract_context"], "seed", 123)
    with pytest.raises(guards.CandidateGuardReject, match="guard_input_provenance_changed"):
        guarded(runner, inputs)
    assert len(runner.calls) == 1
    assert len(runner.closed) == 1


@pytest.mark.parametrize("kind", ["fit", "score"])
def test_reference_provenance_is_checked_before_causal_reuse(inputs, monkeypatch, kind):
    original = guards._prepared_fit_score

    def changed(*args, **kwargs):
        value = original(*args, **kwargs)
        if kind == "fit":
            return replace(value, fit_artifact_sha256="0" * 64)
        return replace(value, scores=tuple(score + 1 for score in value.scores))

    monkeypatch.setattr(guards, "_prepared_fit_score", changed)
    runner = PhaseRecorder()
    with pytest.raises(guards.CandidateGuardReject, match=f"guard_{kind}_provenance_changed"):
        guarded(runner, inputs)
    assert len(runner.calls) == 4


def test_standalone_guards_keep_independent_execution_behavior(inputs):
    runner = PhaseRecorder()
    assert guards.check_determinism(runner, **inputs).passed
    assert [call["phase"] for call in runner.calls] == ["fit", "score", "fit", "score"]
    runner = PhaseRecorder()
    assert guards.check_causality(runner, **inputs).passed
    assert [call["phase"] for call in runner.calls] == ["fit", "score", "score", "score", "score"]


def test_original_deadline_bounds_phases_and_total_wall_time(inputs, monkeypatch):
    clock = SimpleNamespace(now=100.0, step=2.0)
    monkeypatch.setattr(guards, "time", SimpleNamespace(monotonic=lambda: clock.now))
    runner = PhaseRecorder(clock=clock)
    result = guarded(runner, inputs, remaining_seconds=20)
    assert result.guarded_wall_seconds == 14.0
    assert result.evaluation.fit_seconds + result.evaluation.score_seconds == 4.0
    assert [call["timeout_seconds"] for call in runner.calls] == [20, 18, 16, 14, 12, 10, 8]
    clock.now = 100.0
    clock.step = 5.0
    runner = PhaseRecorder(clock=clock)
    with pytest.raises(guards.CandidateGuardReject, match="timeout"):
        guarded(runner, inputs, remaining_seconds=12)
    assert len(runner.calls) == len(runner.closed) == 3


def test_stop_before_causal_phase_prevents_new_container(inputs):
    runner = PhaseRecorder()

    def stop():
        if len(runner.calls) >= 4:
            raise RuntimeError("run stopped")

    with pytest.raises(RuntimeError, match="run stopped"):
        guarded(runner, inputs, admission_check=stop)
    assert len(runner.calls) == len(runner.closed) == 4


@pytest.mark.parametrize(
    "reason,exit_code,exception,match",
    [
        ("cleanup unavailable", None, SandboxRunError, "cleanup unavailable"),
        ("candidate timed out", None, guards.CandidateGuardReject, "timeout"),
        (
            "candidate returned invalid score",
            65,
            guards.CandidateGuardReject,
            "score_length_or_index_mismatch",
        ),
    ],
)
def test_runner_failure_and_drain_are_never_masked(inputs, reason, exit_code, exception, match):
    runner = PhaseRecorder()
    runner.failure = SandboxRunError(reason, exit_code=exit_code)
    with pytest.raises(exception, match=match):
        guarded(runner, inputs)
    assert len(runner.calls) == len(runner.closed) == 1


def test_hardcoding_rejection_still_precedes_any_phase(inputs):
    inputs["candidate_source"] = b"x = [" + b",".join([b"1"] * 64) + b"]"
    runner = PhaseRecorder()
    with pytest.raises(guards.CandidateGuardReject, match="hardcoding_numeric_sequence"):
        guarded(runner, inputs)
    assert runner.calls == []
