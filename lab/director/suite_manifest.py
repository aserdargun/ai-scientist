"""Strict private manifest loader for label-free Director suite tasks."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

import numpy as np
import pandas as pd
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
)
from sqlalchemy import Engine, text

from harness.contracts import FitContext
from lab.director.contracts import SourceProvenance
from lab.director.suite import SuiteTask
from lab.director.suite_weights import SuiteWeightInput, suite_weight_task_key, suite_weights
from lab.suite_limits import MAX_SUITE_MANIFEST_BYTES

if TYPE_CHECKING:
    from lab.operating_modes.public_snapshot import PublicTaskBinding

MAX_SUITE_MATRIX_BYTES = 64 * 1024**2


class SuiteTaskManifest(BaseModel):
    """JSON task carrying only approved numeric features and trusted provenance."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    task_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    dataset_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    split_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    session_id: Annotated[StrictStr, Field(min_length=1, max_length=256)]
    profile_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    family: Literal["EVT", "PDM", "NRM"]
    task_weight: Annotated[StrictFloat, Field(gt=0, allow_inf_nan=False)]
    independent_family: Annotated[StrictStr, Field(min_length=1, max_length=256)]
    label_tier: Literal["gold", "silver", "bronze"]
    provenance: SourceProvenance
    context: dict[StrictStr, object]
    columns: tuple[StrictStr, ...] = Field(min_length=1, max_length=256)
    train: tuple[tuple[StrictFloat, ...], ...] = Field(min_length=1, max_length=1_000_000)
    evaluation: tuple[tuple[StrictFloat, ...], ...] = Field(min_length=1, max_length=1_000_000)

    @field_validator("columns", "train", "evaluation", mode="before")
    @classmethod
    def normalize_json_arrays(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(tuple(row) if isinstance(row, list) else row for row in value)
        return value


class SuiteManifest(BaseModel):
    """Versioned ordered suite input; candidate-visible records contain no labels."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal["director-suite.v1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    suite_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    suite_version: Annotated[StrictInt, Field(ge=1)]
    tasks: tuple[SuiteTaskManifest, ...] = Field(min_length=1, max_length=256)
    weight_policy: Literal["spec-3.2.5-appendix-c-waterfill.v1", "single_snapshot_study.v1"]
    family_cap: Annotated[StrictFloat, Field(gt=0.0, le=1.0)]
    type_shares: dict[StrictStr, StrictFloat]
    family_shares: dict[StrictStr, StrictFloat]

    @field_validator("tasks", mode="before")
    @classmethod
    def normalize_tasks(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("type_shares", "family_shares")
    @classmethod
    def normalize_share_keys(cls, value: dict[str, float]) -> dict[str, float]:
        if not value or any(not key for key in value):
            raise ValueError("suite weight shares need non-empty keys")
        if any(not math.isfinite(float(share)) or float(share) <= 0 for share in value.values()):
            raise ValueError("suite weight shares must be positive finite values")
        return value


def write_suite_manifest(
    tasks: tuple[SuiteTask, ...],
    path: Path,
    *,
    suite_id: str = "public-ad-v1",
    suite_version: int = 1,
    single_snapshot_study: bool = False,
) -> tuple[int, str]:
    """Atomically write a bounded label-free suite payload and weight receipt."""
    if not tasks or len(tasks) > 256 or not suite_id or suite_version < 1:
        raise ValueError("suite manifest identity or task count is invalid")
    records = tuple(
        SuiteWeightInput(
            task_id=suite_weight_task_key(
                (task.dataset_id, task.split_id, task.session_id, task.task_id)
            ),
            task_type=task.family,
            label_tier=task.label_tier,
            family=task.independent_family,
        )
        for task in tasks
    )
    if single_snapshot_study and len(tasks) != 1:
        raise ValueError("single snapshot study requires exactly one task")
    weights = suite_weights(records, family_cap=1.0 if single_snapshot_study else 0.25)
    for task, weight in zip(tasks, weights.weights, strict=True):
        if task.task_weight != weight:
            raise ValueError("suite task weights must be finalized before writing the manifest")
    task_items = []
    matrix_bytes = 0
    for task in tasks:
        train = task._train
        evaluation = task._evaluation
        if (
            train.ndim != 2
            or evaluation.ndim != 2
            or train.shape[1] != len(task._columns)
            or evaluation.shape[1] != len(task._columns)
        ):
            raise ValueError("suite task matrices differ from the declared feature columns")
        matrix_bytes += train.nbytes + evaluation.nbytes
        if matrix_bytes > MAX_SUITE_MATRIX_BYTES:
            raise ValueError("suite feature matrices exceed the aggregate 64 MiB limit")
        task_items.append(
            {
                "task_id": task.task_id,
                "dataset_id": task.dataset_id,
                "split_id": task.split_id,
                "session_id": task.session_id,
                "profile_sha256": task.profile_sha256,
                "family": task.family,
                "task_weight": task.task_weight,
                "independent_family": task.independent_family,
                "label_tier": task.label_tier,
                "provenance": task.provenance.model_dump(mode="json"),
                "context": {
                    "seed": task.context.seed,
                    "signals": list(task.context.signals),
                    "regime_signals": list(task.context.regime_signals),
                    "sampling_s": task.context.sampling_s,
                    "time_budget_s": task.context.time_budget_s,
                },
                "columns": list(task._columns),
                "train": train.tolist(),
                "evaluation": evaluation.tolist(),
            }
        )
    payload = json.dumps(
        {
            "schema": "director-suite.v1",
            "suite_id": suite_id,
            "suite_version": suite_version,
            "tasks": task_items,
            "weight_policy": (
                "single_snapshot_study.v1"
                if single_snapshot_study
                else "spec-3.2.5-appendix-c-waterfill.v1"
            ),
            "family_cap": weights.family_cap,
            "type_shares": dict(weights.type_shares),
            "family_shares": dict(weights.family_shares),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    if len(payload) > MAX_SUITE_MANIFEST_BYTES:
        raise ValueError("serialized suite manifest exceeds the aggregate 96 MiB limit")
    destination = path.expanduser()
    if destination.is_symlink():
        raise ValueError("suite manifest destination cannot be a symlink")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".suite-", dir=destination.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return len(payload), hashlib.sha256(payload).hexdigest()


def load_suite_manifest(
    path: Path,
    planner_engine: Engine,
    *,
    study_snapshot_sha256: str | None = None,
    public_task_binding: PublicTaskBinding | None = None,
) -> tuple[SuiteManifest, tuple[SuiteTask, ...], str]:
    """Read, hash, strictly parse, and Planner-verify a private suite manifest.

    The manifest must not contain anomaly labels, raw timestamps or split metadata.
    Corresponding labels remain in the Scorer database and never enter a provider.
    """
    candidate = path.expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError("suite manifest must be a regular, non-symlink file")
    metadata = candidate.stat()
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or metadata.st_size > (
        MAX_SUITE_MANIFEST_BYTES
    ):
        raise ValueError("suite manifest permissions or byte size exceed the private input limit")
    if metadata.st_mode & stat.S_IRGRP or metadata.st_mode & stat.S_IROTH:
        raise ValueError("suite manifest must be private to its owner")
    payload = candidate.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    raw = json.loads(payload)
    if not isinstance(raw, dict):
        raise ValueError("suite manifest root must be an object")
    document = SuiteManifest.model_validate(raw, strict=True)
    if document.weight_policy == "single_snapshot_study.v1":
        if (
            study_snapshot_sha256 is None
            or len(document.tasks) != 1
            or document.family_cap != 1.0
            or (
                public_task_binding is None
                and document.tasks[0].provenance.source_manifest_sha256 != study_snapshot_sha256
            )
        ):
            raise ValueError("single snapshot study requires verified mode-grid snapshot authority")
        if public_task_binding is not None:
            from lab.operating_modes.public_snapshot import PublicTaskSnapshot

            bound = PublicTaskSnapshot(
                schema="public-task-snapshot.v1", binding=public_task_binding
            )
            if bound.sha256 != study_snapshot_sha256:
                raise ValueError("public study snapshot differs from its original task binding")
            public_task_binding.verify_study_task(document.tasks[0])
    elif public_task_binding is not None:
        raise ValueError("public task authority requires a single development study")
    elif document.family_cap != 0.25:
        raise ValueError("benchmark suite requires the fixed independent-family cap")
    computed_weights = suite_weights(
        tuple(
            SuiteWeightInput(
                task_id=suite_weight_task_key(
                    (task.dataset_id, task.split_id, task.session_id, task.task_id)
                ),
                task_type=task.family,
                label_tier=task.label_tier,
                family=task.independent_family,
            )
            for task in document.tasks
        ),
        family_cap=document.family_cap,
    )
    if (
        tuple(task.task_weight for task in document.tasks) != computed_weights.weights
        or document.type_shares != computed_weights.type_shares
        or document.family_shares != computed_weights.family_shares
    ):
        raise ValueError("suite manifest weight receipts differ from canonical water-fill")
    forbidden = {"label", "labels", "anomaly", "anomalies", "changepoint", "timestamp", "datetime"}
    tasks: list[SuiteTask] = []
    total_matrix_bytes = 0
    identities: set[tuple[str, str, str, str]] = set()
    with planner_engine.connect() as connection:
        for task in document.tasks:
            identity = (task.dataset_id, task.split_id, task.session_id, task.task_id)
            if identity in identities:
                raise ValueError("suite manifest repeats a task identity")
            identities.add(identity)
            if (
                task.provenance.dataset_id != task.dataset_id
                or task.provenance.split_id != task.split_id
                or task.provenance.session_id != task.session_id
            ):
                raise ValueError("suite task source provenance differs from its profile identity")
            if any(column.lower() in forbidden for column in task.columns):
                raise ValueError("suite manifest feature columns contain forbidden metadata")
            if task.provenance.usage_profile != "noncommercial_research":
                raise ValueError("suite task provenance has an unsupported usage profile")
            context_raw = dict(task.context)
            if set(context_raw) != {
                "seed",
                "signals",
                "regime_signals",
                "sampling_s",
                "time_budget_s",
            }:
                raise ValueError("suite task FitContext fields are incomplete or unexpected")
            context = FitContext(
                seed=_strict_int(context_raw["seed"], "seed"),
                signals=_strict_str_tuple(context_raw["signals"], "signals"),
                regime_signals=_strict_str_tuple(context_raw["regime_signals"], "regime_signals"),
                sampling_s=_strict_optional_positive_int(context_raw["sampling_s"], "sampling_s"),
                time_budget_s=_strict_finite(context_raw["time_budget_s"], "time_budget_s"),
            )
            if context.seed != 0 or context.signals != task.columns:
                raise ValueError(
                    "suite FitContext does not match the declared numeric feature columns"
                )
            profile = (
                connection.execute(
                    text(
                        "SELECT profile_sha256, visibility, sample_count, task_family "
                        "FROM scorer.dataset_profiles WHERE dataset_id=:dataset_id "
                        "AND split_id=:split_id AND session_id=:session_id"
                    ),
                    {
                        "dataset_id": task.dataset_id,
                        "split_id": task.split_id,
                        "session_id": task.session_id,
                    },
                )
                .mappings()
                .one_or_none()
            )
            if (
                profile is None
                or profile["visibility"] != "dev"
                or profile["profile_sha256"] != task.profile_sha256
                or int(profile["sample_count"]) != len(task.evaluation)
                or profile["task_family"] != task.family
            ):
                raise ValueError("suite task is not bound to a matching trusted dev profile")
            train = np.asarray(task.train, dtype=np.float64)
            evaluation = np.asarray(task.evaluation, dtype=np.float64)
            if train.ndim != 2 or evaluation.ndim != 2 or train.shape[1] != len(task.columns):
                raise ValueError("suite feature matrices do not match the approved columns")
            if (
                evaluation.shape[1] != len(task.columns)
                or not np.isfinite(train).all()
                or not np.isfinite(evaluation).all()
            ):
                raise ValueError("suite feature matrices must be finite and rectangular")
            if train.nbytes + evaluation.nbytes > 128 * 1024**2:
                raise ValueError("one suite task exceeds its 128 MiB feature limit")
            total_matrix_bytes += train.nbytes + evaluation.nbytes
            if total_matrix_bytes > MAX_SUITE_MATRIX_BYTES:
                raise ValueError("suite feature matrices exceed the aggregate 64 MiB limit")
            tasks.append(
                SuiteTask.from_frames(
                    task_id=task.task_id,
                    dataset_id=task.dataset_id,
                    split_id=task.split_id,
                    session_id=task.session_id,
                    profile_sha256=task.profile_sha256,
                    family=task.family,
                    task_weight=float(task.task_weight),
                    independent_family=task.independent_family,
                    label_tier=task.label_tier,
                    provenance=task.provenance,
                    context=context,
                    train=pd.DataFrame(train, columns=task.columns),
                    evaluation=pd.DataFrame(evaluation, columns=task.columns),
                    task_ids_for_guard=frozenset(item.task_id for item in document.tasks),
                )
            )
    return document, tuple(tasks), digest


def _strict_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"FitContext {name} must be an integer")
    return value


def _strict_optional_positive_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    result = _strict_int(value, name)
    if result < 1:
        raise ValueError(f"FitContext {name} must be positive when known")
    return result


def _strict_finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"FitContext {name} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"FitContext {name} must be finite")
    return result


def _strict_str_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"FitContext {name} must be a string array")
    return tuple(value)
