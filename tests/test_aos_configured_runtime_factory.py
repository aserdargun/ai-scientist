"""Pure configured adapter checks; stand-ins do not establish runtime admission."""

from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import aos_configured_runtime_factory as adapter


class Denied(RuntimeError):
    pass


class Binding:
    def __init__(self, **value):
        self.value = deepcopy(value)

    def __getattr__(self, key):
        if key.startswith("__"):
            raise AttributeError(key)
        value = self.value[key]
        return Binding(**value) if isinstance(value, dict) else value

    def model_dump(self, **_options):
        return deepcopy(self.value)

    @classmethod
    def model_validate(cls, value, **_options):
        return cls(**value)

    def __eq__(self, other):
        return isinstance(other, Binding) and self.value == other.value


@dataclass(frozen=True)
class Peer:
    pid: int = 22222
    uid: int = os.getuid()
    start_ticks: int = 17
    boot_id: str = "11111111-1111-1111-1111-111111111111"
    invocation_id: str = "a" * 32
    control_group: str = "/user.slice/swapp-lab-gpu-broker.service"


class SourceVerifier:
    def __init__(self, *arguments, **options):
        self.arguments, self.options = arguments, options

    def __call__(self, selected):
        return self.options["verify_runtime"](selected)


class NativeFactory:
    def __init__(self, *arguments, **options):
        self.arguments, self.options = arguments, options
        self.controller = None
        self.history = object()
        self.expected_peer = Mock(return_value="native-peer")

    def __call__(self, controller):
        self.controller = controller
        return self.history

    def confirm_runtime(self, profiles):
        selected = {profile: self.arguments[0][profile] for profile in profiles}
        return self.options["verify_source"](selected)


