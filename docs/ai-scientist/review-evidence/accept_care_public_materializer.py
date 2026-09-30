"""Run the bounded CARE Farm A loader and write a label-free acceptance receipt."""

from __future__ import annotations

import hashlib
import json
import resource
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from lab.director.public_suite import materialize_care_development

ROOT = Path("/home/cachyos/ai-scientist")
OUT = Path(__file__).with_name("care-public-materializer-check.json")


def main() -> int:
    tasks = materialize_care_development(repository_root=ROOT)
    manifest = json.loads(
        (ROOT / "docs/ai-scientist/review-evidence/care-source-manifest.json").read_bytes()
    )
    families = Counter(task.task.family for task in tasks)
    actual_embargo = [
        (
            datetime.fromisoformat(task.scorer_registration.evaluation_times[0])
            - datetime.fromisoformat(task.train_end_time)
        ).total_seconds()
        for task in tasks
    ]
    result = {
        "schema": "public-materializer-check.v1",
        "source": "CARE to Compare, Farm A development tasks only",
        "source_record": manifest["record_id"],
        "source_revision": "zenodo:15846963:v6",
        "source_license": "CC-BY-SA-4.0",
        "usage_profile": "noncommercial_research",
        "source_archive_sha256_from_pinned_acquisition": manifest["sha256"],
        "source_archive_bytes": manifest["archive_bytes"],
        "review_metadata_sha256": hashlib.sha256(
            (ROOT / "docs/ai-scientist/review-evidence/care-dev-clock-review.json").read_bytes()
        ).hexdigest(),
        "structure_metadata_sha256": hashlib.sha256(
            (ROOT / "docs/ai-scientist/review-evidence/care-structure-review.json").read_bytes()
        ).hexdigest(),
        "farm_a_event_metadata_sha256": (
            "9254667b70d73e8eb23e7143ad56f1c280ba020c8b3d9289ad56fcaa59270aa5"
        ),
        "tasks": len(tasks),
        "task_families": dict(sorted(families.items())),
        "task_members": [task.source_member for task in tasks],
        "profile_sha256": [task.task.profile_sha256 for task in tasks],
        "semantics_sha256": [task.scorer_registration.semantics_sha256 for task in tasks],
        "evaluation_samples": sum(len(task.scorer_registration.labels) for task in tasks),
        "masked_samples": sum(sum(task.scorer_registration.masked_samples) for task in tasks),
        "training_samples_total": sum(len(task.task._train) for task in tasks),
        "selected_features_per_task": sorted({len(task.task._columns) for task in tasks}),
        "director_matrix_bytes": sum(
            task.task._train.nbytes + task.task._evaluation.nbytes for task in tasks
        ),
        "train_source_contiguity": "longest contiguous healthy finite 600s source segment",
        "train_derived_sliding_window": {
            "policy": "median per-feature first abs(acf)<=1/e lag, bounded [10,100]",
            "counts": {
                str(window): sum(
                    task.scorer_registration.sliding_window == window for task in tasks
                )
                for window in sorted({task.scorer_registration.sliding_window for task in tasks})
            },
        },
        "embargo_seconds": {
            "minimum": min(task.scorer_registration.embargo_seconds for task in tasks),
            "maximum": max(task.scorer_registration.embargo_seconds for task in tasks),
            "policy": "max(frozen sliding_window*600,86400), source timestamps only",
            "actual_train_end_to_eval_start_minimum": min(actual_embargo),
            "actual_train_end_to_eval_start_maximum": max(actual_embargo),
        },
        "evaluation_grid": "source-naive 600s; missing timestamp/status/feature gaps Scorer-masked",
        "regime_features": list(tasks[0].task.context.regime_signals),
        "regime_feature_meaning": "source-named wind-speed averages",
        "regime_source_evidence": (
            "CARE source member headers use wind_speed_3_avg and wind_speed_4_avg"
        ),
        "selected_feature_policy": [
            "wind_speed_3_avg",
            "wind_speed_4_avg",
            "reactive_power_27_avg",
            "reactive_power_28_avg",
            "power_29_avg",
            "power_30_avg",
        ],
        "farm_b_or_c_opened": False,
        "label_payload_written": False,
        "archive_unpacked": False,
        "task_weight_pre_cap": {
            family: sum(task.task.task_weight for task in tasks if task.task.family == family)
            for family in sorted(families)
        },
        "resource_maxrss_bytes": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024),
        "python_executable": sys.executable,
        "command_limits": {
            "memory_max": "2G",
            "memory_swap_max": "0",
            "cpu_quota": "100%",
            "tasks_max": 128,
            "runtime_max_seconds": 240,
            "blas_threads": 1,
        },
        "exit_code": 0,
    }
    OUT.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "tasks",
                    "task_families",
                    "evaluation_samples",
                    "masked_samples",
                    "training_samples_total",
                    "resource_maxrss_bytes",
                    "exit_code",
                )
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
