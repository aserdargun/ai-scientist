"""Self service publication preserves authority and refuses unsafe activation."""

import hashlib
import json

import pytest
from sqlalchemy import create_engine

from ops import install_synthetic_research as installer
from ops import research_configuration as configuration
from ops import start_lab


def _private(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


def test_append_registry_preserves_existing_authority_and_exact_retry(monkeypatch, tmp_path):
    monkeypatch.setattr(installer, "RUNTIME", tmp_path)
    manifest = _private(tmp_path / "suite.json", {"fixture": True})
    entry = {
        "suite_id": "old",
        "track": "anomaly",
        "program_version": "v1",
        "suite_manifest_path": manifest.name,
        "suite_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "provider": "local-qwen",
        "provider_config_sha256": "a" * 64,
        "proposal_limit": 6,
    }
    current = _private(
        tmp_path / "registry.json", {"schema": "lab-suite-registry.v1", "suites": [entry]}
    )
    new = {**entry, "suite_id": "new"}
    incoming = _private(
        tmp_path / "incoming.json", {"schema": "lab-suite-registry.v1", "suites": [new]}
    )
    installer.append_registry(current, incoming)
    document = json.loads(current.read_text())
    assert [item["suite_id"] for item in document["suites"]] == ["old", "new"]
    assert document["suites"][0]["provider_config_sha256"] == entry["provider_config_sha256"]
    generation = (current.read_bytes(), current.stat().st_mtime_ns, current.stat().st_ino)
    installer.append_registry(current, incoming)
    assert generation == (current.read_bytes(), current.stat().st_mtime_ns, current.stat().st_ino)
    _private(
        incoming, {"schema": "lab-suite-registry.v1", "suites": [{**new, "proposal_limit": 1}]}
    )
    with pytest.raises(ValueError, match="cannot overwrite"):
        installer.append_registry(current, incoming)
    assert generation == (current.read_bytes(), current.stat().st_mtime_ns, current.stat().st_ino)


def test_live_services_deny_activation_before_database_or_publication(monkeypatch, tmp_path):
    monkeypatch.setattr(configuration, "CONFIG", tmp_path)
    pending = tmp_path / "research-worker.pending.env"
    pending.write_text("SWAPP_LAB_GPU_UNIT=fixed.service\n")
    pending.chmod(0o600)
    old = tmp_path / "lab-worker.env"
    old.write_text("existing\n")
    monkeypatch.setattr(configuration, "installed_configuration", lambda **_: {"fixture": True})
    monkeypatch.setattr(
        start_lab, "check_unit", lambda *_: {"ActiveState": "active", "MainPID": "123"}
    )
    monkeypatch.setattr(
        installer, "engine_for", lambda *_args, **_kwargs: pytest.fail("database reached")
    )
    with pytest.raises(ValueError, match="live activation unsupported"):
        configuration.activate_pending_configuration()
    assert old.read_text() == "existing\n"
    assert pending.is_file()
    assert not (tmp_path / "research.json").exists()


@pytest.mark.parametrize(
    "state,denied",
    [
        ("stopped", False),
        ("completed", False),
        ("running", False),
        ("stop_requested", False),
        ("queued", True),
    ],
)
def test_legacy_ownerless_run_cleanup_is_read_only(state, denied):
    engine = create_engine("sqlite://")
    with engine.connect() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS lab")
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS scorer")
        connection.exec_driver_sql("CREATE TABLE lab.runs (run_id TEXT, state TEXT)")
        connection.exec_driver_sql("CREATE TABLE lab.director_run_owners (run_id TEXT)")
        connection.exec_driver_sql("CREATE TABLE lab.director_owner_generations (run_id TEXT)")
        for table in (
            "lab.director_stop_closures",
            "lab.director_restart_requests",
            "lab.director_recoveries",
            "scorer.score_jobs",
            "scorer.holdout_reservations",
            "scorer.care_baseline_calibration_claims",
        ):
            connection.exec_driver_sql(f"CREATE TABLE {table} (state TEXT)")
        connection.exec_driver_sql("INSERT INTO lab.runs VALUES ('legacy', ?)", (state,))
        connection.exec_driver_sql("PRAGMA query_only=ON")
        if denied:
            with pytest.raises(ValueError, match="current nonterminal run"):
                configuration.assert_activation_ledger_quiescent(connection)
        else:
            configuration.assert_activation_ledger_quiescent(connection)
        assert connection.exec_driver_sql("SELECT run_id,state FROM lab.runs").all() == [
            ("legacy", state)
        ]
        if state == "running":
            connection.exec_driver_sql("PRAGMA query_only=OFF")
            connection.exec_driver_sql(
                "INSERT INTO lab.director_owner_generations VALUES ('legacy')"
            )
            connection.exec_driver_sql("PRAGMA query_only=ON")
            with pytest.raises(ValueError, match="current nonterminal run"):
                configuration.assert_activation_ledger_quiescent(connection)
        if state == "stopped":
            connection.exec_driver_sql("PRAGMA query_only=OFF")
            connection.exec_driver_sql("INSERT INTO scorer.score_jobs VALUES ('running')")
            connection.exec_driver_sql("PRAGMA query_only=ON")
            with pytest.raises(ValueError, match="Scorer inflight job"):
                configuration.assert_activation_ledger_quiescent(connection)
    engine.dispose()


def test_duplicate_deployment_environment_binding_is_rejected(tmp_path):
    path = tmp_path / "worker.env"
    path.write_text("SWAPP_LAB_GPU_UNIT=one.service\nSWAPP_LAB_GPU_UNIT=two.service\n")
    path.chmod(0o600)
    with pytest.raises(ValueError, match="syntax"):
        configuration.read_environment(path)
