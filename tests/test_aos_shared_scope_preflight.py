"""Read-only shared caller checks on real private fixtures; no runtime/model authority."""

import hashlib
import importlib.util
import json
import os
import signal
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_aos_native_launch import receipt_launch as receipt_launch

from scripts import aos_native_launch as launch


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def test_shared_builder_receives_only_the_explicit_reviewed_scientist_root(tmp_path):
    plan, activation = object(), object()
    observed = []

    class Transport:
        def __init__(self, *, scientist_root):
            observed.append(scientist_root)

        def launch_command(self, selected_plan, selected_activation):
            assert (selected_plan, selected_activation) == (plan, activation)
            return ["reviewed-command"]

    assert launch._shared_launch_command(Transport, plan, activation, str(tmp_path)) == [
        "reviewed-command"
    ]
    assert observed == [tmp_path]


@pytest.mark.parametrize("api", ["legacy-static", "implicit-kwargs", "wrong-builder"])
def test_unsupported_shared_transport_api_fails_without_default_root_fallback(tmp_path, api):
    constructed = Mock()
    builder = Mock()

    class LegacyTransport:
        def __init__(self):
            constructed()

        launch_command = staticmethod(builder)

    class ImplicitTransport:
        def __init__(self, **kwargs):
            constructed(**kwargs)

        launch_command = staticmethod(builder)

    class WrongBuilderTransport:
        def __init__(self, *, scientist_root):
            constructed(scientist_root=scientist_root)

        def launch_command(self, plan):
            builder(plan)

    selected = {
        "legacy-static": LegacyTransport,
        "implicit-kwargs": ImplicitTransport,
        "wrong-builder": WrongBuilderTransport,
    }[api]
    with pytest.raises(ValueError, match="Unsupported shared Desktop"):
        launch._shared_launch_command(selected, object(), object(), str(tmp_path))
    if api != "wrong-builder":
        constructed.assert_not_called()
    builder.assert_not_called()


