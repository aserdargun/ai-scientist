"""Alembic environment reads a protected DSN file and never logs credentials."""

from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

from lab.db.schema import metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

dsn_path = Path(os.environ.get("LAB_MIGRATOR_DSN_FILE", "data/runtime/postgres/migrator.dsn"))
dsn = dsn_path.read_text(encoding="utf-8").strip()
config.set_main_option("sqlalchemy.url", dsn.replace("%", "%%"))
target_metadata = metadata


def run_migrations_offline() -> None:
    """Run migrations without opening a database connection."""
    context.configure(
        url=dsn,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table_schema="lab",
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations over the configured protected connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    try:
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                include_schemas=True,
                version_table_schema="lab",
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
