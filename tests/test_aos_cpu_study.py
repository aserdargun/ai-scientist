"""Verified CPU description remains label-free, read-only and ordered by the pinned grid."""

from __future__ import annotations

import builtins
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from lab.api.aos_cpu_capability import CpuSnapshotSummary, LabCpuCapability, LabCpuStudy
from lab.api.mode_experiments import canonical_document
from lab.api.registry import SuiteRegistry
from lab.director.parameter_grid import ParameterGridProvider
from tests import test_aos_cpu_capability as shared

aos_cpu_installation = shared.aos_cpu_installation
capability_inputs = shared.capability_inputs


def study(inputs, api, *, token=shared.AOS_TOKEN, suite=None):
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    return api.get("/v1/aos-cpu-study/" + (suite or inputs.fixture.entry.suite_id), headers=headers)


@pytest.mark.parametrize(
    "token,status",
    [(None, 401), ("unknown", 401), (shared.LOCAL_TOKEN, 403), (shared.FOREIGN_TOKEN, 404)],
)
def test_study_reuses_original_authentication_and_exact_owner(capability_inputs, token, status):
    with shared.client(capability_inputs) as api:
        response = study(capability_inputs, api, token=token)
    assert response.status_code == status
    assert capability_inputs.fixture.grant.owner_id not in response.text


@pytest.mark.parametrize("missing", ["registry", "suite", "grant"])
def test_description_does_not_create_missing_authority(capability_inputs, missing):
    f = capability_inputs.fixture
    if missing == "grant":
        f.registry.entries[f.entry.suite_id] = f.entry.model_copy(update={"aos_cpu_study": None})
    with shared.client(capability_inputs, missing_registry=missing == "registry") as api:
        response = study(capability_inputs, api, suite="unknown" if missing == "suite" else None)
    assert response.status_code == (503 if missing == "registry" else 404)


def test_registered_order_hashes_and_public_summary_without_database(capability_inputs):
    f = capability_inputs.fixture
    provider = ParameterGridProvider.load(
        f.grid_path,
        configuration_sha256=f.entry.provider_config_sha256,
        registry_entry_sha256=f.registry.entry_sha256(f.entry),
    )
    snapshot = f.store.load(f.digest)
    with shared.client(capability_inputs) as api:
        legacy_before = shared.get(capability_inputs, api)
        response = study(capability_inputs, api)
        legacy_after = shared.get(capability_inputs, api)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["schema"] == "scientist.lab-cpu-study.v1"
    assert result["capability"] == legacy_before.json()
    assert legacy_before.content == legacy_after.content
    assert (
        legacy_before.content
        == LabCpuCapability.model_validate(legacy_before.json(), strict=True)
        .model_dump_json(by_alias=True)
        .encode()
    )
    assert result["snapshot"] == {
        "source_kind": "synthetic",
        "snapshot_sha256": f.digest,
        "source_id": snapshot.selection.source_id,
        "source_version": snapshot.selection.source_version,
        "sensors": list(snapshot.sensors),
        "rows": len(snapshot.values),
        "train_rows": snapshot.train_rows,
        "evaluation_input_rows": len(snapshot.values) - snapshot.train_rows,
        "first_utc": snapshot.timestamps_utc[0],
        "last_utc": snapshot.timestamps_utc[-1],
        "entity_authority": False,
    }
    assert result["grid"]["selection"] == "ordered_registered_prefix"
    assert result["grid"]["request_semantics"] == "first_n_of_available_prefix"
    assert result["grid"]["registered_configuration_count"] == 2
    assert result["grid"]["available_prefix_count"] == 2
    for position, (item, entry) in enumerate(
        zip(result["grid"]["configurations"], provider.entries, strict=True), 1
    ):
        assert item == {"position": position, "method": entry["configuration"]["method"], **entry}
        assert (
            hashlib.sha256(canonical_document(item["configuration"])).hexdigest()
            == item["configuration_sha256"]
        )
    assert [v["method"] for v in result["grid"]["configurations"]] == ["lsh", "som"]
    for forbidden in (
        str(f.runtime),
        "values",
        "timestamps_utc",
        "event_labels",
        "mode_labels",
        "quality_labels",
        "entity_column",
        "suite_manifest_path",
        "scenario_path",
        "postgresql",
        "sqlite://",
        shared.AOS_TOKEN,
    ):
        assert forbidden not in response.text
    LabCpuStudy.model_validate(result, strict=True)


def test_grant_and_entry_limit_return_only_first_registered_configuration(capability_inputs):
    f = capability_inputs.fixture
    grant = f.grant.model_copy(update={"max_experiments": 1})
    entry = f.entry.model_copy(update={"aos_cpu_study": grant, "proposal_limit": 1})
    f.registry = SuiteRegistry((entry,), f.runtime)
    with shared.client(capability_inputs) as api:
        response = study(capability_inputs, api)
    assert response.status_code == 200, response.text
    grid = response.json()["grid"]
    assert grid["registered_configuration_count"] == 2
    assert grid["available_prefix_count"] == 1
    assert [v["method"] for v in grid["configurations"]] == ["lsh"]
    assert grid["configurations"][0]["position"] == 1
    assert response.json()["capability"]["max_experiments"] == 1


@pytest.mark.parametrize("artifact", ["suite", "grid", "snapshot", "installed", "source_kind"])
def test_study_rejects_live_artifact_drift(capability_inputs, artifact):
    f = capability_inputs.fixture
    directory = f.store.directory(f.digest)
    if artifact == "source_kind":
        path = directory / "manifest.json"
        value = json.loads(path.read_bytes())
        value["source_kind"] = "private_database"
        path.write_text(json.dumps(value))
    else:
        path = {
            "suite": f.suite_path,
            "grid": f.grid_path,
            "snapshot": directory / "snapshot.json",
            "installed": directory / "installed.json",
        }[artifact]
        path.write_bytes(path.read_bytes() + b"changed")
    with shared.client(capability_inputs) as api:
        response = study(capability_inputs, api)
    assert response.status_code == 404
    assert str(path) not in response.text


def test_no_evaluator_label_io_or_scorer_module_import(capability_inputs, monkeypatch):
    # Setup already installed the fixture; only the GET boundary is guarded here.
    original_open = Path.open
    original_import = builtins.__import__

    def guarded_open(path, *args, **kwargs):
        if path.name == "evaluator.json":
            pytest.fail("study endpoint read evaluator-only labels")
        return original_open(path, *args, **kwargs)

    def guarded_import(name, *args, **kwargs):
        if name.startswith("lab.scorer"):
            pytest.fail("study endpoint imported Scorer-only code")
        return original_import(name, *args, **kwargs)

    with shared.client(capability_inputs) as api:
        monkeypatch.setattr(Path, "open", guarded_open)
        monkeypatch.setattr(builtins, "__import__", guarded_import)
        response = study(capability_inputs, api)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "changes",
    [
        {"source_id": "synthetic./private/source"},
        {"source_version": "v1.seed0/../private"},
        {"sensors": ["/private/sensor.csv"]},
    ],
)
def test_public_metadata_rejects_pathlike_strings(capability_inputs, changes):
    with shared.client(capability_inputs) as api:
        response = study(capability_inputs, api)
    assert response.status_code == 200
    with pytest.raises(ValidationError):
        CpuSnapshotSummary.model_validate(response.json()["snapshot"] | changes, strict=True)
