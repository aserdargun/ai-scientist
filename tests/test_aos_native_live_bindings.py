"""Pure service stand-ins; these checks never establish real runtime admission."""

from __future__ import annotations

import hashlib
import json
import os
import time
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lab.llm.aos_gpu_broker import PeerGeneration
from lab.llm.aos_gpu_control import ControlPolicy, LabAOSControl, digest
from lab.llm.gpu_scheduler import ProcessIdentity
from scripts import aos_native_live_bindings as live


def _write(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def capture(tmp_path, monkeypatch):
    aos_unit, lab_unit = "swapp-aos-gpu-review.service", "swapp-lab-gpu-review.service"
    source_root = tmp_path / "aos"
    source_root.mkdir()
    entries = {}
    for profile, kind in (
        ("aos.decider.turn.v1", "decider"),
        ("aos.bonsai.recovery.v1", "bonsai-recovery"),
        ("aos.bonsai.vision.v1", "bonsai-vision"),
    ):
        entries[profile] = {
            "kind": kind,
            "manifest": str(tmp_path / "manifest.json"),
            "manifest_sha256": "a" * 64,
            "deployment_digest": "b" * 64,
            "python": str(tmp_path / "python"),
            "model_paths": [str(tmp_path / "model")],
            "budgets": {
                "activation_seconds": 30,
                "inference_seconds": 30,
                "total_seconds": 60,
                "queue_seconds": 90,
            },
            "response_schema_sha256": "c" * 64,
            "temperature": 0.0,
            "max_output_tokens": 128,
            "context_tokens": 1024,
            "output_contract": {
                "name": "aos-scientist-profile-output.v2",
                "version": 2,
                "bundle_sha256": "d" * 64,
            },
        }
    config = {
        "schema": "swapp-aos-gpu-profiles.v1",
        "source_root": str(source_root),
        "profiles": entries,
    }
    profile_path, policy_path = tmp_path / "profiles.json", tmp_path / "policy.json"
    profile_hash = _write(profile_path, config)
    # Keep native profile parsing/hash behavior, but deliberately replace the
    # artifact-verifying lookup: these are generation fixtures, not model files
    # or a reviewed output bundle. Production ProfileRegistry.get stays intact.
    fixture_profiles = live.load_profiles(profile_path, source_root=source_root)._profiles

    def fixture_get(profile_id, deployment_digest):
        profile = fixture_profiles[profile_id]
        if profile.deployment_digest != deployment_digest:
            raise ValueError("Fixture profile deployment differs")
        return profile

    registry = SimpleNamespace(get=fixture_get)
    monkeypatch.setattr(live, "load_profiles", Mock(return_value=registry))
    policy_value = {
        "enabled": True,
        "caller_unit": aos_unit,
        "source_files": {"scientist": {"source.py": "a" * 64}, "aos": {"source.py": "b" * 64}},
    }
    policy_hash = _write(policy_path, policy_value)
    policy = SimpleNamespace(
        config=deepcopy(policy_value),
        sha256=digest(policy_value),
        verify=Mock(),
        _pin=ControlPolicy._pin,
    )
    monkeypatch.setattr(live, "ControlPolicy", Mock(return_value=policy))
    monkeypatch.setattr(live, "_user_bus", Mock())
    boot = "11111111-1111-1111-1111-111111111111"
    generations = {}
    for index, unit in enumerate((live.BROKER_UNIT, aos_unit, lab_unit)):
        generations[unit] = {
            "pid": 22000 + index,
            "uid": os.getuid(),
            "start_ticks": 50 + index,
            "boot_id": boot,
            "unit": unit,
            "invocation_id": str(index + 1) * 32,
            "control_group": "/user.slice/swapp-gpu.slice/" + unit,
        }
    caller = generations[aos_unit]
    peer = PeerGeneration(
        **caller, parent_pid=caller["pid"], parent_start_ticks=caller["start_ticks"]
    )
    observer = Mock(side_effect=lambda unit, *_args, **_kwargs: deepcopy(generations[unit]))
    monkeypatch.setattr(live, "_generation", observer)
    authenticator = SimpleNamespace(
        authenticate=Mock(return_value=peer), still_current=Mock(return_value=True)
    )
    monkeypatch.setattr(live, "SystemdSocketPeerAuthenticator", Mock(return_value=authenticator))
    resolver = SimpleNamespace(verify=Mock(return_value=True))
    monkeypatch.setattr(live, "SystemdPrincipalResolver", Mock(return_value=resolver))
    arguments = (profile_path, profile_hash, policy_path, policy_hash)
    options = {"aos_unit": aos_unit, "lab_unit": lab_unit}
    return SimpleNamespace(**locals())


def test_full_bindings_equal_native_control_mapping_and_use_aos_mainpid(capture):
    ctx = capture
    result = live.capture_reviewed_bindings(*ctx.arguments, **ctx.options)
    native = LabAOSControl(
        policy=ctx.policy,
        authenticator=ctx.authenticator,
        store=None,
        server_generation=ctx.generations[live.BROKER_UNIT],
        clock=time.monotonic,
    )
    profiles = live.load_profiles(ctx.profile_path, source_root=ctx.source_root)
    assert set(result) == live.PROFILE_KEYS
    for profile, binding in result.items():
        assert binding == native._binding(ctx.peer, profiles.get(profile, "b" * 64))
        assert binding["caller_generation"]["pid"] == ctx.peer.pid != os.getpid()
        assert binding["caller_generation"]["parent_pid"] == ctx.peer.pid
    assert ctx.authenticator.authenticate.call_count == 2
    assert ctx.policy.verify.call_count == 2
    assert ctx.resolver.verify.call_count == 2
    assert live.control_deadline.get() is None


def test_no_live_lab_principal_is_required_when_explicitly_absent(capture):
    ctx = capture
    live.capture_reviewed_bindings(*ctx.arguments, aos_unit=ctx.aos_unit, lab_unit=None)
    live.SystemdPrincipalResolver.assert_not_called()
    assert all(call.args[0] != ctx.lab_unit for call in ctx.observer.call_args_list)


@pytest.mark.parametrize(
    "change",
    [
        "disabled",
        "wrong-caller",
        "bad-file-hash",
        "peer-restart",
        "server-restart",
        "lab-revoked",
        "policy-revoked",
    ],
)
def test_capture_rejects_changed_authority(capture, change):
    ctx = capture
    if change in {"disabled", "wrong-caller"}:
        ctx.policy.config["enabled" if change == "disabled" else "caller_unit"] = (
            False if change == "disabled" else "swapp-aos-gpu-other.service"
        )
    elif change == "bad-file-hash":
        ctx.profile_path.write_text("{}")
    elif change == "peer-restart":
        ctx.authenticator.authenticate.side_effect = [
            ctx.peer,
            PeerGeneration(**{**asdict(ctx.peer), "start_ticks": ctx.peer.start_ticks + 1}),
        ]
    elif change == "server-restart":
        original = ctx.observer.side_effect
        counts = {}

        def observe(unit, *args, **kwargs):
            result = original(unit, *args, **kwargs)
            counts[unit] = counts.get(unit, 0) + 1
            if unit == live.BROKER_UNIT and counts[unit] == 2:
                result["invocation_id"] = "f" * 32
            return result

        ctx.observer.side_effect = observe
    elif change == "lab-revoked":
        ctx.resolver.verify.side_effect = [True, False]
    else:
        ctx.policy.verify.side_effect = [None, ValueError("policy revoked")]
    with pytest.raises(ValueError):
        live.capture_reviewed_bindings(*ctx.arguments, **ctx.options)
    assert live.control_deadline.get() is None


def test_capture_preserves_outer_deadline_and_denies_expiry(capture):
    token = live.control_deadline.set(time.monotonic() - 1)
    try:
        with pytest.raises(ValueError, match="deadline"):
            live.capture_reviewed_bindings(*capture.arguments, **capture.options)
        assert live.control_deadline.get() < time.monotonic()
    finally:
        live.control_deadline.reset(token)


@pytest.mark.parametrize("change", [None, "wrong-uid", "wrong-cgroup", "process-restart"])
def test_generation_checks_actual_service_process(monkeypatch, change):
    unit, pid = live.BROKER_UNIT, 23456
    boot = "11111111-1111-1111-1111-111111111111"
    group = "/user.slice/" + unit
    values = {
        "MainPID": str(pid),
        "LoadState": "loaded",
        "ActiveState": "active",
        "ControlGroup": group,
        "InvocationID": "a" * 32,
    }
    identity = ProcessIdentity(pid, 70, boot)
    monkeypatch.setattr(live, "_systemctl_show", Mock(return_value=values))
    monkeypatch.setattr(live, "_boot_id", lambda: boot)
    monkeypatch.setattr(
        live,
        "_process_cgroup",
        lambda _pid: group + "/other" if change == "wrong-cgroup" else group,
    )
    reader = Mock(return_value=identity)
    if change == "process-restart":
        reader.side_effect = [identity, ProcessIdentity(pid, 71, boot)]
    monkeypatch.setattr(live, "_read_process_identity", reader)
    actual_stat = live.Path.stat

    def process_stat(path, *args, **kwargs):
        if str(path) == f"/proc/{pid}":
            return SimpleNamespace(st_uid=os.getuid() + (1 if change == "wrong-uid" else 0))
        return actual_stat(path, *args, **kwargs)

    monkeypatch.setattr(live.Path, "stat", process_stat)
    token = live.control_deadline.set(time.monotonic() + 10)
    try:
        if change is None:
            assert live._generation(unit, "lab", require_slice=False)["pid"] == pid
        else:
            with pytest.raises(ValueError):
                live._generation(unit, "lab", require_slice=False)
    finally:
        live.control_deadline.reset(token)