@pytest.fixture
def shared_launch(receipt_launch, monkeypatch, tmp_path):
    """The scope validators/builders are real; all native/service hooks stay inert."""
    manifest_path = os.environ.get("LAB_SHARED_TEST_AOS_SOURCE_MANIFEST")
    manifest_sha = os.environ.get("LAB_SHARED_TEST_AOS_SOURCE_MANIFEST_SHA256")
    if manifest_path is None and manifest_sha is None:
        pytest.skip("Opt-in AOS integration requires its reviewed source manifest and SHA256")
    if not manifest_path or not manifest_sha:
        pytest.fail("Opt-in AOS integration requires both source manifest path and SHA256")
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        pytest.fail("Opt-in AOS integration requires the native pidfd-capable AOS interpreter")
    # Explicit opt-in never masks import errors or source pin failures as integration skips.
    import aos
    from aos.contracts import canonical, digest
    from aos.shared_desktop_host import SystemdSharedDesktopTransport
    from aos.shared_desktop_plan import (
        SharedDesktopActivation,
        SharedDesktopLimits,
        SharedDesktopTemplate,
        prepare_plan,
    )
    from aos.shared_desktop_provision import claim_launch, load_provision, provision_scope

    case = receipt_launch
    # Broker entry transport has its own real socket/ledger tests. This source
    # compatibility fixture never contacts a broker or launches a native worker.
    case.shared_runtime = Mock()
    monkeypatch.setattr(launch, "_shared_launch_runtime", Mock(return_value=case.shared_runtime))
    root = Path(aos.__file__).resolve().parents[2]
    script = root / "scripts/serve_desktop.py"
    spec = importlib.util.spec_from_file_location("shared_preflight_fixture_desktop", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # Definitions only: never invoke its Desktop main.
    sources = {str(script.relative_to(root)): sha(script.read_bytes())}
    for name, imported in tuple(sys.modules.items()):
        if name == "aos" or name.startswith("aos."):
            path = Path(imported.__file__).absolute()
            sources[str(path.relative_to(root))] = sha(path.read_bytes())
    # Bind the real helper fixture to the externally reviewed immutable source closure.
    manifest_raw = Path(manifest_path).read_bytes()
    assert sha(manifest_raw) == manifest_sha
    manifest = json.loads(manifest_raw)
    selected = manifest["source_sha256"]
    assert manifest["source_count"] == len(selected) <= 256
    assert sources.items() <= selected.items(), "Reviewed closure omitted a loaded dependency"
    assert all(sha((root / name).read_bytes()) == pin for name, pin in selected.items())
    sources = selected
    args = case.cfg["factory_arguments"]
    scientist_root = Path(args["source_roots"]["scientist"])
    launcher = scientist_root / "scripts/aos_native_launch.py"
    launcher.write_bytes(Path(launch.__file__).read_bytes())
    args["source_roots"]["aos"] = str(root)
    args["source_files"]["aos"] = sources
    static_path = Path(case.cfg["artifact_input_path"])
    static = json.loads(static_path.read_bytes())
    static["source_inputs"].update(
        {str(root / name): checksum for name, checksum in sources.items()}
    )
    static["source_inputs"][str(launcher)] = sha(launcher.read_bytes())

    def pin(path, value):
        raw = canonical(value).encode()
        path.write_bytes(raw)
        path.chmod(0o600)
        return sha(raw)

    manager = tmp_path / "manager"
    manager.mkdir(mode=0o700)
    binary = tmp_path / "inert-python"
    binary.write_bytes(b"# never executed fixture\n")
    binary.chmod(0o600)
    config = tmp_path / "inert-config.json"
    config_sha = pin(config, {"fixture": True})
    template_sources = {str(root / name): checksum for name, checksum in sources.items()}
    template_sources.update(
        {
            str(binary): sha(binary.read_bytes()),
            str(launcher): sha(launcher.read_bytes()),
        }
    )
    template = SharedDesktopTemplate(
        origin="http://127.0.0.1:8765",
        manager_base=str(manager),
        session_root=str(manager),
        python_path=str(binary),
        python_sha256=sha(binary.read_bytes()),
        launcher_path=str(launcher),
        launcher_sha256=sha(launcher.read_bytes()),
        source_files=template_sources,
        source_sha256=digest(template_sources),
        config_files={str(config): config_sha},
        config_sha256=digest({str(config): config_sha}),
        broker_socket=str(tmp_path / "inert-broker.sock"),
        broker_identity_sha256="d" * 64,
        limits=SharedDesktopLimits(
            cpu_quota_percent=200,
            memory_max_bytes=1073741824,
            tasks_max=128,
            stop_timeout_seconds=5,
        ),
    )
    plan = prepare_plan(template, predecessor=None, new_session="app-" + "1" * 32)
    plan_path = tmp_path / "plan.json"
    plan_sha = pin(plan_path, plan.model_dump(mode="json"))
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    # Real filesystem-only provisioning and claim helpers; no service or runtime is launched.
    provision = provision_scope(plan, current_boot_id=boot)
    provision_path = Path(plan.session_directory) / "shared-provision.json"
    provision_sha = provision.provision_sha256()
    assert load_provision(plan, provision_path, provision_sha, current_boot_id=boot) == provision
    activation_path, launch_path = tmp_path / "activation.json", tmp_path / "launch.json"
    case.cfg["caller_unit"] = launch.SHARED_CALLER_UNIT
    case.cfg["shared_scope"] = {
        "plan_path": str(plan_path),
        "plan_sha256": plan_sha,
        "provision_path": str(provision_path),
        "provision_sha256": provision_sha,
        "activation_path": str(activation_path),
    }
    # Only exercise source/config wiring here. The admission factory is inert and never
    # consumes this deliberately non-authoritative request as a maintenance receipt.
    case.cfg["native_exclusion"] = {
        "schema": "scientist.native-exclusion-review.v1",
        "request": {"fixture": "no maintenance or execution authority"},
        "receipt_sha256": "9" * 64,
        "legacy_state_path": str(tmp_path / "inert-current.json"),
        "candidate_manifest_path": str(root / "scripts/shared-only-runtime-v1/manifest.json"),
        "candidate_patch_path": str(root / "scripts/shared-only-runtime-v1/source.patch.txt"),
        "promoted_source_files": dict(static["source_inputs"]),
    }
    case.cfg["artifact_validity_seconds"] = 900
    case.review["authority"]["caller_unit"] = launch.SHARED_CALLER_UNIT
    case.review["authority"]["expires_boottime"] = 600.0
    case.review["config_files"] = {str(provision_path): provision_sha}
    for binding in case.bindings.values():
        binding["caller_generation"]["unit"] = launch.SHARED_CALLER_UNIT
    args["config_files"] = {str(provision_path): provision_sha}
    case.cfg["retained_review_path"] = str(case.review_path)
    case.cfg["retained_review_sha256"] = pin(case.review_path, case.review)
    case.cfg["artifact_input_sha256"] = pin(static_path, static)
    launch_sha = pin(launch_path, case.cfg)
    activation_files = {
        str(launch_path): launch_sha,
        str(provision_path): provision_sha,
        str(case.review_path): case.cfg["retained_review_sha256"],
    }
    activation = SharedDesktopActivation(
        plan_sha256=plan_sha,
        reviewed_launch_input_path=str(launch_path),
        reviewed_launch_input_sha256=launch_sha,
        source_files=template_sources,
        source_sha256=digest(template_sources),
        config_files=activation_files,
        config_sha256=digest(activation_files),
        boot_id=boot,
        issued_monotonic=90.0,
        expires_monotonic=600.0,
        capability_proof_sha256="e" * 64,
        source_proof_sha256="f" * 64,
    )
    activation_sha = pin(activation_path, activation.model_dump(mode="json"))
    claim_launch(plan, provision, provision_sha, activation_sha, current_boot_id=boot)
    command = SystemdSharedDesktopTransport(scientist_root=scientist_root).launch_command(
        plan, activation
    )
    argv = command[command.index("--expected-launch-input-sha256") + 2 :]
    monkeypatch.setattr(sys, "argv", ["aos_native_launch.py", *argv])
    case.plan, case.provision, case.activation = plan, provision, activation
    case.static, case.launch_path, case.launch_sha = static, launch_path, launch_sha
    case.activation_path, case.pin, case.argv = activation_path, pin, argv
    case.guard = lambda: launch._shared_scope_preflight(
        case.cfg, args, static, launch_path, case.launch_sha, case.deadline
    )
    case.prepare = lambda: launch._prepare_launch(
        launch_path, pin(launch_path, case.cfg), case.deadline
    )

    def repin_activation(fields):
        changed = activation.model_copy(update=fields)
        changed_sha = pin(activation_path, changed.model_dump(mode="json"))
        from aos.shared_desktop_provision import LAUNCH_INTENT_NAME

        marker_path = Path(plan.session_directory) / LAUNCH_INTENT_NAME
        marker = json.loads(marker_path.read_bytes())
        marker["activation_sha256"] = changed_sha
        pin(marker_path, marker)

    case.repin_activation = repin_activation
    return case


@pytest.mark.parametrize("missing", ["manifest", "sha256", "native-pidfd"])
def test_explicit_opt_in_requires_complete_pins_and_native_interpreter(
    monkeypatch, request, missing
):
    monkeypatch.setenv("LAB_SHARED_TEST_AOS_SOURCE_MANIFEST", "/unused-reviewed-manifest.json")
    monkeypatch.setenv("LAB_SHARED_TEST_AOS_SOURCE_MANIFEST_SHA256", "a" * 64)
    if missing == "manifest":
        monkeypatch.delenv("LAB_SHARED_TEST_AOS_SOURCE_MANIFEST")
    elif missing == "sha256":
        monkeypatch.delenv("LAB_SHARED_TEST_AOS_SOURCE_MANIFEST_SHA256")
    else:
        monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)
    with pytest.raises(pytest.fail.Exception, match="Opt-in AOS integration requires"):
        request.getfixturevalue("shared_launch")


