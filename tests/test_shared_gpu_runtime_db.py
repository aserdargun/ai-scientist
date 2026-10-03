from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from lab import cli
from lab.cli import _validated_gpu_runtime_database
from lab.director.ownership import ExecutionOwner


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True)
    path.chmod(0o700)
    return path


def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = _private_directory(tmp_path / "home")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    _private_directory(home / ".local")
    _private_directory(home / ".local/state")
    _private_directory(home / ".local/state/swapp-gpu")
    return home


def test_exact_shared_broker_database_is_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _home(monkeypatch, tmp_path)
    database = home / ".local/state/swapp-gpu/arbiter.sqlite3"

    assert _validated_gpu_runtime_database(database) == database


def test_provider_uses_only_the_fixed_shared_database_from_service_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _home(monkeypatch, tmp_path)
    database = home / ".local/state/swapp-gpu/arbiter.sqlite3"
    captured: dict[str, object] = {}
    expected_config = "a" * 64
    run_id = uuid4()
    engine = object()
    owner = ExecutionOwner(
        run_id=run_id, generation=1, invocation_id="d" * 32, execution_sha256="e" * 64
    )

    def observer() -> None:
        pass

    def observer_factory(actual_engine, actual_owner):
        assert actual_engine is engine
        assert actual_owner is owner
        return observer

    monkeypatch.setenv("SWAPP_AOS_GPU_UNIT", "swapp-aos-gpu-review.service")
    monkeypatch.setenv(
        "SWAPP_LAB_GPU_UNIT", "swapp-ai-scientist-director-dispatch-" + "b" * 32 + ".service"
    )
    monkeypatch.setenv("SWAPP_GPU_RUNTIME_DB", str(database))
    monkeypatch.setattr(cli, "provider_config_sha256", lambda profile_set: expected_config)
    monkeypatch.setattr(cli, "SystemdPrincipalResolver", lambda units: units)
    monkeypatch.setattr(cli, "active_execution_owner", lambda: owner)
    monkeypatch.setattr(cli, "_director_model_observer", observer_factory)

    def provider_factory(**kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(configuration_sha256=expected_config)

    monkeypatch.setattr(cli, "LocalQwenProposalProvider", provider_factory)
    provider = cli._local_qwen_provider(
        run_id, "c" * 64, director_engine=engine, profile_set="research"
    )

    assert provider.configuration_sha256 == expected_config
    assert captured["runtime_database"] == database
    assert captured["cancellation_observer"] is observer
    assert captured["proposal_contract"] == "candidate-python.v1"


def test_shared_database_may_be_created_by_broker_after_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _home(monkeypatch, tmp_path)
    database = home / ".local/state/swapp-gpu/arbiter.sqlite3"
    database.touch(mode=0o600)

    assert _validated_gpu_runtime_database(database) == database


def test_sibling_shared_database_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _home(monkeypatch, tmp_path)
    sibling = home / ".local/state/swapp-gpu/other.sqlite3"

    with pytest.raises(ValueError, match="direct child|fixed broker"):
        _validated_gpu_runtime_database(sibling)


def test_shared_database_symlink_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _home(monkeypatch, tmp_path)
    database = home / ".local/state/swapp-gpu/arbiter.sqlite3"
    target = home / ".local/state/swapp-gpu/target.sqlite3"
    target.touch(mode=0o600)
    database.symlink_to(target)

    with pytest.raises(ValueError, match="symlink"):
        _validated_gpu_runtime_database(database)


def test_shared_path_symlink_component_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _private_directory(tmp_path / "home")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    local = home / ".local"
    target = _private_directory(tmp_path / "other-local")
    local.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="unsafe directory component"):
        _validated_gpu_runtime_database(home / ".local/state/swapp-gpu/arbiter.sqlite3")


def test_shared_state_requires_private_modes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _home(monkeypatch, tmp_path)
    state = home / ".local/state"
    state.chmod(0o755)

    with pytest.raises(ValueError, match="private to the service owner"):
        _validated_gpu_runtime_database(home / ".local/state/swapp-gpu/arbiter.sqlite3")


def test_existing_private_project_runtime_path_remains_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _home(monkeypatch, tmp_path)
    monkeypatch.setattr("lab.cli.GPU_RUNTIME_ROOT", home / "project/data/runtime/gpu")
    runtime = _private_directory(home / "project/data/runtime/gpu")
    database = runtime / "arbiter.sqlite3"
    database.touch(mode=0o600)
    database.chmod(0o600)

    assert _validated_gpu_runtime_database(database) == database


def test_project_runtime_rejects_arbitrary_parent(tmp_path: Path) -> None:
    outside = tmp_path / "other/arbiter.sqlite3"

    with pytest.raises(ValueError, match="direct child"):
        _validated_gpu_runtime_database(outside)


def test_project_runtime_rejects_non_private_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _home(monkeypatch, tmp_path)
    runtime = _private_directory(home / "project/data/runtime/gpu")
    monkeypatch.setattr("lab.cli.GPU_RUNTIME_ROOT", runtime)
    database = runtime / "arbiter.sqlite3"
    database.touch(mode=0o644)
    database.chmod(0o644)

    with pytest.raises(ValueError, match="private regular file"):
        _validated_gpu_runtime_database(database)
