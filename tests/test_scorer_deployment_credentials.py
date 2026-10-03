"""Deployment ledger selection across real launch and Scorer entrypoint paths.

Services and model execution are intercepted; these tests do not establish real
model acceptance. The ledger regression uses two independent local databases.
"""

from __future__ import annotations

import os
import stat
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
import sqlalchemy
from sqlalchemy import create_engine, text

from lab import cli
from lab.scorer import (
    care_calibration_supervisor,
    care_calibration_worker,
    credential_path,
    holdout_recovery,
    holdout_supervisor,
    holdout_worker,
    mode_snapshot_worker,
    resume_recovery,
    stop_recovery,
    supervisor,
    worker,
)

LAUNCH_ROUTES = (
    "score",
    "score_recovery",
    "finalize",
    "empty_finalize",
    "mode_install",
    "holdout",
    "holdout_recovery",
    "holdout_run_recovery",
    "resume_recovery",
    "stop_recovery",
    "care_cell",
    "director_dispatch",
    "director_resume",
)
CONSUMER_ROUTES = (
    "score",
    "score_recovery",
    "finalize",
    "empty_finalize",
    "mode_install",
    "holdout",
    "holdout_recovery",
    "holdout_run_recovery",
    "resume_recovery",
    "stop_recovery",
    "care_cell",
    "care_cli",
)


class ReachedBoundary(BaseException):
    """Stop before an external operation without entering production error cleanup."""


@pytest.fixture(autouse=True)
def clean_deployment_environment(monkeypatch):
    monkeypatch.delenv(credential_path.SCORER_DSN_ENVIRONMENT, raising=False)


def _credential(path: Path, *, password: str = "isolated-private-password") -> Path:
    path.write_text(f"postgresql+psycopg://scorer:{password}@127.0.0.1:55599/isolated\n")
    path.chmod(0o600)
    return path


def _launch(route: str, monkeypatch) -> None:
    identity = uuid4()
    digest = "a" * 64
    if route == "score":
        supervisor.run_scorer_process(identity)
    elif route == "score_recovery":
        supervisor.run_scorer_recovery_process(identity, expected_claim_invocation_id=digest[:32])
    elif route == "finalize":
        supervisor.run_scorer_finalize_process(
            identity, admitted_generation=1, execution_sha256=digest
        )
    elif route == "empty_finalize":
        supervisor.run_scorer_empty_baseline_stop_process(identity)
    elif route == "mode_install":
        supervisor.run_mode_snapshot_install(digest)
    elif route == "holdout":
        holdout_supervisor.run_holdout_process(
            identity, admitted_generation=1, execution_sha256=digest
        )
    elif route == "holdout_recovery":
        holdout_supervisor.run_holdout_recovery_process(identity)
    elif route == "holdout_run_recovery":
        holdout_supervisor.run_holdout_run_recovery_process(identity)
    elif route == "resume_recovery":
        holdout_supervisor.run_director_restart_recovery_process(identity)
    elif route == "stop_recovery":
        holdout_supervisor.run_stopped_director_recovery_process(identity)
    elif route == "care_cell":
        monkeypatch.setattr(
            care_calibration_supervisor,
            "_claim_row",
            lambda *_args: {
                "calibration_id": identity,
                "generation": 1,
                "state": "reserved",
                "control_state": "running",
                "claim_seconds_left": 100,
                "job_seconds_left": 100,
            },
        )
        care_calibration_supervisor.run_care_baseline_cell_process(
            object(), calibration_id=identity, claim_id=uuid4(), generation=1
        )
    else:
        assert route in {"director_dispatch", "director_resume"}
        cli._launch_director_unit(
            identity, unit_runtime=60, restart_id=uuid4() if route == "director_resume" else None
        )