def test_exact_pristine_claim_is_read_only_and_preserves_legacy_authority_gates(shared_launch):
    case = shared_launch
    before = {
        path: path.read_bytes()
        for path in (
            case.launch_path,
            case.activation_path,
            Path(case.cfg["shared_scope"]["provision_path"]),
            Path(case.plan.session_directory) / "shared-launch-intent.json",
        )
    }
    case.guard()
    assert {path: path.read_bytes() for path in before} == before
    assert not Path(case.plan.database).exists() and os.listdir(case.plan.workspace) == []
    _module, factory, _lab = case.prepare()
    factory.verify_shared_scope_before_main()
    case.capture.assert_called_once()
    case.native.assert_called_once()
    case.shared_runtime.enter.assert_called_once_with(case.deadline)
    assert case.shared_runtime.verify.call_count == 2
    assert json.loads(case.output.read_bytes())["expires_boottime"] == 600.0


def test_shared_entry_denial_prevents_native_work_and_receipt(shared_launch):
    case = shared_launch
    case.shared_runtime.enter.side_effect = ValueError("entry revoked")
    with pytest.raises(ValueError, match="entry revoked"):
        case.prepare()
    case.native.assert_not_called()
    case.shared_runtime.verify.assert_not_called()
    assert not case.output.exists()


