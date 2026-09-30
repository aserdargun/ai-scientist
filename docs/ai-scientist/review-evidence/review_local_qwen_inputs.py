"""Prepare label-separated synthetic inputs for the real local-model smoke run.

No proposal source, fake-provider scenario, database rows, model call or experiment
is created here. The separate execution review must use the production Director.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

from review_director_synthetic_scenario import fixture_tasks

ROOT = Path(__file__).resolve().parents[3]


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def fixture_documents(run_id: UUID) -> tuple[dict, list[dict]]:
    """Keep labels in separate in-memory Scorer installation records."""
    task_inputs, profiles = [], []
    for index, (train, evaluation, labels) in enumerate(fixture_tasks()):
        identity = {
            "dataset_id": f"local-qwen-smoke-{run_id}-{index}",
            "split_id": "review-v1", "session_id": "local",
        }
        profile_sha = digest(canonical({
            **identity, "columns": list(train.columns),
            "train": train.to_numpy().tolist(),
            "evaluation": evaluation.to_numpy().tolist(),
            "labels": labels.tolist(), "sampling_s": 60, "sliding_window": 4,
        }))
        task_inputs.append({
            **identity, "task_id": f"synthetic-task-{index}",
            "profile_sha256": profile_sha, "family": "EVT", "task_weight": 0.25,
            "provenance": {
                **identity, "source_manifest_sha256": profile_sha,
                "source_revision": "synthetic-review-v1", "license_id": "CC0-1.0",
                "attribution": "Local generated review fixture",
                "access_terms": "Synthetic inputs for real local-model integration review",
                "usage_profile": "noncommercial_research",
            },
            "context": {
                "seed": 0, "signals": list(train.columns), "regime_signals": [],
                "sampling_s": 60, "time_budget_s": 90.0,
            },
            "columns": list(train.columns), "train": train.to_numpy().tolist(),
            "evaluation": evaluation.to_numpy().tolist(),
        })
        profiles.append({
            **identity, "profile_sha256": profile_sha, "sample_count": len(labels),
            "sliding_window": 4, "visibility": "dev", "labels": labels.tolist(),
        })
    return {
        "schema": "director-suite.v1", "suite_id": "synthetic.local-qwen-smoke.v1",
        "suite_version": 1, "tasks": task_inputs,
    }, profiles


def main(output: Path) -> int:
    if output.exists():
        raise ValueError("refusing to overwrite input preparation evidence")
    os.umask(0o077)
    run_id = uuid4()
    runtime = ROOT / "data/runtime/local-qwen-review" / str(run_id)
    runtime.mkdir(parents=True, mode=0o700)
    manifest, profiles = fixture_documents(run_id)
    payload = canonical(manifest)
    manifest_path = runtime / "suite.json"
    with manifest_path.open("xb") as stream:
        stream.write(payload)
    sources = [Path(__file__), Path(__file__).with_name("review_director_synthetic_scenario.py")]
    record = {
        "schema": "local-qwen-smoke-input-preparation.v1", "run_id": str(run_id),
        "scope": "Only four synthetic EVT inputs; no fake proposals, DB installation, model invocation or scientific acceptance.",
        "runtime_directory": str(runtime.relative_to(ROOT)),
        "suite_file": str(manifest_path.relative_to(ROOT)),
        "suite_manifest_sha256": digest(payload), "suite_manifest_bytes": len(payload),
        "source_sha256": {str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in sources},
        "tasks": [{"task_id": task["task_id"], "profile_sha256": profile["profile_sha256"],
                   "train_rows": len(task["train"]), "evaluation_rows": len(task["evaluation"]),
                   "sensor_count": len(task["columns"])}
                  for task, profile in zip(manifest["tasks"], profiles, strict=True)],
        "required_execution": {"minimum_real_proposals": 6, "minimum_S1_calls": 2,
                               "minimum_S2_calls": 2, "real_guard_scorer_referee": True},
        "checks": {
            "four_synthetic_evt_tasks": len(manifest["tasks"]) == 4,
            "no_labels_in_suite": b'"labels"' not in payload and b'"is_anomaly"' not in payload,
            "private_directory": runtime.stat().st_mode & 0o777 == 0o700,
            "private_manifest": manifest_path.stat().st_mode & 0o777 == 0o600,
            "no_provider_scenario_written": sorted(p.name for p in runtime.iterdir()) == ["suite.json"],
            "repeatable_profiles": fixture_documents(run_id) == (manifest, profiles),
        },
        "execution_started": False,
    }
    record["preparation_passed"] = all(record["checks"].values())
    with output.open("x") as stream:
        json.dump(record, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"run_id": str(run_id), "output": str(output),
                      "preparation_passed": record["preparation_passed"],
                      "execution_started": False}))
    return 0 if record["preparation_passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    raise SystemExit(main(parser.parse_args().output))
