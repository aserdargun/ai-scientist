"""Live Director journal replay proof for the run-end holdout crash boundary."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from test_holdout_loop_recovery import _minimal_loop_state
from test_postgres_holdout030_recovery import _engines, _sha
from test_postgres_run_end_unavailable import (
    _counts,
    _registration,
    _seed_scored_candidate_without_intent,
)

from lab.director import loop as loop_module
from lab.director.budget import BudgetSnapshot, RunBudget
from lab.director.journal import DirectorRunLease
from lab.director.loop import (
    DirectorLoop,
    DirectorLoopResult,
    DirectorLoopState,
    HoldoutApprovedSnapshot,
    _budget_to_json,
)


@pytest.mark.live
def test_real_journal_reuses_unavailable_state_checkpoint_across_marker_crash(
    tmp_path: Path,
) -> None:
    """A crash after state append must replay the exact sequence before adding its marker."""
    migrator, director, _planner, _scorer = _engines()
    run_id = uuid4()
    request = {
        "track": "anomaly",
        "suite": "synthetic.journal.replay",
        "suite_manifest_sha256": "a" * 64,
        "proposal_limit": 3,
        "budget": {"experiments": 3, "wall_seconds": 120, "model_tokens": 0},
    }
    with migrator.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO lab.runs "
                "(run_id,origin,owner_id,idempotency_key,payload_sha256,request_json,state) "
                "VALUES (:run,'local','fixture:holdout030',:key,:payload,"
                "CAST(:request AS jsonb),'running')"
            ),
            {
                "run": run_id,
                "key": f"journal-replay-{run_id.hex}",
                "payload": hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest(),
                "request": json.dumps(request),
            },
        )

    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(mode=0o700)
    os.chmod(artifact_root, 0o700)
    prior_state = _minimal_loop_state(run_id, "synthetic-candidate").model_copy(
        update={
            "completed_proposals": 2,
            "next_ordinal": 3,
            "proposal_limit": 3,
            "budget": _budget_to_json(BudgetSnapshot(wall_seconds=45.0, elapsed_wall_seconds=15.0)),
            "holdout_approved_snapshot": HoldoutApprovedSnapshot(
                experiment_id="synthetic-approved-baseline",
                source_sha256="1" * 64,
                tree_sha256="2" * 40,
                source_blob_sha256="3" * 64,
                seed0_by_task={"task": 0.1},
                seed1_by_task={"task": 0.1},
                suite_seed_scores=(0.1, 0.1, 0.1),
                noise_sd=0.01,
                best_suite=0.1,
                periodic_keep_watermark=0,
            ),
        }
    )
    loop = object.__new__(DirectorLoop)
    loop.run_id = run_id
    loop.director_engine = director
    loop.artifact_root = artifact_root
    frozen_budget = _budget_to_json(BudgetSnapshot(wall_seconds=0.0, elapsed_wall_seconds=60.0))
    state = loop._run_end_unavailable_state(prior_state, frozen_budget)
    assert state.champion_experiment_id == "synthetic-approved-baseline"
    assert state.budget["wall_seconds"] == 0.0
    assert state.completed_proposals == 2 < state.proposal_limit

    # First process dies after writing the rollback state but before its marker.
    with DirectorRunLease(director, run_id) as lease:
        loop.lease = lease
        first = loop._persist_state(state, checkpoint_tag="run_end")
    assert first["sequence"] == 0

    # The resumed process must reuse that exact journal receipt, then write marker 1.
    with DirectorRunLease(director, run_id) as lease:
        loop.lease = lease
        replay = loop._persist_state(state, checkpoint_tag="run_end")
        assert replay == first
        marker = lease.append_checkpoint(
            sequence=1,
            key="holdout-state-application:run_end:1",
            phase="holdout_state_application",
            payload={
                "schema": "director-holdout-state-application.v1",
                "run_id": str(run_id),
                "application_sha256": "b" * 64,
                "state_sha256": replay["payload_sha256"],
            },
            artifact_root=artifact_root,
        )
        assert marker["sequence"] == 1

    # A later replay after the marker is also idempotent and leaves both sequences fixed.
    with DirectorRunLease(director, run_id) as lease:
        loop.lease = lease
        assert loop._persist_state(state, checkpoint_tag="run_end") == first
        assert (
            lease.read_checkpoint(
                key="holdout-state-application:run_end:1", artifact_root=artifact_root
            )["receipt"]
            == marker
        )


@pytest.mark.live
def test_run_startup_replays_frozen_unavailable_wrapper_without_new_work(
    tmp_path: Path, monkeypatch
) -> None:
    """Recover freeze→fence→rollback after a state-before-marker crash using real PG journal."""
    migrator, director, planner, scorer = _engines()
    try:
        fixture = _seed_scored_candidate_without_intent(
            migrator, director, planner, scorer, tmp_path
        )
        run_id = fixture["run_id"]
        candidate_id = str(fixture["experiment_id"])
        suite_id = str(fixture["suite_id"])
        artifact_root = tmp_path / "wrapper-artifacts"
        artifact_root.mkdir(mode=0o700)
        os.chmod(artifact_root, 0o700)

        prior_state = _minimal_loop_state(run_id, candidate_id).model_copy(
            update={
                "suite_id": suite_id,
                "suite_version": 3,
                "budget": _budget_to_json(
                    BudgetSnapshot(wall_seconds=20.0, elapsed_wall_seconds=20.0)
                ),
                "holdout_approved_snapshot": HoldoutApprovedSnapshot(
                    experiment_id="synthetic-prior-approved-champion",
                    source_sha256=_sha(b"approved source"),
                    tree_sha256="2" * 40,
                    source_blob_sha256=_sha(b"approved source blob"),
                    seed0_by_task={"task": 0.1},
                    seed1_by_task={"task": 0.1},
                    suite_seed_scores=(0.1, 0.1, 0.1),
                    noise_sd=0.01,
                    best_suite=0.1,
                    periodic_keep_watermark=0,
                ),
            }
        )
        with DirectorRunLease(director, run_id) as lease:
            prior_receipt = lease.append_checkpoint(
                sequence=0,
                key="director-state:0",
                phase="director_loop_state",
                payload=prior_state.model_dump(mode="json", by_alias=True),
                artifact_root=artifact_root,
            )
        terminal = DirectorLoopResult(
            run_id=run_id,
            status="budget_exhausted",
            completed_proposals=0,
            next_ordinal=1,
            champion_experiment_id=candidate_id,
            best_suite=0.1,
            checkpoint_sha256=prior_receipt["payload_sha256"],
        )

        def make_loop(lease: DirectorRunLease) -> DirectorLoop:
            loop = object.__new__(DirectorLoop)
            loop.run_id = run_id
            loop.director_engine = director
            loop.planner_engine = planner
            loop.lease = lease
            loop.runner = object()
            loop.tasks = ()
            loop.suite_manifest_sha256 = "a" * 64
            loop.provider = object()
            loop.budget = RunBudget(
                proposal_limit=1,
                wall_limit=120,
                token_limit=0,
                snapshot=BudgetSnapshot(wall_seconds=20.0, elapsed_wall_seconds=20.0),
            )
            loop.artifact_root = artifact_root
            loop.proposal_limit = 1
            loop.holdout_enabled = True
            loop._verify_execution_identity = lambda _calibration: None

            def load_real_state(_calibration):
                rollback = lease.read_checkpoint(
                    key="director-state:0:run_end", artifact_root=artifact_root
                )
                checkpoint = rollback or lease.read_checkpoint(
                    key="director-state:0", artifact_root=artifact_root
                )
                assert checkpoint is not None
                state = DirectorLoopState.model_validate_json(
                    json.dumps(checkpoint["payload"], sort_keys=True, separators=(",", ":")),
                    strict=True,
                ).verify_consistency()
                return state, checkpoint["receipt"]

            loop._load_or_initialize = load_real_state
            loop._recover_unresolved_holdout_budget = lambda: (_ for _ in ()).throw(
                AssertionError("frozen run-end action must stop before proposal budget work")
            )
            return loop

        # Simulate process loss after the freeze append but before the SQL fence.
        with DirectorRunLease(director, run_id) as lease:
            loop = make_loop(lease)
            loop.budget = RunBudget(
                proposal_limit=1,
                wall_limit=120,
                token_limit=0,
                snapshot=BudgetSnapshot(wall_seconds=120.0, elapsed_wall_seconds=0.0),
            )

            def crash_after_freeze(*_args, **_kwargs):
                raise RuntimeError("simulated freeze-only crash")

            with pytest.raises(RuntimeError, match="simulated freeze-only crash"):
                loop._fence_run_end_unavailable(
                    terminal,
                    reason="fresh_wall_budget_unavailable",
                    remaining_wall_seconds=0.25,
                    original_intent=None,
                    fence=crash_after_freeze,
                )

        counts_before = _counts(migrator, run_id, suite_id)
        assert counts_before[0:3] == (0, 0, 0)
        assert _registration(director, run_id) == {
            "intent_registered": False,
            "reservation_exists": False,
        }

        # Startup must recover from the freeze, not spend the older state budget.
        with DirectorRunLease(director, run_id) as lease:
            loop = make_loop(lease)
            original_append = lease.append_checkpoint

            def crash_before_marker(**kwargs):
                if kwargs.get("key") == "holdout-state-application:run_end:1":
                    raise RuntimeError("simulated crash before state marker")
                return original_append(**kwargs)

            lease.append_checkpoint = crash_before_marker
            monkeypatch.setattr(
                loop_module, "read_registered_calibration", lambda *_a, **_kw: object()
            )
            with pytest.raises(RuntimeError, match="simulated crash before state marker"):
                loop.run()

        state_key = "director-state:0:run_end"
        marker_key = "holdout-state-application:run_end:1"
        with DirectorRunLease(director, run_id) as lease:
            rollback = lease.read_checkpoint(key=state_key, artifact_root=artifact_root)
            assert rollback is not None
            original_rollback_receipt = rollback["receipt"]
            assert lease.read_checkpoint(key=marker_key, artifact_root=artifact_root) is None
        rollback_state = DirectorLoopState.model_validate_json(
            json.dumps(rollback["payload"], sort_keys=True, separators=(",", ":")),
            strict=True,
        ).verify_consistency()
        assert rollback_state.champion_experiment_id == "synthetic-prior-approved-champion"
        assert rollback_state.budget["wall_seconds"] == 120.0
        assert rollback_state.completed_proposals < rollback_state.proposal_limit

        with DirectorRunLease(director, run_id) as lease:
            loop = make_loop(lease)
            monkeypatch.setattr(
                loop_module, "read_registered_calibration", lambda *_a, **_kw: object()
            )
            replayed_result = loop.run()
            replayed_state = lease.read_checkpoint(key=state_key, artifact_root=artifact_root)
            marker = lease.read_checkpoint(key=marker_key, artifact_root=artifact_root)
            assert replayed_state is not None and marker is not None
            assert replayed_state["receipt"] == original_rollback_receipt
            assert marker["payload"]["state_sha256"] == original_rollback_receipt["payload_sha256"]
            replayed_marker_receipt = marker["receipt"]

        assert replayed_result.status == "budget_exhausted"
        assert replayed_result.champion_experiment_id == "synthetic-prior-approved-champion"
        terminal_counts = _counts(migrator, run_id, suite_id)
        assert terminal_counts == counts_before[:4] + (1, counts_before[5])
        assert _registration(director, run_id) == {
            "intent_registered": False,
            "reservation_exists": False,
        }

        with director.connect() as connection:
            checkpoint_count = connection.execute(
                text(
                    "SELECT count(*) FROM lab.run_events WHERE run_id=:run "
                    "AND event_type='director.checkpoint'"
                ),
                {"run": run_id},
            ).scalar_one()

        # A complete wrapper replay after the marker exists remains read-only.
        with DirectorRunLease(director, run_id) as lease:
            loop = make_loop(lease)
            repeated_result = loop.run()
            repeated_state = lease.read_checkpoint(key=state_key, artifact_root=artifact_root)
            repeated_marker = lease.read_checkpoint(key=marker_key, artifact_root=artifact_root)
            assert repeated_state is not None and repeated_marker is not None
            assert repeated_state["receipt"] == replayed_state["receipt"]
            assert repeated_marker["receipt"] == replayed_marker_receipt
        with director.connect() as connection:
            repeated_checkpoint_count = connection.execute(
                text(
                    "SELECT count(*) FROM lab.run_events WHERE run_id=:run "
                    "AND event_type='director.checkpoint'"
                ),
                {"run": run_id},
            ).scalar_one()
        assert repeated_result == replayed_result
        assert repeated_checkpoint_count == checkpoint_count
        assert _counts(migrator, run_id, suite_id) == terminal_counts
        assert _registration(director, run_id) == {
            "intent_registered": False,
            "reservation_exists": False,
        }
    finally:
        for engine in (migrator, director, planner, scorer):
            engine.dispose()
