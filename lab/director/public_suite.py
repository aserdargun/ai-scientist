"""Materialize real public benchmark tasks and install Scorer-private labels."""

from __future__ import annotations

import hashlib
import json
import os
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
from sqlalchemy import Engine, insert, select

from harness.contracts import FitContext
from harness.public_data import (
    CAREPublicTask,
    derive_train_sliding_window,
    expand_gecco_tsb_source_grid,
    load_care_farm_a_task,
    load_skab_session,
    load_smd_machine,
    load_smd_manifest,
    load_source_manifest,
    load_tsb_curated_series,
    regularize_public_series,
    split_tsb_curated_prefix,
)
from lab.db.schema import dataset_labels, dataset_profiles, dataset_task_semantics
from lab.director.contracts import SourceProvenance
from lab.director.suite import SuiteTask
from lab.director.suite_weights import SuiteWeightInput, suite_weight_task_key, suite_weights

CARE_ARCHIVE_SHA256 = "ca61379e98956d891041ad45c885109bd8a14199fde0688d0184a11c2d4194f1"
CARE_ARCHIVE_BYTES = 5_503_439_673
CARE_LICENSE = "CC-BY-SA-4.0"
CARE_ATTRIBUTION = (
    "Gück, Roelofs, Faulstich, CARE to Compare, Data 9(12):138 (2024), "
    "https://doi.org/10.5281/zenodo.15846963"
)
CARE_ACCESS_TERMS = (
    "CC BY-SA 4.0; noncommercial research profile only; preserve attribution and share-alike "
    "terms for redistributed adaptations"
)
CARE_FEATURE_POLICY = "care_named_physical_averages_train_status_only.v3"
CARE_FEATURE_COLUMNS = (
    "wind_speed_3_avg",
    "wind_speed_4_avg",
    "reactive_power_27_avg",
    "reactive_power_28_avg",
    "power_29_avg",
    "power_30_avg",
)
CARE_SPLIT_POLICY = "care_Adev_train_healthy_embargo_1day_prediction_status_eval_ignored.v3"
PUBLIC_SUITE_ID = "public-ad-v1"
SCORER_LABEL_INSERT_BATCH_ROWS = 10_000


@dataclass(frozen=True, slots=True)
class ScorerTaskRegistration:
    """Trusted label and temporal semantics that are never passed to Director."""

    dataset_id: str
    split_id: str
    session_id: str
    sample_count: int
    sliding_window: int
    embargo_seconds: int
    profile_sha256: str
    task_family: Literal["EVT", "PDM", "NRM"]
    sampling_s: int | None
    labels: tuple[bool, ...]
    evaluation_times: tuple[str | int | float, ...]
    masked_samples: tuple[bool, ...]
    failure_windows: tuple[tuple[int, int], ...]
    semantics_sha256: str | None
    visibility: Literal["dev", "holdout", "sealed"]


@dataclass(frozen=True, slots=True)
class MaterializedPublicTask:
    """Director-visible features plus a separately installable Scorer record."""

    task: SuiteTask
    scorer_registration: ScorerTaskRegistration
    source_member: str
    train_start_time: str | None
    train_end_time: str | None
    embargo_seconds: int
    source_train_start_index: int | None = None
    source_train_end_index: int | None = None
    source_evaluation_start_index: int | None = None
    source_evaluation_end_index: int | None = None
    source_row_offset: int = 0
    time_axis_policy: str | None = None


