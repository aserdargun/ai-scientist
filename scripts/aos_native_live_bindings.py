"""Capture reviewed bindings from real user services; no ACK, allocator or GPU access.

Call after the canonical broker and AOS controller services are active. Expected
file hashes come from the independently reviewed configuration. This observation
does not replace artifact verification, current rights checks or runtime admission.
An optional live Lab principal is verified, but is not part of the wire binding.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import asdict
from pathlib import Path

from lab.llm.aos_gpu_control import (
    CONTROL_SCHEMA_HASH,
    INFER_SCHEMA_HASH,
    SCHEMA,
    ControlPolicy,
    digest,
)
from lab.llm.aos_gpu_control_store import (
    TERMINAL_SCHEMA_HASH,
    control_deadline,
    validate_admission_binding,
)
from lab.llm.aos_gpu_executor import PROJECT_ROOT, SystemdSocketPeerAuthenticator
from lab.llm.aos_gpu_service import BROKER_UNIT, PROFILE_KEYS, load_profiles
from lab.llm.aos_profile_output import decode
from lab.llm.gpu_scheduler import (
    PrincipalReceipt,
    ProcessIdentity,
    SystemdPrincipalResolver,
    _boot_id,
    _principal_unit_matches,
    _process_cgroup,
    _read_process_identity,
    _systemctl_show,
)
from scripts.aos_configured_runtime_factory import _profile_config_sha256
from scripts.check_aos_model_environment import read_regular

CAPTURE_SECONDS = 10.0


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _remaining():
    deadline = control_deadline.get()
    remaining = 0 if deadline is None else deadline - time.monotonic()
    _require(remaining > 0, "Live binding capture deadline expired")
    return remaining


def _reviewed_file(path, expected):
    _remaining()
    path = Path(path)
    _require(
        path.is_absolute() and ".." not in path.parts, "Explicit absolute receipt path required"
    )
    _require(
        type(expected) is str and re.fullmatch(r"[a-f0-9]{64}", expected) is not None,
        "Independent SHA-256 receipt required",
    )
    raw = read_regular(path, 65536)
    _require(hashlib.sha256(raw).hexdigest() == expected, "Reviewed file receipt changed")
    _remaining()
    return decode(raw, limit=65536)


def _user_bus():
    runtime = Path("/run/user") / str(os.getuid())
    bus = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    _require(
        os.getuid() != 0
        and os.environ.get("XDG_RUNTIME_DIR") == str(runtime)
        and not runtime.is_symlink()
        and runtime.is_dir()
        and runtime.stat().st_uid == os.getuid()
        and re.fullmatch(
            rf"unix:path={re.escape(str(runtime / 'bus'))}"
            r"(?:,guid=[a-f0-9]{32})?",
            bus,
        )
        is not None,
        "Live capture requires the actual non-root user systemd bus",
    )


def _generation(unit, owner, *, require_slice):
    values = _systemctl_show(unit, owner=owner, timeout=min(3.0, _remaining()))
    pid_text = values["MainPID"]
    _require(pid_text.isdecimal() and int(pid_text) > 1, "Service MainPID is unavailable")
    pid = int(pid_text)
    identity = _read_process_identity(pid)
    group = values["ControlGroup"]
    _require(
        identity is not None
        and identity.boot_id == _boot_id()
        and values["LoadState"] == "loaded"
        and values["ActiveState"] == "active"
        and re.fullmatch(r"[a-f0-9]{32}", values["InvocationID"]) is not None
        and group.startswith("/")
        and ".." not in Path(group).parts
        and group.endswith("/" + unit)
        and _process_cgroup(pid) == group
        and (not require_slice or "/swapp-gpu.slice/" in group)
        and Path(f"/proc/{pid}").stat().st_uid == os.getuid(),
        "Service is not the current same-user canonical generation",
    )
    _require(
        _read_process_identity(pid) == identity
        and _systemctl_show(unit, owner=owner, timeout=min(3.0, _remaining())) == values,
        "Service generation changed during observation",
    )
    return {
        **asdict(identity),
        "uid": os.getuid(),
        "unit": unit,
        "invocation_id": values["InvocationID"],
        "control_group": group,
    }


def capture_reviewed_bindings(
    profile_config_path,
    expected_profile_config_sha256,
    policy_path,
    expected_policy_file_sha256,
    *,
    aos_unit,
    lab_unit,
):
    """Observe all three full bindings, optionally checking a live Lab service.

    ``lab_unit=None`` allows initial AOS-only capture. A supplied Lab service must
    already be running in the same fixed GPU slice. No service is started here.
    The caller binding identifies the AOS service MainPID, never this helper PID.
    """
    original_deadline = control_deadline.get()
    deadline = time.monotonic() + CAPTURE_SECONDS
    token = control_deadline.set(
        min(deadline, original_deadline) if original_deadline is not None else deadline
    )
    try:
        _user_bus()
        _require(_principal_unit_matches("aos", aos_unit), "AOS GPU principal unit required")
        authenticator = SystemdSocketPeerAuthenticator(aos_unit)
        resolver = (
            None
            if lab_unit is None
            else SystemdPrincipalResolver({"aos": aos_unit, "lab": lab_unit})
        )
        profile_config_path, policy_path = Path(profile_config_path), Path(policy_path)
        config = _reviewed_file(profile_config_path, expected_profile_config_sha256)
        expected_policy = _reviewed_file(policy_path, expected_policy_file_sha256)
        _require(
            type(config) is dict
            and set(config) == {"schema", "source_root", "profiles"}
            and type(config["source_root"]) is str
            and Path(config["source_root"]).is_absolute()
            and type(config["profiles"]) is dict
            and set(config["profiles"]) == PROFILE_KEYS,
            "Reviewed three-profile configuration required",
        )
        source_root = Path(config["source_root"])
        profiles = load_profiles(profile_config_path, source_root=source_root)
        policy = ControlPolicy(policy_path, profiles=profiles, source_root=PROJECT_ROOT)
        _require(
            type(expected_policy) is dict
            and policy.config == expected_policy
            and policy.config.get("enabled") is True
            and policy.config.get("caller_unit") == aos_unit,
            "Reviewed enabled policy for this AOS principal required",
        )
        policy.verify()
        server = _generation(BROKER_UNIT, "lab", require_slice=False)
        caller = _generation(aos_unit, "aos", require_slice=True)
        peer = authenticator.authenticate(caller["pid"], os.getuid())
        _require(
            asdict(peer)
            == {**caller, "parent_pid": caller["pid"], "parent_start_ticks": caller["start_ticks"]}
            and peer.boot_id == server["boot_id"],
            "Authenticated caller differs from the original service MainPID",
        )
        lab = None if resolver is None else _generation(lab_unit, "lab", require_slice=True)
        if lab is not None:
            receipt = PrincipalReceipt(
                "lab",
                ProcessIdentity(lab["pid"], lab["start_ticks"], lab["boot_id"]),
                lab_unit,
                lab["invocation_id"],
            )
            _require(resolver.verify(receipt), "Lab GPU principal verification failed")
        bindings = {}
        for profile_id, entry in config["profiles"].items():
            profile = profiles.get(profile_id, entry["deployment_digest"])
            _require(
                profile.config_sha256 == _profile_config_sha256(profile_id, entry, source_root),
                "Loaded profile differs from the reviewed file",
            )
            bindings[profile_id] = validate_admission_binding(
                {
                    "server_generation": dict(server),
                    "caller_generation": asdict(peer),
                    "policy_sha256": policy.sha256,
                    "source_fingerprints": {
                        key: digest(value) for key, value in policy.config["source_files"].items()
                    },
                    "profile_id": profile_id,
                    "profile_pin": policy._pin(profile),
                    "infer_schema": {
                        "name": "aos-scientist-runtime.v1",
                        "version": 1,
                        "sha256": INFER_SCHEMA_HASH,
                    },
                    "control_schema": {"name": SCHEMA, "version": 1, "sha256": CONTROL_SCHEMA_HASH},
                    "terminal_schema": {
                        "name": "aos-scientist-terminal.v1",
                        "version": 1,
                        "sha256": TERMINAL_SCHEMA_HASH,
                    },
                },
                require_output_contract=True,
            )
        policy.verify()
        _require(
            _reviewed_file(profile_config_path, expected_profile_config_sha256) == config
            and _reviewed_file(policy_path, expected_policy_file_sha256) == expected_policy,
            "Reviewed configuration changed during capture",
        )
        _require(
            _generation(BROKER_UNIT, "lab", require_slice=False) == server
            and _generation(aos_unit, "aos", require_slice=True) == caller
            and authenticator.authenticate(peer.pid, peer.uid) == peer
            and authenticator.still_current(peer),
            "Original service generation changed",
        )
        if lab is not None:
            _require(
                _generation(lab_unit, "lab", require_slice=True) == lab
                and resolver.verify(receipt),
                "Original Lab GPU principal changed",
            )
        _remaining()
        return bindings
    finally:
        control_deadline.reset(token)
