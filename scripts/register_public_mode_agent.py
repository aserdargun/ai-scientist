"""Operator-only finite public local-agent registration; no run, model or GPU admission."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Literal

from lab.api.mode_experiments import ModeAgentRequest, ModeSnapshotStore, register_agent
from lab.api.mode_sources import authorize_snapshot
from lab.api.registry import PublicDevAgentStudy
from lab.director.local_llm import provider_config_sha256


def prepare_policy(
    store: ModeSnapshotStore,
    *,
    snapshot_sha256: str,
    owner_id: str,
    profile_set: Literal["smoke", "research"],
    experiments: int,
    wall_seconds: int,
    model_tokens: int,
) -> PublicDevAgentStudy:
    """Bind only the exact already installed, owner-authorized DEV snapshot."""
    authorize_snapshot(store, snapshot_sha256, owner_id)
    snapshot = store.public_snapshot(snapshot_sha256)
    if snapshot is None or store.installed(snapshot_sha256) is None:
        raise ValueError("installed public development snapshot required")
    binding = snapshot.binding
    return PublicDevAgentStudy(
        owner_id=owner_id,
        origin="local",
        snapshot_sha256=snapshot_sha256,
        binding_sha256=binding.sha256,
        source_suite_manifest_sha256=binding.source_suite_manifest_sha256,
        original_task_sha256=binding.original_task_sha256,
        profile_sha256=binding.original_task.profile_sha256,
        max_experiments=experiments,
        max_wall_seconds=wall_seconds,
        model_tokens=model_tokens,
        profile_set=profile_set,
        provider_config_sha256=provider_config_sha256(
            profile_set, "operating-mode-config.v1", public_fit=True
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--profile-set", required=True, choices=("smoke", "research"))
    parser.add_argument("--experiments", required=True, type=int)
    parser.add_argument("--wall-seconds", required=True, type=int)
    parser.add_argument("--model-tokens", required=True, type=int)
    parser.add_argument("--register", action="store_true", help="publish the explicit grant only")
    args = parser.parse_args()
    runtime = args.runtime_root.resolve(strict=True)
    store = ModeSnapshotStore(runtime / "mode-snapshots")
    policy = prepare_policy(
        store,
        snapshot_sha256=args.snapshot,
        owner_id=args.owner,
        profile_set=args.profile_set,
        experiments=args.experiments,
        wall_seconds=args.wall_seconds,
        model_tokens=args.model_tokens,
    )
    result = {"policy": policy.model_dump(mode="json"), "registered": False}
    if args.register:
        _, entry = register_agent(
            store,
            ModeAgentRequest(
                idempotency_key="operator-register-public-agent",
                snapshot_sha256=policy.snapshot_sha256,
                experiments=policy.max_experiments,
                wall_seconds=policy.max_wall_seconds,
                model_tokens=policy.model_tokens,
                profile_set=policy.profile_set,
            ),
            args.registry,
            runtime,
            public_dev_agent_study=policy,
            operator_grant=True,
        )
        result.update(registered=True, suite_id=entry.suite_id)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
