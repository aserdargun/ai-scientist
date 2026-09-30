"""Startup identity and launch checks; external commands and processes are stubbed."""

import json
import os

import pytest

from ops import start_lab as module


def _stub(monkeypatch, name="api", *, entrypoint=False, active=True):
    item = next(item for item in module.SERVICES if item[0] == name)
    _, memory, cpu, tasks, _, port, args = item
    argv = [module.PYTHON, "-m", *args]
    if entrypoint:
        argv = [str(module.ROOT / ".venv/bin/lab"), *args[1:]]
    state = {
        "LoadState": "loaded",
        "ActiveState": "active" if active else "inactive",
        "WorkingDirectory": str(module.ROOT),
        "ExecStart": f"{{ path={argv[0]} ; argv[]={' '.join(argv)} ; ignore_errors=no ; pid=42 }}",
        "MainPID": "42",
        "MemoryMax": str(int(memory[:-1]) * 1024 ** (3 if memory[-1] == "G" else 2)),
        "MemorySwapMax": "0",
        "CPUQuotaPerSecUSec": "1s" if cpu == "100%" else "500ms",
        "TasksMax": tasks,
    }
    process = [str(module.ROOT / ".venv/bin/python3"), *argv] if entrypoint else list(argv)

    def command(actual, **kwargs):
        assert actual[:3] == ["systemctl", "--user", "show"]
        assert actual[3] == f"swapp-ai-scientist-{name}.service"
        assert "MemoryMax" in actual[4] and "CPUQuotaPerSecUSec" in actual[4]
        return "\n".join(f"{key}={value}" for key, value in state.items())

    monkeypatch.setattr(module, "command", command)
    monkeypatch.setattr(module, "_process_identity", lambda pid: (module.ROOT, process))
    return (f"swapp-ai-scientist-{name}.service", args[0], port), state, process


@pytest.mark.parametrize(
    "name,entrypoint",
    [
        ("api", False),
        ("director-drain", False),
        ("console", False),
        ("api", True),
        ("director-drain", True),
    ],
)
def test_exact_current_and_installed_commands_are_accepted(monkeypatch, name, entrypoint):
    args, state, _ = _stub(monkeypatch, name, entrypoint=entrypoint)
    assert module.check_unit(*args) == state


@pytest.mark.parametrize("name,entrypoint", [("api", False), ("director-drain", True)])
def test_extra_definition_arguments_are_rejected(monkeypatch, name, entrypoint):
    args, state, _ = _stub(monkeypatch, name, entrypoint=entrypoint)
    state["ExecStart"] = state["ExecStart"].replace(" ; ignore_errors", " --extra ; ignore_errors")
    with pytest.raises(RuntimeError, match="komutu veya kaynak"):
        module.check_unit(*args)


@pytest.mark.parametrize("extra", ["--port=9999", "--poll-seconds=1"])
def test_entire_process_argv_is_checked(monkeypatch, extra):
    args, _, process = _stub(monkeypatch)
    process.append(extra)
    with pytest.raises(RuntimeError, match="süreç kimliği"):
        module.check_unit(*args)


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("cap", ["MemoryMax", "MemorySwapMax", "CPUQuotaPerSecUSec", "TasksMax"])
def test_resource_caps_are_required_before_accept_or_start(monkeypatch, active, cap):
    args, state, _ = _stub(monkeypatch, "director-drain", active=active)
    state[cap] = "infinity"
    with pytest.raises(RuntimeError, match="komutu veya kaynak"):
        module.check_unit(*args)


def test_second_execstart_definition_is_rejected(monkeypatch):
    args, state, _ = _stub(monkeypatch)
    state["ExecStart"] += " " + state["ExecStart"]
    with pytest.raises(RuntimeError, match="komutu veya kaynak"):
        module.check_unit(*args)


@pytest.mark.parametrize("console_active", [False, True])
def test_start_preserves_console_session_and_leaves_active_services_alone(
    monkeypatch, tmp_path, console_active
):
    config = tmp_path / "config"
    runtime = tmp_path / "runtime"
    config.mkdir()
    runtime.mkdir()
    for path in (config / "lab-api.env", config / "lab-worker.env", runtime / "principals.json"):
        path.write_text("fixture")
        path.chmod(0o600)
    (runtime / "registry.json").write_text("{}")
    dist = tmp_path / "console/web/dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("fixture")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "CONFIG", config)
    monkeypatch.setattr(module, "RUNTIME", runtime)
    monkeypatch.setattr(module.shutil, "which", lambda name: "/fixture/" + name)
    session_dir = f"/run/user/{os.getuid()}"
    session_bus = f"unix:path={session_dir}/bus"
    monkeypatch.setenv("XDG_RUNTIME_DIR", session_dir)
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", session_bus)
    calls = []
    ready = []
    launched = False

    def check_unit(unit, module_name, port):
        active = not unit.endswith("-console.service") or console_active or launched
        return {"LoadState": "loaded" if active else "not-found",
                "ActiveState": "active" if active else "inactive"}

    def command(argv, **kwargs):
        nonlocal launched
        calls.append(argv)
        if argv == ["docker", "inspect", module.CONTAINER]:
            return json.dumps([{
                "Id": "fixture-db-id",
                "Config": {"Labels": {"org.swapp.component": "ai-scientist-m0-ledger"}},
                "HostConfig": {
                    "PortBindings": {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "55432"}]},
                    "Memory": 512 * 1024**2, "MemorySwap": 512 * 1024**2,
                    "NanoCpus": 1_000_000_000, "PidsLimit": 128,
                },
                "State": {"Status": "running"},
            }])
        if argv[:3] == ["docker", "exec", "fixture-db-id"]:
            assert argv[3:] == ["pg_isready", "-U", "swapp_lab_admin", "-d", "swapp_lab"]
            return "ready"
        if argv[0] == "systemd-run":
            assert not console_active
            assert "--unit=swapp-ai-scientist-console.service" in argv
            launched = True
            return ""
        pytest.fail(f"unexpected service mutation: {argv}")

    monkeypatch.setattr(module, "check_unit", check_unit)
    monkeypatch.setattr(module, "command", command)
    monkeypatch.setattr(module, "wait_http", lambda url, **kwargs: ready.append(url))
    module.start()
    launches = [argv for argv in calls if argv[0] == "systemd-run"]
    assert len(launches) == int(not console_active)
    if launches:
        argv = launches[0]
        assert f"--setenv=XDG_RUNTIME_DIR={session_dir}" in argv
        assert f"--setenv=DBUS_SESSION_BUS_ADDRESS={session_bus}" in argv
        assert "--property=MemoryMax=512M" in argv
        assert "--property=MemorySwapMax=0" in argv
        assert "--property=CPUQuota=50%" in argv
        assert "--property=TasksMax=64" in argv
        assert argv[-3:] == [module.PYTHON, "-m", "console.server"]
    assert ready == ["http://127.0.0.1:8766/health", "http://127.0.0.1:8788/console-api/overview"]
