#!/usr/bin/env python3
"""One-task source-pipeline diagnostic; reads Farm B only, no full archive hash."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from harness import care_holdout as care

ARCHIVE = Path("/home/cachyos/ai-scientist/data/public/care/15846963/CARE_To_Compare.zip")

with zipfile.ZipFile(ARCHIVE, "r") as archive:
    events = care._parse_event_info(archive.read(care.EVENT_INFO_MEMBER))
    print("stage=event-info ok", flush=True)
    desc_hash = care._verify_feature_descriptions(archive.read(care.FEATURE_DESCRIPTION_MEMBER))
    print("stage=feature-description", desc_hash, flush=True)
    event_id = min(care.MEMBER_PINS)
    member, member_hash, _ = care.MEMBER_PINS[event_id]
    event = events[event_id]
    rows = care._read_source_rows(archive, event_id=event_id, event=event)
    print("stage=member-rows", len(rows), member_hash, flush=True)
    task = care._materialize_task(
        event_id=event_id,
        event=event,
        rows=rows,
        source_member=member,
        source_member_sha256=member_hash,
        source_archive_sha256=care.FARM_B_ARCHIVE_SHA256,
        manifest_sha256=care._suite_manifest_digest(),
    )
    print(
        "stage=materialized",
        json.dumps(
            {
                "task": task.task_id,
                "family": task.family,
                "fit_rows": len(task.train_values),
                "evaluation_rows": len(task.evaluation_values),
                "window": task.sliding_window,
            }
        ),
        flush=True,
    )
