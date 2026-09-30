"""Deterministic zero-token mode configurations, executed by normal experiment gates."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from lab.director.contracts import CandidateProposal
from lab.director.fake_llm import AgentContext, ProposalTurn
from lab.director.journal import canonical_bytes

GRID_SCHEMA = "operating-mode-parameter-grid.v1"
GRID_PROVIDER_ID = "operating-mode-grid.v1"
MAX_GRID_BYTES = 128 * 1024


def grid_document(snapshot_sha256: str, configurations: list[dict[str, Any]]) -> dict[str, Any]:
    """Freeze normalized configurations and generated candidate identities in list order."""
    from lab.operating_modes import ModeConfig, candidate_source

    if (
        len(snapshot_sha256) != 64
        or any(c not in "0123456789abcdef" for c in snapshot_sha256)
        or not 1 <= len(configurations) <= 35
    ):
        raise ValueError("grid requires one snapshot identity and 1..35 configurations")
    entries = []
    for raw in configurations:
        config = ModeConfig.model_validate(raw, strict=True)
        source = candidate_source(config)
        entries.append(
            {
                "configuration": config.model_dump(mode="json"),
                "configuration_sha256": hashlib.sha256(
                    canonical_bytes(config.model_dump(mode="json"))
                ).hexdigest(),
                "candidate_sha256": hashlib.sha256(source).hexdigest(),
            }
        )
    return {
        "schema": GRID_SCHEMA,
        "provider": GRID_PROVIDER_ID,
        "snapshot_sha256": snapshot_sha256,
        "entries": entries,
        "selection": "ordered_explicit_grid",
        "model_tokens": 0,
    }


class ParameterGridProvider:
    """Produces source/hypothesis only: all measurements remain Scorer-owned."""

    provider_id = GRID_PROVIDER_ID

    def __init__(
        self, document: dict[str, Any], *, configuration_sha256: str, registry_entry_sha256: str
    ):
        from lab.operating_modes import ModeConfig, candidate_source

        if document.get("schema") != GRID_SCHEMA or document.get("provider") != self.provider_id:
            raise ValueError("unsupported deterministic grid identity")
        if hashlib.sha256(canonical_bytes(document)).hexdigest() != configuration_sha256:
            raise ValueError("grid bytes differ from immutable configuration hash")
        entries = document.get("entries")
        if not isinstance(entries, list) or not 1 <= len(entries) <= 35:
            raise ValueError("grid entry count is invalid")
        expected = grid_document(
            document["snapshot_sha256"], [entry["configuration"] for entry in entries]
        )
        if document != expected:
            raise ValueError("grid candidate/configuration identities changed")
        self.configuration_sha256 = configuration_sha256
        self.registry_entry_sha256 = registry_entry_sha256
        self.snapshot_sha256 = document["snapshot_sha256"]
        self.entries = tuple(entries)
        self.sources = tuple(
            candidate_source(ModeConfig.model_validate(entry["configuration"], strict=True)).decode(
                "utf-8"
            )
            for entry in entries
        )

    @classmethod
    def load(
        cls, path: Path, *, configuration_sha256: str, registry_entry_sha256: str
    ) -> ParameterGridProvider:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_GRID_BYTES:
            raise ValueError("grid file must be a bounded private regular file")
        if path.stat().st_mode & 0o077:
            raise ValueError("grid configuration must have private permissions")
        document = json.loads(path.read_bytes())
        if not isinstance(document, dict):
            raise ValueError("grid must be a JSON object")
        return cls(
            document,
            configuration_sha256=configuration_sha256,
            registry_entry_sha256=registry_entry_sha256,
        )

    def __len__(self) -> int:
        return len(self.entries)

    def propose(self, context: AgentContext) -> ProposalTurn:
        index = context.experiment_number - 1
        if not 0 <= index < len(self.entries):
            raise RuntimeError("parameter_grid_exhausted")
        entry = self.entries[index]
        description = json.dumps(entry["configuration"], sort_keys=True, separators=(",", ":"))
        return ProposalTurn(
            proposal=CandidateProposal(
                hypothesis=f"Deterministic operating-mode configuration {index + 1}: {description}",
                move_type=context.move_type or "regime",
                candidate_source=self.sources[index],
                predicted_delta=0.0,
            ),
            messages=(
                f"{self.provider_id}; zero model tokens; no observed score prediction",
                f"snapshot={self.snapshot_sha256}; grid={self.configuration_sha256}; "
                f"configuration={entry['configuration_sha256']}",
            ),
            input_tokens=0,
            output_tokens=0,
        )
