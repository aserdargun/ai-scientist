from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from lab import cli
from lab.sandbox.docker_runner import (
    DEFAULT_SANDBOX_IMAGE,
    LocalDockerRunner,
    SandboxRunError,
    _process_start_time,
)
from lab.scorer import supervisor

_MIGRATION_FILE = (
    Path(__file__).parents[1] / "lab/db/migrations/versions/0024_baseline_operation.py"
)
_MIGRATION_SPEC = importlib.util.spec_from_file_location(
    "baseline_operation_migration", _MIGRATION_FILE
)
assert _MIGRATION_SPEC is not None and _MIGRATION_SPEC.loader is not None
migration = importlib.util.module_from_spec(_MIGRATION_SPEC)
_MIGRATION_SPEC.loader.exec_module(migration)


def test_stopped_sandbox_marker_is_bound_to_run_root_and_director_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = uuid4()
    work_root = tmp_path / str(run_id)
    work_root.mkdir(mode=0o700)
    admission_lock = tmp_path / "sandbox.lock"
    runner = LocalDockerRunner(
        image=DEFAULT_SANDBOX_IMAGE,
        work_root=work_root,
        admission_lock=admission_lock,
    )
    root_stat = runner.work_root.stat()
    owner_start = int(_process_start_time(os.getpid()))
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    # The sandbox phase UUID is intentionally independent from the Director run UUID.
    phase_id = uuid4().hex
    runner.admission_marker.write_text(
        json.dumps(
            {
                "schema": "sandbox-admission-intent.v1",
                "container_name": f"swapp-lab-fit-{phase_id}",
                "owner_pid": os.getpid(),
                "owner_start": str(owner_start),
                "boot_id": boot_id,
                "work_root": str(runner.work_root),
                "work_root_device": root_stat.st_dev,
                "work_root_inode": root_stat.st_ino,
            }
        ),
        encoding="ascii",
    )

    observed_deadlines: list[float | None] = []

    def drained(*, deadline: float | None = None) -> None:
        observed_deadlines.append(deadline)
        runner.admission_marker.unlink()

    monkeypatch.setattr(runner, "_reconcile_owned_containers", drained)
    runner.reconcile_stopped_run(
        run_id,
        owner_pid=os.getpid(),
        owner_start_ticks=owner_start,
        owner_boot_id=boot_id,
        deadline=time.monotonic() + 10,
    )
    assert not runner.admission_marker.exists()
    assert len(observed_deadlines) == 1 and observed_deadlines[0] is not None

    runner.admission_marker.write_text(
        json.dumps(
            {
                "schema": "sandbox-admission-intent.v1",
                "container_name": f"swapp-lab-fit-{phase_id}",
                "owner_pid": os.getpid(),
                "owner_start": str(owner_start),
                "boot_id": boot_id,
                "work_root": str(runner.work_root),
                "work_root_device": root_stat.st_dev,
                "work_root_inode": root_stat.st_ino,
            }
        ),
        encoding="ascii",
    )
    with pytest.raises(SandboxRunError, match="another run"):
        runner.reconcile_stopped_run(
            run_id,
            owner_pid=os.getpid() + 1,
            owner_start_ticks=owner_start,
            owner_boot_id=boot_id,
        )


