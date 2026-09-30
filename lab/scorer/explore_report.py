"""Read EXPLORE provenance only through the sealed terminal finalization chain."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection

from lab.director.explore import ExploreEpisode
from lab.scorer.jobs import read_artifact_bytes


def terminal_explore_provenance(
    connection: Connection,
    *,
    run_id: UUID,
    generation: int,
    execution_sha256: str,
    artifact_root: Path,
) -> dict[str, Any]:
    """Follow a SQL-verified marker to its hash-bound state; admit no new work."""
    seal = (
        connection.execute(
            text(
                "SELECT generation,execution_sha256,source_kind,source_sha256 "
                "FROM lab.terminal_finalizations WHERE run_id=:run"
            ),
            {"run": run_id},
        )
        .mappings()
        .one_or_none()
    )
    if seal is None or seal["source_kind"] != "holdout":
        return {}
    verified = connection.execute(
        text("SELECT to_jsonb(lab.verify_terminal_finalization(:run,:generation,:sha))"),
        {"run": run_id, "generation": generation, "sha": execution_sha256},
    ).scalar_one()
    if (
        not isinstance(verified, dict)
        or verified.get("run_id") != str(run_id)
        or verified.get("generation") != seal["generation"]
        or verified.get("execution_sha256") != execution_sha256
        or verified.get("source_sha256") != seal["source_sha256"]
    ):
        raise ValueError("terminal EXPLORE seal belongs to another execution")
    marker = json.loads(
        read_artifact_bytes(seal["source_sha256"], artifact_root=artifact_root, max_bytes=16_384)
    )
    if (
        not isinstance(marker, dict)
        or marker.get("schema") != "director-holdout-state-application.v1"
        or marker.get("run_id") != str(run_id)
        or not isinstance(marker.get("state_sha256"), str)
    ):
        raise ValueError("terminal EXPLORE marker identity is invalid")
    state_sha256 = marker["state_sha256"]
    state = json.loads(
        read_artifact_bytes(state_sha256, artifact_root=artifact_root, max_bytes=512 * 1024)
    )
    if not isinstance(state, dict) or state.get("run_id") != str(run_id):
        raise ValueError("terminal EXPLORE state belongs to another run")
    if state.get("termination_reason") is None:
        return {}
    episode = ExploreEpisode.model_validate_json(json.dumps(state.get("explore_episode")))
    if (
        state.get("termination_reason") != "explore_exhausted"
        or state.get("loop_phase") != "HOLDOUT_CHECK"
        or episode.intent.run_id != run_id
        or episode.phase != "HOLDOUT_CHECK"
        or state.get("explore_proposals") != 10
        or state.get("explore_family") != episode.intent.target_family
        or state.get("completed_proposals") != episode.intent.start_ordinal + 9
        or state.get("next_ordinal") != episode.intent.start_ordinal + 10
    ):
        raise ValueError("terminal EXPLORE reason differs from its ten terminal receipts")
    return {
        "termination_reason": "explore_exhausted",
        "termination_provenance": {
            "episode_id": episode.intent.episode_id,
            "seal_generation": seal["generation"],
            "writer_generation": generation,
            "target_family": episode.intent.target_family,
            "terminal_receipt_sha256": [item.terminal_sha256 for item in episode.resolutions],
            "terminal_state_sha256": state_sha256,
            "holdout_marker_sha256": seal["source_sha256"],
        },
    }
