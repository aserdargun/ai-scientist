"""Scorer-private, measured Farm B baseline calibration primitives.

This module deliberately has no Director result adapter. Feature matrices enter
the sandbox through ``run_candidate_fit_score``; private labels and temporal
semantics are read and consumed only in this Scorer process.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import numpy as np
import pandas as pd
from sqlalchemy import Engine, text

from harness.baselines import (
    BASELINE_SEEDS,
    BASELINE_VERSIONS,
    FrozenTaskReferences,
    freeze_task_references,
)
from harness.contracts import FitContext
from harness.fingerprint import compute_harness_hash
from lab.director.baselines import baseline_candidate_source
from lab.sandbox.docker_runner import DEFAULT_SANDBOX_IMAGE, PROJECT_ROOT, LocalDockerRunner
from lab.sandbox.evaluation import run_candidate_fit_score
from lab.scorer.holdout import (
    DEFAULT_HOLDOUT_INPUT_ROOT,
    _frame_bytes,
    _read_arrow_frame,
    _read_labels,
    _read_task_semantics,
    _store_holdout_artifact,
    _task_metric,
    _validate_semantics,
)

CARE_BASELINE_ALGORITHMS = ("robust_z", "iforest", "ecod_train_frozen")
CARE_TASK_COUNT = 15
CARE_CELL_COUNT = CARE_TASK_COUNT * len(CARE_BASELINE_ALGORITHMS) * len(BASELINE_SEEDS)
CARE_TASK_WEIGHT_POLICY = "farm-b-equal-task-weight-1-over-15.v1"
CARE_TASK_WEIGHT = 1.0 / CARE_TASK_COUNT
CARE_CELL_RESERVATION_SECONDS = 100
MAX_CARE_CALIBRATION_WALL_SECONDS = 14_400
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _is_valid_alarm_policy(policy: Mapping[str, object]) -> bool:
    if set(policy) != {"threshold", "release", "dwell"}:
        return False
    threshold = policy.get("threshold")
    release = policy.get("release")
    dwell = policy.get("dwell")
    return (
        isinstance(threshold, (int, float))
        and not isinstance(threshold, bool)
        and math.isfinite(float(threshold))
        and isinstance(release, (int, float))
        and not isinstance(release, bool)
        and math.isfinite(float(release))
        and float(release) <= float(threshold)
        and isinstance(dwell, int)
        and not isinstance(dwell, bool)
        and dwell >= 1
    )


@dataclass(frozen=True, slots=True)
class CareCalibrationTask:
    """Scorer input identity and feature matrices, without labels or semantics."""

    task_id: str
    dataset_id: str
    split_id: str
    session_id: str
    task_key: str
    profile_sha256: str
    family: str
    train_sha256: str
    evaluation_sha256: str
    labels_sha256: str
    semantics_sha256: str
    source_member_sha256: str
    source_archive_sha256: str
    source_manifest_sha256: str
    sliding_window: int
    sampling_s: int
    context: FitContext
    train: pd.DataFrame
    evaluation: pd.DataFrame

    def __post_init__(self) -> None:
        if not self.task_id or not self.dataset_id or not self.split_id or not self.session_id:
            raise ValueError("CARE calibration task identity is incomplete")
        if re.fullmatch(r"[0-9a-f]{32}", self.task_key) is None:
            raise ValueError("CARE calibration task_key must be 32 lowercase hex characters")
        for name in (
            "profile_sha256",
            "train_sha256",
            "evaluation_sha256",
            "labels_sha256",
            "semantics_sha256",
            "source_member_sha256",
            "source_archive_sha256",
            "source_manifest_sha256",
        ):
            if _SHA256.fullmatch(getattr(self, name)) is None:
                raise ValueError(f"CARE calibration {name} must be lowercase SHA-256")
        if self.family not in {"PDM", "NRM"}:
            raise ValueError("CARE calibration task family must be PDM or NRM")
        if self.context.sampling_s != self.sampling_s:
            raise ValueError("CARE task cadence differs from its FitContext")
        if self.sliding_window < 1 or self.sampling_s < 1:
            raise ValueError("CARE task window and cadence must be positive")
        if self.train.empty or self.evaluation.empty:
            raise ValueError("CARE calibration feature matrices must not be empty")
        if (
            tuple(map(str, self.train.columns)) != self.context.signals
            or tuple(map(str, self.evaluation.columns)) != self.context.signals
        ):
            raise ValueError("CARE feature columns differ from the trusted FitContext")
        if (
            not np.isfinite(self.train.to_numpy(dtype=np.float64)).all()
            or not np.isfinite(self.evaluation.to_numpy(dtype=np.float64)).all()
        ):
            raise ValueError("CARE feature matrices must contain only finite numbers")

    def identity_json(self) -> dict[str, object]:
        """Return only label-free identity used by the immutable job manifest."""
        context = asdict(self.context)
        return {
            "task_id": self.task_id,
            "dataset_id": self.dataset_id,
            "split_id": self.split_id,
            "session_id": self.session_id,
            "task_key": self.task_key,
            "profile_sha256": self.profile_sha256,
            "family": self.family,
            "train_sha256": self.train_sha256,
            "evaluation_sha256": self.evaluation_sha256,
            "labels_sha256": self.labels_sha256,
            "semantics_sha256": self.semantics_sha256,
            "source_member_sha256": self.source_member_sha256,
            "source_archive_sha256": self.source_archive_sha256,
            "source_manifest_sha256": self.source_manifest_sha256,
            "sliding_window": self.sliding_window,
            "sampling_s": self.sampling_s,
            "context": context,
        }


@dataclass(frozen=True, slots=True)
class CareCalibrationManifest:
    """Immutable source and execution identity fixed before cell dispatch."""

    calibration_id: str
    suite_id: str
    suite_version: int
    suite_manifest_sha256: str
    development_manifest_sha256: str
    epsilon: float
    task_weight_policy: str
    task_weight_policy_sha256: str
    source_manifest_sha256: str
    source_archive_sha256: str
    harness_sha256: str
    image_sha256: str
    wall_limit_seconds: int
    tasks: tuple[dict[str, object], ...]
    baseline_source_sha256: Mapping[str, str]
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class CareBaselineCellReceipt:
    """Private terminal measurement for one task × algorithm × seed."""

    task_id: str
    task_key: str
    algorithm: str
    algorithm_version: str
    seed: int
    baseline_source_sha256: str
    harness_sha256: str
    image_sha256: str
    profile_sha256: str
    train_sha256: str
    evaluation_sha256: str
    labels_sha256: str
    semantics_sha256: str
    fit_context_sha256: str
    fit_artifact_sha256: str
    score_document_sha256: str
    policy: dict[str, object]
    raw_task_score: float
    auxiliary_metrics: Mapping[str, float]
    fit_seconds: float
    score_seconds: float
    claim_id: UUID | None = None
    claim_generation: int | None = None
    worker_invocation_id: str | None = None

    def canonical(self) -> dict[str, object]:
        payload = asdict(self)
        if self.claim_id is None:
            payload["claim_id"] = None
        elif isinstance(self.claim_id, UUID):
            payload["claim_id"] = str(self.claim_id)
        else:
            raise TypeError("CARE receipt claim_id must be a UUID")
        return payload


@dataclass(frozen=True, slots=True)
class CareBaselineCellClaim:
    """Scorer-authoritative cell admission; only a new claim may launch a worker."""

    claim_id: UUID | None
    generation: int | None
    state: str
    reserved_seconds: int
    reserved_at: datetime | None
    deadline_at: datetime | None
    newly_created: bool


def _rpc_json_object(value: object, *, label: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Scorer returned malformed {label}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Scorer returned malformed {label}")
    return cast(dict[str, Any], value)


def _claim_from_row(row: Mapping[str, Any], *, newly_created: bool) -> CareBaselineCellClaim:
    claim_id = row.get("claim_id")
    generation = row.get("generation")
    state = row.get("state")
    reserved_seconds = row.get("reserved_seconds")
    reserved_at = row.get("reserved_at")
    deadline_at = row.get("deadline_at")
    if (
        not isinstance(claim_id, UUID)
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
        or isinstance(reserved_seconds, bool)
        or not isinstance(reserved_seconds, int)
        or reserved_seconds != CARE_CELL_RESERVATION_SECONDS
        or not isinstance(state, str)
        or state not in {"reserved", "running", "succeeded", "failed"}
        or not isinstance(reserved_at, datetime)
        or not isinstance(deadline_at, datetime)
    ):
        raise ValueError("Scorer returned an invalid durable CARE claim")
    return CareBaselineCellClaim(
        claim_id,
        generation,
        state,
        reserved_seconds,
        reserved_at,
        deadline_at,
        newly_created,
    )


def read_care_baseline_cell_claim(
    engine: Engine, calibration_id: UUID, *, task_key: str, algorithm: str, seed: int
) -> CareBaselineCellClaim | None:
    """Read an existing cell before deciding whether the budget permits new work."""
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT claim_id,generation,state,reserved_seconds,reserved_at,deadline_at "
                    "FROM scorer.care_baseline_calibration_claims "
                    "WHERE calibration_id=:calibration_id AND task_key=:task_key "
                    "AND algorithm=:algorithm AND seed=:seed"
                ),
                {
                    "calibration_id": calibration_id,
                    "task_key": task_key,
                    "algorithm": algorithm,
                    "seed": seed,
                },
            )
            .mappings()
            .one_or_none()
        )
    return (
        None if row is None else _claim_from_row(cast(Mapping[str, Any], row), newly_created=False)
    )


def reserve_care_baseline_cell(
    engine: Engine,
    calibration_id: str,
    *,
    task_key: str,
    algorithm: str,
    seed: int,
) -> CareBaselineCellClaim:
    """Durably charge a fixed cell reservation before any scorer worker launch."""
    try:
        parsed_id = UUID(calibration_id)
    except (ValueError, AttributeError) as exc:
        raise ValueError("CARE calibration_id must be a UUID") from exc
    if re.fullmatch(r"[0-9a-f]{32}", task_key) is None:
        raise ValueError("CARE task key is malformed")
    if (
        algorithm not in CARE_BASELINE_ALGORITHMS
        or isinstance(seed, bool)
        or not isinstance(seed, int)
        or seed not in BASELINE_SEEDS
    ):
        raise ValueError("CARE cell is outside the fixed algorithm/seed grid")
    proposed_claim_id = uuid4()
    with engine.begin() as connection:
        raw = connection.execute(
            text(
                "SELECT lab.reserve_care_baseline_cell(:calibration_id,:claim_id,:task_key,"
                ":algorithm,:seed)"
            ),
            {
                "calibration_id": parsed_id,
                "claim_id": proposed_claim_id,
                "task_key": task_key,
                "algorithm": algorithm,
                "seed": seed,
            },
        ).scalar_one()
    payload = _rpc_json_object(raw, label="CARE cell reservation")
    state = payload.get("state")
    newly_created = payload.get("newly_created")
    if state == "incomplete" and newly_created is False:
        return CareBaselineCellClaim(None, None, "incomplete", 0, None, None, False)
    if type(newly_created) is not bool or (newly_created and state != "reserved"):
        raise ValueError("Scorer returned an invalid CARE claim state")
    for key in ("reserved_at", "deadline_at"):
        timestamp = payload.get(key)
        if isinstance(timestamp, str):
            try:
                payload[key] = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"Scorer returned an invalid CARE {key} timestamp") from exc
    try:
        payload["claim_id"] = UUID(str(payload["claim_id"]))
        return _claim_from_row(payload, newly_created=newly_created)
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("Scorer returned an incomplete CARE claim") from exc


def bind_care_baseline_worker(
    engine: Engine,
    claim_id: UUID,
    generation: int,
    *,
    pid: int,
    start_ticks: str,
    boot_id: str,
    unit: str,
    invocation_id: str,
    cgroup: str,
) -> None:
    """Bind one reserved claim to the exact systemd worker generation."""
    with engine.begin() as connection:
        raw = connection.execute(
            text(
                "SELECT lab.bind_care_baseline_worker(:claim_id,:generation,:pid,:start_ticks,"
                ":boot_id,:unit,:invocation_id,:cgroup)"
            ),
            {
                "claim_id": claim_id,
                "generation": generation,
                "pid": pid,
                "start_ticks": start_ticks,
                "boot_id": boot_id,
                "unit": unit,
                "invocation_id": invocation_id,
                "cgroup": cgroup,
            },
        ).scalar_one()
    payload = _rpc_json_object(raw, label="CARE worker binding")
    if (
        (payload.get("claim_id") != claim_id and str(payload.get("claim_id")) != str(claim_id))
        or payload.get("generation") != generation
        or payload.get("state") != "running"
    ):
        raise ValueError("Scorer returned a mismatched CARE worker binding")


def fail_care_baseline_cell(
    engine: Engine,
    claim_id: UUID,
    generation: int,
    failure_code: str,
    *,
    worker_invocation_id: str | None = None,
) -> None:
    """Persist a bitless terminal failure without refunding its reservation."""
    with engine.begin() as connection:
        raw = connection.execute(
            text(
                "SELECT lab.fail_care_baseline_cell(:claim_id,:generation,:failure_code,"
                ":worker_invocation_id)"
            ),
            {
                "claim_id": claim_id,
                "generation": generation,
                "failure_code": failure_code,
                "worker_invocation_id": worker_invocation_id,
            },
        ).scalar_one()
    payload = _rpc_json_object(raw, label="CARE cell failure")
    if str(payload.get("claim_id")) != str(claim_id) or (
        payload.get("generation") != generation
        or payload.get("state") != "failed"
        or payload.get("failure_code") != failure_code
    ):
        raise ValueError("Scorer returned a mismatched CARE cell failure")


@dataclass(frozen=True, slots=True)
class CareTaskCalibration:
    task_id: str
    task_key: str
    family: str
    task_weight: float
    base_score: float
    reference_score: float
    references: FrozenTaskReferences


@dataclass(frozen=True, slots=True)
class FrozenCareBaselineCalibration:
    """Private frozen measurement document and its digest."""

    calibration_id: str
    suite_id: str
    suite_version: int
    suite_manifest_sha256: str
    development_manifest_sha256: str
    epsilon: float
    task_weight_policy: str
    manifest_sha256: str
    calibration_sha256: str
    task_count: int
    cell_count: int
    tasks: tuple[CareTaskCalibration, ...]
    receipts: tuple[CareBaselineCellReceipt, ...]


def build_care_calibration_manifest(
    *,
    calibration_id: str,
    suite_id: str,
    suite_version: int,
    suite_manifest_sha256: str,
    development_manifest_sha256: str,
    epsilon: float,
    tasks: Sequence[CareCalibrationTask],
    harness_sha256: str,
    image_sha256: str,
    wall_limit_seconds: int = MAX_CARE_CALIBRATION_WALL_SECONDS,
) -> CareCalibrationManifest:
    """Bind all input and algorithm identities before any baseline is run."""
    if suite_id != "care-farm-b-measured":
        raise ValueError("CARE baseline calibration can bind only the measured Farm B suite")
    if not calibration_id or isinstance(suite_version, bool) or suite_version < 1:
        raise ValueError("CARE calibration identity is invalid")
    try:
        UUID(calibration_id)
    except ValueError as exc:
        raise ValueError("CARE calibration_id must be a UUID") from exc
    if (
        _SHA256.fullmatch(suite_manifest_sha256) is None
        or _SHA256.fullmatch(development_manifest_sha256) is None
        or not math.isfinite(epsilon)
        or not 0 <= epsilon < 1
    ):
        raise ValueError("CARE calibration suite manifest or epsilon is invalid")
    if len(tasks) != CARE_TASK_COUNT or len({task.task_id for task in tasks}) != CARE_TASK_COUNT:
        raise ValueError("CARE calibration requires the exact 15 unique Farm B tasks")
    families = [task.family for task in tasks]
    if families.count("PDM") != 6 or families.count("NRM") != 9:
        raise ValueError("CARE calibration requires exactly six PDM and nine NRM tasks")
    if (
        len({task.source_manifest_sha256 for task in tasks}) != 1
        or len({task.source_archive_sha256 for task in tasks}) != 1
    ):
        raise ValueError("CARE calibration tasks have mixed source identities")
    for digest in (harness_sha256, image_sha256):
        if _SHA256.fullmatch(digest) is None:
            raise ValueError("CARE calibration runtime identity must be lowercase SHA-256")
    if not 1 <= wall_limit_seconds <= MAX_CARE_CALIBRATION_WALL_SECONDS:
        raise ValueError("CARE calibration wall budget exceeds the 4-hour hard limit")
    ordered_tasks = tuple(
        sorted(
            tasks, key=lambda task: (task.dataset_id, task.split_id, task.session_id, task.task_id)
        )
    )
    if any(
        task.task_key
        != hashlib.sha256(f"{suite_manifest_sha256}:{task.task_id}".encode()).hexdigest()[:32]
        for task in ordered_tasks
    ):
        raise ValueError("CARE task keys do not match the frozen suite manifest")
    weights = {task.task_id: CARE_TASK_WEIGHT for task in ordered_tasks}
    policy_sha = _sha256(
        _canonical_json(
            {
                "policy_id": CARE_TASK_WEIGHT_POLICY,
                "task_count": CARE_TASK_COUNT,
                "weights": weights,
                "family_counts": {"PDM": 6, "NRM": 9},
            }
        )
    )
    sources = {name: _sha256(baseline_candidate_source(name)) for name in CARE_BASELINE_ALGORITHMS}
    source_manifest = ordered_tasks[0].source_manifest_sha256
    source_archive = ordered_tasks[0].source_archive_sha256
    body: dict[str, object] = {
        "schema": "care-farm-b-baseline-calibration-manifest.v1",
        "calibration_id": calibration_id,
        "suite_id": suite_id,
        "suite_version": suite_version,
        "suite_manifest_sha256": suite_manifest_sha256,
        "development_manifest_sha256": development_manifest_sha256,
        "epsilon": epsilon,
        "task_weight_policy": CARE_TASK_WEIGHT_POLICY,
        "task_weight_policy_sha256": policy_sha,
        "source_manifest_sha256": source_manifest,
        "source_archive_sha256": source_archive,
        "harness_sha256": harness_sha256,
        "image_sha256": image_sha256,
        "wall_limit_seconds": wall_limit_seconds,
        "algorithms": [
            {
                "name": name,
                "version": BASELINE_VERSIONS[name],
                "source_sha256": sources[name],
            }
            for name in CARE_BASELINE_ALGORITHMS
        ],
        "seeds": list(BASELINE_SEEDS),
        "tasks": [task.identity_json() for task in ordered_tasks],
    }
    manifest_sha256 = _sha256(_canonical_json(body))
    return CareCalibrationManifest(
        calibration_id=calibration_id,
        suite_id=suite_id,
        suite_version=suite_version,
        suite_manifest_sha256=suite_manifest_sha256,
        development_manifest_sha256=development_manifest_sha256,
        epsilon=epsilon,
        task_weight_policy=CARE_TASK_WEIGHT_POLICY,
        task_weight_policy_sha256=policy_sha,
        source_manifest_sha256=source_manifest,
        source_archive_sha256=source_archive,
        harness_sha256=harness_sha256,
        image_sha256=image_sha256,
        wall_limit_seconds=wall_limit_seconds,
        tasks=tuple(task.identity_json() for task in ordered_tasks),
        baseline_source_sha256=sources,
        manifest_sha256=manifest_sha256,
    )


def score_care_baseline_cell(
    engine: Engine,
    runner: LocalDockerRunner,
    *,
    task: CareCalibrationTask,
    algorithm: str,
    seed: int,
    harness_sha256: str,
    image_sha256: str,
    remaining_seconds: int,
    admission_check: Any | None = None,
) -> CareBaselineCellReceipt:
    """Run one allowlisted fit/score pair and calculate its private family metric."""
    if (
        algorithm not in CARE_BASELINE_ALGORITHMS
        or isinstance(seed, bool)
        or not isinstance(seed, int)
        or seed not in BASELINE_SEEDS
    ):
        raise ValueError("CARE calibration algorithm or seed is outside the fixed grid")
    if not 1 <= remaining_seconds <= 600:
        raise ValueError("CARE calibration cell deadline must be within 1..600 seconds")
    deadline = time.monotonic() + remaining_seconds

    def remaining() -> int:
        value = math.floor(deadline - time.monotonic())
        if value < 1:
            raise TimeoutError("CARE calibration cell deadline is exhausted")
        return value

    if runner.image != DEFAULT_SANDBOX_IMAGE:
        raise ValueError("CARE calibration requires the pinned sandbox image")
    if compute_harness_hash(PROJECT_ROOT).sha256 != harness_sha256:
        raise ValueError("CARE calibration harness digest differs from the trusted source tree")
    if image_sha256 != DEFAULT_SANDBOX_IMAGE.rsplit("@sha256:", 1)[-1].removeprefix("sha256:"):
        raise ValueError("CARE calibration image digest differs from the pinned image")
    source = baseline_candidate_source(algorithm)
    source_sha256 = _sha256(source)
    context = FitContext(
        seed=seed,
        signals=task.context.signals,
        regime_signals=task.context.regime_signals,
        sampling_s=task.context.sampling_s,
        time_budget_s=task.context.time_budget_s,
    )
    fit_context_sha256 = _sha256(_canonical_json(asdict(context)))
    if (
        _sha256(_frame_bytes(task.train, columns=context.signals)) != task.train_sha256
        or _sha256(_frame_bytes(task.evaluation, columns=context.signals)) != task.evaluation_sha256
    ):
        raise ValueError("CARE calibration feature matrix differs from its immutable blob digest")
    row = {
        "task_key": task.task_key,
        "dataset_id": task.dataset_id,
        "split_id": task.split_id,
        "session_id": task.session_id,
        "profile_sha256": task.profile_sha256,
        "family": task.family,
        "semantics_sha256": task.semantics_sha256,
        "sampling_s": task.sampling_s,
        "sliding_window": task.sliding_window,
    }
    labels = _read_labels(engine, row, len(task.evaluation))
    label_sha256 = _sha256(_canonical_json([int(value) for value in labels]))
    if label_sha256 != task.labels_sha256:
        raise ValueError("CARE calibration labels differ from the frozen Scorer profile")
    semantics = _read_task_semantics(engine, row)
    _validate_semantics(
        semantics,
        task_family=task.family,
        sample_count=len(task.evaluation),
        sampling_s=task.sampling_s,
        labels=labels,
    )
    measured = run_candidate_fit_score(
        runner,
        candidate_source=source,
        train=task.train,
        evaluation=task.evaluation,
        context=context,
        remaining_seconds=remaining(),
        fit_timeout_seconds=min(60, remaining()),
        score_timeout_seconds=min(30, remaining()),
        trusted_baseline_name=algorithm,
        admission_check=admission_check,
    )
    raw_score, auxiliary = _task_metric(
        family=task.family,
        labels=labels,
        scores=measured.scores,
        policy=measured.policy,
        semantics=semantics,
        sliding_window=task.sliding_window,
        sampling_s=task.sampling_s,
    )
    if not math.isfinite(raw_score) or not 0 <= raw_score <= 1:
        raise ValueError("CARE baseline family metric must be finite and within [0,1]")
    return CareBaselineCellReceipt(
        task_id=task.task_id,
        task_key=task.task_key,
        algorithm=algorithm,
        algorithm_version=BASELINE_VERSIONS[algorithm],
        seed=seed,
        baseline_source_sha256=source_sha256,
        harness_sha256=harness_sha256,
        image_sha256=image_sha256,
        profile_sha256=task.profile_sha256,
        train_sha256=task.train_sha256,
        evaluation_sha256=task.evaluation_sha256,
        labels_sha256=label_sha256,
        semantics_sha256=task.semantics_sha256,
        fit_context_sha256=fit_context_sha256,
        fit_artifact_sha256=measured.fit_artifact_sha256,
        score_document_sha256=_sha256(measured.score_document),
        policy=asdict(measured.policy),
        raw_task_score=float(raw_score),
        auxiliary_metrics={str(key): float(value) for key, value in auxiliary.items()},
        fit_seconds=float(measured.fit_seconds),
        score_seconds=float(measured.score_seconds),
    )


def run_care_baseline_cell(
    engine: Engine,
    runner: LocalDockerRunner,
    manifest: CareCalibrationManifest,
    task: CareCalibrationTask,
    *,
    algorithm: str,
    seed: int,
    cell_limit_seconds: int = 600,
) -> CareBaselineCellReceipt:
    """Execute and durably record one cell against the job's original deadline."""
    if not 1 <= cell_limit_seconds <= 600:
        raise ValueError("CARE calibration cell limit must be within 1..600 seconds")
    with engine.connect() as connection:
        job = (
            connection.execute(
                text(
                    "SELECT calibration_manifest_sha256,harness_sha256,image_sha256,deadline_at,"
                    "source_manifest_sha256,source_archive_sha256 "
                    "FROM scorer.care_baseline_calibration_jobs "
                    "WHERE calibration_id=:calibration_id"
                ),
                {"calibration_id": UUID(manifest.calibration_id)},
            )
            .mappings()
            .one_or_none()
        )
        task_row = (
            connection.execute(
                text(
                    "SELECT task_id,dataset_id,split_id,session_id,profile_sha256,family,"
                    "task_weight,"
                    "train_sha256,evaluation_sha256,evaluation_rows,labels_sha256,semantics_sha256,"
                    "source_member_sha256,sliding_window,sampling_s,context_json "
                    "FROM scorer.care_baseline_calibration_tasks "
                    "WHERE calibration_id=:calibration_id AND task_key=:task_key"
                ),
                {"calibration_id": UUID(manifest.calibration_id), "task_key": task.task_key},
            )
            .mappings()
            .one_or_none()
        )
        seconds_left = connection.execute(
            text("SELECT floor(extract(epoch FROM (:deadline_at - clock_timestamp())))::integer"),
            {"deadline_at": job["deadline_at"]}
            if job is not None
            else {"deadline_at": datetime.now(UTC)},
        ).scalar_one()
    expected_task_identity = (
        task.task_id,
        task.dataset_id,
        task.split_id,
        task.session_id,
        task.profile_sha256,
        task.family,
        CARE_TASK_WEIGHT,
        task.train_sha256,
        task.evaluation_sha256,
        len(task.evaluation),
        task.labels_sha256,
        task.semantics_sha256,
        task.source_member_sha256,
        task.sliding_window,
        task.sampling_s,
        json.loads(_canonical_json(asdict(task.context))),
    )
    stored_task_identity: tuple[Any, ...] | None = (
        None if task_row is None else tuple(task_row.values())
    )
    if (
        job is None
        or stored_task_identity is None
        or any(
            stored != expected
            for stored, expected in zip(stored_task_identity, expected_task_identity, strict=True)
        )
        or job["calibration_manifest_sha256"] != manifest.manifest_sha256
        or job["harness_sha256"] != manifest.harness_sha256
        or job["image_sha256"] != manifest.image_sha256
        or job["source_manifest_sha256"] != task.source_manifest_sha256
        or job["source_archive_sha256"] != task.source_archive_sha256
    ):
        raise ValueError("CARE calibration cell differs from its durable job identity")
    remaining = min(cell_limit_seconds, seconds_left)
    if remaining < 1:
        raise TimeoutError("CARE calibration job wall deadline is exhausted")
    receipt = score_care_baseline_cell(
        engine,
        runner,
        task=task,
        algorithm=algorithm,
        seed=seed,
        harness_sha256=manifest.harness_sha256,
        image_sha256=manifest.image_sha256,
        remaining_seconds=remaining,
    )
    persist_care_baseline_cell(engine, manifest.calibration_id, receipt)
    return receipt


