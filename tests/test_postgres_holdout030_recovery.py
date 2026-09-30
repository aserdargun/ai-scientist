"""Role-separated SQL recovery proofs on explicitly synthetic ledger fixtures.

These tests exercise production admission/recovery RPCs with independent
Director and Scorer connections. The baseline ledger rows and score receipts
are intentionally synthetic SQL fixtures; they are not candidate/model runs,
host-process death proofs, or 10-KEEP acceptance evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text

from lab.director.contracts import ExperimentDocument, TrajectoryDocument
from lab.director.ledger import commit_experiment_record, register_experiment, transition_experiment
from lab.director.task_plan import RunTaskAssignment, plan_run_tasks
from lab.scorer.jobs import claim_score_job, enqueue_score_job

SUITE_VERSION = 3
TEST_DB_PREFIX = "swapp_lab_m0_holdout_030_"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _engines() -> tuple[object, object, object, object]:
    value = os.environ.get("LAB_HOLDOUT_TEST_DSN_DIR")
    if not value:
        pytest.skip("set LAB_HOLDOUT_TEST_DSN_DIR to the disposable holdout-030 database")
    root = Path(value).resolve(strict=True)
    assert root.is_dir() and not root.is_symlink()
    engines = tuple(
        create_engine((root / f"{role}.dsn").read_text().strip(), pool_pre_ping=True)
        for role in ("migrator", "director", "planner", "scorer")
    )
    for engine, role in zip(engines, ("migrator", "director", "planner", "scorer"), strict=True):
        with engine.connect() as connection:
            database, session_user = connection.execute(
                text("SELECT current_database(), session_user")
            ).one()
        assert database.startswith(TEST_DB_PREFIX)
        assert session_user == f"swapp_lab_{role}"
    return engines


def _seed_profile_and_suite(migrator, suite_id: str) -> tuple[str, str, str, str]:
    """Seed only immutable synthetic dataset/suite registration rows."""
    private_sha = _sha((suite_id + ":private").encode())
    dev_sha = _sha((suite_id + ":development").encode())
    dataset = f"synthetic-holdout-{uuid4().hex[:24]}"
    split = "synthetic-split-v1"
    session = "synthetic-session-1"
    profile_sha = _sha((dataset + ":profile").encode())
    semantics = {
        "schema": "public-task-semantics.v1",
        "task_family": "EVT",
        "sampling_s": 1,
        "evaluation_times": [0, 1],
        "masked_samples": [False, False],
        "failure_windows": [],
    }
    semantics_sha = _sha(
        json.dumps(semantics, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    )
    with migrator.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO scorer.holdout_suite_versions "
                "(suite_id,suite_version,manifest_sha256,development_manifest_sha256,"
                "task_count,epsilon) VALUES (:suite,:version,:private,:development,1,0.01)"
            ),
            {
                "suite": suite_id,
                "version": SUITE_VERSION,
                "private": private_sha,
                "development": dev_sha,
            },
        )
        connection.execute(
            text(
                "INSERT INTO scorer.dataset_profiles "
                "(dataset_id,split_id,session_id,sample_count,sliding_window,profile_sha256,"
                "task_family,visibility) VALUES (:dataset,:split,:session,2,1,:sha,'EVT','dev')"
            ),
            {"dataset": dataset, "split": split, "session": session, "sha": profile_sha},
        )
        holdout_dataset = f"{dataset}-private"
        holdout_profile_sha = _sha((holdout_dataset + ":profile").encode())
        connection.execute(
            text(
                "INSERT INTO scorer.dataset_profiles "
                "(dataset_id,split_id,session_id,sample_count,sliding_window,profile_sha256,"
                "task_family,visibility) VALUES (:dataset,:split,:session,2,1,:sha,'EVT','holdout')"
            ),
            {
                "dataset": holdout_dataset,
                "split": split,
                "session": session,
                "sha": holdout_profile_sha,
            },
        )
        connection.execute(
            text(
                "INSERT INTO scorer.dataset_task_semantics "
                "(dataset_id,split_id,session_id,task_family,sampling_s,evaluation_times_json,"
                "masked_samples_json,failure_windows_json,semantics_sha256) "
                "VALUES (:dataset,:split,:session,'EVT',1,CAST(:times AS jsonb),"
                "CAST(:masks AS jsonb),CAST(:windows AS jsonb),:sha)"
            ),
            {
                "dataset": holdout_dataset,
                "split": split,
                "session": session,
                "times": json.dumps(semantics["evaluation_times"]),
                "masks": json.dumps(semantics["masked_samples"]),
                "windows": json.dumps(semantics["failure_windows"]),
                "sha": semantics_sha,
            },
        )
        connection.execute(
            text(
                "INSERT INTO scorer.holdout_suite_tasks "
                "(suite_id,suite_version,task_key,dataset_id,split_id,session_id,profile_sha256,"
                "family,task_weight,base_score,reference_score,sliding_window,sampling_s,"
                "train_sha256,evaluation_sha256,semantics_sha256,context_json) "
                "VALUES (:suite,:version,:task,:dataset,:split,:session,:profile,'EVT',"
                "1.0,0.2,0.8,1,1,:train,:evaluation,:semantics,'{}'::jsonb)"
            ),
            {
                "suite": suite_id,
                "version": SUITE_VERSION,
                "task": uuid4().hex,
                "dataset": holdout_dataset,
                "split": split,
                "session": session,
                "profile": holdout_profile_sha,
                "train": _sha(b"synthetic train"),
                "evaluation": _sha(b"synthetic eval"),
                "semantics": semantics_sha,
            },
        )
    return dataset, split, session, dev_sha


def _seed_run_with_scored_baseline(migrator, director, planner, scorer, tmp_path):
    """Seed a synthetic run/intent ledger and finalize its baseline through role RPCs.

    Migrator setup creates the disposable run, immutable suite/profile rows,
    calibration, intent, and checkpoint receipts. Director and Planner use
    their production RPC paths; Scorer claims and inserts one clearly synthetic
    score receipt through the normal claim/score guards. No model or OS worker
    process runs.
    """
    run_id = uuid4()
    suite_id = f"synthetic.holdout030.{uuid4().hex[:20]}"
    dataset, split, session, dev_sha = _seed_profile_and_suite(migrator, suite_id)
    candidate = b"synthetic baseline source; no candidate execution"
    candidate_sha = _sha(candidate)
    candidate_blob_sha = _sha(b"synthetic candidate artifact reference")
    inputs_sha = _sha(b"synthetic baseline inputs")
    calibration_sha = _sha(b"synthetic baseline calibration")
    experiment_id = f"exp_{uuid4().hex}"
    request = {
        "track": "anomaly",
        "suite": suite_id,
        "suite_manifest_sha256": dev_sha,
        "proposal_limit": 1,
        "budget": {"experiments": 1, "wall_seconds": 120, "model_tokens": 0},
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
                "key": f"fixture-{run_id.hex}",
                "payload": _sha(json.dumps(request, sort_keys=True).encode()),
                "request": json.dumps(request),
            },
        )
    registration = register_experiment(
        director,
        experiment_id=experiment_id,
        run_id=str(run_id),
        sequence=0,
        experiment_number=None,
        kind="baseline",
        baseline_name="robust_z",
        parent_experiment_id=None,
        candidate_sha256=candidate_sha,
        candidate_blob_sha256=candidate_blob_sha,
        inputs_sha256=inputs_sha,
        move_type="detector",
        system="S1",
        hypothesis="synthetic SQL fixture baseline",
        predicted_delta=None,
        proposal={"schema": "baseline.registration.v1", "fixture_only": True},
    )
    assert registration["status"] == "proposed"
    transition_experiment(director, experiment_id=experiment_id, status="primary_running")
    plan_run_tasks(
        planner,
        run_id=run_id,
        assignments=(
            RunTaskAssignment(
                experiment_id=experiment_id,
                evaluation_kind="baseline",
                task_id=dataset,
                seed=0,
                candidate_sha256=candidate_sha,
                dataset_id=dataset,
                split_id=split,
                session_id=session,
            ),
        ),
    )
    candidate_output = (
        b'{"schema":"candidate-scores.v1","sample_indices":[0,1],"scores":[0.1,0.9]}'
    )
    job_id = enqueue_score_job(
        planner,
        run_id=run_id,
        experiment_id=experiment_id,
        evaluation_kind="baseline",
        task_id=dataset,
        seed=0,
        candidate_sha256=candidate_sha,
        candidate_output=candidate_output,
        artifact_root=tmp_path,
    )
    invocation = uuid4().hex
    claim = claim_score_job(
        scorer,
        job_id=job_id,
        claim_unit=f"swapp-ai-scientist-scorer-{job_id.hex}.service",
        claim_invocation_id=invocation,
    )
    assert claim is not None
    with scorer.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO scorer.task_scores "
                "(run_id,experiment_id,evaluation_kind,task_id,seed,score,guard_results,"
                "score_job_id,claim_token,worker_invocation_id) VALUES "
                "(:run,:experiment,'baseline',:task,0,CAST(:score AS jsonb),"
                "CAST(:guard_results AS jsonb),:job,:token,:invocation)"
            ),
            {
                "run": run_id,
                "experiment": experiment_id,
                "task": dataset,
                "score": json.dumps(
                    {
                        "candidate_sha256": candidate_sha,
                        "candidate_output_sha256": _sha(candidate_output),
                        "vus_pr": 0.5,
                        "vus_roc": 0.5,
                    }
                ),
                "guard_results": json.dumps({"fixture_only": True}),
                "job": job_id,
                "token": claim.claim_token,
                "invocation": invocation,
            },
        )
    experiment = ExperimentDocument.model_validate(
        {
            "schema": "experiment.v1",
            "experiment_id": experiment_id,
            "run_id": run_id,
            "ordinal": 1,
            "kind": "baseline",
            "experiment_number": None,
            "baseline_name": "robust_z",
            "calibration_sha256": None,
            "agent_version": "synthetic-holdout030-fixture.v1",
            "parent_experiment_id": None,
            "candidate_sha256": candidate_sha,
            "candidate_blob_sha256": candidate_blob_sha,
            "move_type": "detector",
            "system": "S1",
            "hypothesis": "synthetic SQL fixture baseline",
            "predicted_delta": None,
            "inputs_sha256": inputs_sha,
            "parent_tree": "e" * 40,
            "child_tree": "e" * 40,
            "harness_sha256": "c" * 64,
            "image_sha256": "d" * 64,
            "suite_id": suite_id,
            "suite_version": SUITE_VERSION,
            "per_task": (
                {
                    "task_id": dataset,
                    "task_family": "EVT",
                    "score_norm": None,
                    "vus_pr": 0.5,
                    "vus_roc": 0.5,
                    "task_score": 0.5,
                    "fa_per_day": None,
                    "duty_fraction": None,
                    "event_f1": None,
                    "fit_seconds": 0.0,
                    "score_seconds": 0.0,
                },
            ),
            "suite_score": None,
            "guards": {"fixture_only": "pass"},
            "decision": None,
            "status": "scored",
            "fit_seconds": 0.0,
            "score_seconds": 0.0,
            "llm_input_tokens": 0,
            "llm_output_tokens": 0,
            "wall_seconds": 0.0,
        },
        strict=True,
    )
    messages_sha = _sha(b"[]")
    trajectory = TrajectoryDocument.model_validate(
        {
            "schema": "trajectory.v1",
            "trajectory_id": f"trj_{uuid4().hex}",
            "run_id": run_id,
            "experiment_id": experiment_id,
            "kind": "baseline",
            "experiment_number": None,
            "baseline_name": "robust_z",
            "calibration_sha256": None,
            "agent_version": "synthetic-holdout030-fixture.v1",
            "model_id": "baseline/no-llm.synthetic-fixture",
            "usage_profile": "noncommercial_research",
            "source_provenance": (),
            "quantization": "none",
            "adapter": "none",
            "system": "S1",
            "thinking": False,
            "temperature": 0.0,
            "top_p": 1.0,
            "context_template": "synthetic.fixture.v1",
            "inputs_sha256": inputs_sha,
            "messages_blob_sha256": messages_sha,
            "tool_calls": 0,
            "outcome": None,
            "quality_tier": "bronze",
            "secrets_scrubbed": False,
            "people_scrubbed": False,
            "raw_values_scrubbed": False,
            "exclusions": ("synthetic SQL fixture; not model evidence",),
        },
        strict=True,
    )
    experiment_blob = _sha(experiment.model_dump_json().encode())
    trajectory_blob = _sha(trajectory.model_dump_json().encode())
    commit_experiment_record(
        director,
        experiment=experiment,
        trajectory=trajectory,
        experiment_blob_sha256=experiment_blob,
        trajectory_blob_sha256=trajectory_blob,
    )
    with migrator.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO lab.baseline_calibrations "
                "(run_id,suite_id,suite_version,calibration_sha256,blob_sha256,task_count) "
                "VALUES (:run,:suite,:version,:calibration,:blob,1)"
            ),
            {
                "run": run_id,
                "suite": suite_id,
                "version": SUITE_VERSION,
                "calibration": calibration_sha,
                "blob": calibration_sha,
            },
        )
        connection.execute(
            text(
                "INSERT INTO lab.holdout_run_end_intents "
                "(run_id,terminal_status,completed_proposals,proposal_limit,"
                "candidate_experiment_id,candidate_sha256,state_checkpoint_key,"
                "state_checkpoint_sha256,state_checkpoint_sequence,intent_checkpoint_sha256,"
                "intent_checkpoint_sequence,wall_seconds,elapsed_wall_seconds,reserved_wall_seconds,"
                "model_tokens,reserved_model_tokens) VALUES (:run,'proposal_limit_reached',1,1,"
                ":experiment,:candidate,'director-state:1',:state_sha,1,:intent_sha,2,120,1,0,0,0)"
            ),
            {
                "run": run_id,
                "experiment": experiment_id,
                "candidate": candidate_sha,
                "state_sha": _sha(b"synthetic state checkpoint"),
                "intent_sha": _sha(b"synthetic run-end intent"),
            },
        )
    return {
        "run_id": run_id,
        "suite_id": suite_id,
        "experiment_id": experiment_id,
        "candidate_sha": candidate_sha,
        "candidate_blob_sha": candidate_blob_sha,
        "manifest_sha": _sha((suite_id + ":private").encode()),
        "intent_sha": _sha(b"synthetic run-end intent"),
    }


def _reserve(director, record: dict[str, object], request_key: str, reservation_id: UUID):
    with director.begin() as connection:
        return connection.execute(
            text(
                "SELECT lab.reserve_holdout_check(:run,:reservation,:request,:candidate,"
                "'run_end',1)"
            ),
            {
                "run": record["run_id"],
                "reservation": reservation_id,
                "request": request_key,
                "candidate": record["experiment_id"],
            },
        ).scalar_one()


def _quota_snapshot(migrator, run_id: UUID, suite_id: str) -> tuple[int, int]:
    with migrator.connect() as connection:
        run_used = connection.execute(
            text("SELECT used FROM lab.holdout_run_quotas WHERE run_id=:run"), {"run": run_id}
        ).scalar_one()
        suite_used = connection.execute(
            text(
                "SELECT used FROM lab.holdout_suite_quotas "
                "WHERE suite_id=:suite AND suite_version=:version"
            ),
            {"suite": suite_id, "version": SUITE_VERSION},
        ).scalar_one()
    return int(run_used), int(suite_used)


@pytest.mark.live
def test_holdout_recovery_rpc_cas_and_fence_race(tmp_path) -> None:
    """Exercise Scorer CAS/recovery and Director fence/reservation run-lock races."""
    migrator, director, planner, scorer = _engines()
    try:
        reserved = _seed_run_with_scored_baseline(migrator, director, planner, scorer, tmp_path)
        run_id = reserved["run_id"]
        reservation_id = uuid4()
        first = _reserve(director, reserved, f"recovery-{reservation_id.hex}", reservation_id)
        assert first["state"] == "reserved"
        before = _quota_snapshot(migrator, run_id, str(reserved["suite_id"]))
        with scorer.begin() as connection:
            failed_once = connection.execute(
                text("SELECT lab.recover_unclaimed_holdout_failure(:reservation,:run)"),
                {"reservation": reservation_id, "run": run_id},
            ).scalar_one()
            failed_retry = connection.execute(
                text("SELECT lab.recover_unclaimed_holdout_failure(:reservation,:run)"),
                {"reservation": reservation_id, "run": run_id},
            ).scalar_one()
            listed = connection.execute(
                text("SELECT lab.list_holdout_recovery_targets(:run)"), {"run": run_id}
            ).scalar_one()
        assert failed_once == failed_retry
        assert failed_once["state"] == "failed" and failed_once["bit"] is None
        assert listed == []
        late_identity = {
            "reservation": reservation_id,
            "pid": 31337,
            "ticks": 123456,
            "boot": str(uuid4()),
            "unit": f"swapp-ai-scientist-scorer-{reservation_id.hex}.service",
            "invocation": uuid4().hex,
            "cgroup": (
                "/user.slice/swapp-ai-scientist-scorer.slice/"
                f"swapp-ai-scientist-scorer-{reservation_id.hex}.service"
            ),
        }
        with pytest.raises(Exception, match="not runnable"):
            with scorer.begin() as connection:
                connection.execute(
                    text(
                        "SELECT lab.claim_holdout_reservation(:reservation,:pid,:ticks,:boot,"
                        ":unit,:invocation,:cgroup)"
                    ),
                    {"reservation": reservation_id, **late_identity},
                )
        with director.connect() as connection:
            bit = connection.execute(
                text("SELECT lab.read_holdout_bit(:run,:reservation)"),
                {"run": run_id, "reservation": reservation_id},
            ).scalar_one()
        assert bit["state"] == "failed" and bit["bit"] is None
        assert _quota_snapshot(migrator, run_id, str(reserved["suite_id"])) == before
        with migrator.connect() as connection:
            result_rows = connection.execute(
                text("SELECT count(*) FROM scorer.holdout_results WHERE reservation_id=:id"),
                {"id": reservation_id},
            ).scalar_one()
        assert result_rows == 0

        running = _seed_run_with_scored_baseline(migrator, director, planner, scorer, tmp_path)
        running_id = running["run_id"]
        running_reservation = uuid4()
        _reserve(director, running, f"running-{running_reservation.hex}", running_reservation)
        identity = {
            "pid": 31338,
            "ticks": 234567,
            "boot": str(uuid4()),
            "unit": f"swapp-ai-scientist-scorer-{running_reservation.hex}.service",
            "invocation": uuid4().hex,
            "cgroup": (
                "/user.slice/swapp-ai-scientist-scorer.slice/"
                f"swapp-ai-scientist-scorer-{running_reservation.hex}.service"
            ),
        }
        with scorer.begin() as connection:
            claim = connection.execute(
                text(
                    "SELECT lab.claim_holdout_reservation(:reservation,:pid,:ticks,:boot,"
                    ":unit,:invocation,:cgroup)"
                ),
                {"reservation": running_reservation, "run": running_id, **identity},
            ).scalar_one()
            target = connection.execute(
                text("SELECT lab.read_holdout_recovery_target(:reservation)"),
                {"reservation": running_reservation},
            ).scalar_one()
        assert claim["worker_unit"] == identity["unit"]
        assert target["state"] == "running"
        before_running = _quota_snapshot(migrator, running_id, str(running["suite_id"]))
        with scorer.begin() as connection:
            admitted = connection.execute(
                text(
                    "SELECT lab.check_holdout_admission(:reservation,:run,:pid,:ticks,:boot,"
                    ":unit,:invocation,:cgroup)"
                ),
                {
                    "reservation": running_reservation,
                    "run": running_id,
                    **identity,
                },
            ).scalar_one()
            stale = connection.execute(
                text(
                    "SELECT lab.check_holdout_admission(:reservation,:run,:pid,:ticks,:boot,"
                    ":unit,:invocation,:cgroup)"
                ),
                {
                    "reservation": running_reservation,
                    "run": running_id,
                    **{**identity, "ticks": identity["ticks"] + 1},
                },
            ).scalar_one()
        assert admitted is True and stale is False
        with migrator.begin() as connection:
            connection.execute(
                text(
                    "UPDATE lab.runs SET state='stop_requested',stop_requested=true "
                    "WHERE run_id=:run"
                ),
                {"run": running_id},
            )
        with scorer.begin() as connection:
            stopped_admission = connection.execute(
                text(
                    "SELECT lab.check_holdout_admission(:reservation,:run,:pid,:ticks,:boot,"
                    ":unit,:invocation,:cgroup)"
                ),
                {"reservation": running_reservation, "run": running_id, **identity},
            ).scalar_one()
        assert stopped_admission is False
        with pytest.raises(Exception, match="stale"):
            with scorer.begin() as connection:
                connection.execute(
                    text(
                        "SELECT lab.recover_holdout_failure(:reservation,:run,:pid,:ticks,:boot,"
                        ":unit,:invocation,:cgroup)"
                    ),
                    {
                        "reservation": running_reservation,
                        "run": running_id,
                        **{**identity, "ticks": identity["ticks"] + 1},
                    },
                )
        with scorer.begin() as connection:
            recovered = connection.execute(
                text(
                    "SELECT lab.recover_holdout_failure(:reservation,:run,:pid,:ticks,:boot,"
                    ":unit,:invocation,:cgroup)"
                ),
                {"reservation": running_reservation, "run": running_id, **identity},
            ).scalar_one()
        assert recovered["state"] == "failed" and recovered["bit"] is None
        assert _quota_snapshot(migrator, running_id, str(running["suite_id"])) == before_running

        race = _seed_run_with_scored_baseline(migrator, director, planner, scorer, tmp_path)
        race_id = race["run_id"]
        reservation = uuid4()
        intent_sha = str(race["intent_sha"])
        budget_key = f"holdout-budget-reconciled:{reservation}"
        budget_sha = _sha(b"synthetic reconciled budget checkpoint")
        with migrator.begin() as connection:
            for sequence, key, phase, digest in (
                (
                    1,
                    f"holdout-budget-reserved:{reservation}",
                    "holdout_budget_reserved",
                    _sha(b"reserved"),
                ),
                (12, budget_key, "holdout_budget_reconciled", budget_sha),
            ):
                connection.execute(
                    text(
                        "INSERT INTO lab.run_events(event_id,run_id,event_type,event_json) "
                        "VALUES (:id,:run,'director.checkpoint',CAST(:json AS jsonb))"
                    ),
                    {
                        "id": uuid4(),
                        "run": race_id,
                        "json": json.dumps(
                            {
                                "key": key,
                                "phase": phase,
                                "sequence": sequence,
                                "payload_sha256": digest,
                            }
                        ),
                    },
                )

        barrier = Barrier(2)

        def reserve_race():
            barrier.wait(timeout=5)
            try:
                value = _reserve(director, race, f"fence-race-{reservation.hex}", reservation)
                return "reserved", value
            except Exception as error:  # expected if the fence commits first
                return "rejected", str(error)

        def fence_race():
            barrier.wait(timeout=5)
            try:
                with director.begin() as connection:
                    value = connection.execute(
                        text(
                            "SELECT lab.fence_missing_holdout_run_end(:run,:intent,:sequence,"
                            ":budget_key,:budget_sha)"
                        ),
                        {
                            "run": race_id,
                            "intent": intent_sha,
                            "sequence": 2,
                            "budget_key": budget_key,
                            "budget_sha": budget_sha,
                        },
                    ).scalar_one()
                return "fenced", value
            except Exception as error:  # expected if reservation commits first
                return "rejected", str(error)

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = [pool.submit(reserve_race), pool.submit(fence_race)]
            results = [future.result(timeout=20) for future in outcomes]
        winners = [name for name, _ in results if name in {"reserved", "fenced"}]
        assert len(winners) == 1, results
        with migrator.connect() as connection:
            final_reservations = connection.execute(
                text(
                    "SELECT count(*) FROM lab.holdout_reservations WHERE run_id=:run "
                    "AND trigger_kind='run_end' AND trigger_index=1"
                ),
                {"run": race_id},
            ).scalar_one()
            fence_count = connection.execute(
                text("SELECT count(*) FROM lab.holdout_run_end_fences WHERE run_id=:run"),
                {"run": race_id},
            ).scalar_one()
            quota = connection.execute(
                text("SELECT used FROM lab.holdout_run_quotas WHERE run_id=:run"),
                {"run": race_id},
            ).scalar_one_or_none()
        assert (final_reservations, fence_count) in {(1, 0), (0, 1)}
        assert quota == (1 if final_reservations else None)
        assert sum(1 for name, _ in results if name == "rejected") == 1
    finally:
        for engine in (migrator, director, planner, scorer):
            engine.dispose()
