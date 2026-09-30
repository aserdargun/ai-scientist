"""Build immutable typed candidate source for allowlisted harness baselines."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal, cast
from uuid import UUID

import numpy as np
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)
from sqlalchemy import Engine, text
from sqlalchemy.engine import RowMapping

from harness.baselines import (
    BASELINE_SEEDS,
    BASELINE_VERSIONS,
    CHAMPION_NOISE_VERSION,
    NORMALIZATION_FLOORS,
    REFERENCE_SUMMARY_VERSION,
    FrozenTaskReferences,
    freeze_task_references,
    normalize_task_score,
)
from lab.sandbox.docker_runner import DEFAULT_SANDBOX_IMAGE

BaselineName = Literal["robust_z", "iforest", "ecod_train_frozen"]
BASELINE_NAMES: tuple[BaselineName, BaselineName, BaselineName] = (
    "ecod_train_frozen",
    "iforest",
    "robust_z",
)


class BaselineSeedScores(BaseModel):
    """Scores for one named baseline in the fixed seed order."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    name: BaselineName
    scores: tuple[StrictFloat, StrictFloat, StrictFloat]
    scorer_artifacts_sha256: tuple[StrictStr, StrictStr, StrictStr]

    @field_validator("scorer_artifacts_sha256")
    @classmethod
    def validate_artifact_digests(cls, values: tuple[str, str, str]) -> tuple[str, str, str]:
        invalid = any(
            len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value)
            for value in values
        )
        if invalid:
            raise ValueError("Scorer artifact references must be lowercase SHA-256 digests")
        return values


class BaselineMean(BaseModel):
    """Arithmetic mean over one baseline's fixed seed set."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    name: BaselineName
    value: StrictFloat


class BaselineVersion(BaseModel):
    """Algorithm identity and implementation version for replay."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    name: BaselineName
    version: StrictStr
    candidate_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]


