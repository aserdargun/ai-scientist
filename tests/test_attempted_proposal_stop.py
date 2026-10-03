"""CPU-only attempted-stop envelope guards; native SQL proof is a separate gate."""

import json
from copy import deepcopy
from uuid import uuid4

import pytest

from lab.director.attempted_proposal_stop import stop_envelope
from lab.director.ownership import ExecutionOwner
from lab.director.recovery import RecoveryPending


def fixture():
    owner = ExecutionOwner(uuid4(), 2, "a" * 32, "b" * 64)
    proposal = {
        "schema": "director-proposal-checkpoint.v1",
        "run_id": str(owner.run_id),
        "experiment_id": "exp_" + "c" * 32,
        "candidate_sha256": "d" * 64,
    }
    job = {
        "job_id": str(uuid4()),
        "run_id": str(owner.run_id),
        "experiment_id": proposal["experiment_id"],
        "evaluation_kind": "primary",
        "task_id": "synthetic-task",
        "seed": 0,
        "candidate_sha256": "d" * 64,
        "artifact_sha256": "e" * 64,
        "admitted_generation": 2,
        "execution_sha256": owner.execution_sha256,
        "state": "running",
        "claim_invocation_id": "f" * 32,
        "claimed_by": "private-native-token",
        "attempt": 1,
    }
    return owner, proposal, job


def plan_for(owner, proposal, jobs):
    return [
        {
            "run_id": str(owner.run_id),
            "experiment_id": proposal["experiment_id"],
            "evaluation_kind": "primary",
            "task_id": job["task_id"],
            "seed": 0,
        }
        for job in jobs
    ]


def test_freezes_complete_native_rows_without_mutating_checkpoint_or_claims():
    owner, proposal, job = fixture()
    before = deepcopy((proposal, job))
    envelope = stop_envelope(
        proposal, [job], owner=owner, primary_plan=plan_for(owner, proposal, [job])
    )
    assert json.loads(envelope["proposal_text"]) == proposal
    assert json.loads(envelope["admitted_inventory_text"]) == [job]
    assert envelope["expected_owner"] == {
        "generation": 2,
        "invocation_id": owner.invocation_id,
        "execution_sha256": owner.execution_sha256,
    }
    envelope["admitted_inventory_text"] = "changed"
    assert (proposal, job) == before


@pytest.mark.parametrize(
    "change",
    [
        {"run_id": str(uuid4())},
        {"experiment_id": "exp_" + "9" * 32},
        {"evaluation_kind": "confirmation"},
        {"seed": 1},
        {"admitted_generation": 1},
        {"execution_sha256": "9" * 64},
        {"candidate_sha256": "9" * 64},
    ],
)
def test_rejects_unfinished_inventory_outside_exact_primary_owner(change):
    owner, proposal, job = fixture()
    job.update(change)
    with pytest.raises(RecoveryPending):
        stop_envelope(proposal, [job], owner=owner, primary_plan=plan_for(owner, proposal, [job]))


def test_requires_real_bounded_unique_admission_and_stable_order():
    owner, proposal, job = fixture()
    with pytest.raises(RecoveryPending):
        stop_envelope(proposal, [], owner=owner, primary_plan=plan_for(owner, proposal, [job]))
    with pytest.raises(RecoveryPending):
        stop_envelope(
            proposal, [job, job], owner=owner, primary_plan=plan_for(owner, proposal, [job])
        )
    another = {**job, "job_id": str(uuid4()), "task_id": "second-task"}
    assert stop_envelope(
        proposal,
        [another, job],
        owner=owner,
        primary_plan=plan_for(owner, proposal, [job, another]),
    ) == stop_envelope(
        proposal,
        [job, another],
        owner=owner,
        primary_plan=plan_for(owner, proposal, [job, another]),
    )


def test_retains_completed_historical_generation_as_immutable_evidence():
    owner, proposal, job = fixture()
    job.update(state="completed", admitted_generation=1, claimed_by=None)
    assert json.loads(
        stop_envelope(proposal, [job], owner=owner, primary_plan=plan_for(owner, proposal, [job]))[
            "admitted_inventory_text"
        ]
    ) == [job]


