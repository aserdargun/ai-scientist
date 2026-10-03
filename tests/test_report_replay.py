"""Focused output and replay-contract checks for M0 report artifacts."""

from __future__ import annotations

import hashlib
import json
from html.parser import HTMLParser
from types import SimpleNamespace
from uuid import UUID

import pytest

from lab.director.contracts import (
    ExperimentDecision,
    ExperimentDocument,
    ReplayManifestDocument,
    TrajectoryDocument,
    replay_configuration_sha256,
)
from lab.director.suite_guards import check_complete_suite_position_bias
from lab.replay import _initial_best_suite
from lab.reporting import _LedgerExperiment, _parsed_canonical_object, render_run_report


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class _TableColumns(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_header = False
        self.current_cells = 0
        self.header_cells = 0
        self.body_cells: list[int] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag == "thead":
            self.in_header = True
        elif tag == "tr":
            self.current_cells = 0
        elif tag in {"td", "th"}:
            self.current_cells += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "tr":
            if self.in_header:
                self.header_cells = self.current_cells
            else:
                self.body_cells.append(self.current_cells)
        elif tag == "thead":
            self.in_header = False


def _pair(
    run_id: UUID, *, baseline: bool, sequence: int, family: str = "EVT"
) -> _LedgerExperiment:
    experiment_id = f"exp_{sequence:032x}"
    source = f"pipeline-{sequence}".encode()
    candidate_sha = hashlib.sha256(source).hexdigest()
    decision = (
        None
        if baseline
        else ExperimentDecision(
            verdict="KEEP",
            delta=0.2,
            ci_low=0.1,
            noise_sd=0.0,
            reason="measured improvement",
        )
    )
    experiment = ExperimentDocument(
        schema="experiment.v1",
        experiment_id=experiment_id,
        run_id=run_id,
        ordinal=sequence + 1,
        kind="baseline" if baseline else "proposal",
        experiment_number=None if baseline else 1,
        baseline_name="robust_z" if baseline else None,
        calibration_sha256=None if baseline else _digest("calibration"),
        agent_version="test.v1",
        parent_experiment_id=None if baseline else f"exp_{0:032x}",
        candidate_sha256=candidate_sha,
        candidate_blob_sha256=candidate_sha,
        move_type="hparam",
        system="S1",
        hypothesis="<script>private prompt text</script>",
        predicted_delta=None if baseline else 0.2,
        inputs_sha256=_digest("inputs"),
        parent_tree="a" * 40,
        child_tree=None if baseline else "b" * 40,
        harness_sha256=_digest("harness"),
        image_sha256=_digest("image"),
        suite_id="suite.test.v1",
        suite_version=1,
        per_task=(
            {
                "task_id": "sealed-canary-task-77",
                "task_family": family,
                "score_norm": 0.7,
                "vus_pr": 0.7 if family == "EVT" else None,
                "vus_roc": 0.8 if family == "EVT" else None,
                "task_score": 0.7 if family != "EVT" else None,
                "fa_per_day": 0.3 if family == "PDM" else None,
                "duty_fraction": 0.02 if family == "NRM" else None,
                "event_f1": None,
                "fit_seconds": 1.0,
                "score_seconds": 1.0,
            },
        )
        if not baseline
        else (),
        suite_score=None if baseline else 0.7,
        guards={"hardcoding": "pass", "determinism": "pass", "causality": "pass"},
        decision=decision,
        status="scored",
        fit_seconds=1.0,
        score_seconds=1.0,
        llm_input_tokens=0,
        llm_output_tokens=0,
        wall_seconds=2.0,
    )
    trajectory = TrajectoryDocument(
        schema="trajectory.v1",
        trajectory_id=f"trj_{sequence:032x}",
        run_id=run_id,
        experiment_id=experiment_id,
        kind="baseline" if baseline else "proposal",
        experiment_number=None if baseline else 1,
        baseline_name="robust_z" if baseline else None,
        calibration_sha256=None if baseline else _digest("calibration"),
        agent_version="test.v1",
        model_id="fixture.model",
        usage_profile="noncommercial_research",
        source_provenance=(),
        quantization="none",
        adapter="fixture",
        system="S1",
        thinking=False,
        temperature=0.0,
        top_p=1.0,
        context_template="fixture.v1",
        inputs_sha256=_digest("inputs"),
        messages_blob_sha256=_digest("messages"),
        tool_calls=0,
        outcome=decision,
        quality_tier="bronze",
        secrets_scrubbed=False,
        people_scrubbed=False,
        raw_values_scrubbed=False,
        exclusions=("labels excluded",),
        provider_receipt={"holdout_canary": "sealed-canary-task-77"},
    )
    return _LedgerExperiment(sequence, experiment, trajectory)


def test_html_report_counts_verdicts_and_omits_private_fields() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000123")
    report = render_run_report(
        run_id,
        (_pair(run_id, baseline=True, sequence=0), _pair(run_id, baseline=False, sequence=1)),
        baseline_score=0.5,
    )
    html = report.html.decode("utf-8")
    assert report.sha256 == hashlib.sha256(report.html).hexdigest()
    assert report.summary["experiment_count"] == 1
    assert report.summary["keep_count"] == 1
    assert "Dev suite score staircase" in html
    assert "H 944.00 V" in html
    assert "VUS-PR" in html
    assert "sealed-canary-task-77" not in html
    assert "private prompt text" not in html
    assert "holdout_canary" not in html
    assert "labels excluded" not in html
    assert "<script>" not in html
    columns = _TableColumns()
    columns.feed(html)
    assert columns.header_cells == 7
    assert columns.body_cells == [columns.header_cells, columns.header_cells]


def test_family_report_does_not_invent_evt_metrics_for_pdm() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000125")
    report = render_run_report(
        run_id,
        (_pair(run_id, baseline=True, sequence=0),
         _pair(run_id, baseline=False, sequence=1, family="PDM")),
        baseline_score=0.5,
    )
    html = report.html.decode()
    assert "PDM (1): Task score 0.7; FA/day 0.3" in html
    assert "VUS-PR" not in html


def test_replay_manifest_fingerprint_is_strict_and_complete() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000123")
    digest = _digest("value")
    fields: dict[str, object] = {
        "schema": "referee-replay.v1",
        "decision_stage": "primary",
        "experiment_id": f"exp_{1:032x}",
        "run_id": run_id,
        "parent_experiment_id": f"exp_{0:032x}",
        "parent_tree": "a" * 40,
        "child_tree": "b" * 40,
        "candidate_sha256": digest,
        "parent_source_sha256": digest,
        "child_source_sha256": digest,
        "calibration_sha256": digest,
        "harness_sha256": digest,
        "image_sha256": digest,
        "suite_id": "suite.test.v1",
        "suite_version": 1,
        "referee_source_sha256": digest,
        "python_version": "3.12.13",
        "numpy_version": "2.2.6",
        "task_ids": ("task-1",),
        "expected_decision": {
            "verdict": "KEEP",
            "delta": 0.2,
            "ci_low": 0.1,
            "noise_sd": 0.0,
            "reason": "measured improvement",
        },
        "task_families": ("EVT",),
        "profile_sha256": (digest,),
        "normalization_base": (0.1,),
        "normalization_reference": (0.9,),
        "parent": (0.2,),
        "child": (0.4,),
        "weights": (1.0,),
        "parent_raw_scores": ((0.3,),),
        "child_raw_scores": ((0.5,),),
        "parent_noise_raw_scores": ((0.3,), (0.4,), (0.5,)),
        "parent_noise_seed_ids": (0, 1, 2),
        "parent_noise_evaluation_kinds": ("baseline", "baseline", "baseline"),
        "parent_noise_output_sha256": ((digest,), (digest,), (digest,)),
        "parent_noise_score_sha256": (digest, digest, digest),
        "parent_seed_ids": (0,),
        "parent_seed_evaluation_kinds": ("baseline",),
        "decision_seeds": (0,),
        "parent_seed_output_sha256": ((digest,),),
        "parent_seed_score_sha256": (digest,),
        "child_seed_output_sha256": ((digest,),),
        "child_seed_score_sha256": (digest,),
        "eps": 0.01,
        "noise_sd": 0.0,
        "simpler": False,
        "guards_ok": True,
        "guard_results": {"hardcoding": "pass"},
        "position_bias_threshold": 0.8,
        "max_task_drop": 0.5,
        "best_suite": 0.2,
        "n_boot": 4000,
        "bootstrap_seed": 0,
    }
    fields["configuration_sha256"] = replay_configuration_sha256(fields)
    manifest = ReplayManifestDocument.model_validate(fields, strict=True)
    assert manifest.configuration_sha256 == replay_configuration_sha256(
        manifest.model_dump(mode="json", by_alias=True)
    )
    fields["guards_ok"] = "true"
    with pytest.raises(ValueError):
        ReplayManifestDocument.model_validate(fields, strict=True)


def test_initial_best_suite_is_the_measured_three_seed_mean() -> None:
    assert _initial_best_suite((0.1, 0.5, 0.9)).hex() == (0.5).hex()


def test_original_canonical_json_remains_readable_when_new_defaults_are_absent() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000124")
    pair = _pair(run_id, baseline=False, sequence=1)
    payload = pair.trajectory.model_dump(mode="json", by_alias=True)
    payload.pop("replay_manifests")
    payload.pop("terminal_replay")
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    assert "replay_manifests" not in _parsed_canonical_object(encoded)
    parsed = TrajectoryDocument.model_validate_json(encoded, strict=True)
    assert parsed.replay_manifests == ()
    with pytest.raises(ValueError, match="duplicate keys"):
        _parsed_canonical_object(b'{"schema":"x","schema":"y"}')


def test_nrm_guard_requires_complete_artifact_bound_suite(monkeypatch: pytest.MonkeyPatch) -> None:
    from lab.director import runner

    vectors = {
        "normal-a": tuple(float(i) for i in range(32)),
        "normal-b": tuple(float(i % 4) for i in range(32)),
    }
    blobs: dict[str, bytes] = {}
    rows = {}
    for task_id, scores in vectors.items():
        payload = json.dumps(
            {
                "schema": "candidate-scores.v1",
                "sample_indices": list(range(len(scores))),
                "scores": list(scores),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        digest = hashlib.sha256(payload).hexdigest()
        blobs[digest] = payload
        rows[task_id] = {
            "candidate_output_sha256": digest,
            "position_bias": check_complete_suite_position_bias(
                {task_id: scores}, {task_id: "NRM"}
            ).task_bias[task_id],
        }
    monkeypatch.setattr(runner, "read_artifact_bytes", lambda digest: blobs[digest])
    tasks = tuple(SimpleNamespace(task_id=task_id, family="NRM") for task_id in vectors)
    check = runner._suite_position_bias_checks(tasks, rows, seed_count=1)[0]
    assert check.state == "fail"
    assert check.expected_tasks == 2 and check.violating_tasks == 1
    incomplete_rows = {"normal-a": rows["normal-a"]}
    incomplete = runner._suite_position_bias_checks(
        tasks, incomplete_rows, seed_count=1
    )[0]
    assert incomplete.state == "pending"
    assert not incomplete.passed


@pytest.mark.parametrize("family", ["EVT", "NRM"])
def test_replay_score_evidence_uses_only_selected_artifact_root(
    tmp_path, monkeypatch: pytest.MonkeyPatch, family: str
) -> None:
    from contextlib import contextmanager

    from harness.baselines import normalize_task_score
    from lab.director.baselines import (
        BASELINE_NAMES,
        build_task_calibration,
        calibration_sha256,
        freeze_calibration_document,
    )
    from lab.director.journal import canonical_bytes
    from lab.replay import _verify_score_evidence
    from lab.scorer.jobs import DEFAULT_ARTIFACT_ROOT

    # The ambient default is an isolated empty cwd, not the user's runtime.
    monkeypatch.chdir(tmp_path)
    artifact_root = tmp_path / "selected-run-artifacts"
    payload = canonical_bytes(
        {
            "schema": "candidate-scores.v1",
            "sample_indices": list(range(32)),
            "scores": [float(index % 4) for index in range(32)],
        }
    )
    digest = hashlib.sha256(payload).hexdigest()

    def fixture_blob(root):
        shard = root / digest[:2]
        shard.mkdir(parents=True, mode=0o700)
        path = shard / f"{digest}.json"
        path.write_bytes(payload)
        path.chmod(0o600)
        return path

    selected_blob = fixture_blob(artifact_root)
    run_id = UUID("00000000-0000-0000-0000-000000000126")
    parent_id, child_id = "exp_" + "0" * 32, "exp_" + "1" * 32
    task = build_task_calibration(
        task_id="task-1",
        dataset_id="fixture",
        split_id="dev",
        session_id="session-1",
        profile_sha256=_digest("profile"),
        family=family,
        baseline_seed_scores={name: {seed: 0.2 for seed in range(3)} for name in BASELINE_NAMES},
        scorer_artifact_sha256={
            name: {seed: digest for seed in range(3)} for name in BASELINE_NAMES
        },
        baseline_candidate_sha256={name: _digest(name) for name in BASELINE_NAMES},
        task_weight=1.0,
    )
    calibration = freeze_calibration_document(
        run_id=run_id,
        suite_id="fixture-suite",
        suite_version=1,
        harness_sha256=_digest("harness"),
        image_sha256=_digest("image"),
        tasks=(task,),
        expected_task_identities=(("fixture", "dev", "session-1", "task-1"),),
        champion_experiment_id=parent_id,
        champion_baseline_name="robust_z",
        champion_suite_scores={seed: 0.0 for seed in range(3)},
    )
    bias = check_complete_suite_position_bias(
        {task.task_id: tuple(float(index % 4) for index in range(32))} if family == "NRM" else {},
        {task.task_id: family},
    )

    def row(seed, kind, score):
        return {
            "candidate_sha256": _digest("candidate"),
            "seed": seed,
            "task_id": task.task_id,
            "evaluation_kind": kind,
            "dataset_id": task.dataset_id,
            "split_id": task.split_id,
            "session_id": task.session_id,
            "profile_sha256": task.profile_sha256,
            "task_family": family,
            "task_score": score,
            "candidate_output_sha256": digest,
            "position_bias": bias.task_bias.get(task.task_id),
        }

    parent_rows = [row(seed, "baseline", 0.2) for seed in range(3)]
    child_rows = [row(0, "primary", 0.3)]

    class Engine:
        @contextmanager
        def connect(self):
            yield self

        def execute(self, query, parameters):
            del query
            rows = parent_rows if parameters["experiment_id"] == parent_id else child_rows
            return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: rows))

    def receipt(experiment_id, seed, kind, score):
        return hashlib.sha256(
            canonical_bytes(
                {
                    "experiment_id": experiment_id,
                    "seed": seed,
                    "evaluation_kind": kind,
                    "task_ids": (task.task_id,),
                    "raw_scores": (score,),
                    "output_sha256": (digest,),
                }
            )
        ).hexdigest()

    manifest = SimpleNamespace(
        run_id=run_id,
        calibration_sha256=calibration_sha256(calibration),
        task_ids=(task.task_id,),
        task_families=(family,),
        profile_sha256=(task.profile_sha256,),
        normalization_base=(task.base_score,),
        normalization_reference=(task.reference_score,),
        weights=(1.0,),
        parent_experiment_id=parent_id,
        experiment_id=child_id,
        parent_seed_ids=(0,),
        parent_seed_evaluation_kinds=("baseline",),
        parent_raw_scores=((0.2,),),
        parent_seed_output_sha256=((digest,),),
        parent_seed_score_sha256=(receipt(parent_id, 0, "baseline", 0.2),),
        decision_seeds=(0,),
        child_raw_scores=((0.3,),),
        child_seed_output_sha256=((digest,),),
        child_seed_score_sha256=(receipt(child_id, 0, "primary", 0.3),),
        parent_noise_seed_ids=(0, 1, 2),
        parent_noise_evaluation_kinds=("baseline", "baseline", "baseline"),
        parent_noise_raw_scores=((0.2,), (0.2,), (0.2,)),
        parent_noise_output_sha256=((digest,), (digest,), (digest,)),
        parent_noise_score_sha256=tuple(
            receipt(parent_id, seed, "baseline", 0.2) for seed in range(3)
        ),
        position_bias_threshold=0.8,
        guard_results={"position_bias": "pass"},
        parent=(
            float(normalize_task_score(0.2, task.base_score, task.reference_score, family=family)),
        ),
        child=(
            float(normalize_task_score(0.3, task.base_score, task.reference_score, family=family)),
        ),
        noise_sd=0.0,
    )

    def verify():
        _verify_score_evidence(
            Engine(),
            manifest,
            parent_experiment=SimpleNamespace(
                experiment_id=parent_id, candidate_sha256=_digest("candidate")
            ),
            child_experiment=SimpleNamespace(
                experiment_id=child_id, candidate_sha256=_digest("candidate")
            ),
            calibration=calibration,
            artifact_root=artifact_root,
        )

    verify()
    # A matching blob in the ambient default must not rescue missing run evidence.
    fixture_blob(DEFAULT_ARTIFACT_ROOT)
    selected_blob.unlink()
    with pytest.raises(FileNotFoundError):
        verify()
