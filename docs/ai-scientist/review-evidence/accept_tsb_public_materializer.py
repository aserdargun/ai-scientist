"""Bounded receipt for the real selected TSB public materializers."""

from __future__ import annotations

import hashlib
import json
import resource
from pathlib import Path

from lab.director.public_suite import materialize_tsb_development

ROOT = Path("/home/cachyos/ai-scientist")
OUTPUT = Path(__file__).with_name("tsb-public-materializer-check.json")
tasks = materialize_tsb_development(repository_root=ROOT)
receipt = {
    "schema": "tsb-public-materializer-check.v1",
    "exit_status": 0,
    "scope": "fixed Genesis, GECCO and CATSv2 TSB-AD-M members; dev labels remain scorer-side",
    "repository_root": str(ROOT),
    "source_manifest_sha256": hashlib.sha256(
        (ROOT / "docs/ai-scientist/review-evidence/tsb-archive-manifest.json").read_bytes()
    ).hexdigest(),
    "tasks": [
        {
            "dataset_id": item.task.dataset_id,
            "split_id": item.task.split_id,
            "task_id": item.task.task_id,
            "family": item.task.family,
            "independent_family": item.task.independent_family,
            "label_tier": item.task.label_tier,
            "source_member": item.source_member,
            "source_revision": item.task.provenance.source_revision,
            "source_license": item.task.provenance.license_id,
            "profile_sha256": item.task.profile_sha256,
            "train_shape": list(item.task._train.shape),
            "evaluation_shape": list(item.task._evaluation.shape),
            "cadence_seconds": item.task.context.sampling_s,
            "train_source_indices": [
                item.source_train_start_index,
                item.source_train_end_index,
            ],
            "evaluation_source_indices": [
                item.source_evaluation_start_index,
                item.source_evaluation_end_index,
            ],
            "source_row_offset": item.source_row_offset,
            "time_axis_policy": item.time_axis_policy,
            "sliding_window": item.scorer_registration.sliding_window,
            "embargo_seconds": item.embargo_seconds,
            "masked_evaluation_samples": sum(item.scorer_registration.masked_samples),
        }
        for item in tasks
    ],
    "aggregate_matrix_bytes": sum(
        item.task._train.nbytes + item.task._evaluation.nbytes for item in tasks
    ),
    "command_limits": {
        "memory_max": "2G",
        "memory_swap_max": 0,
        "cpu_quota": "100%",
        "tasks_max": 128,
        "runtime_max_seconds": 240,
        "blas_threads": 1,
    },
    "max_rss_bytes": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024),
    "dataset_bytes_or_labels": "not copied to receipt",
}
payload = json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n"
OUTPUT.write_text(payload, encoding="utf-8")
print(payload, end="")
