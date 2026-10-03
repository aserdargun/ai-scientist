"""Bounded systemd admission for direct, one-run Director dispatches."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from lab import cli
from lab.llm.native_runtime import ModelRuntimeError


@pytest.fixture(autouse=True)
def slice_limits(monkeypatch):
    """Unit tests must not provision or inspect actual user services."""
    check = Mock()
    monkeypatch.setattr(cli.SystemdUnitManager, "ensure_slice", check)
    return check


class _Result:
    def __init__(self, value: object) -> None:
        self.value = value

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> object:
        return self.value


class _Connection:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row

    def execute(self, *_args: object, **_kwargs: object) -> _Result:
        return _Result(self.row)


class _Engine:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row
        self.disposed = False

    @contextmanager
    def connect(self):
        yield _Connection(self.row)

    def dispose(self) -> None:
        self.disposed = True


def _engine(*, state: str = "queued", wall_seconds: object = 60) -> _Engine:
    return _Engine(
        {
            "state": state,
            "request_json": {"budget": {"wall_seconds": wall_seconds}},
        }
    )


def test_direct_dispatch_executes_inline_only_in_trusted_owner_unit(monkeypatch) -> None:
    run_id = uuid4()
    expected = {"run_id": str(run_id), "state": "completed"}
    monkeypatch.setattr(cli, "is_current_dispatch_owner", lambda requested: requested == run_id)
    monkeypatch.setattr(
        cli,
        "_dispatch_director_run_with_global_slot",
        lambda requested: expected if requested == run_id else {},
    )

    assert cli._dispatch_director_run_owned(run_id) == expected


def test_direct_dispatch_launches_bounded_run_bound_service(monkeypatch, slice_limits) -> None:
    run_id = uuid4()
    engine = _engine()
    monkeypatch.setattr(cli, "is_current_dispatch_owner", lambda _requested: False)
    monkeypatch.setattr(cli, "_director_engine", lambda: engine)
    monkeypatch.setenv("LAB_SUITE_REGISTRY_FILE", "/trusted/registry.json")
    monkeypatch.setenv("SWAPP_LAB_GPU_UNIT", "swapp-lab-gpu.service")
    captured: list[str] = []

    def fake_run(command, **kwargs):
        slice_limits.assert_called_once_with()
        captured.extend(command)
        assert kwargs["timeout"] == 60 + cli.DIRECTOR_DISPATCH_OVERHEAD_SECONDS + 60
        return CompletedProcess(
            command,
            0,
            stdout=f'{{"run_id":"{run_id}","state":"completed"}}\n',
            stderr="",
        )

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    result = cli._dispatch_director_run_owned(run_id)

    assert result["run_id"] == str(run_id)
    assert result["owner_unit"] == f"swapp-ai-scientist-director-dispatch-{run_id.hex}.service"
    assert result["owner_exit_code"] == 0
    assert engine.disposed
    assert f"--unit=swapp-ai-scientist-director-dispatch-{run_id.hex}.service" in captured
    assert "--property=MemoryMax=2G" in captured
    assert "--property=MemorySwapMax=0" in captured
    assert "--property=CPUQuota=100%" in captured
    assert "--property=KillMode=control-group" in captured
    assert f"--property=RuntimeMaxSec={60 + cli.DIRECTOR_DISPATCH_OVERHEAD_SECONDS}" in captured
    assert "--setenv=SWAPP_LAB_GPU_UNIT=swapp-lab-gpu.service" in captured
    assert "--setenv=LAB_SUITE_REGISTRY_FILE=/trusted/registry.json" in captured
    assert "--setenv=PATH=" + os.environ.get("PATH", "") not in captured


@pytest.mark.parametrize("resume", [False, True])
def test_incompatible_slice_prevents_dispatch_and_resume_launch(monkeypatch, slice_limits, resume):
    slice_limits.side_effect = ModelRuntimeError(
        "shared GPU cgroup has an unexpected memory.max limit"
    )
    launch = Mock()
    monkeypatch.setattr(cli.subprocess, "run", launch)
    with pytest.raises(ModelRuntimeError, match="memory.max"):
        cli._launch_director_unit(uuid4(), unit_runtime=60, restart_id=uuid4() if resume else None)
    slice_limits.assert_called_once_with()
    launch.assert_not_called()


@pytest.mark.parametrize("invalid", ["run", "restart", "bool", "fraction", "zero", "large", "env"])
def test_invalid_launch_input_cannot_provision_slice(monkeypatch, slice_limits, invalid):
    run_id, restart_id, budget = uuid4(), None, 60
    if invalid == "run":
        run_id = str(run_id)
    elif invalid == "restart":
        restart_id = "not-a-uuid"
    elif invalid in {"bool", "fraction", "zero", "large"}:
        budget = {"bool": True, "fraction": 1.5, "zero": 0, "large": 999_999}[invalid]
    else:
        monkeypatch.setenv("LAB_SUITE_REGISTRY_FILE", "/private/registry\nextra")
    launch = Mock()
    monkeypatch.setattr(cli.subprocess, "run", launch)
    with pytest.raises((TypeError, ValueError)):
        cli._launch_director_unit(run_id, unit_runtime=budget, restart_id=restart_id)
    slice_limits.assert_not_called()
    launch.assert_not_called()


def test_direct_local_qwen_requires_matching_gpu_principal_before_launch(monkeypatch) -> None:
    run_id = uuid4()
    engine = _Engine(
        {
            "state": "queued",
            "request_json": {
                "provider": "local-qwen",
                "budget": {"wall_seconds": 60},
            },
        }
    )
    monkeypatch.setattr(cli, "is_current_dispatch_owner", lambda _requested: False)
    monkeypatch.setattr(cli, "_director_engine", lambda: engine)
    monkeypatch.setenv("SWAPP_LAB_GPU_UNIT", "swapp-ai-scientist-director-drain.service")
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("mismatched GPU principal must fail before launch"),
    )

    with pytest.raises(ValueError, match="SWAPP_LAB_GPU_UNIT"):
        cli._dispatch_director_run_owned(run_id)
    assert engine.disposed


@pytest.mark.parametrize(
    ("state", "wall_seconds"),
    (("running", 60), ("queued", True), ("queued", 0), ("queued", 14_401)),
)
def test_direct_dispatch_rejects_unbounded_or_nonqueued_request(
    monkeypatch, state: str, wall_seconds: object
) -> None:
    run_id = uuid4()
    monkeypatch.setattr(cli, "is_current_dispatch_owner", lambda _requested: False)
    monkeypatch.setattr(
        cli,
        "_director_engine",
        lambda: _engine(state=state, wall_seconds=wall_seconds),
    )
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("invalid request must not launch systemd"),
    )

    with pytest.raises(ValueError):
        cli._dispatch_director_run_owned(run_id)


def _private_dsn(path: Path) -> str:
    info = path.lstat()
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError("dispatch-owner review DSN must be a private regular file")
    return path.read_text(encoding="utf-8").strip()


@pytest.mark.skipif(
    os.environ.get("LAB_RECOVERY_PG_TEST") != "1",
    reason="requires the explicitly provisioned isolated recovery PostgreSQL database",
)
def test_production_dispatch_claim_persists_actual_run_bound_owner() -> None:
    """A queued run is claimed and bound inside the actual run-specific unit."""
    root = Path(__file__).resolve().parents[1]
    postgres = root / "data/runtime/postgres"
    migrator = create_engine(_private_dsn(postgres / "migrator.dsn"), pool_size=1, max_overflow=0)
    director = create_engine(_private_dsn(postgres / "director.dsn"), pool_size=2, max_overflow=0)
    run_id = uuid4()
    unit = f"swapp-ai-scientist-director-dispatch-{run_id.hex}.service"
    request = {
        "idempotency_key": f"dispatch-owner-review:{run_id.hex}",
        "track": "anomaly",
        "suite": "recovery.dispatch.review.v1",
        "budget": {"experiments": 1, "wall_seconds": 60, "model_tokens": 0},
        "program_version": "director.v1",
        "external_task_id": None,
        "external_run_id": None,
        "external_action_id": None,
        "suite_manifest_sha256": "a" * 64,
        "scenario_sha256": "b" * 64,
        "provider": "fake-json",
        "provider_config_sha256": None,
        "provider_registry_entry_sha256": "c" * 64,
        "proposal_limit": 1,
    }
    payload = json.dumps(
        {key: value for key, value in request.items() if key != "idempotency_key"},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    payload_sha = hashlib.sha256(payload).hexdigest()
    unit_started = False
    try:
        with migrator.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO lab.runs(run_id,origin,owner_id,idempotency_key,payload_sha256,"
                    "request_json,state,stop_requested) VALUES (:id,'local',:owner,:key,:sha,"
                    "CAST(:request AS jsonb),'queued',false)"
                ),
                {
                    "id": run_id,
                    "owner": f"dispatch-owner-review:{run_id.hex}",
                    "key": f"dispatch-owner-review:{run_id.hex}",
                    "sha": payload_sha,
                    "request": payload.decode("utf-8"),
                },
            )

        command = [
            "/usr/bin/systemd-run",
            "--user",
            "--wait",
            "--collect",
            "--quiet",
            "--pipe",
            f"--unit={unit}",
            "--slice=swapp-gpu.slice",
            "--property=MemoryMax=2G",
            "--property=MemorySwapMax=0",
            "--property=CPUQuota=100%",
            "--property=TasksMax=128",
            "--property=KillMode=control-group",
            "--property=RuntimeMaxSec=60",
            f"--working-directory={root}",
            f"--setenv=PYTHONPATH={root}",
            f"--setenv=LAB_DIRECTOR_DSN_FILE={postgres / 'director.dsn'}",
            f"--setenv=LAB_SUITE_REGISTRY_FILE={root / 'pyproject.toml'}",
            str(root / ".venv/bin/python"),
            str(root / "tests/director_dispatch_owner_review.py"),
            "--run-id",
            str(run_id),
        ]
        unit_started = True
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr[-2000:]
        lines = completed.stdout.strip().splitlines()
        assert lines, "the owned dispatcher child returned no JSON receipt"
        result = json.loads(lines[-1])
        assert result == {
            "dispatch": "failed",
            "error_type": "_IntentionalPreMeasurementFailure",
            "run_id": str(run_id),
            "state": "failed",
        }

        with director.connect() as connection:
            run_state, report_sha = connection.execute(
                text("SELECT state,report_sha256 FROM lab.runs WHERE run_id=:id"),
                {"id": run_id},
            ).one()
            owner = connection.execute(
                text(
                    "SELECT payload_sha256,worker_pid,worker_start_ticks,worker_boot_id,"
                    "worker_unit,worker_invocation_id,worker_cgroup "
                    "FROM lab.director_run_owners WHERE run_id=:id"
                ),
                {"id": run_id},
            ).one()
            events = (
                connection.execute(
                    text(
                        "SELECT event_type FROM lab.run_events WHERE run_id=:id "
                        "ORDER BY created_at,event_id"
                    ),
                    {"id": run_id},
                )
                .scalars()
                .all()
            )
            experiment_count = connection.execute(
                text("SELECT count(*) FROM lab.experiments WHERE run_id=:id"),
                {"id": run_id},
            ).scalar_one()
        assert run_state == "failed"
        assert report_sha is None
        assert owner.payload_sha256 == payload_sha
        assert owner.worker_unit == unit
        assert owner.worker_pid > 1 and owner.worker_start_ticks > 0
        assert len(owner.worker_boot_id) == 36
        assert len(owner.worker_invocation_id) == 32
        assert owner.worker_cgroup.endswith("/" + unit)
        assert events == ["run.started", "run.failed"]
        assert experiment_count == 0
    finally:
        # --wait guarantees the unit has exited before returning; this fallback
        # only stops this unique review unit if the systemd client itself failed.
        if unit_started:
            subprocess.run(
                ["/usr/bin/systemctl", "--user", "stop", unit],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        with migrator.begin() as connection:
            connection.execute(text("DELETE FROM lab.runs WHERE run_id=:id"), {"id": run_id})
        import shutil

        shutil.rmtree(root / "data/runtime/director-artifacts" / str(run_id), ignore_errors=True)
        director.dispose()
        migrator.dispose()