def test_shared_revocation_after_native_verification_prevents_receipt(shared_launch):
    case = shared_launch

    def revoke():
        case.shared_runtime.enter.assert_called_once_with(case.deadline)
        case.shared_runtime.verify.side_effect = ValueError("entry revoked")

    case.on_verify = revoke
    with pytest.raises(ValueError, match="entry revoked"):
        case.prepare()
    case.native.assert_called_once()
    assert not case.output.exists()


def test_shared_revocation_before_desktop_is_rechecked(shared_launch):
    case = shared_launch
    _module, factory, _lab = case.prepare()
    case.shared_runtime.verify.side_effect = ValueError("entry revoked")
    with pytest.raises(ValueError, match="entry revoked"):
        factory.verify_shared_scope_before_main()
    assert case.shared_runtime.verify.call_count == 2


def test_unsupported_transport_fails_before_native_verification_or_receipt(
    shared_launch, monkeypatch
):
    import aos.shared_desktop_host

    case = shared_launch
    fallback = Mock()

    class UnsupportedTransport:
        def __init__(self):
            fallback()

    monkeypatch.setattr(
        aos.shared_desktop_host, "SystemdSharedDesktopTransport", UnsupportedTransport
    )
    with pytest.raises(ValueError, match="explicit scientist_root is required"):
        case.prepare()
    fallback.assert_not_called()
    case.native.assert_not_called()
    assert not case.output.exists()


def test_shared_builder_does_not_adopt_the_aos_default_scientist_root(
    shared_launch, monkeypatch, tmp_path
):
    import aos.shared_desktop_host

    case = shared_launch
    roots = case.cfg["factory_arguments"]["source_roots"]
    monkeypatch.setattr(aos.shared_desktop_host, "SCIENTIST_ROOT", Path(roots["scientist"]))
    different_root = tmp_path / "other-scientist"
    different_root.mkdir()
    roots["scientist"] = str(different_root)
    with pytest.raises(ValueError, match="independently configured Scientist source root"):
        case.guard()
    case.native.assert_not_called()
    assert not case.output.exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "extra",
        "relative",
        "traversal",
        "control",
        "uppercase-sha",
        "partial",
    ],
)
def test_shared_scope_contract_rejects_implicit_or_noncanonical_selection_before_capture(
    shared_launch, mutation
):
    case = shared_launch
    scope = case.cfg["shared_scope"]
    if mutation == "missing":
        del case.cfg["shared_scope"]
    elif mutation == "extra":
        scope["activation_sha256"] = "a" * 64
    elif mutation == "relative":
        scope["plan_path"] = "relative.json"
    elif mutation == "traversal":
        scope["plan_path"] = "/tmp/../plan.json"
    elif mutation == "control":
        scope["plan_path"] = "/tmp/plan\n.json"
    elif mutation == "uppercase-sha":
        scope["plan_sha256"] = "A" * 64
    else:
        del scope["provision_sha256"]
    with pytest.raises(ValueError):
        case.prepare()
    case.capture.assert_not_called()
    case.native.assert_not_called()


def test_legacy_acceptance_rejects_shared_scope_before_capture(receipt_launch):
    case = receipt_launch
    case.cfg["shared_scope"] = {}
    with pytest.raises(ValueError, match="Legacy acceptance"):
        case.prepare()
    case.capture.assert_not_called()


