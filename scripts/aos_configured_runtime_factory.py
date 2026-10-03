"""Compose native AOS configured-source and bootstrap admission providers.

Use this module in the actual AOS interpreter. Full reviewed bindings and file
receipts must be provisioned independently before construction; they never come
from a broker ACK or from hashing observed files to manufacture expected trust.

``verify_artifact_closure(selected_bindings, requirements)`` is REQUIRED. The
host provider must independently check the original native manifest artifact
hash gates, exact manifest dependency VERSION gate, actual interpreter/source
identity and current runtime rights/revocation. The supplied requirements retain
the complete pinned manifest and its native artifact/dependency maps. A bounded
current verification receipt can support this callback; its provider must reject
changed artifact identities, source, interpreter or service generations. This
callback does not require another multi-GB weight scan on each control check.
Native workers still verify their manifest artifact bytes before activation,
inside scheduler ownership. These checks do not establish a broader dependency
byte attestation. The callback must complete with None or raise within the native
source deadline. No permissive provider or manufactured receipt is supplied.

Wire all four explicit serve_desktop.main hooks:
    scientist_admission_factory=factory,
    scientist_bootstrap_expected_peer=factory.expected_peer,
    scientist_confirm_runtime=factory.confirm_runtime,
    scientist_output_contract=factory.output_contract.
The scientist_bootstrap_factory shortcut requires the concrete native factory;
this configured wrapper must use the four explicit hooks above.
The native bootstrap factory owns durable intent persistence. This adapter has
no writer, allocator, model loader, synthetic clock or service launch path.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import time
from copy import deepcopy
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

from scripts.aos_native_admission_factory import (
    _authenticate,
    _bounded,
    _deadline_scope,
    _DeadlineAuthenticator,
    _require_deadline_support,
    _supports_deadline,
    _verification_deadline,
    _verify_caller_service,
)

PROFILES = {
    "aos.decider.turn.v1": "decider",
    "aos.bonsai.recovery.v1": "bonsai-recovery",
    "aos.bonsai.vision.v1": "bonsai-vision",
}
SCHEMAS = {
    "aos.decider.turn.v1": "decider-response.template.schema.json",
    "aos.bonsai.recovery.v1": "bonsai-recovery-content.schema.json",
    "aos.bonsai.vision.v1": "bonsai-vision-content.schema.json",
}
SCHEMA_FILES = frozenset(
    {
        *SCHEMAS.values(),
        "decider-usage.schema.json",
        "bonsai-response.schema.json",
        "bonsai-usage.schema.json",
        "result.schema.json",
        "bonsai-raw.schema.json",
    }
)
PROFILE_KEYS = frozenset(
    {
        "kind",
        "manifest",
        "manifest_sha256",
        "deployment_digest",
        "python",
        "model_paths",
        "budgets",
        "response_schema_sha256",
        "temperature",
        "max_output_tokens",
        "context_tokens",
        "output_contract",
    }
)


def _load_aos():
    from aos.contracts import digest
    from aos.scientist_admission_history import ScientistAdmissionBindingV2
    from aos.scientist_bootstrap import CONTROL_DESCRIPTOR_SHA256, INFER_DESCRIPTOR_SHA256
    from aos.scientist_bootstrap_factory import ScientistBootstrapAdmissionFactory
    from aos.scientist_protocol import _reject_constant, _unique_object
    from aos.scientist_source_authority import ScientistConfiguredSourceVerifier, _read
    from aos.scientist_terminal import TERMINAL_DESCRIPTOR_SHA256
    from aos.scientist_transport import (
        BROKER_UNIT,
        BrokerPeer,
        ScientistAdmissionError,
        SystemdBrokerAuthenticator,
        SystemdCallerAuthenticator,
        _process_identity,
    )
    from aos.supervisor import RecoveryPlan
    from aos.vision import VisionScene

    api = SimpleNamespace(
        native_deadline_required=True,
        native_schema_hashes={
            "recovery": digest(RecoveryPlan.model_json_schema()),
            "vision": digest(VisionScene.model_json_schema()),
        },
        ScientistAdmissionBindingV2=ScientistAdmissionBindingV2,
        CONTROL_DESCRIPTOR_SHA256=CONTROL_DESCRIPTOR_SHA256,
        INFER_DESCRIPTOR_SHA256=INFER_DESCRIPTOR_SHA256,
        ScientistBootstrapAdmissionFactory=ScientistBootstrapAdmissionFactory,
        _reject_constant=_reject_constant,
        _unique_object=_unique_object,
        ScientistConfiguredSourceVerifier=ScientistConfiguredSourceVerifier,
        _read=_read,
        TERMINAL_DESCRIPTOR_SHA256=TERMINAL_DESCRIPTOR_SHA256,
        BROKER_UNIT=BROKER_UNIT,
        BrokerPeer=BrokerPeer,
        ScientistAdmissionError=ScientistAdmissionError,
        SystemdBrokerAuthenticator=SystemdBrokerAuthenticator,
        SystemdCallerAuthenticator=SystemdCallerAuthenticator,
        _process_identity=_process_identity,
    )
    _require_deadline_support(api)
    _require_source_deadline_support(api)
    return api


def _require_source_deadline_support(api):
    _require(
        api,
        _supports_deadline(api.ScientistConfiguredSourceVerifier.__call__),
        "Reviewed native source verifier outer deadline support is unavailable",
    )


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _manifest_deployment_sha256(manifest):
    """Match native worker/configuration manifest identity, including Unicode."""
    raw = json.dumps(
        manifest, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def native_deployment_sha256(manifest, kind, schema_hashes):
    """Match the source-pinned native supervisors; retain raw manifest separately."""
    if kind not in PROFILES.values():
        raise ValueError("Unknown native deployment kind")
    pins = deepcopy(manifest)
    if kind != "decider":
        pins["recovery_schema_sha256"] = _fingerprint(schema_hashes["recovery"])
        pins["recovery_protocol"] = "aos-bonsai-recovery-v1"
    if kind == "bonsai-vision":
        pins["vision_schema_sha256"] = _fingerprint(schema_hashes["vision"])
        pins["vision_protocol"] = "aos-bonsai-vision-v1"
    return _manifest_deployment_sha256(pins)


def _require(api, condition, message):
    if not condition:
        raise api.ScientistAdmissionError(message)


def _absolute(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Configured runtime requires explicit absolute non-traversing paths")
    return path


def _fingerprint(value):
    if type(value) is not str or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError("Configured runtime requires independently supplied SHA-256 receipts")
    return value


def _profile_config_sha256(profile_id, entry, source_root):
    """Mirror AOSProfile.config_sha256 without importing the Scientist runtime."""
    value = {"profile_id": profile_id, "source_root": str(source_root), **entry}
    for key in ("manifest", "python"):
        value[key] = str(Path(value[key]))
    value["model_paths"] = [str(Path(path)) for path in value["model_paths"]]
    return _digest(value)


class ConfiguredScientistAdmissionFactory:
    """Native bootstrap composition with explicit configured runtime authority."""

    def __init__(
        self,
        reviewed_bindings,
        policy_path,
        profile_config_path,
        control_socket_path,
        *,
        policy_file_sha256,
        profile_file_sha256,
        source_roots,
        source_files,
        config_files,
        model_root,
        output_contract_directory,
        verify_artifact_closure=None,
    ):
        if not callable(verify_artifact_closure):
            raise TypeError(
                "Independent artifact/dependency closure and current rights are required"
            )
        self._policy = _absolute(policy_path)
        self._profiles = _absolute(profile_config_path)
        self._socket = _absolute(control_socket_path)
        self._model_root = _absolute(model_root)
        self._output_dir = _absolute(output_contract_directory)
        self._policy_hash = _fingerprint(policy_file_sha256)
        self._profile_hash = _fingerprint(profile_file_sha256)
        if type(source_roots) is not dict or set(source_roots) != {"aos", "scientist"}:
            raise ValueError("Exact AOS and Scientist source roots are required")
        self._roots = MappingProxyType(
            {name: _absolute(path) for name, path in source_roots.items()}
        )
        if type(config_files) is not dict or not config_files:
            raise ValueError(
                "Independently reviewed configuration and output file receipts are required"
            )
        configs = {_absolute(path): _fingerprint(value) for path, value in config_files.items()}
        if self._profiles in configs and configs[self._profiles] != self._profile_hash:
            raise ValueError("Explicit profile receipt and configuration map disagree")
        configs[self._profiles] = self._profile_hash
        self._configs = MappingProxyType(configs)
        self._attest = verify_artifact_closure
        api = self._api = _load_aos()
        if getattr(api, "native_deadline_required", False):
            _require_source_deadline_support(api)
        _require(
            api,
            type(reviewed_bindings) is dict and set(reviewed_bindings) == set(PROFILES),
            "Full independently reviewed bindings for all three profiles are required",
        )
        reviewed = {}
        for profile, value in reviewed_bindings.items():
            value = (
                value.model_dump(mode="json")
                if isinstance(value, api.ScientistAdmissionBindingV2)
                else deepcopy(value)
            )
            binding = api.ScientistAdmissionBindingV2.model_validate(value, strict=True)
            _require(
                api,
                binding.profile_id == profile,
                "Reviewed full binding belongs to another profile",
            )
            reviewed[profile] = _canonical(binding.model_dump(mode="json")).decode("utf-8")
        self._reviewed = MappingProxyType(reviewed)
        self._authenticator = api.SystemdBrokerAuthenticator()
        if getattr(api, "native_deadline_required", False):
            self._authenticator = _DeadlineAuthenticator(api, self._authenticator)
        self._source = api.ScientistConfiguredSourceVerifier(
            self._policy,
            dict(self._roots),
            deepcopy(reviewed_bindings),
            deepcopy(source_files),
            config_files=dict(self._configs),
            verify_runtime=self._verify_runtime,
        )
        self._factory = api.ScientistBootstrapAdmissionFactory(
            deepcopy(reviewed_bindings),
            self._socket,
            control_descriptor_sha256=api.CONTROL_DESCRIPTOR_SHA256,
            verify_source=(
                self._verify_source
                if getattr(api, "native_deadline_required", False)
                else self._source
            ),
            authenticator=self._authenticator,
        )

        if getattr(api, "native_deadline_required", False):
            original_current = getattr(self._factory, "_check_current", None)
            _require(
                api,
                callable(original_current),
                "Reviewed native bootstrap current verifier is unavailable",
            )

            def checked_current(*args, **kwargs):
                with _deadline_scope(api, 3.5):
                    return original_current(*args, **kwargs)

            self._factory._check_current = checked_current

    @_bounded(3.5)
    def _verify_source(self, selected):
        """Forward one absolute monotonic deadline; native retains its five-second cap."""
        if getattr(self._api, "native_deadline_required", False):
            _require(
                self._api,
                _supports_deadline(self._source),
                "Reviewed native source verifier outer deadline support is unavailable",
            )
            return self._source(selected, deadline=_verification_deadline.get())
        return self._source(selected)

    @_bounded(3.5)
    def __call__(self, controller):
        return self._factory(controller)

    @_bounded(3.5)
    def expected_peer(self, request, binding):
        return self._factory.expected_peer(request, binding)

    @_bounded(3.5)
    def confirm_runtime(self, profiles):
        return self._factory.confirm_runtime(profiles)

    @property
    def output_contract(self):
        """Delegate the native cross-profile contract check and defensive copy."""
        return self._factory.output_contract

    def _bindings(self):
        return {
            profile: self._api.ScientistAdmissionBindingV2.model_validate(
                json.loads(raw), strict=True
            )
            for profile, raw in self._reviewed.items()
        }

    def _json(self, path, deadline, *, expected=None, private=False, limit=256 * 1024):
        raw = self._api._read(path, limit, deadline, limit, private=private)
        _require(
            self._api,
            expected is None or hashlib.sha256(raw).hexdigest() == expected,
            "Configured runtime file differs from independent receipt: " + str(path),
        )
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=self._api._unique_object,
            parse_constant=self._api._reject_constant,
        )
        _require(self._api, type(value) is dict, "Configured runtime JSON must be an object")
        _canonical(value)
        return value

    def _pinned_json(self, path, deadline):
        _require(
            self._api,
            path in self._configs,
            "Configured artifact lacks an independently reviewed file receipt: " + str(path),
        )
        return self._json(path, deadline, expected=self._configs[path])

    def _schema_bundle(self, reviewed, deadline):
        bundle = self._pinned_json(self._output_dir / "bundle.json", deadline)
        _require(
            self._api,
            set(bundle)
            == {
                "name",
                "version",
                "schemas",
                "adapter_source_sha256",
                "request_binding_source_sha256",
            }
            and bundle["name"] == "aos-scientist-profile-output.v2"
            and type(bundle["version"]) is int
            and bundle["version"] == 2
            and type(bundle["schemas"]) is dict
            and set(bundle["schemas"]) == SCHEMA_FILES,
            "Configured output bundle schema differs",
        )
        adapter_path = self._roots["scientist"] / "lab/llm/aos_profile_output.py"
        adapter_bytes = self._api._read(adapter_path, 8 * 1024**2, deadline, 8 * 1024**2)
        _require(
            self._api,
            bundle["adapter_source_sha256"]
            == bundle["request_binding_source_sha256"]
            == hashlib.sha256(adapter_bytes).hexdigest(),
            "Configured output adapter source differs from pinned bundle",
        )
        for name, checksum in bundle["schemas"].items():
            schema = self._pinned_json(self._output_dir / name, deadline)
            _require(
                self._api,
                _digest(schema) == checksum,
                "Configured output schema differs from pinned bundle",
            )
        output_pin = {key: bundle[key] for key in ("name", "version")}
        output_pin["bundle_sha256"] = _digest(bundle)
        _require(
            self._api,
            all(
                binding.profile_pin.output_contract.model_dump(mode="json") == output_pin
                and binding.profile_pin.response_schema_sha256
                == bundle["schemas"][SCHEMAS[profile]]
                for profile, binding in reviewed.items()
            ),
            "Reviewed response schema or output contract differs from configured bundle",
        )

    def _profile(self, profile_id, entry, reviewed, deadline):
        api, pin = self._api, reviewed.profile_pin
        _require(
            api,
            type(entry) is dict
            and set(entry) == PROFILE_KEYS
            and entry["kind"] == PROFILES[profile_id]
            and type(entry["model_paths"]) is list
            and len(entry["model_paths"]) == 2,
            "Configured profile entry differs from fixed native profile",
        )
        _require(
            api,
            entry["manifest_sha256"] == pin.manifest_sha256
            and entry["deployment_digest"] == pin.deployment_digest
            and entry["response_schema_sha256"] == pin.response_schema_sha256
            and entry["output_contract"] == pin.output_contract.model_dump(mode="json")
            and _profile_config_sha256(profile_id, entry, self._roots["aos"]) == pin.config_sha256,
            "Configured profile hash, deployment or output pins differ from review",
        )
        budgets = entry["budgets"]
        _require(
            api,
            type(budgets) is dict
            and set(budgets)
            == {"activation_seconds", "inference_seconds", "total_seconds", "queue_seconds"}
            and all(type(value) is int and value > 0 for value in budgets.values())
            and budgets["activation_seconds"] <= 600
            and budgets["inference_seconds"] <= 540
            and budgets["total_seconds"] <= 720
            and budgets["total_seconds"]
            >= (budgets["activation_seconds"] + budgets["inference_seconds"])
            and budgets["queue_seconds"] >= budgets["total_seconds"] + 30,
            "Configured native profile budgets differ from bounded runtime",
        )
        _require(
            api,
            type(entry["temperature"]) in (int, float)
            and math.isfinite(entry["temperature"])
            and 0 <= entry["temperature"] <= 1
            and type(entry["max_output_tokens"]) is int
            and 1 <= entry["max_output_tokens"] <= 512
            and type(entry["context_tokens"]) is int
            and 256 <= entry["context_tokens"] <= 16384,
            "Configured native profile sampling or context exceeds bounds",
        )
        manifest_path = _absolute(entry["manifest"])
        manifest = self._pinned_json(manifest_path, deadline)
        _require(
            api,
            self._configs[manifest_path] == pin.manifest_sha256
            and native_deployment_sha256(manifest, entry["kind"], api.native_schema_hashes)
            == pin.deployment_digest,
            "Configured model manifest or canonical deployment differs from review",
        )
        keys = (
            ("model_path", "code_path")
            if entry["kind"] == "decider"
            else ("model_path", "runtime_path")
        )
        roots = [_absolute(manifest[key]) for key in keys]
        _require(
            api,
            entry["model_paths"] == [str(path) for path in roots]
            and self._model_root.is_dir()
            and not self._model_root.is_symlink()
            and all(
                path.is_dir()
                and not path.is_symlink()
                and path.resolve(strict=True).is_relative_to(self._model_root.resolve(strict=True))
                for path in roots
            ),
            "Configured artifact roots differ from pinned existing model root",
        )
        python = _absolute(entry["python"])
        target = python.resolve(strict=True)
        info = target.stat()
        _require(
            api,
            stat.S_ISREG(info.st_mode)
            and info.st_uid in (0, os.getuid())
            and not stat.S_IMODE(info.st_mode) & 0o022
            and os.access(python, os.X_OK),
            "Configured actual model interpreter is unavailable or untrusted",
        )
        if entry["kind"] != "decider":
            _require(
                api,
                manifest.get("parallel") == 1
                and all(
                    manifest.get(key) == entry[key]
                    for key in ("temperature", "max_output_tokens", "context_tokens")
                ),
                "Configured Bonsai sampling differs from pinned manifest",
            )
        return {
            "profile": deepcopy(entry),
            "manifest_path": str(manifest_path),
            "manifest": manifest,
            "model_root": str(self._model_root),
            "artifact_roots": [str(path) for path in roots],
            "interpreter": str(python),
            "interpreter_target": str(target),
            "required_attestation": [
                "original native manifest artifact hash gates including weights and projector",
                "exact native manifest dependency version gate",
                "actual interpreter and source identity matching native verification receipts",
                "current host policy rights and revocation",
            ],
        }

    @_bounded(4)
    def _verify_runtime(self, selected):
        api, reviewed = self._api, self._bindings()
        _require(
            api,
            type(selected) is dict
            and selected
            and set(selected) <= set(reviewed)
            and all(
                isinstance(value, api.ScientistAdmissionBindingV2) and value == reviewed[profile]
                for profile, value in selected.items()
            ),
            "Runtime selection differs from independently reviewed full bindings",
        )
        deadline = _verification_deadline.get()
        config = self._json(self._profiles, deadline, expected=self._profile_hash, private=True)
        policy = self._json(
            self._policy, deadline, expected=self._policy_hash, private=True, limit=65536
        )
        _require(
            api,
            set(config) == {"schema", "source_root", "profiles"}
            and config["schema"] == "swapp-aos-gpu-profiles.v1"
            and config["source_root"] == str(self._roots["aos"])
            and self._roots["aos"].is_dir()
            and not self._roots["aos"].is_symlink()
            and type(config["profiles"]) is dict
            and set(config["profiles"]) == set(PROFILES),
            "Configured native three-profile registry or AOS source root differs",
        )
        _require(
            api,
            policy.get("enabled") is True
            and policy.get("history_reconcile") is False
            and policy.get("control_socket") == str(self._socket)
            and all(
                policy.get("caller_unit") == binding.caller_generation.unit
                and _digest(policy) == binding.policy_sha256
                for binding in reviewed.values()
            ),
            "Configured enabled policy, caller or control socket differs from review",
        )
        self._schema_bundle(reviewed, deadline)
        requirements = {}
        for profile, binding in reviewed.items():
            _require(
                api,
                binding.server_generation.unit == api.BROKER_UNIT
                and binding.infer_schema.sha256 == api.INFER_DESCRIPTOR_SHA256
                and binding.control_schema.sha256 == api.CONTROL_DESCRIPTOR_SHA256
                and binding.terminal_schema.sha256 == api.TERMINAL_DESCRIPTOR_SHA256,
                "Reviewed native broker or schema descriptor differs",
            )
            closure = self._profile(profile, config["profiles"][profile], binding, deadline)
            if profile in selected:
                expected = binding.server_generation.model_dump(mode="json")
                peer = api.BrokerPeer(
                    **{key: value for key, value in expected.items() if key != "unit"}
                )
                _require(
                    api,
                    _authenticate(api, self._authenticator.authenticate, peer.pid, peer.uid) == peer
                    and _authenticate(api, self._authenticator.still_current, peer) is True,
                    "Configured broker is not the actual current native service generation",
                )
                _verify_caller_service(api, binding.caller_generation)
                requirements[profile] = closure
        _require(
            api,
            self._attest(deepcopy(selected), deepcopy(requirements)) is None,
            "Independent artifact/dependency attestation must complete or raise",
        )
        _require(
            api, time.monotonic() < deadline, "Configured runtime attestation deadline expired"
        )
        for binding in selected.values():
            expected = binding.server_generation.model_dump(mode="json")
            peer = api.BrokerPeer(
                **{key: value for key, value in expected.items() if key != "unit"}
            )
            _require(
                api,
                _authenticate(api, self._authenticator.still_current, peer) is True,
                "Configured native broker generation changed during independent attestation",
            )
            _verify_caller_service(api, binding.caller_generation)
