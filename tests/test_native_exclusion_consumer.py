"""Consumer decisions and actual callback wiring; AOS/process observations are inert stand-ins.

These tests do not establish the producer's physical absence or GPU coexistence claims.
"""

import hashlib
import json
import os
import sys
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
from test_aos_native_admission_factory import Peer
from test_aos_native_launch import receipt_launch as receipt_launch

from lab.llm.aos_gpu_control_store import control_deadline
from scripts import aos_native_launch as launch


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def plain(value):
    if isinstance(value, Wire):
        return value.model_dump()
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    return deepcopy(value)


class Wire:
    """Typed AOS boundaries are mocked; the production consumer performs its own comparisons."""

    def __init__(self, **value):
        self.value = plain(value)

    def __getattr__(self, name):
        value = self.value[name]
        if name in {
            "store",
            "legacy",
            "shared_caller",
            "process",
            "caller_generation",
            "server_generation",
            "profile_pin",
        }:
            return Wire(**value)
        return value

    @classmethod
    def model_validate(cls, value, **_):
        return cls(**value)

    def model_dump(self, **_):
        return deepcopy(self.value)

    def __eq__(self, other):
        return isinstance(other, Wire) and self.value == other.value


@pytest.fixture
def exclusion(monkeypatch, tmp_path):
    # The files are real and hash-pinned. Their modules and native observations are inert.
    modules = {}
    for name in (
        "contracts",
        "native_exclusion",
        "native_maintenance",
        "native_handover",
        "lifecycle",
        "shared_desktop_host",
        "shared_desktop_plan",
        "scientist_admission_history",
        "scientist_transport",
    ):
        module = ModuleType("aos." + name)
        module.__file__ = str(tmp_path / "aos/src/aos" / (name + ".py"))
        path = Path(module.__file__)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# inert native exclusion fixture\n")
        modules[name] = module
        monkeypatch.setitem(sys.modules, module.__name__, module)
    # Keep other mocked AOS modules from neighbouring tests outside this fixture's closure.
    for name in tuple(sys.modules):
        if (name == "aos" or name.startswith("aos.")) and name not in {
            module.__name__ for module in modules.values()
        }:
            monkeypatch.delitem(sys.modules, name)
    schema = tmp_path / "aos/schemas/native_exclusion_evidence.schema.json"
    schema.parent.mkdir()
    schema.write_text('{"fixture":true}')
    paths = [Path(module.__file__) for module in modules.values()] + [schema]
    promoted = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    configuration = {
        "source_roots": {"aos": str(tmp_path / "aos")},
        "source_files": {
            "aos": {str(path.relative_to(tmp_path / "aos")): promoted[str(path)] for path in paths}
        },
        "policy_path": "inert",
        "policy_file_sha256": "a" * 64,
        "profile_config_path": "inert",
        "profile_file_sha256": "b" * 64,
    }
    identity = Wire(
        pid=os.getpid(),
        uid=os.getuid(),
        start_ticks=22,
        pid_namespace=55,
        boot_id="11111111-1111-1111-1111-111111111111",
    )
    caller = Wire(
        unit=launch.SHARED_CALLER_UNIT,
        invocation_id="c" * 32,
        control_group="/owned/" + launch.SHARED_CALLER_UNIT,
        **{key: identity.value[key] for key in ("pid", "uid", "start_ticks", "boot_id")},
    )
    request = dict(
        request_id="native-maintenance-" + "d" * 32,
        principal="test-principal",
        owner_uid=os.getuid(),
        boot_id=identity.boot_id,
        store={"fixture": True},
        issued_boottime=900.0,
        expires_boottime=1100.0,
        expected_session="app-" + "e" * 32,
        original_state_sha256="f" * 64,
        handover_sha256="1" * 64,
        shared_plan_sha256="2" * 64,
        candidate_manifest_sha256="3" * 64,
        candidate_patch_sha256="4" * 64,
        config_files={"/fixture/ünicode-plan": "5" * 64},
    )
    request["config_sha256"] = digest(request["config_files"])
    review = dict(
        schema="scientist.native-exclusion-review.v1",
        request=request,
        receipt_sha256="6" * 64,
        legacy_state_path="/fixture/current.json",
        candidate_manifest_path="/fixture/manifest.json",
        candidate_patch_path="/fixture/source.patch.txt",
        promoted_source_files=promoted,
    )
    scope = dict(plan_path="/fixture/plan.json", plan_sha256=request["shared_plan_sha256"])
    case = SimpleNamespace(
        review=review,
        scope=scope,
        configuration=configuration,
        sources=promoted,
        caller=caller,
        identity=identity,
        calls=[],
        mono=100.0,
        boot=1000.0,
        mutate=lambda document: None,
        producer_error=None,
        wrong_hash=False,
        utf8_hash=False,
        advance_boot=0.0,
    )
    monkeypatch.setattr(launch.time, "monotonic", lambda: case.mono)
    monkeypatch.setattr(launch.time, "clock_gettime", lambda _clock: case.boot)
    modules["contracts"].digest = digest
    modules["lifecycle"].process_identity = lambda _pid: case.identity
    modules["native_maintenance"].NativeMaintenanceRequest = Wire
    modules["shared_desktop_host"].SharedServiceBinding = Wire
    modules["native_exclusion"].NativeExclusionEvidence = Wire

    class Reader:
        def __init__(self, store, original, receipt_sha, shared, **paths):
            case.calls.append(("constructor", plain(store), plain(original), receipt_sha, paths))
            self.request, self.caller = original, shared

        def read_native_exclusion(self, handover, plan, *, deadline):
            case.calls.append(("read", handover, plan, deadline))
            if case.producer_error:
                raise case.producer_error
            req = self.request.model_dump()
            document = {
                "schema_version": "aos.native-exclusion.v1",
                "profile": "shared-only-runtime-v1",
                "scope": "repository-managed-entrypoints",
                "recorded_at": "fixture",
                **{
                    key: req[key]
                    for key in (
                        "request_id",
                        "principal",
                        "owner_uid",
                        "boot_id",
                        "issued_boottime",
                        "expires_boottime",
                        "handover_sha256",
                        "shared_plan_sha256",
                        "candidate_manifest_sha256",
                        "candidate_patch_sha256",
                        "config_files",
                        "config_sha256",
                    )
                },
                "maintenance_request_sha256": digest(req),
                "maintenance_receipt_sha256": review["receipt_sha256"],
                "source_files": deepcopy(promoted),
                "source_sha256": digest(promoted),
                "shared_caller": self.caller.model_dump(),
                "legacy": {
                    "manager_session": req["expected_session"],
                    "original_state_sha256": req["original_state_sha256"],
                },
                "effective_deadline_boottime": deadline,
                "observed_boottime": case.boot,
                "native_admission_disabled": True,
                "legacy_workers_absent": True,
                "expiry_reopens_native": False,
                "allocation_authority": False,
                "shared_launch_authorized": False,
                "gpu_release_verified": False,
            }
            case.mutate(document)
            case.boot += case.advance_boot
            checksum = digest(document)
            if case.utf8_hash:
                checksum = hashlib.sha256(launch.canonical(document)).hexdigest()
            return document, "0" * 64 if case.wrong_hash else checksum

    modules["native_exclusion"].NativeExclusionReader = Reader
    case.rights = launch.CurrentRuntimeRights(
        {}, configuration, native_exclusion=review, shared_scope=scope, source_inputs=promoted
    )
    case.modules = modules
    return case


