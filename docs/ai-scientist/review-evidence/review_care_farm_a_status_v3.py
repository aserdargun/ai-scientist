"""Source-bound CARE Farm A exposure check; reads the complete Adev task set."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from lab.director.public_suite import materialize_care_development


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.repository_root.resolve(strict=True)
    materialized = materialize_care_development(repository_root=root)
    rows: list[dict[str, object]] = []
    for item in materialized:
        registration = item.scorer_registration
        masks = registration.masked_samples
        labels = registration.labels
        windows = registration.failure_windows
        event = {
            index
            for start, stop in windows
            for index in range(start, stop + 1)
        }
        unmasked_total = sum(not value for value in masks)
        positive_unmasked = sum(
            label and not mask for label, mask in zip(labels, masks, strict=True)
        )
        positive_masked = sum(label and mask for label, mask in zip(labels, masks, strict=True))
        negative_unmasked = sum(
            not label and not mask for label, mask in zip(labels, masks, strict=True)
        )
        failure_window_unmasked = sum(not masks[index] for index in event)
        rows.append(
            {
                "task_id": item.task.task_id,
                "family": item.task.family,
                "samples": registration.sample_count,
                "masked": sum(masks),
                "unmasked_total": unmasked_total,
                "negative_unmasked_healthy": negative_unmasked,
                "positive_unmasked": positive_unmasked,
                "positive_masked": positive_masked,
                "failure_window_unmasked": failure_window_unmasked,
            }
        )
    pdm = [row for row in rows if row["family"] == "PDM"]
    nrm = [row for row in rows if row["family"] == "NRM"]
    result = {
        "schema": "care-farm-a-status-policy-v3-materialization.v1",
        "suite_version": 3,
        "task_count": len(rows),
        "pdm_count": len(pdm),
        "nrm_count": len(nrm),
        "all_pdm_have_healthy_exposure": all(row["negative_unmasked_healthy"] > 0 for row in pdm),
        "all_pdm_have_unmasked_event_support": all(
            row["positive_unmasked"] > 0
            and row["positive_unmasked"] == row["failure_window_unmasked"]
            for row in pdm
        ),
        "all_nrm_have_healthy_exposure": all(row["negative_unmasked_healthy"] > 0 for row in nrm),
        "all_tasks_have_no_positive_masked_points": all(
            row["positive_masked"] == 0 for row in rows
        ),
        "tasks": rows,
    }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    if (
        result["task_count"] != 22
        or result["pdm_count"] != 12
        or result["nrm_count"] != 10
        or not result["all_pdm_have_healthy_exposure"]
        or not result["all_pdm_have_unmasked_event_support"]
        or not result["all_nrm_have_healthy_exposure"]
        or not result["all_tasks_have_no_positive_masked_points"]
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