def raw_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def configured(tmp_path, monkeypatch):
    roots = {"aos": tmp_path / "aos", "scientist": tmp_path / "scientist"}
    for path in roots.values():
        path.mkdir()
    model_root = tmp_path / "models"
    model_root.mkdir()
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    python = tmp_path / "python"
    python.write_bytes(b"existing executable interpreter fixture")
    python.chmod(0o700)
    output_source = roots["scientist"] / "lab/llm/aos_profile_output.py"
    output_source.parent.mkdir(parents=True)
    output_source.write_bytes(b"reviewed output adapter fixture")
    configs = {}
    bundle = dict(
        name="aos-scientist-profile-output.v2",
        version=2,
        adapter_source_sha256=raw_hash(output_source),
        request_binding_source_sha256=raw_hash(output_source),
        schemas={},
    )
    for name in adapter.SCHEMA_FILES:
        schema = {"title": name, "type": "object"}
        path = output_dir / name
        path.write_bytes(adapter._canonical(schema))
        configs[path] = raw_hash(path)
        bundle["schemas"][name] = adapter._digest(schema)
    bundle_path = output_dir / "bundle.json"
    bundle_path.write_bytes(adapter._canonical(bundle))
    configs[bundle_path] = raw_hash(bundle_path)
    output_pin = dict(name=bundle["name"], version=2, bundle_sha256=adapter._digest(bundle))
    entries, reviewed = {}, {}
    peer = Peer()
    for index, (profile, kind) in enumerate(adapter.PROFILES.items()):
        model, runtime = model_root / (str(index) + "-model"), model_root / (str(index) + "-code")
        model.mkdir()
        runtime.mkdir()
        manifest = dict(
            model_path=str(model),
            model_files={"weights": "a" * 64},
            dependencies={"pinned-package": "1.0"},
        )
        manifest["code_path" if kind == "decider" else "runtime_path"] = str(runtime)
        if kind != "decider":
            manifest.update(temperature=0.0, max_output_tokens=512, context_tokens=1536, parallel=1)
        manifest_path = tmp_path / (str(index) + "-manifest.json")
        manifest_path.write_bytes(adapter._canonical(manifest))
        configs[manifest_path] = raw_hash(manifest_path)
        entry = dict(
            kind=kind,
            manifest=str(manifest_path),
            manifest_sha256=raw_hash(manifest_path),
            deployment_digest=adapter.native_deployment_sha256(
                manifest, kind, {"recovery": "4" * 64, "vision": "5" * 64}
            ),
            python=str(python),
            model_paths=[str(model), str(runtime)],
            budgets=dict(
                activation_seconds=60, inference_seconds=30, total_seconds=90, queue_seconds=120
            ),
            response_schema_sha256=bundle["schemas"][adapter.SCHEMAS[profile]],
            temperature=0.0,
            max_output_tokens=512,
            context_tokens=1536,
            output_contract=output_pin,
        )
        entries[profile] = entry
        reviewed[profile] = Binding(
            profile_id=profile,
            server_generation={**asdict(peer), "unit": "broker.service"},
            caller_generation={"pid": os.getpid()},
            policy_sha256="b" * 64,
            profile_pin=dict(
                deployment_digest=entry["deployment_digest"],
                manifest_sha256=entry["manifest_sha256"],
                response_schema_sha256=entry["response_schema_sha256"],
                output_contract=output_pin,
                config_sha256=adapter._profile_config_sha256(profile, entry, roots["aos"]),
            ),
            infer_schema={"sha256": "1" * 64},
            control_schema={"sha256": "2" * 64},
            terminal_schema={"sha256": "3" * 64},
        )
    socket = tmp_path / "control.sock"
    policy = dict(
        enabled=True,
        history_reconcile=False,
        control_socket=str(socket),
        caller_unit="caller.service",
    )
    policy_path = tmp_path / "policy.json"
    policy_path.write_bytes(adapter._canonical(policy))
    profile_path = tmp_path / "profiles.json"
    config = dict(
        schema="swapp-aos-gpu-profiles.v1", source_root=str(roots["aos"]), profiles=entries
    )
    profile_path.write_bytes(adapter._canonical(config))
    for value in reviewed.values():
        value.value["policy_sha256"] = adapter._digest(policy)
        value.value["caller_generation"]["unit"] = policy["caller_unit"]
    auth = SimpleNamespace(
        authenticate=Mock(return_value=peer), still_current=Mock(return_value=True)
    )
    api = SimpleNamespace(
        native_schema_hashes={"recovery": "4" * 64, "vision": "5" * 64},
        ScientistAdmissionBindingV2=Binding,
        ScientistAdmissionError=Denied,
        SystemdBrokerAuthenticator=Mock(return_value=auth),
        BrokerPeer=Peer,
        BROKER_UNIT="broker.service",
        INFER_DESCRIPTOR_SHA256="1" * 64,
        CONTROL_DESCRIPTOR_SHA256="2" * 64,
        TERMINAL_DESCRIPTOR_SHA256="3" * 64,
        ScientistConfiguredSourceVerifier=SourceVerifier,
        ScientistBootstrapAdmissionFactory=NativeFactory,
        _read=lambda path, *_arguments, **_options: path.read_bytes(),
        _unique_object=dict,
        _reject_constant=Mock(side_effect=ValueError),
    )
    monkeypatch.setattr(adapter, "_load_aos", lambda: api)
    caller = Mock()
    monkeypatch.setattr(adapter, "_verify_caller_service", caller)
    attestation = Mock(return_value=None)
    arguments = dict(
        reviewed_bindings=reviewed,
        policy_path=policy_path,
        profile_config_path=profile_path,
        control_socket_path=socket,
        policy_file_sha256=raw_hash(policy_path),
        profile_file_sha256=raw_hash(profile_path),
        source_roots=roots,
        source_files={"aos": {}, "scientist": {}},
        config_files=configs,
        model_root=model_root,
        output_contract_directory=output_dir,
        verify_artifact_closure=attestation,
    )
    factory = adapter.ConfiguredScientistAdmissionFactory(**arguments)
    selected = {"aos.decider.turn.v1": reviewed["aos.decider.turn.v1"]}
    return SimpleNamespace(**locals())