class TaskCalibration(BaseModel):
    """Frozen per-task baseline measurements and normalization reference."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    task_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    dataset_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    split_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    session_id: Annotated[StrictStr, Field(min_length=1, max_length=256)]
    profile_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    family: Literal["EVT", "PDM", "NRM"]
    seed_scores: tuple[BaselineSeedScores, BaselineSeedScores, BaselineSeedScores]
    seed_means: tuple[BaselineMean, BaselineMean, BaselineMean]
    base_score: StrictFloat
    reference_score: StrictFloat
    family_floor: StrictFloat
    task_weight: StrictFloat
    baseline_versions: tuple[BaselineVersion, BaselineVersion, BaselineVersion]
    seed_set: tuple[StrictInt, StrictInt, StrictInt] = BASELINE_SEEDS
    summary_version: StrictStr = REFERENCE_SUMMARY_VERSION

    @model_validator(mode="after")
    def verify_calibration_arithmetic(self) -> TaskCalibration:
        if self.seed_set != BASELINE_SEEDS or self.summary_version != REFERENCE_SUMMARY_VERSION:
            raise ValueError("unsupported frozen baseline calibration version")
        names = tuple(item.name for item in self.seed_scores)
        if names != BASELINE_NAMES:
            raise ValueError("baseline score entries are incomplete or out of canonical order")
        means = tuple(item.name for item in self.seed_means)
        if means != BASELINE_NAMES:
            raise ValueError("baseline mean entries are incomplete or out of canonical order")
        versions = tuple(item.name for item in self.baseline_versions)
        if versions != BASELINE_NAMES:
            raise ValueError("baseline version entries are incomplete or out of canonical order")
        computed = {item.name: float(np.mean(item.scores)) for item in self.seed_scores}
        actual_means = {item.name: item.value for item in self.seed_means}
        if any(computed[name] != actual_means[name] for name in BASELINE_NAMES):
            raise ValueError("baseline mean differs from measured seed scores")
        if self.base_score != computed["robust_z"] or self.reference_score != max(
            computed.values()
        ):
            raise ValueError("base/reference differ from the frozen baseline measurements")
        if self.family_floor != NORMALIZATION_FLOORS[self.family] or self.task_weight <= 0:
            raise ValueError("task family floor or trusted weight is invalid")
        expected_versions = tuple(BASELINE_VERSIONS[name] for name in BASELINE_NAMES)
        if tuple(item.version for item in self.baseline_versions) != expected_versions:
            raise ValueError("baseline implementation version changed")
        return self


class FrozenCalibrationDocument(BaseModel):
    """Canonical run-scoped record of baseline values fixed before proposals."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: str = Field(alias="schema", pattern=r"^baseline-calibration\.v1$")
    run_id: UUID
    suite_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    suite_version: Annotated[StrictInt, Field(ge=1)]
    harness_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    image_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    tasks: tuple[TaskCalibration, ...]
    champion_experiment_id: Annotated[StrictStr, Field(min_length=1, max_length=128)] | None = None
    champion_baseline_name: BaselineName | None = None
    champion_seed_scores: tuple[StrictFloat, StrictFloat, StrictFloat] | None = None
    champion_noise_sd: StrictFloat | None = Field(default=None, ge=0.0)
    champion_noise_version: StrictStr | None = None
    score_metric: Literal["vus_pr", "task_score"] = "vus_pr"
    score_metric_version: Literal["scorer.vus-pr.v1", "scorer.family-primary.v1"] = (
        "scorer.vus-pr.v1"
    )

    @field_validator("tasks")
    @classmethod
    def validate_tasks(cls, tasks: tuple[TaskCalibration, ...]) -> tuple[TaskCalibration, ...]:
        def identity(task: TaskCalibration) -> tuple[str, str, str, str]:
            return task.dataset_id, task.split_id, task.session_id, task.task_id

        identities = [identity(task) for task in tasks]
        if not tasks or len(identities) != len(set(identities)):
            raise ValueError("frozen calibration tasks must be non-empty and unique")
        if tuple(sorted(tasks, key=identity)) != tasks:
            raise ValueError("frozen calibration tasks must use canonical identity order")
        return tasks

    @model_validator(mode="after")
    def validate_score_metric(self) -> FrozenCalibrationDocument:
        if any(task.family != "EVT" for task in self.tasks) and (
            self.score_metric != "task_score"
            or self.score_metric_version != "scorer.family-primary.v1"
        ):
            raise ValueError("PDM/NRM calibration must name the family primary task_score metric")
        if self.score_metric == "vus_pr" and (
            self.score_metric_version != "scorer.vus-pr.v1"
            or any(task.family != "EVT" for task in self.tasks)
        ):
            raise ValueError("VUS-PR calibration is valid only for EVT tasks")
        if (
            self.score_metric == "task_score"
            and self.score_metric_version != "scorer.family-primary.v1"
        ):
            raise ValueError("task_score calibration must name scorer.family-primary.v1")
        return self

    @model_validator(mode="after")
    def verify_champion_noise(self) -> FrozenCalibrationDocument:
        if self.champion_seed_scores is None:
            if (
                self.champion_experiment_id is not None
                or self.champion_baseline_name is not None
                or self.champion_noise_sd is not None
            ):
                raise ValueError("champion noise metadata requires measured champion scores")
            if self.champion_noise_version is not None:
                raise ValueError("champion noise version has no measured champion")
            return self
        if (
            self.champion_experiment_id is None
            or self.champion_baseline_name is None
            or self.champion_noise_sd is None
        ):
            raise ValueError("champion identity and noise are required with seed scores")
        expected = (
            0.0
            if self.champion_seed_scores[0]
            == self.champion_seed_scores[1]
            == self.champion_seed_scores[2]
            else float(np.std(self.champion_seed_scores, ddof=0))
        )
        if (
            self.champion_noise_sd != expected
            or self.champion_noise_version != CHAMPION_NOISE_VERSION
        ):
            raise ValueError("champion noise differs from its frozen suite seed inputs")
        return self


