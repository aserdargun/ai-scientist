"""Read-only AOS source preflight. Source presence never authorizes a runtime turn.

Run this before an experiment admission command, joined with ``&&``. Exit 2
means unsupported source; exit 3 means joint capability confirmation is pending.
There is deliberately no successful admission path until a jointly agreed
runtime capability contract exists. No AOS code, model or service is executed.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import stat
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

MAX_SOURCE_BYTES = 2 * 1024**2
HISTORICAL_FILES = (
    "scripts/serve_desktop.py",
    "services/decider/broker_worker.py",
    "services/bonsai/broker_worker.py",
    "src/aos/gpu_turn.py",
    "src/aos/lab_external.py",
)
HISTORICAL_OPTIONS = frozenset(
    {
        "--shared-gpu-turns",
        "--lab-external-api-url",
        "--lab-external-token-file",
        "--lab-external-suite",
    }
)
# Retain fixture/import compatibility; historical hooks are one explicit profile.
REQUIRED_FILES = HISTORICAL_FILES
RUNTIME_FILES = (
    "scripts/serve_desktop.py",
    "services/decider/broker_worker.py",
    "services/bonsai/broker_worker.py",
    "services/broker_runtime.py",
    "src/aos/scientist_protocol.py",
    "src/aos/scientist_transport.py",
    "src/aos/scientist_async.py",
    "src/aos/scientist_decision.py",
    "src/aos/scientist_intents.py",
    "src/aos/scientist_lab.py",
    "src/aos/scientist_lab_journal.py",
    "src/aos/scientist_lab_service.py",
)
RUNTIME_CLASSES = {
    "services/broker_runtime.py": {"TurnGate"},
    "src/aos/scientist_protocol.py": {"ScientistTurnRequest", "ScientistTurnReceipt"},
    "src/aos/scientist_transport.py": {"ScientistTurnClient", "SystemdBrokerAuthenticator"},
    "src/aos/scientist_async.py": {"ScientistAsyncTurnClient"},
    "src/aos/scientist_decision.py": {"ScientistDecisionEngine"},
    "src/aos/scientist_intents.py": {"ScientistIntentJournal"},
    "src/aos/scientist_lab.py": {"ScientistLabClient", "ScientistLabPolicy"},
    "src/aos/scientist_lab_journal.py": {"ScientistLabJournal"},
    "src/aos/scientist_lab_service.py": {"ScientistLabService"},
}
# Exact admission_v2 selection in AOS scripts/scientist_source_report.py.
# runtime_v1 above remains its existing, separately reviewed preflight profile.
ADMISSION_RUNTIME_FILES = (
    "scripts/serve_desktop.py",
    "services/broker_runtime.py",
    *RUNTIME_FILES[1:3],
    *RUNTIME_FILES[4:],
    "src/aos/scientist_desktop.py",
    "src/aos/scientist_supervisor.py",
    "src/aos/scientist_inventory.py",
)
ADMISSION_FILES = ADMISSION_RUNTIME_FILES + (
    "scripts/scientist_source_report.py",
    "services/bonsai_projection.py",
    "src/aos/contracts.py",
    "src/aos/supervisor.py",
    "src/aos/vision.py",
    "src/aos/scientist_admission_history.py",
    "src/aos/scientist_decider_receipt.py",
    "src/aos/scientist_bonsai_receipt.py",
    "src/aos/scientist_profile_output.py",
    "schemas/scientist_bonsai_wire_projection.schema.json",
    "schemas/scientist_admission_binding.schema.json",
    "schemas/scientist_admission_capture.schema.json",
    "schemas/scientist_admission_record.schema.json",
    "schemas/scientist_admission_binding_v2.schema.json",
    "schemas/scientist_admission_capture_v2.schema.json",
    "schemas/scientist_admission_record_v2.schema.json",
    "database/migrations/0018_scientist_turn_intents.sql",
    "database/migrations/0023_scientist_admission_history.sql",
)
ADMISSION_MARKERS = {
    "src/aos/scientist_admission_history.py": {
        "ScientistAdmissionHistory",
        "ScientistProfilePin",
        "ScientistProfilePinV2",
        "ScientistOutputContractPin",
        "ScientistAdmissionBindingV2",
        "ScientistAdmissionCaptureV2",
        "ScientistAdmissionRecordV2",
    },
    "src/aos/scientist_decider_receipt.py": {"validate_decider_receipt"},
    "src/aos/scientist_bonsai_receipt.py": {"validate_bonsai_receipt", "validate_profile_receipt"},
    "src/aos/scientist_profile_output.py": {
        "AdmissionProfileOutputValidator",
        "admission_profile_validator",
    },
    "services/bonsai_projection.py": {
        "projection_pin",
        "verify_projection_pin",
        "project_bonsai_response",
    },
}
TERMINAL_ADDITIONS = (
    "src/aos/scientist_terminal.py",
    "schemas/scientist_terminal_evidence.schema.json",
)
EVIDENCE_ADDITIONS = (
    "src/aos/scientist_evidence_transport.py",
    "schemas/scientist_evidence_transport.schema.json",
    "schemas/scientist_evidence_transport_request.schema.json",
)
EVIDENCE_FILES = ADMISSION_FILES + TERMINAL_ADDITIONS + EVIDENCE_ADDITIONS
EVIDENCE_PROFILE = "evidence_transport_candidate_v2"
CLIENT_ADDITIONS = ("src/aos/scientist_evidence_client.py",)
CLIENT_FILES = EVIDENCE_FILES + CLIENT_ADDITIONS
CLIENT_PROFILE = "evidence_client_candidate_v2"
JOURNAL_ADDITIONS = (
    "src/aos/scientist_evidence_journal.py",
    "database/migrations/0024_scientist_evidence_controls.sql",
    "src/aos/storage.py",
    "src/aos/dataset_audit.py",
    "schemas/dataset_audit.schema.json",
)
JOURNAL_FILES = CLIENT_FILES + JOURNAL_ADDITIONS
JOURNAL_PROFILE = "evidence_journal_candidate_v2"
RELEASE_ADDITIONS = (
    "src/aos/scientist_release_proof.py",
    "schemas/scientist_release_proof.schema.json",
    "src/aos/scientist_budget_witness.py",
    "schemas/scientist_original_budget_witness.schema.json",
)
RELEASE_FILES = JOURNAL_FILES + RELEASE_ADDITIONS
RELEASE_PROFILE = "release_proof_candidate_v1"
RETAINED_ADDITIONS = (
    "src/aos/scientist_retained_evidence_transport.py",
    "schemas/scientist_retained_evidence_transport.schema.json",
    "schemas/scientist_retained_evidence_transport_request.schema.json",
    "database/migrations/0025_scientist_retained_evidence_controls.sql",
)
RETAINED_FILES = RELEASE_FILES + RETAINED_ADDITIONS
RETAINED_PROFILE = "retained_evidence_candidate_v3"
RESOLUTION_ADDITIONS = (
    "src/aos/scientist_resolution.py",
    "database/migrations/0026_scientist_turn_resolutions.sql",
)
RESOLUTION_FILES = RETAINED_FILES + RESOLUTION_ADDITIONS
RESOLUTION_PROFILE = "resolution_candidate_v3"
RETAINED_HOST_ADDITIONS = ("src/aos/scientist_retained_host.py",)
RETAINED_HOST_FILES = RESOLUTION_FILES + RETAINED_HOST_ADDITIONS
RETAINED_HOST_PROFILE = "retained_host_candidate_v3"
PHYSICAL_ADDITIONS = ("src/aos/bounded_process.py",)
PHYSICAL_FILES = RETAINED_HOST_FILES + PHYSICAL_ADDITIONS
PHYSICAL_PROFILE = "physical_observer_candidate_v1"
BOOTSTRAP_ADDITIONS = ("src/aos/scientist_bootstrap.py",)
BOOTSTRAP_FILES = PHYSICAL_FILES + BOOTSTRAP_ADDITIONS
BOOTSTRAP_PROFILE = "bootstrap_capture_candidate_v1"
PROVIDER_ADDITIONS = ("src/aos/scientist_retained_provider.py",)
PROVIDER_FILES = BOOTSTRAP_FILES + PROVIDER_ADDITIONS
PROVIDER_PROFILE = "retained_provider_candidate_v1"
FACTORY_ADDITIONS = ("src/aos/scientist_bootstrap_factory.py",)
FACTORY_FILES = PROVIDER_FILES + FACTORY_ADDITIONS
FACTORY_PROFILE = "bootstrap_factory_candidate_v1"
CONFIGURED_SOURCE_ADDITIONS = ("src/aos/scientist_source_authority.py",)
CONFIGURED_SOURCE_FILES = FACTORY_FILES + CONFIGURED_SOURCE_ADDITIONS
CONFIGURED_SOURCE_PROFILE = "configured_source_candidate_v1"
LAB_READBACK_ADDITIONS = (
    "src/aos/scientist_lab_readbacks.py",
    "schemas/scientist_lab_readback.schema.json",
    "database/migrations/0027_scientist_lab_readbacks.sql",
)
LAB_READBACK_FILES = CONFIGURED_SOURCE_FILES + LAB_READBACK_ADDITIONS
LAB_READBACK_PROFILE = "lab_readback_history_candidate_v1"
NO_ADMISSION_ADDITIONS = (
    "src/aos/scientist_no_admission.py",
    "schemas/scientist_no_admission_observation.schema.json",
    "database/migrations/0028_scientist_no_admission_closures.sql",
)
NO_ADMISSION_FILES = LAB_READBACK_FILES + NO_ADMISSION_ADDITIONS
NO_ADMISSION_PROFILE = "no_admission_observation_candidate_v1"
NO_ADMISSION_SCHEMA_SHA256 = "92791f45ef6a319a27a5aade363832b737978080826537b8a1ffa7eef700cd63"
SCIENTIST_NO_ADMISSION_SCHEMA = (
    "lab/llm/contracts/no_admission_observation_v1/observation.schema.json"
)
NO_ADMISSION_RECOVERY_ADDITIONS = ("schemas/scientist_no_admission_recovery.schema.json",)
NO_ADMISSION_RECOVERY_FILES = NO_ADMISSION_FILES + NO_ADMISSION_RECOVERY_ADDITIONS
NO_ADMISSION_RECOVERY_PROFILE = "no_admission_retained_recovery_candidate_v1"
NO_ADMISSION_RECOVERY_SCHEMA_SHA256 = (
    "6d173eb1231989b3b3304ae08455f9b10c192dc0aa37afa54acccb223615690b"
)
SCIENTIST_NO_ADMISSION_RECOVERY_SCHEMA = (
    "lab/llm/contracts/no_admission_observation_recovery_v1/recovery.schema.json"
)
NO_ADMISSION_PROFILES = (NO_ADMISSION_PROFILE, NO_ADMISSION_RECOVERY_PROFILE)
NATIVE_PROFILES = (
    RETAINED_HOST_PROFILE,
    PHYSICAL_PROFILE,
    BOOTSTRAP_PROFILE,
    PROVIDER_PROFILE,
    FACTORY_PROFILE,
    CONFIGURED_SOURCE_PROFILE,
    LAB_READBACK_PROFILE,
    NO_ADMISSION_PROFILE,
    NO_ADMISSION_RECOVERY_PROFILE,
)
# Public API shape only; these definitions cannot attest to execution or authority.
NATIVE_METHODS = {
    NO_ADMISSION_ADDITIONS[0]: {
        "ScientistNoAdmissionVerifier": {"verify"},
        "ScientistNoAdmissionJournal": {"append"},
    },
    CONFIGURED_SOURCE_ADDITIONS[0]: {
        "ScientistConfiguredSourceVerifier": {"__init__", "__call__", "_policy_matches"},
    },
    FACTORY_ADDITIONS[0]: {
        "ScientistBootstrapAdmissionFactory": {
            "__init__",
            "confirm_runtime",
            "__call__",
            "expected_peer",
            "_persist_intent",
        },
    },
    RETAINED_HOST_ADDITIONS[0]: {
        "ScientistRetainedHost": {
            "__init__",
            "discover",
            "reconcile",
            "resolve",
            "inspect",
            "inspect_resolution",
        },
    },
    BOOTSTRAP_ADDITIONS[0]: {
        "ScientistBootstrapCodec": {
            "__init__",
            "encode_request",
            "decode_request",
            "decode_response",
        },
        "ScientistBootstrapCapture": {"__init__", "prepare", "prepare_async", "__call__"},
    },
    PROVIDER_ADDITIONS[0]: {
        "ScientistRetainedProviderClient": {"__init__", "close", "verify_peer", "exchange"},
        "ScientistRetainedProviderAdapter": {
            "__init__",
            "create_budget_verifier",
            "verify_source",
            "read_source",
            "verify_resolver",
            "verify_physical",
        },
    },
}
RETAINED_SCHEMA_SHA256 = "cbbfa1e109cf28bac8143c01975eb575b6fcfb44970d84d1c50828ca60ac1070"
# AOS's standalone request adds exact token lengths to its full-schema refs.
# This is a separately reviewed request pin, not the complete transport hash.
RETAINED_REQUEST_SHA256 = "141896bab866fbc005c7b638be265e9496ed47b0895e8dbaae91b3952e08d802"
SCIENTIST_RETAINED_SCHEMA = "docs/ai-scientist/contracts/retained-evidence-transport-v3.schema.json"
EVIDENCE_MARKERS = {
    "src/aos/scientist_terminal.py": {"ScientistTerminalReceipt", "ScientistTerminalVerifier"},
    "src/aos/scientist_evidence_transport.py": {"ScientistEvidenceCodec", "request_schema"},
}
SOURCE_PROFILES = (
    "historical_hooks",
    "runtime_v1",
    "admission_v2",
    EVIDENCE_PROFILE,
    CLIENT_PROFILE,
    JOURNAL_PROFILE,
    RELEASE_PROFILE,
    RETAINED_PROFILE,
    RESOLUTION_PROFILE,
    *NATIVE_PROFILES,
)
SNAPSHOT_KINDS = ("current_checkout", "isolated_historical", "reviewed_snapshot")
PROFILE_IDS = frozenset({"aos.decider.turn.v1", "aos.bonsai.recovery.v1", "aos.bonsai.vision.v1"})


def _git_bytes(root: Path, *arguments: str) -> bytes:
    # Even `status` may execute clean/process filters while comparing worktree
    # content. Querying effective config does not run those commands. Reject
    # configured filters before content inspection; never mutate Git config.
    filter_query = ("config", "--null", "--get-regexp", r"^filter\..*\.(clean|process)$")
    if arguments and arguments[0] in {"status", "diff"} and _git_bytes(root, *filter_query):
        raise ValueError("git_content_filter_configured")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-C", str(root), *arguments],
        capture_output=True,
        timeout=5,
        check=False,
        env=environment,
    )
    if result.returncode:
        if arguments == filter_query and result.returncode == 1:
            return b""  # No matching filter configuration.
        if arguments[:2] == ("ls-files", "--error-unmatch") and result.returncode == 1:
            raise ValueError("required_source_untracked")
        raise ValueError("git_snapshot_unavailable")
    return result.stdout


def _git(root: Path, *arguments: str) -> str:
    return _git_bytes(root, *arguments).decode("utf-8").strip()


def _git_snapshot(root: Path) -> dict[str, Any]:
    status = _git_bytes(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    return {
        "head": _git(root, "rev-parse", "HEAD"),
        "tracked_diff_sha256": hashlib.sha256(
            _git_bytes(root, "diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD")
        ).hexdigest(),
        "status_sha256": hashlib.sha256(status).hexdigest(),
        "checkout_dirty": bool(status),
        "tracked_checkout_dirty": bool(_git(root, "status", "--porcelain", "--untracked-files=no")),
    }


def _selected_tracking(root: Path, selected: tuple[str, ...]) -> set[str]:
    raw = _git_bytes(root, "ls-files", "-z", "--", *selected)
    return {os.fsdecode(name) for name in raw.split(b"\0") if name}


def _mapping_sha256(value: dict[str, str]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _source_bytes(root: Path, relative: str) -> bytes:
    path = root / relative
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("source_symlink")
        if part == root:
            break
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SOURCE_BYTES:
        raise ValueError("source_type_or_size")
    data = path.read_bytes()
    if len(data) > MAX_SOURCE_BYTES:
        raise ValueError("source_type_or_size")
    return data


def _source(root: Path, relative: str) -> tuple[bytes, ast.Module]:
    data = _source_bytes(root, relative)
    return data, ast.parse(data, filename=relative)


def _admission_selection(tree: ast.Module) -> bool:
    """Read only the two static declarations; never import/evaluate AOS code."""
    try:
        if _assignment(tree, "SOURCE_FILES") != ADMISSION_RUNTIME_FILES:
            return False
        declarations = [
            node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "SOURCE_PROFILES"
                for target in node.targets
            )
        ]
        if len(declarations) != 1 or not isinstance(declarations[0], ast.Dict):
            return False
        candidates = [
            value
            for key, value in zip(declarations[0].keys, declarations[0].values, strict=True)
            if isinstance(key, ast.Constant) and key.value == "admission_v2"
        ]
        if len(candidates) != 1:
            return False
        value = candidates[0]
        return (
            isinstance(value, ast.BinOp)
            and isinstance(value.op, ast.Add)
            and isinstance(value.left, ast.Name)
            and value.left.id == "SOURCE_FILES"
            and ast.literal_eval(value.right) == ADMISSION_FILES[len(ADMISSION_RUNTIME_FILES) :]
        )
    except (ValueError, TypeError, SyntaxError):
        return False


def _evidence_selection(
    tree: ast.Module,
    *,
    client: bool = False,
    journal: bool = False,
    release: bool = False,
    retained: bool = False,
    resolution: bool = False,
    native_profile: str | None = None,
) -> bool:
    """Verify the two exact tuple extensions without executing observer code."""
    extensions = [
        ("terminal_candidate_v1", "admission_v2", TERMINAL_ADDITIONS),
        (EVIDENCE_PROFILE, "terminal_candidate_v1", EVIDENCE_ADDITIONS),
    ]
    resolution = resolution or native_profile in NATIVE_PROFILES
    retained = retained or resolution
    if client or journal or release or retained:
        extensions.append((CLIENT_PROFILE, EVIDENCE_PROFILE, CLIENT_ADDITIONS))
    if journal or release or retained:
        extensions.append((JOURNAL_PROFILE, CLIENT_PROFILE, JOURNAL_ADDITIONS))
    if release or retained:
        extensions.append((RELEASE_PROFILE, JOURNAL_PROFILE, RELEASE_ADDITIONS))
    if retained:
        extensions.append((RETAINED_PROFILE, RELEASE_PROFILE, RETAINED_ADDITIONS))
    if resolution:
        extensions.append((RESOLUTION_PROFILE, RETAINED_PROFILE, RESOLUTION_ADDITIONS))
    native_extensions = (
        (RETAINED_HOST_PROFILE, RESOLUTION_PROFILE, RETAINED_HOST_ADDITIONS),
        (PHYSICAL_PROFILE, RETAINED_HOST_PROFILE, PHYSICAL_ADDITIONS),
        (BOOTSTRAP_PROFILE, PHYSICAL_PROFILE, BOOTSTRAP_ADDITIONS),
        (PROVIDER_PROFILE, BOOTSTRAP_PROFILE, PROVIDER_ADDITIONS),
        (FACTORY_PROFILE, PROVIDER_PROFILE, FACTORY_ADDITIONS),
        (CONFIGURED_SOURCE_PROFILE, FACTORY_PROFILE, CONFIGURED_SOURCE_ADDITIONS),
        (LAB_READBACK_PROFILE, CONFIGURED_SOURCE_PROFILE, LAB_READBACK_ADDITIONS),
        (NO_ADMISSION_PROFILE, LAB_READBACK_PROFILE, NO_ADMISSION_ADDITIONS),
        (NO_ADMISSION_RECOVERY_PROFILE, NO_ADMISSION_PROFILE, NO_ADMISSION_RECOVERY_ADDITIONS),
    )
    if native_profile in NATIVE_PROFILES:
        extensions.extend(native_extensions[: NATIVE_PROFILES.index(native_profile) + 1])
    for name, parent, suffix in extensions:
        declarations = [
            node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "SOURCE_PROFILES"
                and isinstance(target.slice, ast.Constant)
                and target.slice.value == name
                for target in node.targets
            )
        ]
        if len(declarations) != 1:
            return False
        value = declarations[0]
        if not (
            isinstance(value, ast.BinOp)
            and isinstance(value.op, ast.Add)
            and isinstance(value.left, ast.Subscript)
            and isinstance(value.left.value, ast.Name)
            and value.left.value.id == "SOURCE_PROFILES"
            and isinstance(value.left.slice, ast.Constant)
            and value.left.slice.value == parent
        ):
            return False
        try:
            if ast.literal_eval(value.right) != suffix:
                return False
        except (ValueError, TypeError, SyntaxError):
            return False
    return True


def _schema_sha256(raw: bytes) -> str:
    """Canonical full JSON content; duplicate/nonfinite values cannot be hidden."""

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_schema_key")
            result[key] = value
        return result

    value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("schema_object_required")
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _options(tree: ast.Module) -> set[str]:
    return {
        argument.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_argument"
        for argument in node.args
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
    }


def _assignment(tree: ast.Module, name: str) -> object:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            value = node.value
            if (
                isinstance(value, ast.BinOp)
                and isinstance(value.op, ast.Mult)
                and isinstance(value.left, ast.Constant)
                and type(value.left.value) is int
                and isinstance(value.right, ast.Constant)
                and type(value.right.value) is int
            ):
                return value.left.value * value.right.value
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "frozenset"
                and len(value.args) == 1
            ):
                return frozenset(ast.literal_eval(value.args[0]))
            if (
                isinstance(value, ast.Subscript)
                and isinstance(value.value, ast.Name)
                and value.value.id == "Literal"
            ):
                return ast.literal_eval(value.slice)
            return ast.literal_eval(value)
    raise ValueError("protocol_marker_missing")


def _wire_versions(
    tree: ast.Module, classes: frozenset[str] = frozenset({"GpuInferenceResult", "_WireReceipt"})
) -> list[object]:
    versions = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name not in classes:
            continue
        for field in node.body:
            if (
                isinstance(field, ast.AnnAssign)
                and isinstance(field.target, ast.Name)
                and field.target.id == "version"
                and isinstance(field.annotation, ast.Subscript)
                and isinstance(field.annotation.value, ast.Name)
                and field.annotation.value.id == "Literal"
            ):
                versions.append(ast.literal_eval(field.annotation.slice))
    return versions


def inspect_source(
    root: Path,
    expected_head: str,
    *,
    snapshot_kind: str = "current_checkout",
    source_profile: str = "historical_hooks",
    expected_selected_source_sha256: str | None = None,
    expected_tracked_diff_sha256: str | None = None,
    expected_selected_untracked_sha256: str | None = None,
) -> dict[str, Any]:
    """Reject source mismatches; leave runtime capability confirmation pending."""
    report: dict[str, Any] = {
        "schema": "aos_lab_source_preflight.v1",
        "snapshot_kind": snapshot_kind,
        "source_profile": source_profile,
        "source_root": str(root),
        "expected_head": expected_head,
        "expected_selected_source_sha256": expected_selected_source_sha256,
        "expected_tracked_diff_sha256": expected_tracked_diff_sha256,
        "expected_selected_untracked_sha256": expected_selected_untracked_sha256,
        "status": "unsupported",
        "source_ready": False,
        "source_scope": "Selected source only; not full dependency/deployment closure",
        "admission_allowed": False,
        "runtime_capability_confirmed": False,
        "checks_are": "read-only Git and AST source checks; no CLI/model/service execution",
        "issues": [],
        "source_sha256": {},
    }
    issues = report["issues"]
    if snapshot_kind not in SNAPSHOT_KINDS:
        issues.append({"code": "unsupported_snapshot_kind"})
        return report
    reviewed = snapshot_kind == "reviewed_snapshot"
    if source_profile not in SOURCE_PROFILES:
        issues.append({"code": "unsupported_source_profile"})
        return report
    required_files = {
        "historical_hooks": HISTORICAL_FILES,
        "runtime_v1": RUNTIME_FILES,
        "admission_v2": ADMISSION_FILES,
        EVIDENCE_PROFILE: EVIDENCE_FILES,
        CLIENT_PROFILE: CLIENT_FILES,
        JOURNAL_PROFILE: JOURNAL_FILES,
        RELEASE_PROFILE: RELEASE_FILES,
        RETAINED_PROFILE: RETAINED_FILES,
        RESOLUTION_PROFILE: RESOLUTION_FILES,
        RETAINED_HOST_PROFILE: RETAINED_HOST_FILES,
        PHYSICAL_PROFILE: PHYSICAL_FILES,
        BOOTSTRAP_PROFILE: BOOTSTRAP_FILES,
        PROVIDER_PROFILE: PROVIDER_FILES,
        FACTORY_PROFILE: FACTORY_FILES,
        CONFIGURED_SOURCE_PROFILE: CONFIGURED_SOURCE_FILES,
        LAB_READBACK_PROFILE: LAB_READBACK_FILES,
        NO_ADMISSION_PROFILE: NO_ADMISSION_FILES,
        NO_ADMISSION_RECOVERY_PROFILE: NO_ADMISSION_RECOVERY_FILES,
    }[source_profile]
    pins = {
        "selected_source_sha256": expected_selected_source_sha256,
        "tracked_diff_sha256": expected_tracked_diff_sha256,
        "selected_untracked_sha256": expected_selected_untracked_sha256,
    }
    for name, pin in pins.items():
        if reviewed and pin is None:
            issues.append({"code": "missing_expected_" + name})
        elif pin is not None and (
            not isinstance(pin, str) or not re.fullmatch(r"[0-9a-f]{64}", pin)
        ):
            issues.append({"code": "invalid_expected_" + name})
    if not isinstance(expected_head, str) or not re.fullmatch(r"[0-9a-f]{40}", expected_head):
        issues.append({"code": "invalid_expected_head"})
    if issues:
        return report
    try:
        if root.is_symlink() or not root.is_dir():
            raise ValueError("checkout_path_invalid")
        root = root.resolve(strict=True)
        if Path(_git(root, "rev-parse", "--show-toplevel")).resolve() != root:
            raise ValueError("wrong_checkout_root")
        before = _git_snapshot(root)
        tracked = _selected_tracking(root, required_files)
        report["observed_head"] = before["head"]
        report.update({key: value for key, value in before.items() if key != "head"})
        if report["observed_head"] != expected_head:
            issues.append({"code": "wrong_checkout_revision"})
        if before["tracked_checkout_dirty"] and not reviewed:
            issues.append({"code": "tracked_checkout_dirty"})
        trees = {}
        schemas = {}
        for relative in required_files:
            try:
                data = _source_bytes(root, relative)
                report["source_sha256"][relative] = hashlib.sha256(data).hexdigest()
                if relative not in tracked and not reviewed:
                    issues.append({"code": "required_source_untracked", "path": relative})
                if relative.endswith(".py"):
                    trees[relative] = ast.parse(data, filename=relative)
                elif (
                    source_profile in {RETAINED_PROFILE, RESOLUTION_PROFILE, *NATIVE_PROFILES}
                    and relative in RETAINED_ADDITIONS[1:3]
                ):
                    schemas[relative] = _schema_sha256(data)
                elif (
                    source_profile in NO_ADMISSION_PROFILES
                    and relative == NO_ADMISSION_ADDITIONS[1]
                ):
                    schemas[relative] = _schema_sha256(data)
                elif (
                    source_profile == NO_ADMISSION_RECOVERY_PROFILE
                    and relative == NO_ADMISSION_RECOVERY_ADDITIONS[0]
                ):
                    schemas[relative] = _schema_sha256(data)
            except FileNotFoundError:
                issues.append({"code": "required_source_missing", "path": relative})
            except (ValueError, SyntaxError, OSError) as error:
                code = str(error) if isinstance(error, ValueError) else "source_unreadable"
                issues.append({"code": code, "path": relative})
        if source_profile == "historical_hooks" and "scripts/serve_desktop.py" in trees:
            for option in sorted(HISTORICAL_OPTIONS - _options(trees["scripts/serve_desktop.py"])):
                issues.append({"code": "required_cli_option_missing", "option": option})
        if source_profile == "historical_hooks" and "src/aos/gpu_turn.py" in trees:
            try:
                client = trees["src/aos/gpu_turn.py"]
                if (
                    _assignment(client, "_BROKER_UNIT") != "swapp-lab-gpu-broker.service"
                    or _assignment(client, "_PROFILE_IDS") != PROFILE_IDS
                    or _assignment(client, "_MAX_FRAME_BYTES") != 128 * 1024
                    or _wire_versions(client) != [1, 1]
                    or any(type(version) is not int for version in _wire_versions(client))
                ):
                    issues.append({"code": "shared_gpu_protocol_incompatible"})
            except (ValueError, TypeError):
                issues.append({"code": "shared_gpu_protocol_unrecognized"})
        if source_profile != "historical_hooks":
            # These are structural source markers, never runtime attestations.
            for relative, required in RUNTIME_CLASSES.items():
                if relative in trees:
                    present = {
                        node.name for node in trees[relative].body if isinstance(node, ast.ClassDef)
                    }
                    if not required <= present:
                        issues.append({"code": "runtime_source_marker_missing", "path": relative})
            for relative in (
                "services/decider/broker_worker.py",
                "services/bonsai/broker_worker.py",
            ):
                if relative in trees and not any(
                    isinstance(node, ast.FunctionDef) and node.name == "main"
                    for node in trees[relative].body
                ):
                    issues.append({"code": "runtime_worker_entry_missing", "path": relative})
            try:
                if "src/aos/scientist_protocol.py" in trees:
                    protocol = trees["src/aos/scientist_protocol.py"]
                    profiles = _assignment(protocol, "Profile")
                    if (
                        _assignment(protocol, "MAX_FRAME_BYTES") != 128 * 1024
                        or not isinstance(profiles, tuple)
                        or frozenset(profiles) != PROFILE_IDS
                        or _wire_versions(
                            protocol, frozenset({"ScientistTurnRequest", "ScientistTurnReceipt"})
                        )
                        != [1, 1]
                        or any(
                            type(version) is not int
                            for version in _wire_versions(
                                protocol,
                                frozenset({"ScientistTurnRequest", "ScientistTurnReceipt"}),
                            )
                        )
                    ):
                        issues.append({"code": "runtime_v1_protocol_incompatible"})
                if (
                    "src/aos/scientist_transport.py" in trees
                    and _assignment(trees["src/aos/scientist_transport.py"], "BROKER_UNIT")
                    != "swapp-lab-gpu-broker.service"
                ):
                    issues.append({"code": "runtime_v1_broker_unit_incompatible"})
            except (ValueError, TypeError):
                issues.append({"code": "runtime_v1_protocol_unrecognized"})
        if source_profile not in {"historical_hooks", "runtime_v1"}:
            observer = trees.get("scripts/scientist_source_report.py")
            if observer is not None and not _admission_selection(observer):
                issues.append({"code": "admission_v2_source_selection_incompatible"})
            if (
                source_profile
                in {
                    EVIDENCE_PROFILE,
                    CLIENT_PROFILE,
                    JOURNAL_PROFILE,
                    RELEASE_PROFILE,
                    RETAINED_PROFILE,
                    RESOLUTION_PROFILE,
                    *NATIVE_PROFILES,
                }
                and observer is not None
                and not _evidence_selection(
                    observer,
                    client=source_profile == CLIENT_PROFILE,
                    journal=source_profile == JOURNAL_PROFILE,
                    release=source_profile == RELEASE_PROFILE,
                    retained=source_profile == RETAINED_PROFILE,
                    resolution=source_profile == RESOLUTION_PROFILE,
                    native_profile=source_profile if source_profile in NATIVE_PROFILES else None,
                )
            ):
                issues.append({"code": "evidence_source_selection_incompatible"})
            marker_sets = dict(ADMISSION_MARKERS)
            if source_profile in {
                EVIDENCE_PROFILE,
                CLIENT_PROFILE,
                JOURNAL_PROFILE,
                RELEASE_PROFILE,
                RETAINED_PROFILE,
                RESOLUTION_PROFILE,
                *NATIVE_PROFILES,
            }:
                marker_sets.update(EVIDENCE_MARKERS)
            if source_profile in {
                CLIENT_PROFILE,
                JOURNAL_PROFILE,
                RELEASE_PROFILE,
                RETAINED_PROFILE,
                RESOLUTION_PROFILE,
                *NATIVE_PROFILES,
            }:
                marker_sets[CLIENT_ADDITIONS[0]] = {"ScientistEvidenceClient"}
            if source_profile in {
                JOURNAL_PROFILE,
                RELEASE_PROFILE,
                RETAINED_PROFILE,
                RESOLUTION_PROFILE,
                *NATIVE_PROFILES,
            }:
                marker_sets[JOURNAL_ADDITIONS[0]] = {"ScientistEvidenceJournal"}
            if source_profile in {
                RELEASE_PROFILE,
                RETAINED_PROFILE,
                RESOLUTION_PROFILE,
                *NATIVE_PROFILES,
            }:
                marker_sets[RELEASE_ADDITIONS[0]] = {"ScientistReleaseProofVerifier"}
                marker_sets[RELEASE_ADDITIONS[2]] = {
                    "ScientistBudgetWitnessVerifier",
                    "ScientistRetainedTerminalVerifier",
                }
            if source_profile in {RETAINED_PROFILE, RESOLUTION_PROFILE, *NATIVE_PROFILES}:
                marker_sets[RETAINED_ADDITIONS[0]] = {"ScientistRetainedEvidenceCodec"}
            if source_profile in {RESOLUTION_PROFILE, *NATIVE_PROFILES}:
                marker_sets[RESOLUTION_ADDITIONS[0]] = {"ScientistResolutionJournal"}
            if source_profile in NATIVE_PROFILES:
                marker_sets[RETAINED_HOST_ADDITIONS[0]] = {"ScientistRetainedHost"}
                if source_profile in {
                    PHYSICAL_PROFILE,
                    BOOTSTRAP_PROFILE,
                    PROVIDER_PROFILE,
                    FACTORY_PROFILE,
                    CONFIGURED_SOURCE_PROFILE,
                    LAB_READBACK_PROFILE,
                    NO_ADMISSION_PROFILE,
                    NO_ADMISSION_RECOVERY_PROFILE,
                }:
                    marker_sets[PHYSICAL_ADDITIONS[0]] = {"run_bounded"}
                if source_profile in {
                    BOOTSTRAP_PROFILE,
                    PROVIDER_PROFILE,
                    FACTORY_PROFILE,
                    CONFIGURED_SOURCE_PROFILE,
                    LAB_READBACK_PROFILE,
                    NO_ADMISSION_PROFILE,
                    NO_ADMISSION_RECOVERY_PROFILE,
                }:
                    marker_sets[BOOTSTRAP_ADDITIONS[0]] = set(
                        NATIVE_METHODS[BOOTSTRAP_ADDITIONS[0]]
                    )
                if source_profile in {
                    PROVIDER_PROFILE,
                    FACTORY_PROFILE,
                    CONFIGURED_SOURCE_PROFILE,
                    LAB_READBACK_PROFILE,
                    NO_ADMISSION_PROFILE,
                    NO_ADMISSION_RECOVERY_PROFILE,
                }:
                    marker_sets[PROVIDER_ADDITIONS[0]] = {
                        *NATIVE_METHODS[PROVIDER_ADDITIONS[0]],
                        "prepare_retained_provider_channel",
                    }
                if source_profile in {
                    FACTORY_PROFILE,
                    CONFIGURED_SOURCE_PROFILE,
                    LAB_READBACK_PROFILE,
                    NO_ADMISSION_PROFILE,
                    NO_ADMISSION_RECOVERY_PROFILE,
                }:
                    marker_sets[FACTORY_ADDITIONS[0]] = set(NATIVE_METHODS[FACTORY_ADDITIONS[0]])
                if source_profile in {
                    CONFIGURED_SOURCE_PROFILE,
                    LAB_READBACK_PROFILE,
                    NO_ADMISSION_PROFILE,
                    NO_ADMISSION_RECOVERY_PROFILE,
                }:
                    marker_sets[CONFIGURED_SOURCE_ADDITIONS[0]] = set(
                        NATIVE_METHODS[CONFIGURED_SOURCE_ADDITIONS[0]]
                    )
                if source_profile in {LAB_READBACK_PROFILE, *NO_ADMISSION_PROFILES}:
                    marker_sets[LAB_READBACK_ADDITIONS[0]] = {
                        "ScientistLabReadbacks",
                        "ScientistLabReadback",
                    }
                    marker_sets.setdefault("src/aos/scientist_transport.py", set()).add(
                        "SystemdCallerAuthenticator"
                    )
                native_methods = dict(NATIVE_METHODS)
                if source_profile in NO_ADMISSION_PROFILES:
                    marker_sets[NO_ADMISSION_ADDITIONS[0]] = {
                        "ScientistNoAdmissionVerifier",
                        "ScientistNoAdmissionJournal",
                        "sqlite_store_identity",
                    }
                if source_profile == NO_ADMISSION_RECOVERY_PROFILE:
                    recovery_methods = {
                        "ScientistNoAdmissionRetainedVerifier": {"verify"},
                        "ScientistNoAdmissionRetainedJournal": {"append"},
                    }
                    marker_sets[NO_ADMISSION_ADDITIONS[0]].update(recovery_methods)
                    native_methods[NO_ADMISSION_ADDITIONS[0]] = {
                        **native_methods[NO_ADMISSION_ADDITIONS[0]],
                        **recovery_methods,
                    }
                if source_profile in {LAB_READBACK_PROFILE, *NO_ADMISSION_PROFILES}:
                    native_methods["src/aos/scientist_transport.py"] = {
                        "SystemdCallerAuthenticator": {"authenticate", "still_current"},
                    }
                async_methods = {}
                if source_profile in {
                    BOOTSTRAP_PROFILE,
                    PROVIDER_PROFILE,
                    FACTORY_PROFILE,
                    CONFIGURED_SOURCE_PROFILE,
                    LAB_READBACK_PROFILE,
                    NO_ADMISSION_PROFILE,
                    NO_ADMISSION_RECOVERY_PROFILE,
                }:
                    native_methods["src/aos/scientist_desktop.py"] = {
                        "ScientistDesktopBinding": {"prepare_infer"},
                    }
                    async_methods = {
                        (BOOTSTRAP_ADDITIONS[0], "ScientistBootstrapCapture"): {"prepare_async"},
                        ("src/aos/scientist_desktop.py", "ScientistDesktopBinding"): {
                            "prepare_infer"
                        },
                    }
                for relative, classes in native_methods.items():
                    if relative not in trees:
                        continue
                    for name, methods in classes.items():
                        declarations = [
                            node
                            for node in trees[relative].body
                            if isinstance(node, ast.ClassDef) and node.name == name
                        ]
                        present = (
                            {
                                node.name
                                for node in declarations[0].body
                                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                            }
                            if len(declarations) == 1
                            else set()
                        )
                        present_async = (
                            {
                                node.name
                                for node in declarations[0].body
                                if isinstance(node, ast.AsyncFunctionDef)
                            }
                            if len(declarations) == 1
                            else set()
                        )
                        if (
                            not methods <= present
                            or not async_methods.get((relative, name), set()) <= present_async
                            or (relative == NO_ADMISSION_ADDITIONS[0] and methods & present_async)
                        ):
                            issues.append(
                                {
                                    "code": "native_source_api_missing",
                                    "path": relative,
                                    "class": name,
                                }
                            )
            for relative, markers in marker_sets.items():
                if relative in trees:
                    present = {
                        node.name
                        for node in trees[relative].body
                        if isinstance(node, (ast.ClassDef, ast.FunctionDef))
                    }
                    if not markers <= present:
                        issues.append(
                            {"code": "admission_v2_source_marker_missing", "path": relative}
                        )
        if source_profile in {RETAINED_PROFILE, RESOLUTION_PROFILE, *NATIVE_PROFILES}:
            scientist_root = Path(__file__).resolve().parents[1]
            local_schema = _source_bytes(scientist_root, SCIENTIST_RETAINED_SCHEMA)
            local_pin = _schema_sha256(local_schema)
            report["retained_schema_sha256"] = schemas.get(RETAINED_ADDITIONS[1])
            report["retained_request_schema_sha256"] = schemas.get(RETAINED_ADDITIONS[2])
            report["scientist_retained_schema_sha256"] = local_pin
            if (
                local_pin != RETAINED_SCHEMA_SHA256
                or schemas.get(RETAINED_ADDITIONS[1]) != local_pin
            ):
                issues.append({"code": "retained_full_schema_mismatch"})
            if schemas.get(RETAINED_ADDITIONS[2]) != RETAINED_REQUEST_SHA256:
                issues.append({"code": "retained_request_schema_mismatch"})
            if _source_bytes(scientist_root, SCIENTIST_RETAINED_SCHEMA) != local_schema:
                issues.append({"code": "scientist_schema_changed_during_preflight"})
        if source_profile in NO_ADMISSION_PROFILES:
            scientist_root = Path(__file__).resolve().parents[1]
            local_schema = _source_bytes(scientist_root, SCIENTIST_NO_ADMISSION_SCHEMA)
            local_pin = _schema_sha256(local_schema)
            report["no_admission_schema_sha256"] = schemas.get(NO_ADMISSION_ADDITIONS[1])
            report["scientist_no_admission_schema_sha256"] = local_pin
            report["no_admission_feature_admitted"] = False
            if (
                local_pin != NO_ADMISSION_SCHEMA_SHA256
                or schemas.get(NO_ADMISSION_ADDITIONS[1]) != local_pin
            ):
                issues.append({"code": "no_admission_full_schema_mismatch"})
            if _source_bytes(scientist_root, SCIENTIST_NO_ADMISSION_SCHEMA) != local_schema:
                issues.append({"code": "scientist_no_admission_schema_changed_during_preflight"})
            no_admission = trees.get(NO_ADMISSION_ADDITIONS[0])
            if no_admission is not None:
                try:
                    required_markers = {
                        "NAME": "aos-scientist-no-admission-observation.v1",
                        "VERSION": 1,
                        "FEATURE": "no-admission-observation.v1",
                        "SCHEMA_SHA256": NO_ADMISSION_SCHEMA_SHA256,
                    }
                    if source_profile == NO_ADMISSION_RECOVERY_PROFILE:
                        required_markers["RECOVERY_SCHEMA_SHA256"] = (
                            NO_ADMISSION_RECOVERY_SCHEMA_SHA256
                        )
                    for name, expected_marker in required_markers.items():
                        assignments = [
                            node
                            for node in no_admission.body
                            if isinstance(node, ast.Assign)
                            and any(
                                isinstance(target, ast.Name) and target.id == name
                                for target in node.targets
                            )
                        ]
                        observed_marker = _assignment(no_admission, name)
                        if (
                            len(assignments) != 1
                            or type(observed_marker) is not type(expected_marker)
                            or observed_marker != expected_marker
                        ):
                            raise ValueError("no_admission_protocol_incompatible")
                except (ValueError, TypeError):
                    issues.append({"code": "no_admission_protocol_incompatible"})
        if source_profile == NO_ADMISSION_RECOVERY_PROFILE:
            scientist_root = Path(__file__).resolve().parents[1]
            local_schema = _source_bytes(scientist_root, SCIENTIST_NO_ADMISSION_RECOVERY_SCHEMA)
            local_pin = _schema_sha256(local_schema)
            report["no_admission_recovery_schema_sha256"] = schemas.get(
                NO_ADMISSION_RECOVERY_ADDITIONS[0]
            )
            report["scientist_no_admission_recovery_schema_sha256"] = local_pin
            report["no_admission_recovery_feature_admitted"] = False
            if (
                local_pin != NO_ADMISSION_RECOVERY_SCHEMA_SHA256
                or schemas.get(NO_ADMISSION_RECOVERY_ADDITIONS[0]) != local_pin
            ):
                issues.append({"code": "no_admission_recovery_full_schema_mismatch"})
            if (
                _source_bytes(scientist_root, SCIENTIST_NO_ADMISSION_RECOVERY_SCHEMA)
                != local_schema
            ):
                issues.append(
                    {"code": "scientist_no_admission_recovery_schema_changed_during_preflight"}
                )
        # Bind file bytes, tracking membership and Git state across both reads.
        for relative, digest in report["source_sha256"].items():
            if hashlib.sha256(_source_bytes(root, relative)).hexdigest() != digest:
                issues.append({"code": "source_changed_during_preflight", "path": relative})
        if _selected_tracking(root, required_files) != tracked:
            issues.append({"code": "selected_tracking_changed_during_preflight"})
        if _git_snapshot(root) != before:
            issues.append({"code": "checkout_changed_during_preflight"})
        if set(report["source_sha256"]) == set(required_files):
            report["selected_source_sha256"] = _mapping_sha256(report["source_sha256"])
            report["selected_untracked_source_sha256"] = {
                name: value
                for name, value in report["source_sha256"].items()
                if name not in tracked
            }
            report["selected_untracked_sha256"] = _mapping_sha256(
                report["selected_untracked_source_sha256"]
            )
            if (
                expected_selected_source_sha256 is not None
                and report["selected_source_sha256"] != expected_selected_source_sha256
            ):
                issues.append({"code": "selected_source_pin_mismatch"})
        for name in ("tracked_diff_sha256", "selected_untracked_sha256"):
            if pins[name] is not None and report.get(name) != pins[name]:
                issues.append({"code": name.removesuffix("_sha256") + "_pin_mismatch"})
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        code = str(error) if isinstance(error, ValueError) else "checkout_unavailable"
        issues.append({"code": code})
    if not issues:
        report["status"] = "pending"
        report["source_ready"] = True
        issues.append({"code": "joint_runtime_capability_confirmation_pending"})
    report["authority"] = "existing Lab shared broker only; allocation, auth and fencing unchanged"
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aos-root", type=Path, default=Path("/home/cachyos/aos"))
    parser.add_argument("--expected-head", required=True, help="Exact reviewed AOS Git commit")
    parser.add_argument(
        "--snapshot-kind",
        choices=SNAPSHOT_KINDS,
        default="current_checkout",
    )
    parser.add_argument("--source-profile", choices=SOURCE_PROFILES, required=True)
    parser.add_argument(
        "--expected-selected-source-sha256", help="Optional reviewed selected-source SHA-256"
    )
    parser.add_argument("--expected-tracked-diff-sha256")
    parser.add_argument("--expected-selected-untracked-sha256")
    args = parser.parse_args(argv)
    report = inspect_source(
        args.aos_root,
        args.expected_head,
        snapshot_kind=args.snapshot_kind,
        source_profile=args.source_profile,
        expected_selected_source_sha256=args.expected_selected_source_sha256,
        expected_tracked_diff_sha256=args.expected_tracked_diff_sha256,
        expected_selected_untracked_sha256=args.expected_selected_untracked_sha256,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 3 if report["status"] == "pending" else 2


if __name__ == "__main__":
    raise SystemExit(main())
