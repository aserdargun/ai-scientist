"""One bounded, read-only PostgreSQL capture in an exactly owned short-lived process.

Credentials arrive only over the private parent pipe. This process never creates a
Lab run; its output is an unowned immutable input until the API commits access.
"""

from __future__ import annotations

import contextlib
import json
import math
import signal
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import Double, bindparam, cast, column, create_engine, select, table, text
from sqlalchemy.pool import NullPool


class CaptureCancelled(RuntimeError):
    """The original capture deadline or its caller ended."""


def capture(job: dict[str, Any], cancelled: threading.Event) -> str:
    """Stream one MVCC snapshot into bounded files; no labels or host model loads."""
    from lab.operating_modes.stream import create_database_input

    deadline = job["deadline"]
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ValueError("invalid capture deadline")

    def active() -> None:
        if cancelled.is_set() or time.monotonic() >= deadline:
            raise CaptureCancelled("capture ended")

    active()
    recipe, definition, relation = job["request"], job["definition"], job["relation"]
    engine = create_engine(
        job["dsn"],
        poolclass=NullPool,
        connect_args={
            "connect_timeout": max(1, min(3, math.ceil(deadline - time.monotonic()))),
            "application_name": "swapp-mode-stream-" + job["capture_id"],
        },
    )
    if engine.dialect.name != "postgresql":
        engine.dispose()
        raise ValueError("PostgreSQL source required")
    driver: list[Any] = [None]
    done = threading.Event()

    def cancel_connection() -> None:
        while not done.wait(0.025):
            if cancelled.is_set() or time.monotonic() >= deadline:
                cancelled.set()
                current = driver[0]
                if current is not None:
                    # Only this capture's connection is cancelled. The parent also
                    # bounds shutdown if libpq/server cancellation cannot complete.
                    with contextlib.suppress(Exception):
                        current.cancel_safe(timeout=1.0)
                    return

    watcher = threading.Thread(target=cancel_connection, name="capture-cancel", daemon=True)
    watcher.start()
    try:
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            driver[0] = connection.connection.driver_connection
            if not callable(getattr(driver[0], "cancel_safe", None)):
                raise ValueError(
                    "source capture requires the installed psycopg3 bounded cancel API"
                )
            active()
            with connection.begin():
                connection.execute(text("SET TRANSACTION READ ONLY"))
                # timestamptz preserves instants but libpq renders it in the
                # session timezone; request UTC without reinterpreting naive data.
                connection.execute(text("SET LOCAL TIME ZONE 'UTC'"))
                names = list(
                    dict.fromkeys(
                        [relation["timestamp_column"], *recipe["sensors"]]
                        + ([relation["entity_column"]] if relation["entity_column"] else [])
                    )
                )
                source = table(
                    relation["table"], *(column(name) for name in names), schema=relation["schema"]
                )
                selected_columns = dict(source.c.items())
                stamp = selected_columns[relation["timestamp_column"]]
                # Float8 limits wire values before Python sees them, including a
                # configured view accidentally exposing oversized text/numerics.
                query = (
                    select(
                        stamp, *(cast(selected_columns[name], Double) for name in recipe["sensors"])
                    )
                    .where(stamp >= bindparam("start"), stamp < bindparam("end"))
                    .order_by(stamp)
                    .limit(bindparam("row_limit"))
                )
                params: dict[str, Any] = {
                    "start": pd.Timestamp(recipe["start_utc"]).to_pydatetime(),
                    "end": pd.Timestamp(recipe["end_utc"]).to_pydatetime(),
                    "row_limit": recipe["row_limit"] + 1,
                }
                if relation["entity_column"]:
                    query = query.where(
                        selected_columns[relation["entity_column"]] == bindparam("entity")
                    )
                    params["entity"] = recipe["entity"]

                def bound_statement() -> None:
                    active()
                    milliseconds = max(
                        1, min(5000, math.floor((deadline - time.monotonic()) * 1000))
                    )
                    connection.execute(
                        text("SELECT set_config('statement_timeout', :timeout, true)"),
                        {"timeout": f"{milliseconds}ms"},
                    ).scalar_one()

                bound_statement()
                timestamp_type = connection.execute(
                    text(
                        "SELECT a.atttypid = 'pg_catalog.timestamptz'::regtype "
                        "FROM pg_catalog.pg_attribute a "
                        "JOIN pg_catalog.pg_class c ON c.oid=a.attrelid "
                        "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE n.nspname=:schema AND c.relname=:relation "
                        "AND a.attname=:stamp AND a.attnum>0 AND NOT a.attisdropped "
                        "AND c.relkind IN ('r','p','v','m')"
                    ),
                    {
                        "schema": relation["schema"],
                        "relation": relation["table"],
                        "stamp": relation["timestamp_column"],
                    },
                ).scalar_one_or_none()
                if timestamp_type is not True:
                    raise ValueError("source timestamp must be an actual timestamptz column")
                bound_statement()
                result = connection.execute(
                    query.execution_options(stream_results=True, max_row_buffer=64), params
                )

                def rows() -> Iterator[tuple[Any, tuple[float | None, ...]]]:
                    try:
                        while True:
                            bound_statement()
                            batch = result.fetchmany(64)
                            active()
                            if not batch:
                                break
                            for row in batch:
                                active()
                                yield row[0], tuple(row[1:])
                    finally:
                        result.close()

                manifest = create_database_input(
                    Path(job["input_root"]),
                    source_definition=definition,
                    request=recipe,
                    rows=rows(),
                    admission_check=active,
                )
                active()
                return str(manifest.sha256)
    finally:
        done.set()
        driver[0] = None
        engine.dispose()
        watcher.join(timeout=1.1)


def main() -> int:
    """Private finite worker protocol; exceptions never disclose a DSN or source row."""
    cancelled = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_args: cancelled.set())
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError("capture request too large")
        job = json.loads(raw)
        digest = capture(job, cancelled)
        print(json.dumps({"input_sha256": digest}, separators=(",", ":")))
        return 0
    except CaptureCancelled:
        print('{"error":"capture_cancelled"}')
        return 2
    except Exception:
        print('{"error":"source_capture_failed"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