def freeze_care_baseline_calibration(
    manifest: CareCalibrationManifest,
    tasks: Sequence[CareCalibrationTask],
    receipts: Sequence[CareBaselineCellReceipt],
) -> FrozenCareBaselineCalibration:
    """Recompute task base/ref from the exact full 135-cell trusted grid."""
    expected_manifest = build_care_calibration_manifest(
        calibration_id=manifest.calibration_id,
        suite_id=manifest.suite_id,
        suite_version=manifest.suite_version,
        suite_manifest_sha256=manifest.suite_manifest_sha256,
        development_manifest_sha256=manifest.development_manifest_sha256,
        epsilon=manifest.epsilon,
        tasks=tasks,
        harness_sha256=manifest.harness_sha256,
        image_sha256=manifest.image_sha256,
        wall_limit_seconds=manifest.wall_limit_seconds,
    )
    if expected_manifest != manifest:
        raise ValueError("CARE calibration manifest identity was altered after provisioning")
    receipt_cells = [(receipt.task_id, receipt.algorithm, receipt.seed) for receipt in receipts]
    if len(receipt_cells) != len(set(receipt_cells)):
        raise ValueError("CARE calibration contains a duplicate task/algorithm/seed cell")
    if len(receipts) != CARE_CELL_COUNT:
        raise ValueError("CARE calibration cannot freeze an incomplete 135-cell grid")
    ordered_tasks = tuple(
        sorted(
            tasks, key=lambda task: (task.dataset_id, task.split_id, task.session_id, task.task_id)
        )
    )
    if tuple(task.identity_json() for task in ordered_tasks) != manifest.tasks:
        raise ValueError("CARE calibration inputs differ from the immutable manifest")
    receipt_by_cell: dict[tuple[str, str, int], CareBaselineCellReceipt] = {}
    for receipt in receipts:
        cell = (receipt.task_id, receipt.algorithm, receipt.seed)
        if cell in receipt_by_cell:
            raise ValueError("CARE calibration contains a duplicate task/algorithm/seed cell")
        receipt_by_cell[cell] = receipt
    expected_cells = {
        (task.task_id, name, seed)
        for task in ordered_tasks
        for name in CARE_BASELINE_ALGORITHMS
        for seed in BASELINE_SEEDS
    }
    if set(receipt_by_cell) != expected_cells:
        raise ValueError("CARE calibration cells do not exactly cover the fixed grid")
    task_results: list[CareTaskCalibration] = []
    canonical_receipts: list[CareBaselineCellReceipt] = []
    for task in ordered_tasks:
        per_algorithm: dict[str, dict[int, float]] = {}
        for algorithm in CARE_BASELINE_ALGORITHMS:
            per_algorithm[algorithm] = {}
            for seed in BASELINE_SEEDS:
                receipt = receipt_by_cell[(task.task_id, algorithm, seed)]
                expected_context = FitContext(
                    seed=seed,
                    signals=task.context.signals,
                    regime_signals=task.context.regime_signals,
                    sampling_s=task.context.sampling_s,
                    time_budget_s=task.context.time_budget_s,
                )
                expected_context_sha256 = _sha256(_canonical_json(asdict(expected_context)))
                if (
                    receipt.task_key != task.task_key
                    or receipt.algorithm_version != BASELINE_VERSIONS[algorithm]
                    or receipt.baseline_source_sha256 != manifest.baseline_source_sha256[algorithm]
                    or receipt.harness_sha256 != manifest.harness_sha256
                    or receipt.image_sha256 != manifest.image_sha256
                    or receipt.profile_sha256 != task.profile_sha256
                    or receipt.train_sha256 != task.train_sha256
                    or receipt.evaluation_sha256 != task.evaluation_sha256
                    or receipt.labels_sha256 != task.labels_sha256
                    or receipt.semantics_sha256 != task.semantics_sha256
                    or receipt.fit_context_sha256 != expected_context_sha256
                    or not math.isfinite(receipt.raw_task_score)
                    or not 0 <= receipt.raw_task_score <= 1
                    or not _SHA256.fullmatch(receipt.fit_context_sha256)
                    or not _SHA256.fullmatch(receipt.fit_artifact_sha256)
                    or not _SHA256.fullmatch(receipt.score_document_sha256)
                    or not all(
                        math.isfinite(value) and value >= 0
                        for value in (receipt.fit_seconds, receipt.score_seconds)
                    )
                    or not _is_valid_alarm_policy(receipt.policy)
                    or not all(math.isfinite(value) for value in receipt.auxiliary_metrics.values())
                ):
                    raise ValueError("CARE calibration receipt identity or value is invalid")
                per_algorithm[algorithm][seed] = receipt.raw_task_score
                canonical_receipts.append(receipt)
        references = freeze_task_references(per_algorithm, family=task.family)
        task_results.append(
            CareTaskCalibration(
                task_id=task.task_id,
                task_key=task.task_key,
                family=task.family,
                task_weight=CARE_TASK_WEIGHT,
                base_score=references.base_score,
                reference_score=references.reference_score,
                references=references,
            )
        )
    canonical_receipts.sort(key=lambda row: (row.task_id, row.algorithm, row.seed))
    document = {
        "schema": "care-farm-b-baseline-calibration.v1",
        "calibration_id": manifest.calibration_id,
        "suite_id": manifest.suite_id,
        "suite_version": manifest.suite_version,
        "suite_manifest_sha256": manifest.suite_manifest_sha256,
        "development_manifest_sha256": manifest.development_manifest_sha256,
        "epsilon": manifest.epsilon,
        "manifest_sha256": manifest.manifest_sha256,
        "task_weight_policy": manifest.task_weight_policy,
        "task_weight_policy_sha256": manifest.task_weight_policy_sha256,
        "tasks": [
            {
                "task_id": row.task_id,
                "task_key": row.task_key,
                "family": row.family,
                "task_weight": row.task_weight,
                "base_score": row.base_score,
                "reference_score": row.reference_score,
                "reference_summary_version": row.references.summary_version,
                "algorithm_seed_scores": {
                    name: list(row.references.seed_scores[name])
                    for name in CARE_BASELINE_ALGORITHMS
                },
            }
            for row in task_results
        ],
        "receipts": [row.canonical() for row in canonical_receipts],
    }
    return FrozenCareBaselineCalibration(
        calibration_id=manifest.calibration_id,
        suite_id=manifest.suite_id,
        suite_version=manifest.suite_version,
        suite_manifest_sha256=manifest.suite_manifest_sha256,
        development_manifest_sha256=manifest.development_manifest_sha256,
        epsilon=manifest.epsilon,
        task_weight_policy=manifest.task_weight_policy,
        manifest_sha256=manifest.manifest_sha256,
        calibration_sha256=_sha256(_canonical_json(document)),
        task_count=len(task_results),
        cell_count=len(canonical_receipts),
        tasks=tuple(task_results),
        receipts=tuple(canonical_receipts),
    )


