"""Read the bounded public development update; never infer acceptance from it."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

PROGRESS_PATH = Path(__file__).resolve().parents[1] / "docs/ai-scientist/development-progress.json"
MAX_PROGRESS_BYTES = 8192
_FIELDS = {
    "schema",
    "updated_at",
    "current_work",
    "latest_result",
    "next_step",
    "cpu_status",
    "gpu_status",
}
_STATES = {"pending", "running", "passed", "blocked", "quarantined"}


def read_development_progress() -> dict[str, Any] | None:
    """Missing or invalid updates are unavailable, rather than cached as current."""
    try:
        if PROGRESS_PATH.is_symlink() or not PROGRESS_PATH.is_file():
            return None
        with PROGRESS_PATH.open("rb") as stream:
            raw = stream.read(MAX_PROGRESS_BYTES + 1)
        if len(raw) > MAX_PROGRESS_BYTES:
            return None
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != _FIELDS:
            return None
        if value["schema"] != "development-progress.v1":
            return None
        for key in ("current_work", "latest_result", "next_step", "updated_at"):
            item = value[key]
            if not isinstance(item, str) or not 1 <= len(item) <= 600:
                return None
            if any(ord(char) < 32 for char in item):
                return None
        if any(value[key] not in _STATES for key in ("cpu_status", "gpu_status")):
            return None
        updated = datetime.fromisoformat(value["updated_at"].replace("Z", "+00:00"))
        if updated.tzinfo is None:
            return None
        return value
    except (OSError, ValueError, TypeError):
        return None