def test_default_artifact_authority_is_denied_before_import_or_read(monkeypatch):
    load = Mock(side_effect=AssertionError("must not import AOS"))
    monkeypatch.setattr(adapter, "_load_aos", load)
    with pytest.raises(TypeError, match="closure"):
        adapter.ConfiguredScientistAdmissionFactory(
            {},
            "policy",
            "profiles",
            "socket",
            policy_file_sha256="a" * 64,
            profile_file_sha256="b" * 64,
            source_roots={},
            source_files={},
            config_files={},
            model_root="model",
            output_contract_directory="output",
        )
    load.assert_not_called()


def test_native_source_factory_wiring_uses_one_native_authenticator_and_writer(configured):
    ctx = configured
    native, source = ctx.factory._factory, ctx.factory._source
    assert native.options["verify_source"] is source
    assert native.options["authenticator"] is ctx.auth
    assert source.arguments[0] == ctx.policy_path
    assert source.options["verify_runtime"] == ctx.factory._verify_runtime
    assert "persist_intent" not in native.options
    assert "clock" not in native.options
    controller = object()
    assert ctx.factory(controller) is native.history
    assert ctx.factory.expected_peer("request", "binding") == "native-peer"
    native.expected_peer.assert_called_once_with("request", "binding")
    ctx.factory._verify_runtime(ctx.selected)
    assert ctx.caller.call_count == 2
    assert ctx.attestation.call_args.args[0] == ctx.selected
    closure = ctx.attestation.call_args.args[1]["aos.decider.turn.v1"]
    assert closure["manifest"]["dependencies"] == {"pinned-package": "1.0"}
    assert closure["manifest"]["model_files"] == {"weights": "a" * 64}
    assert closure["interpreter"] == str(ctx.python)
    ctx.factory.confirm_runtime(
        {
            "aos.decider.turn.v1": ctx.selected["aos.decider.turn.v1"].value["profile_pin"][
                "deployment_digest"
            ]
        }
    )


def test_output_contract_delegates_native_cross_profile_authority(configured, monkeypatch):
    contract = configured.reviewed["aos.decider.turn.v1"].profile_pin.output_contract
    getter = Mock(side_effect=[contract, Denied("native cross-profile contract mismatch")])
    monkeypatch.setattr(NativeFactory, "output_contract", property(getter), raising=False)
    assert configured.factory.output_contract is contract
    getter.assert_called_once_with(configured.factory._factory)
    with pytest.raises(Denied, match="cross-profile"):
        _ = configured.factory.output_contract