def deadline_target(owner):
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    return {
        "state": "stop_requested",
        "stop_requested": True,
        "mode": "active",
        "current_generation": owner.generation,
        "worker_invocation_id": owner.invocation_id,
        "execution_sha256": owner.execution_sha256,
        "deadline_at": now + timedelta(seconds=600),
        "first_stop_at": now - timedelta(seconds=30),
    }


def engine_for_target(target):
    from unittest.mock import MagicMock

    engine = MagicMock()
    query = engine.connect.return_value.__enter__.return_value.execute.return_value
    query.mappings.return_value.one.return_value = target
    return engine


def test_original_first_stop_deadline_caps_execution_and_caller_allowance():
    from datetime import timedelta

    from lab.director.attempted_proposal_stop import _current_deadline

    owner, _, _ = fixture()
    target = deadline_target(owner)
    engine = engine_for_target(target)
    expected = target["first_stop_at"] + timedelta(seconds=120)
    assert _current_deadline(engine, owner=owner, cleanup_deadline=None) == expected
    assert (
        _current_deadline(engine, owner=owner, cleanup_deadline=target["deadline_at"]) == expected
    )
    earlier = expected - timedelta(seconds=20)
    assert _current_deadline(engine, owner=owner, cleanup_deadline=earlier) == earlier
    engine.begin.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        {"current_generation": 3},
        {"worker_invocation_id": "9" * 32},
        {"execution_sha256": "9" * 64},
        {"mode": "reconciling"},
        {"state": "running", "stop_requested": False},
        {"first_stop_at": None},
    ],
)
def test_changed_owner_or_missing_stop_proof_never_authorizes_mutation(change):
    from lab.director.attempted_proposal_stop import _current_deadline

    owner, _, _ = fixture()
    target = deadline_target(owner)
    target.update(change)
    engine = engine_for_target(target)
    with pytest.raises(RecoveryPending):
        _current_deadline(engine, owner=owner, cleanup_deadline=None)
    engine.begin.assert_not_called()


def test_expired_first_stop_is_not_replaced_by_later_execution_deadline():
    from datetime import timedelta

    from lab.director.attempted_proposal_stop import _current_deadline

    owner, _, _ = fixture()
    target = deadline_target(owner)
    target["first_stop_at"] -= timedelta(seconds=120)
    engine = engine_for_target(target)
    with pytest.raises(RecoveryPending):
        _current_deadline(engine, owner=owner, cleanup_deadline=target["deadline_at"])
    engine.begin.assert_not_called()


@pytest.mark.parametrize("deadline", [None, "2026-09-30T12:00:00Z"])
def test_missing_or_untyped_execution_deadline_remains_pending(deadline):
    from lab.director.attempted_proposal_stop import _current_deadline

    owner, _, _ = fixture()
    target = deadline_target(owner)
    target["deadline_at"] = deadline
    engine = engine_for_target(target)
    with pytest.raises(RecoveryPending, match="original execution deadline unavailable"):
        _current_deadline(engine, owner=owner, cleanup_deadline=None)
    engine.begin.assert_not_called()


