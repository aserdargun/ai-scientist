#!/usr/bin/env python3
"""Bounded real-data acceptance driver for the local Farm B materializer."""

from __future__ import annotations

import argparse
import hashlib
import json
import resource
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.ipc as ipc

from harness.care_holdout import (
    FARM_B_ARCHIVE_SHA256,
    FARM_B_LICENSE,
    FARM_B_REVISION,
    MAX_WINDOW,
    MIN_WINDOW,
    REFERENCE_DAYS,
    SAMPLING_SECONDS,
    USAGE_PROFILE,
    FarmBCareSuite,
    load_care_farm_b_holdout,
)

MAX_BLOB_BYTES = 32 * 1024**2
MAX_TOTAL_BYTES = 2 * 1024**3


def _arrow_bytes(frame: pa.Table | object) -> int:
    table = (
        frame
        if isinstance(frame, pa.Table)
        else pa.Table.from_pandas(frame.reset_index(drop=True), preserve_index=False)
    )  # type: ignore[attr-defined]
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.tell()


def _task_report(task: object) -> dict[str, object]:
    evaluation = task.evaluation_values  # type: ignore[attr-defined]
    train = task.train_values  # type: ignore[attr-defined]
    masks = task.masked_samples  # type: ignore[attr-defined]
    labels = task.labels  # type: ignore[attr-defined]
    times = task.evaluation_times  # type: ignore[attr-defined]
    source_indices = task.evaluation_source_indices  # type: ignore[attr-defined]
    prefix_count = REFERENCE_DAYS * 24 * 60 * 60 // SAMPLING_SECONDS
    prefix_masks = masks[:prefix_count]
    prediction_masks = masks[prefix_count:]
    train_bytes = _arrow_bytes(train)
    eval_bytes = _arrow_bytes(evaluation)
    if train_bytes <= 0 or eval_bytes <= 0 or max(train_bytes, eval_bytes) > MAX_BLOB_BYTES:
        raise ValueError("real Farm B Arrow artifact exceeds the shared per-blob bound")
    prediction_start = datetime.strptime(task.prediction_start, "%Y-%m-%d %H:%M:%S")  # type: ignore[attr-defined]
    prediction_end = datetime.strptime(task.prediction_end, "%Y-%m-%d %H:%M:%S")  # type: ignore[attr-defined]
    prediction_delta = (prediction_end - prediction_start).total_seconds()
    if prediction_delta < 0 or prediction_delta % SAMPLING_SECONDS:
        raise ValueError("real Farm B prediction interval is not on the source cadence")
    expected_prediction_rows = int(prediction_delta // SAMPLING_SECONDS) + 1
    if len(prediction_masks) != expected_prediction_rows:
        raise ValueError("real Farm B prediction grid differs from source timestamp endpoints")
    if len(times) != len(masks) or len(masks) != len(labels) or len(labels) != len(evaluation):
        raise ValueError("real Farm B task arrays are misaligned")
    if task.family == "PDM":  # type: ignore[attr-defined]
        positive_count = sum(labels)
        healthy = sum(not mask and not label for mask, label in zip(masks, labels, strict=True))
        failure = task.failure_windows[0]  # type: ignore[attr-defined]
        if not positive_count or not healthy:
            raise ValueError("real Farm B PDM lacks positive or healthy exposure")
    else:
        positive_count = 0
        healthy = sum(not mask for mask in masks)
        failure = None
        if any(labels):
            raise ValueError("real Farm B NRM labels contain positives")
    if any(label and mask for label, mask in zip(labels, masks, strict=True)):
        raise ValueError("real Farm B task has positive labels on masked points")
    observed_reference = sum(index is not None for index in source_indices[:prefix_count])
    if observed_reference > prefix_count:
        raise AssertionError("reference observation count is impossible")
    return {
        "task_id": task.task_id,  # Scorer-only receipt; never place in Director prompts.
        "private_source_identity_sha256": task.source_identity,
        "family": task.family,
        "source_member": task.source_member,
        "source_member_sha256": task.source_member_sha256,
        "fit_rows": len(train),
        "fit_start": task.fit_start,
        "fit_end": task.fit_end,
        "fit_cutoff": task.train_cutoff,
        "reference_start": task.reference_start,
        "prediction_start": task.prediction_start,
        "prediction_end": task.prediction_end,
        "reference_grid_rows": prefix_count,
        "reference_source_rows_observed": observed_reference,
        "reference_rows_masked": sum(prefix_masks),
        "prediction_grid_rows": len(prediction_masks),
        "evaluation_mask_vector_rows": len(masks),
        "prediction_endpoint_grid_rows": expected_prediction_rows,
        "prediction_rows_masked": sum(prediction_masks),
        "prediction_positive_labels_unmasked": positive_count,
        "healthy_unmasked_rows_outside_failure": healthy,
        "failure_window_in_extended_grid": list(failure) if failure is not None else None,
        "sliding_window_train_only": task.sliding_window,
        "arrow_train_bytes": train_bytes,
        "arrow_evaluation_bytes": eval_bytes,
        "arrow_task_total_bytes": train_bytes + eval_bytes,
        "semantics_sha256": task.semantics_sha256,
    }


def build_report(
    suite: FarmBCareSuite,
    archive: Path,
    driver_path: Path,
    loader_path: Path,
    scorer_adapter_path: Path,
) -> dict[str, object]:
    task_reports = [_task_report(task) for task in suite.tasks]
    total_bytes = sum(int(task["arrow_task_total_bytes"]) for task in task_reports)
    if total_bytes > MAX_TOTAL_BYTES:
        raise ValueError("real Farm B Arrow inputs exceed the shared 2 GiB aggregate bound")
    summary = {
        "task_count": len(task_reports),
        "pdm_count": sum(task["family"] == "PDM" for task in task_reports),
        "nrm_count": sum(task["family"] == "NRM" for task in task_reports),
        "prediction_rows_retained": sum(int(task["prediction_grid_rows"]) for task in task_reports),
        "reference_rows_added": sum(int(task["reference_grid_rows"]) for task in task_reports),
        "evaluation_rows_total": suite.evaluation_rows,
        "expected_evaluation_rows": 87_248,
        "all_15_tasks_present": len(task_reports) == 15,
        "all_prediction_mask_vector_lengths_match_source_grids": all(
            int(task["evaluation_mask_vector_rows"])
            == int(task["reference_grid_rows"]) + int(task["prediction_endpoint_grid_rows"])
            and int(task["prediction_grid_rows"]) == int(task["prediction_endpoint_grid_rows"])
            for task in task_reports
        ),
        "all_pdm_tasks_have_positive_and_healthy_exposure": all(
            int(task["prediction_positive_labels_unmasked"]) > 0
            and int(task["healthy_unmasked_rows_outside_failure"]) > 0
            for task in task_reports
            if task["family"] == "PDM"
        ),
        "fit_rows_minimum": min(int(task["fit_rows"]) for task in task_reports),
        "fit_rows_maximum": max(int(task["fit_rows"]) for task in task_reports),
        "window_minimum": min(int(task["sliding_window_train_only"]) for task in task_reports),
        "window_maximum": max(int(task["sliding_window_train_only"]) for task in task_reports),
        "window_bounds": [MIN_WINDOW, MAX_WINDOW],
        "per_blob_max_bytes": MAX_BLOB_BYTES,
        "per_blob_observed_max_bytes": max(
            max(int(task["arrow_train_bytes"]), int(task["arrow_evaluation_bytes"]))
            for task in task_reports
        ),
        "aggregate_limit_bytes": MAX_TOTAL_BYTES,
        "aggregate_arrow_bytes": total_bytes,
        "all_tasks_below_per_blob_limit": all(
            max(int(task["arrow_train_bytes"]), int(task["arrow_evaluation_bytes"]))
            < MAX_BLOB_BYTES
            for task in task_reports
        ),
        "aggregate_below_limit": total_bytes < MAX_TOTAL_BYTES,
        "farm_c_payloads_opened": False,
        "database_writes": False,
        "gpu_or_model_started": False,
    }
    if (
        summary["task_count"] != 15
        or summary["pdm_count"] != 6
        or summary["nrm_count"] != 9
        or summary["prediction_rows_retained"] != 72_128
        or summary["reference_rows_added"] != 15_120
        or summary["evaluation_rows_total"] != 87_248
        or not summary["all_prediction_mask_vector_lengths_match_source_grids"]
        or not summary["all_pdm_tasks_have_positive_and_healthy_exposure"]
        or not summary["all_tasks_below_per_blob_limit"]
        or not summary["aggregate_below_limit"]
    ):
        raise ValueError("real Farm B suite does not meet its frozen source/policy acceptance")
    return {
        "schema": "care-b-prefix-materialization.v1",
        "scope": "real-data materialization only; no benchmark equivalence or scoring",
        "archive_path": str(archive),
        "source_record": "https://zenodo.org/records/15846963",
        "source_revision": FARM_B_REVISION,
        "archive_sha256": suite.source_archive_sha256,
        "expected_archive_sha256": FARM_B_ARCHIVE_SHA256,
        "source_manifest_sha256": suite.manifest_sha256,
        "event_info_sha256": suite.source_event_info_sha256,
        "feature_description_sha256": suite.source_feature_description_sha256,
        "license": FARM_B_LICENSE,
        "usage_profile": USAGE_PROFILE,
        "policy": {
            "adaptation_version": suite.policy_revision,
            "published_benchmark_equivalence_claimed": False,
            "feature_columns": list(suite.tasks[0].train_values.columns),
            "sampling_seconds": SAMPLING_SECONDS,
            "reference_days": REFERENCE_DAYS,
            "prefix_slots_per_task": 1008,
            "fit_cutoff": "reference_start - 86400 seconds",
            "fit_selection": "longest 0/2 finite-feature train run; exact 600s; earliest tie",
            "window": "autocorrelation <1/e on fit-only values; median lag clipped 10..100",
            "evaluation_mask": "mask gaps, nonfinite rows, and status outside 0/2; no status fill",
            "missing_value_fill": "fit medians fill missing cells; row masked; zero retained",
            "labels": "Scorer-only; inclusive PDM event positives unless masked; NRM all normal",
            "identity_boundary": "task identity, hashes, labels stay in Scorer; no Director path",
            "train_health_claim": "reference uses train role; entire train not assumed healthy",
        },
        "summary": summary,
        "tasks": task_reports,
        "measurement": {
            "driver": str(driver_path),
            "driver_sha256": hashlib.sha256(driver_path.read_bytes()).hexdigest(),
            "loader": str(loader_path),
            "loader_sha256": hashlib.sha256(loader_path.read_bytes()).hexdigest(),
            "scorer_adapter": str(scorer_adapter_path),
            "scorer_adapter_sha256": hashlib.sha256(scorer_adapter_path.read_bytes()).hexdigest(),
            "max_rss_kib": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),
            "raw_series_or_label_arrays_written": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--archive",
        type=Path,
        default=Path("/home/cachyos/ai-scientist/data/public/care/15846963/CARE_To_Compare.zip"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/ai-scientist/review-evidence/care-b-prefix-materialization.json"),
    )
    args = parser.parse_args()
    if args.output.is_symlink():
        raise ValueError("Farm B materialization evidence output cannot be a symlink")
    suite = load_care_farm_b_holdout(args.archive)
    report = build_report(
        suite,
        args.archive,
        Path(__file__),
        Path("harness/care_holdout.py"),
        Path("lab/scorer/care_holdout.py"),
    )
    encoded = json.dumps(report, sort_keys=True, indent=2, allow_nan=False).encode() + b"\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(encoded)
    print(
        json.dumps(
            {
                "report": str(args.output),
                "report_sha256": hashlib.sha256(encoded).hexdigest(),
                "tasks": report["summary"]["task_count"],
                "pdm": report["summary"]["pdm_count"],
                "nrm": report["summary"]["nrm_count"],
                "evaluation_rows": report["summary"]["evaluation_rows_total"],
                "aggregate_arrow_bytes": report["summary"]["aggregate_arrow_bytes"],
                "max_rss_kib": report["measurement"]["max_rss_kib"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"care-b-prefix-materialization-error={type(exc).__name__}")
        raise SystemExit(1) from exc