def provision_care_calibration(
    engine: Engine,
    manifest: CareCalibrationManifest,
    tasks: Sequence[CareCalibrationTask],
) -> datetime:
    """Provision immutable input identity as Migrator before any cell dispatch.

    Repeating an exact provision request returns its original absolute deadline;
    it never gives a resumed job a fresh wall allowance. Database grants and
    insert guards restrict this operation to the Migrator role.
    """
    try:
        calibration_id = UUID(manifest.calibration_id)
    except (ValueError, AttributeError) as exc:
        raise ValueError("CARE calibration_id must be a UUID") from exc
    expected_manifest = build_care_calibration_manifest(
        calibration_id=manifest.calibration_id,
        suite_id=manifest.suite_id,
        suite_version=manifest.suite_version,
        suite_manifest_sha256=manifest.suite_manifest_sha256,
        development_manifest_sha256=manifest.development_manifest_sha256,
        epsilon=manifest.epsilon,
        tasks=tasks,
        harness_sha256=manifest.harness_sha256,
        image_sha256=manifest.image_sha256,
        wall_limit_seconds=manifest.wall_limit_seconds,
    )
    if expected_manifest != manifest:
        raise ValueError("CARE calibration inputs differ from the immutable manifest")
    task_by_key = {task.task_key: task for task in tasks}
    artifact_root = PROJECT_ROOT / DEFAULT_HOLDOUT_INPUT_ROOT
    for task in task_by_key.values():
        for frame, expected_digest in (
            (task.train, task.train_sha256),
            (task.evaluation, task.evaluation_sha256),
        ):
            payload = _frame_bytes(frame, columns=task.context.signals)
            if _sha256(payload) != expected_digest:
                raise ValueError("CARE input frame does not match its registered Arrow digest")
            if _store_holdout_artifact(payload, root=artifact_root) != expected_digest:
                raise ValueError("CARE input artifact store changed the content digest")
    algorithm_sources = {
        name: {
            "version": BASELINE_VERSIONS[name],
            "source_sha256": manifest.baseline_source_sha256[name],
        }
        for name in CARE_BASELINE_ALGORITHMS
    }
    with engine.begin() as connection:
        control_exists = connection.execute(
            text(
                "SELECT 1 FROM scorer.care_baseline_calibration_control "
                "WHERE calibration_id=:calibration_id"
            ),
            {"calibration_id": calibration_id},
        ).scalar_one_or_none()
        if control_exists is None:
            job_exists = connection.execute(
                text(
                    "SELECT 1 FROM scorer.care_baseline_calibration_jobs "
                    "WHERE calibration_id=:calibration_id"
                ),
                {"calibration_id": calibration_id},
            ).scalar_one_or_none()
            if job_exists is None:
                connection.execute(
                    text(
                        "INSERT INTO scorer.care_baseline_calibration_jobs "
                        "(calibration_id,suite_id,suite_version,suite_manifest_sha256,"
                        "calibration_manifest_sha256,development_manifest_sha256,epsilon,"
                        "source_manifest_sha256,source_archive_sha256,task_weight_policy,"
                        "task_weight_policy_sha256,algorithm_sources_json,harness_sha256,image_sha256,"
                        "wall_limit_seconds,deadline_at) VALUES "
                        "(:calibration_id,:suite_id,:suite_version,"
                        ":suite_manifest_sha256,:manifest_sha256,:development_manifest_sha256,:epsilon,"
                        ":source_manifest_sha256,:source_archive_sha256,:task_weight_policy,"
                        ":task_weight_policy_sha256,CAST(:algorithm_sources_json AS jsonb),"
                        ":harness_sha256,:image_sha256,:wall_limit_seconds,"
                        "clock_timestamp() + (:wall_limit_seconds * interval '1 second')) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {
                        "calibration_id": calibration_id,
                        "suite_id": manifest.suite_id,
                        "suite_version": manifest.suite_version,
                        "suite_manifest_sha256": manifest.suite_manifest_sha256,
                        "manifest_sha256": manifest.manifest_sha256,
                        "development_manifest_sha256": manifest.development_manifest_sha256,
                        "epsilon": manifest.epsilon,
                        "source_manifest_sha256": manifest.source_manifest_sha256,
                        "source_archive_sha256": manifest.source_archive_sha256,
                        "task_weight_policy": manifest.task_weight_policy,
                        "task_weight_policy_sha256": manifest.task_weight_policy_sha256,
                        "algorithm_sources_json": _canonical_json(algorithm_sources).decode(
                            "ascii"
                        ),
                        "harness_sha256": manifest.harness_sha256,
                        "image_sha256": manifest.image_sha256,
                        "wall_limit_seconds": manifest.wall_limit_seconds,
                    },
                )
            connection.execute(
                text(
                    "INSERT INTO scorer.care_baseline_calibration_control(calibration_id,state) "
                    "VALUES (:calibration_id,'ready') ON CONFLICT DO NOTHING"
                ),
                {"calibration_id": calibration_id},
            )
        # Acquires the canonical control -> job lock order, including exact
        # replay of an existing job. Never SELECT .. FOR UPDATE on the job first.
        locked_deadline = connection.execute(
            text("SELECT lab.lock_care_calibration_execution(:calibration_id)"),
            {"calibration_id": calibration_id},
        ).scalar_one()
        job = (
            connection.execute(
                text(
                    "SELECT suite_id,suite_version,suite_manifest_sha256,"
                    "calibration_manifest_sha256,development_manifest_sha256,epsilon,"
                    "source_manifest_sha256,source_archive_sha256,task_weight_policy,"
                    "task_weight_policy_sha256,algorithm_sources_json,harness_sha256,image_sha256,"
                    "wall_limit_seconds,deadline_at FROM scorer.care_baseline_calibration_jobs "
                    "WHERE calibration_id=:calibration_id"
                ),
                {"calibration_id": calibration_id},
            )
            .mappings()
            .one_or_none()
        )
        expected_job = (
            manifest.suite_id,
            manifest.suite_version,
            manifest.suite_manifest_sha256,
            manifest.manifest_sha256,
            manifest.development_manifest_sha256,
            manifest.epsilon,
            manifest.source_manifest_sha256,
            manifest.source_archive_sha256,
            manifest.task_weight_policy,
            manifest.task_weight_policy_sha256,
            algorithm_sources,
            manifest.harness_sha256,
            manifest.image_sha256,
            manifest.wall_limit_seconds,
        )
        if (
            job is None
            or tuple(
                job[key]
                for key in (
                    "suite_id",
                    "suite_version",
                    "suite_manifest_sha256",
                    "calibration_manifest_sha256",
                    "development_manifest_sha256",
                    "epsilon",
                    "source_manifest_sha256",
                    "source_archive_sha256",
                    "task_weight_policy",
                    "task_weight_policy_sha256",
                    "algorithm_sources_json",
                    "harness_sha256",
                    "image_sha256",
                    "wall_limit_seconds",
                )
            )
            != expected_job
        ):
            raise ValueError(
                "CARE calibration UUID or suite version is already bound to other identity"
            )
        deadline = job["deadline_at"]
        if deadline != locked_deadline:
            raise ValueError("CARE calibration deadline changed during provisioning")
        for task in task_by_key.values():
            connection.execute(
                text(
                    "INSERT INTO scorer.care_baseline_calibration_tasks "
                    "(calibration_id,task_key,task_id,dataset_id,split_id,session_id,profile_sha256,"
                    "family,task_weight,train_sha256,evaluation_sha256,evaluation_rows,labels_sha256,"
                    "semantics_sha256,source_member_sha256,sliding_window,sampling_s,context_json) "
                    "VALUES (:calibration_id,:task_key,:task_id,:dataset_id,:split_id,:session_id,"
                    ":profile_sha256,:family,:task_weight,:train_sha256,:evaluation_sha256,"
                    ":evaluation_rows,:labels_sha256,:semantics_sha256,:source_member_sha256,"
                    ":sliding_window,:sampling_s,CAST(:context_json AS jsonb)) "
                    "ON CONFLICT DO NOTHING"
                ),
                {
                    "calibration_id": calibration_id,
                    "task_key": task.task_key,
                    "task_id": task.task_id,
                    "dataset_id": task.dataset_id,
                    "split_id": task.split_id,
                    "session_id": task.session_id,
                    "profile_sha256": task.profile_sha256,
                    "family": task.family,
                    "task_weight": CARE_TASK_WEIGHT,
                    "train_sha256": task.train_sha256,
                    "evaluation_sha256": task.evaluation_sha256,
                    "evaluation_rows": len(task.evaluation),
                    "labels_sha256": task.labels_sha256,
                    "semantics_sha256": task.semantics_sha256,
                    "source_member_sha256": task.source_member_sha256,
                    "sliding_window": task.sliding_window,
                    "sampling_s": task.sampling_s,
                    "context_json": _canonical_json(asdict(task.context)).decode("ascii"),
                },
            )
        stored_tasks = (
            connection.execute(
                text(
                    "SELECT task_key,task_id,dataset_id,split_id,session_id,profile_sha256,family,"
                    "task_weight,train_sha256,evaluation_sha256,evaluation_rows,labels_sha256,"
                    "semantics_sha256,source_member_sha256,sliding_window,sampling_s,context_json "
                    "FROM scorer.care_baseline_calibration_tasks "
                    "WHERE calibration_id=:calibration_id"
                ),
                {"calibration_id": calibration_id},
            )
            .mappings()
            .all()
        )
        if len(stored_tasks) != CARE_TASK_COUNT:
            raise ValueError("CARE calibration task provision is incomplete")
        task_rows = {row["task_key"]: row for row in stored_tasks}
        for task in task_by_key.values():
            row = task_rows.get(task.task_key)
            expected = (
                task.task_id,
                task.dataset_id,
                task.split_id,
                task.session_id,
                task.profile_sha256,
                task.family,
                CARE_TASK_WEIGHT,
                task.train_sha256,
                task.evaluation_sha256,
                len(task.evaluation),
                task.labels_sha256,
                task.semantics_sha256,
                task.source_member_sha256,
                task.sliding_window,
                task.sampling_s,
                json.loads(_canonical_json(asdict(task.context))),
            )
            actual = (
                None
                if row is None
                else tuple(
                    row[key]
                    for key in (
                        "task_id",
                        "dataset_id",
                        "split_id",
                        "session_id",
                        "profile_sha256",
                        "family",
                        "task_weight",
                        "train_sha256",
                        "evaluation_sha256",
                        "evaluation_rows",
                        "labels_sha256",
                        "semantics_sha256",
                        "source_member_sha256",
                        "sliding_window",
                        "sampling_s",
                        "context_json",
                    )
                )
            )
            if actual != expected:
                raise ValueError("CARE calibration task key is already bound to other content")
        control = connection.execute(
            text(
                "SELECT state,reserved_wall_seconds FROM "
                "scorer.care_baseline_calibration_control WHERE calibration_id=:calibration_id"
            ),
            {"calibration_id": calibration_id},
        ).first()
        if control is None or control[0] not in {"ready", "running"}:
            raise ValueError("CARE calibration execution is already terminal")
    return cast(datetime, deadline)