@dataclass(frozen=True, slots=True)
class PublicSuiteWeightMetadata:
    """Post-water-fill allocations that must be sealed with a complete suite."""

    policy: str
    family_cap: float
    type_shares: tuple[tuple[str, float], ...]
    family_shares: tuple[tuple[str, float], ...]
    capped_families: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _TSBSourceConfig:
    name: Literal["Genesis", "GECCO", "CATSv2"]
    member: str
    source_family: str
    signal_type: str
    sampling_s: int | None
    source_row_offset: int
    time_axis: str
    license: str
    attribution: str
    tier: Literal["silver", "bronze"]
    review_sha: str


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def task_semantics_sha256(
    *,
    task_family: str,
    sampling_s: int,
    evaluation_times: tuple[str | int | float, ...],
    masked_samples: tuple[bool, ...],
    failure_windows: tuple[tuple[int, int], ...],
) -> str:
    """Return the immutable digest for Scorer-only task metadata."""
    payload = {
        "schema": "public-task-semantics.v1",
        "task_family": task_family,
        "sampling_s": sampling_s,
        "evaluation_times": evaluation_times,
        "masked_samples": masked_samples,
        "failure_windows": failure_windows,
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _profile_digest(
    *,
    identity: tuple[str, str, str],
    source_manifest_sha256: str,
    source_member_sha256: str,
    feature_policy: str,
    split_policy: str,
    task_family: str,
    sliding_window: int,
    train: pd.DataFrame,
    evaluation: pd.DataFrame,
    semantics_sha256: str,
) -> str:
    digest = hashlib.sha256()
    digest.update(
        _canonical_json(
            {
                "schema": "public-profile.v1",
                "identity": identity,
                "source_manifest_sha256": source_manifest_sha256,
                "source_member_sha256": source_member_sha256,
                "feature_policy": feature_policy,
                "split_policy": split_policy,
                "task_family": task_family,
                "sliding_window": sliding_window,
                "columns": tuple(str(name) for name in train.columns),
                "train_shape": train.shape,
                "evaluation_shape": evaluation.shape,
                "semantics_sha256": semantics_sha256,
                "usage_profile": "noncommercial_research",
            }
        )
    )
    for frame in (train, evaluation):
        values = np.asarray(frame.to_numpy(dtype="<f8", copy=True), dtype="<f8", order="C")
        digest.update(values.tobytes(order="C"))
    return digest.hexdigest()


def materialize_care_development(
    *,
    repository_root: Path,
    sliding_window: int | None = None,
) -> tuple[MaterializedPublicTask, ...]:
    """Build all and only Farm A CARE dev tasks from checksum-pinned source records.

    This function reads the source metadata and Farm A members. It never opens Farm B
    holdout or Farm C sealed members. The 22 task records are materialized one at a
    time; the 5.5 GB archive is not unpacked to a second tree.
    """
    evidence = repository_root / "docs/ai-scientist/review-evidence"
    source_manifest_path = evidence / "care-source-manifest.json"
    dev_review_path = evidence / "care-dev-clock-review.json"
    structure_path = evidence / "care-structure-review.json"
    for path in (source_manifest_path, dev_review_path, structure_path):
        if path.is_symlink() or not path.is_file():
            raise ValueError("CARE public-source evidence file is missing or unsafe")
    source_raw = source_manifest_path.read_bytes()
    source_manifest = json.loads(source_raw)
    source_manifest_sha = hashlib.sha256(source_raw).hexdigest()
    if (
        source_manifest.get("dataset") != "CARE to Compare"
        or source_manifest.get("sha256") != CARE_ARCHIVE_SHA256
        or source_manifest.get("archive_bytes") != CARE_ARCHIVE_BYTES
        or source_manifest.get("license") != "CC-BY-SA-4.0 (Zenodo metadata)"
        or source_manifest.get("record_id") != 15846963
    ):
        raise ValueError("CARE archive acquisition identity or license differs from evidence")
    readme_record = next(
        (
            item
            for item in source_manifest.get("descriptions", [])
            if item.get("member") == "CARE_To_Compare/README.txt"
        ),
        None,
    )
    if readme_record is None:
        raise ValueError("CARE source manifest has no source status-code documentation record")
    readme_path = repository_root / readme_record["local_path"]
    if readme_path.is_symlink() or not readme_path.is_file():
        raise ValueError("CARE source README is missing or unsafe")
    readme_raw = readme_path.read_bytes()
    if hashlib.sha256(readme_raw).hexdigest() != readme_record["sha256"]:
        raise ValueError("CARE source README hash differs from the acquisition manifest")
    for documented_status in (
        "0: Normal Operation",
        "2: Idling",
        "3: Service",
        "4: Downtime",
        "5: Other",
    ):
        if documented_status not in readme_raw.decode("utf-8", errors="strict"):
            raise ValueError("CARE status-code mask policy is not present in pinned source docs")
    archive_path = repository_root / source_manifest["archive"]
    if (
        archive_path.is_symlink()
        or not archive_path.is_file()
        or archive_path.stat().st_size != CARE_ARCHIVE_BYTES
    ):
        raise ValueError("CARE public archive path or byte size differs from frozen evidence")
    dev_review = json.loads(dev_review_path.read_bytes())
    structure = json.loads(structure_path.read_bytes())
    if dev_review.get("archive_sha256_previously_verified") != CARE_ARCHIVE_SHA256:
        raise ValueError("CARE Farm A review is not bound to the pinned archive")
    farm_a = next((farm for farm in structure.get("farms", []) if farm.get("farm") == "A"), None)
    if (
        farm_a is None
        or farm_a.get("dataset_files") != 22
        or farm_a.get("metadata_sha256")
        != "9254667b70d73e8eb23e7143ad56f1c280ba020c8b3d9289ad56fcaa59270aa5"
    ):
        raise ValueError("CARE Farm A source structure evidence differs from the pinned record")
    member_records = dev_review.get("files")
    if not isinstance(member_records, list) or len(member_records) != 22:
        raise ValueError("CARE Farm A dev review must list exactly 22 source files")
    event_member = "CARE_To_Compare/Wind Farm A/event_info.csv"
    with zipfile.ZipFile(archive_path) as archive:
        event_info = archive.read(event_member)
    if hashlib.sha256(event_info).hexdigest() != farm_a["metadata_sha256"]:
        raise ValueError("CARE Farm A event metadata hash differs from the pinned evidence")

    loaded: list[tuple[CAREPublicTask, str]] = []
    for record in sorted(member_records, key=lambda item: item["member"]):
        member = record.get("member")
        if not isinstance(member, str) or not member.startswith(
            "CARE_To_Compare/Wind Farm A/datasets/"
        ):
            raise ValueError("CARE development manifest contains a non-A dataset member")
        task = load_care_farm_a_task(
            archive_path,
            member=member,
            event_info_csv=event_info,
            expected_member_sha256=record["sha256"],
            expected_member_bytes=record["bytes"],
            source_revision="zenodo:15846963:v6",
            source_license=CARE_LICENSE,
            source_manifest_sha256=source_manifest_sha,
            attribution=CARE_ATTRIBUTION,
            access_terms=CARE_ACCESS_TERMS,
            sliding_window=sliding_window,
            feature_columns=CARE_FEATURE_COLUMNS,
        )
        loaded.append((task, member))
    if (
        sum(task.family == "PDM" for task, _member in loaded) != 12
        or sum(task.family == "NRM" for task, _member in loaded) != 10
    ):
        raise ValueError("CARE Farm A metadata must resolve to 12 PDM and 10 NRM dev tasks")
    family_counts = {
        family: sum(task.family == family for task, _member in loaded) for family in ("PDM", "NRM")
    }
    materialized: list[MaterializedPublicTask] = []
    for care_task, member in loaded:
        semantics_sha = task_semantics_sha256(
            task_family=care_task.family,
            sampling_s=care_task.sampling_period_s,
            evaluation_times=care_task.evaluation_times,
            masked_samples=care_task.masked_samples,
            failure_windows=care_task.failure_windows,
        )
        member_sha = next(
            record["sha256"] for record in member_records if record["member"] == member
        )
        profile_sha = _profile_digest(
            identity=(care_task.dataset_id, care_task.split_id, care_task.session_id),
            source_manifest_sha256=source_manifest_sha,
            source_member_sha256=member_sha,
            feature_policy=CARE_FEATURE_POLICY,
            split_policy=CARE_SPLIT_POLICY,
            task_family=care_task.family,
            train=care_task.train_values,
            evaluation=care_task.evaluation_values,
            sliding_window=care_task.sliding_window,
            semantics_sha256=semantics_sha,
        )
        provenance = SourceProvenance(
            dataset_id=care_task.dataset_id,
            split_id=care_task.split_id,
            session_id=care_task.session_id,
            source_manifest_sha256=care_task.source_manifest_sha256,
            source_revision=care_task.source_revision,
            license_id=care_task.source_license,
            attribution=care_task.attribution,
            access_terms=care_task.access_terms,
            usage_profile=care_task.usage_profile,
        )
        columns = tuple(str(name) for name in care_task.train_values.columns)
        suite_task = SuiteTask.from_frames(
            task_id=care_task.task_id,
            dataset_id=care_task.dataset_id,
            split_id=care_task.split_id,
            session_id=care_task.session_id,
            profile_sha256=profile_sha,
            family=care_task.family,
            task_weight=(0.3 if care_task.family == "PDM" else 0.2)
            / family_counts[care_task.family],
            independent_family=(f"care-wind-farm-telemetry:{care_task.family}"),
            label_tier="silver",
            provenance=provenance,
            context=FitContext(
                seed=0,
                signals=columns,
                regime_signals=tuple(name for name in columns if name.startswith("wind_speed_")),
                sampling_s=care_task.sampling_period_s,
                time_budget_s=60.0,
            ),
            train=care_task.train_values,
            evaluation=care_task.evaluation_values,
            task_ids_for_guard=frozenset(
                {
                    f"{care_task.dataset_id}:{care_task.split_id}:{care_task.session_id}:{care_task.task_id}"
                }
            ),
            evaluation_instants=care_task.evaluation_times,
        )
        registration = ScorerTaskRegistration(
            dataset_id=care_task.dataset_id,
            split_id=care_task.split_id,
            session_id=care_task.session_id,
            sample_count=len(care_task.labels),
            sliding_window=care_task.sliding_window,
            embargo_seconds=care_task.embargo_seconds,
            profile_sha256=profile_sha,
            task_family=care_task.family,
            sampling_s=care_task.sampling_period_s,
            labels=care_task.labels,
            evaluation_times=care_task.evaluation_times,
            masked_samples=care_task.masked_samples,
            failure_windows=care_task.failure_windows,
            semantics_sha256=semantics_sha,
            visibility="dev",
        )
        materialized.append(
            MaterializedPublicTask(
                task=suite_task,
                scorer_registration=registration,
                source_member=member,
                train_start_time=care_task.train_times[0],
                train_end_time=care_task.train_times[-1],
                embargo_seconds=care_task.embargo_seconds,
            )
        )
    return tuple(materialized)


def materialize_skab_development(*, repository_root: Path) -> tuple[MaterializedPublicTask, ...]:
    """Materialize pinned SKAB valve1/0 with a source-order, label-blind split."""
    evidence = repository_root / "docs/ai-scientist/review-evidence"
    manifest_path = evidence / "skab-source-manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("SKAB source manifest is missing or unsafe")
    source_raw = manifest_path.read_bytes()
    manifest = load_source_manifest(manifest_path)
    if (
        manifest.dataset != "SKAB"
        or manifest.revision != "b2c0d46c2971dcbfe71e26087b6d231998bb91c2"
        or manifest.repository_license != "GPL-3.0 (upstream LICENSE)"
    ):
        raise ValueError("SKAB pinned revision or source license differs from reviewed source")
    relative = "data/public/skab/b2c0d46c2971dcbfe71e26087b6d231998bb91c2/data/valve1/0.csv"
    series = load_skab_session(manifest, relative, repository_root)
    boundary = int(len(series.values) * 0.6)
    if boundary < 20 or len(series.values) - boundary < 20:
        raise ValueError("SKAB source session is too short for the fixed prefix split")
    grid = regularize_public_series(series, train_stop_index=boundary, max_forward_fill_steps=3)
    boundary = int(len(grid.values) * 0.6)
    observed_train = np.flatnonzero(~grid.masked[:boundary])
    train_runs: list[tuple[int, int]] = []
    if observed_train.size:
        start = previous = int(observed_train[0])
        for raw_index in observed_train[1:]:
            current = int(raw_index)
            if current != previous + 1:
                train_runs.append((start, previous + 1))
                start = current
            previous = current
        train_runs.append((start, previous + 1))
    train_start, train_end = max(
        train_runs, key=lambda run: (run[1] - run[0], -run[0])
    )
    train_values = grid.values.iloc[train_start:train_end].reset_index(drop=True)
    window = derive_train_sliding_window(train_values.to_numpy(dtype=np.float64))
    evaluation_start = boundary + window
    if train_end - train_start < window + 1 or len(grid.values) - evaluation_start < window:
        raise ValueError("SKAB fixed train-derived embargo leaves insufficient samples")
    eval_values = grid.values.iloc[evaluation_start:].reset_index(drop=True)
    times = tuple(grid.timestamps_original[evaluation_start:])
    train_times = tuple(grid.timestamps_original[train_start:train_end])
    sampling_s = grid.sampling_period_s
    labels = tuple(bool(value) for value in grid.labels[evaluation_start:])
    masks = tuple(bool(value) for value in grid.masked[evaluation_start:])
    if any(label and mask for label, mask in zip(labels, masks, strict=True)):
        raise ValueError("SKAB positives cannot occur on reconstructed missing grid rows")
    split_id = "public-benchmark-fixed-source-prefix.v1"
    embargo_seconds = (evaluation_start - train_end) * sampling_s
    semantics_sha = task_semantics_sha256(
        task_family="EVT",
        sampling_s=sampling_s,
        evaluation_times=times,
        masked_samples=masks,
        failure_windows=(),
    )
    manifest_sha = hashlib.sha256(source_raw).hexdigest()
    source_record = next(item for item in manifest.files if item.path == relative)
    profile_sha = _profile_digest(
        identity=("SKAB", split_id, series.session_id),
        source_manifest_sha256=manifest_sha,
        source_member_sha256=source_record.sha256,
        feature_policy="skab-manifest-sensors.v1",
        split_policy="public-benchmark-fixed-source-prefix-window-embargo.v1",
        task_family="EVT",
        sliding_window=window,
        train=train_values,
        evaluation=eval_values,
        semantics_sha256=semantics_sha,
    )
    provenance = SourceProvenance(
        dataset_id="SKAB",
        split_id=split_id,
        session_id=series.session_id,
        source_manifest_sha256=manifest_sha,
        source_revision=manifest.revision,
        license_id="GPL-3.0",
        attribution="Gavriil et al., SKAB: A Benchmark for Anomaly Detection in Time Series",
        access_terms="GPL-3.0; noncommercial research profile",
        usage_profile="noncommercial_research",
    )
    columns = tuple(str(column) for column in train_values.columns)
    task_id = "valve1-0-fixed-source-session"
    suite_task = SuiteTask.from_frames(
        task_id=task_id,
        dataset_id="SKAB",
        split_id=split_id,
        session_id=series.session_id,
        profile_sha256=profile_sha,
        family="EVT",
        task_weight=0.5,
        independent_family="SKAB:industrial-process-telemetry:EVT",
        label_tier="silver",
        provenance=provenance,
        context=FitContext(
            seed=0,
            signals=columns,
            regime_signals=tuple(
                name for name in columns if name in {"Temperature", "Current", "Pressure"}
            ),
            sampling_s=sampling_s,
            time_budget_s=60.0,
        ),
        train=train_values,
        evaluation=eval_values,
        task_ids_for_guard=frozenset({f"SKAB:{series.session_id}:{task_id}"}),
        evaluation_instants=times,
    )
    registration = ScorerTaskRegistration(
        dataset_id="SKAB",
        split_id=split_id,
        session_id=series.session_id,
        sample_count=len(labels),
        sliding_window=window,
        embargo_seconds=embargo_seconds,
        profile_sha256=profile_sha,
        task_family="EVT",
        sampling_s=sampling_s,
        labels=labels,
        evaluation_times=times,
        masked_samples=masks,
        failure_windows=(),
        semantics_sha256=semantics_sha,
        visibility="dev",
    )
    return (
        MaterializedPublicTask(
            task=suite_task,
            scorer_registration=registration,
            source_member=relative,
            train_start_time=train_times[0],
            train_end_time=train_times[-1],
            embargo_seconds=embargo_seconds,
            source_train_start_index=train_start,
            source_train_end_index=train_end,
            source_evaluation_start_index=evaluation_start,
            source_evaluation_end_index=len(grid.values),
            time_axis_policy=(
                "fixed-60-percent-source-prefix;train-derived-window-sample-embargo;"
                "original-naive-time-kept-scorer-side"
            ),
        ),
    )


def materialize_smd_development(
    *,
    repository_root: Path,
    entity_limit: int = 1,
    train_rows: int = 12_000,
    evaluation_rows: int = 12_000,
) -> tuple[MaterializedPublicTask, ...]:
    """Materialize SMD's official per-machine train/test telemetry splits.

    Only train, test and test-label source files are opened by the loader. The
    separate interpretation labels are provenance-only and are never consumed.
    The source paper supports a 60-second relative sample axis, with no invented
    absolute timestamp origin.
    """
    evidence = repository_root / "docs/ai-scientist/review-evidence"
    manifest_path = evidence / "smd-source-manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("SMD source manifest is missing or unsafe")
    source_raw = manifest_path.read_bytes()
    manifest = load_smd_manifest(manifest_path)
    manifest_sha = hashlib.sha256(source_raw).hexdigest()
    entities = sorted({str(record.entity) for record in manifest.source_files if record.entity})
    if len(entities) != 28 or manifest.license != "MIT":
        raise ValueError("SMD must contain the pinned 28-machine MIT source manifest")
    if isinstance(entity_limit, bool) or not isinstance(entity_limit, int) or entity_limit != 1:
        raise ValueError("SMD public-suite uses one predeclared lexical-first entity")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 12_000
        for value in (train_rows, evaluation_rows)
    ):
        raise ValueError(
            "SMD selected official window rows must be within the fixed 1..12000 bound"
        )
    # Select by pinned entity name before opening any test-label file. This
    # lexical prefix is deterministic and independent of test outcomes.
    selected_entities = entities[:entity_limit]
    materialized: list[MaterializedPublicTask] = []
    for entity in selected_entities:
        # A one-sample placeholder passes the source loader's dimensional check;
        # the frozen VUS window is derived immediately from official train rows.
        source_task = load_smd_machine(manifest, repository_root, entity, sliding_window=1)
        # Selection starts only after the entity/window bounds have been frozen;
        # the official labels are sliced after feature selection and never guide it.
        if (
            len(source_task.train_values) < train_rows
            or len(source_task.eval_values) < evaluation_rows
        ):
            raise ValueError("SMD official split is shorter than the frozen suite window")
        # Use fixed contiguous suffix windows from the official train/test roles.
        # Bounds are frozen before reading label values, and original test indices
        # remain attached to the scorer semantics/time axis.
        train_values = source_task.train_values.iloc[-train_rows:].reset_index(drop=True)
        evaluation_values = source_task.eval_values.iloc[-evaluation_rows:].reset_index(drop=True)
        eval_labels = source_task.eval_labels[-evaluation_rows:]
        selected_train_start = len(source_task.train_values) - train_rows
        selected_eval_start = len(source_task.eval_values) - evaluation_rows
        window = derive_train_sliding_window(train_values.to_numpy(dtype=np.float64))
        source_records = sorted(
            (
                record
                for record in manifest.source_files
                if record.entity == entity and record.source_split != "interpretation_label"
            ),
            key=lambda record: record.path,
        )
        member_sha = hashlib.sha256(
            _canonical_json(
                {
                    "source_files": tuple(
                        (record.path, record.sha256) for record in source_records
                    ),
                    "selection": {
                        "policy": "lexical-first-entity-official-split-suffix.v1",
                        "train_indices": [selected_train_start, len(source_task.train_values)],
                        "evaluation_indices": [selected_eval_start, len(source_task.eval_values)],
                    },
                }
            )
        ).hexdigest()
        dataset_id = "SMD"
        split_id = "official-train-test"
        split_id = f"official-tail-{train_rows}-{evaluation_rows}.v1"
        identity = (dataset_id, split_id, source_task.session_id)
        selected_indices = source_task.eval_indices[-evaluation_rows:]
        times = tuple(index * int(source_task.sample_period_s) for index in selected_indices)
        masks = tuple(False for _ in selected_indices)
        semantics_sha = task_semantics_sha256(
            task_family="EVT",
            sampling_s=int(source_task.sample_period_s),
            evaluation_times=times,
            masked_samples=masks,
            failure_windows=(),
        )
        profile_sha = _profile_digest(
            identity=identity,
            source_manifest_sha256=manifest_sha,
            source_member_sha256=member_sha,
            feature_policy="smd-38-ordered-sensors.v1",
            split_policy="smd-official-machine-train-test.v1",
            task_family="EVT",
            sliding_window=window,
            train=train_values,
            evaluation=evaluation_values,
            semantics_sha256=semantics_sha,
        )
        provenance = SourceProvenance(
            dataset_id=dataset_id,
            split_id=split_id,
            session_id=source_task.session_id,
            source_manifest_sha256=manifest_sha,
            source_revision=source_task.source_revision,
            license_id="MIT",
            attribution=(
                "Su et al., Robust Anomaly Detection for Multivariate Time Series through "
                "Stochastic Recurrent Neural Network, KDD 2019, OmniAnomaly supplementary SMD"
            ),
            access_terms=(
                "MIT; noncommercial research profile; selection is lexical-first pinned entity, "
                f"official split suffixes train[{selected_train_start}:] and "
                f"test[{selected_eval_start}:] before labels are read"
            ),
            usage_profile="noncommercial_research",
        )
        task_id = entity.replace("/", "-")
        canonical_guard_id = f"{dataset_id}:{split_id}:{source_task.session_id}:{task_id}"
        suite_task = SuiteTask.from_frames(
            task_id=task_id,
            dataset_id=dataset_id,
            split_id=split_id,
            session_id=source_task.session_id,
            profile_sha256=profile_sha,
            family="EVT",
            task_weight=0.5,
            independent_family="SMD:server-telemetry:EVT",
            label_tier="silver",
            provenance=provenance,
            context=FitContext(
                seed=0,
                signals=tuple(str(column) for column in source_task.train_values.columns),
                regime_signals=(),
                sampling_s=int(source_task.sample_period_s),
                time_budget_s=60.0,
            ),
            train=train_values,
            evaluation=evaluation_values,
            task_ids_for_guard=frozenset({canonical_guard_id}),
            evaluation_instants=times,
        )
        registration = ScorerTaskRegistration(
            dataset_id=dataset_id,
            split_id=split_id,
            session_id=source_task.session_id,
            sample_count=len(eval_labels),
            sliding_window=window,
            embargo_seconds=0,
            profile_sha256=profile_sha,
            task_family="EVT",
            sampling_s=int(source_task.sample_period_s),
            labels=tuple(bool(value) for value in eval_labels),
            evaluation_times=times,
            masked_samples=masks,
            failure_windows=(),
            semantics_sha256=semantics_sha,
            visibility="dev",
        )
        materialized.append(
            MaterializedPublicTask(
                task=suite_task,
                scorer_registration=registration,
                source_member=entity,
                train_start_time=None,
                train_end_time=None,
                embargo_seconds=0,
            )
        )
    return tuple(materialized)