@pytest.mark.parametrize("mutation", ["extra", "duplicate", "abbreviation", "default-path", "port"])
def test_builder_exact_argv_rejects_extra_duplicate_abbreviated_and_default_inputs(
    shared_launch, monkeypatch, mutation
):
    case = shared_launch
    argv = list(case.argv)
    if mutation == "extra":
        argv.append("--native-fallback")
    elif mutation == "duplicate":
        argv.extend(["--database", case.plan.database])
    elif mutation == "abbreviation":
        argv[argv.index("--workspace")] = "--work"
    elif mutation == "default-path":
        argv[argv.index("--knowledge-root") + 1] = "/tmp/aos-default-knowledge"
    else:
        argv[argv.index("--port") + 1] = "8766"
    monkeypatch.setattr(sys, "argv", [sys.argv[0], *argv])
    with pytest.raises(ValueError, match="argv"):
        case.guard()
    case.native.assert_not_called()


@pytest.mark.parametrize(
    "mutation",
    [
        "dirty-workspace",
        "database",
        "session-extra",
        "mode",
        "replace-workspace",
        "symlink",
    ],
)
def test_claimed_scope_does_not_adopt_runtime_files_or_replaced_directories(
    shared_launch, mutation
):
    case = shared_launch
    workspace = Path(case.plan.workspace)
    if mutation == "dirty-workspace":
        (workspace / "unowned.txt").write_text("fixture")
    elif mutation == "database":
        Path(case.plan.database).write_bytes(b"not a database")
    elif mutation == "session-extra":
        (workspace.parent / "extra.json").write_bytes(b"{}")
    elif mutation == "mode":
        workspace.chmod(0o755)
    else:
        workspace.rename(workspace.parent / "old-workspace")
        if mutation == "symlink":
            workspace.symlink_to(workspace.parent / "old-workspace", target_is_directory=True)
        else:
            workspace.mkdir(mode=0o700)
    with pytest.raises((ValueError, OSError)):
        case.guard()
    case.native.assert_not_called()


@pytest.mark.parametrize("mutation", ["expired", "wrong-boot", "launch-sha", "source-map"])
def test_marker_bound_activation_keeps_exact_launch_boot_source_and_original_expiry(
    shared_launch, mutation
):
    case = shared_launch
    if mutation == "expired":
        case.now = 600.0
    elif mutation == "wrong-boot":
        case.repin_activation({"boot_id": "00000000-0000-0000-0000-000000000001"})
    elif mutation == "launch-sha":
        changed = dict(case.activation.config_files)
        changed[str(case.launch_path)] = "0" * 64
        from aos.contracts import digest

        case.repin_activation(
            {
                "reviewed_launch_input_sha256": "0" * 64,
                "config_files": changed,
                "config_sha256": digest(changed),
            }
        )
    else:
        changed = dict(case.activation.source_files)
        changed[next(iter(changed))] = "0" * 64
        from aos.contracts import digest

        case.repin_activation({"source_files": changed, "source_sha256": digest(changed)})
    with pytest.raises(ValueError, match="activation"):
        case.guard()


@pytest.mark.parametrize(
    "mutation", ["factory-pin", "activation-pin", "retained-pin", "retained-map", "partial"]
)
def test_receipt_is_pinned_in_all_present_config_closures(shared_launch, mutation):
    case = shared_launch
    key = case.cfg["shared_scope"]["provision_path"]
    if mutation == "factory-pin":
        case.cfg["factory_arguments"]["config_files"][key] = "0" * 64
    elif mutation == "activation-pin":
        from aos.contracts import digest

        files = dict(case.activation.config_files, **{key: "0" * 64})
        case.repin_activation({"config_files": files, "config_sha256": digest(files)})
    elif mutation == "retained-pin":
        case.cfg["retained_review_sha256"] = "0" * 64
    elif mutation == "retained-map":
        case.review["config_files"][key] = "0" * 64
        changed_sha = case.pin(case.review_path, case.review)
        case.cfg["retained_review_sha256"] = changed_sha
        from aos.contracts import digest

        files = dict(case.activation.config_files, **{str(case.review_path): changed_sha})
        case.repin_activation({"config_files": files, "config_sha256": digest(files)})
    else:
        del case.cfg["retained_review_sha256"]
    # Keep the launch pin coherent so each test reaches its own selected config gate.
    case.launch_sha = case.pin(case.launch_path, case.cfg)
    from aos.contracts import digest

    files = json.loads(case.activation_path.read_bytes())["config_files"]
    files[str(case.launch_path)] = case.launch_sha
    case.repin_activation(
        {
            "reviewed_launch_input_sha256": case.launch_sha,
            "config_files": files,
            "config_sha256": digest(files),
        }
    )
    with pytest.raises(ValueError):
        case.guard()


