#!/usr/bin/env python3
"""Prepare disabled native broker configuration from reviewed local sources.

No model imports, service activation, scheduler construction or DB writes.
This is a reviewable configuration; it does not authorize experiment admission.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def read_file(path, limit):
    path = Path(path).absolute()
    info = path.lstat()
    if (
        path.resolve(strict=True) != path
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid not in {0, os.getuid()}
        or info.st_mode & 0o022
        or info.st_size > limit
    ):
        raise ValueError("untrusted configuration source: " + str(path))
    raw = path.read_bytes()
    if len(raw) > limit:
        raise ValueError("configuration source exceeds limit")
    return raw


NATIVE_SOURCE_PROFILES = (
    "retained_provider_candidate_v1",
    "bootstrap_factory_candidate_v1",
    "configured_source_candidate_v1",
    "lab_readback_history_candidate_v1",
    "no_admission_observation_candidate_v1",
    "no_admission_retained_recovery_candidate_v1",
)


def verified_source_receipt(path, expected_sha256, *, required_source_profile=None):
    from scripts.check_aos_lab_compatibility import inspect_source

    raw = read_file(path, 256 * 1024)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("reviewed source receipt hash differs")
    receipt = json.loads(raw)
    report = receipt.get("report", receipt)
    if required_source_profile is not None and (
        required_source_profile not in NATIVE_SOURCE_PROFILES
        or report.get("source_profile") != required_source_profile
    ):
        raise ValueError("explicit reviewed source profile differs")
    if (
        report.get("source_profile") not in NATIVE_SOURCE_PROFILES
        or report.get("snapshot_kind") != "reviewed_snapshot"
        or report.get("source_ready") is not True
        or report.get("admission_allowed") is not False
    ):
        raise ValueError("reviewed native provider source receipt required")
    current = inspect_source(
        Path(report["source_root"]),
        report["expected_head"],
        snapshot_kind="reviewed_snapshot",
        source_profile=report["source_profile"],
        expected_selected_source_sha256=report["selected_source_sha256"],
        expected_tracked_diff_sha256=report["tracked_diff_sha256"],
        expected_selected_untracked_sha256=report["selected_untracked_sha256"],
    )
    if not current["source_ready"] or current["source_sha256"] != report["source_sha256"]:
        raise ValueError("native AOS source changed since review")
    return current


def native_schema_hashes(aos_root, python):
    """Read schema identities in the actual source-pinned AOS interpreter, CPU only."""
    code = """import sys,json
sys.path.insert(0, sys.argv[1])
from aos.contracts import digest
from aos.supervisor import RecoveryPlan
from aos.vision import VisionScene
print(json.dumps({"recovery":digest(RecoveryPlan.model_json_schema()),
                 "vision":digest(VisionScene.model_json_schema())}))