def materialize_tsb_development(
    *, repository_root: Path, evaluation_rows: int | None = None
) -> tuple[MaterializedPublicTask, ...]:
    """Build the frozen Genesis, GECCO and CATSv2 TSB-AD-M tasks.

    Each task is selected by a fixed curated archive member. The source `_tr_`
    prefix defines training; window derivation reads training sensor values only,
    then an equal sample-count embargo is applied. The `_1st_` token and labels do
    not select or move any boundary. GECCO is expanded back onto its original CSV
    row grid, including masked non-finite rows and the documented local clock reset.
    """
    evidence = repository_root / "docs/ai-scientist/review-evidence"
    tsb_manifest_path = evidence / "tsb-archive-manifest.json"
    if tsb_manifest_path.is_symlink() or not tsb_manifest_path.is_file():
        raise ValueError("TSB archive manifest is missing or unsafe")
    manifest_raw = tsb_manifest_path.read_bytes()
    manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
    archive_manifest = json.loads(manifest_raw)
    if (
        archive_manifest.get("dataset") != "TSB-AD-M"
        or archive_manifest.get("sha256")
        != "7de86ac27f30eeb48d833bb061055670e3f3de07defd995cf2bd5db10ccc9a0d"
    ):
        raise ValueError("TSB manifest differs from frozen archive source")

    genesis_original_path = evidence / "genesis-original-source.json"
    cats_original_path = evidence / "cats-original-source.json"
    cats_mapping_path = evidence / "cats-curated-mapping-review.json"
    gecco_review_path = evidence / "gecco-source-clock-review.json"
    for path in (genesis_original_path, cats_original_path, cats_mapping_path, gecco_review_path):
        if path.is_symlink() or not path.is_file():
            raise ValueError("one or more TSB original-source mapping records are missing")
    genesis_original = json.loads(genesis_original_path.read_bytes())
    cats_original = json.loads(cats_original_path.read_bytes())
    cats_mapping_raw = cats_mapping_path.read_bytes()
    cats_mapping = json.loads(cats_mapping_raw)
    if (
        genesis_original.get("license") != "CC-BY-NC-SA-4.0 (official Kaggle metadata)"
        or genesis_original.get("usage_profile") != "noncommercial_research"
        or genesis_original.get("tsb_crosscheck", {}).get("timestamp_units")
        != "numeric source Timestamp; unit interpretation still requires source documentation"
        or cats_original.get("record_id") != 8338435
        or cats_original.get("license_metadata", {}).get("id") != "cc-by-4.0"
        or cats_original.get("sha256")
        != "259b5849a134e3318ed5e683bca0582d8c1005d1a008bb86094c8131ca1b2ff3"
        or cats_mapping.get("source_sha256") != cats_original["sha256"]
        or cats_mapping.get("clock_mapping_verified") is not True
    ):
        raise ValueError("TSB source license or original-row mapping evidence is invalid")
    tsb_root = repository_root / archive_manifest["archive"]
    if tsb_root.is_symlink() or not tsb_root.is_file():
        raise ValueError("pinned TSB archive is missing or unsafe")

    configs = (
        _TSBSourceConfig(
            name="Genesis",
            member="TSB-AD-M/001_Genesis_id_1_Sensor_tr_4055_1st_15538.csv",
            source_family="industrial-process",
            signal_type="industrial-process-sensors",
            sampling_s=None,
            source_row_offset=0,
            time_axis="ordered-row-index; original numeric Timestamp unit undocumented",
            license="CC-BY-NC-SA-4.0",
            attribution="Kaggle init-owl, Genesis Demonstrator Data for Machine Learning",
            tier="silver",
            review_sha=hashlib.sha256(genesis_original_path.read_bytes()).hexdigest(),
        ),
        _TSBSourceConfig(
            name="GECCO",
            member="TSB-AD-M/173_GECCO_id_1_Sensor_tr_16165_1st_16265.csv",
            source_family="industrial-water-quality",
            signal_type="water-quality-sensors",
            sampling_s=60,
            source_row_offset=0,
            time_axis=(
                "original-order; 60s nominal; one source clock reset masked; timezone unspecified"
            ),
            license="CC-BY-4.0",
            attribution="Moritz et al., GECCO Industrial Challenge 2018 Dataset, Zenodo 3884398",
            tier="silver",
            review_sha=hashlib.sha256(gecco_review_path.read_bytes()).hexdigest(),
        ),
        _TSBSourceConfig(
            name="CATSv2",
            member="TSB-AD-M/138_CATSv2_id_1_Sensor_tr_16568_1st_16668.csv",
            source_family="simulated-dynamical-system",
            signal_type="simulated-telemetry",
            sampling_s=1,
            source_row_offset=4_900_000,
            time_axis="CATSv2 source-relative 1Hz row index; source timezone unspecified",
            license="CC-BY-4.0",
            attribution=(
                "Fleith, Controlled Anomalies Time Series (CATS) Dataset v2, Zenodo 8338435"
            ),
            tier="bronze",
            review_sha=hashlib.sha256(cats_mapping_raw).hexdigest(),
        ),
    )
    materialized: list[MaterializedPublicTask] = []
    for config in configs:
        series = load_tsb_curated_series(
            manifest_path=tsb_manifest_path,
            repository_root=repository_root,
            member=config.member,
            source_family=config.source_family,
            signal_type=config.signal_type,
            sampling_period_s=config.sampling_s,
            source_row_offset=config.source_row_offset,
            time_axis_policy=config.time_axis,
        )
        if config.name == "GECCO":
            series = expand_gecco_tsb_source_grid(
                series,
                repository_root=repository_root,
                review_path=gecco_review_path,
            )
        split = split_tsb_curated_prefix(series, evaluation_rows=evaluation_rows)
        labels = tuple(bool(value) for value in split.evaluation_labels)
        if len(labels) != len(split.evaluation_values):
            raise ValueError("TSB evaluation labels no longer align with source-grid values")
        family: Literal["EVT"] = "EVT"
        dataset_id = f"TSB-AD-M-{config.name}"
        split_id = (
            "curated-prefix-window-embargo.v2"
            if evaluation_rows is None
            else f"curated-prefix-window-embargo-tail-{evaluation_rows}.v1"
        )
        task_id = f"{config.name.lower()}-id-1"
        session_id = f"{series.archive_sha256}:{series.member}"
        source_binding = hashlib.sha256(
            _canonical_json(
                {
                    "archive_manifest_sha256": manifest_sha,
                    "member_sha256": series.member_sha256,
                    "member_crc32": series.member_crc32,
                    "source_review_sha256": config.review_sha,
                    "member": series.member,
                    "train_stop": series.curated_train_stop,
                    "selection_policy": (
                        "tsb-full-source-evaluation-train-prefix-derived-window-embargo.v2"
                        if evaluation_rows is None
                        else "tsb-curated-prefix-fixed-evaluation-tail.v1"
                    ),
                    "train_indices": split.train_indices,
                    "evaluation_indices": split.evaluation_indices,
                    "embargo_samples": split.embargo_samples,
                    "time_axis_policy": series.time_axis_policy,
                }
            )
        ).hexdigest()
        semantics_sha = (
            task_semantics_sha256(
                task_family=family,
                sampling_s=config.sampling_s,
                evaluation_times=split.evaluation_instants,
                masked_samples=split.masked_samples,
                failure_windows=(),
            )
            if config.sampling_s is not None
            else None
        )
        profile_binding = semantics_sha or hashlib.sha256(
            _canonical_json(
                {
                    "sampling_s": None,
                    "time_axis_policy": series.time_axis_policy,
                    "evaluation_instants": split.evaluation_instants,
                    "masked": split.masked_samples,
                }
            )
        ).hexdigest()
        profile_sha = _profile_digest(
            identity=(dataset_id, split_id, session_id),
            source_manifest_sha256=source_binding,
            source_member_sha256=series.member_sha256,
            feature_policy=(
                "tsb-member-ordered-sensor-columns-label-stripped.v2"
                if evaluation_rows is None
                else "tsb-member-ordered-sensor-columns-label-stripped.v1"
            ),
            split_policy=(
                "tsb-full-evaluation-curated-train-prefix-derived-window-source-order-embargo.v2"
                if evaluation_rows is None
                else (
                    "tsb-curated-train-prefix-derived-window-embargo-fixed-tail-"
                    f"{evaluation_rows}.v1"
                )
            ),
            task_family=family,
            sliding_window=split.sliding_window,
            train=split.train_values,
            evaluation=split.evaluation_values,
            semantics_sha256=profile_binding,
        )
        provenance = SourceProvenance(
            dataset_id=dataset_id,
            split_id=split_id,
            session_id=session_id,
            source_manifest_sha256=source_binding,
            source_revision=series.archive_sha256,
            license_id=config.license,
            attribution=config.attribution,
            access_terms=(
                "Source license applies; this installation is noncommercial research only. "
                "Original source clock/timestamp limitations remain disclosed in task metadata."
            ),
            usage_profile="noncommercial_research",
        )
        columns = tuple(str(column) for column in split.train_values.columns)
        suite_task = SuiteTask.from_frames(
            task_id=task_id,
            dataset_id=dataset_id,
            split_id=split_id,
            session_id=session_id,
            profile_sha256=profile_sha,
            family=family,
            task_weight=0.5,
            independent_family=(
                f"tsb-{config.name.lower()}:{config.signal_type}:EVT"
            ),
            label_tier=config.tier,
            provenance=provenance,
            context=FitContext(
                seed=0,
                signals=columns,
                regime_signals=(),
                sampling_s=config.sampling_s,
                time_budget_s=60.0,
            ),
            train=split.train_values,
            evaluation=split.evaluation_values,
            task_ids_for_guard=frozenset(
                {f"{dataset_id}:{split_id}:{session_id}:{task_id}"}
            ),
            evaluation_instants=split.evaluation_instants,
        )
        registration = ScorerTaskRegistration(
            dataset_id=dataset_id,
            split_id=split_id,
            session_id=session_id,
            sample_count=len(labels),
            sliding_window=split.sliding_window,
            embargo_seconds=(
                split.embargo_samples * int(config.sampling_s)
                if config.sampling_s is not None
                else 0
            ),
            profile_sha256=profile_sha,
            task_family=family,
            sampling_s=config.sampling_s,
            labels=labels,
            evaluation_times=split.evaluation_instants,
            masked_samples=split.masked_samples,
            failure_windows=(),
            semantics_sha256=semantics_sha,
            visibility="dev",
        )
        materialized.append(
            MaterializedPublicTask(
                task=suite_task,
                scorer_registration=registration,
                source_member=series.member,
                train_start_time=None,
                train_end_time=None,
                embargo_seconds=registration.embargo_seconds,
                source_evaluation_start_index=(
                    series.source_row_offset + split.evaluation_indices[0]
                ),
                source_evaluation_end_index=(
                    series.source_row_offset + split.evaluation_indices[-1] + 1
                ),
                source_train_start_index=series.source_row_offset + split.train_indices[0],
                source_train_end_index=series.source_row_offset + split.train_indices[-1] + 1,
                source_row_offset=series.source_row_offset,
                time_axis_policy=series.time_axis_policy,
            )
        )
    return tuple(materialized)


