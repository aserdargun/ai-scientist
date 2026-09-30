"""Actual Director probe against private registered/unregistered synthetic SQL rows."""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_postgres_holdout030_recovery import _engines
from test_postgres_run_end_unavailable import _seed_current_profile_and_suite

from lab.director.holdout import holdout_suite_is_registered


@pytest.mark.live
@pytest.mark.parametrize("case", ["registered", "missing_version", "manifest_mismatch"])
def test_registration_probe_matches_real_registry_columns(case):
    engines = _engines()
    migrator, director, _, scorer = engines
    suite = f"synthetic.registration029.{uuid4().hex[:16]}"
    run_id = uuid4()
    try:
        if case == "missing_version":
            dev_sha = "a" * 64
        else:
            _, _, _, dev_sha = _seed_current_profile_and_suite(migrator, scorer, suite)
        request = {
            "suite": suite,
            "suite_manifest_sha256": "b" * 64 if case == "manifest_mismatch" else dev_sha,
        }
        payload = json.dumps(request, sort_keys=True, separators=(",", ":"))
        with migrator.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO lab.runs "
                    "(run_id,origin,owner_id,idempotency_key,payload_sha256,request_json,state) "
                    "VALUES (:run,'local','fixture:registration029',:key,:sha,"
                    "CAST(:request AS jsonb),'queued')"
                ),
                {
                    "run": run_id,
                    "key": str(run_id),
                    "sha": hashlib.sha256(payload.encode()).hexdigest(),
                    "request": payload,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO lab.baseline_calibrations "
                    "(run_id,suite_id,suite_version,calibration_sha256,blob_sha256,task_count) "
                    "VALUES (:run,:suite,3,:sha,:sha,1)"
                ),
                {"run": run_id, "suite": suite, "sha": "c" * 64},
            )
        assert holdout_suite_is_registered(director, run_id=run_id) is (case == "registered")
        assert holdout_suite_is_registered(director, run_id=uuid4()) is False
    finally:
        for engine in engines:
            engine.dispose()


@pytest.mark.live
@pytest.mark.parametrize("role", [0, 2, 3])
def test_registration_probe_retains_director_only_authority(role):
    engines = _engines()
    try:
        with pytest.raises(DBAPIError) as error, engines[role].connect() as connection:
            connection.execute(
                text("SELECT lab.holdout_suite_is_registered(:run)"), {"run": uuid4()}
            )
        assert error.value.orig.sqlstate == ("P0001" if role == 0 else "42501")
    finally:
        for engine in engines:
            engine.dispose()
