"""Exact, fail-closed Referee replay from immutable Director artifacts."""

from __future__ import annotations

import hashlib
import inspect
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import numpy as np
from sqlalchemy import Engine, text

from harness.baselines import champion_noise_sd, normalize_task_score
from harness.referee import decide
from lab.director.artifacts import read_director_artifact, read_registered_calibration
from lab.director.baselines import calibration_sha256
from lab.director.contracts import ReplayManifestDocument, replay_configuration_sha256
from lab.director.journal import canonical_bytes
from lab.director.ledger import canonical_json_bytes
from lab.director.runner import POSITION_BIAS_THRESHOLD, _source_is_measurably_simpler
from lab.director.suite_guards import check_complete_suite_position_bias
from lab.reporting import read_run_pairs
from lab.scorer.jobs import read_artifact_bytes
from lab.scorer.service import parse_candidate_score


@dataclass(frozen=True, slots=True)
class ReplayedDecision:
    """One immutable scored proposal and the decision recomputed from its manifest."""

    experiment_id: str
    decision_stage: str
    verdict: str
    delta: float | None
    ci_low: float | None
    noise_sd: float
    manifest_sha256: str
    configuration_sha256: str


def _referee_source_sha256() -> str:
    source = inspect.getsourcefile(decide)
    if source is None:
        raise ValueError("cannot identify current Referee source")
    return hashlib.sha256(Path(source).read_bytes()).hexdigest()


