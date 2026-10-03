"""Bounded fixtures for isolated persistent startup; no service or model launch."""

import json
import os
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from console import app as console
from lab.api.app import create_app
from lab.api.registry import load_suite_registry
from lab.db.schema import runs
from ops import lab_profile as module
from tests.test_api_and_scorer import _engine, _request


@pytest.fixture
def profile(tmp_path):
    result = module.LabProfile("field-lab", root=tmp_path)
    result.prepare()
    return result


def test_prepare_is_private_cpu_only_idempotent_and_never_starts_services(profile, monkeypatch):
    commands = Mock(side_effect=AssertionError("preparation must be offline"))
    monkeypatch.setattr(module, "invoke", commands)
    principal = (profile.directory / "principals.json").read_bytes()
    profile.prepare()
    assert (profile.directory / "principals.json").read_bytes() == principal
    assert (
        load_suite_registry(
            profile.directory / "registry.json", profile.root / "data/runtime"
        ).entries
        == {}
    )
    for component in ("api", "director-drain", "console"):
        environment = (profile.directory / f"{component}.env").read_text()
        assert 'LAB_CPU_ONLY="true"' in environment
        assert 'MODEL_RUNS_ENABLED="false"' in environment
        assert 'CUDA_VISIBLE_DEVICES=""' in environment
        assert "Restart=on-failure" in profile.service_text(component)
        assert "[Install]" not in profile.service_text(component)
    for file in profile.directory.rglob("*"):
        if file.is_file():
            assert file.stat().st_mode & 0o777 == 0o600
    commands.assert_not_called()


@pytest.mark.parametrize("change", ["root", "port", "environment", "principal", "dsn"])
def test_profile_rejects_drift_before_resource_operations(profile, change):
    if change in {"root", "port"}:
        path = profile.directory / "profile.json"
        value = json.loads(path.read_text())
        value["root" if change == "root" else "api_port"] = "/other" if change == "root" else 1234
        path.write_text(json.dumps(value))
    elif change == "environment":
        path = profile.directory / "api.env"
        path.write_text(path.read_text().replace('LAB_CPU_ONLY="true"', 'LAB_CPU_ONLY="false"'))
    elif change == "principal":
        path = profile.directory / "principals.json"
        value = json.loads(path.read_text())
        value["principals"][0]["owner_id"] = "another:owner"
        path.write_text(json.dumps(value))
    else:
        path = profile.directory / "postgres/director.dsn"
        path.write_text(path.read_text().replace(":55434/", ":55432/"))
    with pytest.raises(ValueError):
        profile.validate()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "shared"])
def test_private_config_rejects_aliases_and_public_permissions(profile, kind):
    path = profile.directory / "api.env"
    if kind == "shared":
        path.chmod(0o644)
    else:
        other = profile.directory / "other.env"
        path.rename(other)
        if kind == "symlink":
            path.symlink_to(other)
        else:
            os.link(other, path)
    with pytest.raises(ValueError, match="private"):
        profile.validate()


def database_info(profile):
    return {
        "Id": "owned",
        "Image": module.POSTGRES_IMAGE,
        "Config": {"Labels": {module.LABEL: profile.identity}},
        "HostConfig": {
            "PortBindings": {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "55434"}]},
            "Memory": 512 * 1024**2,
            "MemorySwap": 512 * 1024**2,
            "NanoCpus": 500_000_000,
            "PidsLimit": 64,
            "RestartPolicy": {"Name": "no"},
        },
        "Mounts": [{"Name": profile.volume, "Destination": "/var/lib/postgresql/data", "RW": True}],
        "State": {"Status": "exited"},
    }


@pytest.mark.parametrize("change", ["label", "image", "memory", "swap", "cpu", "pids", "storage"])
def test_database_inspection_rejects_foreign_binding_or_unbounded_resources(
    profile, monkeypatch, change
):
    info = database_info(profile)
    if change == "label":
        info["Config"]["Labels"][module.LABEL] = "foreign"
    elif change == "image":
        info["Image"] = "other-image"
    elif change == "storage":
        info["Mounts"][0]["Name"] = "main-ledger"
    else:
        key = {"memory": "Memory", "swap": "MemorySwap", "cpu": "NanoCpus", "pids": "PidsLimit"}[
            change
        ]
        info["HostConfig"][key] = 0
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        return "owned\n" if args[1:3] == ["container", "ls"] else json.dumps([info])

    monkeypatch.setattr(module, "invoke", command)
    with pytest.raises(ValueError, match="database identity"):
        profile.inspect_database()
    assert all(args[1] in {"container", "inspect"} for args in calls)


def test_cpu_api_rejects_direct_model_and_unregistered_admission_without_queueing(monkeypatch):
    monkeypatch.setenv("LAB_CPU_ONLY", "true")
    engine = _engine()
    app = create_app(
        director_engine=engine, director_token="u" * 32, allow_unregistered_suites=True
    )
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {'u' * 32}"}
        assert client.post("/v1/runs", headers=headers, json=_request()).status_code == 403
        assert (
            client.post(
                "/v1/mode-agent-experiments",
                headers=headers,
                json={
                    "idempotency_key": "preview-cpu-model-blocked",
                    "snapshot_sha256": "a" * 64,
                },
            ).status_code
            == 403
        )
    with engine.connect() as connection:
        assert not connection.execute(select(runs)).all()