def retry_after_drained_fixture(monkeypatch, tmp_path):
    """CPU protocol fixture: W1 genuinely represented once; no replacement identity is supplied."""
    import hashlib
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from lab.api import registry as registry_module
    from lab.director import attempted_proposal_stop as module
    from lab.director.journal import canonical_bytes
    from lab.director.recovery import OwnerGeneration

    owner, proposal, job = fixture()
    proposal.update(experiment_number=1, calibration_sha256="c" * 64)
    target = deadline_target(owner)
    recovery_id = uuid4()
    process_owner = OwnerGeneration(
        "e" * 64,
        123,
        456,
        str(uuid4()),
        "swapp-ai-scientist-director-resume-test.service",
        owner.invocation_id,
        "/user.slice/fixture.service",
    )
    row = {
        "status": "primary_running",
        "experiment_id": proposal["experiment_id"],
        "experiment_number": 1,
        "calibration_sha256": "c" * 64,
    }
    execution = {"suite_id": "fixture", "registry_entry_sha256": "d" * 64}
    plan = plan_for(owner, proposal, [job]) + [
        {**plan_for(owner, proposal, [job])[0], "task_id": f"missing-{i}"} for i in range(3)
    ]
    state = {
        "plan": plan,
        "physical_error": None,
        "physical_calls": [],
        "writes": [],
        "marker": None,
        "drain_calls": [],
        "commit_calls": 0,
        "fail_commit_once": True,
    }
    original_invocation = "7" * 32
    terminal_text = json.dumps(
        {
            "jobs": [{**job, "state": "failed"}],
            "drains": [{"job_id": job["job_id"], "recovery_invocation": original_invocation}],
        }
    )
    director = MagicMock()
    connection = director.connect.return_value.__enter__.return_value
    director.begin.return_value.__enter__.return_value = connection

    def query(statement, params):
        sql = str(statement)
        result = MagicMock()
        if "SELECT r.state,r.stop_requested" in sql:
            result.mappings.return_value.one.return_value = deepcopy(target)
        elif "SELECT execution_json" in sql:
            result.scalar_one.return_value = execution
        elif "SELECT * FROM lab.experiments" in sql:
            result.mappings.return_value.all.return_value = [deepcopy(row)]
        elif "SELECT p.*,a.state AS closure_state" in sql:
            result.mappings.return_value.one_or_none.return_value = deepcopy(state["marker"])
            result.mappings.return_value.one.return_value = deepcopy(state["marker"])
        elif "SELECT to_jsonb(t)" in sql:
            result.scalars.return_value.all.return_value = deepcopy(state["plan"])
        elif "SELECT to_jsonb(j)" in sql:
            result.scalars.return_value.all.return_value = [deepcopy(job)]
        elif "SELECT lab.begin_stopped_proposal_closure" in sql:
            envelope_text = params["proposal"]
            envelope = json.loads(envelope_text)
            state["writes"].append(envelope["schema"])
            if envelope["schema"] == "attempted-stop-retirement.v2":
                roster = state["marker"]["recovery_roster_json"]
                actor = next(
                    a
                    for a in roster["attempts"]
                    if a["worker_invocation_id"]
                    == envelope["worker_identity"]["worker_invocation_id"]
                )
                roster["retirements"].append(
                    {
                        "retired_ordinal": actor["attempt_ordinal"],
                        "identity_sha256": actor["identity_sha256"],
                        "observation_sha256": "3" * 64,
                    }
                )
                result.scalar_one.return_value = 60.0
                return result
            if state["marker"] is None:
                inventory = envelope["admitted_inventory_text"]
                state["marker"] = {
                    "stop_mode": "primary_admitted",
                    "stop_protocol_version": 2,
                    "expected_generation": owner.generation,
                    "execution_sha256": owner.execution_sha256,
                    "closure_owner_json": __import__("dataclasses").asdict(process_owner),
                    "closure_created_at": target["first_stop_at"],
                    "original_primary_plan_text": envelope["original_primary_plan_text"],
                    "original_primary_plan_sha256": hashlib.sha256(
                        envelope["original_primary_plan_text"].encode()
                    ).hexdigest(),
                    "child_inventory_text": None,
                    "child_inventory_sha256": None,
                    "missing_primary_inventory_text": None,
                    "missing_primary_inventory_sha256": None,
                    "recovery_roster_json": {"attempts": [], "retirements": []},
                    "closure_state": "pending",
                    "proposal_sha256": hashlib.sha256(canonical_bytes(proposal)).hexdigest(),
                    "reservation_sha256": "8" * 64,
                    "reconciled_sha256": None,
                    "admitted_inventory_text": inventory,
                    "admitted_inventory_sha256": hashlib.sha256(inventory.encode()).hexdigest(),
                    "stop_envelope_sha256": hashlib.sha256(envelope_text.encode()).hexdigest(),
                    "terminal_inventory_text": None,
                    "terminal_inventory_sha256": None,
                    "recovery_invocation": None,
                    "created_at": target["first_stop_at"],
                }
            else:
                assert (
                    hashlib.sha256(envelope_text.encode()).hexdigest()
                    == state["marker"]["stop_envelope_sha256"]
                )
            result.scalar_one.return_value = 60.0
        elif "SELECT lab.close_stopped_unattempted_tasks" in sql:
            state["writes"].append("finalize")
            marker = state["marker"]
            missing = {
                "schema": "attempted-stop-missing.v2",
                "original_primary_plan_sha256": marker["original_primary_plan_sha256"],
                "outcomes": state["plan"][1:],
            }
            final = {
                "schema": "attempted-stop-terminal.v2",
                "original_primary_plan": json.loads(marker["original_primary_plan_text"]),
                "original_primary_plan_sha256": marker["original_primary_plan_sha256"],
                "child_inventory_sha256": marker["child_inventory_sha256"],
                "missing_primary_inventory": missing,
                "terminal_inventory": json.loads(terminal_text),
                "recovery_roster_json": deepcopy(marker["recovery_roster_json"]),
            }
            marker.update(
                closure_state="drained",
                terminal_inventory_text=json.dumps(final),
                terminal_inventory_sha256=hashlib.sha256(json.dumps(final).encode()).hexdigest(),
                missing_primary_inventory_text=json.dumps(missing),
                missing_primary_inventory_sha256=hashlib.sha256(
                    json.dumps(missing).encode()
                ).hexdigest(),
            )

        else:
            raise AssertionError("unexpected fixture query: " + sql)
        return result

    connection.execute.side_effect = query
    planner = MagicMock()
    planner.connect.return_value.__enter__.return_value = connection
    planner.begin.return_value.__enter__.return_value = connection
    monkeypatch.setattr(module, "read_registered_calibration", lambda *a, **kw: object())
    monkeypatch.setattr(module, "calibration_sha256", lambda *a: "c" * 64)
    monkeypatch.setattr(module, "_stored_turn", lambda **kw: (object(), proposal))
    deadproof = MagicMock(return_value=True)
    terminal_proof = MagicMock()
    monkeypatch.setattr(module, "prove_stopped_owner_dead", deadproof)
    monkeypatch.setattr(module, "_prove_terminal_jobs", terminal_proof)
    monkeypatch.setattr(module, "_drain_director_sandbox", MagicMock())

    def drain(*args, **kwargs):
        # A normal second launch would have a genuinely NEW invocation, never old W1.
        invocation = original_invocation if not state["drain_calls"] else "9" * 32
        state["drain_calls"].append(invocation)
        if state["marker"]["closure_state"] == "drained":
            raise AssertionError("drained receipt was relaunched under a different invocation")
        prior_roster = deepcopy(state["marker"]["recovery_roster_json"])
        if prior_roster["attempts"]:
            assert len(prior_roster["retirements"]) == len(prior_roster["attempts"])
        state["marker"].update(
            recovery_invocation=invocation,
            child_inventory_text=terminal_text,
            child_inventory_sha256=hashlib.sha256(terminal_text.encode()).hexdigest(),
            recovery_roster_json={
                "attempts": [
                    {
                        "attempt_ordinal": len(prior_roster["attempts"]) + 1,
                        "worker_pid": 4242,
                        "worker_start_ticks": 100,
                        "worker_boot_id": str(uuid4()),
                        "worker_unit": "swapp-ai-scientist-scorer-" + "6" * 32 + ".service",
                        "worker_invocation_id": invocation,
                        "worker_cgroup": "/fixture/scorer.service",
                        "expected_generation": owner.generation,
                        "execution_sha256": owner.execution_sha256,
                        "original_primary_plan_sha256": state["marker"][
                            "original_primary_plan_sha256"
                        ],
                    }
                ],
                "retirements": [],
            },
        )
        actor = state["marker"]["recovery_roster_json"]["attempts"][0]
        identity = {key: actor[key] for key in module._WORKER_FIELDS}
        actor["identity_text"] = json.dumps(identity)
        actor["identity_sha256"] = hashlib.sha256(actor["identity_text"].encode()).hexdigest()
        state["marker"]["recovery_roster_json"] = {
            "attempts": prior_roster["attempts"] + [actor],
            "retirements": prior_roster["retirements"],
        }
        child = {
            "schema": "attempted-stop-children.v2",
            "original_primary_plan": json.loads(state["marker"]["original_primary_plan_text"]),
            "original_primary_plan_sha256": state["marker"]["original_primary_plan_sha256"],
            "admitted_inventory_sha256": state["marker"]["admitted_inventory_sha256"],
            "admitted_terminal": json.loads(terminal_text),
            "recovery_roster_json": deepcopy(state["marker"]["recovery_roster_json"]),
        }
        state["marker"].update(
            child_inventory_text=json.dumps(child),
            child_inventory_sha256=hashlib.sha256(json.dumps(child).encode()).hexdigest(),
        )
        if state.get("partial_crash_once"):
            state["partial_crash_once"] = False
            state["marker"].update(child_inventory_text=None, child_inventory_sha256=None)
            return SimpleNamespace(state="pending", exit_code=1)
        return SimpleNamespace(state="children_drained", exit_code=0)

    monkeypatch.setattr(module, "run_stopped_director_recovery_process", drain)

    def physical(identity):
        state["physical_calls"].append(deepcopy(identity))
        if state["physical_error"]:
            raise RuntimeError(state["physical_error"])
        return {
            "schema": "attempted-stop-retirement-observation.v2",
            "worker_identity": identity,
            "process_retired": True,
            "cgroup_empty": True,
            "observed_boot_id": identity["worker_boot_id"],
            "observed_at": __import__("datetime")
            .datetime.now(__import__("datetime").UTC)
            .isoformat(),
            "unit_properties": {
                "LoadState": "loaded",
                "ActiveState": "inactive",
                "MainPID": "0",
                "InvocationID": identity["worker_invocation_id"],
                "ControlGroup": identity["worker_cgroup"],
            },
        }

    monkeypatch.setattr(
        module, "verify_attempted_stop_recovery_retirement", physical, raising=False
    )
    registry = MagicMock()
    registry.entry_sha256.return_value = execution["registry_entry_sha256"]
    suite = tmp_path / "suite.json"
    suite.write_text("{}")
    registry.verify_entry.return_value = (suite, None)
    monkeypatch.setenv("LAB_SUITE_REGISTRY_FILE", str(tmp_path / "registry.json"))
    monkeypatch.setattr(registry_module, "load_suite_registry", lambda *a: registry)
    experiment, trajectory = object(), object()
    documents = MagicMock(return_value=(experiment, trajectory))
    monkeypatch.setattr(module, "_proposal_documents", documents)
    monkeypatch.setattr(module, "remaining_stop_closure_seconds", lambda *a: 60)
    monkeypatch.setattr(module, "canonical_json_bytes", lambda doc: b"cpu-fixture-document")
    monkeypatch.setattr(module, "store_director_artifact", lambda *a, **kw: "5" * 64)

    def commit(*args, **kwargs):
        state["commit_calls"] += 1
        if state["fail_commit_once"]:
            state["fail_commit_once"] = False
            raise RuntimeError("bounded injected record_experiment commit failure")

    monkeypatch.setattr(module, "commit_experiment_record", commit)
    arguments = dict(
        director=director,
        planner=planner,
        run_id=owner.run_id,
        recovery_id=recovery_id,
        owner=process_owner,
        execution_owner=owner,
        lease=MagicMock(),
        artifact_root=tmp_path,
    )
    return module, arguments, state, deadproof, terminal_proof, target


