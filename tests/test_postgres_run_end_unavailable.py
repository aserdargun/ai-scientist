"""SQL regression probes for the owner-bound bitless run-end-unavailable fence.

All rows are disposable synthetic fixtures. The Director/Planner/Scorer flows
used to create a terminal baseline use their production role paths; the run,
calibration, profile/semantics, and checkpoint fixture data is explicitly seeded
by the migrator. Holdout suite versions/tasks are registered by the Scorer.
The setup creates no SQL run-end intent or reservation; the second parameter
case adds only an intent checkpoint event to represent the checkpoint-only
gap. One synthetic Scorer score receipt satisfies the ordinary scored-candidate
gate. No native worker, model, Docker, or real holdout scoring is performed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_postgres_holdout030_recovery import (
    SUITE_VERSION,
    _engines,
    _sha,
)

from lab.director.contracts import ExperimentDocument, TrajectoryDocument
from lab.director.holdout import (
    fence_run_end_unavailable,
    read_run_end_unavailable,
    reserve_holdout_check,
)
from lab.director.ledger import commit_experiment_record, register_experiment, transition_experiment
from lab.director.ownership import (
    ExecutionContract,
    OwnerProcessIdentity,
    assert_execution_owner_transaction,
    bind_execution_owner,
    canonical_execution_bytes,
    claim_initial_execution,
    reset_execution_owner,
)
from lab.director.task_plan import RunTaskAssignment, plan_run_tasks
from lab.scorer.jobs import _assert_scorer_job_execution, claim_score_job, enqueue_score_job


def _seed_current_profile_and_suite(migrator, scorer, suite_id: str) -> tuple[str, str, str, str]:
    """Seed synthetic profiles as migrator and register the suite as Scorer.

    Schema 0022 requires the Scorer session for suite registration; Scorer has
    only SELECT on dataset profiles/semantics. This ordinary synthetic suite
    intentionally does not claim a measured CARE calibration receipt.
    """
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
    with scorer.begin() as connection:
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
    with migrator.begin() as connection:
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
    with scorer.begin() as connection:
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


def _seed_scored_candidate_without_intent(
    migrator,
    director,
    planner,
    scorer,
    tmp_path,
    *,
    claim_owner: bool = False,
    wall_seconds: int = 120,
    model_tokens: int = 0,
    proposal_limit: int = 1,
    stop_before_scoring: bool = False,
):
    """Create a synthetic scored baseline while leaving run-end SQL admission absent."""
    run_id = uuid4()
    suite_id = f"synthetic.runend-unavailable.{uuid4().hex[:16]}"
    dataset, split, session, dev_sha = _seed_current_profile_and_suite(migrator, scorer, suite_id)
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
        "proposal_limit": proposal_limit,
        "budget": {
            "experiments": proposal_limit,
            "wall_seconds": wall_seconds,
            "model_tokens": model_tokens,
        },
    }
    request_sha = _sha(canonical_execution_bytes(request))
    with migrator.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO lab.runs "
                "(run_id,origin,owner_id,idempotency_key,payload_sha256,request_json,state) "
                "VALUES (:run,'local','fixture:runend-unavailable',:key,:payload,"
                "CAST(:request AS jsonb),:state)"
            ),
            {
                "run": run_id,
                "key": f"fixture-{run_id.hex}",
                "payload": request_sha,
                "request": json.dumps(request),
                "state": "queued" if claim_owner else "running",
            },
        )
    owner_token = None
    if claim_owner:
        stat = Path(f"/proc/{os.getpid()}/stat").read_text(encoding="utf-8")
        start_ticks = int(stat[stat.rfind(")") + 2 :].split()[19])
        worker_unit = f"swapp-ai-scientist-034-sql-fixture-{run_id.hex[:8]}.service"
        process = OwnerProcessIdentity(
            payload_sha256=request_sha,
            worker_pid=os.getpid(),
            worker_start_ticks=start_ticks,
            worker_boot_id=Path("/proc/sys/kernel/random/boot_id")
            .read_text(encoding="utf-8")
            .strip(),
            worker_unit=worker_unit,
            worker_invocation_id=uuid4().hex,
            worker_cgroup=f"/user.slice/fixture/{worker_unit}",
        )
        contract = ExecutionContract.build(
            run_id=run_id,
            request=request,
            request_sha256=request_sha,
            suite_id=suite_id,
            suite_version=SUITE_VERSION,
            suite_manifest_sha256=dev_sha,
            registry_entry_sha256=_sha(b"synthetic fixture registry entry"),
            harness_sha256=_sha(b"synthetic fixture harness"),
            image_sha256=_sha(b"synthetic fixture image"),
        )
        owner = claim_initial_execution(director, contract=contract, process=process)
        owner_token = bind_execution_owner(owner)
    try:
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
        if stop_before_scoring:
            return {
                "run_id": run_id,
                "suite_id": suite_id,
                "experiment_id": experiment_id,
                "candidate_sha": candidate_sha,
                "candidate_blob_sha": candidate_blob_sha,
                "inputs_sha": inputs_sha,
                "task_id": dataset,
                "split_id": split,
                "session_id": session,
                "job_id": job_id,
                "suite_manifest_sha": dev_sha,
                "owner": owner if claim_owner else None,
                "owner_context_token": owner_token,
            }
        invocation = uuid4().hex
        claim = claim_score_job(
            scorer,
            job_id=job_id,
            claim_unit=f"swapp-ai-scientist-scorer-{job_id.hex}.service",
            claim_invocation_id=invocation,
        )
        assert claim is not None
        with scorer.begin() as connection:
            asserted_run, asserted_generation, asserted_sha = _assert_scorer_job_execution(
                connection,
                job_id=job_id,
                claim_token=claim.claim_token,
                invocation_id=invocation,
            )
            assert (asserted_run, asserted_generation, asserted_sha) == (
                run_id,
                claim.admitted_generation,
                claim.execution_sha256,
            )
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
                "agent_version": "synthetic-runend-unavailable.v1",
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
                "suite_version": 3,
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
                "agent_version": "synthetic-runend-unavailable.v1",
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
        commit_experiment_record(
            director,
            experiment=experiment,
            trajectory=trajectory,
            experiment_blob_sha256=_sha(experiment.model_dump_json().encode()),
            trajectory_blob_sha256=_sha(trajectory.model_dump_json().encode()),
        )
        with migrator.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO lab.baseline_calibrations "
                    "(run_id,suite_id,suite_version,calibration_sha256,blob_sha256,task_count) "
                    "VALUES (:run,:suite,3,:calibration,:blob,1)"
                ),
                {
                    "run": run_id,
                    "suite": suite_id,
                    "calibration": calibration_sha,
                    "blob": calibration_sha,
                },
            )
        return {
            "run_id": run_id,
            "suite_id": suite_id,
            "experiment_id": experiment_id,
            "candidate_sha": candidate_sha,
            "candidate_blob_sha": candidate_blob_sha,
            "inputs_sha": inputs_sha,
            "calibration_sha": calibration_sha,
            "task_id": dataset,
            "suite_manifest_sha": dev_sha,
            "owner": owner if claim_owner else None,
            "owner_context_token": owner_token,
        }
    except BaseException:
        if owner_token is not None:
            reset_execution_owner(owner_token)
        raise


def _write_checkpoints(
    director,
    *,
    run_id: UUID,
    intent_checkpoint: bool,
) -> dict[str, object]:
    state_key = "director-state:synthetic-prior"
    state_sha = _sha(b"synthetic prior Director state")
    state_sequence = 10
    unavailable_sha = _sha(b"synthetic unavailable admission intent")
    unavailable_sequence = 12 if not intent_checkpoint else 13
    events = [
        (
            state_key,
            "director_loop_state",
            state_sequence,
            state_sha,
        ),
    ]
    intent_sha: str | None = None
    intent_sequence: int | None = None
    if intent_checkpoint:
        intent_sha = _sha(b"synthetic run-end intent without SQL registration")
        intent_sequence = 11
        events.append(
            (
                "director-holdout-run-end-intent",
                "holdout_run_end_intent",
                intent_sequence,
                intent_sha,
            )
        )
    events.append(
        (
            "director-holdout-run-end-unavailable-intent:1",
            "holdout_run_end_unavailable_intent",
            unavailable_sequence,
            unavailable_sha,
        )
    )
    with director.begin() as connection:
        assert_execution_owner_transaction(connection, run_id=run_id)
        for key, phase, sequence, digest in events:
            connection.execute(
                text(
                    "INSERT INTO lab.run_events(event_id,run_id,event_type,event_json) "
                    "VALUES (:id,:run,'director.checkpoint',CAST(:event AS jsonb))"
                ),
                {
                    "id": uuid4(),
                    "run": run_id,
                    "event": json.dumps(
                        {
                            "key": key,
                            "phase": phase,
                            "sequence": sequence,
                            "payload_sha256": digest,
                        }
                    ),
                },
            )
    return {
        "state_key": state_key,
        "state_sha": state_sha,
        "state_sequence": state_sequence,
        "budget_sha": _sha(b"synthetic canonical budget snapshot"),
        "checkpoint_sha": unavailable_sha,
        "checkpoint_sequence": unavailable_sequence,
        "intent_sha": intent_sha,
        "intent_sequence": intent_sequence,
    }


def _fence(director, run_id: UUID, candidate_id: str, reason: str, proof: dict[str, object]):
    return fence_run_end_unavailable(
        director,
        run_id=run_id,
        candidate_experiment_id=candidate_id,
        reason=reason,
        prior_state_key=proof["state_key"],
        prior_state_sha256=proof["state_sha"],
        prior_state_sequence=proof["state_sequence"],
        budget_snapshot_sha256=proof["budget_sha"],
        unavailable_checkpoint_sha256=proof["checkpoint_sha"],
        unavailable_checkpoint_sequence=proof["checkpoint_sequence"],
        remaining_wall_seconds=0.25 if reason == "fresh_wall_budget_unavailable" else 5.0,
        original_intent_checkpoint_sha256=proof["intent_sha"],
        original_intent_checkpoint_sequence=proof["intent_sequence"],
    )


def _fence_with_generation(
    director,
    *,
    run_id: UUID,
    candidate_id: str,
    reason: str,
    proof: dict[str, object],
    generation: int | None,
    invocation_id: str | None,
    execution_sha256: str | None,
) -> object:
    """Call the owner-bound SQL adapter with an explicit captured identity."""
    with director.begin() as connection:
        return connection.execute(
            text(
                "SELECT lab.fence_run_end_unavailable(:run,:candidate,:reason,:state_key,"
                ":state_sha,:state_seq,:budget_sha,:checkpoint_sha,:checkpoint_seq,"
                ":remaining_wall,:intent_sha,:intent_seq,:generation,:invocation,:execution_sha)"
            ),
            {
                "run": run_id,
                "candidate": candidate_id,
                "reason": reason,
                "state_key": proof["state_key"],
                "state_sha": proof["state_sha"],
                "state_seq": proof["state_sequence"],
                "budget_sha": proof["budget_sha"],
                "checkpoint_sha": proof["checkpoint_sha"],
                "checkpoint_seq": proof["checkpoint_sequence"],
                "remaining_wall": 0.25 if reason == "fresh_wall_budget_unavailable" else 5.0,
                "intent_sha": proof["intent_sha"],
                "intent_seq": proof["intent_sequence"],
                "generation": generation,
                "invocation": invocation_id,
                "execution_sha": execution_sha256,
            },
        ).scalar_one()


def _registration(director, run_id: UUID) -> dict[str, object]:
    with director.connect() as connection:
        return connection.execute(
            text("SELECT lab.read_run_end_admission_registration(:run)"), {"run": run_id}
        ).scalar_one()


def _counts(migrator, run_id: UUID, suite_id: str) -> tuple[int, int, int, int, int, int]:
    with migrator.connect() as connection:
        result = connection.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM lab.holdout_run_quotas WHERE run_id=:run),"
                "(SELECT count(*) FROM lab.holdout_reservations WHERE run_id=:run),"
                "(SELECT count(*) FROM scorer.holdout_results hr JOIN lab.holdout_reservations r "
                "ON r.reservation_id=hr.reservation_id WHERE r.run_id=:run),"
                "(SELECT count(*) FROM scorer.task_scores WHERE run_id=:run),"
                "(SELECT count(*) FROM lab.holdout_run_end_unavailable WHERE run_id=:run),"
                "(SELECT count(*) FROM lab.holdout_suite_quotas WHERE suite_id=:suite)"
            ),
            {"run": run_id, "suite": suite_id},
        ).one()
    return tuple(int(value) for value in result)


@pytest.mark.live
@pytest.mark.parametrize(
    ("reason", "intent_checkpoint"),
    [
        ("fresh_wall_budget_unavailable", False),
        ("intent_checkpoint_without_sql_registration", True),
    ],
)
def test_unavailable_fence_rpc_is_role_bound_idempotent_and_bitless(
    tmp_path, reason: str, intent_checkpoint: bool
) -> None:
    migrator, director, planner, scorer = _engines()
    owner_token = None
    try:
        fixture = _seed_scored_candidate_without_intent(
            migrator, director, planner, scorer, tmp_path, claim_owner=True
        )
        owner_token = fixture["owner_context_token"]
        owner = fixture["owner"]
        run_id = fixture["run_id"]
        candidate_id = str(fixture["experiment_id"])
        proof = _write_checkpoints(director, run_id=run_id, intent_checkpoint=intent_checkpoint)
        before = _counts(migrator, run_id, str(fixture["suite_id"]))
        assert before[0:3] == (0, 0, 0)
        assert before[4] == 0 and before[5] == 0
        assert _registration(director, run_id) == {
            "intent_registered": False,
            "reservation_exists": False,
        }

        receipt = _fence(director, run_id, candidate_id, reason, proof)
        retry = _fence(director, run_id, candidate_id, reason, proof)
        readback = read_run_end_unavailable(director, run_id=run_id)
        assert receipt == retry == readback
        assert receipt.state == "failed" and receipt.bit is None
        assert receipt.reason == reason
        assert receipt.candidate_experiment_id == candidate_id
        assert receipt.admitted_generation == owner.generation
        assert receipt.execution_sha256 == owner.execution_sha256
        assert _registration(director, run_id) == {
            "intent_registered": False,
            "reservation_exists": False,
        }
        with pytest.raises(Exception, match="identity changed"):
            conflicting = {**proof, "budget_sha": _sha(b"different synthetic budget")}
            _fence(director, run_id, candidate_id, reason, conflicting)

        # Production experiment registration must observe the durable fence.
        calibration_sha = _sha(b"synthetic baseline calibration")
        with pytest.raises(Exception, match="durably unavailable"):
            register_experiment(
                director,
                experiment_id=f"exp_{uuid4().hex}",
                run_id=str(run_id),
                sequence=1,
                experiment_number=1,
                kind="proposal",
                baseline_name=None,
                parent_experiment_id=None,
                candidate_sha256=_sha(b"synthetic post-fence proposal"),
                candidate_blob_sha256=_sha(b"synthetic proposal blob"),
                inputs_sha256=_sha(b"synthetic proposal inputs"),
                move_type="detector",
                system="S1",
                hypothesis="synthetic post-fence proposal attempt",
                predicted_delta=0.1,
                proposal={"calibration_sha256": calibration_sha},
            )

        # The public registration RPC must reject the durable unavailable fence
        # before considering another admission; Director has no direct INSERT grant.
        with pytest.raises(DBAPIError, match="durably unavailable") as intent_error:
            with director.begin() as connection:
                assert_execution_owner_transaction(connection, run_id=run_id)
                connection.execute(
                    text(
                        "SELECT lab.register_holdout_run_end_intent("
                        ":run,CAST(:intent AS jsonb),:generation,:invocation,:execution_sha)"
                    ),
                    {
                        "run": run_id,
                        "generation": owner.generation,
                        "invocation": owner.invocation_id,
                        "execution_sha": owner.execution_sha256,
                        "intent": json.dumps(
                            {
                                "schema": "director-holdout-run-end-intent.v1",
                                "run_id": str(run_id),
                                "admitted_generation": owner.generation,
                                "execution_sha256": owner.execution_sha256,
                                "terminal_status": "budget_exhausted",
                                "candidate_experiment_id": candidate_id,
                                "candidate_sha256": fixture["candidate_sha"],
                                "completed_proposals": 0,
                                "abandoned_checkpoints": [],
                                "proposal_limit": 1,
                                "state_checkpoint_key": proof["state_key"],
                                "state_checkpoint_sha256": proof["state_sha"],
                                "state_checkpoint_sequence": proof["state_sequence"],
                                "state_application_key": None,
                                "state_application_sha256": None,
                                "state_application_receipt_sha256": None,
                                "budget": {
                                    "wall_seconds": 0.0,
                                    "elapsed_wall_seconds": 0.0,
                                    "reserved_wall_seconds": 0.0,
                                    "model_tokens": 0,
                                    "reserved_model_tokens": 0,
                                },
                            }
                        ),
                    },
                )
        assert getattr(intent_error.value.orig, "sqlstate", None) == "P0001"

        # A run-end reserve attempt has no registered intent to authorize it;
        # it must not mint a quota row or reservation after the fence.
        with pytest.raises(
            DBAPIError, match="durable final champion|durably unavailable"
        ) as reservation_error:
            reserve_holdout_check(
                director,
                run_id=run_id,
                reservation_id=uuid4(),
                request_key=f"post-fence-{uuid4().hex}",
                candidate_experiment_id=candidate_id,
                trigger_kind="run_end",
                trigger_index=1,
            )
        assert getattr(reservation_error.value.orig, "sqlstate", None) == "P0001"
        after = _counts(migrator, run_id, str(fixture["suite_id"]))
        assert after == before[:4] + (1, before[5])
        assert after[3] == before[3]
    finally:
        if owner_token is not None:
            reset_execution_owner(owner_token)
        for engine in (migrator, director, planner, scorer):
            engine.dispose()


@pytest.mark.live
def test_unavailable_fence_rejects_prior_state_after_newer_state_is_durable(tmp_path) -> None:
    """A current owner cannot close from a superseded durable Director state."""
    migrator, director, planner, scorer = _engines()
    owner_token = None
    try:
        fixture = _seed_scored_candidate_without_intent(
            migrator, director, planner, scorer, tmp_path, claim_owner=True
        )
        owner_token = fixture["owner_context_token"]
        run_id = fixture["run_id"]
        candidate_id = str(fixture["experiment_id"])
        proof = _write_checkpoints(director, run_id=run_id, intent_checkpoint=False)
        # No unfinished proposal may independently cause this rejection:
        # the newer durable state alone invalidates the old state reference.
        latest_key = "director-state:synthetic-later"
        latest_sha = _sha(b"synthetic newer Director state")
        with director.begin() as connection:
            assert_execution_owner_transaction(connection, run_id=run_id)
            connection.execute(
                text(
                    "INSERT INTO lab.run_events(event_id,run_id,event_type,event_json) "
                    "VALUES (:id,:run,'director.checkpoint',CAST(:event AS jsonb))"
                ),
                {
                    "id": uuid4(),
                    "run": run_id,
                    "event": json.dumps(
                        {
                            "key": latest_key,
                            "phase": "director_loop_state",
                            "sequence": int(proof["checkpoint_sequence"]) + 1,
                            "payload_sha256": latest_sha,
                        }
                    ),
                },
            )
        before = _counts(migrator, run_id, str(fixture["suite_id"]))
        with pytest.raises(DBAPIError, match="prior state|latest state") as stale_error:
            _fence(director, run_id, candidate_id, "fresh_wall_budget_unavailable", proof)
        assert getattr(stale_error.value.orig, "sqlstate", None) == "P0001"
        assert read_run_end_unavailable(director, run_id=run_id) is None
        assert _counts(migrator, run_id, str(fixture["suite_id"])) == before
        with migrator.connect() as connection:
            later = connection.execute(
                text(
                    "SELECT event_json->>'payload_sha256' FROM lab.run_events "
                    "WHERE run_id=:run AND event_type='director.checkpoint' "
                    "AND event_json->>'key'=:key"
                ),
                {"run": run_id, "key": latest_key},
            ).scalar_one()
        assert later == latest_sha
    finally:
        if owner_token is not None:
            reset_execution_owner(owner_token)
        for engine in (migrator, director, planner, scorer):
            engine.dispose()
