"""Private score artifacts stay in the selected deployment; no services or live DB."""

import os
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from uuid import uuid4

import pytest
import sqlalchemy

from lab.director import baseline_operation
from lab.scorer import jobs, service, supervisor, worker


class ReachedLaunch(BaseException):
    """Intercept a launch before external effects or exception recovery."""


def test_stopped_baseline_keeps_private_root_when_recovering_running_scorer(monkeypatch, tmp_path):
    run_id, job_id = uuid4(), uuid4()
    request = {"purpose": "baseline"}
    owner = SimpleNamespace(
        payload_sha256="a" * 64,
        worker_pid=101,
        worker_start_ticks=55,
        worker_boot_id="fixture-boot",
        worker_unit="fixture.service",
        worker_invocation_id="b" * 32,
        worker_cgroup="/fixture",
    )
    identity = {
        **vars(owner),
        "owner_payload": owner.payload_sha256,
        "request_json": request,
        "stored_report_sha256": None,
        "state": "stop_requested",
    }
    director, planner = MagicMock(), MagicMock()
    director_rows = director.connect.return_value.__enter__.return_value.execute.return_value
    planner_rows = planner.connect.return_value.__enter__.return_value.execute.return_value
    director_rows.mappings.return_value.one_or_none.return_value = identity
    planner_rows.mappings.return_value.all.return_value = [
        {"job_id": job_id, "state": "running", "claim_invocation_id": "c" * 32}
    ]
    monkeypatch.setattr(baseline_operation, "capture_current_owner", lambda *a: owner)
    monkeypatch.setattr(baseline_operation.time, "monotonic", lambda: 100.0)
    launch = Mock(side_effect=ReachedLaunch)
    monkeypatch.setattr(baseline_operation, "run_scorer_recovery_process", launch)
    lease = MagicMock(run_id=run_id)
    with pytest.raises(ReachedLaunch):
        baseline_operation._finish_stopped_baseline(
            director,
            planner,
            run_id=run_id,
            request=request,
            payload_sha256=owner.payload_sha256,
            tasks=(),
            suite_id="fixture",
            suite_version=1,
            harness_sha256="d" * 64,
            image_sha256="e" * 64,
            lease=lease,
            runner=MagicMock(),
            artifact_root=tmp_path,
            deadline=130.0,
        )
    launch.assert_called_once_with(
        job_id,
        expected_claim_invocation_id="c" * 32,
        remaining_seconds=30,
        artifact_root=tmp_path,
    )