def build_task_calibration(
    *,
    task_id: str,
    dataset_id: str,
    split_id: str,
    session_id: str,
    profile_sha256: str,
    family: Literal["EVT", "PDM", "NRM"],
    baseline_seed_scores: Mapping[str, Mapping[int, float]],
    scorer_artifact_sha256: Mapping[str, Mapping[int, str]],
    baseline_candidate_sha256: Mapping[str, str],
    task_weight: float,
) -> TaskCalibration:
    """Validate measured Scorer values and freeze the per-task reference."""
    references: FrozenTaskReferences = freeze_task_references(baseline_seed_scores, family=family)
    if (
        set(scorer_artifact_sha256) != set(BASELINE_NAMES)
        or set(baseline_candidate_sha256) != set(BASELINE_NAMES)
        or not np.isfinite(task_weight)
        or task_weight <= 0
    ):
        raise ValueError("all measured baseline identities and artifacts are required")
    for name in BASELINE_NAMES:
        if set(scorer_artifact_sha256[name]) != set(BASELINE_SEEDS):
            raise ValueError("Scorer artifact references require exactly seeds 0, 1, and 2")
        digest = baseline_candidate_sha256[name]
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("baseline source identity must be a lowercase SHA-256 digest")
    seed_scores = tuple(
        BaselineSeedScores(
            name=name,
            scores=references.seed_scores[name],
            scorer_artifacts_sha256=(
                scorer_artifact_sha256[name][0],
                scorer_artifact_sha256[name][1],
                scorer_artifact_sha256[name][2],
            ),
        )
        for name in BASELINE_NAMES
    )
    seed_means = tuple(
        BaselineMean(name=name, value=references.seed_means[name]) for name in BASELINE_NAMES
    )
    baseline_versions = tuple(
        BaselineVersion(
            name=name,
            version=BASELINE_VERSIONS[name],
            candidate_sha256=baseline_candidate_sha256[name],
        )
        for name in BASELINE_NAMES
    )
    return TaskCalibration(
        task_id=task_id,
        dataset_id=dataset_id,
        split_id=split_id,
        session_id=session_id,
        profile_sha256=profile_sha256,
        family=family,
        seed_scores=cast(
            tuple[BaselineSeedScores, BaselineSeedScores, BaselineSeedScores], seed_scores
        ),
        seed_means=cast(tuple[BaselineMean, BaselineMean, BaselineMean], seed_means),
        base_score=references.base_score,
        reference_score=references.reference_score,
        family_floor=NORMALIZATION_FLOORS[family],
        task_weight=task_weight,
        baseline_versions=cast(
            tuple[BaselineVersion, BaselineVersion, BaselineVersion], baseline_versions
        ),
        seed_set=BASELINE_SEEDS,
        summary_version=references.summary_version,
    )


