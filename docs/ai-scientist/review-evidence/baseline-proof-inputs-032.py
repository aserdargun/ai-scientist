"""Prepare four-family synthetic inputs for the later isolated baseline proof.

This uses production manifest and registry validation. It installs no database
rows and starts no API, worker, sandbox, provider, model or AOS process.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import runpy
from uuid import UUID, uuid4

from harness.contracts import FitContext
from lab.api.registry import load_suite_registry
from lab.director.contracts import SourceProvenance
from lab.director.suite import SuiteTask
from lab.director.suite_manifest import SuiteManifest, write_suite_manifest


ROOT = Path(__file__).resolve().parents[3]
BUILDER = "docs/ai-scientist/review-evidence/review_director_synthetic_scenario.py"


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def private_json(path: Path, value: object) -> str:
    payload = canonical(value)
    with path.open("xb") as stream:
        stream.write(payload)
    path.chmod(0o600)
    return hashlib.sha256(payload).hexdigest()


def prepare_fixture_inputs(runtime: Path, fixture_id: UUID) -> dict[str, object]:
    """Return receipts for a new private directory, without SQL or run admission."""
    if runtime.exists() or runtime.is_symlink():
        raise ValueError("fixture runtime directory must be new")
    runtime.mkdir(mode=0o700, parents=True)
    generated = runpy.run_path(str(ROOT / BUILDER))["fixture_tasks"]()
    if len(generated) != 4:
        raise ValueError("baseline fixture requires exactly four independent families")
    task_ids = frozenset(f"synthetic-task-{index}" for index in range(4))
    tasks = []
    profiles = []
    label_rows = []
    for index, (train, evaluation, labels) in enumerate(generated):
        dataset_id = f"baseline-proof-{fixture_id.hex}-{index}"
        identity = {"dataset_id": dataset_id, "split_id": "review-v1", "session_id": "local"}
        profile = {
            **identity,
            "columns": list(train.columns),
            "train": train.to_numpy().tolist(),
            "evaluation": evaluation.to_numpy().tolist(),
            "labels": labels.astype(bool).tolist(),
            "sampling_s": 60,
            "sliding_window": 4,
        }
        profile_sha = hashlib.sha256(canonical(profile)).hexdigest()
        provenance = SourceProvenance(
            **identity,
            source_manifest_sha256=profile_sha,
            source_revision="synthetic-review-v1",
            license_id="CC0-1.0",
            attribution="Locally generated synthetic correlated-sensor fixture.",
            access_terms="Generated test data; no external dataset or public-data claim.",
            usage_profile="noncommercial_research",
        )
        tasks.append(
            SuiteTask.from_frames(
                task_id=f"synthetic-task-{index}",
                **identity,
                profile_sha256=profile_sha,
                family="EVT",
                task_weight=0.25,
                independent_family=f"synthetic-process-{index}:sensors:EVT",
                label_tier="gold",
                provenance=provenance,
                context=FitContext(0, tuple(train.columns), (), 60, 90.0),
                train=train,
                evaluation=evaluation,
                task_ids_for_guard=task_ids,
            )
        )
        profiles.append(
            {
                **identity,
                "profile_sha256": profile_sha,
                "sample_count": len(labels),
                "sliding_window": 4,
                "visibility": "dev",
                "task_family": "EVT",
            }
        )
        label_rows.extend(
            {**identity, "sample_index": row, "is_anomaly": bool(label)}
            for row, label in enumerate(labels)
        )

    invalid_path = runtime / "invalid-two-task.json"
    try:
        write_suite_manifest(tuple(tasks[:2]), invalid_path, suite_id="invalid.fixture.v1")
    except ValueError as exc:
        negative_type = type(exc).__name__
        negative_reason = str(exc)
    else:
        raise AssertionError("two families unexpectedly passed the required family cap")
    if invalid_path.exists():
        raise AssertionError("rejected fixture unexpectedly produced a manifest")

    manifest_path = runtime / "suite.json"
    byte_count, manifest_sha = write_suite_manifest(
        tuple(tasks), manifest_path, suite_id="synthetic.baseline-proof.v1", suite_version=1
    )
    manifest = SuiteManifest.model_validate(json.loads(manifest_path.read_bytes()), strict=True)
    if (
        manifest.family_cap != 0.25
        or manifest.type_shares != {"EVT": 1.0}
        or len(manifest.family_shares) != 4
        or set(manifest.family_shares.values()) != {0.25}
        or any(task.task_weight != 0.25 for task in manifest.tasks)
    ):
        raise AssertionError("fixture does not preserve the production weight policy")
    scenario_sha = private_json(
        runtime / "scenario.json", {"schema": "review-fake-provider-input.v1", "proposals": []}
    )
    registry_sha = private_json(
        runtime / "registry.json",
        {
            "schema": "lab-suite-registry.v1",
            "suites": [
                {
                    "suite_id": manifest.suite_id,
                    "track": "anomaly",
                    "program_version": "fixture-v1",
                    "suite_manifest_path": "suite.json",
                    "suite_manifest_sha256": manifest_sha,
                    "provider": "fake-json",
                    "scenario_path": "scenario.json",
                    "scenario_sha256": scenario_sha,
                    "proposal_limit": 1,
                }
            ],
        },
    )
    registry = load_suite_registry(runtime / "registry.json", runtime)
    entry = registry.get(manifest.suite_id)
    registry.verify_entry(entry)
    private_profile_sha = private_json(
        runtime / "scorer-profile-inputs.json",
        {
            "schema": "synthetic-baseline-private-profile-inputs.v1",
            "profiles": profiles,
            "labels": label_rows,
        },
    )
    if any("labels" in task.model_dump() for task in manifest.tasks):
        raise AssertionError("label rows entered the candidate-visible manifest")
    return {
        "runtime": str(runtime),
        "fixture_id": str(fixture_id),
        "task_count": len(tasks),
        "profile_count": len(profiles),
        "label_row_count": len(label_rows),
        "planned_score_count": len(tasks) * 3 * 3,
        "measured_score_count": 0,
        "manifest_bytes": byte_count,
        "family_cap": manifest.family_cap,
        "type_shares": manifest.type_shares,
        "family_shares": manifest.family_shares,
        "manifest_sha256": manifest_sha,
        "scenario_sha256": scenario_sha,
        "registry_sha256": registry_sha,
        "registry_entry_sha256": registry.entry_sha256(entry),
        "private_profile_inputs_sha256": private_profile_sha,
        "two_task_rejection": {"type": negative_type, "reason": negative_reason},
        "sql_profile_installation": "not_run",
        "planner_database_readback": "not_run",
        "provider_constructed": False,
    }


def main() -> None:
    import lab.director.suite_manifest as manifest_module

    assert Path(manifest_module.__file__).resolve() == ROOT / "lab/director/suite_manifest.py"
    source_paths = (
        BUILDER,
        "lab/director/suite.py",
        "lab/director/suite_manifest.py",
        "lab/director/suite_weights.py",
        "lab/director/contracts.py",
        "lab/api/registry.py",
        "harness/contracts.py",
    )
    hashes = {
        path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in source_paths
    }
    fixture_id = uuid4()
    runtime = ROOT / "data/runtime/parallel-m0/baseline-proof-input-preparation" / fixture_id.hex
    result = prepare_fixture_inputs(runtime, fixture_id)
    assert hashes == {
        path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in source_paths
    }
    result.update(
        schema="baseline-proof-input-preparation.v1",
        scope="Synthetic input preparation and production manifest/registry validation only. No SQL profile installation or Planner database readback, run admission, baseline measurement, API, worker, sandbox, provider, model, GPU or AOS execution.",
        source_sha256=hashes,
        source_unchanged=True,
        driver_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    )
    evidence = ROOT / "docs/ai-scientist/review-evidence" / (
        f"baseline-proof-input-preparation-{fixture_id.hex[:12]}.json"
    )
    evidence.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"evidence": str(evidence.relative_to(ROOT)), **result}, sort_keys=True))


if __name__ == "__main__":
    main()