def finalize_public_suite_weights(
    tasks: tuple[MaterializedPublicTask, ...],
) -> tuple[tuple[MaterializedPublicTask, ...], PublicSuiteWeightMetadata]:
    """Apply Appendix C type/tier weights and the independent-family water-fill.

    `independent_family` is explicitly source × signal type × task type. Extra
    files/entities from one source share its capped family allocation. The suite
    cannot be finalized until four distinct independent families are present.
    """
    if not tasks:
        raise ValueError("public suite cannot be empty")
    identities = [
        (item.task.dataset_id, item.task.split_id, item.task.session_id, item.task.task_id)
        for item in tasks
    ]
    if len(identities) != len(set(identities)):
        raise ValueError("public suite repeats a task identity")
    records = tuple(
        SuiteWeightInput(
            task_id=suite_weight_task_key(identity),
            task_type=item.task.family,
            label_tier=item.task.label_tier,
            family=item.task.independent_family,
        )
        for identity, item in zip(identities, tasks, strict=True)
    )
    weights = suite_weights(records)
    weighted = tuple(
        replace(item, task=replace(item.task, task_weight=weight))
        for item, weight in zip(tasks, weights.weights, strict=True)
    )
    metadata = PublicSuiteWeightMetadata(
        policy="spec-3.2.5-appendix-c-waterfill.v1",
        family_cap=weights.family_cap,
        type_shares=tuple(sorted(weights.type_shares.items())),
        family_shares=tuple(sorted(weights.family_shares.items())),
        capped_families=weights.capped_families,
    )
    return weighted, metadata