def test_console_profile_port_keeps_browser_origin_guard_and_persistent_watch_path(
    profile, monkeypatch
):
    monkeypatch.setenv("LAB_CONSOLE_PORT", "8789")
    watched = profile.directory / "watched-runs.json"
    monkeypatch.setenv("LAB_CONSOLE_WATCH_FILE", str(watched))
    assert console.WatchStore().path == watched
    app = console.create_app(watch_path=watched, dist_root=profile.root / "none")
    with TestClient(app, base_url="http://127.0.0.1:8789", client=("127.0.0.1", 50000)) as client:
        assert client.get("/console-api/overview").status_code == 200
        assert (
            client.get("/console-api/overview", headers={"Host": "127.0.0.1:8788"}).status_code
            == 403
        )
        response = client.post(
            "/console-api/runs",
            json={},
            headers={"Origin": "http://127.0.0.1:8788", "X-Lab-Console": "1"},
        )
        assert response.status_code == 403


@pytest.mark.parametrize("port", ["0", "22", "65536", "x", "8789\n"])
def test_console_port_rejects_invalid_settings(monkeypatch, port):
    monkeypatch.setenv("LAB_CONSOLE_PORT", port)
    with pytest.raises(ValueError):
        console.console_port()


def test_migrations_bind_feature_checkout_and_private_profile_credentials(profile, monkeypatch):
    info = database_info(profile)
    info["State"]["Status"] = "running"
    monkeypatch.setattr(module.LabProfile, "inspect_database", lambda self: info)
    monkeypatch.setattr(module.LabProfile, "_initialize_roles", lambda self: None)
    monkeypatch.setattr(module.LabProfile, "check_queue", lambda self: {})
    calls = []

    def command(args, **kwargs):
        calls.append((args, kwargs))
        return ""

    monkeypatch.setattr(module, "invoke", command)
    profile.provision_database()
    args, kwargs = next(item for item in calls if "alembic" in item[0])
    assert args[-2:] == ["upgrade", "head"]
    assert kwargs["cwd"] == profile.root
    assert kwargs["environment"]["LAB_MIGRATOR_DSN_FILE"] == str(
        profile.directory / "postgres/migrator.dsn"
    )
    calls.clear()
    profile.provision_database()
    assert not any("alembic" in args for args, _ in calls)


def test_ram_and_disk_admission_include_existing_pipeline_caps_and_host_reserve(
    profile, monkeypatch
):
    from types import SimpleNamespace

    original = module.Path.read_text

    def read(path, *args, **kwargs):
        if str(path) == "/proc/meminfo":
            return "MemAvailable: 1048576 kB\n"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(module.Path, "read_text", read)
    monkeypatch.setattr(
        module.LabProfile, "inspect_unit", lambda self, component: {"ActiveState": "inactive"}
    )
    monkeypatch.setattr(module.LabProfile, "inspect_database", lambda self: None)
    monkeypatch.setattr(
        module.shutil, "disk_usage", lambda path: SimpleNamespace(free=30 * 1024**3)
    )
    result = profile.resource_admission()
    assert not result["admitted"]
    assert result["inactive_service_caps_bytes"] == (768 + 512 + 384 + 512) * 1024**2
    assert result["cpu_pipeline_caps_bytes"] == 8 * 1024**3
    assert result["host_reserve_bytes"] == 6 * 1024**3
    assert result["disk_reserve_bytes"] == 20 * 1024**3


@pytest.mark.parametrize("race", [False, True])
def test_idle_stop_keeps_owned_storage_and_rechecks_admission_race(profile, monkeypatch, race):
    info = database_info(profile)
    info["State"]["Status"] = "running"
    monkeypatch.setattr(module.LabProfile, "inspect_database", lambda self: info)
    states = {component: "active" for component in ("api", "director-drain", "console")}
    monkeypatch.setattr(
        module.LabProfile,
        "inspect_unit",
        lambda self, component: {"ActiveState": states[component]},
    )
    queues = iter([{"queued": 0}, {"queued": 1 if race else 0}])
    monkeypatch.setattr(module.LabProfile, "check_queue", lambda self: next(queues))
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        for component in states:
            if args[-1] == profile.unit(component):
                states[component] = "inactive"
        return ""

    monkeypatch.setattr(module, "invoke", command)
    if race:
        with pytest.raises(ValueError, match="arrived"):
            profile.stop()
        assert states["director-drain"] == "active"
        assert not any(args[0] == "docker" for args in calls)
    else:
        profile.stop()
        assert calls[-1] == ["docker", "stop", "--time", "20", "owned"]
        assert all(state == "inactive" for state in states.values())
    assert not any("rm" in args or "kill" in args or "prune" in args for args in calls)
    profile.validate()


def test_nonterminal_work_refuses_stop_before_any_mutation(profile, monkeypatch):
    info = database_info(profile)
    info["State"]["Status"] = "running"
    monkeypatch.setattr(module.LabProfile, "inspect_database", lambda self: info)
    monkeypatch.setattr(
        module.LabProfile, "inspect_unit", lambda self, component: {"ActiveState": "active"}
    )
    monkeypatch.setattr(module.LabProfile, "check_queue", lambda self: {"running": 1})
    mutate = Mock(side_effect=AssertionError("active work must be retained"))
    monkeypatch.setattr(module, "invoke", mutate)
    with pytest.raises(ValueError, match="nonterminal"):
        profile.stop()
    mutate.assert_not_called()
