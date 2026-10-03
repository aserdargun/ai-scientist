"""CPU process/output/deadline checks; no native services, models or deployment authority."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lab.llm.aos_gpu_control_store import control_deadline
from scripts import aos_native_launch as launch


@pytest.fixture
def receipt_launch(monkeypatch, tmp_path):
    """Real pinned files, receipt builder and exclusive publication; runtime gates are inert."""
    from scripts import aos_joint_lab_hooks as hooks
    from scripts import aos_native_artifact_receipts as receipts

    root, aos = tmp_path / "scientist", tmp_path / "aos"
    output = root / "data/runtime/receipt.json"
    output.parent.mkdir(parents=True)
    sources = {}
    for path, body in (
        (root / "scripts/check_aos_model_environment.py", b"# inert verifier\n"),
        (root / "scripts/aos_native_artifact_receipts.py", b"# inert source fixture\n"),
        (
            aos / "scripts/serve_desktop.py",
            b"def main(**kwargs): raise AssertionError('no Desktop')\n",
        ),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        sources[str(path)] = hashlib.sha256(body).hexdigest()
    entrypoint = Path(launch.__file__).absolute()
    sources[str(entrypoint)] = hashlib.sha256(entrypoint.read_bytes()).hexdigest()

    def pin(path, value):
        data = json.dumps(value).encode()
        path.write_bytes(data)
        return hashlib.sha256(data).hexdigest()

    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    caller = "swapp-aos-gpu-joint-acceptance.service"
    broker = "swapp-lab-gpu-broker.service"
    bindings = {
        profile: {
            "caller_generation": {"pid": os.getpid(), "unit": caller, "boot_id": boot},
            "server_generation": {"unit": broker, "boot_id": boot},
        }
        for profile in ("profile", "other")
    }
    review = {
        "schema": "scientist.native-retained-factory-review.v1",
        "enabled": True,
        "authority": {
            "owner": "AGENT",
            "caller_unit": caller,
            "broker_unit": broker,
            "boot_id": boot,
            "issued_boottime": 90.0,
            "expires_boottime": 2000.0,
            "allowed_profiles": list(bindings),
            "control_operations": ["capability", "reconcile"],
            "provider_operations": ["read_budget", "verify_physical"],
            "resolution": "recorded_successful_original_only",
            "max_targets": 16,
        },
    }
    review_path = tmp_path / "retained.json"
    static_path = tmp_path / "static.json"
    cfg = {
        "schema": "scientist.native-launch-review.v1",
        "caller_unit": caller,
        "factory_arguments": {
            "source_roots": {"scientist": str(root), "aos": str(aos)},
            "profile_config_path": "inert-profile",
            "profile_file_sha256": "a" * 64,
            "policy_path": "inert-policy",
            "policy_file_sha256": "b" * 64,
            "source_files": {
                "aos": {"scripts/serve_desktop.py": sources[str(aos / "scripts/serve_desktop.py")]}
            },
        },
        "artifact_input_path": str(static_path),
        "artifact_input_sha256": pin(
            static_path,
            {
                "schema": "scientist.native-static-artifact-input.v1",
                "requirements": {profile: {} for profile in bindings},
                "source_inputs": sources,
            },
        ),
        "receipt_output": str(output),
        "native_verification_python": sys.executable,
        "native_review_path": "inert-native-review",
        "native_review_sha256": "c" * 64,
        "joint_lab_review_path": "inert-joint-review",
        "joint_lab_review_sha256": "d" * 64,
    }
    case = SimpleNamespace(
        cfg=cfg,
        review=review,
        review_path=review_path,
        bindings=bindings,
        output=output,
        now=100.0,
        on_verify=None,
        deadline=time.monotonic() + 10,
    )
    original_clock = time.clock_gettime
    monkeypatch.setattr(
        launch.time,
        "clock_gettime",
        lambda clock: case.now if clock == time.CLOCK_BOOTTIME else original_clock(clock),
    )
    capture = Mock(return_value=bindings)
    monkeypatch.setattr(launch, "capture_reviewed_bindings", capture)
    case.capture = capture
    monkeypatch.setattr(
        receipts, "snapshot", lambda *_: {"verification_interpreter": sys.executable}
    )

    def native_verify(_command, *, deadline):
        assert deadline == case.deadline
        case.now += 10
        if case.on_verify is not None:
            case.on_verify()
        return {
            "schema": "aos-model-environment-readback.v1",
            "artifact_bytes_verified": True,
            "dependency_versions_verified": True,
            "source_and_manifests_unchanged": True,
            "model_instantiated": False,
            "gpu_acquired": False,
            "interpreter": sys.executable,
        }

    case.native = Mock(side_effect=native_verify)
    monkeypatch.setattr(launch, "_verify_native", case.native)
    monkeypatch.setattr(
        launch, "ConfiguredScientistAdmissionFactory", Mock(return_value=SimpleNamespace())
    )
    monkeypatch.setattr(launch, "_retained_factory", Mock(return_value=None))
    monkeypatch.setattr(hooks, "JointLabCapability", Mock(return_value=SimpleNamespace()))

    def retain():
        cfg["retained_review_path"] = str(review_path)
        cfg["retained_review_sha256"] = pin(review_path, review)

    def prepare():
        path = tmp_path / "launch.json"
        return launch._prepare_launch(path, pin(path, cfg), case.deadline)

    case.retain, case.prepare = retain, prepare
    return case


@pytest.mark.parametrize(
    "validity,authority_expiry,expected_expiry",
    [
        (None, None, 410),
        (1, None, 111),
        (300, None, 410),
        (None, 200, 200),
        (300, 200, 200),
        (900, 2000, 1010),
        (900, 600, 600),
    ],
)
def test_new_receipt_preserves_default_and_clamps_reviewed_ttl(
    receipt_launch, validity, authority_expiry, expected_expiry
):
    case = receipt_launch
    if validity is not None:
        case.cfg["artifact_validity_seconds"] = validity
    if authority_expiry is not None:
        case.review["authority"]["expires_boottime"] = authority_expiry
        case.retain()
        original_review = case.review_path.read_bytes()
    case.prepare()
    result = json.loads(case.output.read_bytes())
    assert result["started_boottime"] == 100
    assert result["verified_boottime"] == 110
    assert result["expires_boottime"] == expected_expiry
    assert case.output.stat().st_mode & 0o777 == 0o600
    case.native.assert_called_once()
    if authority_expiry is not None:
        assert case.review_path.read_bytes() == original_review


@pytest.mark.parametrize("validity", [True, False, 0, -1, 901, 300.0, "900", None])
def test_invalid_reviewed_artifact_ttl_fails_before_live_capture(receipt_launch, validity):
    case = receipt_launch
    case.cfg["artifact_validity_seconds"] = validity
    with pytest.raises(ValueError, match="integer from 1 to 900"):
        case.prepare()
    case.capture.assert_not_called()
    case.native.assert_not_called()
    assert not case.output.exists()


def test_extended_artifact_ttl_requires_pinned_original_authority(receipt_launch):
    case = receipt_launch
    case.cfg["artifact_validity_seconds"] = 900
    with pytest.raises(ValueError, match="requires pinned retained authority"):
        case.prepare()
    case.native.assert_not_called()
    assert not case.output.exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"expires_boottime": 100},
        {"issued_boottime": 101},
        {"expires_boottime": float("inf")},
        {"issued_boottime": True},
        {"expires_boottime": 3691},
        {"boot_id": "another-boot"},
        {"owner": "ANOTHER"},
        {"caller_unit": "another-caller.service"},
        {"broker_unit": "another-broker.service"},
        {"allowed_profiles": ["profile"]},
    ],
)
def test_extended_artifact_ttl_rejects_invalid_original_rights_before_verification(
    receipt_launch, changes
):
    case = receipt_launch
    case.cfg["artifact_validity_seconds"] = 900
    case.review["authority"].update(changes)
    case.retain()
    with pytest.raises(ValueError, match="Original retained authority"):
        case.prepare()
    case.native.assert_not_called()
    assert not case.output.exists()


def test_default_receipt_rejects_disabled_retained_authority(receipt_launch):
    case = receipt_launch
    case.review["enabled"] = False
    case.retain()
    with pytest.raises(ValueError, match="enabled original retained authority"):
        case.prepare()
    case.native.assert_not_called()
    assert not case.output.exists()


def test_original_retained_boot_must_match_both_captured_generations(receipt_launch):
    case = receipt_launch
    case.retain()
    case.bindings["profile"]["server_generation"]["boot_id"] = "another-boot"
    with pytest.raises(ValueError, match="differs from live bindings"):
        case.prepare()
    case.native.assert_not_called()
    assert not case.output.exists()


@pytest.mark.parametrize("change", ["expire", "replace"])
def test_original_authority_is_reread_after_verification_before_publication(receipt_launch, change):
    case = receipt_launch
    case.cfg["artifact_validity_seconds"] = 900
    if change == "expire":
        case.review["authority"]["expires_boottime"] = 105
        expected = "expired"
    else:
        case.on_verify = lambda: case.review_path.write_bytes(b"{}")
        expected = "input pin mismatch"
    case.retain()
    with pytest.raises(ValueError, match=expected):
        case.prepare()
    case.native.assert_called_once()
    assert not case.output.exists()


def test_longer_validity_cannot_replace_or_renew_an_existing_receipt(receipt_launch):
    case = receipt_launch
    case.retain()
    case.prepare()
    original, inode = case.output.read_bytes(), case.output.stat().st_ino
    case.cfg["artifact_validity_seconds"] = 900
    with pytest.raises(FileExistsError):
        case.prepare()
    assert case.output.read_bytes() == original
    assert case.output.stat().st_ino == inode
    assert json.loads(original)["expires_boottime"] == 410


def test_runtime_denial_preserves_original_exception_and_hides_private_details(capsys):
    denied = ValueError("private-token-and-config-path")

    def confirm(_profiles):
        try:
            raise KeyError("private-inner-secret")
        except KeyError as inner:
            raise denied from inner

    with pytest.raises(ValueError) as observed:
        launch._confirm_runtime(SimpleNamespace(confirm_runtime=confirm), {"profile": "pin"})
    assert observed.value is denied
    captured = capsys.readouterr()
    assert "private-" not in captured.err
    assert str(Path(__file__)) not in captured.err
    assert captured.out == ""
    chain = json.loads(captured.err)["native_runtime_denied"]
    assert [entry["type"] for entry in chain] == ["ValueError", "KeyError"]
    assert any(frame["function"] == "confirm" for frame in chain[0]["frames"])


@pytest.mark.parametrize("result", [None, "invalid-return"])
def test_runtime_observer_preserves_callback_return_for_native_guard(capsys, result):
    callback = Mock(return_value=result)
    profiles = {"profile": "pin"}
    assert launch._confirm_runtime(SimpleNamespace(confirm_runtime=callback), profiles) is result
    callback.assert_called_once_with(profiles)
    assert capsys.readouterr() == ("", "")


def track_children(monkeypatch):
    actual = subprocess.Popen
    children = []

    def spawn(*args, **kwargs):
        child = actual(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(launch.subprocess, "Popen", spawn)
    return children


def assert_reaped(children):
    assert len(children) == 1
    child = children[0]
    assert child.returncode is not None
    assert not Path(f"/proc/{child.pid}").exists()
    assert child.stdout.closed and child.stderr.closed


def test_verifier_reads_bounded_json_and_disables_cuda(monkeypatch):
    children = track_children(monkeypatch)
    result = launch._verify_native(
        [
            sys.executable,
            "-c",
            "import json,os; print(json.dumps({'cuda':os.environ['CUDA_VISIBLE_DEVICES']}))",
        ],
        deadline=time.monotonic() + 5,
    )
    assert result == {"cuda": ""}
    assert_reaped(children)


def test_verifier_preserves_pinned_model_environment_without_caller_distribution(
    monkeypatch, tmp_path
):
    metadata = tmp_path / "caller_only-0.0.1.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Name: Caller-Only\nVersion: 0.0.1\n")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    children = track_children(monkeypatch)
    observed = launch._verify_native(
        [
            sys.executable,
            "-c",
            "import importlib.metadata,json; "
            "print(json.dumps({'caller_distribution':any("
            "d.metadata['Name']=='Caller-Only' for d in importlib.metadata.distributions())}))",
        ],
        deadline=time.monotonic() + 5,
    )
    assert observed == {"caller_distribution": False}
    assert os.environ["PYTHONPATH"] == str(tmp_path)
    assert_reaped(children)


@pytest.mark.parametrize("fd", [1, 2])
def test_overflow_on_either_pipe_kills_and_reaps_before_buffering(monkeypatch, fd):
    children = track_children(monkeypatch)
    with pytest.raises(ValueError, match="byte bound"):
        launch._verify_native(
            [sys.executable, "-c", f"import os,time; os.write({fd},b'x'*65537); time.sleep(30)"],
            deadline=time.monotonic() + 5,
        )
    assert_reaped(children)


def test_expired_deadline_does_not_spawn(monkeypatch):
    spawn = Mock()
    monkeypatch.setattr(launch.subprocess, "Popen", spawn)
    with pytest.raises(TimeoutError, match="deadline"):
        launch._verify_native([sys.executable], deadline=time.monotonic() - 1)
    spawn.assert_not_called()


def test_remaining_budget_kills_and_reaps_silent_child(monkeypatch):
    children = track_children(monkeypatch)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="deadline"):
        launch._verify_native(
            [sys.executable, "-c", "import time; time.sleep(30)"], deadline=started + 0.2
        )
    assert time.monotonic() - started < 3
    assert_reaped(children)


@pytest.mark.parametrize("code", ["print('bad json')", "print('[]')", "raise SystemExit(3)"])
def test_invalid_native_readback_fails_and_reaps(monkeypatch, code):
    children = track_children(monkeypatch)
    with pytest.raises(ValueError):
        launch._verify_native([sys.executable, "-c", code], deadline=time.monotonic() + 5)
    assert_reaped(children)


def test_timeout_kills_owned_descendant_holding_output_pipe(monkeypatch, tmp_path):
    children = track_children(monkeypatch)
    pid_file = tmp_path / "owned-child.pid"
    code = (
        "import subprocess,sys,pathlib; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid))"
    )
    with pytest.raises(TimeoutError, match="deadline"):
        launch._verify_native([sys.executable, "-c", code], deadline=time.monotonic() + 0.5)
    assert_reaped(children)
    # The descendant belongs to our new session; it must be dead even if init reaps later.
    status = Path(f"/proc/{pid_file.read_text()}/stat")
    until = time.monotonic() + 1
    while status.exists() and status.read_text().split(") ")[1].split()[0] != "Z":
        assert time.monotonic() < until
        time.sleep(0.005)


def test_launch_shares_original_startup_budget_then_restores_context(monkeypatch):
    now, seen = [100.0], []
    monkeypatch.setattr(launch.time, "monotonic", lambda: now[0])
    main = Mock(side_effect=lambda **_kwargs: seen.append(control_deadline.get()))
    factory = SimpleNamespace(
        expected_peer=object(), confirm_runtime=object(), output_contract=object()
    )
    lab = SimpleNamespace(startup=object())

    def prepare(path, expected, deadline):
        assert (path, expected) == ("review", "pin")
        assert deadline == control_deadline.get() == 105.0
        now[0] += 2
        return SimpleNamespace(main=main), factory, lab

    monkeypatch.setattr(launch, "_prepare_launch", prepare)
    token = control_deadline.set(105.0)
    try:
        launch.launch("review", "pin")
        assert control_deadline.get() == 105.0
    finally:
        control_deadline.reset(token)
    assert seen == [105.0]
    assert main.call_args.kwargs["scientist_verify_lab_capability"] is lab


def test_exhausted_startup_never_enters_desktop(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(launch.time, "monotonic", lambda: now[0])
    main = Mock()

    def prepare(*_args):
        now[0] += 121
        return SimpleNamespace(main=main), object(), object()

    monkeypatch.setattr(launch, "_prepare_launch", prepare)
    with pytest.raises(TimeoutError, match="deadline"):
        launch.launch("review", "pin")
    main.assert_not_called()
    assert control_deadline.get() is None


def test_retained_configuration_absent_does_not_import_or_dispatch():
    assert launch._retained_factory({}, {}, {}, object(), object()) is None


@pytest.mark.parametrize("key", ["retained_review_path", "retained_review_sha256"])
def test_retained_configuration_requires_paired_independent_inputs(key):
    with pytest.raises(ValueError, match="both independent"):
        launch._retained_factory({key: "unreviewed"}, {}, {}, object(), object())


def test_retained_configuration_rejects_incompatible_native_before_factory(monkeypatch):
    monkeypatch.setattr(launch, "pinned_json", lambda *_: {"schema": "synthetic"})
    cfg = {"retained_review_path": "review", "retained_review_sha256": "sha"}
    with pytest.raises(ValueError, match="lacks the native"):
        launch._retained_factory(cfg, {}, {}, SimpleNamespace(main=lambda: None), object())


def test_retained_configuration_rejects_source_without_independent_pin(monkeypatch, tmp_path):
    monkeypatch.setattr(launch, "pinned_json", lambda *_: {"schema": "synthetic"})
    cfg = {"retained_review_path": "review", "retained_review_sha256": "sha"}

    def main(*, scientist_retained_resolver_factory=None):
        pytest.fail("Desktop must not start during preflight")

    with pytest.raises(ValueError, match="independent launch pin"):
        launch._retained_factory(
            cfg,
            {"source_roots": {"scientist": str(tmp_path)}},
            {"source_inputs": {}},
            SimpleNamespace(main=main),
            object(),
        )


def test_launch_passes_only_explicit_configured_retained_hook(monkeypatch):
    main = Mock()
    retained = SimpleNamespace(close=Mock())
    factory = SimpleNamespace(
        expected_peer=object(),
        confirm_runtime=object(),
        output_contract=object(),
        retained_resolver_factory=retained,
    )
    lab = SimpleNamespace(startup=object())
    monkeypatch.setattr(
        launch, "_prepare_launch", lambda *_: (SimpleNamespace(main=main), factory, lab)
    )
    launch.launch("review", "pin")
    assert main.call_args.kwargs["scientist_retained_resolver_factory"] is retained
    retained.close.assert_called_once_with()


@pytest.mark.parametrize("inherited, expected_end", [(None, 103.5), (101.0, 101.0)])
@pytest.mark.parametrize("entry", ["legacy", "entered", "missing", "revoked"])
def test_runtime_rights_native_calls_share_earliest_budget(
    monkeypatch, inherited, expected_end, entry
):
    """Native authentication is mocked; this proves callback deadline wiring only."""
    from dataclasses import asdict
    from types import ModuleType

    from test_aos_native_admission_factory import Admission, Model, Peer

    now, calls = [100.0], []
    monkeypatch.setattr(launch.time, "monotonic", lambda: now[0])
    peer = Peer()
    server = {**asdict(peer), "unit": "swapp-lab-gpu-broker.service"}
    caller = {
        "unit": "swapp-aos-gpu-joint-acceptance.service"
        if entry == "legacy"
        else launch.SHARED_CALLER_UNIT
    }
    pin = {"manifest_sha256": "a" * 64}
    binding = Admission(
        server_generation=server, caller_generation=caller, policy_sha256="b" * 64, profile_pin=pin
    )

    def observe(name, result):
        def operation(*_args, deadline):
            calls.append((name, deadline))
            assert deadline == expected_end == control_deadline.get()
            now[0] += 0.2
            return result

        return operation

    history = ModuleType("aos.scientist_admission_history")
    history.ScientistAdmissionBindingV2 = Admission
    native = ModuleType("aos.scientist_transport")
    native.BrokerPeer = Peer
    native.SystemdBrokerAuthenticator = lambda: SimpleNamespace(
        authenticate=observe("broker", peer), still_current=observe("current", True)
    )
    native.SystemdCallerAuthenticator = lambda: SimpleNamespace(
        authenticate=observe("caller", Model(**caller))
    )
    monkeypatch.setitem(sys.modules, "aos.scientist_admission_history", history)
    monkeypatch.setitem(sys.modules, "aos.scientist_transport", native)
    policy = SimpleNamespace(
        config={
            "enabled": True,
            "history_reconcile": False,
            "caller_unit": caller["unit"],
            "profile_pins": {"profile": pin},
        },
        sha256="b" * 64,
        verify=Mock(),
        verify_policy_hash=Mock(),
    )
    monkeypatch.setattr(launch, "pinned_json", lambda *_args: {})
    monkeypatch.setattr(launch, "load_profiles", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(launch, "ControlPolicy", lambda *_args, **_kwargs: policy)
    configuration = {
        "policy_path": "/fixture/policy",
        "policy_file_sha256": "c" * 64,
        "profile_config_path": "/fixture/profiles",
        "profile_file_sha256": "d" * 64,
        "source_roots": {"aos": "/fixture/aos", "scientist": "/fixture/scientist"},
    }
    runtime = Mock()
    if entry == "revoked":
        runtime.verify.side_effect = ValueError("entry revoked")
    rights = launch.CurrentRuntimeRights(
        {"profile": binding.model_dump()},
        configuration,
        shared_launch_runtime=None if entry == "missing" else runtime,
    )
    if entry != "legacy":
        # Native exclusion is exercised separately using its real consumer.
        monkeypatch.setattr(rights, "_verify_native_exclusion", lambda *_: None)
    token = control_deadline.set(inherited)
    try:
        if entry in {"missing", "revoked"}:
            with pytest.raises(ValueError, match="entered launch|entry revoked"):
                rights({"profile": binding}, {"profile": {"profile": pin}})
        else:
            rights({"profile": binding}, {"profile": {"profile": pin}})
        assert control_deadline.get() == inherited
    finally:
        control_deadline.reset(token)
    assert calls == [(name, expected_end) for name in ["broker", "current", "caller"]]
    if entry in {"entered", "revoked"}:
        runtime.verify.assert_called_once_with(expected_end)
    else:
        runtime.verify.assert_not_called()


def test_unreaped_inert_child_denies_verified_output(monkeypatch):
    """An inert stand-in proves bounded reap failure; no unkillable process is launched."""
    streams = [Mock(), Mock()]
    for fd, stream in enumerate(streams):
        stream.fileno.return_value = fd
    child = SimpleNamespace(
        pid=123456789,
        stdout=streams[0],
        stderr=streams[1],
        returncode=None,
        wait=Mock(side_effect=subprocess.TimeoutExpired("inert-child", 0.2)),
    )

    class InertSelector:
        def __init__(self):
            self.keys = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def register(self, stream, _events, index):
            self.keys[stream] = SimpleNamespace(fd=index, fileobj=stream, data=index)

        def unregister(self, stream):
            del self.keys[stream]

        def get_map(self):
            return self.keys

        def select(self, *, timeout):
            assert timeout <= 0.1
            return [(key, 1) for key in self.keys.values()]

    reads = {0: iter([b'{"verified":true}', b""]), 1: iter([b""])}
    monkeypatch.setattr(launch.subprocess, "Popen", lambda *_args, **_kwargs: child)
    monkeypatch.setattr(launch.selectors, "DefaultSelector", InertSelector)
    monkeypatch.setattr(launch.os, "set_blocking", lambda *_args: None)
    monkeypatch.setattr(launch.os, "read", lambda fd, _limit: next(reads[fd]))
    monkeypatch.setattr(
        launch,
        "_owned_child_status",
        lambda _process: SimpleNamespace(si_code=launch.os.CLD_EXITED, si_status=0),
    )
    monkeypatch.setattr(launch, "_proc_identity", lambda _pid: (b"Z", child.pid, child.pid, 99))
    monkeypatch.setattr(launch, "_group_terminal", lambda *_args: True)
    kill = Mock()
    monkeypatch.setattr(launch.os, "killpg", kill)
    with pytest.raises(ValueError, match="cleanup unverified: original group not drained/reaped"):
        launch._verify_native(["inert-child"], deadline=time.monotonic() + 5)
    kill.assert_called_once_with(child.pid, launch.signal.SIGKILL)
    assert 0 < child.wait.call_args.kwargs["timeout"] <= 0.2
    for stream in streams:
        stream.close.assert_called_once()


def test_group_signal_precedes_leader_reap_on_success(monkeypatch):
    children = track_children(monkeypatch)
    actual_kill = launch.os.killpg

    def signal_owned_group(pid, sig):
        assert children[0].returncode is None, "leader was reaped before group cleanup"
        actual_kill(pid, sig)

    monkeypatch.setattr(launch.os, "killpg", signal_owned_group)
    assert (
        launch._verify_native([sys.executable, "-c", "print('{}')"], deadline=time.monotonic() + 5)
        == {}
    )
    assert_reaped(children)


def test_cleanup_never_signals_an_already_reaped_leader(monkeypatch):
    child = SimpleNamespace(pid=123456789, returncode=0, stdout=Mock(), stderr=Mock(), wait=Mock())
    kill = Mock()
    monkeypatch.setattr(launch.os, "killpg", kill)
    with pytest.raises(ValueError, match="cleanup unverified"):
        launch._cleanup_verifier(child, (b"Z", child.pid, child.pid, 99))
    kill.assert_not_called()
    child.wait.assert_not_called()


def test_live_owned_descendant_denies_cleanup_and_prevents_reap(monkeypatch):
    child = SimpleNamespace(pid=123456789, stdout=Mock(), stderr=Mock(), wait=Mock())
    leader = (b"Z", child.pid, child.pid, 99)
    now = iter([100.0, 101.0])
    monkeypatch.setattr(launch.time, "monotonic", lambda: next(now))
    monkeypatch.setattr(launch, "_owned_child_status", lambda _child: object())
    monkeypatch.setattr(launch, "_proc_identity", lambda _pid: leader)
    monkeypatch.setattr(launch, "_group_terminal", lambda *_args: False)
    kill = Mock()
    monkeypatch.setattr(launch.os, "killpg", kill)
    with pytest.raises(ValueError, match="cleanup unverified"):
        launch._cleanup_verifier(child, leader)
    kill.assert_called_once_with(child.pid, launch.signal.SIGKILL)
    child.wait.assert_not_called()


def test_success_requires_descendant_terminal_even_when_pipes_are_closed(monkeypatch, tmp_path):
    children = track_children(monkeypatch)
    pid_file = tmp_path / "owned-child.pid"
    code = (
        "import subprocess,sys,pathlib; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); print('{{}}')"
    )
    assert launch._verify_native([sys.executable, "-c", code], deadline=time.monotonic() + 5) == {}
    assert_reaped(children)
    status = Path(f"/proc/{pid_file.read_text()}/stat")
    assert not status.exists() or status.read_text().split(") ")[1].split()[0] == "Z"


@pytest.mark.parametrize("inherited", [101.0, 99.0])
def test_joint_constructor_observes_inherited_deadline(monkeypatch, tmp_path, inherited):
    """Constructor wiring fixture; current API identity observation is explicitly mocked."""
    from types import ModuleType

    from scripts import aos_joint_lab_hooks as hooks

    roots = {"aos": str(tmp_path / "aos"), "scientist": str(tmp_path / "scientist")}
    for root in roots.values():
        Path(root).mkdir()
    files = {
        "scientist": [
            "lab/api/app.py",
            "lab/api/registry.py",
            "lab/api/aos_capability.py",
            "scripts/aos_joint_lab_hooks.py",
            "scripts/aos_joint_api_authority.py",
            "scripts/aos_native_launch.py",
        ],
        "aos": [
            "src/aos/scientist_lab.py",
            "src/aos/scientist_lab_service.py",
            "src/aos/scientist_lab_journal.py",
            "src/aos/scientist_lab_readbacks.py",
            "src/aos/scientist_protocol.py",
        ],
    }
    source_inputs = {
        str(Path(roots[k]) / name): "a" * 64 for k, names in files.items() for name in names
    }
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    context_path = tmp_path / "authorization-context.json"
    context = dict(
        schema="scientist.joint-api-authorization-context.v1",
        owner_id="owner",
        origin="aos",
        authority_url="http://127.0.0.1:8767",
        suite_id="suite",
        program_version="1",
        token_sha256="b" * 64,
        api_unit="lab-owned-api.service",
        boot_id=boot,
        issued_boottime=90.0,
        expires_boottime=200.0,
        budget=dict(experiments=1, wall_seconds=900, model_tokens=30000),
        automatic_dispatch=False,
        native_inference_authorized=False,
        gpu_release_authorized=False,
    )
    context_path.write_bytes(json.dumps(context).encode())
    context_sha = hashlib.sha256(context_path.read_bytes()).hexdigest()
    source_inputs[str(context_path)] = context_sha
    startup = SimpleNamespace(
        allowed_suites=frozenset({"suite"}),
        principal_id="owner",
        program_version="1",
        authority_url=context["authority_url"],
        authorization_context_sha256=context_sha,
    )
    review = SimpleNamespace(
        source_roots=roots,
        source_inputs=source_inputs,
        startup={},
        token_sha256="b" * 64,
        api_generation=SimpleNamespace(unit=context["api_unit"], boot_id=boot),
        max_experiments=1,
        max_wall_seconds=900,
        max_model_tokens=30000,
        expected_capabilities={
            "suite": SimpleNamespace(suite_id="suite", owner_id="owner", program_version="1")
        },
    )
    service = ModuleType("aos.scientist_lab_service")
    service.ScientistLabStartup = SimpleNamespace(model_validate_json=lambda *_a, **_k: startup)
    service.prepare_scientist_lab_startup = lambda _startup: (None, object())
    monkeypatch.setitem(sys.modules, "aos.scientist_lab_service", service)
    monkeypatch.setattr(hooks.JointLabCapability, "_review", lambda _self: review)
    current = Mock()
    monkeypatch.setattr(hooks.JointLabCapability, "_current", current)
    monkeypatch.setattr(hooks.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(hooks.time, "clock_gettime", lambda _clock: 100.0)
    token = control_deadline.set(inherited)
    try:
        if inherited > 100:
            hooks.JointLabCapability("unused", "a" * 64)
            current.assert_called_once_with(inherited)
        else:
            with pytest.raises(TimeoutError, match="deadline expired"):
                hooks.JointLabCapability("unused", "a" * 64)
            current.assert_not_called()
    finally:
        control_deadline.reset(token)