def _intercept_services(monkeypatch, capture):
    provision = Mock()
    for module in (supervisor, holdout_supervisor, care_calibration_supervisor):
        monkeypatch.setattr(module, "_ensure_aggregate_slice", provision)
        monkeypatch.setattr(module, "_job_lifecycle_lock", lambda *_a, **_kw: nullcontext())
        monkeypatch.setattr(
            module,
            "_systemctl_show",
            lambda *_a, **_kw: {
                "LoadState": "not-found",
                "ActiveState": "inactive",
                "MainPID": "0",
                "InvocationID": "",
                "ControlGroup": "",
            },
        )
    monkeypatch.setattr(cli.SystemdUnitManager, "ensure_slice", provision)
    monkeypatch.setattr(supervisor, "_expected_unit_cgroup", lambda *_a, **_kw: "/owned-test")
    monkeypatch.setattr(supervisor, "_cgroup_is_absent_or_empty", lambda *_a, **_kw: True)

    def intercepted(command, **_kwargs):
        capture.extend(command)
        raise ReachedBoundary

    monkeypatch.setattr(supervisor.subprocess, "Popen", intercepted)
    monkeypatch.setattr(supervisor.subprocess, "run", intercepted)
    return provision


@pytest.mark.parametrize("route", LAUNCH_ROUTES)
def test_every_launcher_transports_only_the_deployment_path(route, monkeypatch, tmp_path):
    path = _credential(tmp_path / "scorer.dsn")
    monkeypatch.setenv(credential_path.SCORER_DSN_ENVIRONMENT, str(path))
    captured = []
    _intercept_services(monkeypatch, captured)
    original_read_text, original_read_bytes = Path.read_text, Path.read_bytes

    def read_text(candidate, *args, **kwargs):
        assert candidate != path, "a parent process must never read the Scorer credential"
        return original_read_text(candidate, *args, **kwargs)

    def read_bytes(candidate, *args, **kwargs):
        assert candidate != path, "a parent process must never read the Scorer credential"
        return original_read_bytes(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    with pytest.raises(ReachedBoundary):
        _launch(route, monkeypatch)
    assert captured.count(f"--setenv=LAB_SCORER_DSN_FILE={path}") == 1
    assert not any("isolated-private-password" in argument for argument in captured)
    assert not any("postgresql+psycopg://" in argument for argument in captured)


@pytest.mark.parametrize("route", LAUNCH_ROUTES)
def test_malformed_deployment_prevents_provisioning_and_subprocess(route, monkeypatch):
    monkeypatch.setenv(credential_path.SCORER_DSN_ENVIRONMENT, "relative/scorer.dsn")
    captured = []
    provision = _intercept_services(monkeypatch, captured)
    with pytest.raises(ValueError, match="must be absolute"):
        _launch(route, monkeypatch)
    provision.assert_not_called()
    assert captured == []


@pytest.mark.parametrize(
    "kind",
    [
        "empty",
        "empty_file",
        "missing",
        "newline",
        "long_path",
        "oversize",
        "public",
        "symlink",
        "parent_link",
        "hardlink",
        "owner",
    ],
)
def test_explicit_credential_metadata_cannot_fall_back(kind, monkeypatch, tmp_path):
    path = _credential(tmp_path / "scorer.dsn")
    if kind == "empty":
        value = ""
    elif kind == "empty_file":
        path.write_bytes(b"")
        value = str(path)
    elif kind == "missing":
        value = str(tmp_path / "missing.dsn")
    elif kind == "newline":
        value = str(path) + "\nextra"
    elif kind == "long_path":
        value = "/" + "x" * credential_path.MAX_CREDENTIAL_PATH_BYTES
    elif kind == "oversize":
        path.write_bytes(b"x" * (credential_path.MAX_CREDENTIAL_FILE_BYTES + 1))
        value = str(path)
    elif kind == "public":
        path.chmod(0o644)
        value = str(path)
    elif kind == "symlink":
        alias = tmp_path / "alias.dsn"
        alias.symlink_to(path)
        value = str(alias)
    elif kind == "parent_link":
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path, target_is_directory=True)
        value = str(alias / path.name)
    elif kind == "hardlink":
        os.link(path, tmp_path / "alias.dsn")
        value = str(path)
    else:
        monkeypatch.setattr(credential_path.os, "getuid", lambda: path.stat().st_uid + 1)
        value = str(path)
    monkeypatch.setenv(credential_path.SCORER_DSN_ENVIRONMENT, value)
    with pytest.raises(ValueError):
        credential_path.configured_scorer_dsn_file(tmp_path / "default.dsn")