def test_drained_retry_after_ledger_commit_failure_reuses_genuine_proof(monkeypatch, tmp_path):
    module, arguments, state, deadproof, terminal_proof, target = retry_after_drained_fixture(
        monkeypatch, tmp_path
    )
    with pytest.raises(RuntimeError, match="record_experiment commit failure"):
        module.reconcile_stopped_attempted_proposal(**arguments)
    persisted = deepcopy(state["marker"])
    assert persisted["closure_state"] == "drained" and state["drain_calls"] == ["7" * 32]
    assert state["commit_calls"] == 1
    assert len(json.loads(persisted["original_primary_plan_text"])) == 4
    assert len(json.loads(persisted["admitted_inventory_text"])) == 1
    prior_deadproofs, prior_terminal_proofs = deadproof.call_count, terminal_proof.call_count

    module.reconcile_stopped_attempted_proposal(**arguments)

    assert state["drain_calls"] == ["7" * 32]
    assert state["marker"] == persisted  # Original receipt/proof/deadline never rewritten.
    assert state["commit_calls"] == 2
    assert (
        deadproof.call_count > prior_deadproofs
        and terminal_proof.call_count > prior_terminal_proofs
    )
    arguments["lease"].heartbeat.assert_called_once()
    assert target["first_stop_at"] == persisted["created_at"]


