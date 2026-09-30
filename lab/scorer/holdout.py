"""Scorer-only holdout registration and isolated candidate comparison."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import stat
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.ipc as ipc
from sqlalchemy import Engine, text

from harness.alarm import apply_alarm_policy, nrm_task_score, pdm_task_score
from harness.baselines import normalize_task_score
from harness.contracts import AlarmPolicy, FitContext
from lab.sandbox.docker_runner import DEFAULT_SANDBOX_IMAGE, LocalDockerRunner, SandboxProfile
from lab.sandbox.evaluation import run_candidate_fit_score
from lab.scorer.jobs import (
    DEFAULT_ARTIFACT_ROOT,
    MAX_ARTIFACT_STORE_BYTES,
    MIN_FREE_DISK_BYTES,
    _acquire_blob_quota_lock,
    _blob_path,
    _blob_store_usage,
    _recover_orphan_blob_temps,
    read_artifact_bytes,
)
from lab.scorer.task_semantics import score_masked_vus, validate_evt_semantics

DEFAULT_HOLDOUT_INPUT_ROOT = Path("data/runtime/holdout-inputs")
# Match the shared content-addressed artifact reader/store's enforced per-blob
# limit. The suite aggregate quota remains 2 GiB; inputs are never truncated.
MAX_HOLDOUT_ARTIFACT_BYTES = 32 * 1024**2
MAX_HOLDOUT_SUITE_BYTES = 2 * 1024**3
MAX_HOLDOUT_CONTEXT_BYTES = 64 * 1024
MAX_HOLDOUT_EVALUATION_SECONDS = 600
MAX_HOLDOUT_TASKS = 256
HOLDOUT_THRESHOLD_COUNT = 250
HOLDOUT_MAX_FA_PER_DAY = 1.0
HOLDOUT_MAX_DUTY_FRACTION = 0.05


@dataclass(frozen=True, slots=True)
class HoldoutTaskSpec:
    """Private task binding used by the Scorer, never returned to Director."""

    task_id: str
    dataset_id: str
    split_id: str
    session_id: str
    profile_sha256: str
    family: str
    task_weight: float
    base_score: float
    reference_score: float
    sliding_window: int
    context: FitContext
    train: pd.DataFrame
    evaluation: pd.DataFrame


@dataclass(frozen=True, slots=True)
class HoldoutRecoveryResult:
    """Operational recovery status; never includes private holdout metrics."""

    reservation_id: UUID
    state: str


def _frame_bytes(frame: pd.DataFrame, *, columns: tuple[str, ...]) -> bytes:
    if frame.empty or tuple(str(name) for name in frame.columns) != columns:
        raise ValueError("holdout numeric matrix columns differ from its FitContext")
    if any(
        name.lower() in {"label", "labels", "anomaly", "is_anomaly", "timestamp", "datetime"}
        for name in columns
    ):
        raise ValueError("holdout feature matrix contains a forbidden label or time column")
    values = frame.to_numpy(dtype=np.float64, copy=False)
    if values.ndim != 2 or not np.isfinite(values).all():
        raise ValueError("holdout numeric matrix contains invalid values")
    sink = pa.BufferOutputStream()
    table = pa.Table.from_pandas(frame.reset_index(drop=True), preserve_index=False)
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    payload = sink.getvalue().to_pybytes()
    if len(payload) > MAX_HOLDOUT_ARTIFACT_BYTES:
        raise ValueError("one holdout Arrow input exceeds the shared 32 MiB artifact bound")
    return cast(bytes, payload)


def _semantics_digest(
    *,
    family: str,
    sampling_s: int | None,
    times: object,
    masks: object,
    failures: object,
) -> str:
    value = {
        "schema": "public-task-semantics.v1",
        "task_family": family,
        "sampling_s": sampling_s,
        "evaluation_times": times,
        "masked_samples": masks,
        "failure_windows": failures,
    }
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode("ascii")
    ).hexdigest()


def _validated_failure_windows(values: object, count: int) -> tuple[tuple[int, int], ...]:
    if not isinstance(values, list):
        raise ValueError("holdout failure windows must be a JSON list")
    result: list[tuple[int, int]] = []
    previous_end = -1
    for pair in values:
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) for value in pair)
        ):
            raise ValueError("holdout failure window is malformed")
        start, end = pair
        if start < 0 or end <= start or end >= count or start <= previous_end:
            raise ValueError("holdout failure windows must be ordered, disjoint, and in range")
        result.append((start, end))
        previous_end = end
    return tuple(result)


def _validate_semantics(
    semantics: Any,
    *,
    task_family: str,
    sample_count: int,
    sampling_s: object,
    labels: np.ndarray | None = None,
) -> dict[str, Any]:
    times = semantics["evaluation_times_json"]
    masks = semantics["masked_samples_json"]
    raw_failures = semantics["failure_windows_json"]
    digest = semantics["semantics_sha256"]
    if (
        semantics["task_family"] != task_family
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or not isinstance(masks, list)
        or len(masks) != sample_count
        or any(type(mask) is not bool for mask in masks)
        or not isinstance(times, list)
        or len(times) != sample_count
    ):
        raise ValueError("registered holdout semantics are malformed or misaligned")
    if isinstance(sampling_s, bool) or (
        sampling_s is not None and (not isinstance(sampling_s, int) or sampling_s <= 0)
    ):
        raise ValueError("holdout sampling cadence is invalid")
    if task_family in {"PDM", "NRM"} and sampling_s is None:
        raise ValueError("PDM/NRM holdout metrics require verified sampling seconds")
    if task_family == "EVT":
        validated_masks = validate_evt_semantics(
            sample_count=sample_count,
            sampling_s=sampling_s,
            evaluation_times=times,
            masked_samples=masks,
            failure_windows=raw_failures,
            semantics_sha256=digest,
        )
        if labels is not None and any(
            bool(labels[index]) and validated_masks[index] for index in range(sample_count)
        ):
            raise ValueError("masked EVT holdout points cannot carry positive labels")
        failures = _validated_failure_windows(raw_failures, sample_count)
        return {"times": times, "masks": validated_masks, "failures": failures}
    if all(isinstance(value, str) and value for value in times):
        try:
            parsed_times = [datetime.fromisoformat(value) for value in times]
        except ValueError as exc:
            raise ValueError("holdout time axis is malformed") from exc
        if any(value.tzinfo is not None for value in parsed_times):
            raise ValueError("holdout time axis must use canonical naive timestamps")
        deltas = [
            (right - left).total_seconds()
            for left, right in zip(parsed_times, parsed_times[1:], strict=False)
        ]
    elif all(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        for value in times
    ):
        deltas = [float(right) - float(left) for left, right in zip(times, times[1:], strict=False)]
    else:
        raise ValueError("holdout time axis has unsupported values")
    if any(delta <= 0 for delta in deltas):
        raise ValueError("holdout time axis must be strictly increasing")
    if sampling_s is not None and any(delta != sampling_s for delta in deltas):
        raise ValueError("holdout time axis differs from its verified cadence")
    failures = _validated_failure_windows(raw_failures, sample_count)
    if task_family == "PDM" and not failures:
        raise ValueError("PDM holdout tasks require at least one failure window")
    elif task_family == "NRM" and failures:
        raise ValueError("NRM holdout tasks cannot contain failure windows")
    actual = _semantics_digest(
        family=task_family,
        sampling_s=sampling_s,
        times=times,
        masks=masks,
        failures=raw_failures,
    )
    if actual != digest:
        raise ValueError("holdout task semantics digest differs from its content")
    if labels is not None:
        if task_family == "PDM":
            positive_indices = {int(index) for index in np.flatnonzero(labels)}
            expected = {
                index
                for start, end in failures
                for index in range(start, end + 1)
                if not masks[index]
            }
            if positive_indices != expected:
                raise ValueError("PDM holdout labels differ from failure semantics")
        elif task_family == "NRM" and np.any(labels):
            raise ValueError("NRM holdout labels must all be normal")
    return {"times": times, "masks": masks, "failures": failures}


def _store_holdout_artifact(payload: bytes, *, root: Path) -> str:
    """Store one immutable Arrow blob under a separate 2 GiB private quota."""
    if not payload or len(payload) > MAX_HOLDOUT_ARTIFACT_BYTES:
        raise ValueError("holdout input artifact is empty or exceeds its byte bound")
    if root.is_symlink():
        raise ValueError("holdout artifact root cannot be a symlink")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root_stat = root.stat()
    if root_stat.st_uid != os.getuid() or stat.S_IMODE(root_stat.st_mode) != 0o700:
        raise ValueError("holdout artifact root must be private to the Scorer user")
    digest = hashlib.sha256(payload).hexdigest()
    lock_fd, lock_path = _acquire_blob_quota_lock(root)
    try:
        _recover_orphan_blob_temps(root)
        target = _blob_path(root, digest)
        if target.exists() or target.is_symlink():
            current = read_artifact_bytes(
                digest, artifact_root=root, max_bytes=MAX_HOLDOUT_ARTIFACT_BYTES
            )
            if current != payload:
                raise ValueError("holdout content address contains different bytes")
            return digest
        if shutil.disk_usage(root).free < MIN_FREE_DISK_BYTES + len(payload):
            raise RuntimeError("holdout input write would breach the disk reserve")
        used = _blob_store_usage(root, lock_path)
        if used + len(payload) > min(MAX_HOLDOUT_SUITE_BYTES, MAX_ARTIFACT_STORE_BYTES):
            raise RuntimeError("private holdout input store reached its 2 GiB quota")
        temporary = target.with_name(f".{digest}.{secrets.token_hex(8)}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            directory_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
    finally:
        os.close(lock_fd)
    return digest


def register_holdout_suite(
    engine: Engine,
    *,
    suite_id: str,
    suite_version: int,
    manifest_sha256: str,
    development_manifest_sha256: str,
    epsilon: float,
    care_calibration_id: UUID | None = None,
    tasks: Sequence[HoldoutTaskSpec],
    input_root: Path = DEFAULT_HOLDOUT_INPUT_ROOT,
) -> dict[str, int | str]:
    """Install a complete private suite after holdout profiles/labels exist.

    The ``engine`` must use the Scorer role. ``manifest_sha256`` identifies this
    private holdout bundle; ``development_manifest_sha256`` independently pins the
    label-free manifest accepted by the production run API. The registry keeps both
    hashes so a run cannot substitute a different development suite under the same
    suite/version key. It registers only Arrow feature refs and opaque task keys;
    labels remain in ``scorer.dataset_labels``.
    """
    if not suite_id or len(suite_id) > 128:
        raise ValueError("holdout suite id is invalid")
    if isinstance(suite_version, bool) or not isinstance(suite_version, int) or suite_version < 1:
        raise ValueError("holdout suite version must be positive")
    if re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None:
        raise ValueError("holdout manifest digest must be lowercase SHA-256")
    if re.fullmatch(r"[0-9a-f]{64}", development_manifest_sha256) is None:
        raise ValueError("development suite manifest digest must be lowercase SHA-256")
    if not np.isfinite(epsilon) or not 0 <= epsilon < 1:
        raise ValueError("holdout epsilon must be finite and within [0,1)")
    if suite_id == "care-farm-b-measured" and care_calibration_id is None:
        raise ValueError("measured Farm B suite requires a frozen baseline calibration")
    if suite_id not in {"care-farm-b-measured", "public-ad-v1"} and care_calibration_id is not None:
        raise ValueError(
            "CARE calibration may bind only the measured Farm B suite "
            "or its verified public binding"
        )
    if care_calibration_id is not None and not isinstance(care_calibration_id, UUID):
        raise ValueError("CARE calibration identity must be a UUID")
    if not tasks or len(tasks) > MAX_HOLDOUT_TASKS:
        raise ValueError("holdout suite must contain 1..256 tasks")
    prepared: list[dict[str, Any]] = []
    total_bytes = 0
    for task in tasks:
        if task.family not in {"EVT", "PDM", "NRM"}:
            raise ValueError("holdout task family is unsupported")
        context = task.context
        sampling_s = context.sampling_s
        if isinstance(sampling_s, bool) or (
            sampling_s is not None and (not isinstance(sampling_s, int) or sampling_s <= 0)
        ):
            raise ValueError("holdout sampling seconds must be positive or explicitly unknown")
        if task.family in {"PDM", "NRM"} and sampling_s is None:
            raise ValueError("PDM/NRM holdout metrics require verified positive sampling seconds")
        if not math.isfinite(context.time_budget_s) or context.time_budget_s <= 0:
            raise ValueError("holdout FitContext time budget must be positive and finite")
        if not np.isfinite(task.task_weight) or task.task_weight <= 0:
            raise ValueError("holdout task weight must be finite and positive")
        if (
            not np.isfinite((task.base_score, task.reference_score)).all()
            or not 0 <= task.base_score <= task.reference_score <= 1
        ):
            raise ValueError("holdout task normalization reference is invalid")
        if (
            not isinstance(task.sliding_window, int)
            or isinstance(task.sliding_window, bool)
            or task.sliding_window < 1
        ):
            raise ValueError("holdout sliding window must be a positive integer")
        if re.fullmatch(r"[0-9a-f]{64}", task.profile_sha256) is None:
            raise ValueError("holdout profile digest is invalid")
        columns = tuple(context.signals)
        train_bytes = _frame_bytes(task.train, columns=columns)
        eval_bytes = _frame_bytes(task.evaluation, columns=columns)
        total_bytes += len(train_bytes) + len(eval_bytes)
        if total_bytes > MAX_HOLDOUT_SUITE_BYTES:
            raise ValueError("holdout suite inputs exceed the 2 GiB bound")
        context_json = asdict(context)
        context_bytes = json.dumps(
            context_json, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        if len(context_bytes) > MAX_HOLDOUT_CONTEXT_BYTES:
            raise ValueError("holdout FitContext exceeds its JSON bound")
        # Compare the same JSON representation PostgreSQL returns, including
        # arrays for FitContext tuples, when registration is repeated.
        context_json = json.loads(context_bytes)
        with engine.connect() as connection:
            profile = (
                connection.execute(
                    text(
                        "SELECT sample_count,profile_sha256,task_family,visibility "
                        "FROM scorer.dataset_profiles WHERE dataset_id=:dataset "
                        "AND split_id=:split AND session_id=:session"
                    ),
                    {
                        "dataset": task.dataset_id,
                        "split": task.split_id,
                        "session": task.session_id,
                    },
                )
                .mappings()
                .one_or_none()
            )
            semantics = (
                connection.execute(
                    text(
                        "SELECT task_family,sampling_s,evaluation_times_json,masked_samples_json,"
                        "failure_windows_json,semantics_sha256 "
                        "FROM scorer.dataset_task_semantics WHERE dataset_id=:dataset "
                        "AND split_id=:split AND session_id=:session"
                    ),
                    {
                        "dataset": task.dataset_id,
                        "split": task.split_id,
                        "session": task.session_id,
                    },
                )
                .mappings()
                .one_or_none()
            )
            label_count = connection.execute(
                text(
                    "SELECT count(*) FROM scorer.dataset_labels WHERE dataset_id=:dataset "
                    "AND split_id=:split AND session_id=:session"
                ),
                {
                    "dataset": task.dataset_id,
                    "split": task.split_id,
                    "session": task.session_id,
                },
            ).scalar_one()
        if (
            profile is None
            or profile["visibility"] != "holdout"
            or profile["profile_sha256"] != task.profile_sha256
            or profile["task_family"] != task.family
            or profile["sample_count"] != len(task.evaluation)
            or label_count != len(task.evaluation)
            or semantics is None
            or semantics["task_family"] != task.family
            or semantics["sampling_s"] != sampling_s
        ):
            raise ValueError("holdout profile, labels, and semantics do not align")
        _validate_semantics(
            semantics,
            task_family=task.family,
            sample_count=len(task.evaluation),
            sampling_s=sampling_s,
        )
        task_key = hashlib.sha256(f"{manifest_sha256}:{task.task_id}".encode()).hexdigest()[:32]
        prepared.append(
            {
                "task_key": task_key,
                "dataset_id": task.dataset_id,
                "split_id": task.split_id,
                "session_id": task.session_id,
                "profile_sha256": task.profile_sha256,
                "family": task.family,
                "task_weight": float(task.task_weight),
                "base_score": float(task.base_score),
                "reference_score": float(task.reference_score),
                "sliding_window": task.sliding_window,
                "sampling_s": sampling_s,
                "train_sha256": _store_holdout_artifact(train_bytes, root=input_root),
                "evaluation_sha256": _store_holdout_artifact(eval_bytes, root=input_root),
                "semantics_sha256": semantics["semantics_sha256"],
                "context_json": context_json,
            }
        )
    with engine.begin() as connection:
        # Serialize absent-row insertion and idempotent readback without UPDATE
        # authority on the immutable version table. Collisions only serialize
        # unrelated keys; all identities are still compared below.
        lock_key = int.from_bytes(
            hashlib.sha256(
                json.dumps(["holdout-registration.v1", suite_id, suite_version]).encode("utf-8")
            ).digest()[:8],
            byteorder="big",
            signed=True,
        )
        connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
        existing = connection.execute(
            text(
                "SELECT manifest_sha256,development_manifest_sha256,task_count,epsilon,"
                "care_calibration_id "
                "FROM scorer.holdout_suite_versions "
                "WHERE suite_id=:suite AND suite_version=:version"
            ),
            {"suite": suite_id, "version": suite_version},
        ).first()
        if existing is None:
            connection.execute(
                text(
                    "INSERT INTO scorer.holdout_suite_versions "
                    "(suite_id,suite_version,manifest_sha256,development_manifest_sha256,"
                    "task_count,epsilon,care_calibration_id) "
                    "VALUES (:suite,:version,:manifest,:development_manifest,:count,:epsilon,"
                    ":care_calibration_id)"
                ),
                {
                    "suite": suite_id,
                    "version": suite_version,
                    "manifest": manifest_sha256,
                    "development_manifest": development_manifest_sha256,
                    "count": len(prepared),
                    "epsilon": epsilon,
                    "care_calibration_id": care_calibration_id,
                },
            )
        elif tuple(existing) != (
            manifest_sha256,
            development_manifest_sha256,
            len(prepared),
            epsilon,
            care_calibration_id,
        ):
            raise ValueError("holdout suite version is already bound to different identity")
        for prepared_task in prepared:
            prior = (
                connection.execute(
                    text(
                        "SELECT dataset_id,split_id,session_id,profile_sha256,family,task_weight,"
                        "base_score,reference_score,"
                        "sliding_window,sampling_s,train_sha256,evaluation_sha256,"
                        "semantics_sha256,context_json "
                        "FROM scorer.holdout_suite_tasks WHERE suite_id=:suite AND "
                        "suite_version=:version "
                        "AND task_key=:task_key"
                    ),
                    {
                        "suite": suite_id,
                        "version": suite_version,
                        "task_key": prepared_task["task_key"],
                    },
                )
                .mappings()
                .first()
            )
            expected = {key: value for key, value in prepared_task.items() if key != "task_key"}
            if prior is not None:
                if any(prior[key] != value for key, value in expected.items()):
                    raise ValueError("holdout task key is already bound to other content")
                continue
            connection.execute(
                text(
                    "INSERT INTO scorer.holdout_suite_tasks "
                    "(suite_id,suite_version,task_key,dataset_id,split_id,session_id,profile_sha256,"
                    "family,task_weight,base_score,reference_score,sliding_window,sampling_s,"
                    "train_sha256,evaluation_sha256,"
                    "semantics_sha256,context_json) "
                    "VALUES (:suite,:version,:task_key,:dataset_id,:split_id,:session_id,"
                    ":profile_sha256,"
                    ":family,:task_weight,:base_score,:reference_score,:sliding_window,"
                    ":sampling_s,:train_sha256,:evaluation_sha256,"
                    ":semantics_sha256,CAST(:context_json "
                    "AS jsonb))"
                ),
                {
                    "suite": suite_id,
                    "version": suite_version,
                    **{
                        **prepared_task,
                        "context_json": json.dumps(
                            prepared_task["context_json"],
                            sort_keys=True,
                            separators=(",", ":"),
                            allow_nan=False,
                        ),
                    },
                },
            )
        installed = connection.execute(
            text(
                "SELECT count(*) FROM scorer.holdout_suite_tasks WHERE "
                "suite_id=:suite AND suite_version=:version"
            ),
            {"suite": suite_id, "version": suite_version},
        ).scalar_one()
        if installed != len(prepared):
            raise ValueError("holdout registry contains an incomplete task set")
    return {
        "suite_id": suite_id,
        "suite_version": suite_version,
        "tasks": len(prepared),
        "input_bytes": total_bytes,
        "manifest_sha256": manifest_sha256,
        "development_manifest_sha256": development_manifest_sha256,
    }


def _read_arrow_frame(digest: str, *, root: Path) -> pd.DataFrame:
    payload = read_artifact_bytes(digest, artifact_root=root, max_bytes=MAX_HOLDOUT_ARTIFACT_BYTES)
    reader = ipc.open_stream(pa.py_buffer(payload))
    table = reader.read_all()
    frame = table.to_pandas()
    if (
        frame.empty
        or not frame.columns.is_unique
        or not np.isfinite(frame.to_numpy(dtype=np.float64)).all()
    ):
        raise ValueError("private holdout matrix is malformed")
    return cast(pd.DataFrame, frame)


def _read_labels(engine: Engine, row: Any, count: int) -> np.ndarray:
    with engine.connect() as connection:
        profile = (
            connection.execute(
                text(
                    "SELECT sample_count,sliding_window,visibility,profile_sha256,task_family "
                    "FROM scorer.dataset_profiles WHERE dataset_id=:dataset AND split_id=:split "
                    "AND session_id=:session"
                ),
                {
                    "dataset": row["dataset_id"],
                    "split": row["split_id"],
                    "session": row["session_id"],
                },
            )
            .mappings()
            .one()
        )
        if (
            profile["visibility"] != "holdout"
            or profile["profile_sha256"] != row["profile_sha256"]
            or profile["task_family"] != row["family"]
        ):
            raise ValueError("holdout profile identity changed after registration")
        if profile["sample_count"] != count or profile["sliding_window"] != row["sliding_window"]:
            raise ValueError("holdout labels and registered matrix dimensions differ")
        labels = connection.execute(
            text(
                "SELECT sample_index,is_anomaly FROM scorer.dataset_labels WHERE "
                "dataset_id=:dataset AND split_id=:split AND session_id=:session "
                "ORDER BY sample_index"
            ),
            {"dataset": row["dataset_id"], "split": row["split_id"], "session": row["session_id"]},
        ).all()
    if len(labels) != count or any(
        index != position for position, (index, _label) in enumerate(labels)
    ):
        raise ValueError("holdout label coverage is incomplete")
    return np.asarray([int(label) for _, label in labels], dtype=np.int8)


def _read_task_semantics(engine: Engine, row: Any) -> dict[str, Any]:
    with engine.connect() as connection:
        semantics = (
            connection.execute(
                text(
                    "SELECT task_family,sampling_s,evaluation_times_json,masked_samples_json,"
                    "failure_windows_json,semantics_sha256 FROM scorer.dataset_task_semantics "
                    "WHERE dataset_id=:dataset AND split_id=:split AND session_id=:session"
                ),
                {
                    "dataset": row["dataset_id"],
                    "split": row["split_id"],
                    "session": row["session_id"],
                },
            )
            .mappings()
            .one_or_none()
        )
    if semantics is None or (
        semantics["task_family"] != row["family"]
        or semantics["sampling_s"] != row["sampling_s"]
        or semantics["semantics_sha256"] != row["semantics_sha256"]
    ):
        raise ValueError("holdout task semantics changed after registration")
    return dict(semantics)


def _task_metric(
    *,
    family: str,
    labels: np.ndarray,
    scores: Sequence[float],
    policy: AlarmPolicy,
    semantics: dict[str, Any],
    sliding_window: int,
    sampling_s: int | None,
) -> tuple[float, dict[str, float]]:
    if family == "EVT":
        result, masked_count = score_masked_vus(
            labels,
            scores,
            masks=semantics["masked_samples_json"],
            sliding_window=sliding_window,
            threshold_count=HOLDOUT_THRESHOLD_COUNT,
        )
        return result.vus_pr, {
            "vus_pr": float(result.vus_pr),
            "scored_sample_count": float(len(labels) - masked_count),
            "masked_sample_count": float(masked_count),
        }
    if family not in {"PDM", "NRM"}:
        raise ValueError("holdout task family has no implemented temporal metric")
    if sampling_s is None:
        raise ValueError("PDM/NRM holdout metrics require verified sampling seconds")
    masks = np.asarray(semantics["masked_samples_json"], dtype=np.bool_)
    score_values = np.asarray(scores, dtype=np.float64)
    if len(masks) != len(score_values):
        raise ValueError("holdout mask and score lengths differ")
    score_values[masks] = np.nan
    active = apply_alarm_policy(
        score_values, AlarmPolicy(policy.threshold, policy.release, policy.dwell)
    )
    if family == "PDM":
        score, fa_per_day, duty_fraction = pdm_task_score(
            active,
            _validated_failure_windows(semantics["failure_windows_json"], len(active)),
            masks,
            sampling_s,
            max_fa=HOLDOUT_MAX_FA_PER_DAY,
            max_duty=HOLDOUT_MAX_DUTY_FRACTION,
        )
    else:
        score, fa_per_day, duty_fraction = nrm_task_score(
            active,
            masks,
            sampling_s,
            max_duty=HOLDOUT_MAX_DUTY_FRACTION,
        )
    return float(score), {
        "fa_per_day": float(fa_per_day),
        "duty_fraction": float(duty_fraction),
    }


_WORKER_IDENTITY_FIELDS = (
    "worker_pid",
    "worker_start_ticks",
    "worker_boot_id",
    "worker_unit",
    "worker_invocation_id",
    "worker_cgroup",
)


def _read_holdout_recovery_target(engine: Engine, reservation_id: UUID) -> dict[str, Any]:
    with engine.begin() as connection:
        value = connection.execute(
            text("SELECT lab.read_holdout_recovery_target_v2(:reservation_id)"),
            {"reservation_id": reservation_id},
        ).scalar_one()
    if not isinstance(value, dict):
        raise ValueError("Scorer returned an invalid holdout recovery target")
    try:
        if UUID(str(value["reservation_id"])) != reservation_id:
            raise ValueError("holdout recovery target reservation identity changed")
        run_id = UUID(str(value["run_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("holdout recovery target UUID is malformed") from exc
    if (
        value.get("state")
        not in {"reserved", "running", "passed", "reverted", "failed", "exhausted"}
        or not isinstance(value.get("run_state"), str)
        or type(value.get("stop_requested")) is not bool
    ):
        raise ValueError("holdout recovery target state is malformed")
    generation = value.get("admitted_generation")
    execution_sha256 = value.get("execution_sha256")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
        or not isinstance(execution_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", execution_sha256) is None
    ):
        raise ValueError("holdout recovery target has no valid immutable owner pair")
    result = dict(value)
    result["reservation_id"] = reservation_id
    result["run_id"] = run_id
    return result


def _worker_identity_from_target(target: dict[str, Any], reservation_id: UUID) -> dict[str, Any]:
    identity = {key: target.get(key) for key in _WORKER_IDENTITY_FIELDS}
    pid, start_ticks = identity["worker_pid"], identity["worker_start_ticks"]
    unit, invocation, cgroup = (
        identity["worker_unit"],
        identity["worker_invocation_id"],
        identity["worker_cgroup"],
    )
    expected_unit = f"swapp-ai-scientist-scorer-{reservation_id.hex}.service"
    try:
        parsed_boot_id = UUID(str(identity["worker_boot_id"]))
    except (TypeError, ValueError) as exc:
        raise ValueError("holdout worker boot identity is malformed") from exc
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 1
        or isinstance(start_ticks, bool)
        or not isinstance(start_ticks, int)
        or start_ticks <= 0
        or str(parsed_boot_id) != identity["worker_boot_id"]
        or unit != expected_unit
        or not isinstance(invocation, str)
        or re.fullmatch(r"[0-9a-f]{32}", invocation) is None
        or not isinstance(cgroup, str)
        or not cgroup.startswith("/")
        or ".." in Path(cgroup).parts
        or not cgroup.endswith("/" + expected_unit)
    ):
        raise ValueError("holdout worker generation is malformed")
    return identity


def _holdout_identity_matches_target(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(left.get(key) == right.get(key) for key in _WORKER_IDENTITY_FIELDS)


def _process_has_holdout_identity(identity: dict[str, Any]) -> bool:
    if (
        identity["worker_boot_id"]
        != Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    ):
        return False
    pid = int(identity["worker_pid"])
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RuntimeError("holdout worker process identity is unreadable") from exc
    try:
        cgroups = Path(f"/proc/{pid}/cgroup").read_text(encoding="ascii").splitlines()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RuntimeError("holdout worker cgroup identity is unreadable") from exc
    unified = next((line.split(":", 2)[2] for line in cgroups if line.startswith("0::")), None)
    return (
        int(fields[19]) == identity["worker_start_ticks"]
        and fields[0] != "Z"
        and unified == identity["worker_cgroup"]
    )


def _cleanup_holdout_orphan_if_owned(identity: dict[str, Any], *, work_root: Path) -> bool:
    """Reconcile a matching orphan only while holding the shared P=1 lock."""
    from lab.sandbox.docker_runner import OWNED_CONTAINER_LABEL

    runner = LocalDockerRunner(
        image=DEFAULT_SANDBOX_IMAGE,
        work_root=work_root,
        profile=SandboxProfile(memory_bytes=2 * 1024**3, cpus=1.0, pids=64, timeout_seconds=180),
    )
    if runner.admission_lock.is_symlink():
        return False
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(runner.admission_lock, flags, 0o600)
    except OSError:
        return False
    lock_acquired = False
    try:
        os.fchmod(descriptor, 0o600)
        lock_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(lock_stat.st_mode)
            or lock_stat.st_uid != os.getuid()
            or lock_stat.st_nlink != 1
            or stat.S_IMODE(lock_stat.st_mode) & 0o077
        ):
            return False
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_acquired = True
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return False
            return False
        marker = runner.admission_marker
        try:
            info = marker.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > 16 * 1024
        ):
            return False
        marker_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            marker_fd = os.open(marker, marker_flags)
            try:
                current = os.fstat(marker_fd)
                if (
                    not stat.S_ISREG(current.st_mode)
                    or current.st_ino != info.st_ino
                    or current.st_dev != info.st_dev
                    or current.st_nlink != 1
                    or current.st_uid != os.getuid()
                    or current.st_size > 16 * 1024
                ):
                    return False
                with os.fdopen(marker_fd, "rb", closefd=False) as handle:
                    payload = handle.read(16 * 1024 + 1)
            finally:
                os.close(marker_fd)
        except OSError:
            return False
        try:
            intent = json.loads(payload.decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        if work_root.is_symlink():
            return False
        expected_root = runner.work_root
        if (
            not isinstance(intent, dict)
            or intent.get("owner_pid") != identity["worker_pid"]
            or intent.get("owner_start") != str(identity["worker_start_ticks"])
            or intent.get("boot_id") != identity["worker_boot_id"]
            or intent.get("work_root") != str(expected_root)
            or intent.get("owner_label") != OWNED_CONTAINER_LABEL
        ):
            return False
        try:
            # Re-read/validate the marker and drain only while the P=1 lock is
            # held. The generic reconciler rejects malformed/foreign ownership.
            runner._reconcile_owned_containers()
        except Exception:
            return False
        try:
            marker.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return False
    finally:
        if lock_acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _failed_holdout_recovery_cas(
    engine: Engine,
    reservation_id: UUID,
    run_id: UUID,
    identity: dict[str, Any],
    *,
    admitted_generation: int,
    execution_sha256: str,
) -> str:
    with engine.begin() as connection:
        value = connection.execute(
            text(
                "SELECT lab.recover_holdout_failure(:reservation_id,:run_id,:worker_pid,"
                ":worker_start_ticks,:worker_boot_id,:worker_unit,:worker_invocation_id,"
                ":worker_cgroup,:generation,:execution_sha256)"
            ),
            {
                "reservation_id": reservation_id,
                "run_id": run_id,
                "generation": admitted_generation,
                "execution_sha256": execution_sha256,
                **identity,
            },
        ).scalar_one()
    if (
        not isinstance(value, dict)
        or value.get("reservation_id")
        not in {
            reservation_id,
            str(reservation_id),
        }
        or value.get("state") not in {"passed", "reverted", "failed", "exhausted"}
    ):
        raise ValueError("Scorer returned an invalid holdout recovery receipt")
    if value["state"] not in {"passed", "reverted"} and value.get("bit") is not None:
        raise ValueError("failed holdout recovery returned a result bit")
    return str(value["state"])


def _failed_unclaimed_holdout_recovery_cas(
    engine: Engine,
    reservation_id: UUID,
    run_id: UUID,
    *,
    admitted_generation: int,
    execution_sha256: str,
) -> str:
    with engine.begin() as connection:
        value = connection.execute(
            text(
                "SELECT lab.recover_unclaimed_holdout_failure(:reservation_id,:run_id,"
                ":generation,:execution_sha256)"
            ),
            {
                "reservation_id": reservation_id,
                "run_id": run_id,
                "generation": admitted_generation,
                "execution_sha256": execution_sha256,
            },
        ).scalar_one()
    if (
        not isinstance(value, dict)
        or value.get("reservation_id") not in {reservation_id, str(reservation_id)}
        or value.get("state") not in {"pending", "passed", "reverted", "failed", "exhausted"}
    ):
        raise ValueError("Scorer returned an invalid unclaimed holdout recovery receipt")
    if value["state"] in {"passed", "reverted"}:
        if type(value.get("bit")) is not bool:
            raise ValueError("terminal holdout recovery result has an invalid bit")
    elif value.get("bit") is not None:
        raise ValueError("failed unclaimed holdout recovery returned a result bit")
    return str(value["state"])


def _recover_unclaimed_holdout_reservation_locked(
    engine: Engine, reservation_id: UUID, target: dict[str, Any]
) -> HoldoutRecoveryResult:
    """Fence a never-claimed reservation only after its launch unit is absent."""
    if any(target.get(field) is not None for field in _WORKER_IDENTITY_FIELDS):
        return HoldoutRecoveryResult(reservation_id, "pending")
    from lab.scorer.supervisor import (
        _cgroup_is_absent_or_empty,
        _expected_unit_cgroup,
        _systemctl_show,
    )

    unit = f"swapp-ai-scientist-scorer-{reservation_id.hex}.service"
    try:
        properties = _systemctl_show(unit)
        expected_group = _expected_unit_cgroup(unit)
    except Exception:
        return HoldoutRecoveryResult(reservation_id, "pending")
    if (
        properties.get("LoadState") != "not-found"
        or properties.get("ActiveState") not in {None, "inactive"}
        or properties.get("MainPID") not in {None, "0"}
        or not _cgroup_is_absent_or_empty(expected_group)
    ):
        return HoldoutRecoveryResult(reservation_id, "pending")
    state = _failed_unclaimed_holdout_recovery_cas(
        engine,
        reservation_id,
        target["run_id"],
        admitted_generation=target["admitted_generation"],
        execution_sha256=target["execution_sha256"],
    )
    return HoldoutRecoveryResult(reservation_id, state)


def _recover_holdout_reservation_locked(
    engine: Engine,
    reservation_id: UUID,
    *,
    deadline: float,
) -> HoldoutRecoveryResult:
    target = _read_holdout_recovery_target(engine, reservation_id)
    if target["state"] in {"passed", "reverted", "failed", "exhausted"}:
        return HoldoutRecoveryResult(reservation_id, str(target["state"]))
    if target["state"] == "reserved":
        return _recover_unclaimed_holdout_reservation_locked(engine, reservation_id, target)
    if target["state"] != "running":
        return HoldoutRecoveryResult(reservation_id, "pending")
    run_id = target["run_id"]
    owner_pair = (target["admitted_generation"], target["execution_sha256"])
    identity = _worker_identity_from_target(target, reservation_id)
    # A fresh Scorer read locks run then reservation and closes before systemd
    # inspection or any host wait. It is re-read again immediately before stop.
    target = _read_holdout_recovery_target(engine, reservation_id)
    if (
        target["state"] != "running"
        or target["run_id"] != run_id
        or (target["admitted_generation"], target["execution_sha256"]) != owner_pair
        or not _holdout_identity_matches_target(
            identity, _worker_identity_from_target(target, reservation_id)
        )
    ):
        return HoldoutRecoveryResult(reservation_id, "pending")

    from lab.scorer.supervisor import (
        _cgroup_is_absent_or_empty,
        _expected_unit_cgroup,
        _owned_unit_cgroup,
        _stop_owned_scorer_unit_locked,
        _systemctl_show,
    )

    unit = str(identity["worker_unit"])
    properties = _systemctl_show(unit)
    run_stop_requested = target["stop_requested"] is True
    current_pid = _process_has_holdout_identity(identity)
    if properties.get("LoadState") == "loaded":
        try:
            control_group = _owned_unit_cgroup(unit, properties)
        except RuntimeError:
            return HoldoutRecoveryResult(reservation_id, "pending")
        if (
            properties.get("InvocationID") != identity["worker_invocation_id"]
            or control_group != identity["worker_cgroup"]
        ):
            return HoldoutRecoveryResult(reservation_id, "pending")
        if properties.get("ActiveState") == "active":
            if properties.get("MainPID") != str(identity["worker_pid"]):
                return HoldoutRecoveryResult(reservation_id, "pending")
            if not run_stop_requested:
                return HoldoutRecoveryResult(reservation_id, "pending")
            authorized = _read_holdout_recovery_target(engine, reservation_id)
            if (
                authorized.get("state") != "running"
                or authorized.get("run_id") != run_id
                or (
                    authorized.get("admitted_generation"),
                    authorized.get("execution_sha256"),
                )
                != owner_pair
                or authorized.get("stop_requested") is not True
                or not _holdout_identity_matches_target(
                    identity, _worker_identity_from_target(authorized, reservation_id)
                )
            ):
                return HoldoutRecoveryResult(reservation_id, "pending")
            timeout_left = max(1, min(30, int(deadline - time.monotonic())))
            _stop_owned_scorer_unit_locked(
                reservation_id,
                invocation_id=str(identity["worker_invocation_id"]),
                timeout_seconds=timeout_left,
            )
            properties = _systemctl_show(unit)
        if properties.get("LoadState") == "loaded":
            if (
                properties.get("ActiveState") != "inactive"
                or properties.get("InvocationID") != identity["worker_invocation_id"]
                or properties.get("MainPID") != "0"
                or properties.get("ControlGroup") != identity["worker_cgroup"]
                or not _cgroup_is_absent_or_empty(str(identity["worker_cgroup"]))
            ):
                return HoldoutRecoveryResult(reservation_id, "pending")
        elif properties.get("LoadState") == "not-found":
            expected_group = _expected_unit_cgroup(unit)
            if expected_group != identity["worker_cgroup"] or not _cgroup_is_absent_or_empty(
                expected_group
            ):
                return HoldoutRecoveryResult(reservation_id, "pending")
        else:
            return HoldoutRecoveryResult(reservation_id, "pending")
    elif properties.get("LoadState") == "not-found":
        expected_group = _expected_unit_cgroup(unit)
        if expected_group != identity["worker_cgroup"] or not _cgroup_is_absent_or_empty(
            expected_group
        ):
            return HoldoutRecoveryResult(reservation_id, "pending")
    else:
        return HoldoutRecoveryResult(reservation_id, "pending")

    if current_pid and target.get("stop_requested") is not True:
        # Never cancel a live worker as an automatic retry/recovery action.
        return HoldoutRecoveryResult(reservation_id, "pending")
    if _process_has_holdout_identity(identity):
        return HoldoutRecoveryResult(reservation_id, "pending")
    try:
        cleanup_complete = _cleanup_holdout_orphan_if_owned(
            identity, work_root=Path("data/runtime/holdout-work") / reservation_id.hex
        )
    except Exception:
        cleanup_complete = False
    if not cleanup_complete:
        return HoldoutRecoveryResult(reservation_id, "pending")
    state = _failed_holdout_recovery_cas(
        engine,
        reservation_id,
        run_id,
        identity,
        admitted_generation=target["admitted_generation"],
        execution_sha256=target["execution_sha256"],
    )
    return HoldoutRecoveryResult(reservation_id, state)


def recover_holdout_reservation(
    engine: Engine, reservation_id: UUID, *, timeout_seconds: int = 30
) -> HoldoutRecoveryResult:
    """Fail one dead, drained reservation without refunding quota or rerunning it."""
    if not isinstance(reservation_id, UUID):
        raise TypeError("holdout reservation id must be a UUID")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 1 <= timeout_seconds <= 120
    ):
        raise ValueError("holdout recovery budget must be 1..120 seconds")
    from lab.scorer.supervisor import _job_lifecycle_lock

    deadline = time.monotonic() + timeout_seconds
    remaining = max(1, int(deadline - time.monotonic()))
    with _job_lifecycle_lock(reservation_id, timeout_seconds=remaining):
        return _recover_holdout_reservation_locked(engine, reservation_id, deadline=deadline)


def recover_holdout_run(engine: Engine, run_id: UUID, *, timeout_seconds: int = 30) -> str:
    """Boundedly reconcile open holdout reservations after an explicit run stop."""
    if not isinstance(run_id, UUID):
        raise TypeError("holdout run id must be a UUID")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 1 <= timeout_seconds <= 120
    ):
        raise ValueError("holdout run recovery budget must be 1..120 seconds")
    with engine.connect() as connection:
        targets = connection.execute(
            text("SELECT lab.list_holdout_recovery_targets_v2(:run_id)"), {"run_id": run_id}
        ).scalar_one()
    if not isinstance(targets, list) or len(targets) > 128:
        raise ValueError("Scorer returned an invalid holdout run recovery list")
    reservation_ids: list[UUID] = []
    for target in targets:
        if not isinstance(target, dict) or set(target) != {
            "reservation_id",
            "state",
            "admitted_generation",
            "execution_sha256",
        }:
            raise ValueError("Scorer returned an invalid holdout run recovery target")
        if target.get("state") not in {"reserved", "running"}:
            raise ValueError("Scorer returned a non-open holdout recovery target")
        generation = target.get("admitted_generation")
        execution_sha256 = target.get("execution_sha256")
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
            or not isinstance(execution_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", execution_sha256) is None
        ):
            raise ValueError("Scorer returned a recovery target without an owner pair")
        try:
            reservation_ids.append(UUID(str(target["reservation_id"])))
        except (TypeError, ValueError) as exc:
            raise ValueError("Scorer returned a malformed holdout reservation id") from exc
    if len(set(reservation_ids)) != len(reservation_ids):
        raise ValueError("Scorer returned duplicate holdout recovery targets")
    deadline = time.monotonic() + timeout_seconds
    pending = False
    for reservation_id in reservation_ids:
        remaining = int(deadline - time.monotonic())
        if remaining < 1:
            pending = True
            break
        result = recover_holdout_reservation(
            engine, reservation_id, timeout_seconds=min(120, remaining)
        )
        pending = pending or result.state == "pending"
    return "pending" if pending else "drained"


def _assert_holdout_admission(
    engine: Engine,
    reservation_id: UUID,
    run_id: UUID,
    identity: dict[str, Any],
    *,
    admitted_generation: int,
    execution_sha256: str,
) -> None:
    with engine.begin() as connection:
        allowed = connection.execute(
            text(
                "SELECT lab.check_holdout_admission(:reservation_id,:run_id,:worker_pid,"
                ":worker_start_ticks,:worker_boot_id,:worker_unit,:worker_invocation_id,"
                ":worker_cgroup,:generation,:execution_sha256)"
            ),
            {
                "reservation_id": reservation_id,
                "run_id": run_id,
                "generation": admitted_generation,
                "execution_sha256": execution_sha256,
                **identity,
            },
        ).scalar_one()
    if allowed is not True:
        raise RuntimeError("holdout run or worker reservation generation is no longer admitted")


def evaluate_holdout_reservation(
    engine: Engine,
    *,
    reservation_id: UUID,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
    input_root: Path = DEFAULT_HOLDOUT_INPUT_ROOT,
    work_root: Path = Path("data/runtime/holdout-work"),
    total_seconds: int = MAX_HOLDOUT_EVALUATION_SECONDS,
    admitted_generation: int,
    execution_sha256: str,
) -> dict[str, object]:
    """Score the reserved source pair in fresh fit/score containers, then publish one bit."""
    if not isinstance(reservation_id, UUID):
        raise TypeError("holdout reservation id must be a UUID")
    if (
        isinstance(admitted_generation, bool)
        or not isinstance(admitted_generation, int)
        or admitted_generation < 1
        or not isinstance(execution_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", execution_sha256) is None
    ):
        raise ValueError("holdout admitted owner pair is malformed")
    if (
        isinstance(total_seconds, bool)
        or not isinstance(total_seconds, int)
        or not 1 <= total_seconds <= MAX_HOLDOUT_EVALUATION_SECONDS
    ):
        raise ValueError("holdout total budget must be 1..600 seconds")
    # Include worker verification, DB claim, and private input loading in the
    # one bounded holdout evaluation window.
    deadline = time.monotonic() + total_seconds
    _remaining_seconds(deadline)
    expected_unit = f"swapp-ai-scientist-scorer-{reservation_id.hex}.service"
    from lab.scorer.worker import verify_systemd_invocation

    invocation = verify_systemd_invocation(expected_unit)
    process_stat = Path(f"/proc/{os.getpid()}/stat").read_text(encoding="ascii")
    process_fields = process_stat.rsplit(")", maxsplit=1)[1].split()
    if process_fields[0] == "Z":
        raise RuntimeError("holdout Scorer process is a zombie")
    start_ticks = int(process_fields[19])
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    with engine.begin() as connection:
        receipt = connection.execute(
            text(
                "SELECT lab.claim_holdout_reservation(:reservation_id,:worker_pid,:start_ticks,"
                ":boot_id,:unit,:invocation,:cgroup,:generation,:execution_sha256)"
            ),
            {
                "reservation_id": reservation_id,
                "worker_pid": os.getpid(),
                "start_ticks": start_ticks,
                "boot_id": boot_id,
                "unit": invocation.unit,
                "invocation": invocation.invocation_id,
                "cgroup": invocation.control_group,
                "generation": admitted_generation,
                "execution_sha256": execution_sha256,
            },
        ).scalar_one()
    _remaining_seconds(deadline)
    if not isinstance(receipt, dict) or receipt.get("reservation_id") not in {
        reservation_id,
        str(reservation_id),
    }:
        raise ValueError("Scorer claim returned a mismatched holdout reservation")
    if (
        receipt.get("admitted_generation") != admitted_generation
        or receipt.get("execution_sha256") != execution_sha256
    ):
        raise ValueError("Scorer claim returned a different admitted owner pair")
    try:
        try:
            claimed_run_id = UUID(str(receipt["run_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Scorer claim returned a malformed run identity") from exc
        worker_identity = _assert_claimed_generation(receipt, reservation_id)

        def admission_check() -> None:
            current_identity = _assert_claimed_generation(receipt, reservation_id)
            if not _holdout_identity_matches_target(worker_identity, current_identity):
                raise RuntimeError("holdout worker generation changed before sandbox admission")
            _assert_holdout_admission(
                engine,
                reservation_id,
                claimed_run_id,
                worker_identity,
                admitted_generation=admitted_generation,
                execution_sha256=execution_sha256,
            )

        candidate = read_artifact_bytes(
            str(receipt["candidate_blob_sha256"]), artifact_root=artifact_root
        )
        reference = read_artifact_bytes(
            str(receipt["reference_blob_sha256"]), artifact_root=artifact_root
        )
        if (
            hashlib.sha256(candidate).hexdigest() != receipt["candidate_blob_sha256"]
            or hashlib.sha256(reference).hexdigest() != receipt["reference_blob_sha256"]
        ):
            raise ValueError("candidate or reference source blob digest mismatch")
        with engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT task_key,dataset_id,split_id,session_id,profile_sha256,family,"
                        "task_weight,"
                        "base_score,reference_score,sliding_window,sampling_s,"
                        "train_sha256,evaluation_sha256,"
                        "semantics_sha256,context_json "
                        "FROM scorer.holdout_suite_tasks WHERE suite_id=:suite AND "
                        "suite_version=:version "
                        "ORDER BY task_key"
                    ),
                    {"suite": receipt["suite_id"], "version": receipt["suite_version"]},
                )
                .mappings()
                .all()
            )
        _remaining_seconds(deadline)
        if not rows or len(rows) > MAX_HOLDOUT_TASKS:
            raise ValueError("registered holdout task count is invalid")
        root = work_root / reservation_id.hex
        if root.is_symlink():
            raise ValueError("holdout worker directory cannot be a symlink")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.stat().st_uid != os.getuid() or stat.S_IMODE(root.stat().st_mode) != 0o700:
            raise ValueError("holdout worker directory is not private")
        runner = LocalDockerRunner(
            image=DEFAULT_SANDBOX_IMAGE,
            work_root=root,
            profile=SandboxProfile(
                memory_bytes=2 * 1024**3, cpus=1.0, pids=64, timeout_seconds=180
            ),
        )
        weighted_delta = 0.0
        weights = 0.0
        task_details: list[dict[str, object]] = []
        for row in rows:
            if row["family"] not in {"EVT", "PDM", "NRM"}:
                raise ValueError("holdout task family is unsupported")
            context_json = row["context_json"]
            if not isinstance(context_json, dict):
                raise ValueError("holdout FitContext is invalid")
            seed = context_json.get("seed")
            signals = context_json.get("signals")
            regime_signals = context_json.get("regime_signals")
            time_budget_s = context_json.get("time_budget_s")
            if (
                isinstance(seed, bool)
                or not isinstance(seed, int)
                or not isinstance(signals, list)
                or any(not isinstance(signal, str) or not signal for signal in signals)
                or not isinstance(regime_signals, list)
                or any(not isinstance(signal, str) or not signal for signal in regime_signals)
                or isinstance(time_budget_s, bool)
                or not isinstance(time_budget_s, (int, float))
                or not math.isfinite(float(time_budget_s))
                or time_budget_s <= 0
            ):
                raise ValueError("holdout FitContext fields are malformed")
            context_sampling = context_json.get("sampling_s")
            if context_sampling != row["sampling_s"]:
                raise ValueError("holdout FitContext cadence changed after registration")
            if row["family"] in {"PDM", "NRM"} and (
                isinstance(context_sampling, bool)
                or not isinstance(context_sampling, int)
                or context_sampling <= 0
            ):
                raise ValueError("PDM/NRM holdout tasks require positive sampling seconds")
            context = FitContext(
                seed=seed,
                signals=tuple(signals),
                regime_signals=tuple(regime_signals),
                sampling_s=context_sampling,
                time_budget_s=float(time_budget_s),
            )
            train = _read_arrow_frame(str(row["train_sha256"]), root=input_root)
            evaluation = _read_arrow_frame(str(row["evaluation_sha256"]), root=input_root)
            if (
                tuple(map(str, train.columns)) != context.signals
                or tuple(map(str, evaluation.columns)) != context.signals
            ):
                raise ValueError("holdout matrix columns differ from FitContext")
            labels = _read_labels(engine, row, len(evaluation))
            semantics = _read_task_semantics(engine, row)
            _validate_semantics(
                semantics,
                task_family=row["family"],
                sample_count=len(evaluation),
                sampling_s=row["sampling_s"],
                labels=labels,
            )
            remaining = _remaining_seconds(deadline)
            reference_eval = run_candidate_fit_score(
                runner,
                candidate_source=reference,
                train=train,
                evaluation=evaluation,
                context=context,
                remaining_seconds=remaining,
                fit_timeout_seconds=min(60, remaining),
                score_timeout_seconds=min(30, remaining),
                trusted_baseline_name="robust_z" if _is_robust_z(reference) else None,
                admission_check=admission_check,
            )
            remaining = _remaining_seconds(deadline)
            candidate_eval = run_candidate_fit_score(
                runner,
                candidate_source=candidate,
                train=train,
                evaluation=evaluation,
                context=context,
                remaining_seconds=remaining,
                fit_timeout_seconds=min(60, remaining),
                score_timeout_seconds=min(30, remaining),
                admission_check=admission_check,
            )
            _remaining_seconds(deadline)
            reference_metric, reference_aux = _task_metric(
                family=row["family"],
                labels=labels,
                scores=reference_eval.scores,
                policy=reference_eval.policy,
                semantics=semantics,
                sliding_window=row["sliding_window"],
                sampling_s=row["sampling_s"],
            )
            _remaining_seconds(deadline)
            candidate_metric, candidate_aux = _task_metric(
                family=row["family"],
                labels=labels,
                scores=candidate_eval.scores,
                policy=candidate_eval.policy,
                semantics=semantics,
                sliding_window=row["sliding_window"],
                sampling_s=row["sampling_s"],
            )
            _remaining_seconds(deadline)
            reference_normalized = normalize_task_score(
                reference_metric,
                float(row["base_score"]),
                float(row["reference_score"]),
                family=row["family"],
            )
            candidate_normalized = normalize_task_score(
                candidate_metric,
                float(row["base_score"]),
                float(row["reference_score"]),
                family=row["family"],
            )
            delta = float(candidate_normalized - reference_normalized)
            weight = float(row["task_weight"])
            weighted_delta += weight * delta
            weights += weight
            task_details.append(
                {
                    "task_key": str(row["task_key"]),
                    "candidate_raw_task_score": candidate_metric,
                    "reference_raw_task_score": reference_metric,
                    "candidate_normalized_score": candidate_normalized,
                    "reference_normalized_score": reference_normalized,
                    "candidate_aux": candidate_aux,
                    "reference_aux": reference_aux,
                    "semantics_sha256": row["semantics_sha256"],
                    "delta": delta,
                    "weight": weight,
                    "fit_seconds": candidate_eval.fit_seconds + reference_eval.fit_seconds,
                    "score_seconds": candidate_eval.score_seconds + reference_eval.score_seconds,
                }
            )
        if weights <= 0:
            raise ValueError("holdout suite has no positive task weight")
        delta = weighted_delta / weights
        _remaining_seconds(deadline)
        details = {
            "schema": "holdout-evaluation.v1",
            "reservation_id": str(reservation_id),
            "suite_id": receipt["suite_id"],
            "suite_version": receipt["suite_version"],
            "manifest_sha256": receipt["manifest_sha256"],
            "candidate_sha256": receipt["candidate_sha256"],
            "reference_sha256": receipt["reference_sha256"],
            "epsilon": float(receipt["epsilon"]),
            "delta": delta,
            "tasks": task_details,
            "threshold_count": HOLDOUT_THRESHOLD_COUNT,
        }
        canonical = json.dumps(
            details, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        result_sha = hashlib.sha256(canonical).hexdigest()
        _remaining_seconds(deadline)
        worker_identity = _assert_claimed_generation(receipt, reservation_id)
        with engine.begin() as connection:
            published = connection.execute(
                text(
                    "SELECT lab.publish_holdout_result(:reservation_id,:delta,CAST(:details "
                    "AS jsonb),:sha,:worker_pid,:worker_start_ticks,:worker_boot_id,:worker_unit,"
                    ":worker_invocation_id,:worker_cgroup,:generation,:execution_sha256)"
                ),
                {
                    "reservation_id": reservation_id,
                    "delta": delta,
                    "details": canonical.decode(),
                    "sha": result_sha,
                    "generation": admitted_generation,
                    "execution_sha256": execution_sha256,
                    **worker_identity,
                },
            ).scalar_one()
        return {"state": published["state"], "bit": published["bit"]}
    except BaseException as exc:
        code = "timeout" if isinstance(exc, TimeoutError) else "evaluation_error"
        try:
            worker_identity = _assert_claimed_generation(receipt, reservation_id)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "SELECT lab.fail_holdout_reservation(:reservation_id,:error_code,"
                        ":worker_pid,:worker_start_ticks,:worker_boot_id,:worker_unit,"
                        ":worker_invocation_id,:worker_cgroup,:generation,:execution_sha256,'active')"
                    ),
                    {
                        "reservation_id": reservation_id,
                        "error_code": code,
                        "generation": admitted_generation,
                        "execution_sha256": execution_sha256,
                        **worker_identity,
                    },
                )
        except Exception:
            exc.add_note("holdout failure publication did not complete; recovery is required")
        raise
    finally:
        # Candidate workspaces contain no source data needed for an audit; only
        # the Scorer-only metric result and its digest remain durable.
        if "root" in locals() and root.exists() and not root.is_symlink():
            shutil.rmtree(root)


def _is_robust_z(source: bytes) -> bool:
    from lab.director.baselines import baseline_candidate_source

    return source == baseline_candidate_source("robust_z")


def _remaining_seconds(deadline: float) -> int:
    remaining = int(deadline - time.monotonic())
    if remaining < 1:
        raise TimeoutError("holdout suite exceeded its total evaluation budget")
    return remaining


def _assert_claimed_generation(receipt: dict[str, Any], reservation_id: UUID) -> dict[str, Any]:
    """Recheck this process still owns the claimed systemd generation before DB writes."""
    from lab.scorer.worker import verify_systemd_invocation

    unit = f"swapp-ai-scientist-scorer-{reservation_id.hex}.service"
    current = verify_systemd_invocation(unit)
    fields = Path(f"/proc/{os.getpid()}/stat").read_text(encoding="ascii")
    start_ticks = int(fields.rsplit(")", maxsplit=1)[1].split()[19])
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    identity = {
        "worker_pid": os.getpid(),
        "worker_start_ticks": start_ticks,
        "worker_boot_id": boot_id,
        "worker_unit": current.unit,
        "worker_invocation_id": current.invocation_id,
        "worker_cgroup": current.control_group,
    }
    for key, value in identity.items():
        if receipt.get(key) != value:
            raise RuntimeError("holdout worker lost its claimed generation")
    return identity
