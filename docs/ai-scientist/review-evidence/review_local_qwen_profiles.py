"""Install only the owned synthetic Scorer inputs for the real Qwen review.

The production suite loader validates the label-free manifest through the Planner
role. This utility never creates a Lab run, proposal, model call or experiment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import delete, func, insert, select, text

from lab.db.schema import dataset_labels, dataset_profiles, run_tasks
from lab.director.suite_manifest import load_suite_manifest
from review_local_qwen_inputs import ROOT, canonical, fixture_documents
from review_scorer_queue import engine


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepared_inputs(path: Path) -> tuple[dict, Path, dict, list[dict]]:
    record = json.loads(path.read_bytes())
    if record.get("schema") != "local-qwen-smoke-input-preparation.v1":
        raise ValueError("unexpected input preparation schema")
    if record.get("preparation_passed") is not True or record.get("execution_started") is not False:
        raise ValueError("input preparation was not successful and isolated")
    fixture_id = UUID(record["run_id"])
    directory = ROOT / "data/runtime/local-qwen-review" / str(fixture_id)
    manifest_path = directory / "suite.json"
    if (
        record["suite_file"] != str(manifest_path.relative_to(ROOT))
        or record["runtime_directory"] != str(directory.relative_to(ROOT))
        or directory.is_symlink() or manifest_path.is_symlink()
        or directory.resolve() != directory
        or directory.stat().st_mode & 0o777 != 0o700
        or manifest_path.stat().st_mode & 0o777 != 0o600
    ):
        raise ValueError("prepared input location or permissions changed")
    expected_sources = {
        "docs/ai-scientist/review-evidence/review_local_qwen_inputs.py",
        "docs/ai-scientist/review-evidence/review_director_synthetic_scenario.py",
    }
    if set(record["source_sha256"]) != expected_sources or any(
        sha(ROOT / name) != value for name, value in record["source_sha256"].items()
    ):
        raise ValueError("prepared fixture sources changed")
    manifest, profiles = fixture_documents(fixture_id)
    if sha(manifest_path) != record["suite_manifest_sha256"] or manifest_path.read_bytes() != canonical(manifest):
        raise ValueError("prepared manifest no longer matches its deterministic fixture")
    return record, manifest_path, manifest, profiles


def remove_profiles(connection, identities: list[str]) -> int:
    references = connection.execute(select(func.count()).select_from(run_tasks).where(
        run_tasks.c.dataset_id.in_(identities)
    )).scalar_one()
    if references:
        raise RuntimeError("owned fixture profiles still belong to a run")
    return connection.execute(delete(dataset_profiles).where(
        dataset_profiles.c.dataset_id.in_(identities)
    )).rowcount


def main(preparation: Path, output: Path, cleanup: bool) -> int:
    if output.exists():
        raise ValueError("refusing to overwrite review evidence")
    os.umask(0o077)
    prepared, manifest_path, manifest, profiles = prepared_inputs(preparation)
    paths = [Path(__file__), preparation, ROOT / "lab/db/schema.py",
             ROOT / "lab/director/suite_manifest.py",
             ROOT / "docs/ai-scientist/review-evidence/review_scorer_queue.py"]
    paths.extend(ROOT / name for name in prepared["source_sha256"])
    source_hashes = {str(path.relative_to(ROOT)): sha(path) for path in paths}
    identities = [profile["dataset_id"] for profile in profiles]
    record = {
        "schema": "local-qwen-smoke-profile-preparation.v1",
        "checked_at": datetime.now(UTC).isoformat(), "fixture_id": prepared["run_id"],
        "scope": "Owned synthetic Scorer profile installation and real Planner loader only; "
                 "no Lab run, candidate proposal, Docker experiment, model or GPU invocation.",
        "operation": "cleanup" if cleanup else "install",
        "suite_file": prepared["suite_file"],
        "suite_manifest_sha256": prepared["suite_manifest_sha256"],
        "source_sha256": source_hashes, "checks": {}, "execution_started": False,
    }
    migrator, planner, scorer = engine("migrator"), engine("planner"), engine("scorer")
    inserted = False
    try:
        with migrator.begin() as connection:
            connection.execute(text("SET LOCAL statement_timeout = '10s'"))
            connection.execute(text("SET LOCAL lock_timeout = '3s'"))
            if cleanup:
                record["removed_profiles"] = remove_profiles(connection, identities)
                record["checks"]["owned_profiles_removed"] = record["removed_profiles"] == 4
            else:
                present = connection.execute(select(func.count()).select_from(dataset_profiles).where(
                    dataset_profiles.c.dataset_id.in_(identities)
                )).scalar_one()
                if present:
                    raise ValueError("refusing to replace existing prepared profiles")
                for profile in profiles:
                    connection.execute(insert(dataset_profiles).values(
                        **{key: value for key, value in profile.items() if key != "labels"}
                    ))
                    connection.execute(insert(dataset_labels), [
                        {"dataset_id": profile["dataset_id"], "split_id": profile["split_id"],
                         "session_id": profile["session_id"], "sample_index": index,
                         "is_anomaly": bool(label)}
                        for index, label in enumerate(profile["labels"])
                    ])
        if not cleanup:
            inserted = True
            document, tasks, digest = load_suite_manifest(manifest_path, planner)
            with scorer.connect() as connection:
                labels = connection.execute(select(dataset_labels).where(
                    dataset_labels.c.dataset_id.in_(identities)
                ).order_by(dataset_labels.c.dataset_id, dataset_labels.c.sample_index)).mappings().all()
            expected = [(profile["dataset_id"], index, bool(label)) for profile in profiles
                        for index, label in enumerate(profile["labels"])]
            observed = [(row["dataset_id"], row["sample_index"], row["is_anomaly"]) for row in labels]
            with planner.connect() as connection:
                references = connection.execute(select(func.count()).select_from(run_tasks).where(
                    run_tasks.c.dataset_id.in_(identities)
                )).scalar_one()
            record.update({"profile_count": len(profiles), "scorer_label_count": len(labels),
                           "profiles": [{key: value for key, value in p.items() if key != "labels"}
                                        for p in profiles]})
            record["checks"].update({
                "four_production_planner_tasks": len(tasks) == 4,
                "suite_identity_and_manifest_match": document.suite_id == manifest["suite_id"]
                    and digest == prepared["suite_manifest_sha256"],
                "scorer_labels_exact": observed == expected,
                "no_run_tasks_created": references == 0,
                "no_labels_in_suite": b'"labels"' not in manifest_path.read_bytes(),
                "no_fake_provider_scenario": not (manifest_path.parent / "proposals.json").exists(),
            })
        record["checks"]["sources_unchanged"] = all(sha(ROOT / name) == value
                                                     for name, value in source_hashes.items())
        if not all(record["checks"].values()):
            raise RuntimeError("profile preparation verification failed")
    except Exception as error:
        # Database exception text may contain a credential-bearing connection URL.
        record["error_type"] = type(error).__name__
        if inserted:
            with migrator.begin() as connection:
                connection.execute(text("SET LOCAL statement_timeout = '10s'"))
                connection.execute(text("SET LOCAL lock_timeout = '3s'"))
                record["rollback_removed_profiles"] = remove_profiles(connection, identities)
    finally:
        for item in (migrator, planner, scorer):
            item.dispose()
        record["preparation_passed"] = not record.get("error_type") and all(record["checks"].values())
        with output.open("x") as stream:
            json.dump(record, stream, indent=2)
            stream.write("\n")
    print(json.dumps({"fixture_id": prepared["run_id"], "output": str(output),
                      "preparation_passed": record["preparation_passed"],
                      "execution_started": False, "checks": record["checks"],
                      "error_type": record.get("error_type")}))
    return 0 if record["preparation_passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preparation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cleanup", action="store_true")
    arguments = parser.parse_args()
    raise SystemExit(main(arguments.preparation.resolve(), arguments.output, arguments.cleanup))