@pytest.mark.parametrize("change", [{"current_generation": 3}, {"worker_invocation_id": "9" * 32}])
def test_drained_retry_stale_owner_does_not_relaunch_or_commit(monkeypatch, tmp_path, change):
    module, arguments, state, _, _, target = retry_after_drained_fixture(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="record_experiment commit failure"):
        module.reconcile_stopped_attempted_proposal(**arguments)
    persisted = deepcopy(state["marker"])
    target.update(change)
    with pytest.raises(RecoveryPending, match="owner changed"):
        module.reconcile_stopped_attempted_proposal(**arguments)
    assert state["drain_calls"] == ["7" * 32] and state["marker"] == persisted
    assert state["commit_calls"] == 1


def test_drained_retry_corrupt_receipt_cannot_relaunch_or_commit(monkeypatch, tmp_path):
    module, arguments, state, _, _, _ = retry_after_drained_fixture(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="record_experiment commit failure"):
        module.reconcile_stopped_attempted_proposal(**arguments)
    state["marker"]["terminal_inventory_sha256"] = "0" * 64
    corrupted = deepcopy(state["marker"])
    with pytest.raises(RecoveryPending, match="inventory hash changed"):
        module.reconcile_stopped_attempted_proposal(**arguments)
    assert state["drain_calls"] == ["7" * 32]
    assert state["marker"] == corrupted and state["commit_calls"] == 1