def install_care_farm_b_calibration(
    migrator_engine: Engine,
    scorer_engine: Engine,
    suite: object,
    *,
    calibration_id: UUID,
    suite_version: int,
    development_manifest_sha256: str,
    epsilon: float,
    wall_limit_seconds: int = MAX_CARE_CALIBRATION_WALL_SECONDS,
) -> dict[str, object]:
    """Provision a measured job from a trusted, already verified Farm B bundle.

    The caller supplies the typed result of ``load_care_farm_b_holdout``; this
    entrypoint never accepts an archive path or raw file name. Scorer readback
    verifies the preinstalled label/semantics profiles. Migrator then writes only
    digests and feature matrices into the new immutable calibration job.
    """
    from harness.care_holdout import FarmBCareSuite
    from lab.scorer.care_holdout import build_care_baseline_calibration_tasks

    if not isinstance(suite, FarmBCareSuite):
        raise ValueError("CARE calibration installer requires a verified FarmBCareSuite")
    if suite.usage_profile != "noncommercial_research" or any(
        task.usage_profile != "noncommercial_research" for task in suite.tasks
    ):
        raise ValueError("CARE calibration is restricted to noncommercial research inputs")
    profile_hashes: dict[str, str] = {}
    with scorer_engine.connect() as connection:
        for task in suite.tasks:
            identity = {
                "dataset_id": task.dataset_id,
                "split_id": task.split_id,
                "session_id": task.session_id,
            }
            profile = (
                connection.execute(
                    text(
                        "SELECT sample_count,profile_sha256,task_family,visibility "
                        "FROM scorer.dataset_profiles WHERE dataset_id=:dataset_id "
                        "AND split_id=:split_id AND session_id=:session_id"
                    ),
                    identity,
                )
                .mappings()
                .one_or_none()
            )
            label_count = connection.execute(
                text(
                    "SELECT count(*) FROM scorer.dataset_labels WHERE dataset_id=:dataset_id "
                    "AND split_id=:split_id AND session_id=:session_id"
                ),
                identity,
            ).scalar_one()
            semantics = (
                connection.execute(
                    text(
                        "SELECT task_family,sampling_s,semantics_sha256 "
                        "FROM scorer.dataset_task_semantics WHERE dataset_id=:dataset_id "
                        "AND split_id=:split_id AND session_id=:session_id"
                    ),
                    identity,
                )
                .mappings()
                .one_or_none()
            )
            if (
                profile is None
                or profile["visibility"] != "holdout"
                or profile["task_family"] != task.family
                or profile["sample_count"] != len(task.evaluation_values)
                or label_count != len(task.evaluation_values)
                or semantics is None
                or semantics["task_family"] != task.family
                or semantics["sampling_s"] != task.sampling_period_s
                or semantics["semantics_sha256"] != task.semantics_sha256
            ):
                raise ValueError("CARE Farm B profile readback differs from verified source bundle")
            profile_hashes[task.task_id] = str(profile["profile_sha256"])
    tasks = build_care_baseline_calibration_tasks(suite, profile_sha256_by_task=profile_hashes)
    harness_digest = compute_harness_hash(PROJECT_ROOT).sha256
    image_digest = DEFAULT_SANDBOX_IMAGE.rsplit("@sha256:", 1)[-1].removeprefix("sha256:")
    manifest = build_care_calibration_manifest(
        calibration_id=str(calibration_id),
        suite_id="care-farm-b-measured",
        suite_version=suite_version,
        suite_manifest_sha256=suite.manifest_sha256,
        development_manifest_sha256=development_manifest_sha256,
        epsilon=epsilon,
        tasks=tasks,
        harness_sha256=harness_digest,
        image_sha256=image_digest,
        wall_limit_seconds=wall_limit_seconds,
    )
    deadline = provision_care_calibration(migrator_engine, manifest, tasks)
    return {
        "calibration_id": str(calibration_id),
        "state": "ready",
        "task_count": CARE_TASK_COUNT,
        "expected_cells": CARE_CELL_COUNT,
        "manifest_sha256": manifest.manifest_sha256,
        "deadline_at": deadline.isoformat(),
    }


