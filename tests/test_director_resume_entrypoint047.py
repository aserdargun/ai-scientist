"""Inert CLI resume checks: no DB, service, subprocess, sandbox, model or GPU launch."""

from __future__ import annotations

import hashlib
import json
from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from lab import cli


@pytest.fixture(autouse=True)
def slice_limits(monkeypatch):
    """Keep the shared slice provisioning boundary inert in these CLI tests."""
    check = MagicMock()
    monkeypatch.setattr(cli.SystemdUnitManager, "ensure_slice", check)
    return check


def validated_target(monkeypatch, tmp_path):
    suite = tmp_path / "suite.json"
    suite.write_bytes(b"{}")
    digest = hashlib.sha256(suite.read_bytes()).hexdigest()
    entry = SimpleNamespace(
        suite_id="synthetic.resume",
        track="anomaly",
        program_version="fixture",
        provider="fake-json",
        suite_manifest_sha256=digest,
        scenario_sha256="a" * 64,
        provider_config_sha256=None,
        proposal_contract="candidate-python.v1",
        snapshot_sha256=None,
        proposal_limit=1,
    )
    registry = MagicMock()
    registry.get.return_value = entry
    registry.verify_entry.return_value = (suite, tmp_path / "scenario.json")
    registry.entry_sha256.return_value = "b" * 64
    request = dict(
        suite=entry.suite_id,
        track="anomaly",
        program_version="fixture",
        provider="fake-json",
        proposal_limit=1,
        suite_manifest_sha256=digest,
        scenario_sha256="a" * 64,
        provider_config_sha256=None,
        provider_registry_entry_sha256="b" * 64,
        budget=dict(experiments=1, wall_seconds=60, model_tokens=0),
    )
    payload = json.dumps(
        request, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    row = {"request_json": request, "payload_sha256": hashlib.sha256(payload).hexdigest()}
    monkeypatch.setattr(
        cli,
        "SuiteManifest",
        MagicMock(
            model_validate_json=lambda *a, **kw: SimpleNamespace(
                suite_id=entry.suite_id, suite_version=1
            )
        ),
    )
    monkeypatch.setattr(cli, "compute_harness_hash", lambda *_: SimpleNamespace(sha256="c" * 64))
    monkeypatch.setattr(cli, "DEFAULT_SANDBOX_IMAGE", "image@sha256:" + "d" * 64)
    return row, registry


def test_initial_and_resume_share_same_immutable_validation(monkeypatch, tmp_path):
    row, registry = validated_target(monkeypatch, tmp_path)
    run = uuid4()
    initial = cli._validate_dispatch_target(run, row, registry)
    resumed = cli._validate_dispatch_target(run, row, registry)
    assert initial.contract == resumed.contract
    assert initial.contract.wall_seconds == 60
    assert initial.entry_sha256 == "b" * 64


@pytest.mark.parametrize(
    "field,value",
    [
        ("suite_manifest_sha256", "f" * 64),
        ("provider_registry_entry_sha256", "f" * 64),
        ("program_version", "changed"),
        ("proposal_limit", 2),
        ("scenario_sha256", "e" * 64),
    ],
)
def test_changed_original_request_rejected_before_resume_claim(monkeypatch, tmp_path, field, value):
    row, registry = validated_target(monkeypatch, tmp_path)
    row["request_json"][field] = value
    with pytest.raises(ValueError, match="registry"):
        cli._validate_dispatch_target(uuid4(), row, registry)


def test_changed_request_digest_rejected(monkeypatch, tmp_path):
    row, registry = validated_target(monkeypatch, tmp_path)
    row["payload_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="payload digest"):
        cli._validate_dispatch_target(uuid4(), row, registry)


def test_resume_launcher_uses_exact_uuid_unit_and_allowlisted_environment(
    monkeypatch, slice_limits
):
    run, restart = uuid4(), uuid4()
    monkeypatch.setenv("LAB_SUITE_REGISTRY_FILE", "/private/registry.json")
    monkeypatch.setenv("UNTRUSTED_COMMAND", "do-not-copy")
    command = []

    def launch(argv, **kwargs):
        slice_limits.assert_called_once_with()
        command.extend(argv)
        assert kwargs["timeout"] == 660
        return CompletedProcess(
            argv,
            0,
            stdout=json.dumps(
                {"run_id": str(run), "restart_id": str(restart), "state": "completed"}
            ),
            stderr="",
        )

    monkeypatch.setattr(cli.subprocess, "run", launch)
    result = cli._launch_director_unit(run, restart_id=restart, unit_runtime=600)
    assert (
        "--unit=swapp-ai-scientist-director-resume-" + run.hex + "-" + restart.hex + ".service"
        in command
    )
    assert command[-7:] == [
        "lab.cli",
        "director",
        "resume",
        "--run-id",
        str(run),
        "--restart-id",
        str(restart),
    ]
    assert "--property=RuntimeMaxSec=600" in command
    assert not any("UNTRUSTED_COMMAND" in item for item in command)
    assert result["owner_exit_code"] == 0


def test_resume_launcher_rejects_different_attempt_receipt(monkeypatch):
    run, restart = uuid4(), uuid4()
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *a, **kw: CompletedProcess(
            [], 0, stdout=json.dumps({"run_id": str(run), "restart_id": str(uuid4())}), stderr=""
        ),
    )
    with pytest.raises(RuntimeError, match="restart attempt"):
        cli._launch_director_unit(run, restart_id=restart, unit_runtime=120)


def test_resume_runtime_uses_remaining_original_deadline(monkeypatch):
    run, restart = uuid4(), uuid4()
    engine = MagicMock()
    query = engine.connect.return_value.__enter__.return_value.execute.return_value
    query.mappings.return_value.one_or_none.return_value = {
        "state": "running",
        "stop_requested": False,
        "request_json": {"provider": "fake-json"},
        "remaining_seconds": 0,
    }
    monkeypatch.setattr(cli, "_director_engine", lambda: engine)
    monkeypatch.setattr(cli, "_is_current_resume_unit", lambda *_: False)
    launch = MagicMock(return_value={"run_id": str(run)})
    monkeypatch.setattr(cli, "_launch_director_unit", launch)
    cli._resume_director_run_owned(run, restart)
    assert launch.call_args.kwargs["unit_runtime"] == cli.DIRECTOR_DISPATCH_OVERHEAD_SECONDS
    engine.dispose.assert_called_once()


def test_resume_global_slot_wraps_entire_coordinator(monkeypatch):
    run, restart = uuid4(), uuid4()
    events = []
    monkeypatch.setattr(cli, "_is_current_resume_unit", lambda *_: True)

    def slot(run_id, operation):
        events.append("lock")
        result = operation()
        events.append("unlock")
        return result

    monkeypatch.setattr(cli, "_with_director_global_slot", slot)
    monkeypatch.setattr(
        cli, "_resume_director_run", lambda *a: events.append("resume") or {"state": "completed"}
    )
    result = cli._resume_director_run_owned(run, restart)
    assert events == ["lock", "resume", "unlock"]
    assert result["restart_id"] == str(restart)


def test_terminal_resume_never_calls_loop_or_admission(monkeypatch):
    from lab.director import holdout_replay, task_plan

    loop = MagicMock()
    state = MagicMock(holdout_last_status="passed", completed_proposals=1, proposal_limit=1)
    state.model_dump.return_value = {"state": "exact"}
    loop._load_or_initialize.return_value = (state, {"payload_sha256": "a" * 64})
    replay = MagicMock()
    monkeypatch.setattr(holdout_replay, "recover_observed_terminal_holdouts", replay)
    monkeypatch.setattr(
        task_plan,
        "read_terminal_holdout_checkpoints",
        lambda *a, **kw: ({}, {"state": "exact"}, {"state_sha256": "a" * 64}),
    )
    cli._resume_terminal_loop(loop, object())
    loop.run.assert_not_called()
    loop.check_run_end_holdout.assert_not_called()
    loop._result.assert_called_once_with(
        state, {"payload_sha256": "a" * 64}, "proposal_limit_reached"
    )
    replay.assert_called_once()


def test_terminal_resume_cannot_initialize_missing_state(monkeypatch):
    from lab.director import holdout_replay

    loop = MagicMock()
    loop._latest_state_key.side_effect = RuntimeError("missing state")
    monkeypatch.setattr(holdout_replay, "recover_observed_terminal_holdouts", lambda *a, **kw: 0)
    with pytest.raises(RuntimeError, match="missing state"):
        cli._resume_terminal_loop(loop, object())
    loop._load_or_initialize.assert_not_called()
    loop.run.assert_not_called()


def test_resume_cli_accepts_only_uuid_identity_and_returns_pending(monkeypatch, capsys):
    run, restart = uuid4(), uuid4()
    monkeypatch.setattr(
        cli,
        "_resume_director_run_owned",
        lambda r, s: {"run_id": str(r), "restart_id": str(s), "state": "recovery_pending"},
    )
    assert (
        cli.main(["director", "resume", "--run-id", str(run), "--restart-id", str(restart)]) == 75
    )
    assert json.loads(capsys.readouterr().out)["restart_id"] == str(restart)
    with pytest.raises(SystemExit):
        cli.main(["director", "resume", "--run-id", "bad", "--restart-id", str(restart)])


def test_expired_entrypoint_retains_budget_and_never_constructs_model_or_baseline(
    monkeypatch, tmp_path
):
    import argparse
    from datetime import UTC, datetime, timedelta

    from lab.director import artifacts, baseline_runner, holdout, task_plan
    from lab.director.budget import BudgetSnapshot, EpisodeReservation
    from lab.director.ownership import ExecutionOwner, owned_execution
    from lab.director.resume import ResumeReceipt

    run = uuid4()
    owner = ExecutionOwner(run, 2, "a" * 32, "b" * 64)
    now = datetime.now(UTC)
    receipt = ResumeReceipt(
        owner, uuid4(), now - timedelta(seconds=100), now - timedelta(seconds=40), True, "c" * 64
    )
    request = dict(
        suite="synthetic",
        suite_manifest_sha256="d" * 64,
        proposal_limit=1,
        provider="local-qwen",
        budget=dict(experiments=1, wall_seconds=60, model_tokens=10),
    )
    engine = MagicMock()
    query = engine.begin.return_value.__enter__.return_value.execute.return_value
    query.mappings.return_value.one_or_none.return_value = {
        "state": "running",
        "request_json": request,
    }
    monkeypatch.setattr(cli, "_director_engine", lambda: engine)
    monkeypatch.setattr(cli, "_planner_engine", lambda: MagicMock())
    monkeypatch.setattr(cli, "_private_runtime_directory", lambda path: path)
    active = MagicMock(side_effect=AssertionError("fresh admission forbidden"))
    monkeypatch.setattr(cli, "assert_execution_owner_transaction", active)
    closure = MagicMock()
    monkeypatch.setattr(cli, "assert_execution_owner_closure_transaction", closure)
    monkeypatch.setattr(
        cli,
        "load_suite_manifest",
        lambda *a, **kw: (SimpleNamespace(suite_id="synthetic", suite_version=1), (), "d" * 64),
    )
    model = MagicMock(side_effect=AssertionError("model construction forbidden"))
    monkeypatch.setattr(cli, "_local_qwen_provider", model)
    bootstrap = MagicMock(side_effect=AssertionError("baseline bootstrap forbidden"))
    monkeypatch.setattr(cli, "run_baseline_suite", bootstrap)
    monkeypatch.setattr(cli, "compute_harness_hash", lambda *_: SimpleNamespace(sha256="e" * 64))
    monkeypatch.setattr(cli, "LocalDockerRunner", MagicMock())
    lease = MagicMock()
    lease.read_checkpoint.return_value = {"payload": {"holdout_last_status": "passed"}}
    monkeypatch.setattr(
        cli,
        "DirectorRunLease",
        MagicMock(return_value=MagicMock(__enter__=lambda _: lease, __exit__=lambda *a: None)),
    )
    calibration = object()
    monkeypatch.setattr(artifacts, "read_registered_calibration", lambda *a, **kw: calibration)
    reserved = EpisodeReservation(uuid4(), 3, 0)

    def restore(engine, lease, run, budget, root):
        budget.restore(
            BudgetSnapshot(
                wall_seconds=22.0,
                model_tokens=4,
                reserved_wall_seconds=3.0,
                elapsed_wall_seconds=25.0,
                reservations=(reserved,),
            )
        )

    monkeypatch.setattr(baseline_runner, "_restore_latest_baseline_budget", restore)
    monkeypatch.setattr(holdout, "holdout_suite_is_registered", lambda *a, **kw: True)
    loop = MagicMock()
    constructor = MagicMock(return_value=loop)
    monkeypatch.setattr(cli, "DirectorLoop", constructor)
    result = SimpleNamespace(
        status="budget_exhausted",
        checkpoint_sha256="f" * 64,
        model_dump=lambda **kw: {"status": "budget_exhausted"},
    )

    def terminal(received, received_calibration):
        assert received is loop and received_calibration is calibration
        kwargs = constructor.call_args.kwargs
        assert isinstance(kwargs["provider"], cli._ClosureOnlyProvider)
        snapshot = kwargs["budget"].snapshot()
        assert snapshot.wall_seconds == 22.0 and snapshot.model_tokens == 4
        assert snapshot.reservations == (reserved,) and snapshot.elapsed_wall_seconds >= 100
        return result

    monkeypatch.setattr(cli, "_resume_terminal_loop", terminal)
    monkeypatch.setattr(
        task_plan, "read_terminal_holdout_checkpoints", lambda *a, **kw: ({}, {}, {})
    )
    seal = MagicMock()
    monkeypatch.setattr(task_plan, "seal_run_task_plan", seal)
    monkeypatch.setattr(task_plan, "terminal_finalization_seconds", lambda *a, **kw: 30)
    finalize = MagicMock(
        return_value=SimpleNamespace(exit_code=0, result={"state": "completed"}, unit="scorer")
    )
    monkeypatch.setattr(cli, "run_scorer_finalize_process", finalize)
    monkeypatch.setattr(cli, "calibration_sha256", lambda _: "0" * 64)
    args = argparse.Namespace(
        run_id=run,
        resume_receipt=receipt,
        seed_wall_seconds=30,
        artifact_root=tmp_path,
        suite_file=tmp_path / "suite",
        suite_id="synthetic",
        proposal_limit=1,
        provider="local-qwen",
        scenario_file=None,
    )
    with owned_execution(owner):
        response = cli._run_director(args)
    assert response["finalizer"]["state"] == "completed"
    model.assert_not_called()
    bootstrap.assert_not_called()
    active.assert_not_called()
    loop.run.assert_not_called()
    loop.check_run_end_holdout.assert_not_called()
    closure.assert_called_once()
    seal.assert_called_once()
    assert finalize.call_args.kwargs["artifact_root"] == tmp_path


@pytest.mark.parametrize("outcome", ["completed", "stop_requested", "exception_stop"])
def test_coordinator_validates_and_captures_before_claim_then_binds_execution(
    monkeypatch, tmp_path, outcome
):
    from datetime import UTC, datetime, timedelta

    from lab.director import resume
    from lab.director.ownership import ExecutionOwner, active_execution_owner
    from lab.director.resume import ResumeReceipt

    run, restart = uuid4(), uuid4()
    owner = ExecutionOwner(run, 2, "a" * 32, "b" * 64)
    now = datetime.now(UTC)
    receipt = ResumeReceipt(
        owner, restart, now - timedelta(seconds=20), now + timedelta(seconds=40), False, "c" * 64
    )
    events = []
    target = SimpleNamespace(
        purpose="research",
        contract=object(),
        entry=SimpleNamespace(suite_id="suite", provider="fake-json"),
        suite_path=tmp_path / "suite",
        scenario_path=tmp_path / "scenario",
        entry_sha256="d" * 64,
        proposal_limit=1,
        budget={"wall_seconds": 60},
    )
    engine = MagicMock()
    query = engine.connect.return_value.__enter__.return_value.execute.return_value
    query.mappings.return_value.one_or_none.return_value = {
        "state": "running",
        "payload_sha256": "e" * 64,
    }
    monkeypatch.setenv("LAB_SUITE_REGISTRY_FILE", "/private/registry")
    monkeypatch.setattr(cli, "load_suite_registry", lambda *a: MagicMock())
    monkeypatch.setattr(cli, "_director_engine", lambda: engine)
    monkeypatch.setattr(cli, "_planner_engine", lambda: MagicMock())
    monkeypatch.setattr(
        cli, "_validate_dispatch_target", lambda *a: events.append("validate") or target
    )
    observed = SimpleNamespace(
        payload_sha256="e" * 64,
        worker_pid=99,
        worker_start_ticks=100,
        worker_boot_id=str(uuid4()),
        worker_unit=f"swapp-ai-scientist-director-resume-{run.hex}-{restart.hex}.service",
        worker_invocation_id="a" * 32,
        worker_cgroup=f"/user.slice/swapp-ai-scientist-director-resume-{run.hex}-{restart.hex}.service",
    )
    monkeypatch.setattr(
        cli, "capture_current_owner", lambda *a: events.append("capture") or observed
    )
    monkeypatch.setattr(cli, "_private_runtime_directory", lambda path: path)
    monkeypatch.setattr(
        resume, "resume_execution", lambda *a, **kw: events.append("drain_CAS") or receipt
    )

    def execute(args):
        assert active_execution_owner() == owner and args.resume_receipt is receipt
        events.append("execute")
        if outcome == "exception_stop":
            raise RuntimeError("stop interrupted resumed execution")
        return {}

    def record_failure(*args, **kwargs):
        assert kwargs["owner"] is owner
        return {"state": "stop_requested"}

    monkeypatch.setattr(cli, "_run_director", execute)
    monkeypatch.setattr(cli, "_record_claimed_dispatch_failure", record_failure)
    monkeypatch.setattr(cli, "_completed_dispatch_result", lambda *a: {"state": outcome})
    response = cli._resume_director_run(run, restart)
    assert events == ["validate", "capture", "drain_CAS", "execute"]
    assert response["admitted_owner"] == {
        "generation": 2,
        "invocation_id": owner.invocation_id,
        "execution_sha256": owner.execution_sha256,
    }
    if outcome != "exception_stop":
        assert response["generation"] == 2
        assert response["original_deadline_at"] == receipt.deadline_at.isoformat()
    assert receipt.started_at == now - timedelta(seconds=20)
    assert receipt.deadline_at == now + timedelta(seconds=40)
    assert active_execution_owner() is None

    if outcome != "completed":
        child_result = {**response, "run_id": str(run)}
        monkeypatch.setattr(
            cli.subprocess,
            "run",
            lambda *a, **kw: CompletedProcess([], 1, stdout=json.dumps(child_result) + "\n"),
        )
        recovery = MagicMock(return_value={"state": "stopped"})
        monkeypatch.setattr(cli, "_automatic_stopped_baseline_recovery", recovery)
        launched = cli._launch_director_unit(run, restart_id=restart, unit_runtime=60)
        recovery.assert_called_once_with(run, response["admitted_owner"])
        assert launched["state"] == "stopped" and launched["owner_exit_code"] == 1
