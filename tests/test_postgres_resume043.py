"""Opt-in direct-RPC identity negatives; no host processes or services are launched."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_postgres_holdout030_recovery import _engines


@pytest.mark.live
@pytest.mark.parametrize(
    "field",
    [
        "payload_sha256",
        "worker_pid",
        "worker_start_ticks",
        "worker_boot_id",
        "worker_unit",
        "worker_invocation_id",
        "worker_cgroup",
    ],
)
def test_null_resume_claimant_cannot_pass_sql_shape_check(field):
    engines = _engines()
    try:
        identity = {
            "payload_sha256": "a" * 64,
            "worker_pid": 1234,
            "worker_start_ticks": 99,
            "worker_boot_id": str(uuid4()),
            "worker_unit": "invalid.service",
            "worker_invocation_id": "b" * 32,
            "worker_cgroup": "/invalid.service",
        }
        identity[field] = None
        with (
            engines[1].begin() as connection,
            pytest.raises(DBAPIError, match="resume claimant fields are missing or null"),
        ):
            connection.execute(
                text("SELECT lab.claim_resumed_director(:restart,:sha,:process)"),
                {"restart": uuid4(), "sha": "c" * 64, "process": json.dumps(identity)},
            )
    finally:
        for engine in engines:
            engine.dispose()


@pytest.mark.live
@pytest.mark.parametrize("role", ["migrator", "planner", "scorer"])
def test_other_roles_cannot_claim_a_director_restart(role):
    engines = _engines()
    try:
        index = {"migrator": 0, "planner": 2, "scorer": 3}[role]
        with engines[index].begin() as connection, pytest.raises(DBAPIError):
            connection.execute(
                text("SELECT lab.claim_resumed_director(:restart,:sha,:process)"),
                {"restart": uuid4(), "sha": "c" * 64, "process": "{}"},
            )
    finally:
        for engine in engines:
            engine.dispose()