"""
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["CUDA_VISIBLE_DEVICES"] = ""
    result = subprocess.run(
        [str(python), "-B", "-I", "-c", code, str(aos_root / "src")],
        env=environment,
        check=True,
        capture_output=True,
        timeout=15,
    )
    if len(result.stdout) > 1024:
        raise ValueError("Native schema identity response exceeds bound")
    hashes = json.loads(result.stdout)
    if (
        type(hashes) is not dict
        or set(hashes) != {"recovery", "vision"}
        or any(
            type(value) is not str or re.fullmatch(r"[a-f0-9]{64}", value) is None
            for value in hashes.values()
        )
    ):
        raise ValueError("Native schema identity response differs")
    return hashes


def prepare(args):
    from lab.llm import aos_evidence_transport as evidence
    from lab.llm import aos_retained_evidence_transport as retained
    from lab.llm.aos_gpu_control import CONTROL_SCHEMA_HASH, INFER_SCHEMA_HASH, ControlPolicy
    from lab.llm.aos_gpu_executor import _model_paths
    from lab.llm.aos_gpu_service import load_profiles
    from lab.llm.aos_profile_output import NAME, VERSION, load_contract
    from lab.llm.gpu_scheduler import _principal_unit_matches
    from scripts.aos_configured_runtime_factory import native_deployment_sha256

    required_profile = getattr(args, "source_profile", None)
    if required_profile is None:
        report = verified_source_receipt(args.source_receipt, args.expected_source_receipt_sha256)
    else:
        report = verified_source_receipt(
            args.source_receipt,
            args.expected_source_receipt_sha256,
            required_source_profile=required_profile,
        )
    aos_root = Path(report["source_root"])
    python = args.aos_python.absolute()
    decider_python = args.decider_python.absolute()
    if any(not p.is_file() or not os.access(p, os.X_OK) for p in (python, decider_python)):
        raise ValueError("existing executable AOS and Decider model interpreters required")
    output = args.output.absolute()
    if output.exists() or output.is_symlink() or output.resolve().is_relative_to(aos_root):
        raise ValueError("new Scientist-owned output directory required")
    if not _principal_unit_matches("aos", args.caller_unit):
        raise ValueError("caller must be a canonical AOS GPU principal: swapp-aos-gpu-*.service")
    bundle_dir = ROOT / "lab/llm/contracts/profile_output_v2"
    bundle = json.loads(read_file(bundle_dir / "bundle.json", 256 * 1024))
    bundle_sha256 = digest(bundle)
    contract = load_contract(bundle_dir, bundle_sha256)
    schemas = native_schema_hashes(aos_root, python)
    profiles = {}
    for profile_id, kind, path in (
        ("aos.decider.turn.v1", "decider", args.decider_manifest),
        ("aos.bonsai.recovery.v1", "bonsai-recovery", args.bonsai_manifest),
        ("aos.bonsai.vision.v1", "bonsai-vision", args.bonsai_manifest),
    ):
        raw = read_file(path, 256 * 1024)
        manifest = json.loads(raw)
        model_paths = [Path(manifest["model_path"])]
        model_paths.append(
            Path(manifest["code_path"]) if kind == "decider" else Path(manifest["runtime_path"])
        )
        if any(not p.is_absolute() or not p.is_dir() for p in model_paths):
            raise ValueError("local model artifacts must already exist")
        profiles[profile_id] = dict(
            kind=kind,
            manifest=str(path.absolute()),
            manifest_sha256=hashlib.sha256(raw).hexdigest(),
            deployment_digest=native_deployment_sha256(manifest, kind, schemas),
            python=str(decider_python if kind == "decider" else python),
            model_paths=[str(p) for p in model_paths],
            budgets=dict(
                activation_seconds=180, inference_seconds=30, total_seconds=210, queue_seconds=300
            ),
            response_schema_sha256=contract.content_pin(profile_id),
            temperature=manifest.get("temperature", 0.0),
            max_output_tokens=manifest.get("max_output_tokens", 512),
            context_tokens=manifest.get("context_tokens", 16384),
            output_contract=dict(name=NAME, version=VERSION, bundle_sha256=bundle_sha256),
        )
    source_paths = [
        *sorted((ROOT / "lab/llm").glob("*.py")),
        *sorted((ROOT / "lab/llm/contracts").rglob("*.json")),
        ROOT / "scripts/aos_native_admission_factory.py",
        ROOT / "scripts/aos_configured_runtime_factory.py",
        ROOT / "scripts/aos_native_artifact_receipts.py",
        ROOT / "scripts/aos_native_live_bindings.py",
        ROOT / "scripts/check_aos_model_environment.py",
        ROOT / "scripts/aos_native_launch.py",
        ROOT / "scripts/aos_joint_lab_hooks.py",
        ROOT / "lab/api/aos_capability.py",
        ROOT / "lab/api/app.py",
        ROOT / "lab/api/registry.py",
    ]
    scientist_sources = {
        str(p.relative_to(ROOT)): hashlib.sha256(read_file(p, 8 * 1024**2)).hexdigest()
        for p in source_paths
    }
    os.umask(0o077)
    output.mkdir(mode=0o700)
    profile_path = output / "profiles.json"
    profile_path.write_bytes(
        canonical(
            dict(schema="swapp-aos-gpu-profiles.v1", source_root=str(aos_root), profiles=profiles)
        )
    )
    registry = load_profiles(profile_path, source_root=aos_root)
    for profile_id, item in profiles.items():
        profile = registry.get(profile_id, item["deployment_digest"])
        _model_paths(profile, json.loads(read_file(profile.manifest, 256 * 1024)))
    policy_path = output / "control-policy.disabled.json"
    policy = dict(
        schema="swapp-aos-control-policy.v1",
        enabled=False,
        caller_unit=args.caller_unit,
        control_socket=f"/run/user/{os.getuid()}/swapp-gpu/control.sock",
        history_reconcile=False,
        source_files=dict(scientist=scientist_sources, aos=report["source_sha256"]),
        profile_pins={
            p: ControlPolicy._pin(registry.get(p, item["deployment_digest"]))
            for p, item in profiles.items()
        },
        infer_schema_sha256=INFER_SCHEMA_HASH,
        control_schema_sha256=CONTROL_SCHEMA_HASH,
        evidence_schema_sha256=evidence.EVIDENCE_SCHEMA_SHA256,
        evidence_transport_schema_sha256=evidence.EVIDENCE_TRANSPORT_SCHEMA_HASH,
        retained_evidence_transport_schema_sha256=retained.EVIDENCE_TRANSPORT_SCHEMA_HASH,
    )
    policy_path.write_bytes(canonical(policy))
    control = ControlPolicy(policy_path, profiles=registry, source_root=ROOT)
    control.verify()
    verified_source_receipt(
        args.source_receipt,
        args.expected_source_receipt_sha256,
        required_source_profile=required_profile,
    )
    plan = dict(
        schema="aos-scientist-native-configuration-plan.v1",
        aos_source_profile=report.get("source_profile"),
        admission_allowed=False,
        policy_enabled=False,
        aos_commit=report["expected_head"],
        aos_selected_source_sha256=report["selected_source_sha256"],
        profile_config_sha256=hashlib.sha256(profile_path.read_bytes()).hexdigest(),
        policy_sha256=control.sha256,
        source_fingerprints={key: digest(value) for key, value in policy["source_files"].items()},
        profile_pins=policy["profile_pins"],
        model_weight_bytes_verified=False,
        interpreter_dependencies_verified=False,
        services_started=False,
        scheduler_created=False,
        gpu_acquired=False,
        remaining=[
            "review enabled policy and principal rights",
            "current real caller/broker generation",
            "canonical scheduler reservation",
            "full model/dependency closure",
            "real bounded model and independent Scorer acceptance",
        ],
    )
    (output / "plan.json").write_bytes(canonical(plan))
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "source-receipt",
        "output",
        "aos-python",
        "decider-python",
        "decider-manifest",
        "bonsai-manifest",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--expected-source-receipt-sha256", required=True)
    parser.add_argument("--source-profile", choices=NATIVE_SOURCE_PROFILES)
    parser.add_argument("--caller-unit", required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args), sort_keys=True))


if __name__ == "__main__":
    main()
