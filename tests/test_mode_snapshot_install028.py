"""CPU verification of the execute-only synthetic installer boundary."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lab.api.mode_experiments import ModeSnapshotStore, SyntheticSnapshotRequest, canonical_document
from lab.scorer.mode_snapshot import install_synthetic_registration, materialize_synthetic


def _registration(tmp_path):
    store = ModeSnapshotStore(tmp_path)
    digest = store.create_synthetic(SyntheticSnapshotRequest(scenario="step"))["snapshot_sha256"]
    _, registration = materialize_synthetic(store, digest)
    return registration


def _receipt(registration, payload):
    return dict(
        schema="mode-snapshot-registration-receipt.v1",
        registration_sha256=hashlib.sha256(payload.encode()).hexdigest(),
        snapshot_sha256=registration.session_id,
        profile_sha256=registration.profile_sha256,
        semantics_sha256=registration.semantics_sha256,
        sample_count=registration.sample_count,
        sliding_window=registration.sliding_window,
    )


def test_install_uses_one_canonical_rpc_and_checks_persisted_receipt(tmp_path):
    registration = _registration(tmp_path)
    engine = MagicMock()
    engine.dialect.name = "postgresql"
    connection = engine.begin.return_value.__enter__.return_value

    def execute(statement, parameters):
        assert str(statement) == "SELECT scorer.install_mode_snapshot(:bundle,:sha256)"
        payload = parameters["bundle"]
        assert canonical_document(json.loads(payload)).decode() == payload
        assert hashlib.sha256(payload.encode()).hexdigest() == parameters["sha256"]
        return MagicMock(scalar_one=lambda: _receipt(registration, payload))

    connection.execute.side_effect = execute
    result = install_synthetic_registration(engine, registration)
    assert result["snapshot_sha256"] == registration.session_id
    assert connection.execute.call_count == 1


def test_wrong_rpc_receipt_rolls_back_and_never_marks_ready(tmp_path, monkeypatch):
    from lab.scorer import mode_snapshot

    store = ModeSnapshotStore(tmp_path)
    digest = store.create_synthetic(SyntheticSnapshotRequest(scenario="step"))["snapshot_sha256"]
    engine = MagicMock()
    engine.dialect.name = "postgresql"
    query = engine.begin.return_value.__enter__.return_value.execute.return_value
    query.scalar_one.return_value = {}
    write = MagicMock()
    monkeypatch.setattr(mode_snapshot, "write_suite_manifest", write)
    with pytest.raises(ValueError, match="receipt differs"):
        mode_snapshot.install_snapshot(engine, store, digest)
    write.assert_not_called()
    assert not (store.directory(digest) / "installed.json").exists()
    assert engine.begin.return_value.__exit__.call_args.args[0] is ValueError


def test_migration_grants_only_scorer_rpc_and_checks_conflicts_before_writes(monkeypatch):
    path = Path("lab/db/migrations/versions/0028_mode_snapshot_install.py")
    spec = importlib.util.spec_from_file_location("snapshot028", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements = []
    monkeypatch.setattr(module.op, "execute", statements.append)
    module.upgrade()
    from sqlalchemy import text

    assert all(not text(statement)._bindparams for statement in statements)
    sql = "\n".join(statements)
    assert "SECURITY DEFINER" in sql and "SET search_path=pg_catalog,scorer" in sql
    assert "session_user IS DISTINCT FROM 'swapp_lab_scorer'" in sql
    assert (
        "GRANT EXECUTE ON FUNCTION scorer.install_mode_snapshot(text,text) TO swapp_lab_scorer"
        in sql
    )
    assert "GRANT INSERT" not in sql and "UPDATE scorer." not in sql and "DELETE FROM" not in sql
    assert sql.index("existing registration conflicts") < sql.index(
        "INSERT INTO scorer.dataset_profiles"
    )
    assert "p_bundle IS NULL" in sql and "p_sha256 IS NULL" in sql
    assert "canonical IS DISTINCT FROM p_bundle" in sql


def test_non_postgres_installation_never_uses_generic_table_writes(tmp_path):
    registration = _registration(tmp_path)
    engine = MagicMock()
    engine.dialect.name = "sqlite"
    with pytest.raises(ValueError, match="PostgreSQL Scorer RPC"):
        install_synthetic_registration(engine, registration)
    engine.begin.assert_not_called()