def test_unset_deployment_preserves_default_and_omits_transport(tmp_path):
    default = tmp_path / "historical-default.dsn"
    assert credential_path.configured_scorer_dsn_file(default) == default
    assert credential_path.scorer_deployment_environment() == []


@pytest.mark.parametrize("unsafe", ["group_write", "world_write", "foreign_owner"])
def test_unsafe_parent_cannot_substitute_deployment_credential(unsafe, monkeypatch, tmp_path):
    parent = tmp_path / "deployment"
    parent.mkdir(mode=0o755)
    path = _credential(parent / "scorer.dsn")
    original_lstat = Path.lstat

    def lstat(candidate, *args, **kwargs):
        if candidate == parent:
            return SimpleNamespace(
                st_mode=stat.S_IFDIR
                | {"group_write": 0o775, "world_write": 0o757, "foreign_owner": 0o755}[unsafe],
                st_uid=os.getuid() + 1000 if unsafe == "foreign_owner" else os.getuid(),
            )
        return original_lstat(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setenv(credential_path.SCORER_DSN_ENVIRONMENT, str(path))
    with pytest.raises(ValueError, match="unsafe parent"):
        credential_path.configured_scorer_dsn_file()


def test_current_owned_0755_and_root_owned_sticky_ancestors_are_allowed(monkeypatch, tmp_path):
    parent = tmp_path / "deployment"
    parent.mkdir(mode=0o755)
    path = _credential(parent / "scorer.dsn")
    original_lstat = Path.lstat

    def lstat(candidate, *args, **kwargs):
        if candidate == tmp_path:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o1777, st_uid=0)
        return original_lstat(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setenv(credential_path.SCORER_DSN_ENVIRONMENT, str(path))
    assert credential_path.configured_scorer_dsn_file() == path


def _consume(route: str) -> None:
    identity, recovery_id = uuid4(), uuid4()
    digest = "a" * 64
    if route in {"score", "score_recovery", "finalize", "empty_finalize"}:
        option = {
            "score": "--job-id",
            "score_recovery": "--recover-job-id",
            "finalize": "--finalize-run-id",
            "empty_finalize": "--finalize-run-id",
        }[route]
        args = [option, str(identity)]
        if route == "score_recovery":
            args += ["--expected-claim-invocation-id", digest[:32]]
        elif route == "finalize":
            args += ["--finalize-admitted-generation", "1", "--finalize-execution-sha256", digest]
        elif route == "empty_finalize":
            args += ["--finalize-empty-baseline-stop"]
        worker.main(args)
    elif route == "mode_install":
        mode_snapshot_worker.main(["--snapshot-sha256", digest])
    elif route == "holdout":
        holdout_worker.main(
            [
                "--reservation-id",
                str(identity),
                "--admitted-generation",
                "1",
                "--execution-sha256",
                digest,
                "--total-seconds",
                "60",
            ]
        )
    elif route in {"holdout_recovery", "holdout_run_recovery"}:
        holdout_recovery.main(
            [
                "--reservation-id" if route == "holdout_recovery" else "--run-id",
                str(identity),
                "--recovery-unit-id",
                str(recovery_id),
                "--total-seconds",
                "60",
            ]
        )
    elif route in {"resume_recovery", "stop_recovery"}:
        module = resume_recovery if route == "resume_recovery" else stop_recovery
        module.main(
            [
                "--restart-id" if route == "resume_recovery" else "--stop-id",
                str(identity),
                "--recovery-unit-id",
                str(recovery_id),
                "--total-seconds",
                "60",
            ]
        )
    elif route == "care_cell":
        care_calibration_worker.main(
            [
                "--calibration-id",
                str(identity),
                "--claim-id",
                str(recovery_id),
                "--generation",
                "1",
                "--total-seconds",
                "100",
            ]
        )
    else:
        assert route == "care_cli"
        cli.main(["scorer", "care-calibration", "--calibration-id", str(identity)])


@pytest.mark.parametrize("route", CONSUMER_ROUTES)
def test_every_consumer_reads_the_explicit_ledger_credential(route, monkeypatch, tmp_path):
    path = _credential(tmp_path / "isolated.dsn")
    default = _credential(tmp_path / "default.dsn", password="wrong-ledger-password")
    monkeypatch.setenv(credential_path.SCORER_DSN_ENVIRONMENT, str(path))
    reads = []

    def secret(candidate):
        reads.append(candidate)
        assert candidate == path, "a configured worker must not open the default ledger"
        raise ReachedBoundary

    def invocation(unit):
        return SimpleNamespace(unit=unit, invocation_id="a" * 32, control_group="/owned-test")

    for module in (
        worker,
        holdout_worker,
        holdout_recovery,
        resume_recovery,
        stop_recovery,
        mode_snapshot_worker,
    ):
        monkeypatch.setattr(module, "DEFAULT_DSN_FILE", default)
        monkeypatch.setattr(module, "_secret", secret)
        if hasattr(module, "verify_systemd_invocation"):
            monkeypatch.setattr(module, "verify_systemd_invocation", invocation)
    descriptor = os.open(os.devnull, os.O_RDONLY)
    monkeypatch.setattr(worker, "_try_process_admission_lock", lambda: descriptor)
    monkeypatch.setattr(mode_snapshot_worker, "_try_process_admission_lock", lambda: descriptor)
    monkeypatch.setattr(
        care_calibration_worker, "_acquire_process_admission_lock", lambda *_a, **_kw: descriptor
    )
    monkeypatch.setattr(care_calibration_worker, "_worker_identity", lambda: ("1", "a" * 32))
    try:
        with pytest.raises(ReachedBoundary):
            _consume(route)
    finally:
        # Some routes stop before a worker-owned descriptor's finally block.
        try:
            os.close(descriptor)
        except OSError:
            pass
    assert reads == [path]


def test_worker_writes_only_the_configured_ledger(monkeypatch, tmp_path):
    """A real entrypoint chooses between two independent database markers."""
    default = _credential(tmp_path / "default.dsn", password="wrong-ledger-password")
    selected = _credential(tmp_path / "selected.dsn")
    engines = {
        default.read_text().strip(): create_engine(f"sqlite:///{tmp_path / 'default.sqlite3'}"),
        selected.read_text().strip(): create_engine(f"sqlite:///{tmp_path / 'selected.sqlite3'}"),
    }
    for engine in engines.values():
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE marker (writes INTEGER NOT NULL)"))
            connection.execute(text("INSERT INTO marker VALUES (0)"))
    monkeypatch.setattr(worker, "DEFAULT_DSN_FILE", default)
    monkeypatch.setenv(credential_path.SCORER_DSN_ENVIRONMENT, str(selected))
    opened = []

    def isolated_engine(dsn, **_kwargs):
        opened.append(dsn)
        return engines[dsn]

    def process_one(engine, **_kwargs):
        with engine.begin() as connection:
            connection.execute(text("UPDATE marker SET writes = writes + 1"))
        return {"state": "completed"}

    monkeypatch.setattr(sqlalchemy, "create_engine", isolated_engine)
    monkeypatch.setattr(worker, "process_one", process_one)
    monkeypatch.setattr(
        worker, "verify_systemd_invocation", lambda unit: SimpleNamespace(unit=unit)
    )
    monkeypatch.setattr(
        worker, "_try_process_admission_lock", lambda: os.open(os.devnull, os.O_RDONLY)
    )
    try:
        assert worker.main(["--job-id", str(uuid4())]) == 0
        assert opened == [selected.read_text().strip()]
        for credential, expected in ((default, 0), (selected, 1)):
            with engines[credential.read_text().strip()].connect() as connection:
                assert (
                    connection.execute(text("SELECT writes FROM marker")).scalar_one() == expected
                )
    finally:
        for engine in engines.values():
            engine.dispose()