def test_no_retained_pair_keeps_two_receipt_pins_without_creating_authority(shared_launch):
    case = shared_launch
    del case.cfg["retained_review_path"], case.cfg["retained_review_sha256"]
    case.launch_sha = case.pin(case.launch_path, case.cfg)
    from aos.contracts import digest

    files = {
        str(case.launch_path): case.launch_sha,
        case.cfg["shared_scope"]["provision_path"]: case.cfg["shared_scope"]["provision_sha256"],
    }
    case.repin_activation(
        {
            "reviewed_launch_input_sha256": case.launch_sha,
            "config_files": files,
            "config_sha256": digest(files),
        }
    )
    case.guard()
    case.native.assert_not_called()


def test_transitive_source_tamper_is_rejected_before_helper_import(shared_launch, monkeypatch):
    case = shared_launch
    relative = "src/aos/lifecycle.py"
    case.cfg["factory_arguments"]["source_files"]["aos"][relative] = "0" * 64
    case.static["source_inputs"][
        str(Path(case.cfg["factory_arguments"]["source_roots"]["aos"]) / relative)
    ] = "0" * 64
    real_import = __import__
    observed = []

    def watched_import(name, *args, **kwargs):
        if name.startswith("aos"):
            observed.append(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", watched_import)
    with pytest.raises(ValueError, match="source pins"):
        case.guard()
    assert observed == []


def test_second_guard_blocks_workspace_replacement_immediately_before_main(
    shared_launch, monkeypatch
):
    case = shared_launch
    module, factory, lab = case.prepare()
    factory.expected_peer, factory.output_contract = object(), object()
    lab.startup = object()
    main = Mock()
    module.main = main
    monkeypatch.setattr(launch, "_prepare_launch", lambda *a: (module, factory, lab))
    workspace = Path(case.plan.workspace)
    workspace.rename(workspace.parent / "prior-workspace")
    workspace.mkdir(mode=0o700)
    with pytest.raises(ValueError):
        launch.launch(case.launch_path, case.launch_sha)
    main.assert_not_called()


@pytest.mark.parametrize("mutation", ["marker-bytes", "marker-boot", "provision-hash", "plan-hash"])
def test_marker_and_receipt_raw_hashes_cannot_be_replaced_or_rebound(shared_launch, mutation):
    case = shared_launch
    marker = Path(case.plan.session_directory) / "shared-launch-intent.json"
    if mutation == "marker-bytes":
        marker.write_bytes(b" " + marker.read_bytes())
    elif mutation == "marker-boot":
        fields = json.loads(marker.read_bytes())
        fields["boot_id"] = "00000000-0000-0000-0000-000000000001"
        case.pin(marker, fields)
    elif mutation == "provision-hash":
        path = Path(case.cfg["shared_scope"]["provision_path"])
        path.write_bytes(path.read_bytes() + b" ")
    else:
        case.cfg["shared_scope"]["plan_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        case.guard()
    case.native.assert_not_called()


def test_loaded_helper_origin_and_original_startup_deadline_are_fail_closed(
    shared_launch, monkeypatch
):
    import aos.contracts

    case = shared_launch
    monkeypatch.setattr(aos.contracts, "__file__", "/tmp/unreviewed-aos-contracts.py")
    with pytest.raises(ValueError, match="another source root"):
        case.guard()
    case.deadline = time.monotonic() - 1
    with pytest.raises(TimeoutError, match="startup deadline"):
        case.guard()
    case.native.assert_not_called()