def load_care_calibration_inputs(
    engine: Engine, calibration_id: UUID
) -> tuple[CareCalibrationManifest, tuple[CareCalibrationTask, ...]]:
    """Rehydrate only hash-verified feature blobs and trusted DB-bound metadata."""
    with engine.connect() as connection:
        job = (
            connection.execute(
                text(
                    "SELECT suite_id,suite_version,suite_manifest_sha256,"
                    "calibration_manifest_sha256,development_manifest_sha256,epsilon,"
                    "task_weight_policy_sha256,source_manifest_sha256,source_archive_sha256,"
                    "harness_sha256,image_sha256,wall_limit_seconds,algorithm_sources_json "
                    "FROM scorer.care_baseline_calibration_jobs WHERE calibration_id=:id"
                ),
                {"id": calibration_id},
            )
            .mappings()
            .one_or_none()
        )
        rows = (
            connection.execute(
                text(
                    "SELECT task_id,dataset_id,split_id,session_id,task_key,profile_sha256,family,"
                    "train_sha256,evaluation_sha256,labels_sha256,semantics_sha256,"
                    "source_member_sha256,sliding_window,sampling_s,context_json "
                    "FROM scorer.care_baseline_calibration_tasks WHERE calibration_id=:id "
                    "ORDER BY task_key"
                ),
                {"id": calibration_id},
            )
            .mappings()
            .all()
        )
    if job is None or len(rows) != CARE_TASK_COUNT:
        raise ValueError("CARE calibration job or its exact 15-task input is missing")
    artifact_root = PROJECT_ROOT / DEFAULT_HOLDOUT_INPUT_ROOT
    tasks: list[CareCalibrationTask] = []
    for row in rows:
        context_json = row["context_json"]
        if not isinstance(context_json, Mapping):
            raise ValueError("CARE task FitContext receipt is malformed")
        seed = context_json.get("seed")
        signals = context_json.get("signals")
        regime_signals = context_json.get("regime_signals")
        sampling = context_json.get("sampling_s")
        time_budget = context_json.get("time_budget_s")
        if (
            isinstance(seed, bool)
            or not isinstance(seed, int)
            or not isinstance(signals, list)
            or not all(isinstance(item, str) for item in signals)
            or not isinstance(regime_signals, list)
            or not all(isinstance(item, str) for item in regime_signals)
            or isinstance(sampling, bool)
            or not isinstance(sampling, int)
            or isinstance(time_budget, bool)
            or not isinstance(time_budget, (int, float))
        ):
            raise ValueError("CARE task FitContext receipt has invalid field types")
        context = FitContext(
            seed=seed,
            signals=tuple(signals),
            regime_signals=tuple(regime_signals),
            sampling_s=sampling,
            time_budget_s=float(time_budget),
        )
        train = _read_arrow_frame(row["train_sha256"], root=artifact_root)
        evaluation = _read_arrow_frame(row["evaluation_sha256"], root=artifact_root)
        tasks.append(
            CareCalibrationTask(
                task_id=row["task_id"],
                dataset_id=row["dataset_id"],
                split_id=row["split_id"],
                session_id=row["session_id"],
                task_key=row["task_key"],
                profile_sha256=row["profile_sha256"],
                family=row["family"],
                train_sha256=row["train_sha256"],
                evaluation_sha256=row["evaluation_sha256"],
                labels_sha256=row["labels_sha256"],
                semantics_sha256=row["semantics_sha256"],
                source_member_sha256=row["source_member_sha256"],
                source_archive_sha256=job["source_archive_sha256"],
                source_manifest_sha256=job["source_manifest_sha256"],
                sliding_window=row["sliding_window"],
                sampling_s=row["sampling_s"],
                context=context,
                train=train,
                evaluation=evaluation,
            )
        )
    manifest = build_care_calibration_manifest(
        calibration_id=str(calibration_id),
        suite_id=job["suite_id"],
        suite_version=job["suite_version"],
        suite_manifest_sha256=job["suite_manifest_sha256"],
        development_manifest_sha256=job["development_manifest_sha256"],
        epsilon=float(job["epsilon"]),
        tasks=tasks,
        harness_sha256=job["harness_sha256"],
        image_sha256=job["image_sha256"],
        wall_limit_seconds=job["wall_limit_seconds"],
    )
    if (
        manifest.manifest_sha256 != job["calibration_manifest_sha256"]
        or manifest.task_weight_policy_sha256 != job["task_weight_policy_sha256"]
    ):
        raise ValueError(
            "CARE calibration input reconstruction differs from its immutable manifest"
        )
    try:
        stored_sources = job["algorithm_sources_json"]
        if isinstance(stored_sources, str):
            stored_sources = json.loads(stored_sources)
    except json.JSONDecodeError as exc:
        raise ValueError("CARE baseline source manifest is malformed") from exc
    expected_sources = {
        name: {
            "version": BASELINE_VERSIONS[name],
            "source_sha256": manifest.baseline_source_sha256[name],
        }
        for name in CARE_BASELINE_ALGORITHMS
    }
    if stored_sources != expected_sources:
        raise ValueError("CARE baseline source identity differs from its pinned algorithms")
    return manifest, tuple(tasks)