def test_shared_requires_explicit_review_and_legacy_cannot_carry_it(exclusion):
    f = exclusion
    cfg = {"caller_unit": launch.SHARED_CALLER_UNIT, "native_exclusion": f.review}
    assert launch._native_exclusion_contract(cfg) == f.review
    with pytest.raises(ValueError):
        launch._native_exclusion_contract({"caller_unit": launch.SHARED_CALLER_UNIT})
    with pytest.raises(ValueError):
        launch._native_exclusion_contract(cfg | {"caller_unit": "legacy.service"})
    assert launch._native_exclusion_contract({"caller_unit": "legacy.service"}) is None
    missing = launch.CurrentRuntimeRights({}, f.configuration)
    with pytest.raises(ValueError, match="requires native exclusion"):
        missing._verify_native_exclusion(f.caller, 101.0)
    assert not f.calls


def test_explicit_reader_uses_ascii_canonical_and_shortens_original_boottime(exclusion):
    f = exclusion
    f.rights._verify_native_exclusion(f.caller, 101.0)
    assert f.calls[-1] == ("read", "1" * 64, "2" * 64, 1001.0)
    assert f.calls[0][4]["shared_plan_path"] == Path(f.scope["plan_path"])
    f.review["request"]["expires_boottime"] = 1000.5
    f.rights._verify_native_exclusion(f.caller, 101.0)
    assert f.calls[-1][-1] == 1000.5


