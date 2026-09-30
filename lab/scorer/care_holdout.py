"""Scorer-only adapter from the Farm B materializer to private holdout records.

No Director caller or public-suite manifest should import this module. It keeps
labels, event identities, timestamps, masks, and source hashes together for a
trusted privileged installation flow; the runtime Scorer role reads the installed
holdout records and cannot write profiles, labels, or semantics.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from harness.care_holdout import FarmBCareSuite, FarmBCareTask

if TYPE_CHECKING:
    from lab.scorer.care_calibration import CareCalibrationTask
    from lab.scorer.holdout import HoldoutTaskSpec


@dataclass(frozen=True, slots=True)
class CareFarmBTaskBinding:
    """Trusted preinstalled profile and frozen baseline normalization values."""

    profile_sha256: str
    task_weight: float
    base_score: float
    reference_score: float
    seed: int = 0
    time_budget_s: float = 60.0


@dataclass(frozen=True, slots=True)
class CareFarmBRegistration:
    """Complete private binding; never serialize to Director-facing objects."""

    task_spec: HoldoutTaskSpec
    labels: tuple[bool, ...]
    semantics: dict[str, object]
    semantics_sha256: str
    source_identity: str
    source_member: str
    source_member_sha256: str
    source_archive_sha256: str
    source_manifest_sha256: str
    usage_profile: str


def validate_care_farm_b_bindings(
    suite: FarmBCareSuite,
    bindings: Mapping[str, CareFarmBTaskBinding],
) -> tuple[tuple[FarmBCareTask, CareFarmBTaskBinding], ...]:
    """Validate exact trusted task/profile/calibration correspondence."""
    expected_ids = {task.task_id for task in suite.tasks}
    if not suite.tasks or set(bindings) != expected_ids:
        raise ValueError("CARE Farm B bindings must cover every suite task exactly")
    checked: list[tuple[FarmBCareTask, CareFarmBTaskBinding]] = []
    for task in suite.tasks:
        binding = bindings[task.task_id]
        if (
            re.fullmatch(r"[0-9a-f]{64}", binding.profile_sha256) is None
            or not math.isfinite(binding.task_weight)
            or binding.task_weight <= 0
            or not all(
                math.isfinite(value) for value in (binding.base_score, binding.reference_score)
            )
            or not 0 <= binding.base_score <= binding.reference_score <= 1
            or isinstance(binding.seed, bool)
            or not isinstance(binding.seed, int)
            or not math.isfinite(binding.time_budget_s)
            or binding.time_budget_s <= 0
        ):
            raise ValueError("CARE Farm B trusted profile/calibration binding is invalid")
        checked.append((task, binding))
    return tuple(checked)


def build_care_farm_b_registration_records(
    suite: FarmBCareSuite,
    *,
    bindings: Mapping[str, CareFarmBTaskBinding],
) -> tuple[CareFarmBRegistration, ...]:
    """Build typed Scorer task records from already pinned profile/calibration rows.

    The function does no database writes. A trusted privileged installation
    flow (database owner/migrator, outside runtime Scorer) must install profiles,
    labels, and semantics before the read-only Scorer role consumes the separate
    private holdout registry. We require an exact task-key mapping so no task can
    be silently skipped or substituted.
    """
    from harness.contracts import FitContext
    from lab.scorer.holdout import HoldoutTaskSpec

    checked = validate_care_farm_b_bindings(suite, bindings)
    records: list[CareFarmBRegistration] = []
    for task, binding in checked:
        context = FitContext(
            seed=binding.seed,
            signals=tuple(task.train_values.columns),
            regime_signals=(),
            sampling_s=task.sampling_period_s,
            time_budget_s=float(binding.time_budget_s),
        )
        spec = HoldoutTaskSpec(
            task_id=task.task_id,
            dataset_id=task.dataset_id,
            split_id=task.split_id,
            session_id=task.session_id,
            profile_sha256=binding.profile_sha256,
            family=task.family,
            task_weight=float(binding.task_weight),
            base_score=float(binding.base_score),
            reference_score=float(binding.reference_score),
            sliding_window=task.sliding_window,
            context=context,
            train=task.train_values.copy(deep=True),
            evaluation=task.evaluation_values.copy(deep=True),
        )
        if len(task.labels) != len(task.evaluation_values) or len(task.masked_samples) != len(
            task.evaluation_values
        ):
            raise ValueError("CARE Farm B private labels or masks differ from the evaluation grid")
        records.append(
            CareFarmBRegistration(
                task_spec=spec,
                labels=task.labels,
                semantics={
                    "task_family": task.family,
                    "sampling_s": task.sampling_period_s,
                    "evaluation_times": list(task.evaluation_times),
                    "masked_samples": list(task.masked_samples),
                    "failure_windows": [list(window) for window in task.failure_windows],
                },
                semantics_sha256=task.semantics_sha256,
                source_identity=task.source_identity,
                source_member=task.source_member,
                source_member_sha256=task.source_member_sha256,
                source_archive_sha256=task.source_archive_sha256,
                source_manifest_sha256=task.source_manifest_sha256,
                usage_profile=task.usage_profile,
            )
        )
    return tuple(records)


def build_care_baseline_calibration_tasks(
    suite: FarmBCareSuite,
    *,
    profile_sha256_by_task: Mapping[str, str],
    time_budget_s: float = 60.0,
) -> tuple[CareCalibrationTask, ...]:
    """Convert pinned Farm B matrices to label-free identity + private loader inputs.

    Profile hashes must come from the Scorer/Migrator installed profile readback.
    This deliberately does not accept base/reference scores: those exist only after
    the 135 measured receipts have been reduced.
    """
    import hashlib
    import json

    from harness.contracts import FitContext
    from lab.scorer.care_calibration import CareCalibrationTask
    from lab.scorer.holdout import _frame_bytes

    expected = {task.task_id for task in suite.tasks}
    if len(suite.tasks) != 15 or set(profile_sha256_by_task) != expected:
        raise ValueError("CARE calibration input profiles must bind exactly all 15 tasks")
    if not math.isfinite(time_budget_s) or time_budget_s <= 0:
        raise ValueError("CARE calibration trusted fit budget must be positive and finite")
    tasks: list[CareCalibrationTask] = []
    for task in suite.tasks:
        profile_sha = profile_sha256_by_task[task.task_id]
        signals = tuple(map(str, task.train_values.columns))
        context = FitContext(
            seed=0,
            signals=signals,
            regime_signals=(),
            sampling_s=task.sampling_period_s,
            time_budget_s=float(time_budget_s),
        )
        label_sha = hashlib.sha256(
            json.dumps(
                [int(value) for value in task.labels],
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("ascii")
        ).hexdigest()
        train_sha = hashlib.sha256(_frame_bytes(task.train_values, columns=signals)).hexdigest()
        evaluation_sha = hashlib.sha256(
            _frame_bytes(task.evaluation_values, columns=signals)
        ).hexdigest()
        task_key = hashlib.sha256(f"{suite.manifest_sha256}:{task.task_id}".encode()).hexdigest()[
            :32
        ]
        tasks.append(
            CareCalibrationTask(
                task_id=task.task_id,
                dataset_id=task.dataset_id,
                split_id=task.split_id,
                session_id=task.session_id,
                task_key=task_key,
                profile_sha256=profile_sha,
                family=task.family,
                train_sha256=train_sha,
                evaluation_sha256=evaluation_sha,
                labels_sha256=label_sha,
                semantics_sha256=task.semantics_sha256,
                source_member_sha256=task.source_member_sha256,
                source_archive_sha256=task.source_archive_sha256,
                source_manifest_sha256=task.source_manifest_sha256,
                sliding_window=task.sliding_window,
                sampling_s=task.sampling_period_s,
                context=context,
                train=task.train_values.copy(deep=True),
                evaluation=task.evaluation_values.copy(deep=True),
            )
        )
    return tuple(tasks)