def run_care_baseline_claim(
    engine: Engine,
    runner: LocalDockerRunner,
    *,
    calibration_id: UUID,
    claim_id: UUID,
    generation: int,
    invocation_id: str,
    deadline_monotonic: float,
) -> CareBaselineCellReceipt:
    """Execute one already-bound generation and atomically record its receipt."""
    if isinstance(generation, bool) or generation < 1:
        raise ValueError("CARE worker generation is malformed")
    if re.fullmatch(r"[0-9a-f]{32}", invocation_id) is None:
        raise ValueError("CARE worker invocation identity is malformed")
    if not math.isfinite(deadline_monotonic):
        raise ValueError("CARE worker monotonic deadline is malformed")
    with engine.connect() as connection:
        claim = (
            connection.execute(
                text(
                    "SELECT calibration_id,task_key,algorithm,seed,generation,state,"
                    "deadline_at,worker_invocation_id,worker_unit,worker_cgroup,"
                    "floor(extract(epoch FROM (deadline_at-clock_timestamp())))::integer "
                    "AS claim_seconds_left "
                    "FROM scorer.care_baseline_calibration_claims WHERE claim_id=:claim_id"
                ),
                {"claim_id": claim_id},
            )
            .mappings()
            .one_or_none()
        )
        current = (
            connection.execute(
                text(
                    "SELECT control.state AS control_state,"
                    "floor(extract(epoch FROM (j.deadline_at-clock_timestamp())))::integer "
                    "AS job_seconds_left "
                    "FROM scorer.care_baseline_calibration_control control "
                    "JOIN scorer.care_baseline_calibration_jobs j USING(calibration_id) "
                    "WHERE calibration_id=:calibration_id"
                ),
                {"calibration_id": calibration_id},
            )
            .mappings()
            .one_or_none()
        )
    if (
        claim is None
        or current is None
        or claim["calibration_id"] != calibration_id
        or claim["generation"] != generation
        or claim["state"] != "running"
        or claim["worker_invocation_id"] != invocation_id
        or claim["worker_unit"] != f"swapp-ai-scientist-scorer-{claim_id.hex}.service"
        or current["control_state"] != "running"
    ):
        raise ValueError("CARE worker does not own the exact active claim generation")
    claim_seconds_left = claim["claim_seconds_left"]
    job_seconds_left = current["job_seconds_left"]
    if (
        isinstance(claim_seconds_left, bool)
        or not isinstance(claim_seconds_left, int)
        or isinstance(job_seconds_left, bool)
        or not isinstance(job_seconds_left, int)
    ):
        raise ValueError("CARE worker durable budget read is malformed")
    seconds_left = min(
        CARE_CELL_RESERVATION_SECONDS,
        claim_seconds_left - 1,
        job_seconds_left - 1,
        math.floor(deadline_monotonic - time.monotonic()) - 1,
    )
    if seconds_left < 1:
        raise TimeoutError("CARE claim or job wall deadline is exhausted")
    manifest, tasks = load_care_calibration_inputs(engine, calibration_id)
    task_by_key = {task.task_key: task for task in tasks}
    task = task_by_key.get(claim["task_key"])
    if task is None:
        raise ValueError("CARE claim refers to an unregistered task")
    with engine.connect() as connection:
        deadline_row = (
            connection.execute(
                text(
                    "SELECT floor(extract(epoch FROM "
                    "(claim.deadline_at-clock_timestamp())))::integer "
                    "AS claim_seconds_left,"
                    "floor(extract(epoch FROM (job.deadline_at-clock_timestamp())))::integer "
                    "AS job_seconds_left FROM scorer.care_baseline_calibration_claims claim "
                    "JOIN scorer.care_baseline_calibration_jobs job USING(calibration_id) "
                    "WHERE claim.claim_id=:claim_id AND claim.calibration_id=:calibration_id"
                ),
                {"claim_id": claim_id, "calibration_id": calibration_id},
            )
            .mappings()
            .one_or_none()
        )
    if deadline_row is None:
        raise ValueError("CARE worker claim disappeared while loading its inputs")
    db_claim_left = deadline_row["claim_seconds_left"]
    db_job_left = deadline_row["job_seconds_left"]
    if (
        isinstance(db_claim_left, bool)
        or not isinstance(db_claim_left, int)
        or isinstance(db_job_left, bool)
        or not isinstance(db_job_left, int)
    ):
        raise ValueError("CARE worker durable budget read is malformed")
    seconds_left = min(
        seconds_left,
        db_claim_left - 1,
        db_job_left - 1,
        math.floor(deadline_monotonic - time.monotonic()) - 1,
    )
    if seconds_left < 1:
        raise TimeoutError("CARE claim or job deadline expired while loading inputs")

    def admission_check() -> None:
        with engine.connect() as connection:
            state = (
                connection.execute(
                    text(
                        "SELECT control.state,claim.state AS claim_state,claim.generation,"
                        "claim.worker_invocation_id,"
                        "floor(extract(epoch FROM (claim.deadline_at-clock_timestamp())))::integer "
                        "AS claim_seconds_left,"
                        "floor(extract(epoch FROM (job.deadline_at-clock_timestamp())))::integer "
                        "AS job_seconds_left FROM "
                        "scorer.care_baseline_calibration_control control "
                        "JOIN scorer.care_baseline_calibration_claims claim USING(calibration_id) "
                        "JOIN scorer.care_baseline_calibration_jobs job USING(calibration_id) "
                        "WHERE control.calibration_id=:calibration_id AND claim.claim_id=:claim_id"
                    ),
                    {"calibration_id": calibration_id, "claim_id": claim_id},
                )
                .mappings()
                .one_or_none()
            )
        if (
            state is None
            or state["state"] != "running"
            or state["claim_state"] != "running"
            or state["generation"] != generation
            or state["worker_invocation_id"] != invocation_id
            or not isinstance(state["claim_seconds_left"], int)
            or not isinstance(state["job_seconds_left"], int)
            or state["claim_seconds_left"] < 1
            or state["job_seconds_left"] < 1
        ):
            raise RuntimeError("CARE worker generation lost admission before sandbox execution")

    receipt = score_care_baseline_cell(
        engine,
        runner,
        task=task,
        algorithm=claim["algorithm"],
        seed=claim["seed"],
        harness_sha256=manifest.harness_sha256,
        image_sha256=manifest.image_sha256,
        remaining_seconds=seconds_left,
        admission_check=admission_check,
    )
    receipt = replace(
        receipt,
        claim_id=claim_id,
        claim_generation=generation,
        worker_invocation_id=invocation_id,
    )
    persist_care_baseline_cell(engine, str(calibration_id), receipt)
    return receipt


