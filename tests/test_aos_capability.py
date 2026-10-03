"""Read-only capability checks through the real API principal and registry wiring."""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event

from lab.api.app import create_app
from lab.api.registry import ApiPrincipal, SuiteEntry, SuiteRegistry

_AOS_TOKEN = "private-aos-token-" + "a" * 32
_LOCAL_TOKEN = "private-local-token-" + "b" * 32
_AOS_OWNER = "aos:configured-capability-owner"


@pytest.fixture
def capability_inputs(tmp_path, monkeypatch):
    monkeypatch.delenv("LAB_SUITE_REGISTRY_FILE", raising=False)
    manifest = tmp_path / "private-suite-manifest.json"
    manifest.write_bytes(b"{}")
    manifest.chmod(0o600)
    entry = SuiteEntry(
        suite_id="capability.fixture",
        track="anomaly",
        program_version="harness-fixture-v1",
        suite_manifest_path=manifest.name,
        suite_manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
        provider="local-qwen",
        provider_config_sha256="c" * 64,
        proposal_limit=3,
    )
    registry = SuiteRegistry((entry,), tmp_path)
    engine = create_engine("sqlite://")

    def reject_database_connection(*_args):
        raise AssertionError("capability must not access the database")

    event.listen(engine, "connect", reject_database_connection)
    principals = (
        ApiPrincipal(token=_LOCAL_TOKEN, origin="local", owner_id="local:fixture"),
        ApiPrincipal(token=_AOS_TOKEN, origin="aos", owner_id=_AOS_OWNER),
    )
    yield engine, principals, registry, entry, manifest
    engine.dispose()


def _client(inputs, *, registry=None, missing_registry=False):
    engine, principals, original_registry, _entry, _manifest = inputs
    return TestClient(
        create_app(
            director_engine=engine,
            principals=principals,
            suite_registry=None if missing_registry else (registry or original_registry),
        )
    )


@pytest.mark.parametrize("token", [None, "unknown-private-token"])
def test_capability_requires_existing_authenticated_principal(capability_inputs, token):
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    with _client(capability_inputs) as client:
        response = client.get("/v1/aos-capability/capability.fixture", headers=headers)
    assert response.status_code == 401


def test_capability_rejects_local_principal(capability_inputs):
    with _client(capability_inputs) as client:
        response = client.get(
            "/v1/aos-capability/capability.fixture",
            headers={"Authorization": f"Bearer {_LOCAL_TOKEN}"},
        )
    assert response.status_code == 403


def test_capability_returns_authenticated_owner_and_verified_pins_without_secrets(
    capability_inputs,
):
    _engine, _principals, registry, entry, manifest = capability_inputs
    with _client(capability_inputs) as client:
        response = client.get(
            "/v1/aos-capability/capability.fixture",
            headers={"Authorization": f"Bearer {_AOS_TOKEN}", "X-Owner-Id": "forged-owner"},
        )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "schema": "scientist.lab-capability.v1",
        "owner_id": _AOS_OWNER,
        "origin": "aos",
        "suite_id": entry.suite_id,
        "track": entry.track,
        "program_version": entry.program_version,
        "provider": "local-qwen",
        "suite_manifest_sha256": entry.suite_manifest_sha256,
        "suite_entry_sha256": registry.entry_sha256(entry),
        "provider_config_sha256": entry.provider_config_sha256,
        "proposal_limit": entry.proposal_limit,
        "proposal_contract": "candidate-python.v1",
        "allowed_purpose": "research",
        "gpu_release_verified": False,
    }
    for secret in (_AOS_TOKEN, _LOCAL_TOKEN, str(manifest), manifest.name, "sqlite://"):
        assert secret not in response.text


def test_capability_reverifies_manifest_after_registry_load(capability_inputs):
    *_rest, manifest = capability_inputs
    manifest.write_bytes(b'{"changed":true}')
    with _client(capability_inputs) as client:
        response = client.get(
            "/v1/aos-capability/capability.fixture",
            headers={"Authorization": f"Bearer {_AOS_TOKEN}"},
        )
    assert response.status_code == 404
    assert manifest.name not in response.text


def test_capability_rejects_nonlocal_registered_provider(capability_inputs):
    _engine, _principals, registry, entry, manifest = capability_inputs
    scenario = manifest.parent / "private-scenario.json"
    scenario.write_bytes(b"[]")
    scenario.chmod(0o600)
    fake_entry = SuiteEntry.model_validate(
        entry.model_dump()
        | {
            "provider": "fake-json",
            "provider_config_sha256": None,
            "scenario_path": scenario.name,
            "scenario_sha256": hashlib.sha256(scenario.read_bytes()).hexdigest(),
        }
    )
    fake_registry = SuiteRegistry((fake_entry,), registry.runtime_root)
    with _client(capability_inputs, registry=fake_registry) as client:
        response = client.get(
            "/v1/aos-capability/capability.fixture",
            headers={"Authorization": f"Bearer {_AOS_TOKEN}"},
        )
    assert response.status_code == 422


def test_capability_requires_trusted_registry(capability_inputs):
    with _client(capability_inputs, missing_registry=True) as client:
        response = client.get(
            "/v1/aos-capability/capability.fixture",
            headers={"Authorization": f"Bearer {_AOS_TOKEN}"},
        )
    assert response.status_code == 503


def test_capability_rejects_unknown_suite(capability_inputs):
    with _client(capability_inputs) as client:
        response = client.get(
            "/v1/aos-capability/unknown-suite",
            headers={"Authorization": f"Bearer {_AOS_TOKEN}"},
        )
    assert response.status_code == 404