def test_baseline_guards_keep_legacy_missing_purpose_as_research(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[str] = []
    monkeypatch.setattr(migration.op, "execute", statements.append)
    migration.upgrade()
    sql = "\n".join(statements)
    assert "intent->>'purpose' IS DISTINCT FROM 'baseline' THEN RETURN NEW" in sql
    assert sql.count("request_json->>'purpose' IS DISTINCT FROM 'baseline'") == 4
    assert "jsonb_typeof(p_budget) IS DISTINCT FROM 'object'" in sql
    assert "jsonb_typeof(p_budget->'reservations') IS DISTINCT FROM 'array'" in sql
    assert "p_budget->'reservations' IS DISTINCT FROM '[]'::jsonb" in sql
    assert "r.request_json->>'proposal_limit' IS DISTINCT FROM '0'" in sql
    assert "r.request_json->'budget'->>'experiments' IS DISTINCT FROM '0'" in sql
    assert "r.request_json->'budget'->>'model_tokens' IS DISTINCT FROM '0'" in sql


def test_scorer_unit_quiescence_requires_exact_generation_and_empty_cgroup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    unit = f"swapp-ai-scientist-scorer-{job_id.hex}.service"
    invocation_id = "a" * 32
    cgroup = f"/user.slice/swapp-ai-scientist-scorer.slice/{unit}"
    monkeypatch.setattr(supervisor, "_job_lifecycle_lock", lambda *_a, **_kw: nullcontext())
    monkeypatch.setattr(supervisor, "_expected_unit_cgroup", lambda _unit, **_kw: cgroup)
    monkeypatch.setattr(supervisor, "_cgroup_is_empty", lambda _group: True)
    monkeypatch.setattr(supervisor, "_cgroup_is_absent_or_empty", lambda _group: True)

    observed = {
        "LoadState": "not-found",
        "ActiveState": "inactive",
        "InvocationID": "",
        "ControlGroup": "",
        "MainPID": "0",
    }
    monkeypatch.setattr(supervisor, "_systemctl_show", lambda _unit, **_kw: observed.copy())
    assert supervisor.scorer_job_unit_is_quiescent(job_id, expected_invocation_id=None)
    observed["ControlGroup"] = cgroup
    assert not supervisor.scorer_job_unit_is_quiescent(job_id, expected_invocation_id=None)

    observed.update(
        {
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "InvocationID": "b" * 32,
            "ControlGroup": cgroup,
            "MainPID": "0",
        }
    )
    assert not supervisor.scorer_job_unit_is_quiescent(job_id, expected_invocation_id=invocation_id)
    observed["InvocationID"] = invocation_id
    monkeypatch.setattr(supervisor, "_cgroup_is_empty", lambda _group: False)
    assert not supervisor.scorer_job_unit_is_quiescent(job_id, expected_invocation_id=invocation_id)
    monkeypatch.setattr(supervisor, "_cgroup_is_empty", lambda _group: True)
    assert supervisor.scorer_job_unit_is_quiescent(job_id, expected_invocation_id=invocation_id)
    observed["ControlGroup"] = ""
    assert supervisor.scorer_job_unit_is_quiescent(job_id, expected_invocation_id=invocation_id)
    monkeypatch.setattr(supervisor, "_cgroup_is_absent_or_empty", lambda _group: False)
    assert not supervisor.scorer_job_unit_is_quiescent(job_id, expected_invocation_id=invocation_id)
    monkeypatch.setattr(supervisor, "_cgroup_is_absent_or_empty", lambda _group: True)
    observed["ControlGroup"] = cgroup
    observed["InvocationID"] = "c" * 32
    assert not supervisor.scorer_job_unit_is_quiescent(job_id, expected_invocation_id=invocation_id)


def test_expected_scorer_cgroup_uses_caller_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[float] = []

    def show(_unit: str, *, timeout_seconds: float = 5) -> dict[str, str]:
        seen.append(timeout_seconds)
        return {"LoadState": "loaded", "ControlGroup": "/user.slice/scorer.slice"}

    monkeypatch.setattr(supervisor, "_systemctl_show", show)
    unit = f"swapp-ai-scientist-scorer-{'a' * 32}.service"
    assert supervisor._expected_unit_cgroup(unit, timeout_seconds=0.25) == (
        f"/user.slice/scorer.slice/{unit}"
    )
    assert seen == [0.25]


def test_unstarted_stop_dispatch_requires_persisted_canonical_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    report = {
        "schema": "lab.baseline-report.v1",
        "run_id": str(run_id),
        "purpose": "baseline",
        "status": "stopped",
        "calibration_complete": False,
        "budget_verified": False,
        "request_sha256": "a" * 64,
        "suite_id": "fixture.suite",
        "suite_version": None,
        "suite_manifest_sha256": "b" * 64,
        "harness_sha256": "c" * 64,
        "image_sha256": "d" * 64,
        "task_plan_sha256": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
        "task_plan_count": 0,
        "calibration_sha256": None,
        "algorithms": ["robust_z", "iforest", "ecod_train_frozen"],
        "seeds": [0, 1, 2],
        "task_count": 0,
        "score_count": 0,
        "budget_receipt": {"status": "unverified_incomplete", "requested": {}},
        "model_usage": {"input_tokens": 0, "output_tokens": 0, "calls": 0},
        "baseline_records": [],
        "experiment_records": [],
        "task_terminal_outcomes": [],
    }
    digest = hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()

    dbrow = {
        "state": "stopped",
        "report_sha256": digest,
        "stored_sha256": digest,
        "report_json": report,
    }

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, *_args, **_kwargs):
            return SimpleNamespace(mappings=lambda: SimpleNamespace(one_or_none=lambda: dbrow))

    class Engine:
        def connect(self):
            return Connection()

        def dispose(self):
            return None

    monkeypatch.setattr(cli, "_director_engine", Engine)
    assert cli._verified_stopped_baseline_report(
        run_id,
        digest,
        0,
        {"state": "finalized", "research_status": "stopped"},
    )
    assert not cli._verified_stopped_baseline_report(
        run_id,
        "e" * 64,
        0,
        {"state": "finalized", "research_status": "stopped"},
    )
    assert not cli._verified_stopped_baseline_report(
        run_id,
        digest,
        1,
        {"state": "pending", "research_status": "pending"},
    )
    dbrow["state"] = "stop_requested"
    assert not cli._verified_stopped_baseline_report(
        run_id,
        digest,
        0,
        {"state": "finalized", "research_status": "stopped"},
    )
