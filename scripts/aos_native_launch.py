"""Explicit pinned native launch composition; never enables policy."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import inspect
import json
import math
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
import traceback
from pathlib import Path

from lab.llm.aos_gpu_control import ControlPolicy
from lab.llm.aos_gpu_control_store import control_deadline
from lab.llm.aos_gpu_service import load_profiles
from scripts.aos_configured_runtime_factory import ConfiguredScientistAdmissionFactory
from scripts.aos_native_artifact_receipts import (
    MAX_VALIDITY_SECONDS,
    NativeArtifactReceiptProvider,
    build_receipt,
    canonical,
)
from scripts.aos_native_live_bindings import capture_reviewed_bindings
from scripts.check_aos_model_environment import read_regular

STARTUP_SECONDS = 120.0
VERIFICATION_OUTPUT_BYTES = 65536
VERIFICATION_REAP_SECONDS = 0.2
SHARED_CALLER_UNIT = "swapp-aos-gpu-shared-desktop-default.service"
SHARED_SCOPE_KEYS = {
    "plan_path",
    "plan_sha256",
    "provision_path",
    "provision_sha256",
    "activation_path",
}


def _shared_scope_contract(cfg):
    """The shared caller must name its exact filesystem preparation, never infer it."""
    if cfg["caller_unit"] != SHARED_CALLER_UNIT:
        if "shared_scope" in cfg or "shared_launch_runtime" in cfg:
            raise ValueError("Legacy acceptance cannot carry shared scope")
        return None
    scope = cfg.get("shared_scope")
    if type(scope) is not dict or set(scope) != SHARED_SCOPE_KEYS:
        raise ValueError("Shared caller requires the exact reviewed shared scope")
    if any(type(value) is not str or not value for value in scope.values()):
        raise ValueError("Shared scope values must be explicit nonempty strings")
    for name in ("plan_path", "provision_path", "activation_path"):
        path = Path(scope[name])
        if (
            not path.is_absolute()
            or ".." in path.parts
            or str(path) != scope[name]
            or any(ord(character) < 32 or ord(character) == 127 for character in scope[name])
        ):
            raise ValueError("Shared scope paths must be exact canonical absolute paths")
    if any(
        re.fullmatch(r"[a-f0-9]{64}", scope[name]) is None
        for name in ("plan_sha256", "provision_sha256")
    ):
        raise ValueError("Shared scope hashes must be exact lowercase SHA256 pins")
    return scope


def _shared_launch_command(transport_type, plan, activation, scientist_root):
    """Bind the current AOS builder to the independently reviewed Scientist root."""
    root = Path(scientist_root)
    if not root.is_absolute() or root.resolve(strict=True) != root or not root.is_dir():
        raise ValueError("Shared builder requires the exact canonical Scientist source root")
    message = "Unsupported shared Desktop transport API: explicit scientist_root is required"
    try:
        signature = inspect.signature(transport_type)
        parameter = signature.parameters.get("scientist_root")
        if parameter is None or parameter.kind not in {
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }:
            raise ValueError(message)
        signature.bind(scientist_root=root)
    except (TypeError, ValueError) as error:
        raise ValueError(message) from error
    transport = transport_type(scientist_root=root)
    try:
        builder = transport.launch_command
        inspect.signature(builder).bind(plan, activation)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("Unsupported shared Desktop launch_command API") from error
    return builder(plan, activation)


def _shared_scope_preflight(cfg, args, static, path, expected, deadline):
    """Read-only scope integrity; existing live bindings/factory retain all authority gates."""
    scope = _shared_scope_contract(cfg)
    if scope is None:
        return
    _remaining(deadline)
    root = Path(args["source_roots"]["aos"])
    sources = args["source_files"]["aos"]
    if not root.is_absolute() or root.resolve(strict=True) != root:
        raise ValueError("Shared preflight requires the exact canonical AOS source root")
    required = {
        "src/aos/" + name + ".py"
        for name in (
            "shared_desktop_plan",
            "shared_desktop_provision",
            "shared_desktop_host",
            "workspace_identity",
            "contracts",
            "__init__",
        )
    }
    if type(sources) is not dict or not required <= sources.keys() or len(sources) > 256:
        raise ValueError("Shared preflight requires the bounded complete helper source closure")
    # Verify every reviewed AOS source before a transitive module can execute during import.
    for relative, checksum in sources.items():
        if (
            type(relative) is not str
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or str(Path(relative)) != relative
        ):
            raise ValueError("Shared source closure requires canonical relative paths")
        candidate = root / relative
        if (
            not checksum
            or static["source_inputs"].get(str(candidate)) != checksum
            or hashlib.sha256(read_regular(candidate, 8 * 1024**2)).hexdigest() != checksum
        ):
            raise ValueError("Shared preflight helper lacks independent source pins")
        _remaining(deadline)
    from aos.contracts import canonical as aos_canonical
    from aos.shared_desktop_host import SystemdSharedDesktopTransport
    from aos.shared_desktop_plan import (
        load_activation,
        load_plan,
        read_pinned_file,
        read_private_file,
    )
    from aos.shared_desktop_provision import (
        LAUNCH_INTENT_NAME,
        PROVISION_NAME,
        SharedDesktopLaunchIntent,
        load_provision,
        verify_launch_intent,
    )
    from aos.workspace_identity import open_existing_workspace, workspace_identity

    # Include imported AOS dependencies in the independently reviewed source closure.
    for name, module in tuple(sys.modules.items()):
        if name != "aos" and not name.startswith("aos."):
            continue
        candidate = Path(getattr(module, "__file__", "")).absolute()
        if not candidate.is_relative_to(root):
            raise ValueError("Shared preflight imported AOS from another source root")
        checksum = sources.get(str(candidate.relative_to(root)))
        if (
            not checksum
            or static["source_inputs"].get(str(candidate)) != checksum
            or hashlib.sha256(read_regular(candidate, 8 * 1024**2)).hexdigest() != checksum
        ):
            raise ValueError("Shared preflight dependency lacks independent source pins")
        _remaining(deadline)
    plan = load_plan(Path(scope["plan_path"]), scope["plan_sha256"])
    if (
        plan.plan_sha256() != scope["plan_sha256"]
        or plan.template.caller_unit != cfg["caller_unit"]
    ):
        raise ValueError("Shared plan is not its exact canonical reviewed selection")
    provision_path = Path(scope["provision_path"])
    if provision_path != Path(plan.session_directory) / PROVISION_NAME:
        raise ValueError("Shared provision receipt is not at the fixed session path")
    marker_raw = read_private_file(Path(plan.session_directory) / LAUNCH_INTENT_NAME, 16384)
    marker = SharedDesktopLaunchIntent.model_validate_json(marker_raw, strict=True)
    if aos_canonical(marker.model_dump(mode="json")).encode() != marker_raw:
        raise ValueError("Shared launch intent requires canonical private bytes")
    activation = load_activation(Path(scope["activation_path"]), marker.activation_sha256)
    boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    now = time.clock_gettime(time.CLOCK_BOOTTIME)
    if (
        activation.plan_sha256 != scope["plan_sha256"]
        or activation.reviewed_launch_input_path != str(Path(path).absolute())
        or activation.reviewed_launch_input_sha256 != expected
        or activation.source_files != plan.template.source_files
        or activation.source_sha256 != plan.template.source_sha256
        or activation.boot_id != boot
        or not activation.issued_monotonic <= now < activation.expires_monotonic
    ):
        raise ValueError(
            "Shared activation differs from reviewed input, plan, boot or original expiry"
        )
    read_pinned_file(Path(path).absolute(), expected, limit=512 * 1024, private=True)
    if any(
        type(files) is not dict or files.get(str(provision_path)) != scope["provision_sha256"]
        for files in (activation.config_files, args.get("config_files"))
    ):
        raise ValueError("Shared activation and factory must pin the exact provision receipt")
    retained_keys = {"retained_review_path", "retained_review_sha256"} & cfg.keys()
    if retained_keys:
        if len(retained_keys) != 2:
            raise ValueError("Retained review requires both reviewed path and SHA")
        retained_path, retained_sha = cfg["retained_review_path"], cfg["retained_review_sha256"]
        if activation.config_files.get(retained_path) != retained_sha:
            raise ValueError("Shared activation must pin the exact retained review")
        review = json.loads(
            read_pinned_file(Path(retained_path), retained_sha, limit=65536, private=True)
        )
        if (
            type(review) is not dict
            or review.get("schema") != "scientist.native-retained-factory-review.v1"
            or type(review.get("config_files")) is not dict
            or review["config_files"].get(str(provision_path)) != scope["provision_sha256"]
        ):
            raise ValueError("Shared retained review must pin the exact provision receipt")
    provision = load_provision(
        plan,
        provision_path,
        scope["provision_sha256"],
        current_boot_id=boot,
        require_pristine=False,
    )
    if (
        verify_launch_intent(
            plan, scope["provision_sha256"], marker.activation_sha256, current_boot_id=boot
        )
        != marker
    ):
        raise ValueError("Shared launch intent changed during scope verification")
    command = _shared_launch_command(
        SystemdSharedDesktopTransport, plan, activation, args["source_roots"]["scientist"]
    )
    desktop = command[command.index("--expected-launch-input-sha256") + 2 :]
    if sys.argv[1:] != desktop:
        raise ValueError("Shared Desktop argv differs from the exact reviewed builder output")
    descriptors = []
    try:
        for directory, identity in (
            (plan.session_directory, provision.session_identity),
            (plan.workspace, provision.workspace_identity),
        ):
            descriptor = open_existing_workspace(Path(directory))
            descriptors.append((directory, identity, descriptor))
            if (
                workspace_identity(Path(directory), descriptor) != identity
                or stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o700
            ):
                raise ValueError("Shared session/workspace identity or private mode changed")
        if os.listdir(descriptors[1][2]) or set(os.listdir(descriptors[0][2])) != {
            "workspace",
            PROVISION_NAME,
            LAUNCH_INTENT_NAME,
        }:
            raise ValueError(
                "Shared claimed scope must contain only an empty workspace and two receipts"
            )
        for directory, identity, descriptor in descriptors:
            current = open_existing_workspace(Path(directory))
            try:
                if any(
                    workspace_identity(Path(directory), observed) != identity
                    or stat.S_IMODE(os.fstat(observed).st_mode) != 0o700
                    for observed in (descriptor, current)
                ):
                    raise ValueError("Shared session/workspace was replaced during preflight")
            finally:
                os.close(current)
        load_provision(
            plan,
            provision_path,
            scope["provision_sha256"],
            current_boot_id=boot,
            require_pristine=False,
        )
    finally:
        for _directory, _identity, descriptor in reversed(descriptors):
            os.close(descriptor)
    _remaining(deadline)
    return {
        "intent_sha256": hashlib.sha256(marker_raw).hexdigest(),
        "session_directory": plan.session_directory,
        "workspace": plan.workspace,
        "workspace_device": provision.workspace_identity.device,
        "workspace_inode": provision.workspace_identity.inode,
        "workspace_uid": provision.workspace_identity.owner_uid,
        "app_session": plan.app_session,
        "boot_id": boot,
        "issued_boottime": activation.issued_monotonic,
        "expires_boottime": activation.expires_monotonic,
        "activation_config_paths": frozenset(activation.config_files),
    }


def _shared_launch_runtime(cfg, args, static, bindings, expected, verified, deadline):
    """Verify the in-unit client source before constructing any socket client."""
    root = Path(args["source_roots"]["scientist"])
    required = (
        "scripts/aos_shared_launch_runtime.py",
        "lab/llm/shared_launch_authority.py",
        "lab/llm/shared_launch_ledger.py",
        "lab/llm/shared_launch_transport.py",
    )
    for relative in required:
        path = root / relative
        pin = args["source_files"].get("scientist", {}).get(relative)
        if (
            not pin
            or static["source_inputs"].get(str(path)) != pin
            or hashlib.sha256(read_regular(path, 8 * 1024**2)).hexdigest() != pin
        ):
            raise ValueError("Shared launch runtime client lacks independent source pins")
        _remaining(deadline)
    from scripts import aos_shared_launch_runtime as runtime

    for relative in required:
        module_name = relative.removesuffix(".py").replace("/", ".")
        module = sys.modules[module_name]
        if Path(module.__file__).absolute() != root / relative:
            raise ValueError("Shared launch runtime imported another Scientist source root")
    return runtime.build_shared_launch_runtime(cfg, bindings, expected, verified, deadline)


def _retained_factory(cfg, args, static, module, admission_factory):
    """Wire only an independently pinned opt-in factory to a compatible AOS.

    Preflight is read-only: no provider FD, control dispatch or resolution is
    created during startup. Missing native hook support fails before Desktop.
    """
    present = {key for key in ("retained_review_path", "retained_review_sha256") if key in cfg}
    if not present:
        return None
    if len(present) != 2:
        raise ValueError("Retained resolution requires both independent review path and SHA")
    review = pinned_json(cfg["retained_review_path"], cfg["retained_review_sha256"], 65536)
    parameters = inspect.signature(module.main).parameters
    hook = parameters.get("scientist_retained_resolver_factory")
    if hook is None or hook.kind is not inspect.Parameter.KEYWORD_ONLY:
        raise ValueError("Reviewed AOS lacks the native retained resolution factory hook")
    root = Path(args["source_roots"]["scientist"])
    for relative in (
        "scripts/aos_native_retained_factory.py",
        "scripts/aos_native_retained_resolution.py",
        "scripts/aos_native_retained_channel.py",
        "lab/llm/contracts/retained_channel_v1/descriptor.json",
    ):
        candidate = root / relative
        expected_source = static["source_inputs"].get(str(candidate))
        if (
            not expected_source
            or hashlib.sha256(read_regular(candidate, 8 * 1024**2)).hexdigest() != expected_source
        ):
            raise ValueError("Retained factory source lacks its independent launch pin")
    from scripts.aos_native_retained_factory import ConfiguredNativeRetainedFactory

    retained = ConfiguredNativeRetainedFactory(admission_factory, review)
    if retained.verify_configuration() is not None:
        raise ValueError("Retained factory preflight must complete or raise")
    return retained


def _confirm_runtime(factory, profiles):
    """Preserve admission denial while exposing bounded, secret-free failure locations."""
    try:
        return factory.confirm_runtime(profiles)
    except Exception as error:
        # The AOS CLI masks this chain. Do not print exception messages, source
        # text, paths or locals: they may include private configuration values.
        chain = []
        seen = set()
        current = error
        while current is not None and id(current) not in seen and len(chain) < 8:
            seen.add(id(current))
            frames = traceback.extract_tb(current.__traceback__, limit=8)
            chain.append(
                {
                    "type": type(current).__name__[:128],
                    "frames": [
                        {"function": frame.name[:128], "line": frame.lineno} for frame in frames
                    ],
                }
            )
            current = current.__cause__ or (
                None if current.__suppress_context__ else current.__context__
            )
        print(json.dumps({"native_runtime_denied": chain}), file=sys.stderr, flush=True)
        raise


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Native launch startup deadline expired")
    return remaining


def _owned_child_status(process):
    """WNOWAIT retains the direct-child PID and its session/group identifier until cleanup."""
    if process.returncode is not None:
        raise ValueError("Native verification cleanup unverified: child already reaped")
    try:
        return os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError as exc:
        raise ValueError("Native verification cleanup unverified: child ownership lost") from exc


def _proc_identity(pid):
    with Path(f"/proc/{pid}/stat").open("rb") as stream:
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ValueError("Native verification cleanup unverified: process identity oversized")
    fields = raw.rsplit(b") ", 1)[1].split()
    return fields[0], int(fields[2]), int(fields[3]), int(fields[19])


def _group_terminal(process, leader, deadline):
    # An unreaped leader keeps this session identifier unavailable for unrelated processes.
    if _proc_identity(process.pid)[1:] != leader[1:]:
        raise ValueError("Native verification cleanup unverified: original leader changed")
    count = 0
    terminal = True
    for path in Path("/proc").iterdir():
        if not path.name.isdecimal():
            continue
        _remaining(deadline)
        count += 1
        if count > 4096:
            raise ValueError("Native verification cleanup unverified: process scan exceeds bound")
        try:
            state, _group, session, _ticks = _proc_identity(int(path.name))
        except FileNotFoundError:
            continue
        if session == process.pid and state not in (b"Z", b"X"):
            # Includes a descendant that changed its process group inside the owned session.
            terminal = False
    return terminal


def _cleanup_verifier(process, leader):
    deadline = time.monotonic() + VERIFICATION_REAP_SECONDS
    try:
        _owned_child_status(process)
        if leader is None or _proc_identity(process.pid)[1:] != leader[1:]:
            raise ValueError("Original verification leader identity unavailable")
        if leader[1] != process.pid or leader[2] != process.pid:
            raise ValueError("Verification process does not own the original session/group")
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        while not _group_terminal(process, leader, deadline):
            time.sleep(min(0.005, _remaining(deadline)))
        # Never signal a group after this reap; original session members were already terminal.
        process.wait(timeout=_remaining(deadline))
    except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired) as exc:
        raise ValueError(
            "Native verification cleanup unverified: original group not drained/reaped"
        ) from exc
    finally:
        for stream in (process.stdout, process.stderr):
            stream.close()


def _verify_native(command, *, deadline):
    """Bound both pipes before buffering and reap only our verification process group."""
    _remaining(deadline)
    environment = {**os.environ, "CUDA_VISIBLE_DEVICES": ""}
    # The reviewed worker interpreter owns its dependency environment. Caller
    # source roots expose extra distributions to importlib.metadata; the
    # standalone verifier loads its pinned workers by explicit absolute path.
    environment.pop("PYTHONPATH", None)
    process = subprocess.Popen(  # nosec B603
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        close_fds=True,
        env=environment,
    )
    buffers = [bytearray(), bytearray()]
    leader = None
    try:
        leader = _proc_identity(process.pid)
        with selectors.DefaultSelector() as selector:
            for index, stream in enumerate((process.stdout, process.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, index)
            while selector.get_map():
                for key, _events in selector.select(timeout=min(0.1, _remaining(deadline))):
                    room = VERIFICATION_OUTPUT_BYTES - len(buffers[key.data])
                    chunk = os.read(key.fd, min(8192, room + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if len(chunk) > room:
                        raise ValueError("Native verification output exceeds byte bound")
                    buffers[key.data].extend(chunk)
            while (status := _owned_child_status(process)) is None:
                time.sleep(min(0.005, _remaining(deadline)))
            if status.si_code != os.CLD_EXITED or status.si_status != 0:
                raise ValueError("Actual native verification failed")
        _remaining(deadline)
        result = json.loads(buffers[0])
        if not isinstance(result, dict):
            raise ValueError("Actual native verification returned an invalid object")
        return result
    finally:
        _cleanup_verifier(process, leader)


def pinned_json(path, expected, limit=512 * 1024):
    raw = read_regular(Path(path), limit)
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("Independent input pin mismatch")
    return json.loads(raw)


def _artifact_authority_expiry(cfg, bindings, validity_seconds):
    """Cap a new receipt by the pinned original retained authority, when present.

    Full retained configuration/source preflight still runs before Desktop. This
    early check only establishes the finite, same-boot rights backing the TTL.
    """
    present = {key for key in ("retained_review_path", "retained_review_sha256") if key in cfg}
    if not present:
        if validity_seconds > 300:
            raise ValueError("Extended artifact validity requires pinned retained authority")
        return None
    if len(present) != 2:
        raise ValueError("Retained resolution requires both independent review path and SHA")
    review = pinned_json(cfg["retained_review_path"], cfg["retained_review_sha256"], 65536)
    if (
        type(review) is not dict
        or review.get("schema") != "scientist.native-retained-factory-review.v1"
        or review.get("enabled") is not True
        or type(review.get("authority")) is not dict
    ):
        raise ValueError("Artifact validity requires enabled original retained authority")
    authority = review["authority"]
    issued, expires = authority.get("issued_boottime"), authority.get("expires_boottime")
    if (
        not all(
            type(value) in (int, float) and 0 < value <= 2**53 - 1 and math.isfinite(value)
            for value in (issued, expires)
        )
        or not 0 < expires - issued <= 3600
    ):
        raise ValueError("Original retained authority interval is invalid")
    boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    profiles = authority.get("allowed_profiles")
    if (
        authority.get("owner") != "AGENT"
        or authority.get("caller_unit") != cfg["caller_unit"]
        or authority.get("boot_id") != boot
        or not issued <= time.clock_gettime(time.CLOCK_BOOTTIME) < expires
        or type(profiles) is not list
        or not 1 <= len(profiles) <= 3
        or not all(type(profile) is str for profile in profiles)
        or len(set(profiles)) != len(profiles)
        or not set(profiles) <= set(bindings)
        or (validity_seconds > 300 and set(profiles) != set(bindings))
        or any(
            binding["caller_generation"]["unit"] != authority["caller_unit"]
            or binding["server_generation"]["unit"] != authority.get("broker_unit")
            or binding["caller_generation"]["boot_id"] != boot
            or binding["server_generation"]["boot_id"] != boot
            for binding in bindings.values()
        )
    ):
        raise ValueError("Original retained authority is expired or differs from live bindings")
    return expires


def _native_exclusion_contract(cfg):
    """An explicit original maintenance review; never a grant to start or release GPU work."""
    value = cfg.get("native_exclusion")
    if cfg["caller_unit"] != SHARED_CALLER_UNIT:
        if "native_exclusion" in cfg:
            raise ValueError("Legacy caller cannot carry a shared native exclusion review")
        return None
    keys = {
        "schema",
        "request",
        "receipt_sha256",
        "legacy_state_path",
        "candidate_manifest_path",
        "candidate_patch_path",
        "promoted_source_files",
    }
    if (
        type(value) is not dict
        or set(value) != keys
        or value["schema"] != "scientist.native-exclusion-review.v1"
        or type(value["request"]) is not dict
        or type(value["receipt_sha256"]) is not str
        or re.fullmatch(r"[a-f0-9]{64}", value["receipt_sha256"]) is None
        or type(value["promoted_source_files"]) is not dict
        or not 1 <= len(value["promoted_source_files"]) <= 512
    ):
        raise ValueError("Shared caller requires its explicit original native exclusion review")
    paths = [
        value[key]
        for key in ("legacy_state_path", "candidate_manifest_path", "candidate_patch_path")
    ]
    paths.extend(value["promoted_source_files"])
    for name in paths:
        if type(name) is not str:
            raise ValueError("Native exclusion path must be explicit")
        path = Path(name)
        if (
            not path.is_absolute()
            or str(path) != name
            or ".." in path.parts
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
        ):
            raise ValueError("Native exclusion paths must be canonical and absolute")
    if any(
        type(pin) is not str or re.fullmatch(r"[a-f0-9]{64}", pin) is None
        for pin in value["promoted_source_files"].values()
    ):
        raise ValueError("Native exclusion promoted sources require exact hashes")
    return json.loads(canonical(value))


def _native_exclusion_sources(configuration, review, source_inputs, deadline, *, loaded=False):
    """Pin the reader before import and its actual imported dependency origins afterwards."""
    required = {
        "src/aos/native_exclusion.py",
        "src/aos/native_maintenance.py",
        "src/aos/native_handover.py",
        "src/aos/lifecycle.py",
        "src/aos/contracts.py",
        "src/aos/shared_desktop_host.py",
        "src/aos/shared_desktop_plan.py",
        "schemas/native_exclusion_evidence.schema.json",
    }
    sources = configuration["source_files"]
    if not required <= sources["aos"].keys():
        raise ValueError("Native exclusion producer lacks its reviewed source closure")
    selected = {}
    for project, files in sources.items():
        root = Path(configuration["source_roots"][project])
        if not root.is_absolute() or root.resolve(strict=True) != root:
            raise ValueError("Native exclusion source root differs")
        for relative, pin in files.items():
            if (
                type(relative) is not str
                or Path(relative).is_absolute()
                or ".." in Path(relative).parts
                or str(Path(relative)) != relative
            ):
                raise ValueError("Native exclusion source path differs")
            path = root / relative
            if (
                source_inputs.get(str(path)) != pin
                or review["promoted_source_files"].get(str(path)) != pin
                or hashlib.sha256(read_regular(path, 8 * 1024**2)).hexdigest() != pin
            ):
                raise ValueError("Native exclusion producer source is not independently pinned")
            selected[str(path)] = pin
            _remaining(deadline)
    if loaded:
        for name, module in tuple(sys.modules.items()):
            if name == "aos" or name.startswith("aos."):
                path = Path(getattr(module, "__file__", "")).absolute()
                if str(path) not in selected:
                    raise ValueError("Native exclusion imported an unreviewed AOS dependency")


class CurrentRuntimeRights:
    """Actual enabled policy/source revocation and native service generations."""

    def __init__(
        self,
        bindings,
        configuration,
        *,
        native_exclusion=None,
        shared_scope=None,
        source_inputs=None,
        shared_launch_runtime=None,
    ):
        self.bindings = bindings
        self.configuration = configuration
        self.native_exclusion = native_exclusion
        self.shared_scope = shared_scope
        self.source_inputs = source_inputs
        self.shared_launch_runtime = shared_launch_runtime

    def _verify_native_exclusion(self, caller, deadline):
        """Post-spawn observation only; unsupported active-model coexistence stays denied."""
        if caller.unit != SHARED_CALLER_UNIT:
            if self.native_exclusion is not None:
                raise ValueError("Native exclusion cannot authorize a legacy caller")
            return
        review, scope = self.native_exclusion, self.shared_scope
        if review is None or scope is None or self.source_inputs is None:
            raise ValueError("Shared model admission requires native exclusion evidence")
        _native_exclusion_sources(self.configuration, review, self.source_inputs, deadline)
        from aos.contracts import digest as aos_digest
        from aos.lifecycle import process_identity
        from aos.native_exclusion import NativeExclusionEvidence, NativeExclusionReader
        from aos.native_maintenance import NativeMaintenanceRequest
        from aos.shared_desktop_host import SharedServiceBinding

        _native_exclusion_sources(
            self.configuration, review, self.source_inputs, deadline, loaded=True
        )
        request = NativeMaintenanceRequest.model_validate(review["request"], strict=True)
        actual = process_identity(caller.pid)
        if caller.pid != os.getpid() or any(
            getattr(actual, key) != getattr(caller, key)
            for key in ("uid", "pid", "start_ticks", "boot_id")
        ):
            raise ValueError("Native exclusion caller is not this authenticated process")
        expected = SharedServiceBinding(
            unit=caller.unit,
            invocation_id=caller.invocation_id,
            process=actual,
            control_group=caller.control_group,
        )
        before = time.clock_gettime(time.CLOCK_BOOTTIME)
        # Sampling BOOTTIME before MONOTONIC makes this conversion conservative.
        remaining = deadline - time.monotonic()
        end = min(request.expires_boottime, before + remaining)
        if (
            remaining <= 0
            or request.owner_uid != os.getuid()
            or request.boot_id != actual.boot_id
            or not request.issued_boottime <= before < end
            or request.shared_plan_sha256 != scope["plan_sha256"]
        ):
            raise ValueError("Original native exclusion authority is expired or differs")
        reader = NativeExclusionReader(
            request.store,
            request,
            review["receipt_sha256"],
            expected,
            legacy_state_path=Path(review["legacy_state_path"]),
            candidate_manifest_path=Path(review["candidate_manifest_path"]),
            candidate_patch_path=Path(review["candidate_patch_path"]),
            shared_plan_path=Path(scope["plan_path"]),
        )
        document, checksum = reader.read_native_exclusion(
            request.handover_sha256, scope["plan_sha256"], deadline=end
        )
        evidence = NativeExclusionEvidence.model_validate(document, strict=True)
        if (
            aos_digest(document) != aos_digest(evidence.model_dump(mode="json"))
            or aos_digest(document) != checksum
        ):
            raise ValueError("Native exclusion canonical evidence hash differs")
        expected_values = {
            "schema_version": "aos.native-exclusion.v1",
            "profile": "shared-only-runtime-v1",
            "scope": "repository-managed-entrypoints",
            "native_admission_disabled": True,
            "legacy_workers_absent": True,
            "expiry_reopens_native": False,
            "allocation_authority": False,
            "shared_launch_authorized": False,
            "gpu_release_verified": False,
            "request_id": request.request_id,
            "principal": request.principal,
            "owner_uid": request.owner_uid,
            "boot_id": request.boot_id,
            "issued_boottime": request.issued_boottime,
            "expires_boottime": request.expires_boottime,
            "maintenance_request_sha256": aos_digest(request.model_dump(mode="json")),
            "maintenance_receipt_sha256": review["receipt_sha256"],
            "handover_sha256": request.handover_sha256,
            "shared_plan_sha256": scope["plan_sha256"],
            "candidate_manifest_sha256": request.candidate_manifest_sha256,
            "candidate_patch_sha256": request.candidate_patch_sha256,
            "source_files": review["promoted_source_files"],
            "source_sha256": aos_digest(review["promoted_source_files"]),
            "config_files": request.config_files,
            "config_sha256": request.config_sha256,
        }
        after = time.clock_gettime(time.CLOCK_BOOTTIME)
        if (
            any(document[key] != value for key, value in expected_values.items())
            or evidence.shared_caller != expected
            or evidence.legacy.manager_session != request.expected_session
            or evidence.legacy.original_state_sha256 != request.original_state_sha256
            or not before <= evidence.observed_boottime <= after
            or not after < evidence.effective_deadline_boottime <= end
            or process_identity(caller.pid) != actual
        ):
            raise ValueError("Native exclusion evidence differs from the current original scope")
        if time.clock_gettime(time.CLOCK_BOOTTIME) >= evidence.effective_deadline_boottime:
            raise ValueError("Native exclusion expired during final process observation")
        _remaining(deadline)
        return evidence.effective_deadline_boottime

    def __call__(self, selected, requirements):
        from aos.scientist_admission_history import ScientistAdmissionBindingV2
        from aos.scientist_transport import (
            BrokerPeer,
            SystemdBrokerAuthenticator,
            SystemdCallerAuthenticator,
        )

        end = time.monotonic() + 3.5
        previous = control_deadline.get()
        end = min(end, previous) if previous is not None else end
        token = control_deadline.set(end)
        try:
            _remaining(end)
            cfg = self.configuration
            pinned_json(cfg["policy_path"], cfg["policy_file_sha256"], 65536)
            pinned_json(cfg["profile_config_path"], cfg["profile_file_sha256"], 65536)
            profiles = load_profiles(
                Path(cfg["profile_config_path"]), source_root=Path(cfg["source_roots"]["aos"])
            )
            policy = ControlPolicy(
                Path(cfg["policy_path"]),
                profiles=profiles,
                source_root=Path(cfg["source_roots"]["scientist"]),
            )
            if (
                not policy.config
                or policy.config["enabled"] is not True
                or policy.config["history_reconcile"] is not False
            ):
                raise ValueError("Current runtime rights revoked or reconciliation not reviewed")
            if (
                not selected
                or set(selected) != set(requirements)
                or not set(selected) <= set(self.bindings)
            ):
                raise ValueError("Unreviewed runtime selection")
            generations = set()
            exclusion_deadline = None
            for profile, binding in selected.items():
                reviewed = ScientistAdmissionBindingV2.model_validate(
                    self.bindings[profile], strict=True
                )
                if (
                    binding != reviewed
                    or policy.sha256 != binding.policy_sha256
                    or policy.config["caller_unit"] != binding.caller_generation.unit
                ):
                    raise ValueError("Current policy or full reviewed binding differs")
                if policy.config["profile_pins"][profile] != binding.profile_pin.model_dump(
                    mode="json"
                ):
                    raise ValueError("Current profile rights differ")
                if (
                    requirements[profile]["profile"]["manifest_sha256"]
                    != binding.profile_pin.manifest_sha256
                ):
                    raise ValueError("Artifact requirements differ from current profile")
                key = canonical(
                    {
                        "server": binding.server_generation.model_dump(mode="json"),
                        "caller": binding.caller_generation.model_dump(mode="json"),
                    }
                )
                if key in generations:
                    continue
                generations.add(key)
                server = binding.server_generation.model_dump(mode="json")
                peer = BrokerPeer(**{k: v for k, v in server.items() if k != "unit"})
                auth = SystemdBrokerAuthenticator()
                if auth.authenticate(
                    peer.pid, peer.uid, deadline=end
                ) != peer or not auth.still_current(peer, deadline=end):
                    raise ValueError("Original broker generation is no longer current")
                if (
                    SystemdCallerAuthenticator().authenticate(
                        binding.caller_generation, deadline=end
                    )
                    != binding.caller_generation
                ):
                    raise ValueError("Original caller generation is no longer current")
                observed_deadline = self._verify_native_exclusion(binding.caller_generation, end)
                if observed_deadline is not None:
                    exclusion_deadline = (
                        observed_deadline
                        if exclusion_deadline is None
                        else min(exclusion_deadline, observed_deadline)
                    )
                if self.native_exclusion is not None and (
                    not auth.still_current(peer, deadline=end)
                    or SystemdCallerAuthenticator().authenticate(
                        binding.caller_generation, deadline=end
                    )
                    != binding.caller_generation
                ):
                    raise ValueError("Caller or broker changed during native exclusion observation")
            policy.verify()
            policy.verify_policy_hash()
            if any(item.caller_generation.unit == SHARED_CALLER_UNIT for item in selected.values()):
                if self.shared_launch_runtime is None:
                    raise ValueError("Shared model admission requires its entered launch")
                self.shared_launch_runtime.verify(end)
            if exclusion_deadline is not None and (
                time.clock_gettime(time.CLOCK_BOOTTIME) >= exclusion_deadline
            ):
                raise ValueError("Native exclusion expired during final rights observation")
            if time.monotonic() >= end:
                raise ValueError("Current rights observation exceeded its deadline")
        finally:
            control_deadline.reset(token)


def _prepare_launch(path, expected, deadline):
    _remaining(deadline)
    cfg = pinned_json(path, expected)
    if cfg.get("schema") != "scientist.native-launch-review.v1":
        raise ValueError("Missing jointly reviewed native launch input")
    validity_seconds = cfg.get("artifact_validity_seconds", 300)
    if type(validity_seconds) is not int or not 1 <= validity_seconds <= MAX_VALIDITY_SECONDS:
        raise ValueError("Reviewed artifact validity must be an integer from 1 to 900 seconds")
    args = cfg["factory_arguments"]
    aos_unit = cfg["caller_unit"]
    if aos_unit not in {
        "swapp-aos-gpu-joint-acceptance.service",
        "swapp-aos-gpu-shared-desktop-default.service",
    }:
        raise ValueError("Only the two reviewed AOS caller units are supported")
    shared_scope = _shared_scope_contract(cfg)
    native_exclusion = _native_exclusion_contract(cfg)
    # Missing/disabled reviewed policy fails here, before verification or Desktop.
    bindings = capture_reviewed_bindings(
        args["profile_config_path"],
        args["profile_file_sha256"],
        args["policy_path"],
        args["policy_file_sha256"],
        aos_unit=aos_unit,
        lab_unit=None,
    )
    _remaining(deadline)
    for binding in bindings.values():
        if binding["caller_generation"]["pid"] != os.getpid():
            raise ValueError("Startup did not capture this exact service MainPID")
    _artifact_authority_expiry(cfg, bindings, validity_seconds)
    static = pinned_json(cfg["artifact_input_path"], cfg["artifact_input_sha256"])
    if (
        set(static) != {"schema", "requirements", "source_inputs"}
        or static["schema"] != "scientist.native-static-artifact-input.v1"
    ):
        raise ValueError("Independent static artifact requirements are missing")
    # Producer and entrypoint must be part of independently reviewed source inputs.
    for candidate in [
        Path(__file__).absolute(),
        Path(args["source_roots"]["scientist"]) / "scripts/check_aos_model_environment.py",
        Path(args["source_roots"]["scientist"]) / "scripts/aos_native_artifact_receipts.py",
    ]:
        expected_source = static["source_inputs"].get(str(candidate))
        if (
            not expected_source
            or hashlib.sha256(read_regular(candidate, 8 * 1024**2)).hexdigest() != expected_source
        ):
            raise ValueError("Native launch/receipt producer source lacks independent pin")
    shared_runtime = None
    if shared_scope is not None:
        verified = _shared_scope_preflight(cfg, args, static, path, expected, deadline)
        _native_exclusion_sources(args, native_exclusion, static["source_inputs"], deadline)
        shared_runtime = _shared_launch_runtime(
            cfg, args, static, bindings, expected, verified, deadline
        )
        shared_runtime.enter(deadline)

    def native_verify():
        command = [
            cfg["native_verification_python"],
            str(Path(args["source_roots"]["scientist"]) / "scripts/check_aos_model_environment.py"),
            "--receipt",
            cfg["native_review_path"],
            "--expected-receipt-sha256",
            cfg["native_review_sha256"],
        ]
        return _verify_native(command, deadline=deadline)

    receipt = build_receipt(
        bindings,
        static["requirements"],
        static["source_inputs"],
        native_verify,
        validity_seconds=validity_seconds,
    )
    _remaining(deadline)
    authority_expiry = _artifact_authority_expiry(cfg, bindings, validity_seconds)
    if authority_expiry is not None:
        receipt["expires_boottime"] = min(receipt["expires_boottime"], authority_expiry)
    _remaining(deadline)
    body = canonical(receipt)
    if shared_runtime is not None:
        shared_runtime.verify(deadline)
    output = Path(cfg["receipt_output"])
    root = Path(args["source_roots"]["scientist"]) / "data/runtime"
    if not output.is_absolute() or not output.parent.resolve(strict=True).is_relative_to(
        root.resolve(strict=True)
    ):
        raise ValueError("Receipt must be Scientist-owned")
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    _remaining(deadline)
    # The producer digest binds verified output; it does not enable reviewed policy.
    provider = NativeArtifactReceiptProvider(
        output,
        hashlib.sha256(body).hexdigest(),
        verify_current_rights=CurrentRuntimeRights(
            bindings,
            args,
            native_exclusion=native_exclusion,
            shared_scope=shared_scope,
            source_inputs=static["source_inputs"],
            shared_launch_runtime=shared_runtime,
        ),
    )
    factory = ConfiguredScientistAdmissionFactory(
        bindings, **args, verify_artifact_closure=provider
    )
    if shared_scope is not None:

        def verify_shared_scope_before_main():
            _shared_scope_preflight(cfg, args, static, path, expected, deadline)
            shared_runtime.verify(deadline)

        factory.verify_shared_scope_before_main = verify_shared_scope_before_main
    script = Path(args["source_roots"]["aos"]) / "scripts/serve_desktop.py"
    expected_script = args["source_files"]["aos"]["scripts/serve_desktop.py"]
    if hashlib.sha256(read_regular(script, 256 * 1024)).hexdigest() != expected_script:
        raise ValueError("Reviewed AOS entrypoint changed")
    spec = importlib.util.spec_from_file_location("scientist_reviewed_aos_desktop", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    retained = _retained_factory(cfg, args, static, module, factory)
    if retained is not None:
        factory.retained_resolver_factory = retained
    from scripts.aos_joint_lab_hooks import JointLabCapability

    lab = JointLabCapability(cfg["joint_lab_review_path"], cfg["joint_lab_review_sha256"])
    _remaining(deadline)
    return module, factory, lab


def launch(path, expected):
    deadline = time.monotonic() + STARTUP_SECONDS
    previous = control_deadline.get()
    if previous is not None:
        deadline = min(deadline, previous)
    token = control_deadline.set(deadline)
    try:
        module, factory, lab = _prepare_launch(path, expected, deadline)
        _remaining(deadline)
    finally:
        control_deadline.reset(token)
    retained_hooks = {}
    retained = getattr(factory, "retained_resolver_factory", None)
    if retained is not None:
        retained_hooks["scientist_retained_resolver_factory"] = retained
    try:
        if hasattr(factory, "verify_shared_scope_before_main"):
            factory.verify_shared_scope_before_main()
        module.main(
            scientist_admission_factory=factory,
            scientist_bootstrap_expected_peer=factory.expected_peer,
            scientist_confirm_runtime=lambda profiles: _confirm_runtime(factory, profiles),
            scientist_output_contract=factory.output_contract,
            scientist_lab_config=lab.startup,
            scientist_verify_lab_capability=lab,
            **retained_hooks,
        )
    finally:
        if retained is not None:
            # Closing our read-only channel makes no GPU release claim.
            retained.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reviewed-launch-input", required=True)
    parser.add_argument("--expected-launch-input-sha256", required=True)
    options, remainder = parser.parse_known_args()
    sys.argv = [sys.argv[0], *remainder]
    launch(options.reviewed_launch_input, options.expected_launch_input_sha256)
