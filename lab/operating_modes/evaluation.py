"""Pure evaluator summaries; job budgets, decisions and persistence stay in Director."""

from __future__ import annotations

from typing import Any

import numpy as np

from .contracts import PredictionBatch
from .snapshots import SyntheticCase


def summarize_synthetic(case: SyntheticCase, prediction: PredictionBatch) -> dict[str, Any]:
    """Measure detection separately from quality faults; never feed labels to a model."""
    if len(prediction.rows) != len(case.event_labels):
        raise ValueError("prediction and evaluator labels must align")
    event = np.array(case.event_labels)
    quality = np.array(case.quality_labels)
    alarm = np.array([row.alarm is True for row in prediction.rows])
    available = np.array([row.omr_percent is not None for row in prediction.rows])
    healthy = ~event & ~quality
    process = event & ~quality
    hits = np.flatnonzero(alarm & process)
    starts = np.flatnonzero(process)
    scores = [row.omr_percent for row in prediction.rows if row.omr_percent is not None]
    return {
        "snapshot_sha256": case.snapshot.sha256,
        "model_sha256": prediction.model_sha256,
        "scenario": case.scenario,
        "seed": case.seed,
        "rows": len(prediction.rows),
        "scorable_rows": int(available.sum()),
        "quality_rows": int(quality.sum()),
        "out_of_mode_rows": sum(row.state == "out_of_mode" for row in prediction.rows),
        "false_alarm_rate": float(alarm[healthy].mean()) if healthy.any() else None,
        "process_point_recall": float(alarm[process].mean()) if process.any() else None,
        "first_detection_delay_rows": int(hits[0] - starts[0]) if len(hits) else None,
        "mean_omr_percent": float(np.mean(scores)) if scores else None,
        "max_omr_percent": float(np.max(scores)) if scores else None,
        "quality_scored_as_process_success": False,
    }
