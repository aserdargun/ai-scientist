"""Verify the production suite loader against owned PostgreSQL profiles."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from review_director20 import install_fixture, prepare_inputs, private_json
from review_scorer_queue import ROOT, engine
from sqlalchemy import delete, insert, text

from lab.db.schema import dataset_profiles, runs
from lab.director.suite_manifest import load_suite_manifest


def main() -> None:
    run_id = uuid4()
    runtime = ROOT / "data/runtime/director-manifest-review" / str(run_id)
    manifest, profiles, inputs = prepare_inputs(run_id, runtime)
    path = Path(inputs["suite_file"])
    source = ROOT / "lab/director/suite_manifest.py"
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    evidence = {
        "checked_at": datetime.now(UTC).isoformat(),
        "run_id": str(run_id),
        "scope": (
            "Production label-free suite loader and actual Planner PostgreSQL role. "
            "Four synthetic profiles; no baseline, Docker, model or Director20 execution."
        ),
        "source_sha256": source_hash,
        "inputs": inputs,
        "checks": {},
    }
    migrator, planner = engine("migrator"), engine("planner")
    try:
        install_fixture(migrator, run_id, manifest, profiles, inputs)
        parsed, tasks, digest = load_suite_manifest(path, planner)
        evidence["checks"]["valid_manifest_and_exact_hash"] = (
            len(tasks) == len(parsed.tasks) == 4
            and digest == inputs["suite_manifest_sha256"]
            and all(task.frames()[1].shape == (128, 2) for task in tasks)
        )
        evidence["checks"]["task_ids_bound_to_hardcoding_guard"] = all(
            task.task_ids_for_guard == frozenset(item.task_id for item in tasks) for task in tasks
        )
        with planner.connect() as connection:
            evidence["checks"]["planner_has_no_label_read_grant"] = not connection.execute(
                text("SELECT has_table_privilege(current_user,'scorer.dataset_labels','SELECT')")
            ).scalar_one()
        for corruption in ("profile_hash", "sample_count", "label_field", "provenance_identity"):
            changed = copy.deepcopy(manifest)
            first = changed["tasks"][0]
            if corruption == "profile_hash":
                first["profile_sha256"] = "f" * 64
            elif corruption == "sample_count":
                first["evaluation"].pop()
            elif corruption == "label_field":
                first["labels"] = [0] * 128
            else:
                first["provenance"]["dataset_id"] = "other-unrelated-source"
            changed_path = runtime / f"{corruption}.json"
            private_json(changed_path, changed)
            rejected = False
            try:
                load_suite_manifest(changed_path, planner)
            except ValueError:
                rejected = True
            evidence["checks"][f"reject_{corruption}"] = rejected
        # Visibility is immutable even for the migrator: create a separate profile.
        holdout = copy.deepcopy(profiles[0])
        holdout["dataset_id"] += "-holdout"
        holdout["visibility"] = "holdout"
        profiles.append(holdout)
        with migrator.begin() as connection:
            connection.execute(
                insert(dataset_profiles).values(
                    **{key: value for key, value in holdout.items() if key != "labels"}
                )
            )
        changed = copy.deepcopy(manifest)
        changed["tasks"][0]["dataset_id"] = holdout["dataset_id"]
        changed["tasks"][0]["provenance"]["dataset_id"] = holdout["dataset_id"]
        holdout_path = runtime / "holdout.json"
        private_json(holdout_path, changed)
        rejected = False
        try:
            load_suite_manifest(holdout_path, planner)
        except ValueError:
            rejected = True
        evidence["checks"]["reject_non_dev_profile"] = rejected
    except Exception as error:
        evidence["error_type"] = type(error).__name__
        raise
    finally:
        with migrator.begin() as connection:
            connection.execute(delete(runs).where(runs.c.run_id == run_id))
            for profile in profiles:
                connection.execute(
                    delete(dataset_profiles).where(
                        dataset_profiles.c.dataset_id == profile["dataset_id"]
                    )
                )
        migrator.dispose()
        planner.dispose()
        evidence["owned_fixtures_removed"] = True
        evidence["source_unchanged"] = (
            hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
        )
        evidence["all_passed"] = (
            len(evidence["checks"]) == 8
            and all(evidence["checks"].values())
            and evidence["source_unchanged"]
            and "error_type" not in evidence
        )
        result = Path(__file__).with_name("director-manifest-review.json")
        result.write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence), flush=True)
    assert evidence["all_passed"]


if __name__ == "__main__":
    main()
