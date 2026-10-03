"""Opt-in proposal2 authority component; no listener, launcher or GPU effects.

Reviewed private policy is the authority source. Request data selects only an
already reviewed request/digest; it cannot select paths, generations or grants.
Post-spawn entry is recorded against an observed MainPID; physical cleanup
remains a separate obligation and entry grants no GPU or model authority.
Native drain/seal/exclusion checks require a reviewed prerequisite verifier;
without one, admission denies. File hashes alone do not prove those facts.
"""

from __future__ import annotations

import hashlib
import math
import os
import sqlite3
import stat

# Catch only errors from the existing bounded systemctl lookup; no execution here.
import subprocess  # nosec B404
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lab.llm.aos_gpu_control import (
    CONTROL_SCHEMA_HASH,
    INFER_SCHEMA_HASH,
    ControlPolicy,
    strict_json,
)
from lab.llm.aos_gpu_control_store import (
    TERMINAL_SCHEMA_HASH,
    canonical,
    control_deadline,
    digest,
    validate_admission_binding,
)
from lab.llm.aos_gpu_executor import SystemdSocketPeerAuthenticator
from lab.llm.gpu_scheduler import _process_cgroup, _read_process_identity, _systemctl_show
from lab.llm.shared_launch_ledger import (
    SHA,
    DurableLaunchIntent,
    EnteredServiceGeneration,
    LaunchBinding,
    LaunchLedgerError,
    Operation,
    ProcessGeneration,
    ServiceGeneration,
    SharedLaunchLedger,
)

CALL_SECONDS = 5.0
BROKER_UNIT = "swapp-lab-gpu-broker.service"
OPS = frozenset({"issue", "verify", "claim", "enter", "verify_runtime", "status", "revoke"})
_LIMIT = 64 * 1024


def _boottime() -> float:
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def _remaining(deadline: float) -> float:
    value = deadline - _boottime()
    if value <= 0:
        raise LaunchLedgerError("deadline_exceeded")
    return value


def process_generation(pid: int) -> ProcessGeneration:
    """Observe real Linux birth/UID twice; unreadable or changing state denies."""

    def observe() -> ProcessGeneration:
        identity = _read_process_identity(pid)
        if identity is None:
            raise LaunchLedgerError("stale_generation")
        with Path(f"/proc/{pid}/status").open("rb") as stream:
            raw = stream.read(_LIMIT + 1)
        if len(raw) > _LIMIT:
            raise LaunchLedgerError("stale_generation")
        uids = [line.split()[1:] for line in raw.splitlines() if line.startswith(b"Uid:")]
        if len(uids) != 1 or len(uids[0]) != 4:
            raise LaunchLedgerError("stale_generation")
        uid = int(uids[0][0])
        if any(int(value) != uid for value in uids[0]):
            raise LaunchLedgerError("stale_generation")
        if Path(f"/proc/{pid}").stat().st_uid != uid:
            raise LaunchLedgerError("stale_generation")
        return ProcessGeneration(uid=uid, **asdict(identity))

    try:
        before = observe()
        if observe() != before:
            raise LaunchLedgerError("stale_generation")
        return before
    except (OSError, ValueError, IndexError) as exc:
        raise LaunchLedgerError("stale_generation") from exc