@pytest.mark.parametrize(
    "failure", ["live process", "unknown PID", "foreign invocation", "missing cgroup proof"]
)
def test_drained_retry_requires_current_physical_retirement(monkeypatch, tmp_path, failure):
    module, arguments, state, _, _, _ = retry_after_drained_fixture(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="record_experiment commit failure"):
        module.reconcile_stopped_attempted_proposal(**arguments)
    state["physical_error"] = failure
    persisted, writes = deepcopy(state["marker"]), list(state["writes"])
    with pytest.raises(RecoveryPending, match="retirement|seal"):
        module.reconcile_stopped_attempted_proposal(**arguments)
    assert state["marker"] == persisted and state["writes"] == writes
    assert state["commit_calls"] == 1 and state["drain_calls"] == ["7" * 32]


def test_drained_retry_changed_original_plan_is_rejected(monkeypatch, tmp_path):
    module, arguments, state, _, _, _ = retry_after_drained_fixture(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="record_experiment commit failure"):
        module.reconcile_stopped_attempted_proposal(**arguments)
    state["plan"].pop()
    with pytest.raises(RecoveryPending, match="primary plan"):
        module.reconcile_stopped_attempted_proposal(**arguments)
    assert state["commit_calls"] == 1 and state["drain_calls"] == ["7" * 32]