@pytest.fixture
def launch_boundary(monkeypatch, tmp_path):
    root = tmp_path / "data/runtime/private/blobs"
    root.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(supervisor, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("LAB_SCORER_DSN_FILE", raising=False)
    provision = Mock()
    launch = Mock(side_effect=ReachedLaunch)
    monkeypatch.setattr(supervisor, "_ensure_aggregate_slice", provision)
    monkeypatch.setattr(supervisor, "_job_lifecycle_lock", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(
        supervisor,
        "_systemctl_show",
        lambda *a: {
            "LoadState": "not-found",
            "ActiveState": "inactive",
            "MainPID": "0",
            "InvocationID": "",
            "ControlGroup": "",
        },
    )
    monkeypatch.setattr(supervisor, "_expected_unit_cgroup", lambda *a: "/fixture")
    monkeypatch.setattr(supervisor, "_cgroup_is_absent_or_empty", lambda *a: True)
    monkeypatch.setattr(supervisor.subprocess, "Popen", launch)
    monkeypatch.setattr(supervisor.subprocess, "run", launch)
    return root, provision, launch


def _launch(route, **kwargs):
    if route == "score":
        supervisor.run_scorer_process(uuid4(), remaining_seconds=37, **kwargs)
    elif route == "recovery":
        supervisor.run_scorer_recovery_process(
            uuid4(), expected_claim_invocation_id="b" * 32, remaining_seconds=37, **kwargs
        )
    else:
        supervisor.run_scorer_finalize_process(
            uuid4(),
            admitted_generation=7,
            execution_sha256="c" * 64,
            remaining_seconds=37,
            **kwargs,
        )


@pytest.mark.parametrize("route", ["score", "recovery", "finalize"])
def test_explicit_root_transported_alongside_private_ledger(route, launch_boundary, monkeypatch):
    root, _, launch = launch_boundary
    credential = root.parent / "scorer.dsn"
    credential.write_text("private-placeholder")
    credential.chmod(0o600)
    monkeypatch.setenv("LAB_SCORER_DSN_FILE", str(credential))
    with pytest.raises(ReachedLaunch):
        _launch(route, artifact_root=root)
    command = launch.call_args.args[0]
    assert command.count(f"--setenv=LAB_ARTIFACT_ROOT={root}") == 1
    assert command.count(f"--setenv=LAB_SCORER_DSN_FILE={credential}") == 1
    assert "private-placeholder" not in " ".join(command)
    if route == "recovery":
        assert command[command.index("--expected-claim-invocation-id") + 1] == "b" * 32


@pytest.mark.parametrize("route", ["score", "recovery", "finalize"])
def test_omitted_root_preserves_default(route, launch_boundary, monkeypatch):
    root, _, launch = launch_boundary
    monkeypatch.setenv("LAB_ARTIFACT_ROOT", str(root))
    with pytest.raises(ReachedLaunch):
        _launch(route)
    assert not any(
        arg.startswith("--setenv=LAB_ARTIFACT_ROOT=") for arg in launch.call_args.args[0]
    )


@pytest.mark.parametrize("route", ["score", "recovery", "finalize"])
@pytest.mark.parametrize("invalid", ["outside", "public", "symlink", "parent_alias", "uid", "file"])
def test_invalid_root_rejected_before_provision_or_start(
    route, invalid, launch_boundary, monkeypatch, tmp_path
):
    root, provision, launch = launch_boundary
    if invalid == "outside":
        root = tmp_path / "outside"
        root.mkdir(mode=0o700)
    elif invalid == "public":
        root.chmod(0o750)
    elif invalid == "symlink":
        alias = root.parent / "alias"
        alias.symlink_to(root, target_is_directory=True)
        root = alias
    elif invalid == "parent_alias":
        alias = root.parent.parent / "alias"
        alias.symlink_to(root.parent, target_is_directory=True)
        root = alias / root.name
    elif invalid == "uid":
        monkeypatch.setattr(supervisor.os, "getuid", lambda: root.stat().st_uid + 1)
    elif invalid == "file":
        root = root / "file"
        root.touch(mode=0o600)
    with pytest.raises(ValueError, match="artifact root"):
        _launch(route, artifact_root=root)
    provision.assert_not_called()
    launch.assert_not_called()


@pytest.mark.parametrize("route", ["score", "recovery"])
def test_worker_uses_selected_root_for_scoring_and_finalization(route, monkeypatch, tmp_path):
    job_id, run_id = uuid4(), uuid4()
    invocation = worker.ScorerInvocation(
        unit=f"swapp-ai-scientist-scorer-{job_id.hex}.service",
        invocation_id="a" * 32,
        control_group="/fixture",
    )
    fields = dict(
        job_id=job_id,
        run_id=run_id,
        experiment_id="baseline",
        evaluation_kind="baseline",
        task_id="task",
        seed=0,
        candidate_sha256="d" * 64,
        artifact_sha256="e" * 64,
        claim_token=uuid4(),
        admitted_generation=7,
        execution_sha256="c" * 64,
        attempt=1,
        state="running",
        run_state="stop_requested",
        claim_unit=invocation.unit,
        claim_invocation_id="b" * 32,
        claimed_by=uuid4(),
    )
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.return_value.scalar_one.return_value = True
    connection.execute.return_value.mappings.return_value.one_or_none.return_value = fields
    connection.execute.return_value.one_or_none.return_value = None
    monkeypatch.setattr(jobs, "claim_score_job", lambda *a, **kw: SimpleNamespace(**fields))
    read = Mock(return_value=b"private artifact")
    monkeypatch.setattr(jobs, "read_candidate_artifact", read)
    monkeypatch.setattr(
        "harness.fingerprint.compute_harness_hash", lambda *a: SimpleNamespace(sha256="f" * 64)
    )
    scorer = Mock()
    scorer.finalize_if_ready.return_value = None
    factory = Mock(return_value=scorer)
    monkeypatch.setattr(service, "IndependentScorer", factory)
    if route == "score":
        result = worker.process_one(
            engine, job_id=job_id, invocation=invocation, artifact_root=tmp_path
        )
        read.assert_called_once_with(fields["artifact_sha256"], artifact_root=tmp_path)
        assert result["state"] == "completed"
    else:
        result = worker.process_recover_stopped_job(
            engine,
            job_id=job_id,
            invocation=invocation,
            expected_claim_invocation_id="b" * 32,
            artifact_root=tmp_path,
        )
        assert result["state"] == "cancelled_after_drain"
    assert factory.call_args.kwargs["artifact_root"] == tmp_path
    scorer.finalize_if_ready.assert_called_once_with(
        run_id=run_id,
        admitted_generation=7,
        execution_sha256="c" * 64,
    )


@pytest.mark.parametrize("route", ["score", "recovery", "finalize"])
@pytest.mark.parametrize("explicit", [False, True])
def test_entrypoint_transports_root_without_changing_ledger(route, explicit, monkeypatch, tmp_path):
    job_id = uuid4()
    expected_root = tmp_path if explicit else worker.DEFAULT_ARTIFACT_ROOT
    if explicit:
        monkeypatch.setenv("LAB_ARTIFACT_ROOT", str(tmp_path))
    else:
        monkeypatch.delenv("LAB_ARTIFACT_ROOT", raising=False)
    monkeypatch.delenv("LAB_SCORER_DSN_FILE", raising=False)
    monkeypatch.setattr(worker, "verify_systemd_invocation", lambda *a: "verified fixture")
    descriptor = os.open(tmp_path / "admission", os.O_CREAT | os.O_RDWR, 0o600)
    monkeypatch.setattr(worker, "_try_process_admission_lock", lambda: descriptor)
    secret = Mock(return_value="sqlite:///selected-ledger")
    monkeypatch.setattr(worker, "_secret", secret)
    engine = Mock()
    factory = Mock(return_value=engine)
    monkeypatch.setattr(sqlalchemy, "create_engine", factory)
    dispatch = Mock(return_value={"state": "fixture"})
    if route == "score":
        monkeypatch.setattr(worker, "process_one", dispatch)
        args = ["--job-id", str(job_id)]
    elif route == "recovery":
        monkeypatch.setattr(worker, "process_recover_stopped_job", dispatch)
        args = ["--recover-job-id", str(job_id), "--expected-claim-invocation-id", "b" * 32]
    else:
        monkeypatch.setattr(worker, "process_finalize", dispatch)
        args = [
            "--finalize-run-id",
            str(job_id),
            "--finalize-admitted-generation",
            "7",
            "--finalize-execution-sha256",
            "c" * 64,
        ]
    assert worker.main(args) == 0
    assert dispatch.call_args.args == (engine,)
    assert dispatch.call_args.kwargs["artifact_root"] == expected_root
    assert dispatch.call_args.kwargs["invocation"] == "verified fixture"
    factory.assert_called_once_with(
        "sqlite:///selected-ledger",
        pool_size=2,
        max_overflow=0,
        pool_pre_ping=True,
    )
    engine.dispose.assert_called_once_with()
