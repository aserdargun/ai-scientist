"""CPU-only restart ordering, identity, bounded replay and SQL fence contracts."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from lab.director import resume
from lab.director.ownership import ExecutionOwner, OwnerProcessIdentity


def setup_flow(monkeypatch, *, existing=None, age=0, child_state="drained"):
    run, restart = uuid4(), uuid4()
    contract = MagicMock(
        run_id=run,
        request_sha256="a" * 64,
        execution_sha256="b" * 64,
        execution_json={"provider": "fake-json"},
    )
    process = OwnerProcessIdentity(
        "a" * 64,
        200,
        400,
        str(uuid4()),
        f"swapp-ai-scientist-director-resume-{run.hex}-{restart.hex}.service",
        "c" * 32,
        f"/user.slice/swapp-ai-scientist-director-resume-{run.hex}-{restart.hex}.service",
    )
    prior = {
        "worker_pid": 100,
        "worker_start_ticks": 300,
        "worker_boot_id": str(uuid4()),
        "worker_unit": f"swapp-ai-scientist-director-dispatch-{run.hex}.service",
        "worker_invocation_id": "d" * 32,
        "worker_cgroup": f"/user.slice/swapp-ai-scientist-director-dispatch-{run.hex}.service",
    }
    target = {"current_generation": 1, **prior}
    events = []
    monkeypatch.setattr(resume, "_read_target", lambda *a: target)
    monkeypatch.setattr(
        resume, "_owner_is_proven_dead", lambda *a: events.append("owner_dead") or True
    )
    monkeypatch.setattr(resume, "DirectorRunLease", MagicMock())
    monkeypatch.setattr(resume, "_drain_director_sandbox", lambda *a: events.append("sandbox"))
    monkeypatch.setattr(resume, "_prove_terminal_jobs", lambda *a: events.append("jobs"))
    monkeypatch.setattr(
        resume, "_child_blockers", lambda *a, **kw: events.append("independent_children") or []
    )
    monkeypatch.setattr(
        resume, "verified_checkpoint_manifest", lambda *a, **kw: events.append("journal") or []
    )
    monkeypatch.setattr(
        resume,
        "run_director_restart_recovery_process",
        lambda *a, **kw: events.append("recover_children") or MagicMock(state=child_state),
    )
    monkeypatch.setattr(resume, "_claim", lambda *a: events.append("CAS") or "receipt")
    engine, planner = MagicMock(), MagicMock()
    query = engine.connect.return_value.__enter__.return_value.execute.return_value
    query.mappings.return_value.one_or_none.return_value = existing
    results = [
        {
            "state": "pending",
            "created_at": (datetime.now(UTC) - timedelta(seconds=age)).isoformat(),
        },
        "e" * 64,
    ]
    engine.begin.return_value.__enter__.return_value.execute.return_value.scalar_one.side_effect = (
        results
    )
    kwargs = dict(
        contract=contract, process=process, restart_id=restart, artifact_root=Path("/unused")
    )
    return engine, planner, kwargs, events


def test_independent_drain_and_journal_precede_generation_cas(monkeypatch):
    engine, planner, kwargs, events = setup_flow(monkeypatch)
    assert resume.resume_execution(engine, planner, **kwargs) == "receipt"
    assert events == [
        "owner_dead",
        "sandbox",
        "recover_children",
        "jobs",
        "independent_children",
        "owner_dead",
        "journal",
        "CAS",
    ]


def test_live_child_never_reaches_cas(monkeypatch):
    engine, planner, kwargs, events = setup_flow(monkeypatch, child_state="pending")
    with pytest.raises(resume.RecoveryPending, match="children"):
        resume.resume_execution(engine, planner, **kwargs)
    assert "CAS" not in events


def test_restart_cleanup_window_cannot_reset(monkeypatch):
    engine, planner, kwargs, events = setup_flow(monkeypatch, age=121)
    with pytest.raises(resume.RecoveryPending, match="immutable restart cleanup"):
        resume.resume_execution(engine, planner, **kwargs)
    assert "recover_children" not in events and "CAS" not in events


def test_deadline_downtime_and_named_drain_digest():
    now = datetime.now(UTC)
    receipt = resume.ResumeReceipt(
        ExecutionOwner(uuid4(), 2, "a" * 32, "b" * 64),
        uuid4(),
        now - timedelta(seconds=100),
        now - timedelta(seconds=1),
        True,
        "c" * 64,
    )
    assert receipt.elapsed_wall_seconds >= 100
    assert receipt.closure_only
    assert receipt.drain_observation_sha256 == "c" * 64


def test_only_exact_run_bound_resume_units_are_trusted():
    from lab.director.recovery import _valid_owner_unit

    run, restart = uuid4(), uuid4()
    assert _valid_owner_unit(
        f"swapp-ai-scientist-director-resume-{run.hex}-{restart.hex}.service", run
    )
    assert not _valid_owner_unit(
        f"swapp-ai-scientist-director-resume-{uuid4().hex}-{restart.hex}.service", run
    )
    assert not _valid_owner_unit(
        f"swapp-ai-scientist-director-resume-{run.hex}-anything.service", run
    )


def test_migration_binds_attempt_deadline_and_history(monkeypatch):
    path = Path("lab/db/migrations/versions/0030_director_resume.py")
    spec = importlib.util.spec_from_file_location("resume030_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements = []
    monkeypatch.setattr(module.op, "execute", statements.append)
    module.upgrade()
    from sqlalchemy import text

    assert all(not text(statement)._bindparams for statement in statements)
    sql = "\n".join(statements)
    assert "expected_generation+1" in sql
    assert "created_at + interval '120 seconds'" in sql
    assert "resume claimant fields are missing or null" in sql
    assert "jsonb_typeof(proc->'worker_pid') IS DISTINCT FROM 'number'" in sql
    assert "lab.director_restart_job_drains" in sql and "infrastructure_unattempted" in sql
    assert "UPDATE lab.director_execution_contracts" not in sql
    assert "UPDATE lab.director_run_owners" not in sql


def test_bare_scorer_result_is_never_replayed_without_pre_enqueue_evidence():
    from lab.director.evaluation_recovery import recover_evaluation_measurement

    engine, lease = MagicMock(), MagicMock()
    lease.read_checkpoint.return_value = None
    assert (
        recover_evaluation_measurement(
            engine,
            lease,
            run_id=uuid4(),
            experiment_id="candidate",
            kind="primary",
            task_id="task",
            seed=0,
            expected={},
            artifact_root=Path("/unused"),
        )
        is None
    )
    engine.connect.assert_not_called()


@pytest.mark.parametrize("mutate", ["candidate_sha256", "seed", "run_id"])
def test_pre_enqueue_identity_mismatch_rejected_before_score_read(mutate):
    from lab.director.evaluation_recovery import recover_evaluation_measurement
    from lab.director.ownership import owned_execution

    run = uuid4()
    owner = ExecutionOwner(run, 2, "a" * 32, "b" * 64)
    payload = {
        "schema": "director-evaluation-evidence.v1",
        "run_id": str(run),
        "experiment_id": "candidate",
        "evaluation_kind": "primary",
        "task_id": "task",
        "seed": 0,
        "candidate_sha256": "c" * 64,
    }
    payload[mutate] = "changed"
    engine, lease = MagicMock(), MagicMock()
    lease.read_checkpoint.return_value = {"payload": payload}
    with owned_execution(owner), pytest.raises(ValueError, match="identity changed"):
        recover_evaluation_measurement(
            engine,
            lease,
            run_id=run,
            experiment_id="candidate",
            kind="primary",
            task_id="task",
            seed=0,
            expected={"candidate_sha256": "c" * 64},
            artifact_root=Path("/unused"),
        )
    engine.connect.assert_not_called()


def test_retry_credit_is_consumed_before_work_and_cannot_repeat():
    from lab.director.evaluation_recovery import reserve_infrastructure_retry
    from lab.director.ownership import owned_execution

    owner = ExecutionOwner(uuid4(), 2, "a" * 32, "b" * 64)
    engine, lease = MagicMock(), MagicMock(run_id=owner.run_id)
    lease.read_checkpoint.return_value = None
    query = engine.connect.return_value.__enter__.return_value.execute.return_value
    query.scalar_one.return_value = 7
    with owned_execution(owner):
        assert reserve_infrastructure_retry(
            engine,
            lease,
            experiment_id="candidate",
            artifact_root=Path("/unused"),
            remaining_seconds=500,
        )
        assert lease.append_checkpoint.call_args.kwargs["payload"]["maximum_seconds"] == 120
        lease.read_checkpoint.return_value = {"payload": {"retry_ordinal": 1}}
        assert not reserve_infrastructure_retry(
            engine,
            lease,
            experiment_id="candidate",
            artifact_root=Path("/unused"),
            remaining_seconds=500,
        )
    assert lease.append_checkpoint.call_count == 1


@pytest.mark.parametrize(
    ("prefix", "digest_kind"),
    [(0, "present"), (1, "present"), (2, "present"), (3, "present"),
     (1, "absent"), (2, "absent"), (3, "absent"), (1, "wrong"), (1, "null")],
)
def test_holdout_suffix_preserves_historical_bytes_and_single_budget_charge(
    monkeypatch, tmp_path, prefix, digest_kind
):
    import hashlib
    import json

    from test_holdout_loop_recovery import _minimal_loop_state

    from lab.director import holdout_replay
    from lab.director.budget import RunBudget
    from lab.director.holdout import HoldoutReceipt
    from lab.director.journal import canonical_bytes
    from lab.director.loop import HoldoutApprovedSnapshot, _budget_to_json
    from lab.director.ownership import owned_execution

    run, reservation_id = uuid4(), uuid4()
    owner = ExecutionOwner(run, 2, "a" * 32, "b" * 64)
    state = _minimal_loop_state(run, "candidate")
    state = state.model_copy(
        update={
            "holdout_approved_snapshot": HoldoutApprovedSnapshot.from_state(state, keep_count=0)
        }
    )
    budget = RunBudget(monotonic=lambda: 10.0)
    reserved = budget.reserve_work(wall_seconds=50, model_tokens=0)
    checkpoints = {}

    def checkpoint(key, phase, payload):
        digest = hashlib.sha256(canonical_bytes(payload)).hexdigest()
        receipt = {
            "schema": "director-checkpoint.v1",
            "sequence": len(checkpoints),
            "key": key,
            "phase": phase,
            "payload_sha256": digest,
            "blob_sha256": digest,
        }
        checkpoints[key] = {"receipt": receipt, "payload": payload}
        return digest

    prior_sha = checkpoint(
        "director-state:0", "director_loop_state", state.model_dump(mode="json", by_alias=True)
    )
    checkpoint(
        f"holdout-budget-reserved:{reserved.reservation_id}",
        "holdout_budget_reserved",
        {
            "trigger_kind": "run_end",
            "trigger_index": 1,
            "reservation_id": str(reserved.reservation_id),
            "budget": _budget_to_json(budget.snapshot()),
        },
    )
    engine, lease = MagicMock(), MagicMock(run_id=run)
    restart = {
        "restart_id": uuid4(),
        "execution_json": {
            "request": {"budget": {"experiments": 10, "wall_seconds": 1000, "model_tokens": 1000}}
        },
    }
    query = engine.connect.return_value.__enter__.return_value.execute.return_value
    query.mappings.return_value.one.return_value = restart
    query.scalars.return_value.all.side_effect = lambda: [
        c["receipt"] for c in checkpoints.values()
    ]
    lease.read_checkpoint.side_effect = lambda *, key, artifact_root: checkpoints.get(key)
    monkeypatch.setattr(holdout_replay, "assert_historical_receipt", lambda *a, **kw: None)
    monkeypatch.setattr(
        holdout_replay,
        "store_director_artifact",
        lambda blob, **kw: hashlib.sha256(blob).hexdigest(),
    )
    receipt = HoldoutReceipt(
        reservation_id=reservation_id,
        state="passed",
        bit=True,
        run_id=run,
        candidate_experiment_id="candidate",
        trigger_kind="run_end",
        trigger_index=1,
        admitted_generation=1,
        execution_sha256="b" * 64,
    )
    with owned_execution(owner):
        result, _ = holdout_replay.append_terminal_holdout_suffix(
            engine, lease, receipt=receipt, prior_state_sha256=prior_sha, artifact_root=tmp_path
        )
        original = json.loads(
            engine.begin.return_value.__enter__.return_value.execute.call_args.args[1]["bundle"]
        )
        if digest_kind != "present":
            application, _, marker = original["entries"]
            if digest_kind == "absent":
                application["payload"].pop("expected_state_sha256")
            else:
                application["payload"]["expected_state_sha256"] = (
                    "f" * 64 if digest_kind == "wrong" else None
                )
            for entry in (application, marker):
                if entry is marker:
                    entry["payload"]["application_sha256"] = application["receipt"][
                        "payload_sha256"
                    ]
                blob = canonical_bytes(entry["payload"])
                digest = hashlib.sha256(blob).hexdigest()
                entry["receipt"].update(payload_sha256=digest, blob_sha256=digest)
                entry["payload_text"] = blob.decode()
        for entry in original["entries"][:prefix]:
            checkpoints[entry["receipt"]["key"]] = {
                "receipt": entry["receipt"],
                "payload": entry["payload"],
            }
        # Existing application freezes budget elapsed time as well as receipt generation.
        if digest_kind in {"wrong", "null"}:
            with pytest.raises(ValueError, match="state digest conflicts"):
                holdout_replay.append_terminal_holdout_suffix(
                    engine, lease, receipt=receipt, prior_state_sha256=prior_sha,
                    artifact_root=tmp_path,
                )
            return
        result2, _ = holdout_replay.append_terminal_holdout_suffix(
            engine, lease, receipt=receipt, prior_state_sha256=prior_sha, artifact_root=tmp_path
        )
        replayed = json.loads(
            engine.begin.return_value.__enter__.return_value.execute.call_args.args[1]["bundle"]
        )
    assert result.budget["wall_seconds"] == result2.budget["wall_seconds"] == 50.0
    assert result2.budget["reservations"] == []
    assert result2.holdout_checks_passed == 1
    assert replayed["entries"][0]["payload"]["admitted_generation"] == 1
    for old, new in zip(original["entries"][:prefix], replayed["entries"][:prefix], strict=True):
        assert old == new


def test_pre_enqueue_gap_uses_observed_evidence_for_abandonment(monkeypatch):
    import json

    from lab.director import ownership
    from lab.director.evaluation_recovery import recover_abandoned_experiment

    owner = ExecutionOwner(uuid4(), 2, "a" * 32, "b" * 64)
    planner, director, lease = MagicMock(), MagicMock(), MagicMock()
    query = director.connect.return_value.__enter__.return_value.execute.return_value
    query.scalars.return_value.all.return_value = ["evidence-key"]
    query.scalar_one.return_value = False
    lease.read_checkpoint.return_value = {
        "receipt": {"key": "evidence-key"},
        "payload": {
            "experiment_id": "candidate",
            "evaluation_kind": "primary",
            "task_id": "task",
            "seed": 0,
        },
    }
    monkeypatch.setattr(
        ownership, "assert_execution_owner_closure_transaction", lambda *a, **kw: owner
    )
    assert recover_abandoned_experiment(
        planner,
        run_id=owner.run_id,
        experiment_id="candidate",
        evidence_engine=director,
        lease=lease,
        artifact_root=Path("/unused"),
    )
    statement, params = planner.begin.return_value.__enter__.return_value.execute.call_args.args
    assert set(statement.compile().params) == {
        "run", "generation", "invocation", "execution", "experiment", "evidence"
    }
    assert json.loads(params["evidence"])["receipt"]["key"] == "evidence-key"
    assert params["generation"] == 2
    planner.connect.assert_not_called()


def test_finalization_replacements_match_corrected_037_source(monkeypatch):
    import re

    modules = []
    for name in ("0027_terminal_finalization", "0030_director_resume"):
        spec = importlib.util.spec_from_file_location(
            name, Path("lab/db/migrations/versions") / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sql = []
        monkeypatch.setattr(module.op, "execute", sql.append)
        module.upgrade()
        modules.append("\n".join(sql))
    old_sql, new_sql = modules
    for fragment in re.findall(r"\$old\$(.*?)\$old\$", new_sql, re.S):
        assert fragment in old_sql, repr(fragment)