@pytest.mark.parametrize(
    "damage", ["missing_retirement", "missing_actor", "wrong_owner", "wrong_plan"]
)
def test_drained_replay_rejects_incomplete_or_foreign_worker_roster(monkeypatch, tmp_path, damage):
    module, arguments, state, _, _, _ = retry_after_drained_fixture(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="record_experiment commit failure"):
        module.reconcile_stopped_attempted_proposal(**arguments)
    roster = state["marker"]["recovery_roster_json"]
    if damage == "missing_retirement":
        roster["retirements"] = []
    elif damage == "missing_actor":
        roster["attempts"] = []
        roster["retirements"] = []
    elif damage == "wrong_owner":
        roster["attempts"][0]["expected_generation"] += 1
    else:
        roster["attempts"][0]["original_primary_plan_sha256"] = "0" * 64
    writes = list(state["writes"])
    with pytest.raises(RecoveryPending, match="retirement|seal"):
        module.reconcile_stopped_attempted_proposal(**arguments)
    assert state["writes"] == writes and state["commit_calls"] == 1
    assert state["drain_calls"] == ["7" * 32]


def test_partial_recovery_crash_retires_original_actor_before_new_worker(monkeypatch, tmp_path):
    module, arguments, state, _, _, _ = retry_after_drained_fixture(monkeypatch, tmp_path)
    state.update(partial_crash_once=True, fail_commit_once=False)
    with pytest.raises(RecoveryPending, match="undrained"):
        module.reconcile_stopped_attempted_proposal(**arguments)
    first_actor = deepcopy(state["marker"]["recovery_roster_json"]["attempts"][0])
    assert state["marker"]["child_inventory_text"] is None
    module.reconcile_stopped_attempted_proposal(**arguments)
    roster = state["marker"]["recovery_roster_json"]
    assert roster["attempts"][0] == first_actor
    assert [a["worker_invocation_id"] for a in roster["attempts"]] == ["7" * 32, "9" * 32]
    assert [r["retired_ordinal"] for r in roster["retirements"]] == [1, 2]
    assert state["marker"]["closure_state"] == "drained" and state["commit_calls"] == 1


def test_children_seal_retry_does_not_launch_replacement_worker(monkeypatch, tmp_path):
    module, arguments, state, _, _, _ = retry_after_drained_fixture(monkeypatch, tmp_path)
    state["physical_error"] = "live process"
    with pytest.raises(RecoveryPending, match="retirement|seal"):
        module.reconcile_stopped_attempted_proposal(**arguments)
    assert state["marker"]["child_inventory_text"] is not None
    state.update(physical_error=None, fail_commit_once=False)
    module.reconcile_stopped_attempted_proposal(**arguments)
    assert state["drain_calls"] == ["7" * 32] and state["commit_calls"] == 1


@pytest.mark.parametrize(
    "damage",
    [
        "identity_hash",
        "observation_hash",
        "bool_ordinal",
        "child_plan",
        "final_roster",
        "missing_plan",
    ],
)
def test_drained_replay_binds_native_child_and_final_seals(monkeypatch, tmp_path, damage):
    import hashlib

    module, arguments, state, _, _, _ = retry_after_drained_fixture(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="record_experiment commit failure"):
        module.reconcile_stopped_attempted_proposal(**arguments)
    marker = state["marker"]
    if damage in {"identity_hash", "observation_hash", "bool_ordinal"}:
        item = marker["recovery_roster_json"]["retirements"][0]
        item[
            {
                "identity_hash": "identity_sha256",
                "observation_hash": "observation_sha256",
                "bool_ordinal": "retired_ordinal",
            }[damage]
        ] = True if damage == "bool_ordinal" else "0" * 64
    else:
        prefix = (
            "child_inventory"
            if damage == "child_plan"
            else "terminal_inventory"
            if damage == "final_roster"
            else "missing_primary_inventory"
        )
        value = json.loads(marker[prefix + "_text"])
        if damage == "final_roster":
            value["recovery_roster_json"]["retirements"] = []
        else:
            value["original_primary_plan_sha256"] = "0" * 64
        marker[prefix + "_text"] = json.dumps(value)
        marker[prefix + "_sha256"] = hashlib.sha256(marker[prefix + "_text"].encode()).hexdigest()
    writes = list(state["writes"])
    with pytest.raises(RecoveryPending):
        module.reconcile_stopped_attempted_proposal(**arguments)
    assert (
        state["writes"] == writes
        and state["commit_calls"] == 1
        and state["drain_calls"] == ["7" * 32]
    )
