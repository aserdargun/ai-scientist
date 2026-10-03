"""Inert completed-calibration stop fixtures; no live DB, service or GPU effects."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from test_multi_baseline_stop040 import orchestration_fixture

from lab.director import recovery, stop_closure

pytest_plugins = ("test_multi_baseline_stop040",)


def completed_fixture(tmp_path, monkeypatch):
    engine, arguments, payload, receipts, calls = orchestration_fixture(
        tmp_path, monkeypatch, ("scored", "scored", "scored"), (81, 81, 81)
    )
    monkeypatch.setattr(stop_closure, "_has_baseline_calibration", lambda *_args: True)
    sources = tuple(
        SimpleNamespace(name=row["baseline_name"], candidate_sha256=row["candidate_sha256"])
        for row in payload["rows"]
    )
    calibration = SimpleNamespace(
        run_id=arguments["run_id"],
        **{
            key: payload["execution"][key]
            for key in ("suite_id", "suite_version", "harness_sha256", "image_sha256")
        },
        tasks=tuple(
            SimpleNamespace(**task, baseline_versions=sources)
            for task in json.loads(payload["suite_manifest"])["tasks"]
        ),
        champion_experiment_id=payload["rows"][0]["experiment_id"],
        champion_baseline_name=payload["rows"][0]["baseline_name"],
    )
    reader = MagicMock(return_value=calibration)
    monkeypatch.setattr(stop_closure, "read_registered_calibration", reader)
    return engine, arguments, payload, receipts, calls, calibration, reader


def test_calibrated_completed_baseline_phase_needs_no_baseline_rewrite(tmp_path, monkeypatch):
    engine, arguments, payload, receipts, calls, calibration, reader = completed_fixture(
        tmp_path, monkeypatch
    )
    before = deepcopy((payload, receipts))
    for _attempt in range(2):
        stop_closure.reconcile_stopped_baseline(engine, engine, **arguments)
    assert (payload, receipts) == before
    assert calls["begin"] == []
    assert calls["close"] == []
    assert calls["commit"] == []
    assert reader.call_count == 2
    assert arguments["lease"].heartbeat.call_count == 2
    stop_closure.run_stopped_director_recovery_process.assert_not_called()
    stop_closure._drain_director_sandbox.assert_not_called()


@pytest.mark.parametrize(
    "corruption",
    [
        "unfinished",
        "missing_cells",
        "missing_pair",
        "live_owner",
        "calibration_task",
        "calibration_source",
        "champion",
    ],
)
def test_calibrated_stop_retains_full_inventory_and_owner_checks(tmp_path, monkeypatch, corruption):
    engine, arguments, payload, receipts, calls, calibration, reader = completed_fixture(
        tmp_path, monkeypatch
    )
    if corruption == "unfinished":
        payload["rows"][-1]["status"] = "primary_running"
    elif corruption == "missing_cells":
        payload["measurements"].pop()
    elif corruption == "missing_pair":
        receipts[payload["rows"][-1]["experiment_id"]] = None
    elif corruption == "live_owner":
        monkeypatch.setattr(stop_closure, "prove_stopped_owner_dead", lambda *_args: False)
    elif corruption == "calibration_task":
        calibration.tasks[0].profile_sha256 = "f" * 64
    elif corruption == "calibration_source":
        calibration.tasks[0].baseline_versions[0].candidate_sha256 = "f" * 64
    else:
        calibration.champion_experiment_id = "foreign"
    before = deepcopy((payload, receipts))
    with pytest.raises(recovery.RecoveryPending):
        stop_closure.reconcile_stopped_baseline(engine, engine, **arguments)
    assert (payload, receipts) == before
    assert calls["begin"] == calls["close"] == calls["commit"] == []
    arguments["lease"].heartbeat.assert_not_called()


def test_calibrated_stop_does_not_renew_expired_original_cleanup_deadline(tmp_path, monkeypatch):
    engine, arguments, payload, receipts, calls, calibration, reader = completed_fixture(
        tmp_path, monkeypatch
    )
    with pytest.raises(recovery.RecoveryPending, match="original automatic stop cleanup"):
        stop_closure.reconcile_stopped_baseline(
            engine, engine, **arguments, deadline_at=datetime.now(UTC) - timedelta(seconds=1)
        )
    reader.assert_not_called()
    assert calls["begin"] == calls["close"] == calls["commit"] == []


@pytest.mark.parametrize(
    "blocker",
    [
        None,
        "active_score_jobs",
        "gpu_release_unverified",
        "gpu_resource_quarantined",
        "ledger_mismatch",
    ],
)
def test_calibrated_pre_candidate_stop_reaches_original_finalizer_only_after_all_proofs(
    tmp_path, monkeypatch, blocker
):
    """Original recovery/report checks; SQL, OS proof and Scorer process are inert fixtures."""
    from hashlib import sha256

    from lab.director.journal import canonical_bytes
    from lab.director.ownership import ExecutionOwner, active_execution_owner

    engine, arguments, payload, receipts, calls, calibration, reader = completed_fixture(
        tmp_path, monkeypatch
    )
    run_id, old_owner = arguments["run_id"], arguments["owner"]
    execution_owner = ExecutionOwner(run_id, 1, old_owner.worker_invocation_id, "e" * 64)
    recovery_id = recovery._recovery_id(
        run_id, recovery._request_sha256(run_id, "stop_and_finalize")
    )
    # Durable intent and started receipt exist, but no proposal/candidate registration exists.
    checkpoints = [{"key": "proposal-intent:0"}, {"key": "provider-attempt-started:0"}]
    before = deepcopy((payload, receipts, checkpoints))
    report = {"run_id": str(run_id), "status": "stopped", "fixture_only": True}
    report_sha = sha256(canonical_bytes(report)).hexdigest()
    connection = engine.connect.return_value.__enter__.return_value
    original_execute = connection.execute.side_effect

    def result(value):
        reply = MagicMock()
        reply.scalar_one.return_value = value
        reply.mappings.return_value.one.return_value = value
        reply.mappings.return_value.one_or_none.return_value = value
        return reply

    def execute(statement, parameters):
        query = str(statement)
        if "state,payload_sha256,report_sha256" in query:
            return result(
                dict(
                    state="stop_requested",
                    payload_sha256=old_owner.payload_sha256,
                    report_sha256=None,
                    request_json={"provider": "local-qwen"},
                    owner_id="fixture-owner",
                    origin="aos",
                )
            )
        if "FROM lab.director_execution_control AS control" in query:
            return result(
                dict(
                    generation=1,
                    worker_invocation_id=old_owner.worker_invocation_id,
                    execution_sha256="e" * 64,
                )
            )
        if "SELECT request_json" in query:
            return result({"provider": "local-qwen"})
        if "SELECT state FROM lab.runs" in query:
            return result("stop_requested")
        if "lab.request_director_run_stop" in query:
            return result(True)
        if "SELECT r.state,r.report_sha256" in query:
            return result(
                dict(
                    state="stopped",
                    report_sha256=report_sha,
                    report_json=report,
                    stored_report_sha=report_sha,
                )
            )
        return original_execute(statement, parameters)

    connection.execute.side_effect = execute
    engine.begin.return_value.__enter__.return_value.execute.side_effect = execute
    monkeypatch.setattr(recovery, "_current_owner_row", lambda *_args: {})
    monkeypatch.setattr(recovery, "_stored_owner", lambda *_args: old_owner)
    monkeypatch.setattr(recovery, "_ensure_recovery_intent", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(recovery, "DirectorRunLease", lambda *_args: arguments["lease"])
    monkeypatch.setattr(recovery, "_insert_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        recovery,
        "_persist_receipt",
        lambda *_args, **kwargs: {**kwargs["result"], "recovery_state": kwargs["state"]},
    )
    inspections = [0]

    def inspect(*_args, **_kwargs):
        inspections[0] += 1
        return dict(
            owner_proven_dead=True,
            report_valid=False,
            recovery_blockers=(
                ["document_mismatch"] if blocker == "ledger_mismatch" and inspections[0] > 1 else []
            ),
        )

    monkeypatch.setattr(recovery, "inspect_recovery", inspect)
    child_check = MagicMock(return_value=[] if blocker in (None, "ledger_mismatch") else [blocker])
    monkeypatch.setattr(recovery, "_child_blockers", child_check)
    monkeypatch.setattr(stop_closure, "remaining_stop_closure_seconds", lambda *_args: None)
    effects = []

    def seal(_engine, *, run_id):
        assert active_execution_owner() == execution_owner
        effects.append("seal")

    def finalize(selected, **kwargs):
        assert selected == run_id
        assert kwargs["admitted_generation"] == execution_owner.generation
        assert kwargs["execution_sha256"] == execution_owner.execution_sha256
        assert 0 < kwargs["remaining_seconds"] <= 60
        effects.append("finalize")
        return SimpleNamespace(exit_code=0, result={"state": "finalized"}, unit="fixture-scorer")

    monkeypatch.setattr(recovery, "seal_run_task_plan", seal)
    monkeypatch.setattr(recovery, "run_scorer_finalize_process", finalize)
    observed = recovery.apply_stop_and_finalize(
        engine,
        engine,
        run_id=run_id,
        recovery_id=recovery_id,
        remaining_seconds=60,
        reconcile_interrupted_baseline=True,
        artifact_root=tmp_path,
        expected_owner=execution_owner,
        cleanup_deadline=datetime.now(UTC) + timedelta(seconds=60),
    )
    assert (payload, receipts, checkpoints) == before
    assert calls["begin"] == calls["close"] == calls["commit"] == []
    assert child_check.call_args.kwargs["requires_gpu"] is True
    if blocker is None:
        assert observed["action"] == "finalized_stopped"
        assert observed["report_sha256"] == report_sha
        assert effects == ["seal", "finalize"]
    else:
        assert observed["recovery_state"] == "pending"
        assert effects == []
