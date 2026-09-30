"""Small synthetic admission/installer-policy tests, never public materialization or PG."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from lab.api import app as api_module
from lab.api.app import create_app
from lab.api.registry import SuiteEntry, SuiteRegistry
from lab.db.schema import metadata, runs

_SCRIPT = Path(__file__).parents[1] / "scripts/prepare_public_baseline050.py"
_SPEC = importlib.util.spec_from_file_location("public050_installer", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
installer = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(installer)


def entry(tmp_path, **updates):
    for name in ("suite.json", "scenario.json"):
        (tmp_path / name).write_bytes(b"{}")
        (tmp_path / name).chmod(0o600)
    values = dict(
        suite_id="public-four-source-evt-v1",
        track="anomaly",
        program_version="public-cpu-baseline.v3",
        suite_manifest_path="suite.json",
        suite_manifest_sha256=hashlib.sha256(b"{}").hexdigest(),
        provider="fake-json",
        scenario_path="scenario.json",
        scenario_sha256=hashlib.sha256(b"{}").hexdigest(),
        proposal_limit=1,
    )
    values.update(updates)
    return SuiteEntry(**values)


def test_default_registry_digest_preserves_existing_admitted_runs(tmp_path):
    configured = entry(tmp_path)
    legacy = configured.model_dump(mode="json")
    legacy.pop("allowed_purposes")
    expected = hashlib.sha256(
        json.dumps(
            legacy, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()
    assert SuiteRegistry.entry_sha256(configured) == expected
    restricted = entry(tmp_path, allowed_purposes=("baseline",))
    assert SuiteRegistry.entry_sha256(restricted) != expected
    assert set(configured.allowed_purposes) == {"baseline", "research"}


@pytest.mark.parametrize("value", [[], ["baseline", "baseline"], ["other"], [None], "baseline"])
def test_invalid_registry_purposes_rejected(tmp_path, value):
    with pytest.raises(ValueError):
        entry(tmp_path, allowed_purposes=value)


@pytest.mark.parametrize(
    "purposes,research_status,baseline_status",
    [(("baseline",), 422, 202), (("research",), 202, 422), (("research", "baseline"), 202, 202)],
)
def test_api_rejects_disallowed_purpose_before_any_durable_admission(
    tmp_path, monkeypatch, purposes, research_status, baseline_status
):
    raw = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    engine = raw.execution_options(schema_translate_map={"lab": None, "scorer": None})
    metadata.create_all(engine)
    monkeypatch.setattr(
        api_module, "compute_harness_hash", lambda root: SimpleNamespace(sha256="a" * 64)
    )
    registry = SuiteRegistry((entry(tmp_path, allowed_purposes=purposes),), tmp_path)
    app = create_app(director_engine=engine, director_token="t" * 32, suite_registry=registry)
    headers = {"Authorization": "Bearer " + "t" * 32}
    common = dict(suite="public-four-source-evt-v1", program_version="public-cpu-baseline.v3")
    with TestClient(app) as client:
        response = client.post(
            "/v1/runs",
            headers=headers,
            json={
                **common,
                "idempotency_key": "public050-research-001",
                "track": "anomaly",
                "budget": {"experiments": 1, "wall_seconds": 60, "model_tokens": 0},
            },
        )
        assert response.status_code == research_status
        with engine.connect() as connection:
            assert len(connection.execute(select(runs)).all()) == int(research_status == 202)
        response = client.post(
            "/v1/baselines",
            headers=headers,
            json={
                **common,
                "idempotency_key": "public050-baseline-001",
                "budget": {"experiments": 0, "wall_seconds": 60, "model_tokens": 0},
            },
        )
        assert response.status_code == baseline_status
        with engine.connect() as connection:
            records = connection.execute(select(runs.c.request_json)).scalars().all()
            assert len(records) == int(research_status == 202) + int(baseline_status == 202)
            if baseline_status == 202:
                baseline = next(row for row in records if row.get("purpose") == "baseline")
                assert baseline["budget"]["experiments"] == baseline["budget"]["model_tokens"] == 0
    engine.dispose()


def test_frozen_config_has_real_bounded_four_source_scope():
    config = installer.read_config(_SCRIPT.parents[1] / "ops/public-four-source-050.json")
    assert len(config["tasks"]) == 4 and config["suite_version"] == 3
    smd = next(task for task in config["tasks"] if task["dataset_id"] == "SMD")
    assert smd["independent_family"] == "SMD:server-telemetry:EVT"
    assert smd["train_rows"] == smd["evaluation_rows"] == 12000
    genesis = next(task for task in config["tasks"] if task["dataset_id"] == "TSB-AD-M-Genesis")
    assert genesis["license_id"] == "CC-BY-NC-SA-4.0"
    assert config["materialization"]["tsb_evaluation_rows"] is None
    assert config["limits"]["manifest_bytes"] <= installer.MAX_SUITE_MANIFEST_BYTES
    assert len(config["metadata_files"]) + len(config["raw_files"]) == 14
    assert sum(item["uncompressed_bytes"] for item in config["archive_members"]) < 40 * 1024**2


def test_changed_policy_is_rejected_before_materializer(tmp_path):
    config = installer.read_config(_SCRIPT.parents[1] / "ops/public-four-source-050.json")
    config["materialization"]["tsb_evaluation_rows"] = 5000
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="policy"):
        installer.read_config(path)


def test_installer_refuses_scorer_credential_without_connecting(tmp_path, monkeypatch):
    path = tmp_path / "role.dsn"
    path.write_text("postgresql+psycopg://swapp_lab_scorer:fixture@127.0.0.1/swapp_lab")
    path.chmod(0o600)
    connect = MagicMock(side_effect=AssertionError("must reject before DB connection"))
    monkeypatch.setattr(installer, "create_engine", connect)
    with pytest.raises(ValueError, match="role/database"):
        installer.connect_installer(path, role="swapp_lab_migrator", database="swapp_lab")
    connect.assert_not_called()


def test_actual_installer_role_is_independently_checked(tmp_path, monkeypatch):
    path = tmp_path / "role.dsn"
    path.write_text("postgresql+psycopg://swapp_lab_migrator:fixture@127.0.0.1/swapp_lab")
    path.chmod(0o600)
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value.execute.return_value.one.return_value = (
        "swapp_lab_scorer",
        "swapp_lab",
    )
    monkeypatch.setattr(installer, "create_engine", lambda *args, **kwargs: engine)
    with pytest.raises(ValueError, match="actual installer authority"):
        installer.connect_installer(path, role="swapp_lab_migrator", database="swapp_lab")
    engine.dispose.assert_called_once()


def test_missing_source_fails_before_materialization_or_install(tmp_path, monkeypatch):
    runtime = tmp_path / "data/runtime"
    runtime.mkdir(parents=True)
    config = installer.read_config(_SCRIPT.parents[1] / "ops/public-four-source-050.json")
    monkeypatch.setattr(
        installer.shutil, "disk_usage", lambda path: SimpleNamespace(free=100 * 1024**3)
    )
    materializer = MagicMock(side_effect=AssertionError("missing pinned inputs must reject first"))
    monkeypatch.setattr(installer, "materialize_tsb_development", materializer)
    with pytest.raises(ValueError, match="missing"):
        installer.prepare(root=tmp_path, config=config, destination=runtime / "public050")
    materializer.assert_not_called()
    assert not (runtime / "public050").exists()