def persist_care_baseline_cell(
    engine: Engine,
    calibration_id: str,
    receipt: CareBaselineCellReceipt,
) -> None:
    """Atomically persist the measurement and complete its exact claim generation."""
    try:
        parsed_id = UUID(calibration_id)
    except (ValueError, AttributeError) as exc:
        raise ValueError("CARE calibration_id must be a UUID") from exc
    if (
        receipt.claim_id is None
        or isinstance(receipt.claim_generation, bool)
        or not isinstance(receipt.claim_generation, int)
        or receipt.claim_generation < 1
        or not isinstance(receipt.worker_invocation_id, str)
        or re.fullmatch(r"[0-9a-f]{32}", receipt.worker_invocation_id) is None
    ):
        raise ValueError("CARE receipt lacks an exact worker claim identity")
    payload = {
        "baseline_source_sha256": receipt.baseline_source_sha256,
        "harness_sha256": receipt.harness_sha256,
        "image_sha256": receipt.image_sha256,
        "profile_sha256": receipt.profile_sha256,
        "train_sha256": receipt.train_sha256,
        "evaluation_sha256": receipt.evaluation_sha256,
        "labels_sha256": receipt.labels_sha256,
        "semantics_sha256": receipt.semantics_sha256,
        "fit_context_sha256": receipt.fit_context_sha256,
        "fit_artifact_sha256": receipt.fit_artifact_sha256,
        "score_document_sha256": receipt.score_document_sha256,
        "calibration_id": str(parsed_id),
        "policy_json": dict(receipt.policy),
        "raw_task_score": receipt.raw_task_score,
        "auxiliary_metrics_json": dict(receipt.auxiliary_metrics),
        "fit_seconds": receipt.fit_seconds,
        "score_seconds": receipt.score_seconds,
    }
    with engine.begin() as connection:
        raw = connection.execute(
            text(
                "SELECT lab.complete_care_baseline_cell(:claim_id,:generation,"
                ":invocation_id,CAST(:receipt AS jsonb))"
            ),
            {
                "claim_id": receipt.claim_id,
                "generation": receipt.claim_generation,
                "invocation_id": receipt.worker_invocation_id,
                "receipt": _canonical_json(payload).decode("ascii"),
            },
        ).scalar_one()
    result = _rpc_json_object(raw, label="CARE receipt completion")
    if (
        str(result.get("claim_id")) != str(receipt.claim_id)
        or result.get("generation") != receipt.claim_generation
        or result.get("state") != "succeeded"
    ):
        raise ValueError("Scorer returned a mismatched CARE completion receipt")


def _persist_frozen_care_calibration(
    engine: Engine,
    frozen: FrozenCareBaselineCalibration,
) -> None:
    """Atomically seal exact 135-cell receipts and publish summaries/control state."""
    try:
        parsed_id = UUID(frozen.calibration_id)
    except (ValueError, AttributeError) as exc:
        raise ValueError("CARE calibration_id must be a UUID") from exc
    summaries = [
        {
            "task_key": task.task_key,
            "base_score": task.base_score,
            "reference_score": task.reference_score,
            "task_weight": task.task_weight,
        }
        for task in frozen.tasks
    ]
    with engine.begin() as connection:
        raw = connection.execute(
            text(
                "SELECT lab.finalize_care_baseline_calibration(:calibration_id,:manifest_sha256,"
                ":calibration_sha256,CAST(:summaries AS jsonb))"
            ),
            {
                "calibration_id": parsed_id,
                "manifest_sha256": frozen.manifest_sha256,
                "calibration_sha256": frozen.calibration_sha256,
                "summaries": _canonical_json(summaries).decode("ascii"),
            },
        ).scalar_one()
    result = _rpc_json_object(raw, label="CARE calibration finalization")
    if result.get("state") != "complete" or result.get("cell_count") != CARE_CELL_COUNT:
        raise ValueError("Scorer returned an incomplete CARE calibration freeze")


def finalize_care_calibration(
    engine: Engine,
    manifest: CareCalibrationManifest,
    tasks: Sequence[CareCalibrationTask],
) -> FrozenCareBaselineCalibration:
    """Reduce authoritative private receipts and persist the only accepted freeze."""
    try:
        calibration_id = UUID(manifest.calibration_id)
    except (ValueError, AttributeError) as exc:
        raise ValueError("CARE calibration_id must be a UUID") from exc
    task_by_key = {task.task_key: task for task in tasks}
    with engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT c.task_key,c.algorithm,c.seed,c.claim_id,c.claim_generation,"
                    "claim.worker_invocation_id,"
                    "baseline_source_sha256,harness_sha256,"
                    "image_sha256,"
                    "profile_sha256,train_sha256,evaluation_sha256,labels_sha256,semantics_sha256,"
                    "fit_context_sha256,fit_artifact_sha256,score_document_sha256,policy_json,"
                    "raw_task_score,auxiliary_metrics_json,fit_seconds,score_seconds "
                    "FROM scorer.care_baseline_calibration_cells c "
                    "JOIN scorer.care_baseline_calibration_claims claim "
                    "ON claim.claim_id=c.claim_id AND claim.generation=c.claim_generation "
                    "AND claim.calibration_id=c.calibration_id AND claim.task_key=c.task_key "
                    "AND claim.algorithm=c.algorithm AND claim.seed=c.seed "
                    "WHERE c.calibration_id=:calibration_id AND claim.state='succeeded' "
                    "ORDER BY c.task_key,c.algorithm,c.seed"
                ),
                {"calibration_id": calibration_id},
            )
            .mappings()
            .all()
        )
    if any(row["task_key"] not in task_by_key for row in rows):
        raise ValueError("CARE calibration contains receipts for a foreign task")
    receipts = [
        CareBaselineCellReceipt(
            task_id=task_by_key[row["task_key"]].task_id,
            task_key=row["task_key"],
            algorithm=row["algorithm"],
            algorithm_version=BASELINE_VERSIONS[row["algorithm"]],
            seed=row["seed"],
            claim_id=row["claim_id"],
            claim_generation=row["claim_generation"],
            worker_invocation_id=row["worker_invocation_id"],
            baseline_source_sha256=row["baseline_source_sha256"],
            harness_sha256=row["harness_sha256"],
            image_sha256=row["image_sha256"],
            profile_sha256=row["profile_sha256"],
            train_sha256=row["train_sha256"],
            evaluation_sha256=row["evaluation_sha256"],
            labels_sha256=row["labels_sha256"],
            semantics_sha256=row["semantics_sha256"],
            fit_context_sha256=row["fit_context_sha256"],
            fit_artifact_sha256=row["fit_artifact_sha256"],
            score_document_sha256=row["score_document_sha256"],
            policy=dict(row["policy_json"]),
            raw_task_score=float(row["raw_task_score"]),
            auxiliary_metrics={
                str(name): float(value)
                for name, value in dict(row["auxiliary_metrics_json"]).items()
            },
            fit_seconds=float(row["fit_seconds"]),
            score_seconds=float(row["score_seconds"]),
        )
        for row in rows
    ]
    frozen = freeze_care_baseline_calibration(manifest, tasks, receipts)
    _persist_frozen_care_calibration(engine, frozen)
    return frozen


