"""Synthetic stable admission pins; no live policy or service authority."""

import hashlib
import json
from pathlib import Path

from lab.llm.aos_gpu_control import CONTROL_SCHEMA_HASH, INFER_SCHEMA_HASH
from lab.llm.aos_gpu_control_store import (
    TERMINAL_SCHEMA_HASH,
    AdmissionGrant,
    canonical,
    validate_admission_binding,
)
from lab.llm.aos_profile_output import load_contract

BOOT = "00000000-0000-0000-0000-000000000001"


def fixture_current_admission(connection, binding):
    """Synthetic store/runtime fixtures only; supplies no live policy evidence."""
    assert connection.in_transaction
    validate_admission_binding(binding, require_output_contract=True)


def output_pin():
    directory = Path(__file__).resolve().parents[1] / "lab/llm/contracts/profile_output_v2"
    bundle = json.loads((directory / "bundle.json").read_bytes())
    fingerprint = hashlib.sha256(canonical(bundle).encode()).hexdigest()
    load_contract(directory, fingerprint)
    return {"name": "aos-scientist-profile-output.v2", "version": 2, "bundle_sha256": fingerprint}


def binding_for(peer, profile_id, deployment, config, response):
    return {
        "server_generation": {
            "uid": 1000,
            "pid": 300,
            "start_ticks": 90,
            "boot_id": BOOT,
            "unit": "swapp-lab-gpu-fixture.service",
            "invocation_id": "f" * 32,
            "control_group": "/fixture/lab",
        },
        "caller_generation": dict(peer),
        "policy_sha256": "1" * 64,
        "source_fingerprints": {"scientist": "2" * 64, "aos": "3" * 64},
        "profile_id": profile_id,
        "profile_pin": {
            "deployment_digest": deployment,
            "manifest_sha256": "4" * 64,
            "config_sha256": config,
            "response_schema_sha256": response,
            "output_contract": output_pin(),
        },
        "infer_schema": {
            "name": "aos-scientist-runtime.v1",
            "version": 1,
            "sha256": INFER_SCHEMA_HASH,
        },
        "control_schema": {
            "name": "aos-scientist-control.v1",
            "version": 1,
            "sha256": CONTROL_SCHEMA_HASH,
        },
        "terminal_schema": {
            "name": "aos-scientist-terminal.v1",
            "version": 1,
            "sha256": TERMINAL_SCHEMA_HASH,
        },
    }


def grant_for(peer, profile_id, deployment, config, response):
    return AdmissionGrant(
        canonical(binding_for(peer, profile_id, deployment, config, response)), lambda: None
    )
