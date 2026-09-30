"""Owned-UUID PostgreSQL recovery receipt probe (explicit opt-in only)."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from lab.director.artifacts import store_director_artifact
from lab.director.contracts import (
    ExperimentDocument,
    SourceProvenance,
    TrajectoryDocument,
)
from lab.director.ledger import (
    canonical_json_bytes,
    commit_experiment_record,
    register_experiment,
    transition_experiment,
)
from lab.director.recovery import (
    _recovery_id,
    _request_sha256,
    apply_stop_and_finalize,
    inspect_recovery,
)
from lab.director.task_plan import (
    RunTaskAssignment,
    plan_run_tasks,
    record_planner_terminal_outcome,
)
from lab.scorer.supervisor import SYSTEMD_RUN


def _dsn(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise RuntimeError("recovery PG DSN must be a private regular file")
    return path.read_text(encoding="utf-8").strip()


def _create_crashed_candidate_run(
    *, migrator, director, planner, run_id: UUID
) -> tuple[str, Path, str]:
    """Build one real terminal candidate crash and matching task resolution."""
    payload = b'{"provider":"recovery-fixture","budget":{"experiments":1}}'
    payload_sha = hashlib.sha256(payload).hexdigest()
    owner_key = f"recovery-finalize-review:{run_id.hex}"
    with migrator.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO lab.runs(run_id,origin,owner_id,idempotency_key,payload_sha256,"
                "request_json,state,stop_requested) VALUES (:id,'local',:owner,:key,:sha,"
                "CAST(:request AS jsonb),'running',false)"
            ),
            {
                "id": run_id,
                "owner": owner_key,
                "key": owner_key,
                "sha": payload_sha,
                "request": payload.decode(),
            },
        )
    dataset_id, split_id, session_id = (
        f"recovery-{run_id.hex}",
        "dev",
        "session-1",
    )
    with migrator.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO scorer.dataset_profiles(dataset_id,split_id,session_id,sample_count,"
                "sliding_window,profile_sha256,visibility) "
                "VALUES (:dataset,:split,:session,4,2,:sha,'dev')"
            ),
            {
                "dataset": dataset_id,
                "split": split_id,
                "session": session_id,
                "sha": hashlib.sha256(dataset_id.encode()).hexdigest(),
            },
        )
    experiment_id = f"exp_{uuid4().hex}"
    candidate_source = "def score_candidate(values):\n    return [0.0] * len(values)\n"
    candidate_bytes = candidate_source.encode()
    candidate_sha = hashlib.sha256(candidate_bytes).hexdigest()
    artifact_root = Path("data/runtime/director-artifacts") / str(run_id)
    candidate_blob = store_director_artifact(candidate_bytes, artifact_root=artifact_root)
    input_sha = hashlib.sha256(b"recovery review task metadata").hexdigest()
    proposal = {"baseline_name": "robust_z", "fixture": "terminal candidate crash"}
    register_experiment(
        director,
        experiment_id=experiment_id,
        run_id=str(run_id),
        sequence=0,
        experiment_number=None,
        kind="baseline",
        baseline_name="robust_z",
        parent_experiment_id=None,
        candidate_sha256=candidate_sha,
        candidate_blob_sha256=candidate_blob,
        inputs_sha256=input_sha,
        move_type="detector",
        system="S1",
        hypothesis="Synthetic recovery fixture: candidate crash before scoring.",
        predicted_delta=None,
        proposal=proposal,
    )
    transition_experiment(director, experiment_id=experiment_id, status="primary_running")
    experiment = ExperimentDocument(
        schema="experiment.v1",
        experiment_id=experiment_id,
        run_id=run_id,
        ordinal=1,
        kind="baseline",
        experiment_number=None,
        baseline_name="robust_z",
        calibration_sha256=None,
        agent_version="recovery-review.v1",
        parent_experiment_id=None,
        candidate_sha256=candidate_sha,
        candidate_blob_sha256=candidate_blob,
        move_type="detector",
        system="S1",
        hypothesis="Synthetic recovery fixture: candidate crash before scoring.",
        predicted_delta=None,
        inputs_sha256=input_sha,
        parent_tree="a" * 40,
        child_tree=None,
        harness_sha256="b" * 64,
        image_sha256="d" * 64,
        suite_id="recovery-review-suite",
        suite_version=1,
        per_task=(),
        suite_score=None,
        guards={"hardcoding": "not_run", "determinism": "not_run", "causality": "not_run"},
        decision=None,
        status="crashed",
        fit_seconds=0.0,
        score_seconds=0.0,
        llm_input_tokens=0,
        llm_output_tokens=0,
        wall_seconds=0.1,
    )
    provenance = SourceProvenance(
        dataset_id=dataset_id,
        split_id=split_id,
        session_id=session_id,
        source_manifest_sha256="e" * 64,
        source_revision="recovery-fixture.v1",
        license_id="CC0-1.0",
        attribution="Synthetic private recovery test fixture",
        access_terms="No external data",
        usage_profile="noncommercial_research",
    )
    messages = canonical_json_bytes({"schema": "recovery-review.messages.v1", "messages": []})
    messages_blob = store_director_artifact(messages, artifact_root=artifact_root)
    trajectory = TrajectoryDocument(
        schema="trajectory.v1",
        trajectory_id=f"trj_{uuid4().hex}",
        run_id=run_id,
        experiment_id=experiment_id,
        kind="baseline",
        experiment_number=None,
        baseline_name="robust_z",
        calibration_sha256=None,
        agent_version="recovery-review.v1",
        model_id="recovery-fixture.v1",
        usage_profile="noncommercial_research",
        source_provenance=(provenance,),
        quantization="fixture",
        adapter="recovery-review",
        system="S1",
        thinking=False,
        temperature=0.7,
        top_p=0.8,
        context_template="recovery-review.v1",
        inputs_sha256=input_sha,
        messages_blob_sha256=messages_blob,
        tool_calls=0,
        outcome=None,
        quality_tier="bronze",
        secrets_scrubbed=True,
        people_scrubbed=True,
        raw_values_scrubbed=True,
        exclusions=("private recovery test fixture",),
    )
    experiment_bytes = canonical_json_bytes(experiment)
    trajectory_bytes = canonical_json_bytes(trajectory)
    experiment_blob = store_director_artifact(experiment_bytes, artifact_root=artifact_root)
    trajectory_blob = store_director_artifact(trajectory_bytes, artifact_root=artifact_root)
    plan_run_tasks(
        planner,
        run_id=run_id,
        assignments=(
            RunTaskAssignment(
                experiment_id=experiment_id,
                evaluation_kind="baseline",
                task_id="task-1",
                seed=0,
                candidate_sha256=candidate_sha,
                dataset_id=dataset_id,
                split_id=split_id,
                session_id=session_id,
            ),
        ),
    )
    record_planner_terminal_outcome(
        planner,
        run_id=run_id,
        experiment_id=experiment_id,
        evaluation_kind="baseline",
        task_id="task-1",
        seed=0,
        candidate_sha256=candidate_sha,
        outcome_code="candidate_crash",
    )
    commit_experiment_record(
        director,
        experiment=experiment,
        trajectory=trajectory,
        experiment_blob_sha256=experiment_blob,
        trajectory_blob_sha256=trajectory_blob,
    )
    return payload_sha, artifact_root, dataset_id


def _launch_owner_helper(
    root: Path,
    run_id: UUID,
    payload_sha: str,
    *,
    wait: bool,
    child_seconds: float = 0.0,
    parent_seconds: float = 0.0,
    exit_type_cgroup: bool = False,
) -> None:
    command = [
        SYSTEMD_RUN,
        "--user",
        "--collect",
        "--quiet",
        f"--unit=swapp-ai-scientist-director-dispatch-{run_id.hex}.service",
        "--slice=swapp-gpu.slice",
        "--property=MemoryMax=2G",
        "--property=MemorySwapMax=0",
        "--property=CPUQuota=100%",
        "--property=TasksMax=128",
        "--property=RuntimeMaxSec=30",
        f"--working-directory={root}",
        f"--setenv=PYTHONPATH={root}",
        str(root / ".venv/bin/python"),
        str(root / "tests/director_recovery_owner.py"),
        "--run-id",
        str(run_id),
        "--payload-sha256",
        payload_sha,
    ]
    if child_seconds:
        command.extend(("--child-seconds", str(child_seconds)))
    if parent_seconds:
        command.extend(("--parent-seconds", str(parent_seconds)))
    if exit_type_cgroup:
        command.insert(command.index("--property=RuntimeMaxSec=30"), "--property=ExitType=cgroup")
    if wait:
        command[2:2] = ["--wait", "--pipe"]
    else:
        command.insert(2, "--no-block")
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=45 if wait else 10,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-1000:]


def _wait_for_owner(director, run_id: UUID, timeout: float = 5.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with director.connect() as connection:
            row = (
                connection.execute(
                    text("SELECT * FROM lab.director_run_owners WHERE run_id=:id"),
                    {"id": run_id},
                )
                .mappings()
                .one_or_none()
            )
        if row is not None:
            return dict(row)
        time.sleep(0.05)
    raise AssertionError("run-bound owner helper did not persist its identity")


def _stop_owned_unit(unit: str) -> None:
    result = subprocess.run(
        ["/usr/bin/systemctl", "--user", "stop", unit],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-1000:]


def _wait_unit_inactive(unit: str, timeout: float = 10.0) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        properties = _systemd_properties(unit)
        if properties.get("LoadState") == "not-found" or properties.get("ActiveState") in {
            "inactive",
            "failed",
        }:
            return properties
        time.sleep(0.05)
    raise AssertionError(f"owned unit {unit} did not become inactive")


def _cgroup_pids(control_group: str) -> set[int]:
    path = Path("/sys/fs/cgroup") / control_group.lstrip("/")
    if not path.exists():
        return set()
    pids: set[int] = set()
    for directory, _, _ in os.walk(path):
        procs = Path(directory) / "cgroup.procs"
        if procs.exists():
            pids.update(int(value) for value in procs.read_text().split() if value)
    return pids


def _systemd_properties(unit: str) -> dict[str, str]:
    result = subprocess.run(
        [
            "/usr/bin/systemctl",
            "--user",
            "show",
            "--no-pager",
            "--property=LoadState",
            "--property=ActiveState",
            "--property=InvocationID",
            "--property=ControlGroup",
            "--property=MainPID",
            unit,
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-1000:]
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def _cleanup_recovery_fixture(
    migrator, run_id: UUID, dataset_id: str, artifact_root: Path
) -> None:
    with migrator.begin() as connection:
        connection.execute(text("DELETE FROM lab.runs WHERE run_id=:id"), {"id": run_id})
        connection.execute(
            text("DELETE FROM scorer.dataset_profiles WHERE dataset_id=:dataset"),
            {"dataset": dataset_id},
        )
    import shutil

    shutil.rmtree(artifact_root, ignore_errors=True)


@pytest.mark.skipif(
    os.environ.get("LAB_RECOVERY_PG_TEST") != "1",
    reason="requires explicit opt-in to the isolated recovery review database",
)
def test_missing_dispatcher_proof_is_pending_and_idempotent() -> None:
    """Missing old-generation proof never edits the run or invents task records."""
    root = Path(__file__).resolve().parents[1]
    postgres = root / "data/runtime/postgres"
    migrator = create_engine(_dsn(postgres / "migrator.dsn"), pool_size=1, max_overflow=0)
    director = create_engine(_dsn(postgres / "director.dsn"), pool_size=2, max_overflow=0)
    planner = create_engine(_dsn(postgres / "planner.dsn"), pool_size=1, max_overflow=0)
    run_id = uuid4()
    encoded = b'{"budget":{"experiments":1},"provider":"fake-json"}'
    payload_sha = hashlib.sha256(encoded).hexdigest()
    recovery_sha = _request_sha256(run_id, "stop_and_finalize")
    recovery_id = _recovery_id(run_id, recovery_sha)
    try:
        with migrator.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO lab.runs(run_id,origin,owner_id,idempotency_key,payload_sha256,"
                    "request_json,state,stop_requested) VALUES (:id,'local',:owner,:key,:sha,"
                    "CAST(:request AS jsonb),'running',false)"
                ),
                {
                    "id": run_id,
                    "owner": f"recovery-review:{run_id.hex}",
                    "key": f"recovery-review:{run_id.hex}",
                    "sha": payload_sha,
                    "request": encoded.decode("utf-8"),
                },
            )
        first = apply_stop_and_finalize(director, planner, run_id=run_id, recovery_id=recovery_id)
        retry = apply_stop_and_finalize(director, planner, run_id=run_id, recovery_id=recovery_id)
        with director.connect() as connection:
            state = connection.execute(
                text("SELECT state FROM lab.runs WHERE run_id=:id"), {"id": run_id}
            ).scalar_one()
            receipt = (
                connection.execute(
                    text(
                        "SELECT state,request_sha256,result_json,result_sha256,owner_pid "
                        "FROM lab.director_recoveries WHERE recovery_id=:id"
                    ),
                    {"id": recovery_id},
                )
                .mappings()
                .one()
            )
        assert first["recovery_state"] == "pending"
        assert first["reason"] == "owner_proof_missing"
        assert retry["recovery_state"] == "pending"
        assert state == "running"
        assert receipt["state"] == "pending"
        assert receipt["request_sha256"] == recovery_sha
        assert receipt["owner_pid"] is None
        assert not receipt["result_json"].get("report_sha256")
        with pytest.raises(ValueError, match="does not match"):
            apply_stop_and_finalize(director, planner, run_id=run_id, recovery_id=uuid4())
    finally:
        with migrator.begin() as connection:
            connection.execute(text("DELETE FROM lab.runs WHERE run_id=:id"), {"id": run_id})
        planner.dispose()
        director.dispose()
        migrator.dispose()


@pytest.mark.skipif(
    os.environ.get("LAB_RECOVERY_PG_TEST") != "1",
    reason="requires explicit opt-in to the isolated recovery review database",
)
def test_recovery_identity_and_result_checks_reject_partial_nulls() -> None:
    """PostgreSQL CHECK constraints must reject UNKNOWN from nullable comparisons."""
    root = Path(__file__).resolve().parents[1]
    postgres = root / "data/runtime/postgres"
    migrator = create_engine(_dsn(postgres / "migrator.dsn"), pool_size=1, max_overflow=0)
    director = create_engine(_dsn(postgres / "director.dsn"), pool_size=2, max_overflow=0)
    run_id = uuid4()
    payload = b'{"budget":{"experiments":1},"provider":"fake-json"}'
    payload_sha = hashlib.sha256(payload).hexdigest()
    try:
        with migrator.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO lab.runs(run_id,origin,owner_id,idempotency_key,payload_sha256,"
                    "request_json,state,stop_requested) VALUES (:id,'local',:owner,:key,:sha,"
                    "CAST(:request AS jsonb),'running',false)"
                ),
                {
                    "id": run_id,
                    "owner": f"recovery-check-review:{run_id.hex}",
                    "key": f"recovery-check-review:{run_id.hex}",
                    "sha": payload_sha,
                    "request": payload.decode(),
                },
            )

        with pytest.raises(DBAPIError):
            with director.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO lab.director_recoveries(recovery_id,run_id,request_sha256,"
                        "action,observed_run_state,owner_pid,state) VALUES (:recovery,:run_id,:sha,"
                        "'stop_and_finalize','running',1234,'started')"
                    ),
                    {
                        "recovery": uuid4(),
                        "run_id": run_id,
                        "sha": "a" * 64,
                    },
                )

        with pytest.raises(DBAPIError):
            with director.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO lab.director_recoveries(recovery_id,run_id,request_sha256,"
                        "action,observed_run_state,result_json,state) "
                        "VALUES (:recovery,:run_id,:sha,"
                        "'stop_and_finalize','running',CAST('{}' AS jsonb),'started')"
                    ),
                    {"recovery": uuid4(), "run_id": run_id, "sha": "b" * 64},
                )
    finally:
        with migrator.begin() as connection:
            connection.execute(text("DELETE FROM lab.runs WHERE run_id=:id"), {"id": run_id})
        director.dispose()
        migrator.dispose()


@pytest.mark.skipif(
    os.environ.get("LAB_RECOVERY_PG_TEST") != "1",
    reason="requires explicit opt-in to the isolated recovery review database",
)
def test_actual_systemd_owner_receipt_is_immutable_and_active_owner_stays_pending() -> None:
    """A live exact owner generation remains pending and its receipt is immutable."""
    root = Path(__file__).resolve().parents[1]
    postgres = root / "data/runtime/postgres"
    migrator = create_engine(_dsn(postgres / "migrator.dsn"), pool_size=1, max_overflow=0)
    director = create_engine(_dsn(postgres / "director.dsn"), pool_size=2, max_overflow=0)
    planner = create_engine(_dsn(postgres / "planner.dsn"), pool_size=1, max_overflow=0)
    run_id = uuid4()
    unit = f"swapp-ai-scientist-director-dispatch-{run_id.hex}.service"
    dataset_id = f"recovery-{run_id.hex}"
    artifact_root = Path("data/runtime/director-artifacts") / str(run_id)
    payload_sha = ""
    request_sha = _request_sha256(run_id, "stop_and_finalize")
    recovery_id = _recovery_id(run_id, request_sha)
    try:
        payload_sha, artifact_root, dataset_id = _create_crashed_candidate_run(
            migrator=migrator, director=director, planner=planner, run_id=run_id
        )
        _launch_owner_helper(root, run_id, payload_sha, wait=False, parent_seconds=20)
        owner_row = _wait_for_owner(director, run_id)
        pending = apply_stop_and_finalize(
            director, planner, run_id=run_id, recovery_id=recovery_id
        )
        assert owner_row["worker_unit"] == unit
        assert pending["recovery_state"] == "pending"
        assert pending["reason"] == "owner_generation_active"
        assert pending["owner_proven_dead"] is False
        with pytest.raises(DBAPIError):
            with director.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE lab.director_run_owners SET worker_pid=worker_pid "
                        "WHERE run_id=:id"
                    ),
                    {"id": run_id},
                )
    finally:
        if _systemd_properties(unit).get("LoadState") != "not-found":
            _stop_owned_unit(unit)
            _wait_unit_inactive(unit)
        _cleanup_recovery_fixture(migrator, run_id, dataset_id, artifact_root)
        planner.dispose()
        director.dispose()
        migrator.dispose()


@pytest.mark.skipif(
    os.environ.get("LAB_RECOVERY_PG_TEST") != "1",
    reason="requires explicit opt-in to the isolated recovery review database",
)
def test_same_name_replacement_generation_stays_pending_and_is_not_touched() -> None:
    """A new active unit with the old name cannot be killed as if it were the owner."""
    root = Path(__file__).resolve().parents[1]
    postgres = root / "data/runtime/postgres"
    migrator = create_engine(_dsn(postgres / "migrator.dsn"), pool_size=1, max_overflow=0)
    director = create_engine(_dsn(postgres / "director.dsn"), pool_size=2, max_overflow=0)
    planner = create_engine(_dsn(postgres / "planner.dsn"), pool_size=1, max_overflow=0)
    run_id = uuid4()
    unit = f"swapp-ai-scientist-director-dispatch-{run_id.hex}.service"
    dataset_id = f"recovery-{run_id.hex}"
    artifact_root = Path("data/runtime/director-artifacts") / str(run_id)
    replacement_unit_started = False
    try:
        payload_sha, artifact_root, dataset_id = _create_crashed_candidate_run(
            migrator=migrator, director=director, planner=planner, run_id=run_id
        )
        _launch_owner_helper(root, run_id, payload_sha, wait=False, parent_seconds=20)
        old_owner = _wait_for_owner(director, run_id)
        old_properties = _systemd_properties(unit)
        assert old_properties.get("InvocationID") == old_owner["worker_invocation_id"]
        _stop_owned_unit(unit)
        _wait_unit_inactive(unit)
        old_cgroup = str(old_owner["worker_cgroup"])
        deadline = time.monotonic() + 5
        while _cgroup_pids(old_cgroup) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _cgroup_pids(old_cgroup)

        replacement = subprocess.run(
            [
                SYSTEMD_RUN,
                "--user",
                "--no-block",
                "--collect",
                "--quiet",
                f"--unit={unit}",
                "--slice=swapp-gpu.slice",
                "--property=MemoryMax=128M",
                "--property=MemorySwapMax=0",
                "--property=CPUQuota=50%",
                "--property=TasksMax=16",
                "--property=RuntimeMaxSec=20",
                "/usr/bin/sleep",
                "15",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert replacement.returncode == 0, replacement.stderr[-1000:]
        replacement_unit_started = True
        props = _systemd_properties(unit)
        assert props.get("ActiveState") == "active"
        assert props.get("InvocationID") != old_owner["worker_invocation_id"]
        replacement_pid = int(props["MainPID"])
        result = apply_stop_and_finalize(
            director,
            planner,
            run_id=run_id,
            recovery_id=_recovery_id(run_id, _request_sha256(run_id, "stop_and_finalize")),
        )
        after = _systemd_properties(unit)
        assert result["recovery_state"] == "pending"
        assert result["reason"] in {
            "owner_generation_active",
            "owner_generation_unverified",
        }
        assert after.get("ActiveState") == "active"
        assert after.get("InvocationID") == props.get("InvocationID")
        assert after.get("MainPID") == str(replacement_pid)
        with director.connect() as connection:
            state = connection.execute(
                text("SELECT state FROM lab.runs WHERE run_id=:id"), {"id": run_id}
            ).scalar_one()
        assert state == "running"
    finally:
        if replacement_unit_started and _systemd_properties(unit).get("LoadState") != "not-found":
            _stop_owned_unit(unit)
            _wait_unit_inactive(unit)
        _cleanup_recovery_fixture(migrator, run_id, dataset_id, artifact_root)
        planner.dispose()
        director.dispose()
        migrator.dispose()


@pytest.mark.skipif(
    os.environ.get("LAB_RECOVERY_PG_TEST") != "1",
    reason="requires explicit opt-in to the isolated recovery review database",
)
def test_surviving_owner_descendant_blocks_finalization_until_exact_unit_drains() -> None:
    """A dead dispatcher with a live child in its old cgroup is not a dead owner."""
    root = Path(__file__).resolve().parents[1]
    postgres = root / "data/runtime/postgres"
    migrator = create_engine(_dsn(postgres / "migrator.dsn"), pool_size=1, max_overflow=0)
    director = create_engine(_dsn(postgres / "director.dsn"), pool_size=2, max_overflow=0)
    planner = create_engine(_dsn(postgres / "planner.dsn"), pool_size=1, max_overflow=0)
    run_id = uuid4()
    unit = f"swapp-ai-scientist-director-dispatch-{run_id.hex}.service"
    dataset_id = f"recovery-{run_id.hex}"
    artifact_root = Path("data/runtime/director-artifacts") / str(run_id)
    unit_started = False
    try:
        payload_sha, artifact_root, dataset_id = _create_crashed_candidate_run(
            migrator=migrator, director=director, planner=planner, run_id=run_id
        )
        _launch_owner_helper(
            root,
            run_id,
            payload_sha,
            wait=False,
            child_seconds=15,
            parent_seconds=0.1,
            exit_type_cgroup=True,
        )
        unit_started = True
        owner = _wait_for_owner(director, run_id)
        cgroup = str(owner["worker_cgroup"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            props = _systemd_properties(unit)
            if props.get("MainPID") == "0" and _cgroup_pids(cgroup):
                break
            time.sleep(0.05)
        assert props.get("MainPID") == "0"
        descendants = _cgroup_pids(cgroup)
        assert descendants
        assert int(owner["worker_pid"]) not in descendants

        result = apply_stop_and_finalize(
            director,
            planner,
            run_id=run_id,
            recovery_id=_recovery_id(run_id, _request_sha256(run_id, "stop_and_finalize")),
        )
        assert result["recovery_state"] == "pending"
        assert result["reason"] == "owner_generation_unverified"
        assert _cgroup_pids(cgroup) == descendants
        with director.connect() as connection:
            state = connection.execute(
                text("SELECT state FROM lab.runs WHERE run_id=:id"), {"id": run_id}
            ).scalar_one()
        assert state == "running"
    finally:
        if unit_started and _systemd_properties(unit).get("LoadState") != "not-found":
            _stop_owned_unit(unit)
            _wait_unit_inactive(unit)
        _cleanup_recovery_fixture(migrator, run_id, dataset_id, artifact_root)
        planner.dispose()
        director.dispose()
        migrator.dispose()


@pytest.mark.skipif(
    os.environ.get("LAB_RECOVERY_PG_TEST") != "1",
    reason="requires explicit opt-in to the isolated recovery review database",
)
def test_proven_dead_owner_finalizes_verified_candidate_crash() -> None:
    """A real transient dispatch identity can be drained and safely finalized by Scorer."""
    root = Path(__file__).resolve().parents[1]
    postgres = root / "data/runtime/postgres"
    migrator = create_engine(_dsn(postgres / "migrator.dsn"), pool_size=1, max_overflow=0)
    director = create_engine(_dsn(postgres / "director.dsn"), pool_size=2, max_overflow=0)
    planner = create_engine(_dsn(postgres / "planner.dsn"), pool_size=1, max_overflow=0)
    run_id = uuid4()
    dataset_id = f"recovery-{run_id.hex}"
    artifact_root = Path("data/runtime/director-artifacts") / str(run_id)
    owner_unit = f"swapp-ai-scientist-director-dispatch-{run_id.hex}.service"
    try:
        payload_sha, artifact_root, dataset_id = _create_crashed_candidate_run(
            migrator=migrator, director=director, planner=planner, run_id=run_id
        )
        launched = subprocess.run(
            [
                SYSTEMD_RUN,
                "--user",
                "--wait",
                "--collect",
                "--quiet",
                "--pipe",
                f"--unit={owner_unit}",
                "--slice=swapp-gpu.slice",
                "--property=MemoryMax=2G",
                "--property=MemorySwapMax=0",
                "--property=CPUQuota=100%",
                "--property=TasksMax=128",
                "--property=RuntimeMaxSec=30",
                f"--working-directory={root}",
                f"--setenv=PYTHONPATH={root}",
                str(root / ".venv/bin/python"),
                str(root / "tests/director_recovery_owner.py"),
                "--run-id",
                str(run_id),
                "--payload-sha256",
                payload_sha,
            ],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        assert launched.returncode == 0, launched.stderr[-1000:]
        inspected = inspect_recovery(director, planner, run_id)
        assert inspected["owner_proven_dead"] is True
        assert inspected["can_finalize"] is True
        assert inspected["recovery_blockers"] == []

        recovery_id = UUID(inspected["recovery_id"])
        result = apply_stop_and_finalize(
            director,
            planner,
            run_id=run_id,
            recovery_id=recovery_id,
            remaining_seconds=120,
        )
        assert result["recovery_state"] == "completed"
        assert result["action"] == "finalized_stopped"
        assert result["finalizer_exit_code"] == 0
        assert result["report_sha256"]
        retry = apply_stop_and_finalize(
            director,
            planner,
            run_id=run_id,
            recovery_id=recovery_id,
            remaining_seconds=120,
        )
        assert retry["replayed"] is True
        assert retry["report_sha256"] == result["report_sha256"]
        with director.connect() as connection:
            state, report_sha = connection.execute(
                text("SELECT state,report_sha256 FROM lab.runs WHERE run_id=:id"),
                {"id": run_id},
            ).one()
        assert state == "stopped"
        assert report_sha == result["report_sha256"]
    finally:
        with migrator.begin() as connection:
            connection.execute(text("DELETE FROM lab.runs WHERE run_id=:id"), {"id": run_id})
            connection.execute(
                text("DELETE FROM scorer.dataset_profiles WHERE dataset_id=:dataset"),
                {"dataset": dataset_id},
            )
        import shutil

        shutil.rmtree(artifact_root, ignore_errors=True)
        planner.dispose()
        director.dispose()
        migrator.dispose()
