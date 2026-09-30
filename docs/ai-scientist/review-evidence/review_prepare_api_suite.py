"""Provision only owned synthetic profiles; API/Director must create and run jobs."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from review_director20 import prepare_inputs, private_json
from review_scorer_queue import ROOT, engine
from sqlalchemy import delete, func, insert, select

from lab.db.schema import dataset_labels, dataset_profiles, run_tasks
from lab.director.suite_manifest import load_suite_manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cleanup", type=UUID)
    args = parser.parse_args()
    fixture_id = args.cleanup or uuid4()
    directory = ROOT / "data/runtime/api-suite-review" / str(fixture_id)
    dataset_ids = [f"director20-{fixture_id}-{index}" for index in range(4)]
    migrator, planner = engine("migrator"), engine("planner")
    try:
        if args.cleanup:
            with migrator.begin() as connection:
                references = connection.execute(
                    select(func.count()).select_from(run_tasks).where(
                        run_tasks.c.dataset_id.in_(dataset_ids)
                    )
                ).scalar_one()
                if references:
                    raise RuntimeError("API review profiles still belong to run task plans")
                result = connection.execute(
                    delete(dataset_profiles).where(dataset_profiles.c.dataset_id.in_(dataset_ids))
                )
            print(json.dumps({"fixture_id": str(fixture_id), "removed_profiles": result.rowcount}))
            return
        manifest, profiles, inputs = prepare_inputs(fixture_id, directory)
        manifest["suite_id"] = "synthetic.api-integration.v1"
        inputs["suite_manifest_sha256"] = private_json(Path(inputs["suite_file"]), manifest)
        assert [item["dataset_id"] for item in profiles] == dataset_ids
        with migrator.begin() as connection:
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
        loaded, tasks, digest = load_suite_manifest(Path(inputs["suite_file"]), planner)
        assert digest == inputs["suite_manifest_sha256"] and len(tasks) == 4
        record = {
            "checked_at": datetime.now(UTC).isoformat(),
            "fixture_id": str(fixture_id),
            "suite_id": loaded.suite_id,
            "scope": (
                "Prepared four owned synthetic dev profiles and 512 Scorer-only labels. "
                "Production Planner loader verified private label-free manifest. No lab.runs "
                "row was inserted/updated; no API, Director, Docker, Scorer, model or GPU ran. "
                "This is test input preparation, not API/AOS acceptance."
            ),
            "inputs": inputs,
            "profiles": [{key: value for key, value in profile.items() if key != "labels"}
                         for profile in profiles],
            "loader_task_count": len(tasks),
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "cleanup_command": (
                ".venv/bin/python docs/ai-scientist/review-evidence/review_prepare_api_suite.py "
                f"--cleanup {fixture_id}"
            ),
        }
        private_json(directory / "fixture-receipt.json", record)
        Path(__file__).with_name("api-suite-preparation.json").write_text(
            json.dumps(record, indent=2) + "\n"
        )
        print(json.dumps(record, indent=2))
    finally:
        migrator.dispose()
        planner.dispose()


if __name__ == "__main__":
    main()