def test_unicode_manifest_uses_native_ascii_deployment_identity(configured):
    ctx = configured
    profile = "aos.decider.turn.v1"
    path = ctx.tmp_path / "0-manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["description"] = "Türkçe ölçüm"
    path.write_bytes(adapter._canonical(manifest))
    native_deployment = hashlib.sha256(
        json.dumps(
            manifest, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    assert native_deployment != adapter._digest(manifest)
    entry = ctx.config["profiles"][profile]
    entry["manifest_sha256"], entry["deployment_digest"] = raw_hash(path), native_deployment
    pin = ctx.reviewed[profile].value["profile_pin"]
    pin["manifest_sha256"], pin["deployment_digest"] = raw_hash(path), native_deployment
    pin["config_sha256"] = adapter._profile_config_sha256(profile, entry, ctx.roots["aos"])
    ctx.configs[path] = raw_hash(path)
    ctx.profile_path.write_bytes(adapter._canonical(ctx.config))
    ctx.arguments["profile_file_sha256"] = raw_hash(ctx.profile_path)
    factory = adapter.ConfiguredScientistAdmissionFactory(**ctx.arguments)
    factory._verify_runtime({profile: ctx.reviewed[profile]})
    assert ctx.attestation.call_args.args[1][profile]["manifest"]["description"] == "Türkçe ölçüm"


@pytest.mark.parametrize(
    "field,value",
    [
        ("enabled", False),
        ("history_reconcile", True),
        ("caller_unit", "foreign.service"),
        ("control_socket", "/foreign/control.sock"),
    ],
)
def test_changed_or_disabled_policy_denies(configured, field, value):
    configured.policy[field] = value
    configured.policy_path.write_bytes(adapter._canonical(configured.policy))
    with pytest.raises(Denied, match="receipt"):
        configured.factory._verify_runtime(configured.selected)
    configured.attestation.assert_not_called()


def test_wrong_selected_profile_or_full_binding_denies(configured):
    with pytest.raises(Denied, match="selection"):
        configured.factory._verify_runtime({"unknown": configured.selected["aos.decider.turn.v1"]})
    changed = deepcopy(configured.selected)
    changed["aos.decider.turn.v1"].value["policy_sha256"] = "f" * 64
    with pytest.raises(Denied, match="selection"):
        configured.factory._verify_runtime(changed)


@pytest.mark.parametrize(
    "field,value",
    [
        ("kind", "bonsai-vision"),
        ("deployment_digest", "f" * 64),
        ("response_schema_sha256", "f" * 64),
        ("model_paths", ["/wrong", "/wrong"]),
    ],
)
def test_config_profile_or_deployment_cannot_replace_review(configured, field, value):
    configured.config["profiles"]["aos.decider.turn.v1"][field] = value
    configured.profile_path.write_bytes(adapter._canonical(configured.config))
    with pytest.raises(Denied, match="receipt"):
        configured.factory._verify_runtime(configured.selected)


@pytest.mark.parametrize("artifact", ["manifest", "output-schema"])
def test_changed_model_manifest_and_output_schema_deny(configured, artifact):
    path = (
        configured.tmp_path / "0-manifest.json"
        if artifact == "manifest"
        else configured.output_dir / "bonsai-response.schema.json"
    )
    path.write_bytes(b'{"changed":true}')
    with pytest.raises(Denied, match="receipt"):
        configured.factory._verify_runtime(configured.selected)


@pytest.mark.parametrize("artifact", ["model-root", "interpreter"])
def test_missing_model_root_or_interpreter_deny(configured, artifact):
    if artifact == "interpreter":
        configured.python.unlink()
    else:
        (configured.model_root / "0-model").rmdir()
    with pytest.raises(OSError if artifact == "interpreter" else Denied):
        configured.factory._verify_runtime(configured.selected)
    configured.attestation.assert_not_called()


def test_actual_broker_generation_and_caller_generation_deny(configured):
    configured.auth.authenticate.return_value = replace(configured.peer, start_ticks=99)
    with pytest.raises(Denied, match="native service generation"):
        configured.factory._verify_runtime(configured.selected)
    configured.auth.authenticate.return_value = configured.peer
    configured.caller.side_effect = Denied("actual caller process generation revoked")
    with pytest.raises(Denied, match="caller process"):
        configured.factory._verify_runtime(configured.selected)
    configured.attestation.assert_not_called()


def test_independent_dependency_weight_or_rights_revocation_denies(configured):
    configured.attestation.side_effect = Denied("independent model/dependency/rights revoked")
    with pytest.raises(Denied, match="revoked"):
        configured.factory._verify_runtime(configured.selected)


def test_boolean_attestation_cannot_replace_complete_verification(configured):
    configured.attestation.return_value = True
    with pytest.raises(Denied, match="complete or raise"):
        configured.factory._verify_runtime(configured.selected)


def test_attestation_cannot_hide_changed_broker_generation(configured):
    def change_generation(*_arguments):
        configured.auth.still_current.return_value = False

    configured.attestation.side_effect = change_generation
    with pytest.raises(Denied, match="generation changed"):
        configured.factory._verify_runtime(configured.selected)


def test_bonsai_native_enriched_identity_is_required(configured):
    ctx = configured
    profile = "aos.bonsai.vision.v1"
    entry = ctx.config["profiles"][profile]
    manifest = json.loads(Path(entry["manifest"]).read_bytes())
    enriched = dict(
        manifest,
        recovery_schema_sha256="4" * 64,
        recovery_protocol="aos-bonsai-recovery-v1",
        vision_schema_sha256="5" * 64,
        vision_protocol="aos-bonsai-vision-v1",
    )
    deployment = adapter._manifest_deployment_sha256(enriched)
    entry["deployment_digest"] = deployment
    pin = ctx.reviewed[profile].value["profile_pin"]
    pin["deployment_digest"] = deployment
    pin["config_sha256"] = adapter._profile_config_sha256(profile, entry, ctx.roots["aos"])
    ctx.profile_path.write_bytes(adapter._canonical(ctx.config))
    ctx.arguments["profile_file_sha256"] = raw_hash(ctx.profile_path)
    factory = adapter.ConfiguredScientistAdmissionFactory(**ctx.arguments)
    factory._verify_runtime({profile: ctx.reviewed[profile]})


@pytest.mark.parametrize("profile", ["aos.bonsai.recovery.v1", "aos.bonsai.vision.v1"])
def test_raw_bonsai_identity_is_denied_even_when_review_and_config_agree(configured, profile):
    ctx = configured
    entry = ctx.config["profiles"][profile]
    manifest = json.loads(Path(entry["manifest"]).read_bytes())
    entry["deployment_digest"] = adapter._manifest_deployment_sha256(manifest)
    pin = ctx.reviewed[profile].value["profile_pin"]
    pin["deployment_digest"] = entry["deployment_digest"]
    pin["config_sha256"] = adapter._profile_config_sha256(profile, entry, ctx.roots["aos"])
    ctx.profile_path.write_bytes(adapter._canonical(ctx.config))
    ctx.arguments["profile_file_sha256"] = raw_hash(ctx.profile_path)
    factory = adapter.ConfiguredScientistAdmissionFactory(**ctx.arguments)
    with pytest.raises(Denied, match="canonical deployment"):
        factory._verify_runtime({profile: ctx.reviewed[profile]})


def test_native_identity_matches_actual_cpu_supervisors(tmp_path):
    import subprocess

    from scripts.prepare_aos_native_configuration import native_schema_hashes

    root = Path("/home/cachyos/aos")
    python = root / ".venv/bin/python"
    if not python.exists():
        pytest.skip("Actual AOS source checkout/interpreter unavailable")
    manifest = {
        "description": "Türkçe",
        "recovery_protocol": "obsolete",
        "vision_schema_sha256": "old",
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    schemas = native_schema_hashes(root, python)
    code = """import sys,json
sys.path.insert(0,sys.argv[1])
from pathlib import Path
from aos.scientist_supervisor import ScientistBonsaiSupervisor,ScientistBonsaiVisionSupervisor
print(json.dumps([cls(Path(sys.argv[2]),None)._deployment_digest
                  for cls in (ScientistBonsaiSupervisor,ScientistBonsaiVisionSupervisor)]))
"""
    result = subprocess.run(
        [str(python), "-B", "-I", "-c", code, str(root / "src"), str(path)],
        check=True,
        capture_output=True,
        timeout=15,
        env={**os.environ, "CUDA_VISIBLE_DEVICES": ""},
    )
    actual = json.loads(result.stdout)
    assert actual == [
        adapter.native_deployment_sha256(manifest, kind, schemas)
        for kind in ("bonsai-recovery", "bonsai-vision")
    ]
    assert actual[0] != actual[1] != adapter._manifest_deployment_sha256(manifest)
    with pytest.raises(ValueError, match="Unknown"):
        adapter.native_deployment_sha256(manifest, "wrong-kind", schemas)
    with pytest.raises(ValueError, match="SHA-256"):
        adapter.native_deployment_sha256(manifest, "bonsai-vision", {**schemas, "vision": "bad"})
