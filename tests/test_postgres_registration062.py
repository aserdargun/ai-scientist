"""Real PostgreSQL registration concurrency with production Scorer SELECT/INSERT ACLs."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pandas as pd
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import DBAPIError

from harness.contracts import FitContext
from lab.director.public_suite import ScorerTaskRegistration, install_scorer_task
from lab.scorer.holdout import HoldoutTaskSpec, _semantics_digest, register_holdout_suite


@pytest.mark.live
@pytest.mark.parametrize("conflict", ["none", "header", "task"])
def test_registration062_concurrent_scorer_callers(tmp_path, conflict):
    dsn_dir = os.environ.get("LAB_HOLDOUT_TEST_DSN_DIR")
    if not dsn_dir:
        pytest.skip("requires the disposable registration062 PostgreSQL instance")
    root = Path(dsn_dir).resolve(strict=True)
    engines = {
        role: create_engine((root / f"{role}.dsn").read_text().strip(), hide_parameters=True)
        for role in ("migrator", "scorer")
    }
    scorer = engines["scorer"]
    try:
        with scorer.connect() as connection:
            database, role = connection.execute(
                text("SELECT current_database(),session_user")
            ).one()
            assert database.startswith("swapp_lab_m0_registration_062_")
            assert role == "swapp_lab_scorer"
            acl = connection.execute(
                text(
                    "SELECT has_table_privilege(current_user,"
                    "'scorer.holdout_suite_versions','SELECT'),"
                    "has_table_privilege(current_user,'scorer.holdout_suite_versions','INSERT'),"
                    "has_table_privilege(current_user,'scorer.holdout_suite_versions','UPDATE')"
                )
            ).one()
            assert tuple(acl) == (True, True, False)
        # EXPLAIN performs no row lock or write, but checks the former query's ACL.
        with pytest.raises(DBAPIError) as error, scorer.connect() as connection:
            connection.execute(text("EXPLAIN SELECT * FROM scorer.holdout_suite_versions "
                                    "WHERE suite_id='missing' FOR UPDATE"))
        assert error.value.orig.sqlstate == "42501"
        dataset = "synthetic.registration062." + uuid4().hex
        install_scorer_task(
            engines["migrator"],
            ScorerTaskRegistration(
                dataset_id=dataset, split_id="holdout", session_id="one", sample_count=4,
                sliding_window=1, embargo_seconds=0, profile_sha256="a" * 64,
                task_family="EVT", sampling_s=1, labels=(False, False, True, True),
                evaluation_times=(0, 1, 2, 3), masked_samples=(False,) * 4,
                failure_windows=(), visibility="holdout",
                semantics_sha256=_semantics_digest(
                    family="EVT", sampling_s=1, times=(0, 1, 2, 3),
                    masks=(False,) * 4, failures=(),
                ),
            ),
        )
        task = HoldoutTaskSpec(
            task_id="one", dataset_id=dataset, split_id="holdout", session_id="one",
            profile_sha256="a" * 64, family="EVT", task_weight=1.0,
            base_score=0.2, reference_score=0.8, sliding_window=1,
            context=FitContext(seed=0, signals=("x",), regime_signals=(),
                               sampling_s=1, time_budget_s=10.0),
            train=pd.DataFrame({"x": [0.0, 1.0, 0.0, 1.0]}),
            evaluation=pd.DataFrame({"x": [0.0, 1.0, 2.0, 3.0]}),
        )
        suite = "synthetic.registration062." + uuid4().hex
        barrier = Barrier(2, timeout=10)

        def synchronize(_connection, _cursor, statement, _parameters, _context, _many):
            if "pg_advisory_xact_lock" in statement:
                barrier.wait()

        event.listen(scorer, "before_cursor_execute", synchronize)

        def register(index):
            try:
                return register_holdout_suite(
                    scorer, suite_id=suite, suite_version=1,
                    manifest_sha256=("c" if conflict == "header" and index else "b") * 64,
                    development_manifest_sha256="d" * 64, epsilon=0.0,
                    tasks=(replace(task, base_score=0.3)
                           if conflict == "task" and index else task,),
                    input_root=tmp_path / str(index),
                )
            except ValueError as exception:
                return exception

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(register, index) for index in range(2)]
            results = [future.result(timeout=20) for future in futures]
        event.remove(scorer, "before_cursor_execute", synchronize)
        successes = [result for result in results if isinstance(result, dict)]
        failures = [result for result in results if isinstance(result, ValueError)]
        assert len(successes) == (2 if conflict == "none" else 1)
        assert len(failures) == (0 if conflict == "none" else 1)
        if failures:
            assert "already bound" in str(failures[0])
        with scorer.connect() as connection:
            header = connection.execute(
                text("SELECT manifest_sha256,task_count FROM scorer.holdout_suite_versions "
                     "WHERE suite_id=:suite AND suite_version=1"), {"suite": suite}
            ).one()
            rows = connection.execute(
                text("SELECT base_score FROM scorer.holdout_suite_tasks "
                     "WHERE suite_id=:suite AND suite_version=1"), {"suite": suite}
            ).all()
            assert header.task_count == len(rows) == 1
            assert header.manifest_sha256 == successes[0]["manifest_sha256"]
            assert rows[0].base_score in ({0.2, 0.3} if conflict == "task" else {0.2})
            assert not connection.execute(
                text("SELECT has_table_privilege(current_user,"
                     "'scorer.holdout_suite_versions','UPDATE')")
            ).scalar_one()
    finally:
        for engine in engines.values():
            engine.dispose()