def _same_float(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is right
    return float(left).hex() == float(right).hex()


def _initial_best_suite(seed_scores: tuple[float, float, float]) -> float:
    """Use the same fixed arithmetic mean as DirectorLoop initial state."""
    return sum(seed_scores) / 3.0


def _manifest_for_pair(
    experiment: Any,
    manifest: ReplayManifestDocument,
    *,
    engine: Engine,
    run_id: UUID,
    calibration: Any,
    artifact_root: Path,
    by_id: dict[str, Any],
    expected_best_suite: float,
) -> tuple[ReplayManifestDocument, bytes]:
    if manifest.run_id != run_id or manifest.experiment_id != experiment.experiment_id:
        raise ValueError("replay manifest belongs to a different run or experiment")
    for actual, expected, name in (
        (experiment.parent_experiment_id, manifest.parent_experiment_id, "parent experiment"),
        (experiment.parent_tree, manifest.parent_tree, "parent tree"),
        (experiment.child_tree, manifest.child_tree, "child tree"),
        (experiment.candidate_sha256, manifest.candidate_sha256, "candidate source"),
        (experiment.calibration_sha256, manifest.calibration_sha256, "calibration"),
        (experiment.harness_sha256, manifest.harness_sha256, "harness"),
        (experiment.image_sha256, manifest.image_sha256, "sandbox image"),
        (experiment.suite_id, manifest.suite_id, "suite"),
        (experiment.suite_version, manifest.suite_version, "suite version"),
    ):
        if actual != expected:
            raise ValueError(f"replay manifest {name} identity differs from experiment")
    if manifest.referee_source_sha256 != _referee_source_sha256():
        raise ValueError("Referee source differs from the recorded replay implementation")
    if (
        manifest.python_version != platform.python_version()
        or manifest.numpy_version != np.__version__
    ):
        raise ValueError("Python or NumPy version differs from the exact replay environment")
    expected_configuration = replay_configuration_sha256(
        manifest.model_dump(mode="json", by_alias=True)
    )
    if expected_configuration != manifest.configuration_sha256:
        raise ValueError("replay configuration hash mismatch")
    if manifest.best_suite.hex() != expected_best_suite.hex():
        raise ValueError("replay best-suite input differs from prior accepted ledger history")
    if manifest.position_bias_threshold.hex() != POSITION_BIAS_THRESHOLD.hex():
        raise ValueError("replay position-bias threshold differs from the M0 guard policy")
    if experiment.guards != manifest.guard_results or manifest.guards_ok != all(
        value == "pass" for value in manifest.guard_results.values()
    ):
        raise ValueError("replay guard inputs differ from the immutable experiment record")

    parent = by_id.get(manifest.parent_experiment_id)
    if parent is None:
        raise ValueError("replay parent experiment is absent from this run")
    if parent.document.child_tree != manifest.parent_tree:
        raise ValueError("replay parent tree differs from the parent ledger artifact")
    parent_source = read_director_artifact(
        parent.document.candidate_blob_sha256, artifact_root=artifact_root
    )
    child_source = read_director_artifact(
        experiment.candidate_blob_sha256, artifact_root=artifact_root
    )
    if hashlib.sha256(parent_source).hexdigest() != manifest.parent_source_sha256:
        raise ValueError("replay parent source blob failed its content hash")
    if hashlib.sha256(child_source).hexdigest() != manifest.child_source_sha256:
        raise ValueError("replay child source blob failed its content hash")
    if hashlib.sha256(child_source).hexdigest() != manifest.candidate_sha256:
        raise ValueError("candidate source blob differs from the registered candidate hash")
    if hashlib.sha256(parent_source).hexdigest() != parent.document.candidate_sha256:
        raise ValueError("parent source bytes differ from the parent ledger candidate hash")
    if manifest.simpler != _source_is_measurably_simpler(child_source, parent_source):
        raise ValueError("replay simpler input differs from the registered source trees")
    _verify_score_evidence(
        engine,
        manifest,
        parent_experiment=parent.document,
        child_experiment=experiment,
        calibration=calibration,
        artifact_root=artifact_root,
    )
    return manifest, child_source


def _verify_score_evidence(
    engine: Engine,
    manifest: ReplayManifestDocument,
    *,
    parent_experiment: Any,
    child_experiment: Any,
    calibration: Any,
    artifact_root: Path,
) -> None:
    """Cross-check every normalized vector against immutable Scorer rows/artifacts."""
    if calibration_sha256(calibration) != manifest.calibration_sha256:
        raise ValueError("replay calibration differs from its registered receipt")
    task_refs = tuple(
        sorted(
            calibration.tasks,
            key=lambda item: (item.dataset_id, item.split_id, item.session_id, item.task_id),
        )
    )
    if tuple(item.task_id for item in task_refs) != manifest.task_ids:
        raise ValueError("replay task order differs from frozen calibration")
    if (
        tuple(item.family for item in task_refs) != manifest.task_families
        or tuple(item.profile_sha256 for item in task_refs) != manifest.profile_sha256
        or tuple(float(item.base_score).hex() for item in task_refs)
        != tuple(value.hex() for value in manifest.normalization_base)
        or tuple(float(item.reference_score).hex() for item in task_refs)
        != tuple(value.hex() for value in manifest.normalization_reference)
        or tuple(float(item.task_weight).hex() for item in task_refs)
        != tuple(value.hex() for value in manifest.weights)
    ):
        raise ValueError("replay normalization parameters differ from frozen calibration")

    def read_rows(experiment_id: str) -> dict[tuple[str, int, str], Any]:
        with engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        """SELECT * FROM lab.dev_task_results
                         WHERE run_id = :run_id AND experiment_id = :experiment_id"""
                    ),
                    {"run_id": manifest.run_id, "experiment_id": experiment_id},
                )
                .mappings()
                .all()
            )
        indexed: dict[tuple[str, int, str], Any] = {}
        for row in rows:
            if row.get("candidate_sha256") != (
                parent_experiment.candidate_sha256
                if experiment_id == parent_experiment.experiment_id
                else child_experiment.candidate_sha256
            ):
                raise ValueError("Scorer candidate identity differs from immutable ledger")
            seed = row.get("seed")
            task_id = row.get("task_id")
            evaluation_kind = row.get("evaluation_kind")
            if (
                isinstance(seed, bool)
                or not isinstance(seed, int)
                or not isinstance(task_id, str)
                or evaluation_kind not in {"baseline", "primary", "confirmation"}
            ):
                raise ValueError("Scorer row has malformed task/seed identity")
            key = (evaluation_kind, seed, task_id)
            if key in indexed:
                raise ValueError("Scorer view contains duplicate task/seed rows")
            indexed[key] = row
        return indexed

    parent_rows = read_rows(manifest.parent_experiment_id)
    child_rows = read_rows(manifest.experiment_id)

    def verify_seed_group(
        indexed: dict[tuple[str, int, str], Any],
        experiment_id: str,
        seeds: tuple[int, ...],
        evaluation_kinds: tuple[str, ...],
        raw_groups: tuple[tuple[float, ...], ...],
        output_groups: tuple[tuple[str, ...], ...],
        receipt_groups: tuple[str, ...],
    ) -> None:
        for seed, evaluation_kind, expected_raw, expected_outputs, expected_receipt in zip(
            seeds, evaluation_kinds, raw_groups, output_groups, receipt_groups, strict=True
        ):
            raw_scores: list[float] = []
            outputs: list[str] = []
            for index, task in enumerate(task_refs):
                row = indexed.get((evaluation_kind, seed, task.task_id))
                if row is None or (
                    row.get("dataset_id") != task.dataset_id
                    or row.get("split_id") != task.split_id
                    or row.get("session_id") != task.session_id
                    or row.get("profile_sha256") != task.profile_sha256
                    or row.get("task_family", task.family) != task.family
                ):
                    raise ValueError("Scorer row identity differs from calibration task")
                raw_value = row.get("task_score", row.get("vus_pr"))
                if isinstance(raw_value, bool) or not isinstance(raw_value, (float, int, str)):
                    raise ValueError("Scorer row lacks family-specific task_score")
                try:
                    raw_score = float(raw_value)
                except (TypeError, ValueError) as exc:
                    raise ValueError("Scorer task_score is malformed") from exc
                if not np.isfinite(raw_score) or raw_score.hex() != expected_raw[index].hex():
                    raise ValueError("Scorer raw score differs from immutable replay input")
                output_digest = row.get("candidate_output_sha256")
                if output_digest != expected_outputs[index]:
                    raise ValueError("Scorer output receipt differs from immutable replay input")
                output_payload = read_artifact_bytes(output_digest)
                if hashlib.sha256(output_payload).hexdigest() != output_digest:
                    raise ValueError("Scorer output blob failed its SHA-256 check")
                raw_scores.append(raw_score)
                outputs.append(output_digest)
            receipt = hashlib.sha256(
                canonical_bytes(
                    {
                        "experiment_id": experiment_id,
                        "seed": seed,
                        "evaluation_kind": evaluation_kind,
                        "task_ids": manifest.task_ids,
                        "raw_scores": tuple(raw_scores),
                        "output_sha256": tuple(outputs),
                    }
                )
            ).hexdigest()
            if receipt != expected_receipt:
                raise ValueError("Scorer task/seed aggregate receipt hash mismatch")

    verify_seed_group(
        parent_rows,
        parent_experiment.experiment_id,
        manifest.parent_seed_ids,
        manifest.parent_seed_evaluation_kinds,
        manifest.parent_raw_scores,
        manifest.parent_seed_output_sha256,
        manifest.parent_seed_score_sha256,
    )
    verify_seed_group(
        child_rows,
        child_experiment.experiment_id,
        manifest.decision_seeds,
        tuple("primary" if seed == 0 else "confirmation" for seed in manifest.decision_seeds),
        manifest.child_raw_scores,
        manifest.child_seed_output_sha256,
        manifest.child_seed_score_sha256,
    )
    families_by_task = {task.task_id: task.family for task in task_refs}
    position_guard_passed = True
    for seed, evaluation_kind, output_group in zip(
        manifest.decision_seeds,
        ("primary" if seed == 0 else "confirmation" for seed in manifest.decision_seeds),
        manifest.child_seed_output_sha256,
        strict=True,
    ):
        vectors: dict[str, tuple[float, ...]] = {}
        for index, task in enumerate(task_refs):
            if task.family != "NRM":
                continue
            digest = output_group[index]
            payload = read_artifact_bytes(digest)
            if hashlib.sha256(payload).hexdigest() != digest:
                raise ValueError("NRM position-guard output blob failed its SHA-256 check")
            artifact = parse_candidate_score(payload)
            if artifact.sample_indices != list(range(len(artifact.scores))):
                raise ValueError("NRM position-guard scores do not preserve ordered time")
            vectors[task.task_id] = tuple(float(value) for value in artifact.scores)
        check = check_complete_suite_position_bias(
            vectors, families_by_task, threshold=manifest.position_bias_threshold
        )
        position_guard_passed = position_guard_passed and check.passed
        for task_id, derived_bias in check.task_bias.items():
            row = child_rows.get((evaluation_kind, seed, task_id))
            raw_bias = row.get("position_bias") if row is not None else None
            if isinstance(raw_bias, bool) or raw_bias is None:
                raise ValueError("Scorer row omits its NRM position-bias metric")
            try:
                stored_bias = float(raw_bias)
            except (TypeError, ValueError) as exc:
                raise ValueError("Scorer NRM position-bias metric is malformed") from exc
            if not np.isfinite(stored_bias) or not np.isclose(
                stored_bias, derived_bias, rtol=0.0, atol=1e-12
            ):
                raise ValueError("Scorer NRM bias differs from its immutable output artifact")
    recorded_position_guard = manifest.guard_results.get("position_bias")
    if recorded_position_guard not in {"pass", "fail"} or (
        recorded_position_guard == "pass"
    ) != position_guard_passed:
        raise ValueError("replayed NRM position-bias guard differs from immutable Referee inputs")
    verify_seed_group(
        parent_rows,
        parent_experiment.experiment_id,
        manifest.parent_noise_seed_ids,
        manifest.parent_noise_evaluation_kinds,
        manifest.parent_noise_raw_scores,
        manifest.parent_noise_output_sha256,
        manifest.parent_noise_score_sha256,
    )

    def normalize_rows(rows: tuple[tuple[float, ...], ...]) -> tuple[float, ...]:
        return tuple(
            float(
                normalize_task_score(
                    sum(values[index] for values in rows) / len(rows),
                    task.base_score,
                    task.reference_score,
                    family=task.family,
                )
            )
            for index, task in enumerate(task_refs)
        )

    for actual, stored in (
        (normalize_rows(manifest.parent_raw_scores), manifest.parent),
        (normalize_rows(manifest.child_raw_scores), manifest.child),
    ):
        if any(left.hex() != right.hex() for left, right in zip(actual, stored, strict=True)):
            raise ValueError("normalized replay vector differs from raw Scorer rows")
    noise_by_seed: dict[int, float] = {}
    for seed, raw_scores in zip(
        manifest.parent_noise_seed_ids, manifest.parent_noise_raw_scores, strict=True
    ):
        normalized = normalize_rows((raw_scores,))
        noise_by_seed[seed] = sum(
            value * weight for value, weight in zip(normalized, manifest.weights, strict=True)
        ) / sum(manifest.weights)
    if champion_noise_sd(noise_by_seed).hex() != manifest.noise_sd.hex():
        raise ValueError("replay champion noise differs from parent seed evidence")


