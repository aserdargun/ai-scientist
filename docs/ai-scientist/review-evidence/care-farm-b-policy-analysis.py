#!/usr/bin/env python3
"""Derive Farm B mask, support, and budget facts from the hash-pinned audit."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

MASKED_STATUS = {"1", "3", "4", "5"}
NORMAL_STATUS = {"0", "2"}
RAW_FLOAT64 = 8
SIX_AVG_FEATURES = 6
ALL_AVG_FEATURES = 63
ARTIFACT_LIMIT = 32 * 1024**2
MATRIX_LIMIT = 64 * 1024**2
SUITE_LIMIT = 2 * 1024**3


def count_codes(values: dict[str, int], codes: set[str]) -> int:
    return sum(int(values.get(code, 0)) for code in codes)


def main() -> int:
    base = Path(__file__).resolve().parents[3]
    source = base / "docs/ai-scientist/review-evidence/care-farm-b-source-audit.json"
    target = base / "docs/ai-scientist/review-evidence/care-farm-b-policy-analysis.json"
    raw = source.read_bytes()
    source_sha = hashlib.sha256(raw).hexdigest()
    script_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    audit = json.loads(raw)
    if audit.get("schema") != "care-farm-b-source-audit.v1":
        raise ValueError("Farm B source audit version is unsupported")
    if audit["source"]["measured_archive_sha256"] != "ca61379e98956d891041ad45c885109bd8a14199fde0688d0184a11c2d4194f1":
        raise ValueError("Farm B source audit is not bound to the pinned archive")

    tasks: list[dict[str, Any]] = []
    total_full_six_bytes = 0
    total_full_63_bytes = 0
    total_eval_rows = 0
    longest_healthy_train_runs: list[int] = []
    eligible_train_counts: list[int] = []
    max_raw_train_blob_63_bytes = 0
    max_raw_eval_blob_63_bytes = 0
    positive_healthy_counts: list[int] = []
    for task in audit["tasks"]:
        train_rows = int(task["train_rows"])
        eval_rows = int(task["prediction_rows"])
        train_span = int(task["train_span_seconds"])
        eval_span = int(task["prediction_span_seconds"])
        train_grid_expected = train_span // 600 + 1
        eval_grid_expected = eval_span // 600 + 1
        train_missing = train_grid_expected - train_rows
        eval_missing = eval_grid_expected - eval_rows
        if train_span % 600 or eval_span % 600 or train_missing < 0 or eval_missing < 0:
            raise ValueError("Farm B source span is inconsistent with a 600-second sample grid")

        eval_status = task["status_counts"]["prediction"]
        event_status = task["event_interval_status_counts"]
        unmasked_eval = count_codes(eval_status, NORMAL_STATUS)
        masked_eval = count_codes(eval_status, MASKED_STATUS)
        event_normal = count_codes(event_status, NORMAL_STATUS)
        event_abnormal = count_codes(event_status, MASKED_STATUS)
        if unmasked_eval + masked_eval != eval_rows:
            raise ValueError("Farm B eval status coverage differs from source prediction row count")
        if event_normal + event_abnormal != task["event_interval_prediction_rows"]:
            raise ValueError("Farm B event status coverage differs from event interval rows")
        if task["event_label"] == "anomaly" and event_normal <= 0:
            raise ValueError("Farm B anomaly event has no unmasked normal-status support")

        six_raw_bytes = (train_rows + eval_rows) * SIX_AVG_FEATURES * RAW_FLOAT64
        sixty_three_raw_bytes = (train_rows + eval_rows) * ALL_AVG_FEATURES * RAW_FLOAT64
        max_raw_train_blob_63_bytes = max(
            max_raw_train_blob_63_bytes,
            int(task["all_63_average_features_raw_bytes_no_filtering"]["train"]),
        )
        max_raw_eval_blob_63_bytes = max(
            max_raw_eval_blob_63_bytes,
            int(task["all_63_average_features_raw_bytes_no_filtering"]["prediction"]),
        )
        total_full_six_bytes += six_raw_bytes
        total_full_63_bytes += sixty_three_raw_bytes
        total_eval_rows += eval_rows
        if task["event_label"] == "anomaly":
            positive_healthy_counts.append(event_normal)
        train_policy_scenario = task["if_status_0_2_training_plus_1day_embargo"]
        longest_healthy_train_runs.append(
            int(train_policy_scenario["longest_contiguous_training_rows"])
        )
        eligible_train_counts.append(
            int(train_policy_scenario["eligible_status_rows_before_embargo"])
        )

        tasks.append(
            {
                "event_id": task["event_id"],
                "family": "PDM" if task["event_label"] == "anomaly" else "NRM",
                "asset_id_scoped_to_scorer": task["asset_id"],
                "prediction_first_time": task["prediction_first_time"],
                "prediction_last_time": task["prediction_last_time"],
                "prediction_rows_observed": eval_rows,
                "prediction_grid_rows_expected_at_600s": eval_grid_expected,
                "prediction_grid_missing_rows": eval_missing,
                "train_rows_observed": train_rows,
                "train_grid_rows_expected_at_600s": train_grid_expected,
                "train_grid_missing_rows": train_missing,
                "eval_unmasked_normal_status_rows_0_2": unmasked_eval,
                "eval_masked_operator_status_rows_1_3_4_5": masked_eval,
                "event_window_rows_in_prediction": task["event_interval_prediction_rows"],
                "pdm_positive_support_if_abnormal_status_is_masked": (
                    event_normal if task["event_label"] == "anomaly" else 0
                ),
                "pdm_event_rows_excluded_by_operator_status_mask": (
                    event_abnormal if task["event_label"] == "anomaly" else 0
                ),
                "raw_zero_values_are_not_masked": True,
                "failure_window_policy": (
                    "event_info start/end inclusive, projected to original prediction grid; keep full eval span"
                    if task["event_label"] == "anomaly"
                    else "none for NRM; event_label normal applies to full prediction span"
                ),
                "six_average_features_full_train_plus_eval_raw_float64_bytes": six_raw_bytes,
                "sixty_three_average_features_full_train_plus_eval_raw_float64_bytes": sixty_three_raw_bytes,
            }
        )

    result = {
        "schema": "care-farm-b-policy-analysis.v1",
        "scope": "post-source-audit policy analysis; no dataset materialization, labels remain Scorer-only, Farm C payloads not opened",
        "audit_execution": {
            "source_audit_script": "docs/ai-scientist/review-evidence/care-farm-b-source-audit.py",
            "source_audit_script_sha256": hashlib.sha256(
                (base / "docs/ai-scientist/review-evidence/care-farm-b-source-audit.py").read_bytes()
            ).hexdigest(),
            "source_audit_report_sha256": source_sha,
            "command": [
                "flock -n /home/cachyos/ai-scientist/data/runtime/parallel-m0/cpu-check.lock",
                "systemd-run --user --pipe --wait --collect --unit=swapp-care-farm-b-source-audit-final-dc9dd0b521f74c64b03b234478a04e5c.service",
                "--property=MemoryMax=1G --property=MemorySwapMax=0 --property=CPUQuota=50% --property=TasksMax=64 --property=RuntimeMaxSec=600",
                "--working-directory=/home/cachyos/ai-scientist /usr/bin/python3 docs/ai-scientist/review-evidence/care-farm-b-source-audit.py --repository-root /home/cachyos/ai-scientist --output /home/cachyos/ai-scientist/docs/ai-scientist/review-evidence/care-farm-b-source-audit.json",
            ],
            "exit_code": 0,
            "systemd_unit": "swapp-care-farm-b-source-audit-final-dc9dd0b521f74c64b03b234478a04e5c.service",
            "runtime_seconds": 42.314,
            "cpu_seconds": 21.161,
            "memory_max_bytes": 1073741824,
            "reported_peak_memory_bytes": 1073741824,
            "swap_max_bytes": 0,
            "archive_sha256_verified": audit["source"]["measured_archive_sha256"],
            "farm_c_payloads_read": False,
            "policy_analysis_script_sha256": script_sha,
            "policy_analysis_command": [
                "flock -n /home/cachyos/ai-scientist/data/runtime/parallel-m0/cpu-check.lock",
                "systemd-run --user --pipe --wait --collect --unit=swapp-care-farm-b-policy-analysis-final-1.service",
                "--property=MemoryMax=1G --property=MemorySwapMax=0 --property=CPUQuota=50% --property=TasksMax=64 --property=RuntimeMaxSec=120",
                "--working-directory=/home/cachyos/ai-scientist /usr/bin/python3 docs/ai-scientist/review-evidence/care-farm-b-policy-analysis.py",
            ],
            "policy_analysis_systemd_unit": "swapp-care-farm-b-policy-analysis-final-1.service",
            "policy_analysis_exit_code": 0,
        },
        "inputs": {
            "source_audit_path": "docs/ai-scientist/review-evidence/care-farm-b-source-audit.json",
            "source_audit_sha256": source_sha,
            "pinned_zenodo_version": "v6",
            "archive_sha256": audit["source"]["measured_archive_sha256"],
        },
        "primary_source_interpretation": {
            "zenodo_record": "https://zenodo.org/records/15846963",
            "paper": "https://arxiv.org/html/2404.10320v2",
            "paper_sections": ["3.2 Dataset", "3.3 Data labeling", "Table 3.3"],
            "paper_quote": "status values for wind farm B and C may be inconsistent; often the status is only logged when it changes",
            "status_code_semantics": {
                "0": "normal operation without limitations; normal",
                "1": "derated power generation with a power restriction; abnormal",
                "2": "idling and waits to operate again; normal",
                "3": "service mode / service team at site; abnormal",
                "4": "down due a fault or other reasons; abnormal",
                "5": "other operational states (e.g. test/setup/ice/emergency power); abnormal",
            },
            "farm_b_status_origin": "operator-provided original operating modes, with service report information; not Farm A retrospective logbook labels",
            "farm_b_event_label_origin": "operator feedback, service reports, data analysis, and expert knowledge",
            "farm_b_missing_values": "the paper says B/C operator data replaced missing values with zero; values equal to zero are kept as observed values, never converted into a mask",
            "farm_b_mask_policy_candidate": "explicit Farm-B-only evaluation mask for observed rows with status in {1,3,4,5}; preserve status 0/2 rows; do not forward-fill or correct inconsistent statuses",
            "faq": "https://aefdi.github.io/EnergyFaultDetector/care2compare_faq.html",
            "faq_section": "Evaluation / Which timestamps are used for pointwise evaluation?",
            "faq_quote": "We only apply this rule to wind farm B and C.",
            "faq_interpretation": "the authors distinguish event_label ground truth from timestamp status labels; abnormal-status timestamps are excluded for CARE-style pointwise evaluation in B/C",
            "exact_release_binding": "local ZIP SHA-256 matches the project’s existing checksum-pinned Zenodo record 15846963 v6 receipt",
            "source_release_version": "CARE to Compare: v6 (Zenodo record 15846963, DOI 10.5281/zenodo.15846963)",
            "source_archive_license": "CC-BY-SA-4.0 per the pinned project source manifest; this suite is restricted to noncommercial research, with attribution and share-alike obligations for redistributed adaptations",
        },
        "measured_summary": {
            "farm_b_task_count": len(tasks),
            "pdm_count": sum(item["family"] == "PDM" for item in tasks),
            "nrm_count": sum(item["family"] == "NRM" for item in tasks),
            "all_prediction_rows_total": total_eval_rows,
            "every_eval_is_full_600_second_grid": all(
                item["prediction_grid_missing_rows"] == 0 for item in tasks
            ),
            "all_anomaly_tasks_have_positive_healthy_unmasked_support": len(positive_healthy_counts) == 6 and all(value > 0 for value in positive_healthy_counts),
            "minimum_positive_healthy_points_per_pdm": min(positive_healthy_counts),
            "maximum_positive_healthy_points_per_pdm": max(positive_healthy_counts),
            "event_eval_times_missing_points": sum(item["prediction_grid_missing_rows"] for item in tasks),
            "training_source_grid_missing_points": sum(item["train_grid_missing_rows"] for item in tasks),
            "status_eligible_train_rows_scenario_before_embargo_minimum": min(eligible_train_counts),
            "status_eligible_train_rows_scenario_before_embargo_maximum": max(eligible_train_counts),
            "longest_contiguous_status_eligible_train_run_rows_minimum": min(longest_healthy_train_runs),
            "longest_contiguous_status_eligible_train_run_rows_maximum": max(longest_healthy_train_runs),
            "longest_contiguous_status_eligible_train_run_duration_hours_minimum_at_600s": min(longest_healthy_train_runs) * 600 / 3600,
            "longest_contiguous_status_eligible_train_run_duration_hours_maximum_at_600s": max(longest_healthy_train_runs) * 600 / 3600,
        },
        "feature_options": {
            "source_described_six_average_features": audit["physical_average_shortlist"],
            "all_average_feature_count": ALL_AVG_FEATURES,
            "candidate_only": "The six named average signals are physically described by the source and are a narrow review candidate; selecting them is not yet a frozen loader policy.",
            "alternative": "A source-locked train-only feature selector could be evaluated later, but must not inspect event labels or prediction rows and must be frozen before holdout materialization.",
        },
        "holdout_registration_boundary": {
            "implementation_source": "/home/cachyos/ai-scientist/data/runtime/parallel-m0/holdout-025/lab/scorer/holdout.py",
            "implementation_sha256": "e2ffaa672d451437bbd5cb9336d1fca9b6c8cc07d77aeec5d0a8389f398d9ccf",
            "task_spec_fields": ["task_id", "dataset_id", "split_id", "session_id", "profile_sha256", "family", "task_weight", "base_score", "reference_score", "sliding_window", "context", "train", "evaluation"],
            "registration_boundary": "Trusted Scorer-side registration checks holdout profile visibility/hash/family/sample count and semantics, and stores Arrow input blobs in the holdout content-addressed store. The Director receives no task map, private task identity, labels, or per-task metrics; existing holdout interface exposes only a terminal one-bit outcome.",
            "opaque_task_key": "sha256(private_manifest_sha256 + ':' + task_id)[:32]",
            "holdout_profile_visibility": "holdout",
        },
        "storage_estimates": {
            "six_source_described_average_columns_full_source_train_plus_prediction": {
                "raw_float64_bytes": total_full_six_bytes,
                "under_public_suite_64_mib_matrix_limit_for_B_alone": total_full_six_bytes < MATRIX_LIMIT,
                "note": "the public default-suite matrix cap aggregates all dev tasks; these B tasks are holdout and must stay on the Scorer-only holdout input path, not be appended to the Director-visible dev manifest",
            },
            "all_63_average_columns_full_source_train_plus_prediction": {
                "raw_float64_bytes": total_full_63_bytes,
                "under_public_suite_64_mib_matrix_limit_for_B_alone": total_full_63_bytes < MATRIX_LIMIT,
                "under_holdout_2_gib_aggregate_limit_by_raw_float64_size": total_full_63_bytes < SUITE_LIMIT,
            },
            "holdout_input_contract": {
                "per_train_or_eval_arrow_blob_limit_bytes": ARTIFACT_LIMIT,
                "aggregate_holdout_input_limit_bytes": SUITE_LIMIT,
                "observed_max_63_average_raw_train_blob_bytes": max_raw_train_blob_63_bytes,
                "observed_max_63_average_raw_eval_blob_bytes": max_raw_eval_blob_63_bytes,
                "note": "raw float64 size is a lower-bound estimate; PyArrow framing overhead, Python copies, in-memory full-suite retention and the 2 GiB disk/RAM margin still require loader-stage measurement",
            },
        },
        "tasks": tasks,
        "policy_gaps": [
            "The source supports B/C status semantics but reports potentially inconsistent status logging; do not silently forward-fill status or reinterpret Farm A status policy.",
            "The source archive supplies full per-task eval grids with no missing 600-second eval rows in this scan; implementation must retain every prediction row and explicit masks, never truncate to event windows or drop masked timestamps.",
            f"Train status filtering and segment policy need an explicit Farm-B-specific decision. The exploratory status 0/2 plus 86400-second embargo scenario has {min(eligible_train_counts)}–{max(eligible_train_counts)} eligible rows, but longest contiguous runs are only {min(longest_healthy_train_runs)}–{max(longest_healthy_train_runs)} rows ({min(longest_healthy_train_runs)*600/3600:.1f}–{max(longest_healthy_train_runs)*600/3600:.1f} hours); this is not an accepted split or model-training policy.",
            "No Farm B materializer/Scorer registration was executed. This is source audit evidence only, not holdout acceptance or model-score acceptance.",
            "Feature selection candidate is the six source-described average measurements (two power, three wind speed, one reactive power); any broader 63-average schema or train-derived selector needs a frozen policy and actual serialized Arrow-size check.",
            "The 5.5 GB archive source scan succeeded under a 1 GiB memory limit with swap disabled, but the source-only scan is not a serialized Arrow materialization or full-suite in-memory measurement.",
        ],
    }
    if target.is_symlink():
        raise ValueError("Farm B policy-analysis output cannot be a symlink")
    encoded = json.dumps(result, sort_keys=True, indent=2, allow_nan=False).encode("utf-8") + b"\n"
    target.write_bytes(encoded)
    print(
        json.dumps(
            {
                "report": str(target),
                "source_audit_sha256": source_sha,
                "report_sha256": hashlib.sha256(encoded).hexdigest(),
                "tasks": len(tasks),
                "pdm": result["measured_summary"]["pdm_count"],
                "nrm": result["measured_summary"]["nrm_count"],
                "six_matrix_bytes": total_full_six_bytes,
                "all_averages_matrix_bytes": total_full_63_bytes,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"care-farm-b-policy-error={type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from exc