def install_scorer_task(engine: Engine, registration: ScorerTaskRegistration) -> None:
    """Install one profile/label/semantics bundle idempotently with exact readback."""
    if (
        registration.sample_count < 1
        or len(registration.labels) != registration.sample_count
        or any(not isinstance(label, bool) for label in registration.labels)
        or len(registration.masked_samples) != registration.sample_count
        or len(registration.evaluation_times) != registration.sample_count
        or any(not isinstance(masked, bool) for masked in registration.masked_samples)
    ):
        raise ValueError("public scorer registration labels and axis are misaligned")
    semantics: dict[str, object] | None = None
    if registration.sampling_s is None:
        if (
            registration.task_family != "EVT"
            or registration.semantics_sha256 is not None
            or registration.failure_windows
            or any(registration.masked_samples)
        ):
            raise ValueError("unknown source cadence supports unmasked EVT only")
    else:
        if (
            isinstance(registration.sampling_s, bool)
            or not isinstance(registration.sampling_s, int)
            or registration.sampling_s < 1
            or registration.semantics_sha256 is None
        ):
            raise ValueError("known source cadence needs a positive integer and semantics digest")
        semantics = {
            "dataset_id": registration.dataset_id,
            "split_id": registration.split_id,
            "session_id": registration.session_id,
            "task_family": registration.task_family,
            "sampling_s": registration.sampling_s,
            "evaluation_times_json": list(registration.evaluation_times),
            "masked_samples_json": list(registration.masked_samples),
            "failure_windows_json": [list(window) for window in registration.failure_windows],
            "semantics_sha256": registration.semantics_sha256,
        }
        expected_digest = task_semantics_sha256(
            task_family=registration.task_family,
            sampling_s=registration.sampling_s,
            evaluation_times=registration.evaluation_times,
            masked_samples=registration.masked_samples,
            failure_windows=registration.failure_windows,
        )
        if expected_digest != registration.semantics_sha256:
            raise ValueError("public task semantics digest differs from its canonical content")
    with engine.begin() as connection:
        key = {
            "dataset_id": registration.dataset_id,
            "split_id": registration.split_id,
            "session_id": registration.session_id,
        }
        existing = (
            connection.execute(
                select(dataset_profiles).where(
                    dataset_profiles.c.dataset_id == key["dataset_id"],
                    dataset_profiles.c.split_id == key["split_id"],
                    dataset_profiles.c.session_id == key["session_id"],
                )
            )
            .mappings()
            .one_or_none()
        )
        profile_values = {
            **key,
            "sample_count": registration.sample_count,
            "sliding_window": registration.sliding_window,
            "profile_sha256": registration.profile_sha256,
            "task_family": registration.task_family,
            "visibility": registration.visibility,
        }
        if existing is None:
            connection.execute(insert(dataset_profiles).values(**profile_values))
            for batch_start in range(0, registration.sample_count, SCORER_LABEL_INSERT_BATCH_ROWS):
                batch_stop = min(
                    batch_start + SCORER_LABEL_INSERT_BATCH_ROWS, registration.sample_count
                )
                connection.execute(
                    insert(dataset_labels),
                    [
                        {
                            **key,
                            "sample_index": index,
                            "is_anomaly": registration.labels[index],
                        }
                        for index in range(batch_start, batch_stop)
                    ],
                )
            if semantics is not None:
                connection.execute(insert(dataset_task_semantics).values(**semantics))
            return
        if any(existing[name] != value for name, value in profile_values.items()):
            raise ValueError("public profile identity already exists with different trusted data")
        existing_semantics = (
            connection.execute(
                select(dataset_task_semantics).where(
                    dataset_task_semantics.c.dataset_id == key["dataset_id"],
                    dataset_task_semantics.c.split_id == key["split_id"],
                    dataset_task_semantics.c.session_id == key["session_id"],
                )
            )
            .mappings()
            .one_or_none()
        )
        if (semantics is None and existing_semantics is not None) or (
            semantics is not None
            and (
                existing_semantics is None
                or any(
                    existing_semantics[name] != value for name, value in semantics.items()
                )
            )
        ):
            raise ValueError("public profile semantics already exist with different trusted data")
        existing_labels = connection.execute(
            select(dataset_labels.c.sample_index, dataset_labels.c.is_anomaly)
            .where(
                dataset_labels.c.dataset_id == key["dataset_id"],
                dataset_labels.c.split_id == key["split_id"],
                dataset_labels.c.session_id == key["session_id"],
            )
            .order_by(dataset_labels.c.sample_index)
        ).all()
        if [(row.sample_index, bool(row.is_anomaly)) for row in existing_labels] != list(
            enumerate(registration.labels)
        ):
            raise ValueError("public profile labels already exist with different trusted content")