def replay_run(
    engine: Engine,
    run_id: UUID,
    *,
    artifact_root: Path,
) -> tuple[ReplayedDecision, ...]:
    """Recompute every measured proposal decision; missing or changed evidence fails."""
    pairs = read_run_pairs(engine, run_id, artifact_root=artifact_root)
    calibration = read_registered_calibration(engine, run_id=run_id, artifact_root=artifact_root)
    by_id = {item.document.experiment_id: item for item in pairs}
    if len(by_id) != len(pairs):
        raise ValueError("run ledger has duplicate experiment identities")
    output: list[ReplayedDecision] = []
    if calibration.champion_seed_scores is None:
        raise ValueError("exact replay requires frozen champion seed scores")
    # Match DirectorLoop._initial_state: the frozen historical best is the
    # arithmetic mean across the three measured champion seed suite scores.
    best_so_far = _initial_best_suite(calibration.champion_seed_scores)
    for pair in pairs:
        experiment = pair.document
        if experiment.kind != "proposal":
            continue
        if experiment.decision is None:
            raise ValueError(f"proposal {experiment.experiment_id} has no stored decision")
        manifests = pair.trajectory.replay_manifests
        terminal = pair.trajectory.terminal_replay
        if not manifests and terminal is None:
            raise ValueError(
                "exact replay unavailable for "
                f"{experiment.experiment_id}: immutable manifest missing"
            )
        stages = tuple(manifest.decision_stage for manifest in manifests)
        if manifests:
            if stages not in {("primary",), ("primary", "confirmed")}:
                raise ValueError(
                    "replay manifests are missing, duplicated, or out of decision order"
                )
            if experiment.decision.verdict in {"KEEP", "KEEP_SIMPLER"} and stages != (
                "primary",
                "confirmed",
            ):
                raise ValueError("promoted decision has no measured confirmation replay")
        trajectory_payload = canonical_json_bytes(pair.trajectory)
        for manifest in manifests:
            _manifest_for_pair(
                experiment,
                manifest,
                engine=engine,
                run_id=run_id,
                calibration=calibration,
                artifact_root=artifact_root,
                by_id=by_id,
                expected_best_suite=best_so_far,
            )
            if tuple(row.task_id for row in experiment.per_task) != manifest.task_ids:
                raise ValueError("replay task order differs from immutable per-task ledger order")
            if manifest.decision_stage == "confirmed" and manifest.decision_seeds != (0, 1):
                raise ValueError(
                    "confirmation replay does not contain primary and confirmation seeds"
                )
            if manifest.decision_stage == "primary" and manifest.decision_seeds != (0,):
                raise ValueError("primary replay does not contain only primary seed zero")
            result = decide(
                manifest.parent,
                manifest.child,
                manifest.weights,
                eps=manifest.eps,
                noise_sd=manifest.noise_sd,
                simpler=manifest.simpler,
                guards_ok=manifest.guards_ok,
                max_task_drop=manifest.max_task_drop,
                best_suite=manifest.best_suite,
                n_boot=manifest.n_boot,
                seed=manifest.bootstrap_seed,
            )
            recorded = manifest.expected_decision
            if (
                result.verdict != recorded.verdict
                or not _same_float(result.delta, recorded.delta)
                or not _same_float(result.ci_low, recorded.ci_low)
                or result.reason[:128] != recorded.reason
                or not _same_float(manifest.noise_sd, recorded.noise_sd)
            ):
                raise ValueError(
                    f"exact Referee replay mismatch for {experiment.experiment_id} "
                    f"at {manifest.decision_stage} stage"
                )
            output.append(
                ReplayedDecision(
                    experiment_id=experiment.experiment_id,
                    decision_stage=manifest.decision_stage,
                    verdict=result.verdict,
                    delta=result.delta,
                    ci_low=result.ci_low,
                    noise_sd=manifest.noise_sd,
                    manifest_sha256=hashlib.sha256(trajectory_payload).hexdigest(),
                    configuration_sha256=manifest.configuration_sha256,
                )
            )
        if terminal is not None:
            if (
                terminal.experiment_id != experiment.experiment_id
                or terminal.run_id != run_id
                or terminal.candidate_sha256 != experiment.candidate_sha256
                or terminal.parent_experiment_id != experiment.parent_experiment_id
                or terminal.parent_tree != experiment.parent_tree
                or terminal.calibration_sha256 != experiment.calibration_sha256
                or terminal.harness_sha256 != experiment.harness_sha256
                or terminal.image_sha256 != experiment.image_sha256
            ):
                raise ValueError("terminal replay disposition differs from experiment identity")
            expected_status = "crashed" if terminal.evaluation_status == "crashed" else "rejected"
            if experiment.status != expected_status or terminal.decision != experiment.decision:
                raise ValueError(
                    "unmeasured terminal disposition differs from final ledger outcome"
                )
            if (
                terminal.decision.verdict != "REJECT"
                or terminal.decision.delta is not None
                or terminal.decision.ci_low is not None
                or terminal.decision.reason != terminal.terminal_code
            ):
                raise ValueError("terminal disposition must preserve a scoreless strict rejection")
            output.append(
                ReplayedDecision(
                    experiment_id=experiment.experiment_id,
                    decision_stage="terminal",
                    verdict=terminal.decision.verdict,
                    delta=None,
                    ci_low=None,
                    noise_sd=terminal.decision.noise_sd,
                    manifest_sha256=hashlib.sha256(trajectory_payload).hexdigest(),
                    configuration_sha256=hashlib.sha256(
                        canonical_bytes(terminal.model_dump(mode="json", by_alias=True))
                    ).hexdigest(),
                )
            )
        else:
            last_decision = manifests[-1].expected_decision
            last_manifest = manifests[-1]
            if any(
                not _same_float(row.score_norm, score)
                for row, score in zip(experiment.per_task, last_manifest.child, strict=True)
            ):
                raise ValueError("final child vector differs from immutable per-task ledger")
            if (
                experiment.decision.verdict != last_decision.verdict
                or not _same_float(experiment.decision.delta, last_decision.delta)
                or not _same_float(experiment.decision.ci_low, last_decision.ci_low)
                or not _same_float(experiment.decision.noise_sd, last_decision.noise_sd)
                or experiment.decision.reason != last_decision.reason
            ):
                raise ValueError("final experiment decision differs from its final replay stage")
            if experiment.decision.verdict in {"KEEP", "KEEP_SIMPLER"}:
                if experiment.suite_score is None or not np.isfinite(experiment.suite_score):
                    raise ValueError("promoted experiment has no finite recorded suite score")
                best_so_far = max(best_so_far, experiment.suite_score)
    return tuple(output)
