"""CPU capability through the original principal resolver and real pinned registry."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event

from lab.api.app import create_app
from lab.api.registry import ApiPrincipal
from tests import test_aos_cpu_registry as shared

aos_cpu_installation = shared.aos_cpu_installation

AOS_TOKEN = "inert-aos-fixture-" + "a" * 32
LOCAL_TOKEN = "inert-local-fixture-" + "b" * 32
FOREIGN_TOKEN = "inert-other-aos-fixture-" + "c" * 32


@pytest.fixture
def capability_inputs(aos_cpu_installation, monkeypatch):
    monkeypatch.delenv("LAB_SUITE_REGISTRY_FILE", raising=False)
    fixture = aos_cpu_installation
    engine = create_engine("sqlite://")

    def reject_database_connection(*_args):
        raise AssertionError("CPU capability must not access any database")

    event.listen(engine, "connect", reject_database_connection)
    principals = (
        ApiPrincipal(token=AOS_TOKEN, origin="aos", owner_id=fixture.grant.owner_id),
        ApiPrincipal(token=LOCAL_TOKEN, origin="local", owner_id=fixture.grant.owner_id),
        ApiPrincipal(token=FOREIGN_TOKEN, origin="aos", owner_id="aos:other-owner"),
    )
    yield SimpleNamespace(fixture=fixture, engine=engine, principals=principals)
    engine.dispose()


def client(inputs, *, missing_registry=False):
    return TestClient(
        create_app(
            director_engine=inputs.engine,
            principals=inputs.principals,
            suite_registry=None if missing_registry else inputs.fixture.registry,
        )
    )


def get(inputs, api, *, token=AOS_TOKEN, suite=None, **kwargs):
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    headers.update(kwargs.pop("headers", {}))
    return api.get(
        "/v1/aos-cpu-capability/" + (suite or inputs.fixture.entry.suite_id),
        headers=headers,
        **kwargs,
    )


@pytest.mark.parametrize(
    "token, status", [(None, 401), ("unknown", 401), (LOCAL_TOKEN, 403), (FOREIGN_TOKEN, 404)]
)
def test_original_authentication_and_owner_scope(capability_inputs, token, status):
    with client(capability_inputs) as api:
        response = get(capability_inputs, api, token=token)
    assert response.status_code == status
    assert capability_inputs.fixture.grant.owner_id not in response.text


def test_exact_cpu_pins_and_false_authority_without_private_paths(capability_inputs):
    f = capability_inputs.fixture
    with client(capability_inputs) as api:
        response = get(capability_inputs, api, headers={"X-Owner-Id": "forged-owner"})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "schema": "scientist.lab-cpu-capability.v1",
        "owner_id": f.grant.owner_id,
        "origin": "aos",
        "suite_id": f.entry.suite_id,
        "track": "mode",
        "program_version": f.entry.program_version,
        "provider": "mode-grid",
        "source_kind": "synthetic",
        "suite_manifest_sha256": f.entry.suite_manifest_sha256,
        "suite_entry_sha256": f.registry.entry_sha256(f.entry),
        "provider_config_sha256": f.entry.provider_config_sha256,
        "snapshot_sha256": f.grant.snapshot_sha256,
        "aos_cpu_study_sha256": f.grant.sha256,
        "max_experiments": min(f.grant.max_experiments, f.entry.proposal_limit),
        "max_wall_seconds": f.grant.max_wall_seconds,
        "model_tokens": 0,
        "allowed_purpose": "research",
        "allocation_authority": False,
        "gpu_release_verified": False,
        "native_inference_authorized": False,
        "launch_authorized": False,
    }
    for secret in (AOS_TOKEN, LOCAL_TOKEN, FOREIGN_TOKEN, str(f.runtime), "sqlite://"):
        assert secret not in response.text


@pytest.mark.parametrize("missing", ["registry", "suite", "grant"])
def test_missing_authority_is_not_an_implicit_grant(capability_inputs, missing):
    f = capability_inputs.fixture
    if missing == "grant":
        f.registry.entries[f.entry.suite_id] = f.entry.model_copy(update={"aos_cpu_study": None})
    with client(capability_inputs, missing_registry=missing == "registry") as api:
        response = get(
            capability_inputs, api, suite="unknown-suite" if missing == "suite" else None
        )
    assert response.status_code == (503 if missing == "registry" else 404)


@pytest.mark.parametrize("artifact", ["suite", "grid", "snapshot", "installed", "source_kind"])
def test_live_artifact_drift_is_rejected_before_advertising(capability_inputs, artifact):
    f = capability_inputs.fixture
    directory = f.store.directory(f.grant.snapshot_sha256)
    if artifact == "source_kind":
        path = directory / "manifest.json"
        value = json.loads(path.read_bytes())
        value["source_kind"] = "private_database"
        path.write_text(json.dumps(value))
    else:
        path = {
            "suite": f.runtime / f.entry.suite_manifest_path,
            "grid": f.runtime / f.entry.scenario_path,
            "snapshot": directory / "snapshot.json",
            "installed": directory / "installed.json",
        }[artifact]
        path.write_bytes(path.read_bytes() + b"changed")
    with client(capability_inputs) as api:
        response = get(capability_inputs, api)
    assert response.status_code == 404
    assert str(path) not in response.text


def test_cpu_route_does_not_reclassify_the_local_model_capability(capability_inputs):
    f = capability_inputs.fixture
    with client(capability_inputs) as api:
        response = api.get(
            "/v1/aos-capability/" + f.entry.suite_id,
            headers={"Authorization": f"Bearer {AOS_TOKEN}"},
        )
    assert response.status_code == 422
