"""Compile migration SQL through SQLAlchemy without a PostgreSQL connection."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import text


def test_migration_sql_has_no_unintended_sqlalchemy_bind_parameters(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "lab/db/migrations/versions/0043_mode_stream.py"
    spec = importlib.util.spec_from_file_location("mode_stream043_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    statements = []
    monkeypatch.setattr(migration, "op", SimpleNamespace(execute=statements.append))
    migration.upgrade()
    assert statements
    for statement in statements:
        assert text(statement).compile().params == {}, "migration literals became bind parameters"
