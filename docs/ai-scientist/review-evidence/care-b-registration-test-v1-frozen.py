"""Live disposable-PostgreSQL proof for private CARE Farm B registration.

This test does not run a candidate or claim real baseline calibration. Its 0/1
normalization values are synthetic registration-only placeholders.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

import numpy as np
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from harness.care_holdout import load_care_farm_b_holdout
from lab.director.public_suite import ScorerTaskRegistration, install_scorer_task
from lab.scorer.care_holdout import (
    CareFarmBTaskBinding,
    build_care_farm_b_registration_records,
)
from lab.scorer.holdout import (
    MAX_HOLDOUT_ARTIFACT_BYTES,
    _frame_bytes,
    _read_arrow_frame,
    _read_labels,
    _read_task_semantics,
    _semantics_digest,
    register_holdout_suite,
)
from lab.scorer.jobs import read_artifact_bytes

TEST_DATABASE_PREFIX = "swapp_lab_m0_holdout_030_"
ROLE_NAMES = ("migrator", "director", "planner", "scorer")
REPORT_FIELDS = frozenset(
    {
        "schema",
        "suite_version",
        "task_count",
        "pdm_count",
        "nrm_count",
        "sample_count_total",
        "train_rows_total",
        "labels_json_bytes",
        "masks_json_bytes",
        "times_json_bytes",
        "context_json_bytes",
        "arrow_blob_count",
        "arrow_blob_max_bytes",
        "arrow_bytes_total",
        "labels_sha256",
        "masks_sha256",
        "times_sha256",
        "contexts_sha256",
        "semantics_sha256",
        "arrow_sha256",
        "profile_bindings_sha256",
        "suite_manifest_sha256",
        "source_archive_sha256",
        "scorer_readback_tasks",
        "director_planner_denials",
        "synthetic_normalization_only",
        "baseline_scoring_performed",
        "candidate_scoring_performed",
    }
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _add_framed(digest: Any, payload: bytes) -> None:
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _private_report_path(repository_root: Path) -> Path:
    raw = os.environ.get("LAB_CARE_B_REGISTRATION_REPORT")
    if not raw:
        pytest.skip("set LAB_CARE_B_REGISTRATION_REPORT to a private snapshot-local JSON path")
    path = Path(raw)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("CARE registration report path must be an absolute nonsymlink path")
    lexical_parent = Path(os.path.abspath(path.parent))
    if not lexical_parent.is_relative_to(repository_root):
        raise ValueError("CARE registration report must stay inside the isolated snapshot")
    current = repository_root
    for part in lexical_parent.relative_to(repository_root).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("CARE registration report path crosses a symlink")
    resolved_parent = lexical_parent.resolve(strict=True)
    if resolved_parent != lexical_parent:
        raise ValueError("CARE registration report path changes under canonical resolution")
    parent_stat = resolved_parent.stat()
    if parent_stat.st_uid != os.getuid() or stat.S_IMODE(parent_stat.st_mode) != 0o700:
        raise ValueError("CARE registration report parent must be private mode 0700")
    if path.exists() or path.is_symlink():
        raise ValueError("CARE registration report path already exists")
    return resolved_parent / path.name


def _write_private_report(path: Path, report: Mapping[str, object]) -> str:
    if set(report) != REPORT_FIELDS:
        raise ValueError("CARE registration report contains missing or unapproved fields")
    if any(isinstance(value, (dict, list, tuple)) for value in report.values()):
        raise ValueError("CARE registration report permits aggregate scalar fields only")
    payload = _canonical_json_bytes(dict(report)) + b"\n"
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    return _sha256(payload)


def _engines(repository_root: Path) -> dict[str, Engine]:
    value = os.environ.get("LAB_HOLDOUT_TEST_DSN_DIR")
    if not value:
        pytest.skip("set LAB_HOLDOUT_TEST_DSN_DIR to the disposable holdout-030 database")
    raw_root = Path(value)
    if raw_root.is_symlink():
        raise ValueError("holdout test DSN directory cannot be a symlink")
    lexical_root = Path(os.path.abspath(raw_root))
    if not lexical_root.is_relative_to(repository_root):
        raise ValueError("holdout test DSN directory must be inside the isolated snapshot")
    current = repository_root
    for part in lexical_root.relative_to(repository_root).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("holdout test DSN path crosses a symlink")
    dsn_root = lexical_root.resolve(strict=True)
    if dsn_root != lexical_root or not dsn_root.is_dir():
        raise ValueError("holdout test DSN directory is not a canonical directory")
    root_stat = dsn_root.stat()
    if root_stat.st_uid != os.getuid() or stat.S_IMODE(root_stat.st_mode) != 0o700:
        raise ValueError("holdout test DSN directory must be private mode 0700")
    engines: dict[str, Engine] = {}
    try:
        for role in ROLE_NAMES:
            dsn_path = dsn_root / f"{role}.dsn"
            if dsn_path.is_symlink() or not dsn_path.is_file():
                raise ValueError("holdout test role DSN file is missing or unsafe")
            mode = stat.S_IMODE(dsn_path.stat().st_mode)
            if dsn_path.stat().st_uid != os.getuid() or mode & 0o077:
                raise ValueError("holdout test role DSN file must be private")
            engines[role] = create_engine(
                dsn_path.read_text(encoding="utf-8").strip(),
                pool_pre_ping=True,
                hide_parameters=True,
            )
        for role, engine in engines.items():
            with engine.connect() as connection:
                database, session_user = connection.execute(
                    text("SELECT current_database(), session_user")
                ).one()
            _require(
                isinstance(database, str) and database.startswith(TEST_DATABASE_PREFIX),
                "CARE registration test connected to a database outside the disposable prefix",
            )
            _require(
                session_user == f"swapp_lab_{role}",
                "CARE registration test role DSN has unexpected database identity",
            )
        return engines
    except Exception:
        for engine in engines.values():
            engine.dispose()
        raise


def _assert_private_tables_hidden(engine: Engine, *, dataset_id: str, suite_id: str) -> int:
    probes = (
        (
            "SELECT is_anomaly FROM scorer.dataset_labels WHERE dataset_id=:dataset LIMIT 1",
            {"dataset": dataset_id},
        ),
        (
            "SELECT masked_samples_json FROM scorer.dataset_task_semantics "
            "WHERE dataset_id=:dataset LIMIT 1",
            {"dataset": dataset_id},
        ),
        (
            "SELECT dataset_id FROM scorer.holdout_suite_tasks WHERE suite_id=:suite LIMIT 1",
            {"suite": suite_id},
        ),
    )
    verified_denials = 0
    for statement, params in probes:
        try:
            with engine.connect() as connection:
                connection.execute(text(statement), params).first()
        except DBAPIError as error:
            sqlstate = getattr(error.orig, "sqlstate", None) or getattr(
                error.orig, "pgcode", None
            )
            _require(
                sqlstate == "42501",
                "private CARE table denial did not return PostgreSQL insufficient_privilege",
            )
            verified_denials += 1
        else:
            raise AssertionError("Director or Planner can query private CARE holdout records")
    return verified_denials


@pytest.mark.live
def test_real_care_farm_b_registration_roundtrips_through_privileged_and_scorer_roles() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    archive_value = os.environ.get("LAB_CARE_FARM_B_ARCHIVE")
    if not archive_value:
        pytest.skip("set LAB_CARE_FARM_B_ARCHIVE to the checksum-pinned CARE archive")
    archive_path = Path(archive_value)
    if archive_path.is_symlink() or not archive_path.is_file():
        raise ValueError("CARE Farm B archive path is missing or unsafe")
    report_path = _private_report_path(repository_root)
    engines = _engines(repository_root)
    artifact_root: Path | None = None
    try:
        suite = load_care_farm_b_holdout(archive_path)
        private_suite_id = f"care-b-registration-{uuid4().hex}"
        suite_version = 1
        development_manifest_sha256 = _sha256(
            b"registration-only synthetic development identity; no baseline calibration"
        )
        bindings = {
            task.task_id: CareFarmBTaskBinding(
                profile_sha256=_sha256(
                    f"{suite.manifest_sha256}:{task.source_identity}:profile".encode("ascii")
                ),
                task_weight=1.0 / len(suite.tasks),
                base_score=0.0,
                reference_score=1.0,
            )
            for task in suite.tasks
        }
        records = build_care_farm_b_registration_records(suite, bindings=bindings)
        _require(len(records) == 15, "CARE Farm B registration did not produce all 15 tasks")

        label_hasher = hashlib.sha256()
        mask_hasher = hashlib.sha256()
        time_hasher = hashlib.sha256()
        context_hasher = hashlib.sha256()
        semantics_hasher = hashlib.sha256()
        arrow_hasher = hashlib.sha256()
        profile_hasher = hashlib.sha256()
        labels_json_bytes = masks_json_bytes = times_json_bytes = context_json_bytes = 0
        arrow_bytes_total = arrow_blob_max_bytes = train_rows_total = 0
        arrow_blob_count = 0
        expected_payloads: dict[str, tuple[bytes, bytes]] = {}

        artifact_root = Path(
            tempfile.mkdtemp(
                prefix=".care-b-registration-artifacts-",
                dir=repository_root,
            )
        )
        artifact_stat = artifact_root.stat()
        if artifact_stat.st_uid != os.getuid() or stat.S_IMODE(artifact_stat.st_mode) != 0o700:
            raise ValueError("CARE registration artifact root must be private mode 0700")

        public_registrations: list[ScorerTaskRegistration] = []
        expected_records: dict[str, Any] = {}
        for record in records:
            spec = record.task_spec
            _require(spec.family in {"PDM", "NRM"}, "CARE Farm B family changed unexpectedly")
            task_key = hashlib.sha256(
                f"{suite.manifest_sha256}:{spec.task_id}".encode()
            ).hexdigest()[:32]
            expected_records[task_key] = record
            raw_failures = cast(list[list[int]], record.semantics["failure_windows"])
            _require(
                all(len(window) == 2 for window in raw_failures),
                "CARE failure window metadata is malformed",
            )
            public_registrations.append(
                ScorerTaskRegistration(
                    dataset_id=spec.dataset_id,
                    split_id=spec.split_id,
                    session_id=spec.session_id,
                    sample_count=len(spec.evaluation),
                    sliding_window=spec.sliding_window,
                    embargo_seconds=0,
                    profile_sha256=spec.profile_sha256,
                    task_family=cast(Literal["PDM", "NRM"], spec.family),
                    sampling_s=spec.context.sampling_s,
                    labels=record.labels,
                    evaluation_times=tuple(
                        cast(list[str | int | float], record.semantics["evaluation_times"])
                    ),
                    masked_samples=tuple(cast(list[bool], record.semantics["masked_samples"])),
                    failure_windows=tuple((window[0], window[1]) for window in raw_failures),
                    semantics_sha256=record.semantics_sha256,
                    visibility="holdout",
                )
            )
            label_payload = _canonical_json_bytes(list(record.labels))
            mask_payload = _canonical_json_bytes(record.semantics["masked_samples"])
            time_payload = _canonical_json_bytes(record.semantics["evaluation_times"])
            context_payload = _canonical_json_bytes(asdict(spec.context))
            for hasher, payload in (
                (label_hasher, label_payload),
                (mask_hasher, mask_payload),
                (time_hasher, time_payload),
                (context_hasher, context_payload),
            ):
                hasher.update(len(payload).to_bytes(8, "big"))
                hasher.update(payload)
            semantics_hasher.update(bytes.fromhex(record.semantics_sha256))
            profile_binding = {
                "dataset_id": spec.dataset_id,
                "split_id": spec.split_id,
                "session_id": spec.session_id,
                "profile_sha256": spec.profile_sha256,
            }
            _add_framed(profile_hasher, _canonical_json_bytes(profile_binding))
            labels_json_bytes += len(label_payload)
            masks_json_bytes += len(mask_payload)
            times_json_bytes += len(time_payload)
            context_json_bytes += len(context_payload)
            train_rows_total += len(spec.train)
            train_payload = _frame_bytes(spec.train, columns=tuple(spec.context.signals))
            evaluation_payload = _frame_bytes(
                spec.evaluation, columns=tuple(spec.context.signals)
            )
            expected_payloads[task_key] = (train_payload, evaluation_payload)
            for payload in (train_payload, evaluation_payload):
                if len(payload) > MAX_HOLDOUT_ARTIFACT_BYTES:
                    raise AssertionError("CARE Arrow blob exceeds the Scorer per-blob bound")
                arrow_blob_count += 1
                arrow_blob_max_bytes = max(arrow_blob_max_bytes, len(payload))
                arrow_bytes_total += len(payload)
                _add_framed(arrow_hasher, payload)

        for registration in public_registrations:
            install_scorer_task(engines["migrator"], registration)

        registration_result = register_holdout_suite(
            engines["migrator"],
            suite_id=private_suite_id,
            suite_version=suite_version,
            manifest_sha256=suite.manifest_sha256,
            development_manifest_sha256=development_manifest_sha256,
            epsilon=0.0,
            tasks=tuple(record.task_spec for record in records),
            input_root=artifact_root,
        )
        _require(registration_result["tasks"] == 15, "Scorer registered an incomplete task set")
        _require(
            registration_result["input_bytes"] == arrow_bytes_total,
            "Scorer input byte receipt differs from canonical Arrow payloads",
        )
        with engines["scorer"].connect() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT task_key,dataset_id,split_id,session_id,profile_sha256,family,"
                        "task_weight,base_score,reference_score,sliding_window,sampling_s,"
                        "train_sha256,evaluation_sha256,"
                        "semantics_sha256,context_json FROM scorer.holdout_suite_tasks "
                        "WHERE suite_id=:suite AND suite_version=:version ORDER BY task_key"
                    ),
                    {"suite": private_suite_id, "version": suite_version},
                )
                .mappings()
                .all()
            )
        _require(len(rows) == 15, "Scorer registry readback does not contain all 15 tasks")
        _require(
            {str(row["task_key"]) for row in rows} == set(expected_records),
            "Scorer registry task-key coverage differs from the trusted input suite",
        )
        with engines["scorer"].connect() as connection:
            suite_header = connection.execute(
                text(
                    "SELECT manifest_sha256,development_manifest_sha256,task_count,epsilon "
                    "FROM scorer.holdout_suite_versions WHERE suite_id=:suite "
                    "AND suite_version=:version"
                ),
                {"suite": private_suite_id, "version": suite_version},
            ).mappings().one_or_none()
        _require(suite_header is not None, "Scorer suite metadata readback is missing")
        _require(
            suite_header["manifest_sha256"] == suite.manifest_sha256
            and suite_header["development_manifest_sha256"] == development_manifest_sha256
            and suite_header["task_count"] == len(records)
            and suite_header["epsilon"] == 0.0,
            "Scorer suite header metadata differs from the requested immutable registration",
        )

        loaded_labels = loaded_masks = loaded_times = 0
        for row in rows:
            task_key = str(row["task_key"])
            record = expected_records[task_key]
            spec = record.task_spec
            _require(
                row["dataset_id"] == spec.dataset_id
                and row["split_id"] == spec.split_id
                and row["session_id"] == spec.session_id
                and row["profile_sha256"] == spec.profile_sha256
                and row["family"] == spec.family
                and row["task_weight"] == bindings[spec.task_id].task_weight
                and row["base_score"] == bindings[spec.task_id].base_score
                and row["reference_score"] == bindings[spec.task_id].reference_score
                and row["sliding_window"] == spec.sliding_window
                and row["sampling_s"] == spec.context.sampling_s
                and row["semantics_sha256"] == record.semantics_sha256,
                "Scorer task registry identity or family binding differs from the suite",
            )
            context_payload = _canonical_json_bytes(row["context_json"])
            _require(
                context_payload == _canonical_json_bytes(asdict(spec.context)),
                "Scorer task registry FitContext differs from the trusted record",
            )
            _require(
                len(record.labels)
                == len(record.semantics["masked_samples"])
                == len(record.semantics["evaluation_times"])
                == len(spec.evaluation),
                "CARE labels, masks, times, and matrix dimensions are not aligned",
            )
            loaded = _read_labels(engines["scorer"], row, len(spec.evaluation))
            expected_labels = np.asarray(record.labels, dtype=np.int8)
            _require(
                len(loaded) == len(expected_labels)
                and loaded.tobytes() == expected_labels.tobytes(),
                "Scorer runtime label readback differs from the trusted registration",
            )
            semantics = _read_task_semantics(engines["scorer"], row)
            reloaded_semantics_sha256 = _semantics_digest(
                family=str(semantics["task_family"]),
                sampling_s=semantics["sampling_s"],
                times=semantics["evaluation_times_json"],
                masks=semantics["masked_samples_json"],
                failures=semantics["failure_windows_json"],
            )
            _require(
                semantics["task_family"] == spec.family
                and semantics["sampling_s"] == spec.context.sampling_s
                and semantics["evaluation_times_json"] == record.semantics["evaluation_times"]
                and semantics["masked_samples_json"] == record.semantics["masked_samples"]
                and semantics["failure_windows_json"] == record.semantics["failure_windows"]
                and semantics["semantics_sha256"] == record.semantics_sha256,
                "Scorer runtime semantics readback differs from canonical timestamps/masks/windows",
            )
            _require(
                reloaded_semantics_sha256 == record.semantics_sha256,
                "Scorer-reloaded task semantics do not match their canonical digest",
            )
            train_payload, evaluation_payload = expected_payloads[task_key]
            stored_train_bytes = read_artifact_bytes(
                str(row["train_sha256"]), artifact_root=artifact_root
            )
            stored_eval_bytes = read_artifact_bytes(
                str(row["evaluation_sha256"]), artifact_root=artifact_root
            )
            _require(
                stored_train_bytes == train_payload and stored_eval_bytes == evaluation_payload,
                "Scorer Arrow artifact bytes differ from the trusted feature matrices",
            )
            train = _read_arrow_frame(str(row["train_sha256"]), root=artifact_root)
            evaluation = _read_arrow_frame(str(row["evaluation_sha256"]), root=artifact_root)
            _require(
                train.equals(spec.train.reset_index(drop=True))
                and evaluation.equals(spec.evaluation.reset_index(drop=True))
                and tuple(map(str, train.columns)) == tuple(spec.context.signals)
                and tuple(map(str, evaluation.columns)) == tuple(spec.context.signals),
                "Scorer runtime Arrow reload differs in matrix values, shape, or columns",
            )
            loaded_labels += len(loaded)
            loaded_masks += len(semantics["masked_samples_json"])
            loaded_times += len(semantics["evaluation_times_json"])

        denial_count = sum(
            _assert_private_tables_hidden(
                engines[role], dataset_id=records[0].task_spec.dataset_id, suite_id=private_suite_id
            )
            for role in ("director", "planner")
        )
        _require(denial_count == 6, "expected all six Director/Planner privacy probes to be denied")

        report: dict[str, object] = {
            "schema": "care-b-registration-only.v1",
            "suite_version": suite_version,
            "task_count": len(records),
            "pdm_count": sum(record.task_spec.family == "PDM" for record in records),
            "nrm_count": sum(record.task_spec.family == "NRM" for record in records),
            "sample_count_total": sum(len(record.labels) for record in records),
            "train_rows_total": train_rows_total,
            "labels_json_bytes": labels_json_bytes,
            "masks_json_bytes": masks_json_bytes,
            "times_json_bytes": times_json_bytes,
            "context_json_bytes": context_json_bytes,
            "arrow_blob_count": arrow_blob_count,
            "arrow_blob_max_bytes": arrow_blob_max_bytes,
            "arrow_bytes_total": arrow_bytes_total,
            "labels_sha256": label_hasher.hexdigest(),
            "masks_sha256": mask_hasher.hexdigest(),
            "times_sha256": time_hasher.hexdigest(),
            "contexts_sha256": context_hasher.hexdigest(),
            "semantics_sha256": semantics_hasher.hexdigest(),
            "arrow_sha256": arrow_hasher.hexdigest(),
            "profile_bindings_sha256": profile_hasher.hexdigest(),
            "suite_manifest_sha256": suite.manifest_sha256,
            "source_archive_sha256": suite.source_archive_sha256,
            "scorer_readback_tasks": len(rows),
            "director_planner_denials": denial_count,
            "synthetic_normalization_only": True,
            "baseline_scoring_performed": False,
            "candidate_scoring_performed": False,
        }
        _require(report["task_count"] == 15, "registration report task count is not complete")
        _require(
            report["pdm_count"] == 6 and report["nrm_count"] == 9,
            "Farm B family counts changed",
        )
        _require(
            report["sample_count_total"] == 87_248,
            "Farm B complete evaluation row total changed",
        )
        _require(
            loaded_labels == loaded_masks == loaded_times == report["sample_count_total"],
            "Scorer runtime readback did not cover every evaluation row",
        )
        _write_private_report(report_path, report)
    finally:
        for engine in engines.values():
            engine.dispose()
        if artifact_root is not None:
            shutil.rmtree(artifact_root)