@pytest.mark.parametrize(
    "change",
    [
        "hash",
        "utf8_hash",
        "receipt",
        "plan",
        "request",
        "owner",
        "source",
        "config",
        "caller",
        "legacy",
        "expired",
        "extend",
        "stale",
        "release",
        "active_model",
        "original_expired",
    ],
)
def test_failed_expired_or_wrong_scope_evidence_denies(exclusion, change):
    f = exclusion
    if change == "hash":
        f.wrong_hash = True
    elif change == "utf8_hash":
        f.utf8_hash = True
    elif change == "expired":
        f.advance_boot = 2.0
    elif change == "original_expired":
        f.review["request"]["expires_boottime"] = 999.0
    elif change == "active_model":
        f.producer_error = ValueError("unsupported active model interpreter remains")
    else:

        def mutate(doc):
            if change == "caller":
                doc["shared_caller"]["invocation_id"] = "0" * 32
            elif change == "legacy":
                doc["legacy"]["manager_session"] = "app-" + "0" * 32
            elif change == "source":
                doc["source_files"] = {}
            elif change == "config":
                doc["config_files"] = {}
            elif change == "extend":
                doc["effective_deadline_boottime"] += 10
            elif change == "stale":
                doc["observed_boottime"] -= 1
            elif change == "release":
                doc["gpu_release_verified"] = True
            else:
                key = {
                    "receipt": "maintenance_receipt_sha256",
                    "plan": "shared_plan_sha256",
                    "request": "maintenance_request_sha256",
                    "owner": "owner_uid",
                }[change]
                doc[key] = 12345 if change == "owner" else "0" * 64

        f.mutate = mutate
    with pytest.raises(ValueError):
        f.rights._verify_native_exclusion(f.caller, 101.0)


@pytest.mark.parametrize("change", ["bytes", "static_pin", "promoted_pin", "import_origin"])
def test_reader_source_and_import_closure_are_required(exclusion, change):
    f = exclusion
    path = Path(f.modules["native_exclusion"].__file__)
    if change == "bytes":
        path.write_text("# changed fixture\n")
    elif change == "static_pin":
        f.rights.source_inputs = f.sources | {str(path): "0" * 64}
    elif change == "promoted_pin":
        f.review["promoted_source_files"] = f.sources | {str(path): "0" * 64}
    else:
        f.modules["native_exclusion"].__file__ = "/unreviewed/native_exclusion.py"
    with pytest.raises(ValueError):
        f.rights._verify_native_exclusion(f.caller, 101.0)
    assert not f.calls


@pytest.mark.parametrize("expires_during_final_check", [False, True])
def test_actual_current_rights_callback_invokes_reader_after_authentication(
    exclusion, monkeypatch, expires_during_final_check
):
    f = exclusion
    f.configuration["source_roots"]["scientist"] = f.configuration["source_roots"]["aos"]
    events = []
    peer = Peer()
    pin = {"manifest_sha256": "a" * 64}
    binding = Wire(
        server_generation=asdict(peer) | {"unit": "swapp-lab-gpu-broker.service"},
        caller_generation=f.caller.model_dump(),
        policy_sha256="b" * 64,
        profile_pin=pin,
    )
    f.rights.bindings = {"profile": binding.model_dump()}
    f.modules["scientist_admission_history"].ScientistAdmissionBindingV2 = Wire
    native = f.modules["scientist_transport"]
    native.BrokerPeer = Peer

    def observe(name, result):
        def call(*_args, deadline):
            events.append(name)
            assert deadline == 101.0
            return result

        return call

    native.SystemdBrokerAuthenticator = lambda: SimpleNamespace(
        authenticate=observe("broker", peer), still_current=observe("broker-current", True)
    )
    native.SystemdCallerAuthenticator = lambda: SimpleNamespace(
        authenticate=observe("caller", f.caller)
    )
    policy = SimpleNamespace(
        config={
            "enabled": True,
            "history_reconcile": False,
            "caller_unit": f.caller.unit,
            "profile_pins": {"profile": pin},
        },
        sha256="b" * 64,
        verify=Mock(),
        verify_policy_hash=Mock(),
    )
    monkeypatch.setattr(launch, "pinned_json", lambda *_: {})
    monkeypatch.setattr(launch, "load_profiles", lambda *_a, **_k: {})
    monkeypatch.setattr(launch, "ControlPolicy", lambda *_a, **_k: policy)
    f.mutate = lambda _doc: events.append("exclusion")
    if expires_during_final_check:
        # BOOTTIME can advance while MONOTONIC stands still (e.g. host suspend).
        policy.verify.side_effect = lambda: setattr(f, "boot", 1002.0)
    token = control_deadline.set(101.0)
    try:
        if expires_during_final_check:
            with pytest.raises(ValueError, match="expired during final rights"):
                f.rights({"profile": binding}, {"profile": {"profile": pin}})
        else:
            f.rights({"profile": binding}, {"profile": {"profile": pin}})
        assert control_deadline.get() == 101.0
    finally:
        control_deadline.reset(token)
    assert events == ["broker", "broker-current", "caller", "exclusion", "broker-current", "caller"]
    policy.verify.assert_called_once()


def test_legacy_launch_keeps_existing_factory_shape(receipt_launch):
    f = receipt_launch
    _module, _factory, _lab = f.prepare()
    assert "native_exclusion" not in f.cfg["factory_arguments"]
