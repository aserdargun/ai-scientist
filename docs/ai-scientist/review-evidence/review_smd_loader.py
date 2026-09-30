"""Exercise the production SMD loader over all 28 pinned source machines."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from harness.public_data import load_smd_machine, load_smd_manifest

ROOT = Path(__file__).resolve().parents[3]
MANIFEST_PATH = ROOT / "docs/ai-scientist/review-evidence/smd-source-manifest.json"
LOADER_PATH = ROOT / "harness/public_data.py"


def main() -> None:
    manifest = load_smd_manifest(MANIFEST_PATH)
    entities = sorted(
        {record.entity for record in manifest.source_files if record.entity is not None}
    )
    summary = []
    for entity in entities:
        split = load_smd_machine(manifest, ROOT, entity, sliding_window=100)
        summary.append(
            {
                "entity": entity,
                "session_id": split.session_id,
                "train_samples": len(split.train_values),
                "eval_samples": len(split.eval_values),
                "sensor_count": split.eval_values.shape[1],
                "eval_anomaly_samples": int(split.eval_labels.sum()),
                "sampling_seconds": split.sample_period_s,
                "time_axis_policy": split.time_axis_policy,
            }
        )
    print(
        json.dumps(
            {
                "schema": "smd-production-loader-check.v1",
                "result": "pass",
                "benchmark_acceptance": False,
                "sliding_window": 100,
                "window_usage": "loader smoke only; benchmark window policy unresolved",
                "manifest_sha256": hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest(),
                "loader_sha256": hashlib.sha256(LOADER_PATH.read_bytes()).hexdigest(),
                "entity_count": len(summary),
                "train_samples": sum(row["train_samples"] for row in summary),
                "eval_samples": sum(row["eval_samples"] for row in summary),
                "sensor_counts": sorted({row["sensor_count"] for row in summary}),
                "time_axis_policies": sorted({row["time_axis_policy"] for row in summary}),
                "entities": summary,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
