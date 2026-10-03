"""Owner-scoped synthetic CPU registry proofs; no AOS service or real PostgreSQL."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from lab.api.mode_experiments import (
    ModeSnapshotStore,
    SyntheticSnapshotRequest,
    atomic_private,
    canonical_document,
)
from lab.api.registry import AosCpuStudy, SuiteEntry, SuiteRegistry
from lab.director.parameter_grid import grid_document
from lab.scorer.mode_snapshot import install_snapshot
from tests.test_api_and_scorer import _engine
from tests.test_mode_experiments041 import _mock_snapshot_rpc_transport


@pytest.fixture
def aos_cpu_installation(tmp_path, monkeypatch):
    runtime = tmp_path / "data/runtime"
    store = ModeSnapshotStore(runtime / "mode-snapshots")
    digest = store.create_synthetic(SyntheticSnapshotRequest(scenario="step"))["snapshot_sha256"]
    engine = _engine()
    _mock_snapshot_rpc_transport(monkeypatch, engine, store, digest)
    install_snapshot(engine, store, digest)
    directory = store.directory(digest)
    document = grid_document(digest, [{"method": "lsh"}, {"method": "som"}])
    grid_bytes = canonical_document(document)
    config_sha = hashlib.sha256(grid_bytes).hexdigest()
    grid_path = directory / "aos-grid.json"
    atomic_private(grid_path, grid_bytes)
    suite = json.loads((directory / "suite.json").read_bytes())
    suite["suite_id"] = "aos-cpu-test"
    suite_path = directory / "aos-suite.json"
    suite_bytes = canonical_document(suite)
    atomic_private(suite_path, suite_bytes)
    grant = AosCpuStudy(
        owner_id="aos-test-owner",
        origin="aos",
        snapshot_sha256=digest,
        provider_config_sha256=config_sha,
        max_experiments=2,
        max_wall_seconds=600,
        model_tokens=0,
    )
    entry = SuiteEntry(
        suite_id=suite["suite_id"],
        track="mode",
        program_version="mode-grid.v1",
        suite_manifest_path=str(suite_path.relative_to(runtime)),
        suite_manifest_sha256=hashlib.sha256(suite_bytes).hexdigest(),
        provider="mode-grid",
        scenario_path=str(grid_path.relative_to(runtime)),
        scenario_sha256=config_sha,
        provider_config_sha256=config_sha,
        proposal_limit=2,
        snapshot_sha256=digest,
        aos_cpu_study=grant,
        allowed_purposes=("research",),
    )
    registry = SuiteRegistry((entry,), runtime)
    registry_path = runtime / "registry.json"
    atomic_private(
        registry_path,
        canonical_document(
            {"schema": "lab-suite-registry.v1", "suites": [entry.model_dump(mode="json")]}
        ),
    )
    return SimpleNamespace(
        runtime=runtime,
        store=store,
        digest=digest,
        snapshot_sha256=digest,
        snapshot_sha=digest,
        manifest_path=suite_path,
        scenario_path=grid_path,
        engine=engine,
        entry=entry,
        grant=grant,
        registry=registry,
        registry_path=registry_path,
        grid_path=grid_path,
        suite_path=suite_path,
    )


def test_grant_verifies_real_synthetic_grid_without_local_llm(aos_cpu_installation, monkeypatch):
    import lab.director.local_llm as local_llm

    def forbidden(*args, **kwargs):
        pytest.fail("CPU grant must never load local-model configuration")

    monkeypatch.setattr(local_llm, "provider_profile_set_for_sha256", forbidden)
    f = aos_cpu_installation
    assert f.registry.verify_entry(f.entry) == (f.suite_path, f.grid_path)
    f.grant.authorize("aos", "aos-test-owner")
    f.grant.verify_budget(2, 600, 0)
    assert (
        f.grant.sha256
        == hashlib.sha256(canonical_document(f.grant.model_dump(mode="json"))).hexdigest()
    )


@pytest.mark.parametrize("origin,owner", [("local", "aos-test-owner"), ("aos", "other")])
def test_owner_origin_authorization_is_exact(aos_cpu_installation, origin, owner):
    with pytest.raises(ValueError, match="principal"):
        aos_cpu_installation.grant.authorize(origin, owner)


@pytest.mark.parametrize("budget", [(3, 600, 0), (2, 601, 0), (2, 600, 1), (True, 600, 0)])
def test_grant_budget_is_strict(aos_cpu_installation, budget):
    with pytest.raises(ValueError, match="CPU grant"):
        aos_cpu_installation.grant.verify_budget(*budget)


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "local-qwen"},
        {"provider": "mode-stream"},
        {"track": "anomaly"},
        {"program_version": "other"},
        {"allowed_purposes": ["baseline"]},
        {"proposal_limit": 3},
        {"snapshot_sha256": "f" * 64},
        {"provider_config_sha256": "f" * 64},
        {"proposal_contract": "operating-mode-config.v1"},
    ],
)
def test_non_cpu_or_mismatched_entries_reject(aos_cpu_installation, changes):
    payload = aos_cpu_installation.entry.model_dump(mode="json") | changes
    with pytest.raises(ValidationError):
        SuiteEntry.model_validate(payload, strict=True)


def test_absent_grant_preserves_legacy_serialization_and_hash():
    payload = dict(
        suite_id="legacy",
        track="anomaly",
        program_version="fixture",
        suite_manifest_path="suite.json",
        suite_manifest_sha256="a" * 64,
        provider="fake-json",
        scenario_path="scenario.json",
        scenario_sha256="b" * 64,
        proposal_limit=1,
    )
    entry = SuiteEntry(**payload)
    serialized = entry.model_dump(mode="json")
    assert "aos_cpu_study" not in serialized and "public_dev_study" not in serialized
    historical = dict(serialized)
    historical.pop("proposal_contract")
    historical.pop("snapshot_sha256")
    historical.pop("allowed_purposes")
    assert (
        SuiteRegistry.entry_sha256(entry)
        == hashlib.sha256(canonical_document(historical)).hexdigest()
    )


def test_grant_can_bound_prefix_of_whole_pinned_grid(aos_cpu_installation):
    f = aos_cpu_installation
    grant = f.grant.model_copy(update={"max_experiments": 1})
    entry = SuiteEntry.model_validate(
        f.entry.model_dump(mode="json")
        | {"aos_cpu_study": grant.model_dump(mode="json"), "proposal_limit": 1},
        strict=True,
    )
    assert SuiteRegistry((entry,), f.runtime).verify_entry(entry) == (f.suite_path, f.grid_path)


@pytest.mark.parametrize("source_kind", ["private_database", "public_dev", "unknown"])
def test_non_synthetic_snapshot_cannot_use_cpu_grant(aos_cpu_installation, source_kind):
    f = aos_cpu_installation
    path = f.store.directory(f.digest) / "manifest.json"
    manifest = json.loads(path.read_bytes()) | {"source_kind": source_kind}
    path.write_bytes(canonical_document(manifest))
    with pytest.raises(ValueError, match="synthetic snapshot"):
        f.registry.verify_entry(f.entry)


@pytest.mark.parametrize("mutation", ["missing-install", "matrix", "weights", "grid-snapshot"])
def test_install_suite_and_scenario_binding_are_rechecked(aos_cpu_installation, mutation):
    f = aos_cpu_installation
    if mutation == "missing-install":
        (f.store.directory(f.digest) / "installed.json").unlink()
        with pytest.raises(ValueError, match="installation receipt"):
            f.registry.verify_entry(f.entry)
        return
    entry = f.entry
    if mutation in {"matrix", "weights"}:
        suite = json.loads(f.suite_path.read_bytes())
        if mutation == "matrix":
            suite["tasks"][0]["train"][0][0] += 1.0
        else:
            suite["family_cap"] = 0.5
        raw = canonical_document(suite)
        f.suite_path.write_bytes(raw)
        entry = SuiteEntry.model_validate(
            entry.model_dump(mode="json")
            | {"suite_manifest_sha256": hashlib.sha256(raw).hexdigest()},
            strict=True,
        )
    else:
        grid = grid_document("f" * 64, [{"method": "lsh"}, {"method": "som"}])
        raw = canonical_document(grid)
        f.grid_path.write_bytes(raw)
        sha = hashlib.sha256(raw).hexdigest()
        grant = f.grant.model_dump(mode="json") | {"provider_config_sha256": sha}
        entry = SuiteEntry.model_validate(
            entry.model_dump(mode="json")
            | {"aos_cpu_study": grant, "scenario_sha256": sha, "provider_config_sha256": sha},
            strict=True,
        )
    with pytest.raises(ValueError, match="AOS (mode suite|grid differs)"):
        SuiteRegistry((entry,), f.runtime)


def test_public_grant_cannot_coexist_with_aos_cpu_grant(aos_cpu_installation):
    from lab.api.registry import PublicDevStudy

    f = aos_cpu_installation
    public = PublicDevStudy(
        owner_id=f.grant.owner_id,
        origin="local",
        snapshot_sha256=f.digest,
        binding_sha256="a" * 64,
        source_suite_manifest_sha256="b" * 64,
        original_task_sha256="c" * 64,
        profile_sha256="d" * 64,
        max_experiments=2,
        max_wall_seconds=600,
        model_tokens=0,
    )
    with pytest.raises(ValidationError, match="owner-bound synthetic"):
        SuiteEntry.model_validate(
            f.entry.model_dump(mode="json") | {"public_dev_study": public.model_dump(mode="json")},
            strict=True,
        )


def test_canonical_grant_hash_binds_unicode_owner_and_budget(aos_cpu_installation):
    f = aos_cpu_installation
    changed = AosCpuStudy.model_validate(
        f.grant.model_dump(mode="json") | {"owner_id": "aos-Türkçe", "max_wall_seconds": 599},
        strict=True,
    )
    assert changed.sha256 != f.grant.sha256
    assert (
        changed.sha256
        == hashlib.sha256(canonical_document(changed.model_dump(mode="json"))).hexdigest()
    )


def test_registry_never_reads_or_regenerates_evaluator_labels(aos_cpu_installation, monkeypatch):
    from pathlib import Path

    import lab.scorer.mode_snapshot as scorer_snapshot

    f = aos_cpu_installation
    original = Path.read_bytes

    def guarded_read(path):
        if path.name == "evaluator.json":
            pytest.fail("API registry must not read Scorer-only evaluator labels")
        return original(path)

    def forbidden_materialization(*args, **kwargs):
        pytest.fail("API registry must not invoke label reconstruction")

    monkeypatch.setattr(Path, "read_bytes", guarded_read)
    monkeypatch.setattr(scorer_snapshot, "materialize_synthetic", forbidden_materialization)
    assert f.registry.verify_entry(f.entry) == (f.suite_path, f.grid_path)
