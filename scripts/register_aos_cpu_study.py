"""Explicit operator grant for an installed synthetic grid; never starts a run."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from lab.api.mode_experiments import _register_entry, atomic_private, canonical_document
from lab.api.registry import AosCpuStudy, SuiteEntry, load_suite_registry
from lab.director.parameter_grid import ParameterGridProvider


def register_study(
    *,
    runtime_root: Path,
    registry_path: Path,
    source_suite: str,
    owner_id: str,
    max_experiments: int,
    max_wall_seconds: int,
) -> SuiteEntry:
    """Create a new owner-bound identity, preserving the original local suite."""
    registry = load_suite_registry(registry_path, runtime_root)
    source = registry.get(source_suite)
    suite_path, grid_path = registry.verify_entry(source)
    if (
        source.provider != "mode-grid"
        or source.track != "mode"
        or source.public_dev_study is not None
        or source.aos_cpu_study is not None
        or source.provider_config_sha256 is None
        or grid_path is None
        or max_experiments > source.proposal_limit
    ):
        raise ValueError("an existing local synthetic grid and bounded budget are required")
    grid = ParameterGridProvider.load(
        grid_path,
        configuration_sha256=source.provider_config_sha256,
        registry_entry_sha256=registry.entry_sha256(source),
    )
    grant = AosCpuStudy(
        owner_id=owner_id,
        origin="aos",
        snapshot_sha256=grid.snapshot_sha256,
        provider_config_sha256=source.provider_config_sha256,
        max_experiments=max_experiments,
        max_wall_seconds=max_wall_seconds,
        model_tokens=0,
    )
    suite_id = f"aos-cpu-{grant.sha256[:48]}"
    document = json.loads(suite_path.read_bytes())
    document["suite_id"] = suite_id
    encoded = canonical_document(document)
    target = suite_path.parent / f"{suite_id}.json"
    atomic_private(target, encoded)
    entry = SuiteEntry.model_validate(
        source.model_dump(mode="json")
        | {
            "suite_id": suite_id,
            "suite_manifest_path": str(target.relative_to(registry.runtime_root)),
            "suite_manifest_sha256": hashlib.sha256(encoded).hexdigest(),
            "snapshot_sha256": grant.snapshot_sha256,
            "aos_cpu_study": grant.model_dump(mode="json"),
            "proposal_limit": max_experiments,
            "allowed_purposes": ["research"],
        }
    )
    # Existing locked registry publication rechecks source, installation and grid bytes.
    return _register_entry(entry, registry_path, runtime_root)[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--source-suite", required=True)
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--max-experiments", required=True, type=int)
    parser.add_argument("--max-wall-seconds", required=True, type=int)
    args = parser.parse_args()
    try:
        entry = register_study(
            runtime_root=args.runtime_root.resolve(strict=True),
            registry_path=args.registry,
            source_suite=args.source_suite,
            owner_id=args.owner_id,
            max_experiments=args.max_experiments,
            max_wall_seconds=args.max_wall_seconds,
        )
    except (OSError, ValueError, KeyError):
        parser.exit(2, "CPU study registration denied; verify source, owner and budget.\n")
    print(json.dumps({"suite_id": entry.suite_id, "registered": True, "started": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
