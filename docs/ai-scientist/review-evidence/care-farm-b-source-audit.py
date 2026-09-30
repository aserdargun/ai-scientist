#!/usr/bin/env python3
"""Bounded, read-only Farm B audit for the pinned CARE v6 archive.

This script only opens the archive README plus Farm B event metadata, feature
descriptions, and the 15 Farm B CSV members. It never opens Farm C payloads or
extracts archive members to disk. Output contains source statistics, not raw
sensor rows.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import sys
import zipfile
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ARCHIVE_SHA256 = "ca61379e98956d891041ad45c885109bd8a14199fde0688d0184a11c2d4194f1"
ARCHIVE_BYTES = 5_503_439_673
RECORD_ID = 15846963
FARM_PREFIX = "CARE_To_Compare/Wind Farm B/"
TRAINABLE_STATUS = {0, 2}
NONNORMAL_STATUS = {1, 3, 4, 5}
PHYSICAL_AVERAGE_COLUMNS = (
    "reactive_power_11_avg",
    "power_58_avg",
    "wind_speed_59_avg",
    "wind_speed_60_avg",
    "wind_speed_61_avg",
    "power_62_avg",
)
CHUNK_BYTES = 8 * 1024 * 1024


class HashingReader(io.RawIOBase):
    def __init__(self, wrapped: Any) -> None:
        self.wrapped = wrapped
        self.digest = hashlib.sha256()

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: bytearray) -> int:
        data = self.wrapped.read(len(buffer))
        size = len(data)
        buffer[:size] = data
        self.digest.update(data)
        return size

    def close(self) -> None:
        try:
            self.wrapped.close()
        finally:
            super().close()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=CHUNK_BYTES) as handle:
        for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit_task(
    archive: zipfile.ZipFile,
    member: str,
    event: dict[str, str],
    info: zipfile.ZipInfo,
) -> dict[str, Any]:
    # Stream raw data through the hashing reader but retain only scalar counters
    # and the status/time vectors needed for split, cadence, and mask accounting.
    raw = archive.open(member, "r")
    hashing = HashingReader(raw)
    buffered = io.BufferedReader(hashing, buffer_size=1024 * 1024)
    with io.TextIOWrapper(buffered, encoding="utf-8-sig", newline="") as text:
        reader = csv.DictReader(text, delimiter=";")
        header = reader.fieldnames
        if header is None or len(header) != 257 or len(set(header)) != 257:
            raise ValueError(f"Farm B schema differs from documented 257 fields: {member}")
        required = {
            "time_stamp",
            "asset_id",
            "id",
            "train_test",
            "status_type_id",
        }
        if not required.issubset(header):
            raise ValueError(f"Farm B reserved source columns are missing: {member}")
        average_columns = [name for name in header if name.endswith("_avg")]
        if len(average_columns) != 63:
            raise ValueError(f"Farm B average feature count differs from source metadata: {member}")
        if any(name not in header for name in PHYSICAL_AVERAGE_COLUMNS):
            raise ValueError(f"Farm B physical average candidate is absent: {member}")

        row_count = 0
        split_counts: Counter[str] = Counter()
        status_counts: dict[str, Counter[str]] = {
            "train": Counter(),
            "prediction": Counter(),
            "all": Counter(),
        }
        feature_counts: dict[str, Counter[str]] = {
            name: Counter() for name in PHYSICAL_AVERAGE_COLUMNS
        }
        first_timestamp: str | None = None
        last_timestamp: str | None = None
        previous_time: datetime | None = None
        cadence_counts: Counter[int] = Counter()
        prediction_cadence_counts: Counter[int] = Counter()
        previous_prediction_time: datetime | None = None
        prediction_steps_off_600s = 0
        nonpositive_steps = 0
        status_transitions = 0
        previous_status: str | None = None
        asset_ids: set[str] = set()
        train_first: str | None = None
        train_last: str | None = None
        eval_first: str | None = None
        eval_last: str | None = None
        train_first_time: datetime | None = None
        train_last_time: datetime | None = None
        eval_first_time: datetime | None = None
        eval_last_time: datetime | None = None
        train_end_seen = False
        split_prefix = True
        event_id = str(event["event_id"])
        wanted_event_ids = {str(event["event_start_id"]), str(event["event_end_id"])}
        id_hits: dict[str, tuple[int, str, str]] = {}
        healthy_train_timestamps: list[datetime] = []
        event_status: Counter[str] = Counter()
        event_rows = 0

        for row in reader:
            row_count += 1
            split = row.get("train_test", "")
            if split not in {"train", "prediction"}:
                raise ValueError(f"Farm B has unexpected train_test value: {member}")
            if split == "prediction":
                train_end_seen = True
            elif train_end_seen:
                split_prefix = False
            split_counts[split] += 1
            timestamp_text = row.get("time_stamp", "")
            timestamp = datetime.fromisoformat(timestamp_text)
            if timestamp.tzinfo is not None:
                raise ValueError(f"Farm B source timestamp unexpectedly has timezone: {member}")
            if first_timestamp is None:
                first_timestamp = timestamp_text
            last_timestamp = timestamp_text
            if previous_time is not None:
                delta = int((timestamp - previous_time).total_seconds())
                cadence_counts[delta] += 1
                if delta <= 0:
                    nonpositive_steps += 1
            previous_time = timestamp

            asset = row.get("asset_id", "")
            if asset:
                asset_ids.add(asset)
            status_text = row.get("status_type_id", "")
            try:
                status_int = int(status_text)
            except ValueError:
                status_int = -1
            status_counts["all"][status_text] += 1
            status_counts[split][status_text] += 1
            if previous_status is not None and status_text != previous_status:
                status_transitions += 1
            previous_status = status_text

            if split == "train":
                if train_first is None:
                    train_first, train_first_time = timestamp_text, timestamp
                train_last, train_last_time = timestamp_text, timestamp
                if status_int in TRAINABLE_STATUS:
                    healthy_train_timestamps.append(timestamp)
            else:
                if eval_first is None:
                    eval_first, eval_first_time = timestamp_text, timestamp
                if previous_prediction_time is not None:
                    prediction_delta = int((timestamp - previous_prediction_time).total_seconds())
                    prediction_cadence_counts[prediction_delta] += 1
                    if prediction_delta % 600:
                        prediction_steps_off_600s += 1
                previous_prediction_time = timestamp
                eval_last, eval_last_time = timestamp_text, timestamp

            source_id = row.get("id", "")
            if source_id in wanted_event_ids:
                id_hits[source_id] = (row_count - 1, timestamp_text, split)
            if split == "prediction" and source_id:
                source_id_int = int(source_id)
                if int(event["event_start_id"]) <= source_id_int <= int(event["event_end_id"]):
                    event_rows += 1
                    event_status[status_text] += 1

            for column in PHYSICAL_AVERAGE_COLUMNS:
                value = row.get(column, "")
                feature_counts[column]["empty" if value == "" else "present"] += 1
                if value == "0" or value == "0.0":
                    feature_counts[column]["literal_zero"] += 1
                if value:
                    try:
                        numeric = float(value)
                    except ValueError:
                        feature_counts[column]["non_numeric"] += 1
                    else:
                        if not math.isfinite(numeric):
                            feature_counts[column]["non_finite"] += 1

        if not row_count or hashing.digest.hexdigest() == hashlib.sha256(b"").hexdigest():
            raise ValueError(f"Farm B CSV stream was empty: {member}")
        if set(asset_ids) != {str(event["asset_id"])}:
            raise ValueError(f"Farm B event asset differs from row asset IDs: {member}")
        if not split_prefix or not split_counts["train"] or not split_counts["prediction"]:
            raise ValueError(f"Farm B split is not one train prefix and prediction suffix: {member}")
        if set(status_counts["all"]) - {"0", "1", "2", "3", "4", "5"}:
            raise ValueError(f"Farm B contains undocumented status code values: {member}")
        if cadence_counts and set(cadence_counts) - {600}:
            # Preserve the complete measured cadence histogram below; the audit
            # reports any source deviations rather than silently regularizing.
            pass
        for key in wanted_event_ids:
            if key not in id_hits:
                raise ValueError(f"Farm B event endpoint id is absent from source rows: {member}")
        start_hit = id_hits[str(event["event_start_id"])]
        end_hit = id_hits[str(event["event_end_id"])]
        start_time = datetime.fromisoformat(event["event_start"])
        end_time = datetime.fromisoformat(event["event_end"])
        endpoint_match = (
            start_hit[1] == event["event_start"]
            and end_hit[1] == event["event_end"]
            and start_hit[2] == "prediction"
            and end_hit[2] == "prediction"
            and start_hit[0] <= end_hit[0]
        )
        if not endpoint_match:
            raise ValueError(f"Farm B event metadata IDs/timestamps do not align: {member}")
        if train_first_time is None or train_last_time is None or eval_first_time is None or eval_last_time is None:
            raise ValueError(f"Farm B source split has missing boundaries: {member}")

        # Source data reports 0 replacing missing values for Farms B/C. These
        # counters document raw zeros and blanks but do not infer zero=missing.
        embargo_cutoff = eval_first_time - timedelta(days=1)
        if healthy_train_timestamps:
            eligible = [value for value in healthy_train_timestamps if value <= embargo_cutoff]
            longest_rows = 0
            current_rows = 0
            previous_eligible: datetime | None = None
            for value in eligible:
                if previous_eligible is None or value - previous_eligible != timedelta(seconds=600):
                    current_rows = 1
                else:
                    current_rows += 1
                longest_rows = max(longest_rows, current_rows)
                previous_eligible = value
        else:
            longest_rows = 0

        training_bytes = longest_rows * len(PHYSICAL_AVERAGE_COLUMNS) * 8
        eval_rows = split_counts["prediction"]
        evaluation_bytes = eval_rows * len(PHYSICAL_AVERAGE_COLUMNS) * 8
        full_train_bytes_63 = split_counts["train"] * len(average_columns) * 8
        full_eval_bytes_63 = eval_rows * len(average_columns) * 8
        return {
            "event_id": int(event_id),
            "event_label": event["event_label"],
            "asset_id": event["asset_id"],
            "member": member,
            "member_bytes_compressed": info.compress_size,
            "member_bytes_uncompressed": info.file_size,
            "member_crc32": f"{info.CRC:08x}",
            "member_sha256": hashing.digest.hexdigest(),
            "rows": row_count,
            "header_columns": len(header),
            "average_columns": len(average_columns),
            "train_rows": split_counts["train"],
            "prediction_rows": eval_rows,
            "train_prefix": split_prefix,
            "asset_ids_in_rows": sorted(asset_ids),
            "source_first_time": first_timestamp,
            "source_last_time": last_timestamp,
            "train_first_time": train_first,
            "train_last_time": train_last,
            "prediction_first_time": eval_first,
            "prediction_last_time": eval_last,
            "train_span_seconds": int((train_last_time - train_first_time).total_seconds()),
            "prediction_span_seconds": int((eval_last_time - eval_first_time).total_seconds()),
            "prediction_span_days_inclusive": (
                (eval_last_time - eval_first_time).total_seconds() / 86400 + (600 / 86400)
            ),
            "prediction_cadence_seconds_histogram": {
                str(k): v for k, v in sorted(prediction_cadence_counts.items())
            },
            "prediction_cadence_steps_off_600s": prediction_steps_off_600s,
            "cadence_seconds_histogram": {str(k): v for k, v in sorted(cadence_counts.items())},
            "nonpositive_timestamp_steps": nonpositive_steps,
            "status_counts": {
                scope: dict(sorted(counter.items())) for scope, counter in status_counts.items()
            },
            "status_transition_count": status_transitions,
            "event_start_id": int(event["event_start_id"]),
            "event_end_id": int(event["event_end_id"]),
            "event_start": event["event_start"],
            "event_end": event["event_end"],
            "event_endpoint_rows": {
                "start": {"row_index": start_hit[0], "timestamp": start_hit[1], "split": start_hit[2]},
                "end": {"row_index": end_hit[0], "timestamp": end_hit[1], "split": end_hit[2]},
            },
            "event_interval_prediction_rows": event_rows,
            "event_interval_status_counts": dict(sorted(event_status.items())),
            "event_interval_duration_seconds": int((end_time - start_time).total_seconds()),
            "selected_feature_raw_value_counts": {
                name: dict(counter) for name, counter in feature_counts.items()
            },
            "if_status_0_2_training_plus_1day_embargo": {
                "eligible_status_rows_before_embargo": len(healthy_train_timestamps),
                "embargo_seconds": 86400,
                "longest_contiguous_training_rows": longest_rows,
                "selected_six_features_raw_bytes": training_bytes,
                "evaluation_rows_full_prediction": eval_rows,
                "selected_six_features_evaluation_raw_bytes": evaluation_bytes,
                "selected_six_features_total_raw_bytes": training_bytes + evaluation_bytes,
            },
            "all_63_average_features_raw_bytes_no_filtering": {
                "train": full_train_bytes_63,
                "prediction": full_eval_bytes_63,
                "total": full_train_bytes_63 + full_eval_bytes_63,
            },
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repository-root", type=Path, default=Path(__file__).resolve().parents[3]
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.repository_root.resolve(strict=True)
    manifest_path = root / "docs/ai-scientist/review-evidence/care-source-manifest.json"
    structure_path = root / "docs/ai-scientist/review-evidence/care-structure-review.json"
    archive_path = root / "data/public/care/15846963/CARE_To_Compare.zip"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    structure = json.loads(structure_path.read_text(encoding="utf-8"))
    if (
        manifest.get("record_id") != RECORD_ID
        or manifest.get("sha256") != ARCHIVE_SHA256
        or manifest.get("archive_bytes") != ARCHIVE_BYTES
        or archive_path.stat().st_size != ARCHIVE_BYTES
    ):
        raise ValueError("Farm B audit source does not match the frozen Zenodo v6 source receipt")

    archive_sha = sha256_file(archive_path)
    if archive_sha != ARCHIVE_SHA256:
        raise ValueError("Farm B audit archive SHA-256 differs from the frozen source receipt")

    manifest_members = {
        item["path"]: item for item in manifest["members"] if item["path"].startswith(FARM_PREFIX)
    }
    farm_review = next(
        (item for item in structure.get("farms", []) if item.get("farm") == "B"), None
    )
    if farm_review is None or farm_review.get("dataset_files") != 15:
        raise ValueError("Farm B structure review does not list all 15 source CSVs")

    with zipfile.ZipFile(archive_path) as archive:
        b_names = sorted(
            name
            for name in archive.namelist()
            if name.startswith(FARM_PREFIX) and name.endswith(".csv")
        )
        data_names = [name for name in b_names if "/datasets/" in name]
        event_member = FARM_PREFIX + "event_info.csv"
        feature_member = FARM_PREFIX + "feature_description.csv"
        if len(data_names) != 15 or set(b_names) != set(manifest_members):
            raise ValueError("Farm B archive member set differs from pinned source inventory")
        # Deliberately do not open or read any Wind Farm C dataset payload.
        event_info_bytes = archive.read(event_member)
        feature_bytes = archive.read(feature_member)
        event_sha = hashlib.sha256(event_info_bytes).hexdigest()
        feature_sha = hashlib.sha256(feature_bytes).hexdigest()
        expected_event = manifest_members[event_member]
        expected_feature = manifest_members[feature_member]
        if (
            len(event_info_bytes) != expected_event["uncompressed_bytes"]
            or zlib_crc(event_info_bytes) != expected_event["crc32"]
            or len(feature_bytes) != expected_feature["uncompressed_bytes"]
            or zlib_crc(feature_bytes) != expected_feature["crc32"]
        ):
            raise ValueError("Farm B metadata differs from pinned archive inventory")
        if event_sha != farm_review["metadata_sha256"]:
            raise ValueError("Farm B event metadata hash differs from structure review receipt")
        event_rows = list(csv.DictReader(io.StringIO(event_info_bytes.decode("utf-8-sig")), delimiter=";"))
        feature_rows = list(
            csv.DictReader(io.StringIO(feature_bytes.decode("utf-8-sig")), delimiter=";")
        )
        events_by_id = {row["event_id"]: row for row in event_rows}
        if len(events_by_id) != 15 or len(event_rows) != 15:
            raise ValueError("Farm B event metadata does not have exactly 15 unique tasks")
        event_label_counts = Counter(row["event_label"] for row in event_rows)
        if event_label_counts != Counter({"anomaly": 6, "normal": 9}):
            raise ValueError("Farm B event metadata label counts differ from source structure review")
        avg_sensors = [row for row in feature_rows if "average" in row["statistics_type"].split(",")]
        if len(feature_rows) != 63 or len(avg_sensors) != 63:
            raise ValueError("Farm B feature descriptions do not contain 63 average sensors")
        feature_name_map = {
            row["sensor_name"] + "_avg": {
                "description": row["description"].strip(),
                "unit": row["unit"].strip(),
                "is_angle": row["is_angle"].strip(),
                "is_counter": row["is_counter"].strip(),
            }
            for row in avg_sensors
        }
        missing_physical = set(PHYSICAL_AVERAGE_COLUMNS) - set(feature_name_map)
        if missing_physical:
            raise ValueError("Farm B physical average shortlist differs from feature descriptions")

        audited: list[dict[str, Any]] = []
        for member in sorted(data_names):
            event_id = Path(member).stem
            if event_id not in events_by_id:
                raise ValueError(f"Farm B CSV has no matching event metadata: {member}")
            info = archive.getinfo(member)
            pinned = manifest_members[member]
            if (
                info.file_size != pinned["uncompressed_bytes"]
                or info.compress_size != pinned["compressed_bytes"]
                or f"{info.CRC:08x}" != pinned["crc32"]
            ):
                raise ValueError(f"Farm B archive member metadata differs from receipt: {member}")
            audited.append(audit_task(archive, member, events_by_id[event_id], info))

    family_counts = Counter(task["event_label"] for task in audited)
    sum_six = sum(
        task["if_status_0_2_training_plus_1day_embargo"]["selected_six_features_total_raw_bytes"]
        for task in audited
    )
    sum_63 = sum(
        task["all_63_average_features_raw_bytes_no_filtering"]["total"] for task in audited
    )
    max_six_artifact_raw = max(
        max(
            task["if_status_0_2_training_plus_1day_embargo"]["selected_six_features_raw_bytes"],
            task["if_status_0_2_training_plus_1day_embargo"][
                "selected_six_features_evaluation_raw_bytes"
            ],
        )
        for task in audited
    )
    max_63_artifact_raw = max(
        max(
            task["all_63_average_features_raw_bytes_no_filtering"]["train"],
            task["all_63_average_features_raw_bytes_no_filtering"]["prediction"],
        )
        for task in audited
    )
    report = {
        "schema": "care-farm-b-source-audit.v1",
        "scope": "read-only hash-verified Farm B source audit; no Farm C payloads opened; no materialization or Scorer writes",
        "source": {
            "record_id": RECORD_ID,
            "record_url": "https://zenodo.org/records/15846963",
            "doi": "10.5281/zenodo.15846963",
            "archive_bytes": ARCHIVE_BYTES,
            "expected_archive_sha256": ARCHIVE_SHA256,
            "measured_archive_sha256": archive_sha,
            "source_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "source_structure_review_sha256": hashlib.sha256(structure_path.read_bytes()).hexdigest(),
            "event_info_sha256": event_sha,
            "feature_description_sha256": feature_sha,
            "farm_b_member_count": len(audited),
        },
        "source_members": [
            {
                "member": task["member"],
                "compressed_bytes": task["member_bytes_compressed"],
                "uncompressed_bytes": task["member_bytes_uncompressed"],
                "crc32": task["member_crc32"],
                "sha256": task["member_sha256"],
            }
            for task in audited
        ],
        "source_semantics": {
            "training_and_prediction_split": "source train_test column; measured per file below, no row compression/reordering",
            "time": "naive anonymized source timestamps; official paper states 10-minute resolution; exact measured cadence histogram is per task; source clock is not UTC",
            "event_labels": "trusted event_info.csv event_label; 6 anomaly/PDM and 9 normal/NRM across all 15 tasks; no task selection based on label content",
            "status_type_id": "paper v2 and author FAQ identify Farm B/C status IDs as operator SCADA state plus service reports; codes 0 and 2 are normal, 1/3/4/5 abnormal; paper notes B/C status logging may be inconsistent. Do not reuse Farm A retrospective-logbook rule; any evaluation mask must be an explicit Farm-B-only policy using raw row status without forward-filling or inferred correction.",
            "farm_b_missing_values": "paper states missing values were replaced with numeric zero; report records zero/blank counts but zero is not reinterpreted as missing",
            "feature_summary": "257 columns total, 63 described average sensor features; source note recommends Avg-only for most Farm B analyses because Min/Max/Std are often physically implausible",
            "labels_and_masks": "event_label and event_info event interval are Scorer-only; status masks and event labels must not enter Director candidate context or public manifest",
        },
        "feature_descriptions": feature_name_map,
        "physical_average_shortlist": list(PHYSICAL_AVERAGE_COLUMNS),
        "task_counts": {
            "total": len(audited),
            "anomaly_pdm": family_counts["anomaly"],
            "normal_nrm": family_counts["normal"],
            "unique_assets": sorted({task["asset_id"] for task in audited}),
        },
        "tasks": audited,
        "storage_estimates": {
            "six_source_named_avg_selection": {
                "selection_basis": "fixed source-described physical signals, chosen without prediction values or event labels; not yet an accepted production feature policy",
                "train_assumption": "status IDs 0/2 plus 86400-second embargo before prediction prefix; longest contiguous raw train segment, as a byte-estimation scenario only",
                "evaluation": "all source prediction rows retained; no label-driven truncation",
                "all_train_and_prediction_raw_float64_matrix_bytes": sum_six,
                "max_single_train_or_eval_arrow_raw_values_bytes": max_six_artifact_raw,
                "single_artifact_limit_bytes": 32 * 1024**2,
                "aggregate_suite_limit_bytes": 2 * 1024**3,
                "default_suite_matrix_limit_bytes": 64 * 1024**2,
                "matrix_limit_note": "public SuiteManifest embeds train+evaluation matrices and enforces 64 MiB over the full suite. Scorer holdout uses separate per-blob 32 MiB Arrow artifacts and 2 GiB aggregate quota; B holdout should not be added as public dev matrix data.",
            },
            "all_63_avg_features_no_train_filter": {
                "all_train_and_prediction_raw_float64_matrix_bytes": sum_63,
                "max_single_train_or_eval_arrow_raw_values_bytes": max_63_artifact_raw,
                "single_artifact_limit_bytes": 32 * 1024**2,
                "aggregate_suite_limit_bytes": 2 * 1024**3,
                "default_suite_matrix_limit_bytes": 64 * 1024**2,
            },
        },
        "runtime": {
            "streaming": "CSV members streamed one at a time directly from zip; no archive extraction and no Farm C payload reads",
            "retained_data": "row counters, status/time scalars, six feature zero/blank counters, and per-file SHA digests only",
        },
    }
    output_path = args.output
    if output_path.is_symlink():
        raise ValueError("audit output path cannot be a symlink")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, sort_keys=True, indent=2, allow_nan=False).encode("utf-8") + b"\n"
    output_path.write_bytes(payload)
    print(
        json.dumps(
            {
                "schema": report["schema"],
                "archive_sha256": archive_sha,
                "task_count": len(audited),
                "pdm_count": family_counts["anomaly"],
                "nrm_count": family_counts["normal"],
                "six_feature_matrix_bytes": sum_six,
                "all_63_avg_matrix_bytes": sum_63,
                "report_path": str(output_path),
                "report_sha256": hashlib.sha256(payload).hexdigest(),
            },
            sort_keys=True,
        )
    )
    return 0


def zlib_crc(data: bytes) -> str:
    import zlib

    return f"{zlib.crc32(data) & 0xffffffff:08x}"


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"care-farm-b-audit-error={type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from exc