def broker_generation_current(expected: ServiceGeneration, deadline: float) -> bool:
    """Authenticate a pinned remote broker; liveness is not launch permission."""
    try:
        if expected.uid != os.getuid():
            return False
        process = ProcessGeneration.model_validate(
            {name: getattr(expected, name) for name in ("uid", "pid", "start_ticks", "boot_id")}
        )
        if process_generation(expected.pid) != process:
            return False
        values = _systemctl_show(expected.unit, owner="lab", timeout=min(3.0, _remaining(deadline)))
        if any(
            values.get(key) != value
            for key, value in {
                "LoadState": "loaded",
                "ActiveState": "active",
                "MainPID": str(expected.pid),
                "InvocationID": expected.invocation_id,
                "ControlGroup": expected.control_group,
            }.items()
        ):
            return False
        if _process_cgroup(expected.pid) != expected.control_group:
            return False
        after = _systemctl_show(expected.unit, owner="lab", timeout=min(3.0, _remaining(deadline)))
        return (
            after == values
            and process_generation(expected.pid) == process
            and _process_cgroup(expected.pid) == expected.control_group
            and _remaining(deadline) > 0
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return False


def _pid_namespace(pid: int) -> tuple[int, int]:
    """Observe the namespace object twice; proc namespace symlinks are intentional."""
    try:
        path = Path(f"/proc/{pid}/ns/pid")
        before = path.stat()
        after = path.stat()
        observed = (before.st_dev, before.st_ino)
        if observed != (after.st_dev, after.st_ino) or before.st_ino <= 0:
            raise LaunchLedgerError("pid_namespace_changed")
        return observed
    except OSError as error:
        raise LaunchLedgerError("pid_namespace_unavailable") from error


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


class LaunchPolicyEntry(_Strict):
    """Original reviewed binding and server-selected readback paths/maps."""

    binding: LaunchBinding
    plan_path: str = Field(max_length=4096)
    activation_path: str = Field(max_length=4096)
    activation_raw_sha256: SHA
    reviewed_launch_input_path: str = Field(max_length=4096)
    config_files: Annotated[dict[str, SHA], Field(min_length=1, max_length=128)]
    python_files: Annotated[dict[str, SHA], Field(min_length=1, max_length=8)]
    control_issued_boottime: float = Field(ge=0)
    control_expires_boottime: float = Field(gt=0)

    @model_validator(mode="after")
    def exact_pins(self) -> Self:
        """Crossbind maps and a separate finite original-target control window."""
        if self.binding.broker.unit != BROKER_UNIT:
            raise ValueError("launch authority must use the existing fixed broker service")
        for value in (
            self.plan_path,
            self.activation_path,
            self.reviewed_launch_input_path,
            *self.config_files,
            *self.python_files,
        ):
            _path(value)
        if not 0 < self.control_expires_boottime - self.control_issued_boottime <= 900:
            raise ValueError("original-target control lifetime must be at most 900 seconds")
        pins = self.binding.pins
        if (
            digest(self.config_files) != pins.config_map_sha256
            or digest(self.python_files) != pins.python_identity_sha256
            or self.config_files.get(self.reviewed_launch_input_path)
            != pins.reviewed_launch_input_sha256
        ):
            raise ValueError("reviewed config, Python and launch-input map pins differ")
        return self


class LaunchPolicyDocument(_Strict):
    """Closed private policy; absent/disabled never creates an authority."""

    schema_id: Literal["swapp-shared-launch-policy.v1-proposal2.draft"] = Field(alias="schema")
    enabled: bool = False
    contract_sha256: SHA
    entries: list[LaunchPolicyEntry] = Field(max_length=16)

    @model_validator(mode="after")
    def unique_targets(self) -> Self:
        """Never let one request select more than one reviewed binding."""
        if len({item.binding.request_id for item in self.entries}) != len(self.entries):
            raise ValueError("duplicate reviewed request")
        if any(item.binding.pins.contract_sha256 != self.contract_sha256 for item in self.entries):
            raise ValueError("binding contract differs from reviewed policy")
        return self


def _path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or str(path) != value or ".." in path.parts:
        raise LaunchLedgerError("invalid_reviewed_path")
    if any(ord(character) < 32 for character in value):
        raise LaunchLedgerError("invalid_reviewed_path")
    return path


def _read(path: Path, deadline: float, *, private: bool, limit: int = _LIMIT) -> bytes:
    """Bounded nofollow read; path alias, replacement, hardlink and drift deny."""
    _remaining(deadline)
    if path.resolve(strict=True) != path:
        raise LaunchLedgerError("reviewed_file_changed")
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
        raise LaunchLedgerError("reviewed_file_changed")
    if private and (before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o600):
        raise LaunchLedgerError("private_review_required")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if _metadata(os.fstat(descriptor)) != _metadata(before):
            raise LaunchLedgerError("reviewed_file_changed")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(limit + 1)
        if (
            len(raw) > limit
            or _metadata(os.fstat(descriptor)) != _metadata(before)
            or _metadata(path.lstat()) != _metadata(before)
        ):
            raise LaunchLedgerError("reviewed_file_changed")
        if path.resolve(strict=True) != path:
            raise LaunchLedgerError("reviewed_file_changed")
    finally:
        os.close(descriptor)
    _remaining(deadline)
    return raw


def _metadata(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _pinned_json(path: Path, expected: str, deadline: float) -> dict[str, Any]:
    raw = _read(path, deadline, private=True)
    if hashlib.sha256(raw).hexdigest() != expected:
        raise LaunchLedgerError("reviewed_file_changed")
    return strict_json(raw)


class SharedLaunchPolicy:
    """Require an independently supplied raw review hash; re-read before effects."""

    def __init__(self, path: Path | None, *, expected_raw_sha256: str) -> None:
        self.path = path
        self.expected_raw_sha256 = expected_raw_sha256
        if len(expected_raw_sha256) != 64 or any(
            c not in "0123456789abcdef" for c in expected_raw_sha256
        ):
            raise LaunchLedgerError("invalid_review_hash")

    def verify_current(self, deadline: float) -> LaunchPolicyDocument:
        """Exact canonical private policy bytes; any replacement revokes this review."""
        if self.path is None:
            raise LaunchLedgerError("launch_policy_disabled")
        document = LaunchPolicyDocument.model_validate(
            _pinned_json(_path(str(self.path)), self.expected_raw_sha256, deadline)
        )
        if not document.enabled:
            raise LaunchLedgerError("launch_policy_disabled")
        return document

    def resolve(self, request_id: str, binding_sha256: str, deadline: float) -> LaunchPolicyEntry:
        """Resolve the policy's own binding, never a caller supplied object."""
        for entry in self.verify_current(deadline).entries:
            if entry.binding.request_id == request_id:
                if entry.binding.sha256() != binding_sha256:
                    raise LaunchLedgerError("request_conflict")
                return entry
        raise LaunchLedgerError("unreviewed_request")


@dataclass(frozen=True, slots=True)
class _Request:
    operation: str
    peer_pid: int
    peer_uid: int
    deadline: float
    entry: LaunchPolicyEntry
    entered: EnteredServiceGeneration | None = None
    admission_binding: dict[str, Any] | None = None


_request: ContextVar[_Request | None] = ContextVar("shared_launch_request", default=None)


class SharedLaunchAuthority:
    """Authenticated controller composing the existing canonical ledger only."""

    def __init__(
        self,
        database: Path,
        *,
        policy: SharedLaunchPolicy,
        control_policy: ControlPolicy,
        contract_sha256: str,
        broker_current: Callable[[ServiceGeneration, float], bool] = broker_generation_current,
        prerequisites: Callable[[LaunchBinding, Operation, float], None] | None = None,
        authenticator_factory: Callable[
            [str], SystemdSocketPeerAuthenticator
        ] = SystemdSocketPeerAuthenticator,
        draft_enabled: bool = False,
    ) -> None:
        self.policy = policy
        self.control_policy = control_policy
        self.contract_sha256 = contract_sha256
        self._broker_current = broker_current
        self._prerequisites = prerequisites
        self._authenticator_factory = authenticator_factory
        self.ledger = SharedLaunchLedger(database, draft_enabled=draft_enabled, verifier=self)

    def _observe_service(
        self,
        unit: str,
        pid: int,
        uid: int,
        *,
        require_main: bool,
        caller: dict[str, Any] | None = None,
    ) -> EnteredServiceGeneration:
        try:
            peer = self._authenticator_factory(unit).authenticate(pid, uid)
            if require_main and (peer.pid, peer.start_ticks) != (
                peer.parent_pid,
                peer.parent_start_ticks,
            ):
                raise LaunchLedgerError("service_mainpid_required")
            if caller is not None and asdict(peer) != caller:
                raise LaunchLedgerError("runtime_caller_changed")
            actual = process_generation(pid)
            if (actual.uid, actual.pid, actual.start_ticks, actual.boot_id) != (
                peer.uid,
                peer.pid,
                peer.start_ticks,
                peer.boot_id,
            ):
                raise LaunchLedgerError("stale_generation")
            main = process_generation(peer.parent_pid)
            if (main.uid, main.start_ticks, main.boot_id) != (
                peer.uid,
                peer.parent_start_ticks,
                peer.boot_id,
            ) or _process_cgroup(main.pid) != peer.control_group:
                raise LaunchLedgerError("entered_generation_conflict")
            namespace = _pid_namespace(main.pid)
            if _pid_namespace(pid) != namespace:
                raise LaunchLedgerError("pid_namespace_changed")
            return EnteredServiceGeneration(
                **main.model_dump(mode="json"),
                unit=peer.unit,
                invocation_id=peer.invocation_id,
                control_group=peer.control_group,
                pid_namespace_device=namespace[0],
                pid_namespace_inode=namespace[1],
            )
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            if isinstance(error, LaunchLedgerError):
                raise
            raise LaunchLedgerError("stale_service") from error

    def _authenticate(self, request: _Request) -> None:
        binding = request.entry.binding
        if request.operation in {"enter", "verify_runtime"}:
            entered = self._observe_service(
                binding.unit,
                request.peer_pid,
                request.peer_uid,
                require_main=request.admission_binding is None,
                caller=None
                if request.admission_binding is None
                else request.admission_binding["caller_generation"],
            )
            if entered != request.entered or (entered.uid, entered.boot_id) != (
                binding.uid,
                binding.boot_id,
            ):
                raise LaunchLedgerError("entered_generation_conflict")
            if _pid_namespace(binding.broker.pid) != (
                entered.pid_namespace_device,
                entered.pid_namespace_inode,
            ):
                raise LaunchLedgerError("pid_namespace_changed")
            _remaining(request.deadline)
            return
        expected = binding.manager
        if request.operation in {"issue", "revoke"}:
            expected = binding.issuer
        elif request.operation == "status" and request.peer_pid == binding.issuer.pid:
            expected = binding.issuer
        if (request.peer_pid, request.peer_uid) != (expected.pid, expected.uid):
            raise LaunchLedgerError("unauthorized_principal")
        if process_generation(expected.pid) != expected:
            raise LaunchLedgerError("stale_generation")
        _remaining(request.deadline)

    def _policy(self, request: _Request) -> None:
        entry = self.policy.resolve(
            request.entry.binding.request_id, request.entry.binding.sha256(), request.deadline
        )
        if entry != request.entry or entry.binding.pins.contract_sha256 != self.contract_sha256:
            raise LaunchLedgerError("policy_changed")

    def _files(self, request: _Request) -> None:
        entry = request.entry
        binding = entry.binding
        config = self.control_policy.config
        if config is None or config.get("enabled") is not True:
            raise LaunchLedgerError("control_policy_disabled")
        pins = binding.pins
        if (
            self.control_policy.sha256 != pins.policy_revision_sha256
            or str(self.control_policy.source_root) != pins.scientist_root
            or digest(config["source_files"]["aos"]) != pins.aos_import_closure_sha256
            or digest(config["source_files"]["scientist"]) != pins.scientist_import_closure_sha256
            or digest(config["profile_pins"]) != pins.model_profile_sha256
        ):
            raise LaunchLedgerError("control_binding_changed")
        self.control_policy.verify()
        admission = request.admission_binding
        if admission is not None and (
            admission["policy_sha256"] != pins.policy_revision_sha256
            or admission["source_fingerprints"]
            != {name: digest(files) for name, files in config["source_files"].items()}
            or admission["profile_pin"] != config["profile_pins"].get(admission["profile_id"])
            or any(
                admission[name]["sha256"] != checksum
                for name, checksum in (
                    ("infer_schema", INFER_SCHEMA_HASH),
                    ("control_schema", CONTROL_SCHEMA_HASH),
                    ("terminal_schema", TERMINAL_SCHEMA_HASH),
                )
            )
        ):
            raise LaunchLedgerError("runtime_admission_changed")
        for name, pin in config["profile_pins"].items():
            if (
                str(self.control_policy.profiles.get(name, pin["deployment_digest"]).source_root)
                != pins.aos_root
            ):
                raise LaunchLedgerError("control_binding_changed")
        for paths, private, limit in (
            (entry.config_files, True, _LIMIT),
            (entry.python_files, False, 64 * 1024 * 1024),
        ):
            for path, expected in paths.items():
                if (
                    hashlib.sha256(
                        _read(_path(path), request.deadline, private=private, limit=limit)
                    ).hexdigest()
                    != expected
                ):
                    raise LaunchLedgerError("reviewed_file_changed")
        launcher = Path(pins.scientist_root) / "scripts/aos_native_launch.py"
        if (
            hashlib.sha256(
                _read(launcher, request.deadline, private=False, limit=8 * 1024 * 1024)
            ).hexdigest()
            != pins.launcher_sha256
        ):
            raise LaunchLedgerError("reviewed_file_changed")

    def __call__(
        self,
        connection: sqlite3.Connection,
        binding: LaunchBinding,
        operation: Operation,
        intent: DurableLaunchIntent | None,
    ) -> None:
        """Recheck authority inside the original ledger transaction."""
        request = _request.get()
        if request is None or not connection.in_transaction or request.entry.binding != binding:
            raise LaunchLedgerError("authenticated_context_required")
        self._authenticate(request)
        self._policy(request)
        if not self._broker_current(binding.broker, request.deadline):
            raise LaunchLedgerError("stale_broker")
        if operation in {"status", "revoke"}:
            entry = request.entry
            if not entry.control_issued_boottime <= _boottime() < entry.control_expires_boottime:
                raise LaunchLedgerError("original_control_expired")
        else:
            self._files(request)
            if self._prerequisites is None:
                raise LaunchLedgerError("prerequisite_verifier_required")
            self._prerequisites(binding, operation, request.deadline)
            if operation == "claim":
                if intent is None:
                    raise LaunchLedgerError("intent_required")
                _durable_intent(request, intent)
            elif operation in {"enter", "verify_runtime"}:
                if intent is None:
                    raise LaunchLedgerError("intent_required")
                _durable_intent(request, intent, pristine=False)
            self.control_policy.verify_policy_hash()
        # Slow source/OS/readback work cannot bypass revocation or original clocks.
        if not self._broker_current(binding.broker, request.deadline):
            raise LaunchLedgerError("stale_broker")
        self._policy(request)
        if operation not in {"status", "revoke"}:
            self.control_policy.verify_policy_hash()
        self._authenticate(request)
        if operation in {"status", "revoke"} and not (
            request.entry.control_issued_boottime
            <= _boottime()
            < request.entry.control_expires_boottime
        ):
            raise LaunchLedgerError("original_control_expired")
        _remaining(request.deadline)

    def handle(
        self,
        operation: str,
        request_id: str,
        binding_sha256: str,
        intent_sha256: str | None,
        *,
        peer_pid: int,
        peer_uid: int,
        deadline: float,
    ) -> dict[str, Any]:
        """Transport entry: trusted kernel credentials, original BOOTTIME deadline."""
        if operation not in OPS or type(peer_pid) is not int or type(peer_uid) is not int:
            raise LaunchLedgerError("invalid_request")
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise LaunchLedgerError("invalid_deadline")
        remaining = _remaining(deadline)
        if remaining > CALL_SECONDS:
            raise LaunchLedgerError("invalid_deadline")
        entry = self.policy.resolve(request_id, binding_sha256, deadline)
        selected = time.monotonic() + _remaining(deadline)
        inherited = control_deadline.get()
        legacy_deadline = control_deadline.set(
            selected if inherited is None else min(selected, inherited)
        )
        token = None
        try:
            entered = None
            if operation in {"enter", "verify_runtime"}:
                entered = self._observe_service(
                    entry.binding.unit, peer_pid, peer_uid, require_main=True
                )
            request = _Request(operation, peer_pid, peer_uid, deadline, entry, entered)
            self._authenticate(request)
            token = _request.set(request)
            binding = entry.binding
            if operation == "claim":
                if intent_sha256 is None:
                    raise LaunchLedgerError("intent_required")
                intent = DurableLaunchIntent(
                    request_id=request_id,
                    binding_sha256=binding_sha256,
                    intent_sha256=intent_sha256,
                    marker=str(Path(binding.workspace).parent / "shared-launch-intent.json"),
                )
                result = self.ledger.claim(binding, intent)
                return {"status": asdict(result.status), "consumed_now": result.consumed_now}
            if operation in {"enter", "verify_runtime"}:
                if intent_sha256 is None or entered is None:
                    raise LaunchLedgerError("intent_required")
                if operation == "enter":
                    status = self.ledger.enter(binding, intent_sha256, entered)
                else:
                    with self.ledger._transaction(write=False) as connection:
                        _row, intent = self.ledger._entered_row(connection, binding)
                        if intent.intent_sha256 != intent_sha256:
                            raise LaunchLedgerError("intent_conflict")
                        status = self.ledger.verify_runtime(connection, binding, entered)
                return {"status": asdict(status), "consumed_now": False}
            if intent_sha256 is not None:
                raise LaunchLedgerError("unexpected_intent")
            status = getattr(self.ledger, operation)(binding)
            return {"status": asdict(status), "consumed_now": False}
        finally:
            control_deadline.reset(legacy_deadline)
            if token is not None:
                _request.reset(token)

    def verify_runtime(
        self, connection: sqlite3.Connection, admission_binding: dict[str, Any], *, deadline: float
    ) -> None:
        """Resolve the original entered service inside the caller's existing transaction."""
        if not connection.in_transaction:
            raise LaunchLedgerError("existing_transaction_required")
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise LaunchLedgerError("invalid_deadline")
        remaining = _remaining(deadline)
        if remaining > CALL_SECONDS:
            raise LaunchLedgerError("invalid_deadline")
        admission = validate_admission_binding(admission_binding, require_output_contract=True)
        caller = admission["caller_generation"]
        unit = "swapp-aos-gpu-shared-desktop-default.service"
        if caller["unit"] != unit:
            raise LaunchLedgerError("unauthorized_principal")
        outer = control_deadline.get()
        selected = time.monotonic() + remaining
        budget = control_deadline.set(selected if outer is None else min(selected, outer))
        token = None
        try:
            entered = self._observe_service(
                unit, caller["pid"], caller["uid"], require_main=False, caller=caller
            )
            self.ledger._existing_transaction(connection)
            rows = connection.execute(
                "SELECT original.binding_json,entry.entered_json "
                "FROM aos_shared_launch_draft_v1 original "
                "JOIN aos_shared_launch_entries_draft_v1 entry USING(request_id) "
                "WHERE entry.entered_sha256=? LIMIT 2",
                (digest(entered.model_dump(mode="json")),),
            ).fetchall()
            if len(rows) != 1 or rows[0]["entered_json"] != canonical(
                entered.model_dump(mode="json")
            ):
                raise LaunchLedgerError("entry_required")
            binding = LaunchBinding.model_validate_json(rows[0]["binding_json"], strict=True)
            if admission["server_generation"] != binding.broker.model_dump(mode="json"):
                raise LaunchLedgerError("stale_broker")
            entry = self.policy.resolve(binding.request_id, binding.sha256(), deadline)
            request = _Request(
                "verify_runtime", caller["pid"], caller["uid"], deadline, entry, entered, admission
            )
            token = _request.set(request)
            self.ledger.verify_runtime(connection, binding, entered)
        except sqlite3.Error as error:
            raise LaunchLedgerError("entry_required") from error
        finally:
            if token is not None:
                _request.reset(token)
            control_deadline.reset(budget)


def _directory(path: Path, expected: dict[str, Any]) -> None:
    info = path.lstat()
    if (
        path.resolve(strict=True) != path
        or not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_uid != os.getuid()
        or expected
        != {
            "version": "descriptor-workspace-v1",
            "path_sha256": digest({"workspace": str(path)}),
            "device": info.st_dev,
            "inode": info.st_ino,
            "owner_uid": info.st_uid,
        }
    ):
        raise LaunchLedgerError("workspace_changed")


def _durable_intent(
    request: _Request, intent: DurableLaunchIntent, *, pristine: bool = True
) -> None:
    """Exact canonical AOS v1 documents and pristine original directory scope."""
    binding = request.entry.binding
    workspace = Path(binding.workspace)
    session = workspace.parent
    provision_path = session / "shared-provision.json"
    provision = _pinned_json(provision_path, binding.provision_sha256, request.deadline)
    flags = {
        "preparation_only": True,
        "execution_authorized": False,
        "runtime_started": False,
        "runtime_authority": False,
    }
    keys = {
        "version",
        *flags,
        "plan_sha256",
        "template_sha256",
        "app_session",
        "boot_id",
        "manager_base",
        "session_directory",
        "workspace",
        "database",
        "directory_mode",
        "manager_identity",
        "session_identity",
        "workspace_identity",
        "database_absent",
        "token_absent",
        "socket_absent",
        "lifecycle_absent",
        "workspace_empty",
    }
    if set(provision) != keys or any(provision[name] is not value for name, value in flags.items()):
        raise LaunchLedgerError("invalid_provision")
    if any(
        provision[name] is not True
        for name in (
            "database_absent",
            "token_absent",
            "socket_absent",
            "lifecycle_absent",
            "workspace_empty",
        )
    ):
        raise LaunchLedgerError("invalid_provision")
    expected = {
        "version": "1",
        "plan_sha256": binding.plan_sha256,
        "app_session": binding.app_session,
        "boot_id": binding.boot_id,
        "session_directory": str(session),
        "workspace": str(workspace),
        "database": str(session / "trajectory.sqlite3"),
        "directory_mode": "0700",
        "manager_base": str(session.parent),
    }
    if any(provision[name] != value for name, value in expected.items()):
        raise LaunchLedgerError("invalid_provision")
    _directory(session.parent, provision["manager_identity"])
    _directory(session, provision["session_identity"])
    _directory(workspace, provision["workspace_identity"])
    observed_workspace = provision["workspace_identity"]
    if (
        observed_workspace["device"],
        observed_workspace["inode"],
        observed_workspace["owner_uid"],
    ) != (
        binding.workspace_device,
        binding.workspace_inode,
        binding.workspace_uid,
    ):
        raise LaunchLedgerError("workspace_changed")
    plan = _pinned_json(_path(request.entry.plan_path), binding.plan_sha256, request.deadline)
    if (
        any(
            plan.get(name) != value
            for name, value in {
                "version": "1",
                "app_session": binding.app_session,
                "workspace": str(workspace),
                "session_directory": str(session),
                "database": str(session / "trajectory.sqlite3"),
                "predecessor_identity_sha256": binding.predecessor_identity_sha256,
                "template_sha256": provision["template_sha256"],
            }.items()
        )
        or digest(plan.get("template")) != provision["template_sha256"]
    ):
        raise LaunchLedgerError("plan_changed")
    activation = _pinned_json(
        _path(request.entry.activation_path), request.entry.activation_raw_sha256, request.deadline
    )
    if any(
        activation.get(name) != value
        for name, value in {
            "version": "1",
            "plan_sha256": binding.plan_sha256,
            "boot_id": binding.boot_id,
            "clock": "CLOCK_BOOTTIME",
            "issued_monotonic": binding.issued_boottime,
            "expires_monotonic": binding.expires_boottime,
            "reviewed_launch_input_path": request.entry.reviewed_launch_input_path,
            "reviewed_launch_input_sha256": binding.pins.reviewed_launch_input_sha256,
        }.items()
    ):
        raise LaunchLedgerError("activation_changed")
    expected_intent = {
        "version": "1",
        **flags,
        "plan_sha256": binding.plan_sha256,
        "app_session": binding.app_session,
        "boot_id": binding.boot_id,
        "provision_sha256": binding.provision_sha256,
        "activation_sha256": request.entry.activation_raw_sha256,
    }
    observed = _pinned_json(_path(intent.marker), intent.intent_sha256, request.deadline)
    if (
        observed != expected_intent
        or digest(observed) != intent.intent_sha256
        or any(observed[name] is not value for name, value in flags.items())
    ):
        raise LaunchLedgerError("intent_conflict")
    if pristine and (
        os.listdir(workspace)
        or set(os.listdir(session))
        != {
            "workspace",
            "shared-provision.json",
            "shared-launch-intent.json",
        }
    ):
        raise LaunchLedgerError("scope_not_pristine")
    _directory(session, provision["session_identity"])
    _directory(workspace, provision["workspace_identity"])
