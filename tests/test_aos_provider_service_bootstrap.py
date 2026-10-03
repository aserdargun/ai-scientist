"""Pure bootstrap ownership/source-argument checks; never launch a service."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from lab.llm.gpu_scheduler import ProcessIdentity
from scripts import check_aos_provider_service_bootstrap as bootstrap


@pytest.mark.parametrize("failure", [None, "frame", "owner", "early_intent", "missing_event"])
def test_bootstrap_audit_is_durable_and_bound_before_response(tmp_path, failure):
    """The server reads a separate committed DB; a plausible audit is insufficient."""
    database = tmp_path / "aos.sqlite3"
    broker = {"pid": 23, "uid": os.getuid(), "unit": "broker.service"}
    request = {"control_id": "a" * 32, "op": "capability", "target": None}
    original = {"request_id": "b" * 32, "payload": {}}
    binding = dict(
        session_id="session", runtime_id="runtime", owner="AGENT", lease_id="lease", generation=0
    )
    frame = bootstrap.canonical(request)
    payload = dict(
        authority="audit-only",
        control_frame=frame.decode(),
        control_sha256=bootstrap.digest_bytes(frame),
        broker_peer={k: v for k, v in broker.items() if k != "unit"},
        request_id=original["request_id"],
        request_sha256=bootstrap.digest(original),
        intent_binding=binding,
    )
    if failure == "frame":
        payload["control_frame"] = "{}\n"
    with sqlite3.connect(database) as writer:
        writer.executescript(
            "CREATE TABLE desktop_events(event_id TEXT,session_id TEXT,"
            "kind TEXT,payload_json TEXT);"
            "CREATE TABLE desktop_sessions(session_id TEXT,runtime_id TEXT,owner TEXT,"
            "lease_id TEXT,generation INTEGER,status TEXT);"
            "CREATE TABLE scientist_turn_intents(request_id TEXT);"
        )
        writer.execute(
            "INSERT INTO desktop_sessions VALUES(?,?,?,?,?,?)",
            (
                "session",
                "runtime",
                "HUMAN" if failure == "owner" else "AGENT",
                "lease",
                0,
                "running",
            ),
        )
        if failure != "missing_event":
            writer.execute(
                "INSERT INTO desktop_events VALUES(?,?,?,?)",
                (
                    request["control_id"],
                    "session",
                    "scientist_bootstrap_intent",
                    json.dumps(payload),
                ),
            )
        if failure == "early_intent":
            writer.execute(
                "INSERT INTO scientist_turn_intents VALUES(?)", (original["request_id"],)
            )
    database.chmod(0o600)
    if failure:
        with pytest.raises(ValueError, match="bootstrap audit"):
            bootstrap.verify_bootstrap_audit(tmp_path, request, broker, original)
    else:
        proof = bootstrap.verify_bootstrap_audit(tmp_path, request, broker, original)
        assert proof["original_intent_absent"] is True
        assert proof["control_id"] == request["control_id"]


@pytest.fixture
def generation():
    unit = "swapp-aos-provider-bootstrap-" + "a" * 32 + ".service"
    peer = {
        "uid": os.getuid(),
        "pid": 12345,
        "start_ticks": 67890,
        "boot_id": "11111111-1111-1111-1111-111111111111",
        "unit": unit,
        "invocation_id": "b" * 32,
        "control_group": "/user.slice/" + unit,
    }
    snapshot = {
        "Id": unit,
        "LoadState": "loaded",
        "ActiveState": "active",
        "MainPID": "12345",
        "InvocationID": peer["invocation_id"],
        "ControlGroup": peer["control_group"],
    }
    identity = ProcessIdentity(peer["pid"], peer["start_ticks"], peer["boot_id"])
    return unit, peer, snapshot, identity


def test_cleanup_requires_every_exact_service_generation_field(generation):
    unit, peer, snapshot, identity = generation
    assert bootstrap.ownership_matches(unit, snapshot, peer, identity, peer["boot_id"])
    for name, changed in (
        ("Id", bootstrap.BROKER_UNIT),
        ("LoadState", "not-found"),
        ("ActiveState", "inactive"),
        ("MainPID", "99999"),
        ("InvocationID", "c" * 32),
        ("ControlGroup", "/other.service"),
    ):
        assert not bootstrap.ownership_matches(
            unit, {**snapshot, name: changed}, peer, identity, peer["boot_id"]
        )
    assert not bootstrap.ownership_matches(unit, snapshot, peer, None, peer["boot_id"])
    assert not bootstrap.ownership_matches(
        unit,
        snapshot,
        peer,
        replace(identity, start_ticks=identity.start_ticks + 1),
        peer["boot_id"],
    )
    assert not bootstrap.ownership_matches(unit, snapshot, peer, identity, "other-boot")
    assert not bootstrap.ownership_matches(
        bootstrap.BROKER_UNIT, snapshot, peer, identity, peer["boot_id"]
    )


def test_inactive_existing_unit_is_not_available_for_replacement():
    absent = {"LoadState": "not-found", "ActiveState": "inactive", "MainPID": "0"}
    bootstrap.require_absent(absent)
    with pytest.raises(ValueError, match="must not be replaced"):
        bootstrap.require_absent({**absent, "LoadState": "loaded"})
    with pytest.raises(ValueError, match="must not be replaced"):
        bootstrap.require_absent({**absent, "MainPID": "12345"})


@pytest.fixture
def cli(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("bootstrap")
    root = tmp_path / "aos"
    root.mkdir()
    receipt = tmp_path / "preflight.json"
    receipt.write_text("{}")
    arguments = [
        "--aos-root",
        str(root),
        "--workdir",
        str(tmp_path / "private"),
        "--preflight-report",
        str(receipt),
    ]
    for name in (
        "preflight-sha256",
        "selected-source-sha256",
        "tracked-diff-sha256",
        "selected-untracked-sha256",
        "retained-host-sha256",
    ):
        arguments += ["--expected-" + name, "a" * 64]
    return arguments + ["--expected-head", "b" * 40]


def test_dry_plan_never_accesses_systemd_or_creates_workdir(cli, monkeypatch, capsys):
    monkeypatch.setattr(bootstrap, "preflight", lambda _args: {"source_ready": True})

    def forbidden(*_args, **_kwargs):
        raise AssertionError("planning must not call systemd or launch a process")

    monkeypatch.setattr(bootstrap, "user_environment", forbidden)
    monkeypatch.setattr(bootstrap, "unit_snapshot", forbidden)
    monkeypatch.setattr(bootstrap, "launch", forbidden)
    assert bootstrap.main(cli) == 0
    assert not bootstrap.parse_args(cli).workdir.exists()
    report = json.loads(capsys.readouterr().out)
    assert report["execute"] is False
    assert report["admission_allowed"] is False


def test_cli_requires_retained_host_pin_and_rejects_source_write_dir(cli):
    pin_index = cli.index("--expected-retained-host-sha256")
    with pytest.raises(SystemExit):
        bootstrap.parse_args(cli[:pin_index] + cli[pin_index + 2 :])
    changed = list(cli)
    changed[pin_index + 1] = "HEAD"
    with pytest.raises(ValueError, match="SHA256"):
        bootstrap.parse_args(changed)
    changed = list(cli)
    changed[changed.index("--workdir") + 1] = cli[cli.index("--aos-root") + 1] + "/private"
    with pytest.raises(ValueError, match="no AOS writes"):
        bootstrap.parse_args(changed)


def test_driver_does_not_accept_caller_chosen_units_or_internal_stage(cli):
    with pytest.raises(ValueError, match="generated by the driver"):
        bootstrap.parse_args(cli + ["--aos-unit", bootstrap.BROKER_UNIT])
    with pytest.raises(ValueError, match="explicit execution"):
        bootstrap.parse_args(cli + ["--broker-stage"])


def test_service_argv_has_bounded_resources_and_explicit_fd0_consumer(tmp_path):
    args = SimpleNamespace(workdir=tmp_path / "private", aos_root=tmp_path / "aos")
    unit = "swapp-aos-provider-bootstrap-" + "a" * 32 + ".service"
    command = bootstrap.service_command(
        unit,
        args.aos_root / ".venv/bin/python",
        "fixture.py",
        [str(args.workdir), str(args.aos_root), "0"],
        args,
        {"CUDA_VISIBLE_DEVICES": "", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert command[:3] == ["/usr/bin/systemd-run", "--user", "--pipe"]
    assert "--property=Restart=no" in command
    assert "--property=RuntimeMaxSec=180" in command
    assert "--property=MemoryMax=1G" in command
    assert "--property=MemorySwapMax=0" in command
    assert "--property=TasksMax=128" in command
    assert "--property=LimitFSIZE=64M" in command
    assert "--property=CPUQuota=100%" in command
    assert "--property=ReadOnlyPaths=" + str(args.aos_root) in command
    assert "CUDA_VISIBLE_DEVICES=" in command
    assert "--property=TimeoutStartSec=10s" in command
    assert "--property=TimeoutStopSec=5s" in command
    assert "--property=KillMode=control-group" in command
    assert "--property=SendSIGKILL=yes" in command
    assert "--property=UnsetEnvironment=" + " ".join(bootstrap.INJECTION_VARIABLES) in command
    env_index = command.index("/usr/bin/env")
    assert command[env_index + 1] == "-i"
    python_index = command.index(str(args.aos_root / ".venv/bin/python"))
    assert env_index < python_index
    assert "PYTHONPATH=" + str(args.aos_root / "src") in command[env_index:python_index]
    assert not any(value.startswith("--setenv=") for value in command)
    assert command[-1] == "0"
    assert "--scope" not in command and "--no-block" not in command
    with pytest.raises(ValueError, match="invalid transient unit"):
        bootstrap.service_command("unrelated.service", "python", "fixture.py", [], args, {})


def test_observed_service_bounds_reject_default_stop_grace_or_inherited_injection(tmp_path):
    snapshot = {
        "Transient": "yes",
        "Restart": "no",
        "RuntimeMaxUSec": "3min",
        "CPUQuotaPerSecUSec": "1s",
        "MemoryMax": str(1024**3),
        "MemorySwapMax": "0",
        "TasksMax": "128",
        "LimitFSIZE": str(bootstrap.FILE_LIMIT),
        "TimeoutStartUSec": "10s",
        "TimeoutStopUSec": "5s",
        "KillMode": "control-group",
        "SendSIGKILL": "yes",
        "ReadOnlyPaths": str(tmp_path),
        "UnsetEnvironment": " ".join(bootstrap.INJECTION_VARIABLES),
    }
    bootstrap.require_service_limits(snapshot, tmp_path)
    for name, changed in (
        ("TimeoutStopUSec", "1min 30s"),
        ("UnsetEnvironment", ""),
        ("KillMode", "process"),
        ("MemoryMax", "infinity"),
    ):
        with pytest.raises(ValueError):
            bootstrap.require_service_limits({**snapshot, name: changed}, tmp_path)