def canonical_calibration_bytes(document: FrozenCalibrationDocument) -> bytes:
    """Serialize calibration with stable key order and strict finite JSON."""
    return json.dumps(
        document.model_dump(mode="json", by_alias=True, exclude_none=False),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_calibration_payload_bytes(payload: bytes) -> bytes:
    """Validate canonical raw JSON bytes without adding current-schema defaults.

    Older immutable calibration documents can omit fields introduced with defaults.
    Parse their original JSON object (rejecting duplicate keys), canonicalize that
    object, and compare the source bytes instead of serializing a newer model.
    """

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("calibration JSON contains duplicate object keys")
            result[key] = value
        return result

    try:
        parsed = json.loads(
            payload,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant {value}")
            ),
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("calibration artifact is not valid canonical JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("calibration artifact root must be an object")
    try:
        return json.dumps(
            parsed,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("calibration artifact contains values outside strict JSON") from exc


def calibration_sha256(document: FrozenCalibrationDocument) -> str:
    """Digest the exact immutable calibration bytes bound into later proposals."""
    return hashlib.sha256(canonical_calibration_bytes(document)).hexdigest()


def baseline_candidate_sha256(name: str) -> str:
    """Hash the exact deterministic typed wrapper source for one baseline."""
    return hashlib.sha256(baseline_candidate_source(name)).hexdigest()


class TrustedCalibrationTask(BaseModel):
    """Suite-owned task identity and fixed normalization weight."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    task_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    dataset_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    split_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    session_id: Annotated[StrictStr, Field(min_length=1, max_length=256)]
    profile_sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    family: Literal["EVT", "PDM", "NRM"]
    weight: Annotated[StrictFloat, Field(gt=0.0)]


def freeze_calibration_from_database(
    engine: Engine,
    *,
    run_id: UUID,
    runner_image: str,
    harness_sha256: str,
    suite_id: str,
    suite_version: int,
    expected_tasks: tuple[TrustedCalibrationTask, ...],
) -> FrozenCalibrationDocument:
    """Freeze only an exact baseline×seed result grid read from the dev view.

    This query uses the Director credential and never accepts score values from
    a caller. Candidate output artifact SHA values remain attached per result.
    """
    from harness.fingerprint import compute_harness_hash

    if runner_image != DEFAULT_SANDBOX_IMAGE:
        raise ValueError("calibration requires the currently pinned sandbox image")
    runner_digest = runner_image.rsplit("@sha256:", 1)[-1]
    if runner_digest.startswith("sha256:"):
        runner_digest = runner_digest.removeprefix("sha256:")
    if len(runner_digest) != 64 or any(ch not in "0123456789abcdef" for ch in runner_digest):
        raise ValueError("trusted sandbox image digest is invalid")
    current_harness = compute_harness_hash(Path(__file__).resolve().parents[2]).sha256
    if current_harness != harness_sha256:
        raise ValueError("requested harness digest differs from the current trusted tree")
    if not expected_tasks:
        raise ValueError("baseline calibration requires the complete trusted suite task set")
    identities = tuple(
        (task.dataset_id, task.split_id, task.session_id, task.task_id) for task in expected_tasks
    )
    if len(identities) != len(set(identities)):
        raise ValueError("trusted suite task identities must be unique")

    with engine.connect() as connection:
        registered = (
            connection.execute(
                text(
                    """
                SELECT experiment_id, baseline_name, candidate_sha256, status
                  FROM lab.experiments
                 WHERE run_id = :run_id AND kind = 'baseline'
                 ORDER BY baseline_name
                """
                ),
                {"run_id": run_id},
            )
            .mappings()
            .all()
        )
        if len(registered) != len(BASELINE_NAMES):
            raise ValueError("run must register exactly the three baseline experiments")
        experiment_by_name: dict[str, str] = {}
        candidate_hash_by_name: dict[str, str] = {}
        for row in registered:
            name = str(row["baseline_name"])
            if (
                name not in BASELINE_NAMES
                or name in experiment_by_name
                or row["status"] != "scored"
                or row["candidate_sha256"] != baseline_candidate_sha256(name)
            ):
                raise ValueError("registered baseline experiment identity is incomplete or invalid")
            connection.execute(
                text("SELECT lab.experiment_record_receipt(:experiment_id)"),
                {"experiment_id": row["experiment_id"]},
            ).scalar_one()
            experiment_by_name[name] = str(row["experiment_id"])
            candidate_hash_by_name[name] = str(row["candidate_sha256"])
        results = (
            connection.execute(
                text(
                    """
                SELECT experiment_id, evaluation_kind, task_id, seed,
                       candidate_sha256, dataset_id, split_id, session_id,
                       vus_pr, task_score, task_family, profile_sha256, harness_sha256,
                       candidate_output_sha256
                  FROM lab.dev_task_results
                 WHERE run_id = :run_id AND evaluation_kind = 'baseline'
                """
                ),
                {"run_id": run_id},
            )
            .mappings()
            .all()
        )

    expected_result_keys = {
        (
            experiment_by_name[name],
            task.dataset_id,
            task.split_id,
            task.session_id,
            task.task_id,
            seed,
        )
        for task in expected_tasks
        for name in BASELINE_NAMES
        for seed in BASELINE_SEEDS
    }
    task_by_identity = {
        (task.dataset_id, task.split_id, task.session_id, task.task_id): task
        for task in expected_tasks
    }
    measured: dict[tuple[str, str, str, str, str, int], RowMapping] = {}
    for row in results:
        row_identity = (
            str(row["dataset_id"]),
            str(row["split_id"]),
            str(row["session_id"]),
            str(row["task_id"]),
        )
        task_row = task_by_identity.get(row_identity)
        if task_row is None:
            raise ValueError("dev result contains a task outside the trusted suite")
        key = (str(row["experiment_id"]), *row_identity, int(row["seed"]))
        baseline_name: str | None = next(
            (
                candidate
                for candidate, experiment_id in experiment_by_name.items()
                if experiment_id == key[0]
            ),
            None,
        )
        if baseline_name is None or key not in expected_result_keys:
            raise ValueError("dev result contains an unexpected baseline or seed")
        if (
            row["candidate_sha256"] != candidate_hash_by_name[baseline_name]
            or row["profile_sha256"] != task_row.profile_sha256
            or row["harness_sha256"] != harness_sha256
            or row["evaluation_kind"] != "baseline"
            or row["task_family"] != task_row.family
            or row["candidate_output_sha256"] is None
            or key in measured
        ):
            raise ValueError("dev result identity differs from the trusted baseline plan")
        measured[key] = row
    if set(measured) != expected_result_keys:
        raise ValueError("baseline measurements do not exactly cover algorithms×tasks×seeds 0..2")

    task_calibrations: list[TaskCalibration] = []
    for task in expected_tasks:
        scores: dict[str, dict[int, float]] = {}
        artifact_hashes: dict[str, dict[int, str]] = {}
        for name in BASELINE_NAMES:
            experiment_id = experiment_by_name[name]
            scores[name] = {}
            artifact_hashes[name] = {}
            for seed in BASELINE_SEEDS:
                row = measured[
                    (
                        experiment_id,
                        task.dataset_id,
                        task.split_id,
                        task.session_id,
                        task.task_id,
                        seed,
                    )
                ]
                raw_score = row["task_score"]
                if raw_score is None and task.family == "EVT":
                    raw_score = row["vus_pr"]
                if raw_score is None:
                    raise ValueError("Scorer result lacks the trusted family primary task score")
                scores[name][seed] = float(raw_score)
                artifact_hashes[name][seed] = str(row["candidate_output_sha256"])
        task_calibrations.append(
            build_task_calibration(
                task_id=task.task_id,
                dataset_id=task.dataset_id,
                split_id=task.split_id,
                session_id=task.session_id,
                profile_sha256=task.profile_sha256,
                family=task.family,
                baseline_seed_scores=scores,
                scorer_artifact_sha256=artifact_hashes,
                baseline_candidate_sha256=candidate_hash_by_name,
                task_weight=float(task.weight),
            )
        )
    task_score_lookup: dict[tuple[str, str, str, str, str, int], float] = {
        key: float(row["task_score"] if row["task_score"] is not None else row["vus_pr"])
        for key, row in measured.items()
    }
    task_calibrations.sort(
        key=lambda task: (task.dataset_id, task.split_id, task.session_id, task.task_id)
    )
    weights_total = sum(task.task_weight for task in task_calibrations)
    suite_scores: dict[str, dict[int, float]] = {}
    for name in BASELINE_NAMES:
        suite_scores[name] = {}
        experiment_id = experiment_by_name[name]
        for seed in BASELINE_SEEDS:
            weighted_score = 0.0
            for calibration in task_calibrations:
                raw_score = task_score_lookup[
                    (
                        experiment_id,
                        calibration.dataset_id,
                        calibration.split_id,
                        calibration.session_id,
                        calibration.task_id,
                        seed,
                    )
                ]
                weighted_score += calibration.task_weight * normalize_task_score(
                    raw_score,
                    calibration.base_score,
                    calibration.reference_score,
                    family=calibration.family,
                )
            suite_scores[name][seed] = weighted_score / weights_total
    champion_name: BaselineName = "robust_z"
    expected_sorted = tuple(sorted(identities))
    return freeze_calibration_document(
        run_id=run_id,
        suite_id=suite_id,
        suite_version=suite_version,
        harness_sha256=harness_sha256,
        image_sha256=runner_digest,
        expected_task_identities=expected_sorted,
        tasks=tuple(task_calibrations),
        champion_experiment_id=experiment_by_name[champion_name],
        champion_baseline_name=champion_name,
        champion_suite_scores=suite_scores[champion_name],
    )


def freeze_calibration_document(
    *,
    run_id: UUID,
    suite_id: str,
    suite_version: int,
    harness_sha256: str,
    image_sha256: str,
    expected_task_identities: tuple[tuple[str, str, str, str], ...],
    tasks: tuple[TaskCalibration, ...],
    champion_experiment_id: str | None = None,
    champion_baseline_name: BaselineName | None = None,
    champion_suite_scores: Mapping[int, float] | None = None,
) -> FrozenCalibrationDocument:
    """Freeze only a complete exact-profile baseline calibration before proposals."""
    actual = tuple(
        (task.dataset_id, task.split_id, task.session_id, task.task_id) for task in tasks
    )
    actual_sorted = tuple(sorted(actual))
    expected_sorted = tuple(sorted(expected_task_identities))
    if (
        not expected_task_identities
        or len(expected_task_identities) != len(set(expected_task_identities))
        or len(actual) != len(set(actual))
        or actual_sorted != expected_sorted
    ):
        raise ValueError("baseline calibration does not cover the exact trusted task set")
    if (champion_experiment_id is None) != (champion_suite_scores is None) or (
        champion_baseline_name is None
    ) != (champion_suite_scores is None):
        raise ValueError("champion identity and measured suite scores must be supplied together")
    champion_scores: tuple[float, float, float] | None = None
    noise: float | None = None
    noise_version: str | None = None
    if champion_suite_scores is not None:
        if set(champion_suite_scores) != set(BASELINE_SEEDS):
            raise ValueError("champion suite requires separately measured seeds 0, 1, and 2")
        champion_scores = (
            float(champion_suite_scores[0]),
            float(champion_suite_scores[1]),
            float(champion_suite_scores[2]),
        )
        if not np.isfinite(champion_scores).all():
            raise ValueError("champion suite measurements must be finite")
        if champion_scores[0] == champion_scores[1] == champion_scores[2]:
            noise = 0.0
        else:
            noise = float(np.std(champion_scores, ddof=0))
        noise_version = CHAMPION_NOISE_VERSION

    def identity(task: TaskCalibration) -> tuple[str, str, str, str]:
        return task.dataset_id, task.split_id, task.session_id, task.task_id

    return FrozenCalibrationDocument(
        schema="baseline-calibration.v1",
        run_id=run_id,
        suite_id=suite_id,
        suite_version=suite_version,
        harness_sha256=harness_sha256,
        image_sha256=image_sha256,
        tasks=tuple(sorted(tasks, key=identity)),
        champion_experiment_id=champion_experiment_id,
        champion_baseline_name=champion_baseline_name,
        champion_seed_scores=champion_scores,
        champion_noise_sd=noise,
        champion_noise_version=noise_version,
        score_metric="task_score",
        score_metric_version="scorer.family-primary.v1",
    )


def baseline_candidate_source(name: str) -> bytes:
    """Return deterministic source bytes for a baseline to enter normal Docker scoring."""
    if name not in BASELINE_VERSIONS:
        raise ValueError("unknown baseline name")
    source = (
        "from harness.baselines import build_baseline\n\n"
        f"def build_candidate():\n    return build_baseline({name!r})\n"
    )
    return source.encode("utf-8")
