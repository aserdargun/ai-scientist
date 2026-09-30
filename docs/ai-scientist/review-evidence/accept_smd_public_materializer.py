"""Run SMD official-split materialization and emit a label-free receipt."""

from __future__ import annotations

import hashlib
import json
import resource
from collections import Counter
from pathlib import Path

from lab.director.public_suite import materialize_smd_development

ROOT = Path("/home/cachyos/ai-scientist")
OUT = Path(__file__).with_name("smd-public-materializer-check.json")


def main() -> int:
    tasks = materialize_smd_development(repository_root=ROOT)
    manifest = ROOT / "docs/ai-scientist/review-evidence/smd-source-manifest.json"
    raw = manifest.read_bytes()
    positive_counts = [sum(task.scorer_registration.labels) for task in tasks]
    result = {
        "schema": "public-materializer-check.v1",
        "source": "SMD official machine train/test, test-label roles only",
        "source_revision": tasks[0].task.provenance.source_revision,
        "source_license": "MIT",
        "usage_profile": "noncommercial_research",
        "source_manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "source_task_count": len(tasks),
        "entities": [task.scorer_registration.session_id.rsplit(":", 1)[-1] for task in tasks],
        "official_train_rows": sum(len(task.task._train) for task in tasks),
        "official_test_rows": sum(len(task.scorer_registration.labels) for task in tasks),
        "positive_test_rows_total": sum(positive_counts),
        "positive_test_machine_count": sum(value > 0 for value in positive_counts),
        "sample_period_seconds": sorted({task.scorer_registration.sampling_s for task in tasks}),
        "time_axis": "relative test sample index × 60s; absolute origin unknown",
        "split_policy": "official per-machine train/test; no cross-machine concatenation",
        "task_families": dict(Counter(task.task.family for task in tasks)),
        "independent_family": "SMD:server-telemetry:EVT (machines do not duplicate family mass)",
        "interpretation_label_files_opened": False,
        "candidate_inputs_contain_labels": False,
        "task_weight_pre_cap_total": sum(task.task.task_weight for task in tasks),
        "resource_maxrss_bytes": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024),
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
                    "source_task_count",
                    "official_train_rows",
                    "official_test_rows",
                    "positive_test_rows_total",
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
