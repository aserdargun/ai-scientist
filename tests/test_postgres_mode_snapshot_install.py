"""Opt-in role-separated installation; synthetic JSON only, no service/model launch."""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_postgres_holdout030_recovery import _engines

from lab.api.mode_experiments import canonical_document
from lab.director.public_suite import task_semantics_sha256


def bundle():
    session = hashlib.sha256(uuid4().bytes).hexdigest()
    times = [f"2026-01-01T00:00:0{i}+00:00" for i in range(4)]
    semantics = task_semantics_sha256(
        task_family="EVT",
        sampling_s=1,
        evaluation_times=tuple(times),
        masked_samples=(False,) * 4,
        failure_windows=(),
    )
    profile = hashlib.sha256(
        canonical_document(
            dict(
                schema="mode-snapshot-profile.v1",
                snapshot_sha256=session,
                sliding_window=1,
                embargo_samples=1,
                semantics_sha256=semantics,
            )
        )
    ).hexdigest()
    return dict(
        schema="mode-snapshot-registration.v1",
        dataset_id="synthetic-operating-modes",
        split_id="train-window-embargo.v1",
        session_id=session,
        sample_count=4,
        sliding_window=1,
        embargo_seconds=1,
        profile_sha256=profile,
        task_family="EVT",
        sampling_s=1,
        labels=[False, True, False, False],
        evaluation_times=times,
        masked_samples=[False] * 4,
        failure_windows=[],
        semantics_sha256=semantics,
        visibility="dev",
    )


def invoke(engine, document, *, wrong_hash=False, raw=None):
    payload = raw if raw is not None else canonical_document(document).decode()
    digest = "f" * 64 if wrong_hash else hashlib.sha256(payload.encode()).hexdigest()
    with engine.begin() as connection:
        return connection.execute(
            text("SELECT scorer.install_mode_snapshot(:bundle,:sha)"),
            {"bundle": payload, "sha": digest},
        ).scalar_one()


def rows(engine, session):
    with engine.connect() as connection:
        return tuple(
            connection.execute(
                text(
                    f"SELECT to_jsonb(t) FROM scorer.{table} t "
                    "WHERE dataset_id='synthetic-operating-modes' "
                    "AND split_id='train-window-embargo.v1' AND session_id=:session "
                    "ORDER BY to_jsonb(t)::text"
                ),
                {"session": session},
            )
            .scalars()
            .all()
            for table in ("dataset_profiles", "dataset_labels", "dataset_task_semantics")
        )


def cleanup(engine, session):
    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM scorer.dataset_profiles WHERE "
                "dataset_id='synthetic-operating-modes' AND split_id='train-window-embargo.v1' "
                "AND session_id=:session"
            ),
            {"session": session},
        )


@pytest.mark.live
def test_scorer_snapshot_install_success_and_exact_retry():
    engines = _engines()
    document = bundle()
    try:
        receipt = invoke(engines[3], document)
        before = rows(engines[0], document["session_id"])
        assert tuple(map(len, before)) == (1, 4, 1)
        assert receipt["snapshot_sha256"] == document["session_id"]
        assert receipt["profile_sha256"] == document["profile_sha256"]
        assert invoke(engines[3], document) == receipt
        assert rows(engines[0], document["session_id"]) == before
    finally:
        cleanup(engines[0], document["session_id"])
        for engine in engines:
            engine.dispose()


@pytest.mark.live
@pytest.mark.parametrize("role", [1, 2])
def test_director_planner_cannot_execute_snapshot_rpc(role):
    engines = _engines()
    document = bundle()
    try:
        with pytest.raises(DBAPIError) as error:
            invoke(engines[role], document)
        assert error.value.orig.sqlstate == "42501"
        assert rows(engines[0], document["session_id"]) == ([], [], [])
    finally:
        for engine in engines:
            engine.dispose()


@pytest.mark.live
@pytest.mark.parametrize("role", [1, 2, 3])
@pytest.mark.parametrize("table", ["dataset_profiles", "dataset_labels", "dataset_task_semantics"])
def test_snapshot_rpc_adds_no_direct_table_write_grants(role, table):
    engines = _engines()
    try:
        with engines[role].begin() as connection, pytest.raises(DBAPIError) as error:
            connection.execute(
                text(f"INSERT INTO scorer.{table} SELECT * FROM scorer.{table} WHERE false")
            )
        assert error.value.orig.sqlstate == "42501"
    finally:
        for engine in engines:
            engine.dispose()


@pytest.mark.live
@pytest.mark.parametrize(
    "change",
    [
        {"dataset_id": "public-dataset"},
        {"split_id": "holdout"},
        {"session_id": "not-a-hash"},
        {"visibility": "holdout"},
        {"task_family": "PDM"},
        {"sampling_s": 2},
        {"sample_count": 1025},
        {"sliding_window": 0},
        {"labels": [0, 1, 0, 0]},
        {"masked_samples": [False]},
        {"evaluation_times": [1, 2, 3, 4]},
        {"failure_windows": [[0, 1]]},
        {"semantics_sha256": "e" * 64},
        {"profile_sha256": "e" * 64},
        {"extra": "field"},
        {"sample_count": None},
    ],
)
def test_invalid_snapshot_bundle_never_changes_existing_rows(change):
    engines = _engines()
    document = bundle()
    try:
        invoke(engines[3], document)
        before = rows(engines[0], document["session_id"])
        with pytest.raises(DBAPIError):
            invoke(engines[3], {**document, **change})
        assert rows(engines[0], document["session_id"]) == before
    finally:
        cleanup(engines[0], document["session_id"])
        for engine in engines:
            engine.dispose()


@pytest.mark.live
def test_snapshot_wrong_hash_noncanonical_and_stale_labels_leave_rows_unchanged():
    engines = _engines()
    document = bundle()
    try:
        invoke(engines[3], document)
        before = rows(engines[0], document["session_id"])
        for kwargs in ({"wrong_hash": True}, {"raw": json.dumps(document)}, {}):
            altered = (
                {**document, "labels": [False, False, True, False]} if not kwargs else document
            )
            with pytest.raises(DBAPIError):
                invoke(engines[3], altered, **kwargs)
            assert rows(engines[0], document["session_id"]) == before
    finally:
        cleanup(engines[0], document["session_id"])
        for engine in engines:
            engine.dispose()


@pytest.mark.live
@pytest.mark.parametrize("conflict", ["profile", "labels", "semantics"])
def test_existing_registration_conflicts_are_not_repaired_or_overwritten(conflict):
    engines = _engines()
    document = bundle()
    try:
        invoke(engines[3], document)
        statements = {
            "profile": "UPDATE scorer.dataset_profiles SET sample_count=5",
            "labels": "DELETE FROM scorer.dataset_labels",
            "semantics": "DELETE FROM scorer.dataset_task_semantics",
        }
        # Deliberately incomplete trusted fixture; the RPC must never fill or overwrite it.
        with engines[0].begin() as connection:
            connection.execute(
                text(
                    statements[conflict] + " WHERE dataset_id='synthetic-operating-modes' "
                    "AND split_id='train-window-embargo.v1' AND session_id=:session"
                ),
                {"session": document["session_id"]},
            )
        before = rows(engines[0], document["session_id"])
        with pytest.raises(DBAPIError, match="existing registration conflicts"):
            invoke(engines[3], document)
        assert rows(engines[0], document["session_id"]) == before
    finally:
        cleanup(engines[0], document["session_id"])
        for engine in engines:
            engine.dispose()