def run_care_calibration(
    engine: Engine,
    calibration_id: UUID,
    *,
    remaining_seconds: int = MAX_CARE_CALIBRATION_WALL_SECONDS,
) -> dict[str, object]:
    """Resume the fixed 135-cell grid and return status without private metrics."""
    if (
        isinstance(remaining_seconds, bool)
        or not isinstance(remaining_seconds, int)
        or not 1 <= remaining_seconds <= MAX_CARE_CALIBRATION_WALL_SECONDS
    ):
        raise ValueError("CARE calibration driver budget must be within the four-hour cap")
    driver_deadline = time.monotonic() + remaining_seconds
    manifest, tasks = load_care_calibration_inputs(engine, calibration_id)
    from lab.scorer.care_calibration_supervisor import (
        reconcile_care_baseline_claim,
        run_care_baseline_cell_process,
    )

    task_by_key = {task.task_key: task for task in tasks}
    completed = _care_completed_cell_count(engine, calibration_id)
    for task in tasks:
        if task.task_key not in task_by_key:
            raise ValueError("CARE calibration task key disappeared during grid construction")
        for algorithm in CARE_BASELINE_ALGORITHMS:
            for seed in BASELINE_SEEDS:
                existing_claim = read_care_baseline_cell_claim(
                    engine,
                    calibration_id,
                    task_key=task.task_key,
                    algorithm=algorithm,
                    seed=seed,
                )
                left = math.floor(driver_deadline - time.monotonic())
                if existing_claim is None and left < CARE_CELL_RESERVATION_SECONDS + 10:
                    return {
                        "calibration_id": str(calibration_id),
                        "state": "pending",
                        "completed_cells": completed,
                        "expected_cells": CARE_CELL_COUNT,
                    }
                claim = (
                    existing_claim
                    if existing_claim is not None
                    else reserve_care_baseline_cell(
                        engine,
                        str(calibration_id),
                        task_key=task.task_key,
                        algorithm=algorithm,
                        seed=seed,
                    )
                )
                if claim.state == "incomplete":
                    return {
                        "calibration_id": str(calibration_id),
                        "state": "incomplete",
                        "completed_cells": completed,
                        "expected_cells": CARE_CELL_COUNT,
                    }
                if claim.claim_id is None or claim.generation is None:
                    raise ValueError("CARE reservation returned no durable claim identity")
                if claim.newly_created:
                    if claim.deadline_at is None:
                        raise ValueError("new CARE reservation has no durable deadline")
                    left = min(
                        math.floor(driver_deadline - time.monotonic()),
                        math.floor((claim.deadline_at - datetime.now(UTC)).total_seconds()),
                    )
                    if left < 1:
                        return {
                            "calibration_id": str(calibration_id),
                            "state": "pending",
                            "completed_cells": completed,
                            "expected_cells": CARE_CELL_COUNT,
                        }
                    claim_result = run_care_baseline_cell_process(
                        engine,
                        calibration_id=calibration_id,
                        claim_id=claim.claim_id,
                        generation=claim.generation,
                        remaining_seconds=min(CARE_CELL_RESERVATION_SECONDS, left),
                    )
                else:
                    claim_result = reconcile_care_baseline_claim(
                        engine, claim, claim.claim_id, claim.generation
                    )
                if claim_result.state == "succeeded":
                    completed = _care_completed_cell_count(engine, calibration_id)
                    continue
                return {
                    "calibration_id": str(calibration_id),
                    "state": "incomplete" if claim_result.state == "failed" else "pending",
                    "completed_cells": completed,
                    "expected_cells": CARE_CELL_COUNT,
                }
    registration = register_measured_care_farm_b_suite(engine, manifest, tasks)
    return {
        "calibration_id": str(calibration_id),
        "state": "complete",
        "completed_cells": CARE_CELL_COUNT,
        "expected_cells": CARE_CELL_COUNT,
        "suite_id": str(registration["suite_id"]),
        "suite_version": int(registration["suite_version"]),
    }


def _care_completed_cell_count(engine: Engine, calibration_id: UUID) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                text(
                    "SELECT count(*) FROM scorer.care_baseline_calibration_cells cell "
                    "JOIN scorer.care_baseline_calibration_claims claim "
                    "ON claim.claim_id=cell.claim_id AND claim.generation=cell.claim_generation "
                    "AND claim.calibration_id=cell.calibration_id AND claim.task_key=cell.task_key "
                    "AND claim.algorithm=cell.algorithm AND claim.seed=cell.seed "
                    "WHERE cell.calibration_id=:calibration_id AND claim.state='succeeded'"
                ),
                {"calibration_id": calibration_id},
            ).scalar_one()
        )


def register_measured_care_farm_b_suite(
    engine: Engine,
    manifest: CareCalibrationManifest,
    tasks: Sequence[CareCalibrationTask],
) -> dict[str, int | str]:
    """Bind a completed measured freeze to a fresh immutable holdout version."""
    from lab.scorer.holdout import HoldoutTaskSpec, register_holdout_suite

    frozen = finalize_care_calibration(engine, manifest, tasks)
    frozen_by_task = {task.task_id: task for task in frozen.tasks}
    specs = []
    for task in tasks:
        measured = frozen_by_task.get(task.task_id)
        if (
            measured is None
            or measured.task_key
            != hashlib.sha256(
                f"{manifest.suite_manifest_sha256}:{task.task_id}".encode()
            ).hexdigest()[:32]
            or measured.task_key != task.task_key
        ):
            raise ValueError("measured Farm B freeze does not cover the source suite task")
        specs.append(
            HoldoutTaskSpec(
                task_id=task.task_id,
                dataset_id=task.dataset_id,
                split_id=task.split_id,
                session_id=task.session_id,
                profile_sha256=task.profile_sha256,
                family=task.family,
                task_weight=CARE_TASK_WEIGHT,
                base_score=measured.base_score,
                reference_score=measured.reference_score,
                sliding_window=task.sliding_window,
                context=task.context,
                train=task.train,
                evaluation=task.evaluation,
            )
        )
    return register_holdout_suite(
        engine,
        suite_id=manifest.suite_id,
        suite_version=manifest.suite_version,
        manifest_sha256=manifest.suite_manifest_sha256,
        development_manifest_sha256=manifest.development_manifest_sha256,
        epsilon=manifest.epsilon,
        care_calibration_id=UUID(manifest.calibration_id),
        tasks=specs,
    )


def bind_measured_care_development_suite(
    engine: Engine,
    calibration_id: UUID,
    *,
    development_manifest_path: Path,
) -> dict[str, int | str]:
    """Bind the same frozen private measurements to the exact public development suite.

    This Scorer-only operation copies no measurements and changes no calibration
    deadline, claim, generation, or quota. Public features are verified against
    the premeasurement development digest; Farm B identities remain private.
    """
    from lab.director.suite_manifest import SuiteManifest
    from lab.scorer.holdout import HoldoutTaskSpec, register_holdout_suite
    from lab.suite_limits import MAX_SUITE_MANIFEST_BYTES

    path = Path(development_manifest_path)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("development manifest must be a private regular file")
    if path.stat().st_size > MAX_SUITE_MANIFEST_BYTES:
        raise ValueError("development manifest exceeds its fixed bound")
    payload = path.read_bytes()
    development = SuiteManifest.model_validate_json(payload, strict=True)
    manifest, tasks = load_care_calibration_inputs(engine, calibration_id)
    if (
        _sha256(payload) != manifest.development_manifest_sha256
        or development.suite_id != "public-ad-v1"
        or development.suite_version != 3
        or len(development.tasks) != 27
        or sum(task.dataset_id == "CARE-A" for task in development.tasks) != 22
        or any(
            task.provenance.usage_profile != "noncommercial_research" for task in development.tasks
        )
        or any(
            not task.provenance.license_id or not task.provenance.attribution
            for task in development.tasks
        )
    ):
        raise ValueError("public development identity/provenance differs from measured calibration")
    public_ids = {(task.dataset_id, task.split_id, task.session_id) for task in development.tasks}
    private_ids = {(task.dataset_id, task.split_id, task.session_id) for task in tasks}
    if not public_ids.isdisjoint(private_ids):
        raise ValueError("development tasks overlap the frozen private holdout")
    frozen = finalize_care_calibration(engine, manifest, tasks)
    measured = {task.task_id: task for task in frozen.tasks}
    specs = tuple(
        HoldoutTaskSpec(
            task_id=task.task_id,
            dataset_id=task.dataset_id,
            split_id=task.split_id,
            session_id=task.session_id,
            profile_sha256=task.profile_sha256,
            family=task.family,
            task_weight=CARE_TASK_WEIGHT,
            base_score=measured[task.task_id].base_score,
            reference_score=measured[task.task_id].reference_score,
            sliding_window=task.sliding_window,
            context=task.context,
            train=task.train,
            evaluation=task.evaluation,
        )
        for task in tasks
    )
    return register_holdout_suite(
        engine,
        suite_id=development.suite_id,
        suite_version=development.suite_version,
        manifest_sha256=manifest.suite_manifest_sha256,
        development_manifest_sha256=manifest.development_manifest_sha256,
        epsilon=manifest.epsilon,
        care_calibration_id=calibration_id,
        tasks=specs,
    )
