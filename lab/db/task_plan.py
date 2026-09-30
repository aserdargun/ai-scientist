"""Canonical immutable identity hash for the trusted task set of a run."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any
from uuid import UUID

TASK_PLAN_FIELDS = (
    "experiment_id",
    "evaluation_kind",
    "task_id",
    "seed",
    "candidate_sha256",
    "dataset_id",
    "split_id",
    "session_id",
)


def canonical_task_plan_digest(rows: Sequence[Any]) -> str:
    """Hash the stable, complete set of task identities while excluding timestamps."""
    values = [
        {field: row[field] for field in TASK_PLAN_FIELDS}
        for row in sorted(
            rows,
            key=lambda row: (
                row["experiment_id"],
                row["evaluation_kind"],
                row["task_id"],
                row["seed"],
            ),
        )
    ]
    encoded = json.dumps(
        values,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def run_plan_lock_key(run_id: UUID) -> int:
    """Return a signed 64-bit PostgreSQL advisory key scoped to one run."""
    return int.from_bytes(hashlib.sha256(run_id.bytes).digest()[:8], "big", signed=True)
