"""Isolated fixture checks: no GPU, services or real model acceptance."""

import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

import lab.cli as cli
from lab.api import app as api_module
from lab.api.mode_experiments import (
    ModeAgentRequest,
    ModeSnapshotStore,
    SyntheticSnapshotRequest,
    canonical_document,
    register_agent,
)
from lab.api.registry import ApiPrincipal, SuiteEntry
from lab.db.schema import runs
from lab.scorer.mode_snapshot import install_snapshot
from tests.test_api_and_scorer import _engine
from tests.test_mode_experiments041 import _mock_snapshot_rpc_transport
from tests.test_mode_finalization0353 import closure  # noqa: F401 -- pytest fixture


@pytest.fixture
def installation(tmp_path, monkeypatch):
    runtime = tmp_path / "data/runtime"
    store = ModeSnapshotStore(runtime / "mode-snapshots")
    digest = store.create_synthetic(SyntheticSnapshotRequest(scenario="step"))["snapshot_sha256"]
    engine = _engine()
    _mock_snapshot_rpc_transport(monkeypatch, engine, store, digest)
    receipt = install_snapshot(engine, store, digest)
    scenario = runtime / "dummy.json"
    scenario.write_bytes(b"{}")
    scenario.chmod(0o600)
    old = SuiteEntry(
        suite_id="fixture.old",
        track="mode",
        program_version="fixture",
        suite_manifest_path=f"mode-snapshots/{digest}/suite.json",
        suite_manifest_sha256=receipt["suite_manifest_sha256"],
        provider="fake-json",
        scenario_path="dummy.json",
        scenario_sha256=hashlib.sha256(b"{}").hexdigest(),
        proposal_limit=1,
    )
    registry_path = runtime / "registry.json"
    registry_path.write_bytes(
        canonical_document(
            {"schema": "lab-suite-registry.v1", "suites": [old.model_dump(mode="json")]}
        )
    )
    registry_path.chmod(0o600)
    return store, digest, registry_path, runtime, engine, old


def test_agent_registration_is_hash_pinned_and_legacy_hash_unchanged(installation):
    store, digest, path, runtime, _engine_, old = installation
    request = ModeAgentRequest(idempotency_key="mode-agent-fixture-001", snapshot_sha256=digest)
    registry, entry = register_agent(store, request, path, runtime)
    assert entry.provider == "local-qwen"
    assert entry.proposal_contract == "operating-mode-config.v1"
    assert entry.snapshot_sha256 == digest
    assert registry.verify_snapshot(entry)
    assert register_agent(store, request, path, runtime)[1] == entry
    legacy = old.model_dump(mode="json")
    for key in ("allowed_purposes", "proposal_contract", "snapshot_sha256"):
        legacy.pop(key)
    assert registry.entry_sha256(old) == hashlib.sha256(canonical_document(legacy)).hexdigest()
    assert registry.get(old.suite_id) == old


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "fake-json"},
        {"provider": "mode-grid"},
        {"track": "anomaly"},
        {"snapshot_sha256": None},
        {"proposal_contract": "candidate-python.v1"},
    ],
)
def test_contract_snapshot_invariants(installation, changes):
    store, digest, path, runtime, _engine_, _old = installation
    _registry, entry = register_agent(
        store,
        ModeAgentRequest(
            idempotency_key="mode-agent-fixture-002",
            snapshot_sha256=digest,
        ),
        path,
        runtime,
    )
    with pytest.raises(ValidationError):
        SuiteEntry.model_validate(entry.model_dump(mode="json") | changes)


def test_single_snapshot_authority_requires_all_immutable_pins(installation, monkeypatch):
    store, digest, path, runtime, _engine_, _old = installation
    registry, entry = register_agent(
        store,
        ModeAgentRequest(
            idempotency_key="mode-agent-fixture-003",
            snapshot_sha256=digest,
        ),
        path,
        runtime,
    )
    monkeypatch.setattr(cli, "PROJECT_ROOT", runtime.parent.parent)
    monkeypatch.setenv("LAB_SUITE_REGISTRY_FILE", str(path))
    request = dict(
        suite=entry.suite_id,
        provider=entry.provider,
        track=entry.track,
        proposal_contract=entry.proposal_contract,
        snapshot_sha256=digest,
        provider_config_sha256=entry.provider_config_sha256,
        provider_registry_entry_sha256=registry.entry_sha256(entry),
        suite_manifest_sha256=entry.suite_manifest_sha256,
        study_kind="single_snapshot_study",
    )
    suite_file = runtime / entry.suite_manifest_path
    assert cli._registered_mode_agent_snapshot(request, suite_file) == digest
    for key in (
        "provider",
        "track",
        "proposal_contract",
        "snapshot_sha256",
        "provider_config_sha256",
        "provider_registry_entry_sha256",
        "suite_manifest_sha256",
    ):
        with pytest.raises(ValueError):
            cli._registered_mode_agent_snapshot(request | {key: "wrong"}, suite_file)


