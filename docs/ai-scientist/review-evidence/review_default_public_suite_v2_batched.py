"""Main integrated loader/registration/Planner check, using the isolated public DB.

Derived from the Luna public-suite driver; original receipt remains immutable.
The manifest is retained privately for later scoring checks.
"""

from __future__ import annotations

import hashlib
import json
import resource
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

import lab.director.baselines as baselines
import lab.scorer.service as scorer_service
from lab.director.public_suite import build_default_public_suite_manifest
from lab.director.suite_manifest import load_suite_manifest
from lab.api.registry import SuiteEntry, SuiteRegistry
from lab.director.local_llm import provider_config_sha256

WORKTREE = Path(__file__).resolve().parents[3]
REPOSITORY = Path("/home/cachyos/ai-scientist")
PRIVATE_DB = "swapp_lab_m0_public_suite_3ddaa2d8"
REGISTRATION_DSN_PATH = WORKTREE / "data/runtime/parallel-m0/public-suite/data/runtime/postgres/migrator.dsn"
PLANNER_DSN_PATH = WORKTREE / "data/runtime/parallel-m0/public-suite/data/runtime/postgres/planner.dsn"
MANIFEST = WORKTREE / "data/runtime/parallel-m0/integration-024/default-public-suite-v2.json"
RECEIPT = Path(__file__).with_name("default-public-suite-v2-024-batched-check.json")
assert not RECEIPT.exists() and not MANIFEST.exists()

assert scorer_service.__file__.startswith(str(WORKTREE))
assert baselines.__file__.startswith(str(WORKTREE))
registration_url = make_url(REGISTRATION_DSN_PATH.read_text(encoding="utf-8").strip())
planner_url = make_url(PLANNER_DSN_PATH.read_text(encoding="utf-8").strip())
assert registration_url.database == planner_url.database == PRIVATE_DB
registration = create_engine(registration_url, hide_parameters=True)
planner = create_engine(planner_url, hide_parameters=True)
try:
    tasks, weights, byte_count, digest = build_default_public_suite_manifest(
        repository_root=REPOSITORY,
        trusted_registration_engine=registration,
        destination=MANIFEST,
    )
    raw = MANIFEST.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == digest
    assert b'"labels"' not in raw.lower()
    assert b'"timestamp"' not in raw.lower()
    runtime_root = WORKTREE / "data/runtime"
    entry = SuiteEntry(suite_id="public-ad-v1", track="anomaly", program_version="m0-review-v2",
                      suite_manifest_path=str(MANIFEST.relative_to(runtime_root)),
                      suite_manifest_sha256=digest, provider="local-qwen",
                      provider_config_sha256=provider_config_sha256(), proposal_limit=1)
    registry = SuiteRegistry((entry,), runtime_root)
    verified, scenario = registry.verify_entry(entry)
    assert verified == MANIFEST.resolve() and scenario is None
    document, loaded, loaded_digest = load_suite_manifest(MANIFEST, planner)
    assert document.suite_version == 2
    assert loaded_digest == digest
    assert byte_count == len(raw) == MANIFEST.stat().st_size
    matrix_bytes = sum(task._train.nbytes + task._evaluation.nbytes for task in loaded)
    receipt = {
        "schema": "default-public-suite-check.v1",
        "exit_status": 0,
        "database": PRIVATE_DB,
        "scorer_source": scorer_service.__file__,
        "baselines_source": baselines.__file__,
        "suite_id": document.suite_id,
        "suite_version": document.suite_version,
        "task_count": len(loaded),
        "task_families": sorted({task.family for task in loaded}),
        "independent_family_count": len(document.family_shares),
        "independent_families": sorted(document.family_shares),
        "type_shares": document.type_shares,
        "family_shares": document.family_shares,
        "weight_policy": weights.policy,
        "manifest_bytes": byte_count,
        "manifest_sha256": digest,
        "matrix_bytes": matrix_bytes,
        "max_rss_bytes": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024),
        "source_profile_hashes": sorted(task.profile_sha256 for task in loaded),
        "manifest_contains_label_payload": False,
        "api_registry_full_manifest_verified": True,
        "command_limits": {
            "memory_max": "2G",
            "memory_swap_max": 0,
            "cpu_quota": "100%",
            "tasks_max": 128,
            "runtime_max_seconds": 240,
            "blas_threads": 1,
        },
    }
    RECEIPT.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n")
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
finally:
    registration.dispose()
    planner.dispose()