def build_default_public_suite_manifest(
    *,
    repository_root: Path,
    trusted_registration_engine: Engine,
    destination: Path,
) -> tuple[
    tuple[MaterializedPublicTask, ...],
    PublicSuiteWeightMetadata,
    int,
    str,
]:
    """Materialize, Scorer-register and write the bounded default real-data suite.

    CARE keeps its complete source prediction segments. SKAB uses one fixed
    source-order prefix. SMD remains a fixed official train/test tail. TSB v2 keeps
    each complete source evaluation split; the shared 96 MiB manifest limit and
    separate 64 MiB matrix limit bound the serialized input. Scorer labels are
    installed before any label-free manifest is emitted.
    """
    tasks = (
        *materialize_care_development(repository_root=repository_root),
        *materialize_skab_development(repository_root=repository_root),
        *materialize_smd_development(repository_root=repository_root),
        *materialize_tsb_development(repository_root=repository_root, evaluation_rows=None),
    )
    weighted, metadata = finalize_public_suite_weights(tuple(tasks))
    from lab.director.suite_manifest import write_suite_manifest

    if destination.is_symlink():
        raise ValueError("public suite manifest destination cannot be a symlink")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    pending = destination.with_name(f".{destination.name}.pending")
    byte_count, digest = write_suite_manifest(
        tuple(item.task for item in weighted),
        pending,
        suite_id=PUBLIC_SUITE_ID,
        suite_version=3,
    )
    try:
        for item in weighted:
            install_scorer_task(trusted_registration_engine, item.scorer_registration)
        os.replace(pending, destination)
    except BaseException:
        pending.unlink(missing_ok=True)
        raise
    return weighted, metadata, byte_count, digest