def test_api_admits_bounded_agent_to_same_queue_and_retains_idempotency(installation, monkeypatch):
    store, digest, path, runtime, _engine_, _old = installation
    monkeypatch.setattr(api_module, "PROJECT_ROOT", runtime.parent.parent)
    monkeypatch.setattr(
        api_module, "compute_harness_hash", lambda *_: SimpleNamespace(sha256="a" * 64)
    )
    monkeypatch.setenv("LAB_SUITE_REGISTRY_FILE", str(path))
    engine = _engine()
    app = api_module.create_app(
        director_engine=engine,
        principals=(ApiPrincipal(token="u" * 32, origin="local", owner_id="fixture"),),
    )
    headers = {"Authorization": "Bearer " + "u" * 32}
    payload = dict(
        idempotency_key="mode-agent-api-fixture",
        snapshot_sha256=digest,
        experiments=2,
        wall_seconds=3600,
        model_tokens=180000,
    )
    with TestClient(app) as client:
        first = client.post("/v1/mode-agent-experiments", headers=headers, json=payload)
        assert first.status_code == 202, first.text
        repeated = client.post("/v1/mode-agent-experiments", headers=headers, json=payload)
        assert repeated.status_code == 200
        assert repeated.json()["run_id"] == first.json()["run_id"]
        assert (
            client.post(
                "/v1/mode-agent-experiments", headers=headers, json=payload | {"model_tokens": 0}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/v1/mode-agent-experiments", headers=headers, json=payload | {"experiments": True}
            ).status_code
            == 422
        )
    with engine.connect() as connection:
        request = connection.execute(select(runs.c.request_json)).scalar_one()
    assert request["provider"] == "local-qwen"
    assert request["proposal_contract"] == "operating-mode-config.v1"
    assert request["snapshot_sha256"] == digest
    assert request["study_kind"] == "single_snapshot_study"
    assert request["budget"] == {"experiments": 2, "wall_seconds": 3600, "model_tokens": 180000}


def test_agent_closure_uses_existing_scorer_and_cannot_skip_anomaly_holdout(closure):  # noqa: F811
    kwargs, _clock, calls = closure
    request = dict(
        provider="local-qwen",
        track="mode",
        proposal_contract="operating-mode-config.v1",
        snapshot_sha256=kwargs["snapshot_sha256"],
        study_kind="single_snapshot_study",
    )
    result = cli._finalize_mode_study(object(), **(kwargs | {"request": request}))
    assert result["state"] == "finalized"
    calls.clear()
    assert (
        cli._finalize_mode_study(
            object(),
            **(
                kwargs
                | {
                    "request": request | {"track": "anomaly"},
                    "holdout_enabled": True,
                }
            ),
        )
        is None
    )
    assert calls == []


def test_scorer_report_marks_only_hash_bound_mode_agent_and_preserves_anomaly():
    from lab.scorer.service import _mode_study_report_fields

    assert _mode_study_report_fields({"provider": "local-qwen", "track": "anomaly"}) == {}
    request = dict(
        provider="local-qwen",
        track="mode",
        study_kind="single_snapshot_study",
        proposal_contract="operating-mode-config.v1",
        snapshot_sha256="a" * 64,
        provider_config_sha256="b" * 64,
        provider_registry_entry_sha256="c" * 64,
        suite_manifest_sha256="d" * 64,
    )
    fields = _mode_study_report_fields(request)
    assert fields["provider"] == "local-qwen"
    assert fields["benchmark_acceptance"] is False
    assert fields["snapshot_sha256"] == request["snapshot_sha256"]
    with pytest.raises(ValueError, match="immutable study pins"):
        _mode_study_report_fields(request | {"snapshot_sha256": None})
    with pytest.raises(ValueError, match="immutable study pins"):
        _mode_study_report_fields(request | {"track": "anomaly"})


def test_registry_refuses_conflicting_retry_without_overwriting(installation):
    from lab.api.mode_experiments import _register_entry

    store, digest, path, runtime, _engine_, _old = installation
    request = ModeAgentRequest(idempotency_key="mode-agent-conflict-1", snapshot_sha256=digest)
    _registry, entry = register_agent(store, request, path, runtime)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="different bytes"):
        _register_entry(entry.model_copy(update={"proposal_limit": 2}), path, runtime)
    assert path.read_bytes() == before


def test_registry_rejects_suite_rewrite_with_same_snapshot_authority(installation):
    from lab.api.registry import SuiteRegistry

    store, digest, path, runtime, _engine_, _old = installation
    _registry, entry = register_agent(
        store,
        ModeAgentRequest(
            idempotency_key="mode-agent-manifest-1",
            snapshot_sha256=digest,
        ),
        path,
        runtime,
    )
    suite = runtime / entry.suite_manifest_path
    payload = json.loads(suite.read_bytes())
    payload["suite_version"] += 1
    encoded = canonical_document(payload)
    suite.write_bytes(encoded)
    rewritten = entry.model_copy(
        update={"suite_manifest_sha256": hashlib.sha256(encoded).hexdigest()}
    )
    with pytest.raises(ValueError, match="Scorer-installed task"):
        SuiteRegistry((rewritten,), runtime)
