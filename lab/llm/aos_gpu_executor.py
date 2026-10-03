"""Lab-owned AOS turn execution on the one production fair GPU scheduler.

This file is an isolated deployment draft.  It deliberately imports the
production scheduler instead of carrying a second queue implementation.  The
only runnable model commands are assembled from the fixed profile registry;
the UDS caller can send typed input but never an executable, path, budget or
systemd property.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess  # nosec B404 -- fixed systemd and GPU observer executables only
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, cast
from uuid import uuid4

from lab.llm.aos_gpu_broker import BrokerProtocolError, PeerGeneration, TurnReceipt
from lab.llm.aos_gpu_control_store import (
    AdmissionGrant,
    ControlCanceled,
    ControlStore,
    ControlStoreError,
    control_deadline,
)
from lab.llm.aos_profile_output import (
    NAME as OUTPUT_CONTRACT_NAME,
)
from lab.llm.aos_profile_output import (
    VERSION as OUTPUT_CONTRACT_VERSION,
)
from lab.llm.aos_profile_output import (
    OutputContract,
    ProfileOutputError,
    instantiate_decider_schema,
    instantiate_result_schema,
    load_contract,
    project_profile_output,
    validate_result,
)
from lab.llm.aos_profile_output import (
    canonical as output_canonical,
)
from lab.llm.aos_profile_output import (
    decode as decode_output,
)
from lab.llm.gpu_scheduler import (
    MAX_DRAIN_SECONDS,
    GpuLease,
    LeaseConflict,
    PrincipalReceipt,
    ProcessIdentity,
    SharedGpuScheduler,
    SystemdPrincipalResolver,
    _boot_id,
    _process_identity_alive,
    _read_process_identity,
    boottime,
)
from lab.llm.native_runtime import (
    GPU_SLICE,
    MODEL_TMPFS_BYTES,
    MODEL_UNIT_CPU_PERCENT,
    MODEL_UNIT_MEMORY_BYTES,
    MODEL_UNIT_TASKS,
    ModelRuntimeError,
    NvidiaSmiObserver,
    SystemdUnitManager,
    UnitSnapshot,
)

SYSTEMD_RUN = "/usr/bin/systemd-run"
SYSTEMCTL = "/usr/bin/systemctl"
UNSHARE = "/usr/bin/unshare"
IP = "/usr/bin/ip"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODEL_ROOT = Path("/home/cachyos/aos/models")
MAX_REQUEST_BYTES = 128 * 1024
MAX_RESULT_BYTES = 128 * 1024
MAX_LOG_BYTES = 2 * 1024 * 1024
OUTPUT_CONTRACT_DIRECTORY = Path(__file__).resolve().parent / "contracts/profile_output_v2"
OWNER_PRINCIPAL = "aos"
_active_peer: contextvars.ContextVar[PeerGeneration | None] = contextvars.ContextVar(
    "swapp_aos_gpu_peer", default=None
)


class BrokerExecutionError(RuntimeError):
    """A bounded, fail-closed broker turn failure."""


@dataclass(frozen=True, slots=True)
class TurnBudgets:
    activation_seconds: int = 600
    inference_seconds: int = 60
    total_seconds: int = 660
    queue_seconds: int = 900

    def __post_init__(self) -> None:
        values = (
            self.activation_seconds,
            self.inference_seconds,
            self.total_seconds,
            self.queue_seconds,
        )
        if any(type(value) is not int or value <= 0 for value in values):
            raise ValueError("broker deadlines must be positive bounded integers")
        if self.total_seconds < self.activation_seconds + self.inference_seconds:
            raise ValueError("total deadline must cover startup and inference")
        if self.queue_seconds < self.total_seconds + MAX_DRAIN_SECONDS:
            raise ValueError("queue deadline must cover one turn and its drain")
        if (
            self.activation_seconds > 600
            or self.inference_seconds > 540
            or self.total_seconds > 720
        ):
            raise ValueError("broker profile exceeds the scheduler's hard limits")


@dataclass(frozen=True, slots=True)
class AOSProfile:
    profile_id: str
    kind: str
    manifest: Path
    manifest_sha256: str
    deployment_digest: str
    python: Path
    source_root: Path
    model_paths: tuple[Path, ...]
    budgets: TurnBudgets
    response_schema_sha256: str | None = None
    temperature: float = 0.0
    max_output_tokens: int = 512
    context_tokens: int = 16_384
    output_contract: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.output_contract is not None:
            pin = _validate_output_pin(self.output_contract)
            object.__setattr__(self, "output_contract", MappingProxyType(pin))
        expected_kind = {
            "aos.decider.turn.v1": "decider",
            "aos.bonsai.recovery.v1": "bonsai-recovery",
            "aos.bonsai.vision.v1": "bonsai-vision",
        }.get(self.profile_id)
        if expected_kind is None or self.kind != expected_kind:
            raise ValueError("profile id and fixed AOS model kind do not agree")
        for name, value in (
            ("manifest SHA-256", self.manifest_sha256),
            ("deployment digest", self.deployment_digest),
        ):
            if re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        if not self.manifest.is_absolute() or not self.python.is_absolute():
            raise ValueError("fixed profile paths must be absolute")
        if not self.source_root.is_absolute() or self.source_root.is_symlink():
            raise ValueError("isolated AOS source root must be absolute and non-symlinked")
        if not self.model_paths or any(not item.is_absolute() for item in self.model_paths):
            raise ValueError("fixed model artifact paths must be absolute")
        if (
            self.kind != "decider"
            and re.fullmatch(r"[0-9a-f]{64}", self.response_schema_sha256 or "") is None
        ):
            raise ValueError("Bonsai profile requires its immutable response schema digest")
        if (
            type(self.temperature) not in {int, float}
            or not math.isfinite(float(self.temperature))
            or not 0 <= self.temperature <= 1
            or type(self.max_output_tokens) is not int
            or not 1 <= self.max_output_tokens <= 512
            or type(self.context_tokens) is not int
            or not 256 <= self.context_tokens <= 16_384
        ):
            raise ValueError("Bonsai sampling and context caps must match the bounded profile")

    @property
    def config_sha256(self) -> str:
        value = {
            "profile_id": self.profile_id,
            "kind": self.kind,
            "manifest": str(self.manifest),
            "manifest_sha256": self.manifest_sha256,
            "deployment_digest": self.deployment_digest,
            "python": str(self.python),
            "source_root": str(self.source_root),
            "model_paths": [str(item) for item in self.model_paths],
            "budgets": asdict(self.budgets),
            "response_schema_sha256": self.response_schema_sha256,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "context_tokens": self.context_tokens,
        }
        if self.output_contract is not None:
            value["output_contract"] = dict(self.output_contract)
        return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ChildGeneration:
    unit: str
    invocation_id: str
    main_pid: int
    main_start_ticks: int
    boot_id: str
    control_group: str


class ProfileRegistry:
    """Small immutable server-side map; request fields never select paths."""

    def __init__(self, profiles: Mapping[str, AOSProfile]) -> None:
        if set(profiles) != {
            "aos.decider.turn.v1",
            "aos.bonsai.recovery.v1",
            "aos.bonsai.vision.v1",
        }:
            raise ValueError("registry must contain the three reviewed AOS profiles")
        self._profiles = dict(profiles)

    def get(self, profile_id: str, deployment_digest: str) -> AOSProfile:
        try:
            profile = self._profiles[profile_id]
        except KeyError as exc:
            raise BrokerExecutionError("unknown fixed AOS model profile") from exc
        if profile.deployment_digest != deployment_digest:
            raise BrokerExecutionError("AOS deployment identity differs from the pinned profile")
        _verify_profile_manifest(profile)
        _profile_output_contract(profile)
        return profile


def _validate_output_pin(value: Mapping[str, object]) -> dict[str, object]:
    if (
        not isinstance(value, Mapping)
        or set(value) != {"name", "version", "bundle_sha256"}
        or value["name"] != OUTPUT_CONTRACT_NAME
        or type(value["version"]) is not int
        or value["version"] != OUTPUT_CONTRACT_VERSION
        or not isinstance(value["bundle_sha256"], str)
        or re.fullmatch(r"[a-f0-9]{64}", value["bundle_sha256"]) is None
    ):
        raise ValueError("profile output contract must be the closed pinned v2 contract")
    return dict(value)


def _profile_output_contract(profile: AOSProfile) -> OutputContract | None:
    pin = getattr(profile, "output_contract", None)
    if pin is None:
        return None
    try:
        pin = _validate_output_pin(pin)
        contract = load_contract(OUTPUT_CONTRACT_DIRECTORY, str(pin["bundle_sha256"]))
        if contract.content_pin(profile.profile_id) != profile.response_schema_sha256:
            raise ProfileOutputError("profile content schema differs from output bundle")
        return contract
    except (OSError, ValueError, TypeError) as exc:
        raise BrokerExecutionError(
            "pinned profile output contract is unavailable or changed"
        ) from exc


def _verify_profile_manifest(profile: AOSProfile) -> bytes:
    manifest = profile.manifest
    try:
        info = manifest.lstat()
        if not stat.S_ISREG(info.st_mode) or manifest.is_symlink() or info.st_size > 256 * 1024:
            raise BrokerExecutionError("pinned AOS manifest type or size is invalid")
        if info.st_uid != os.getuid() and info.st_uid != 0:
            raise BrokerExecutionError("pinned AOS manifest owner is not trusted")
        data = manifest.read_bytes()
    except OSError as exc:
        raise BrokerExecutionError("pinned AOS manifest is unavailable") from exc
    if hashlib.sha256(data).hexdigest() != profile.manifest_sha256:
        raise BrokerExecutionError("pinned AOS manifest hash changed")
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError as exc:
        raise BrokerExecutionError("pinned AOS manifest is malformed") from exc
    if not isinstance(parsed, dict):
        raise BrokerExecutionError("pinned AOS manifest must be an object")
    if profile.kind != "decider" and (
        parsed.get("temperature") != profile.temperature
        or parsed.get("max_output_tokens") != profile.max_output_tokens
        or parsed.get("context_tokens") != profile.context_tokens
        or parsed.get("parallel") != 1
    ):
        raise BrokerExecutionError("Bonsai model profile differs from the pinned manifest")
    return data


def _profile_payload(profile: AOSProfile, payload: dict[str, object]) -> bytes:
    """Validate the role-specific public request shape and canonicalize it."""
    if profile.kind == "decider":
        if set(payload) != {"request"} or not isinstance(payload["request"], dict):
            raise BrokerExecutionError("Decider payload must contain one typed request")
        request = payload["request"]
        if set(request) != {"state", "question", "options"}:
            raise BrokerExecutionError("Decider request schema differs")
        options = request["options"]
        if (
            not isinstance(request["state"], str)
            or not request["state"].strip()
            or len(request["state"].encode()) > 16 * 1024
            or not isinstance(request["question"], str)
            or not request["question"].strip()
            or len(request["question"].encode()) > 1024
            or not isinstance(options, list)
            or not 2 <= len(options) <= 10
            or any(
                not isinstance(option, dict)
                or set(option) != {"id", "label"}
                or not isinstance(option["id"], str)
                or not isinstance(option["label"], str)
                for option in options
            )
            or len({item["id"] for item in options}) != len(options)
            or len({item["label"] for item in options}) != len(options)
            or any(not item["id"].strip() or not item["label"].strip() for item in options)
        ):
            raise BrokerExecutionError("Decider request violates its typed input limits")
    else:
        _validate_bonsai_body(profile, payload)
    try:
        data = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise BrokerExecutionError("AOS input is not canonical JSON") from exc
    if not data or len(data) > MAX_REQUEST_BYTES:
        raise BrokerExecutionError("AOS input exceeds the fixed request bound")
    return data


def _validate_bonsai_body(profile: AOSProfile, body: dict[str, object]) -> None:
    kind = profile.kind
    required = {
        "model",
        "temperature",
        "max_tokens",
        "stream",
        "chat_template_kwargs",
        "messages",
        "response_format",
    }
    if set(body) != required or body.get("stream") is not False:
        raise BrokerExecutionError("Bonsai request fields differ from the fixed transport")
    max_tokens = body.get("max_tokens")
    temperature = body.get("temperature")
    messages = body.get("messages")
    response_format = body.get("response_format")
    if (
        body.get("model") != "bonsai-" + profile.deployment_digest
        or not isinstance(max_tokens, int)
        or isinstance(max_tokens, bool)
        or not 1 <= max_tokens <= profile.max_output_tokens
        or not isinstance(temperature, (float, int))
        or isinstance(temperature, bool)
        or float(temperature) != float(profile.temperature)
        or not math.isfinite(float(temperature))
        or body.get("chat_template_kwargs") != {"enable_thinking": False}
        or not isinstance(messages, list)
        or len(messages) != 2
        or not all(isinstance(item, dict) for item in messages)
        or [cast(dict[str, object], item).get("role") for item in messages] != ["system", "user"]
        or not isinstance(response_format, dict)
        or set(response_format) != {"type", "json_schema"}
        or response_format.get("type") != "json_schema"
    ):
        raise BrokerExecutionError("Bonsai fixed request policy differs")
    schema_spec = response_format["json_schema"]
    if (
        not isinstance(schema_spec, dict)
        or set(schema_spec) != {"name", "strict", "schema"}
        or schema_spec.get("strict") is not True
        or schema_spec.get("name")
        != ("aos_recovery_plan" if kind == "bonsai-recovery" else "aos_visual_scene")
        or not isinstance(schema_spec.get("schema"), dict)
    ):
        raise BrokerExecutionError("Bonsai response schema identity differs")
    schema_bytes = _canonical_json(schema_spec["schema"]).encode("utf-8")
    if hashlib.sha256(schema_bytes).hexdigest() != profile.response_schema_sha256:
        raise BrokerExecutionError("Bonsai response schema differs from the pinned AOS profile")
    for item in messages:
        message = cast(dict[str, object], item)
        if set(message) != {"role", "content"}:
            raise BrokerExecutionError("Bonsai messages must contain role and content only")
        if message["role"] not in {"system", "user"}:
            raise BrokerExecutionError("Bonsai message role differs")
        content = message["content"]
        if kind == "bonsai-recovery" and not isinstance(content, str):
            raise BrokerExecutionError("recovery input must be text")
        if kind == "bonsai-vision" and message["role"] == "user":
            if not isinstance(content, list) or len(content) != 2:
                raise BrokerExecutionError("vision input must contain text and one bounded image")
            image = content[1]
            if (
                not isinstance(image, dict)
                or image.get("type") != "image_url"
                or not isinstance(image.get("image_url"), dict)
                or not isinstance(image["image_url"].get("url"), str)
                or not image["image_url"]["url"].startswith("data:image/png;base64,")
                or len(image["image_url"]["url"]) > 80_100
            ):
                raise BrokerExecutionError("vision input image exceeds its typed bound")


class PeerBoundPrincipalResolver:
    """Resolve the AOS lane to the authenticated socket peer, not broker PID."""

    def __init__(
        self,
        authenticator: PeerAuthenticator,
        *,
        lab_units: Mapping[str, str],
    ) -> None:
        self._authenticator = authenticator
        self._lab = SystemdPrincipalResolver(dict(lab_units))

    def resolve(self, owner: str) -> PrincipalReceipt:
        if owner == "lab":
            return self._lab.resolve(owner)
        if owner != OWNER_PRINCIPAL:
            raise LeaseConflict("unknown shared GPU owner")
        peer = _active_peer.get()
        if peer is None or not self._authenticator.still_current(peer):
            raise LeaseConflict("authenticated AOS peer generation is unavailable")
        return PrincipalReceipt(
            owner,
            ProcessIdentity(peer.pid, peer.start_ticks, peer.boot_id),
            peer.unit,
            peer.invocation_id,
        )

    def verify(self, receipt: PrincipalReceipt) -> bool:
        if receipt.owner == "lab":
            return self._lab.verify(receipt)
        peer = _active_peer.get()
        if peer is None:
            # Release/recovery can run after a disconnected socket. Revalidate
            # the persisted AOS process generation without accepting a new one.
            if (
                receipt.owner != OWNER_PRINCIPAL
                or receipt.identity.boot_id != _boot_id()
                or not _process_identity_alive(receipt.identity)
            ):
                return False
            try:
                current = self._authenticator.authenticate(receipt.identity.pid, os.getuid())
            except (OSError, ValueError, BrokerProtocolError):
                return False
            return _peer_matches_receipt(current, receipt)
        return _peer_matches_receipt(peer, receipt) and self._authenticator.still_current(peer)


class SystemdSocketPeerAuthenticator:
    """Authenticate the socket process inside one configured AOS service generation."""

    _UNIT = re.compile(r"swapp-aos-[a-z0-9-]+\.service\Z")

    def __init__(self, aos_unit: str, *, units: SystemdUnitManager | None = None) -> None:
        if self._UNIT.fullmatch(aos_unit) is None:
            raise ValueError("AOS peer unit must be a fixed SWAPP service name")
        self.aos_unit = aos_unit
        self.units = units or SystemdUnitManager()

    def _parent(self) -> tuple[str, str, int, int]:
        limit = control_deadline.get()
        remaining = 5.0 if limit is None else min(5.0, limit - time.monotonic())
        if remaining <= 0:
            raise BrokerProtocolError("control identity deadline expired")
        result = subprocess.run(  # nosec B603 -- fixed systemctl argv and validated service name
            [
                SYSTEMCTL,
                "--user",
                "show",
                "--no-pager",
                "--property=LoadState",
                "--property=ActiveState",
                "--property=MainPID",
                "--property=InvocationID",
                "--property=ControlGroup",
                self.aos_unit,
            ],
            capture_output=True,
            text=True,
            timeout=remaining,
            check=False,
        )
        if result.returncode != 0:
            raise BrokerProtocolError("configured AOS service generation is unavailable")
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        group = values.get("ControlGroup", "")
        invocation = values.get("InvocationID", "")
        parent_pid_text = values.get("MainPID", "")
        if (
            values.get("LoadState") != "loaded"
            or values.get("ActiveState") != "active"
            or not re.fullmatch(r"[0-9a-f]{32}", invocation)
            or not group.startswith("/")
            or ".." in Path(group).parts
            or not parent_pid_text.isdecimal()
            or int(parent_pid_text) <= 1
        ):
            raise BrokerProtocolError("configured AOS service identity is not active and bounded")
        parent_identity = _read_process_identity(int(parent_pid_text))
        if parent_identity is None or parent_identity.boot_id != _boot_id():
            raise BrokerProtocolError("configured AOS main process identity is unavailable")
        return group, invocation, int(parent_pid_text), parent_identity.start_ticks

    def authenticate(self, pid: int, uid: int) -> PeerGeneration:
        if type(pid) is not int or pid <= 1 or uid != os.getuid():
            raise BrokerProtocolError("AOS broker peer uid or pid is not authorized")
        identity = _read_process_identity(pid)
        if identity is None or identity.boot_id != _boot_id():
            raise BrokerProtocolError("AOS broker peer process identity is unavailable")
        group, invocation, parent_pid, parent_start = self._parent()
        if pid not in self.units.cgroup_pids(group):
            raise BrokerProtocolError("AOS broker peer is outside the configured service cgroup")
        return PeerGeneration(
            uid,
            pid,
            identity.start_ticks,
            identity.boot_id,
            self.aos_unit,
            invocation,
            group,
            parent_pid,
            parent_start,
        )

    def still_current(self, peer: PeerGeneration) -> bool:
        if peer.unit != self.aos_unit or peer.uid != os.getuid():
            return False
        try:
            return self.authenticate(peer.pid, peer.uid) == peer
        except (
            BrokerProtocolError,
            ModelRuntimeError,
            OSError,
            ValueError,
            subprocess.SubprocessError,
        ):
            return False


def _peer_matches_receipt(peer: PeerGeneration, receipt: PrincipalReceipt) -> bool:
    return receipt.owner == OWNER_PRINCIPAL and (
        peer.pid,
        peer.start_ticks,
        peer.boot_id,
        peer.uid,
        peer.unit,
        peer.invocation_id,
    ) == (
        receipt.identity.pid,
        receipt.identity.start_ticks,
        receipt.identity.boot_id,
        os.getuid(),
        receipt.unit,
        receipt.invocation_id,
    )


@contextlib.contextmanager
def authenticated_peer(peer: PeerGeneration) -> Iterator[None]:
    token = _active_peer.set(peer)
    try:
        yield
    finally:
        _active_peer.reset(token)


class PeerAuthenticator(Protocol):
    def authenticate(self, pid: int, uid: int) -> PeerGeneration: ...

    def still_current(self, peer: PeerGeneration) -> bool: ...


class OwnedProfileRuntime(Protocol):
    def execute(
        self,
        lease: GpuLease,
        profile: AOSProfile,
        request_id: str,
        payload: dict[str, object],
        scheduler: SharedGpuScheduler,
        deadline: float,
    ) -> TurnReceipt: ...

    def verify_drained(self, lease: GpuLease) -> bool: ...

    def failure_diagnostics(self, lease: GpuLease) -> str: ...

    def after_release(self, lease: GpuLease) -> None: ...


@dataclass(frozen=True, slots=True)
class _ChildIntent:
    unit: str
    nonce: str
    workdir: Path
    input_path: Path
    output_path: Path
    ready_path: Path
    go_path: Path
    log_path: Path


class SystemdAOSProfileRuntime:
    """One-shot pinned AOS worker in a transient, isolated systemd child unit.

    This is the only concrete child launcher in the draft.  The broker builds
    worker argv from `profile.kind`, never from request content.  The child
    signals model readiness using a nonce- and request-bound file, then exits
    after one inference; exact unit/cgroup/GPU drain is verified before the
    shared scheduler can pass the turn to the other principal.
    """

    def __init__(
        self,
        database: Path,
        *,
        units: SystemdUnitManager | None = None,
        gpu: NvidiaSmiObserver | None = None,
        clock: Callable[[], float] = boottime,
        sleep: Callable[[float], None] = time.sleep,
        work_root: Path | None = None,
        memory_bytes: int = MODEL_UNIT_MEMORY_BYTES,
        cpu_percent: int = MODEL_UNIT_CPU_PERCENT,
        task_limit: int = MODEL_UNIT_TASKS,
        control_store: ControlStore | None = None,
    ) -> None:
        self.database = database
        self.control_store = control_store
        self.units = units or SystemdUnitManager(memory_bytes=memory_bytes)
        self.gpu = gpu or NvidiaSmiObserver()
        self.clock = clock
        self.sleep = sleep
        self.work_root = (
            work_root
            or Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
            / "swapp-aos-turns"
        )
        self.memory_bytes = memory_bytes
        self.cpu_percent = cpu_percent
        self.task_limit = task_limit
        if (
            memory_bytes > MODEL_UNIT_MEMORY_BYTES
            or memory_bytes < 1024**3
            or cpu_percent != MODEL_UNIT_CPU_PERCENT
            or task_limit > MODEL_UNIT_TASKS
        ):
            raise ValueError("AOS child limits must fit the measured shared host profile")
        self.work_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.work_root.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or self.work_root.is_symlink()
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise BrokerExecutionError("AOS worker directory must be private and user-owned")
        os.chmod(self.work_root, 0o700)
        with closing(self._connect()) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS aos_gpu_child_bindings (
                    owner TEXT NOT NULL CHECK(owner='aos'),
                    request_id TEXT NOT NULL CHECK(length(request_id)=32),
                    fencing_token INTEGER NOT NULL,
                    profile_id TEXT NOT NULL,
                    deployment_digest TEXT NOT NULL CHECK(length(deployment_digest)=64),
                    request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
                    unit TEXT NOT NULL UNIQUE,
                    nonce TEXT NOT NULL CHECK(length(nonce)=64),
                    workdir TEXT NOT NULL,
                    launch_state TEXT NOT NULL CHECK(
                        launch_state IN ('planned','starting','bound','drained')
                    ),
                    invocation_id TEXT,
                    main_pid INTEGER,
                    main_start_ticks INTEGER,
                    boot_id TEXT,
                    control_group TEXT,
                    observed_gpu_pids_json TEXT NOT NULL DEFAULT '[]',
                    total_seconds INTEGER NOT NULL CHECK(total_seconds BETWEEN 1 AND 720),
                    created_boottime REAL NOT NULL,
                    PRIMARY KEY(owner,request_id,fencing_token)
                )"""
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        os.chmod(self.database, 0o600)
        return connection

    def _binding(self, lease: GpuLease) -> sqlite3.Row | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM aos_gpu_child_bindings WHERE owner=? AND request_id=? "
                "AND fencing_token=?",
                (lease.owner, lease.request_id, lease.fencing_token),
            ).fetchone()
            return cast(sqlite3.Row | None, row)

    def _prepare(
        self,
        lease: GpuLease,
        profile: AOSProfile,
        request_id: str,
        payload: dict[str, object],
    ) -> tuple[sqlite3.Row, _ChildIntent]:
        if lease.owner != "aos" or lease.request_id != request_id:
            raise BrokerExecutionError("AOS child lease identity differs from the request")
        manifest = _verify_profile_manifest(profile)
        manifest_obj = json.loads(manifest)
        _model_paths(profile, manifest_obj)
        request_body = _profile_payload(profile, payload)
        workdir = self.work_root / uuid4().hex
        workdir.mkdir(mode=0o700)
        if workdir.is_symlink() or workdir.stat().st_uid != os.getuid():
            raise BrokerExecutionError("AOS turn work directory identity is unsafe")
        os.chmod(workdir, 0o700)
        intent = _ChildIntent(
            unit=f"swapp-aos-gpu-turn-{uuid4().hex}.service",
            nonce=secrets.token_hex(32),
            workdir=workdir,
            input_path=workdir / "request.json",
            output_path=workdir / "response.jsonl",
            ready_path=workdir / "ready.json",
            go_path=workdir / "go.json",
            log_path=workdir / "worker.log",
        )
        _write_private(intent.input_path, request_body)
        _write_private(intent.output_path, b"")
        _write_private(intent.log_path, b"")
        request_hash = hashlib.sha256(request_body).hexdigest()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if self.control_store is not None:
                    self.control_store.launch_handoff(connection, asdict(lease), stage="plan")
                connection.execute(
                    "INSERT INTO aos_gpu_child_bindings(owner,request_id,fencing_token,profile_id,"
                    "deployment_digest,request_sha256,unit,nonce,workdir,launch_state,"
                    "created_boottime,total_seconds) "
                    "VALUES('aos',?,?,?,?,?,?,?,?, 'planned',?,?)",
                    (
                        request_id,
                        lease.fencing_token,
                        profile.profile_id,
                        profile.deployment_digest,
                        request_hash,
                        intent.unit,
                        intent.nonce,
                        str(workdir),
                        self.clock(),
                        profile.budgets.total_seconds,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                shutil.rmtree(workdir)
                raise LeaseConflict("AOS child launch intent already exists") from exc
            except ControlStoreError:
                connection.rollback()
                shutil.rmtree(workdir)
                raise
            row = connection.execute(
                "SELECT * FROM aos_gpu_child_bindings WHERE owner='aos' AND request_id=? "
                "AND fencing_token=?",
                (request_id, lease.fencing_token),
            ).fetchone()
            connection.commit()
        if row is None:
            raise BrokerExecutionError("AOS child launch intent could not be read back")
        return row, intent

    def _worker_argv(
        self, profile: AOSProfile, manifest: Mapping[str, object], ready_path: Path
    ) -> list[str]:
        if profile.kind == "decider":
            entry = profile.source_root / "services/decider/broker_worker.py"
            return [str(profile.python), str(entry), str(profile.manifest), str(ready_path)]
        entry = profile.source_root / "services/bonsai/broker_worker.py"
        role = "vision" if profile.kind == "bonsai-vision" else "recovery"
        return [str(profile.python), str(entry), str(profile.manifest), str(ready_path), role]

    def _launch(
        self, lease: GpuLease, profile: AOSProfile, row: sqlite3.Row, intent: _ChildIntent
    ) -> None:
        self.units.ensure_slice()
        profile.source_root.resolve(strict=True)
        if profile.source_root.is_symlink():
            raise BrokerExecutionError("isolated AOS source root is a symlink")
        if (
            not profile.python.is_absolute()
            or not profile.python.exists()
            or not os.access(profile.python, os.X_OK)
        ):
            raise BrokerExecutionError("fixed AOS model interpreter is unavailable")
        manifest_obj = json.loads(_verify_profile_manifest(profile))
        artifacts = _model_paths(profile, manifest_obj)
        command = [
            SYSTEMD_RUN,
            "--user",
            "--quiet",
            "--collect",
            "--service-type=exec",
            f"--unit={intent.unit}",
            f"--slice={GPU_SLICE}",
            f"--description=SWAPP AOS GPU turn {intent.nonce}",
            f"--property=MemoryMax={self.memory_bytes}",
            "--property=MemorySwapMax=0",
            f"--property=CPUQuota={self.cpu_percent}%",
            f"--property=TasksMax={self.task_limit}",
            f"--property=RuntimeMaxSec={profile.budgets.total_seconds}",
            "--property=TimeoutStartSec=20",
            "--property=TimeoutStopSec=10",
            "--property=KillMode=control-group",
            "--property=OOMPolicy=stop",
            "--property=NoNewPrivileges=yes",
            f"--property=LimitFSIZE={MAX_LOG_BYTES}",
            f"--property=TemporaryFileSystem=/tmp:rw,size={MODEL_TMPFS_BYTES}",
            f"--property=StandardInput=file:{intent.input_path}",
            f"--property=StandardOutput=append:{intent.output_path}",
            f"--property=StandardError=append:{intent.log_path}",
            f"--property=WorkingDirectory={profile.source_root}",
            f"--property=BindReadOnlyPaths={profile.source_root}",
        ]
        for path in (profile.manifest, *artifacts):
            command.append(f"--property=BindReadOnlyPaths={path}")
        runtime_env = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        # Worker /tmp is a dedicated, size-bounded systemd temporary filesystem.
        env_pairs = {
            "PATH": "/usr/bin:/bin",
            "XDG_RUNTIME_DIR": runtime_env,
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
            "SWAPP_GPU_TURN_NONCE": intent.nonce,
            "SWAPP_GPU_READY_FILE": str(intent.ready_path),
            "SWAPP_GPU_GO_FILE": str(intent.go_path),
            "SWAPP_GPU_REQUEST_ID": lease.request_id,
            "SWAPP_GPU_PROFILE_ID": profile.profile_id,
            "SWAPP_GPU_DEPLOYMENT_DIGEST": profile.deployment_digest,
            "SWAPP_GPU_ACTIVATION_SECONDS": str(profile.budgets.activation_seconds),
            "SWAPP_GPU_INFERENCE_SECONDS": str(profile.budgets.inference_seconds),
            "SWAPP_GPU_HOST_NETNS_INODE": str(os.stat("/proc/self/ns/net").st_ino),
            "TMPDIR": "/tmp",  # nosec B108
            "XDG_CACHE_HOME": "/tmp/aos-model-cache/xdg",  # nosec B108
            "TORCH_HOME": "/tmp/aos-model-cache/torch",  # nosec B108
            "HF_HOME": "/tmp/aos-model-cache/huggingface",  # nosec B108
        }
        for key, value in sorted(env_pairs.items()):
            command.append(f"--setenv={key}={value}")
        command.extend(
            [
                UNSHARE,
                "--user",
                "--map-root-user",
                "--net",
                "--",
                str(PROJECT_ROOT / ".venv/bin/python"),
                "-m",
                "lab.llm.netns_exec",
                "--",
                *self._worker_argv(profile, manifest_obj, intent.ready_path),
            ]
        )
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self.control_store is not None:
                self.control_store.launch_handoff(connection, asdict(lease), stage="start")
            cursor = connection.execute(
                "UPDATE aos_gpu_child_bindings SET launch_state='starting' "
                "WHERE owner='aos' AND request_id=? AND fencing_token=? AND launch_state='planned'",
                (lease.request_id, lease.fencing_token),
            )
            if cursor.rowcount != 1:
                raise LeaseConflict("AOS child intent no longer admits a launch")
            connection.commit()
        remaining = min(
            20.0, lease.activation_deadline - self.clock(), lease.total_deadline - self.clock()
        )
        if remaining <= 0:
            raise BrokerExecutionError("AOS startup budget expired before systemd launch")
        if self.control_store is not None:
            self.control_store.check_running(asdict(lease))
        # The argv contains only fixed executables and the validated pinned profile.
        result = subprocess.run(  # nosec B603
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=remaining,
            check=False,
        )
        if result.returncode != 0:
            raise BrokerExecutionError("fixed AOS child systemd unit did not start")

    def _snapshot(self, row: sqlite3.Row) -> UnitSnapshot:
        snapshot = self.units.inspect(row["unit"])
        if snapshot.load_state == "not-found":
            raise BrokerExecutionError("AOS child unit disappeared before result capture")
        if not self._snapshot_matches(row, snapshot, allow_unbound=True):
            raise BrokerExecutionError("AOS child unit identity or resource limits changed")
        return snapshot

    def _snapshot_matches(
        self, row: sqlite3.Row, snapshot: UnitSnapshot, *, allow_unbound: bool
    ) -> bool:
        expected_cgroup_tail = "/swapp-gpu.slice/" + row["unit"]
        identity_ok = (
            snapshot.load_state == "loaded"
            and snapshot.description == f"SWAPP AOS GPU turn {row['nonce']}"
            and f"SWAPP_GPU_TURN_NONCE={row['nonce']}" in snapshot.environment
            and snapshot.control_group.endswith(expected_cgroup_tail)
            and snapshot.memory_max == self.memory_bytes
            and snapshot.memory_swap_max == 0
            and snapshot.cpu_quota_usec == self.cpu_percent * 10_000
            and snapshot.tasks_max == self.task_limit
            and snapshot.runtime_max_usec == int(row["total_seconds"]) * 1_000_000
        )
        if not identity_ok:
            return False
        if row["invocation_id"] is None and allow_unbound:
            return True
        if (
            snapshot.invocation_id != row["invocation_id"]
            or snapshot.control_group != row["control_group"]
        ):
            return False
        if snapshot.active_state == "inactive" and snapshot.main_pid == 0:
            return self.units.cgroup_empty(snapshot.control_group)
        if snapshot.main_pid != row["main_pid"]:
            return False
        try:
            current = _read_process_identity(snapshot.main_pid)
        except (OSError, ValueError, IndexError):
            return snapshot.active_state == "inactive" and snapshot.main_pid == 0
        return current is None or (
            current.start_ticks == row["main_start_ticks"] and current.boot_id == row["boot_id"]
        )

    def _bind(self, lease: GpuLease, snapshot: UnitSnapshot) -> None:
        process = _read_process_identity(snapshot.main_pid)
        if process is None:
            raise BrokerExecutionError("AOS child main process is unavailable at bind")
        if process.boot_id != _boot_id():
            raise BrokerExecutionError("AOS child boot identity differs")
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "UPDATE aos_gpu_child_bindings SET launch_state='bound',invocation_id=?,main_pid=?,"
                "main_start_ticks=?,boot_id=?,control_group=? WHERE owner='aos' AND request_id=? "
                "AND fencing_token=? AND launch_state IN ('starting','bound') "
                "AND (invocation_id IS NULL OR (invocation_id=? AND main_pid=? "
                "AND control_group=?))",
                (
                    snapshot.invocation_id,
                    snapshot.main_pid,
                    process.start_ticks,
                    process.boot_id,
                    snapshot.control_group,
                    lease.request_id,
                    lease.fencing_token,
                    snapshot.invocation_id,
                    snapshot.main_pid,
                    snapshot.control_group,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseConflict("AOS child generation was already bound differently")

    def execute(
        self,
        lease: GpuLease,
        profile: AOSProfile,
        request_id: str,
        payload: dict[str, object],
        scheduler: SharedGpuScheduler,
        deadline: float,
    ) -> TurnReceipt:
        row, intent = self._prepare(lease, profile, request_id, payload)
        try:
            self._launch(lease, profile, row, intent)
            activation_deadline = min(lease.activation_deadline, deadline)
            current = lease
            bound = False
            ready = False
            while self.clock() < activation_deadline:
                if self.control_store is not None:
                    self.control_store.check_running(asdict(lease))
                fresh = scheduler.heartbeat(current)
                if fresh is None:
                    raise BrokerExecutionError("AOS GPU activation lease expired")
                current = fresh
                snapshot = self._snapshot(row)
                if not bound:
                    self._bind(lease, snapshot)
                    row = self._binding(lease) or row
                    bound = True
                self._check_foreign_gpu(snapshot.control_group)
                if intent.ready_path.exists():
                    ready_value = json.loads(_read_private(intent.ready_path, 1024))
                    if ready_value != {
                        "request_id": lease.request_id,
                        "profile_id": profile.profile_id,
                        "deployment_digest": profile.deployment_digest,
                        "nonce": intent.nonce,
                    }:
                        raise BrokerExecutionError("AOS child readiness receipt differs")
                    activated = scheduler.mark_ready(current)
                    if activated is None:
                        raise BrokerExecutionError("AOS GPU inference phase was not admitted")
                    current = activated
                    go = _canonical_json(
                        {
                            "request_id": lease.request_id,
                            "profile_id": profile.profile_id,
                            "deployment_digest": profile.deployment_digest,
                            "nonce": intent.nonce,
                        }
                    ).encode()
                    # Serialize durable cancel with the bounded go-file handoff.
                    # The write lock never covers startup, model work or drain.
                    with closing(self._connect()) as connection:
                        connection.execute("BEGIN IMMEDIATE")
                        if self.control_store is not None:
                            self.control_store.launch_handoff(connection, asdict(lease), stage="go")
                        _write_private(intent.go_path, go)
                        connection.commit()
                    ready = True
                    break
                if snapshot.active_state not in {"active", "activating"}:
                    raise BrokerExecutionError("AOS model worker exited before readiness")
                self.sleep(min(0.2, max(0.0, activation_deadline - self.clock())))
            if not ready:
                raise BrokerExecutionError("AOS model activation deadline expired")
            self._wait_child_exit(current, row, intent, scheduler, deadline)
            response_bytes = _read_private(intent.output_path, MAX_RESULT_BYTES)
            response = (
                decode_output(response_bytes, limit=MAX_RESULT_BYTES)
                if _profile_output_contract(profile) is not None
                else _strict_object(response_bytes)
            )
            usage = _extract_usage(profile, response)
            row = self._binding(lease) or row
            if not row["invocation_id"]:
                raise BrokerExecutionError("AOS model result has no exact child generation")
            return TurnReceipt(
                response,
                usage,
                row["unit"],
                row["invocation_id"],
                int(row["main_pid"]),
                row["control_group"],
            )
        except BaseException:
            # The caller's scheduler.release invokes verify_drained even if the
            # inference path failed.  Never kill by PID or accept a peer claim.
            raise

    def _wait_child_exit(
        self,
        lease: GpuLease,
        row: sqlite3.Row,
        intent: _ChildIntent,
        scheduler: SharedGpuScheduler,
        deadline: float,
    ) -> None:
        while self.clock() < min(lease.inference_deadline, lease.total_deadline, deadline):
            if self.control_store is not None:
                self.control_store.check_running(asdict(lease))
            fresh = scheduler.heartbeat(lease)
            if fresh is None:
                raise BrokerExecutionError("AOS GPU inference lease expired")
            snapshot = self.units.inspect(row["unit"])
            if snapshot.load_state == "not-found":
                if (
                    row["invocation_id"]
                    and row["control_group"]
                    and self.units.cgroup_empty(row["control_group"])
                    and intent.output_path.stat().st_size > 0
                ):
                    return
                raise BrokerExecutionError("AOS child exited without a bounded result")
            self._check_foreign_gpu(snapshot.control_group)
            if not self._snapshot_matches(row, snapshot, allow_unbound=False):
                raise BrokerExecutionError("AOS child generation changed before result capture")
            if snapshot.active_state == "inactive" and snapshot.main_pid == 0:
                self.units.cgroup_empty(snapshot.control_group)
                if intent.output_path.stat().st_size > 0:
                    return
                raise BrokerExecutionError("AOS child exited without a bounded result")
            if snapshot.active_state not in {"active", "activating"}:
                raise BrokerExecutionError("AOS child unit ended before a result was complete")
            self.sleep(0.2)
        raise BrokerExecutionError("AOS model inference deadline expired")

    def _check_foreign_gpu(self, control_group: str) -> None:
        gpu = self.gpu.snapshot()
        owned = self.units.cgroup_pids(control_group)
        foreign = set(gpu.process_memory_mib) - set(gpu.exempt_display_pids) - set(owned)
        if foreign:
            raise BrokerExecutionError("an unadmitted external GPU process is active")

    def verify_drained(self, lease: GpuLease) -> bool:
        if lease.owner != OWNER_PRINCIPAL:
            # The shared queue can carry Lab-owned tickets.  This AOS adapter
            # cannot prove their process generation absent its own binding.
            return False
        row = self._binding(lease)
        if row is None:
            if self.control_store is not None:
                gpu_now = self.gpu.snapshot()
                if set(gpu_now.process_memory_mib) - set(gpu_now.exempt_display_pids):
                    return False
                self.control_store.record_drain(
                    asdict(lease),
                    {
                        "kind": "no_child_intent",
                        "boot_id": _boot_id(),
                        "no_child_intent": True,
                        "observed_boottime": self.clock(),
                        "child_generation": None,
                        "late_start_fenced": True,
                        "cgroup_empty": True,
                        "gpu_absent": True,
                        "observed_gpu_pids": [],
                        "remaining_owned_gpu_pids": [],
                        "remaining_foreign_gpu_pids": [],
                    },
                )
            return True
        started = self.clock()
        try:
            observed = {int(item) for item in json.loads(row["observed_gpu_pids_json"])}
            snapshot = self.units.inspect(row["unit"])
            expected_group = row["control_group"] or ""
            if (
                self.control_store is not None
                and row["invocation_id"] is None
                and snapshot.load_state == "not-found"
                and self.control_store.requires_bound_child(asdict(lease))
            ):
                # A stopped/delayed launch issuer may still submit this start.
                # Time alone is not physical proof of child nonexistence.
                return False
            # systemd-run or its caller can be delayed after start preparation.
            # An unbound planned/starting intent is not proof that no child can
            # appear later. Retain the lease regardless of elapsed time, also
            # on the legacy runtime path without a control store.
            late_start_fence = (
                float(row["created_boottime"]) + int(row["total_seconds"]) + MAX_DRAIN_SECONDS
            )
            if (
                snapshot.load_state == "not-found"
                and row["launch_state"] in {"planned", "starting"}
                and row["invocation_id"] is None
            ):
                return False
            if snapshot.load_state != "not-found":
                if not self._snapshot_matches(row, snapshot, allow_unbound=True):
                    return False
                if self.control_store is not None and not row["invocation_id"]:
                    # Capture the actual process generation before stop erases
                    # MainPID. An unbound inactive unit is not enough evidence.
                    if snapshot.main_pid <= 1:
                        return False
                    self._bind(lease, snapshot)
                    row = self._binding(lease)
                    if row is None:
                        return False
                if row["invocation_id"] and snapshot.invocation_id != row["invocation_id"]:
                    return False
                expected_group = expected_group or snapshot.control_group
                pids = self.units.cgroup_pids(expected_group)
                gpu_before = self.gpu.snapshot()
                owned_gpu = set(gpu_before.process_memory_mib) & set(pids)
                observed.update(owned_gpu)
                if observed:
                    self._persist_gpu_pids(lease, observed)
                if snapshot.active_state in {"active", "activating", "reloading"} or pids:
                    self.units.stop(row["unit"], timeout_seconds=10)
            deadline = min(self.clock() + MAX_DRAIN_SECONDS, started + MAX_DRAIN_SECONDS)
            while self.clock() < deadline:
                now = self.units.inspect(row["unit"])
                if now.load_state != "not-found" and (
                    now.invocation_id != (row["invocation_id"] or snapshot.invocation_id)
                    or now.control_group != expected_group
                ):
                    return False
                if expected_group and not self.units.cgroup_empty(expected_group):
                    self.sleep(0.1)
                    continue
                gpu_now = self.gpu.snapshot()
                owned_still_live = observed & set(gpu_now.process_memory_mib)
                foreign_still_live = (
                    set(gpu_now.process_memory_mib)
                    - set(gpu_now.exempt_display_pids)
                    - (set(self.units.cgroup_pids(expected_group)) if expected_group else set())
                )
                if owned_still_live or foreign_still_live:
                    self.sleep(0.1)
                    continue
                if now.load_state != "not-found" and (
                    now.active_state != "inactive" or now.main_pid != 0
                ):
                    self.sleep(0.1)
                    continue
                if (
                    self.control_store is not None
                    and row["invocation_id"] is None
                    and self.control_store.requires_bound_child(asdict(lease))
                ):
                    return False
                self._mark_drained(lease)
                if self.control_store is not None:
                    actual = self._binding(lease)
                    if actual is None:
                        return False
                    child = (
                        None
                        if not actual["invocation_id"]
                        else {
                            "unit": actual["unit"],
                            "invocation_id": actual["invocation_id"],
                            "pid": actual["main_pid"],
                            "start_ticks": actual["main_start_ticks"],
                            "boot_id": actual["boot_id"],
                            "control_group": actual["control_group"],
                        }
                    )
                    if child is None and (
                        now.load_state != "not-found" or self.clock() < late_start_fence
                    ):
                        return False
                    self.control_store.record_drain(
                        asdict(lease),
                        {
                            "kind": "physical_drain",
                            "boot_id": _boot_id(),
                            "observed_boottime": self.clock(),
                            "child_generation": child,
                            "never_started": child is None,
                            "child_intent": {
                                "unit": actual["unit"],
                                "nonce": actual["nonce"],
                                "created_boottime": actual["created_boottime"],
                                "total_seconds": actual["total_seconds"],
                            },
                            "late_start_fenced": True,
                            "gpu_absent": True,
                            "cgroup_empty": True,
                            "observed_gpu_pids": sorted(observed),
                            "remaining_owned_gpu_pids": [],
                            "remaining_foreign_gpu_pids": [],
                            "late_start_fence": late_start_fence,
                            "final_unit_state": now.active_state,
                            "final_main_pid": now.main_pid,
                        },
                    )
                return True
            return False
        except (OSError, ValueError, sqlite3.Error, ModelRuntimeError, subprocess.SubprocessError):
            return False

    def _persist_gpu_pids(self, lease: GpuLease, observed: set[int]) -> None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT observed_gpu_pids_json FROM aos_gpu_child_bindings WHERE owner='aos' "
                "AND request_id=? AND fencing_token=?",
                (lease.request_id, lease.fencing_token),
            ).fetchone()
            if row is None:
                raise LeaseConflict("AOS child drain binding disappeared")
            prior = {int(item) for item in json.loads(row["observed_gpu_pids_json"])}
            connection.execute(
                "UPDATE aos_gpu_child_bindings SET observed_gpu_pids_json=? WHERE owner='aos' "
                "AND request_id=? AND fencing_token=?",
                (_canonical_json(sorted(prior | observed)), lease.request_id, lease.fencing_token),
            )

    def _mark_drained(self, lease: GpuLease) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE aos_gpu_child_bindings SET launch_state='drained' WHERE owner='aos' "
                "AND request_id=? AND fencing_token=? "
                "AND launch_state IN ('starting','bound','drained')",
                (lease.request_id, lease.fencing_token),
            )

    def after_release(self, lease: GpuLease) -> None:
        row = self._binding(lease)
        if row is None or row["launch_state"] != "drained":
            raise LeaseConflict("AOS runtime work directory remains attached to an undrained turn")
        workdir = Path(row["workdir"])
        if workdir.parent != self.work_root or workdir.is_symlink():
            raise BrokerExecutionError("refusing to clean an unowned AOS turn directory")
        try:
            info = workdir.lstat()
        except FileNotFoundError:
            return
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise BrokerExecutionError("refusing to clean a non-private AOS turn directory")
        try:
            shutil.rmtree(workdir)
        except FileNotFoundError:
            # Duplicate completion/cleanup may observe the same released turn.
            # Keep its immutable drain/terminal evidence; no work is relaunched.
            try:
                workdir.lstat()
            except FileNotFoundError:
                return
            raise

    def failure_diagnostics(self, lease: GpuLease) -> str:
        row = self._binding(lease)
        if row is None:
            return ""
        workdir = Path(row["workdir"])
        if workdir.parent != self.work_root or workdir.is_symlink():
            return "untrusted worker diagnostic path"
        parts = []
        for name in ("worker.log", "llama-server.log"):
            path = workdir / name
            if not path.exists():
                continue
            try:
                data = _read_private(path, MAX_LOG_BYTES)
            except (OSError, BrokerExecutionError):
                parts.append(f"{name}: unreadable or exceeded limit")
                continue
            text = data.decode("utf-8", errors="replace")
            text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1[redacted]", text)
            text = re.sub(r"(?i)(api[-_ ]?key\s*[:=]\s*)[^\s,;]+", r"\1[redacted]", text)
            text = "".join(char for char in text if char in "\n\r\t" or char.isprintable())
            if text:
                parts.append(f"{name}: {text[-4096:]}")
        return "\n".join(parts)[-8192:]


class BrokerOwnedTurnExecutor:
    """Queue and execute AOS requests using Lab's persisted alternating scheduler."""

    def __init__(
        self,
        database: Path,
        *,
        authenticator: PeerAuthenticator,
        lab_units: Mapping[str, str],
        profiles: ProfileRegistry,
        runtime: OwnedProfileRuntime,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = boottime,
        control_store: ControlStore | None = None,
    ) -> None:
        self.database = database
        self.control_store = control_store
        self._authenticator = authenticator
        self._profiles = profiles
        self._runtime = runtime
        self._sleep = sleep
        self._clock = clock
        self._resolver = PeerBoundPrincipalResolver(authenticator, lab_units=lab_units)
        self._ensure_tables()
        self.scheduler = SharedGpuScheduler(
            database,
            principal_resolver=self._resolver,
            drain_verifier=runtime.verify_drained,
            max_activation_seconds=600,
            # The shared queue requires its largest activation plus largest
            # inference window to fit inside the aggregate turn cap.
            max_inference_seconds=120,
            max_total_seconds=720,
            queue_timeout_seconds=900,
            clock=clock,
            control_store=control_store,
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        os.chmod(self.database, 0o600)
        return connection

    def cleanup_terminal_workdirs(self, *, limit: int = 8) -> int:
        """Retry bounded private cleanup after already committed exact releases."""
        if self.control_store is None:
            return 0
        cleaned = 0
        for saved, receipt_sha256 in self.control_store.pending_cleanup_leases(limit=limit):
            try:
                terms = dict(saved)
                identity = terms.pop("owner_identity")
                lease = GpuLease(owner_identity=ProcessIdentity(**identity), **terms)
                self._runtime.after_release(lease)
                self.control_store.complete_cleanup(lease.request_id, receipt_sha256)
            except (
                ControlStoreError,
                LeaseConflict,
                BrokerExecutionError,
                OSError,
                TypeError,
                ValueError,
            ):
                continue
            cleaned += 1
        return cleaned

    def _ensure_tables(self) -> None:
        parent = self.database.parent
        if parent.is_symlink() or not parent.is_dir():
            raise BrokerExecutionError("shared GPU database parent is unsafe")
        info = parent.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise BrokerExecutionError("shared GPU database parent must be private and owned")
        if self.database.exists() and self.database.is_symlink():
            raise BrokerExecutionError("shared GPU database path is a symlink")
        with closing(self._connect()) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS aos_gpu_output_bindings (
                    request_id TEXT PRIMARY KEY CHECK(length(request_id)=32),
                    request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
                    request_json TEXT NOT NULL,
                    principal_json TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    deployment_digest TEXT NOT NULL,
                    profile_config_sha256 TEXT NOT NULL,
                    bundle_sha256 TEXT NOT NULL,
                    derived_response_schema_sha256 TEXT NOT NULL,
                    derived_result_schema_sha256 TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS aos_gpu_turn_results (
                    request_id TEXT PRIMARY KEY CHECK(length(request_id)=32),
                    request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
                    profile_id TEXT NOT NULL,
                    deployment_digest TEXT NOT NULL CHECK(length(deployment_digest)=64),
                    profile_config_sha256 TEXT NOT NULL CHECK(length(profile_config_sha256)=64),
                    principal_json TEXT,
                    state TEXT NOT NULL CHECK(
                        state IN ('intent','queued','running','result_ready','completed','failed')
                    ),
                    response_json TEXT,
                    usage_json TEXT,
                    generation_json TEXT,
                    failure_code TEXT,
                    failure_detail_json TEXT,
                    created_boottime REAL NOT NULL,
                    updated_boottime REAL NOT NULL,
                    CHECK((state IN ('result_ready','completed') AND response_json IS NOT NULL
                           AND usage_json IS NOT NULL AND generation_json IS NOT NULL)
                       OR (state NOT IN ('result_ready','completed') AND response_json IS NULL
                           AND usage_json IS NULL AND generation_json IS NULL))
                )"""
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(aos_gpu_turn_results)")
            }
            if "failure_detail_json" not in columns:
                connection.execute(
                    "ALTER TABLE aos_gpu_turn_results ADD COLUMN failure_detail_json TEXT"
                )
            if "principal_json" not in columns:
                # Historical rows have no authenticated generation receipt.
                # Leave them unbound; a retry must never adopt that history.
                connection.execute(
                    "ALTER TABLE aos_gpu_turn_results ADD COLUMN principal_json TEXT"
                )

    def _bind_output_request(
        self, request_bytes: bytes, profile: AOSProfile, peer: PeerGeneration
    ) -> None:
        """Persist request-specialized validation after admission, before scheduling."""
        contract = _profile_output_contract(profile)
        if contract is None:
            if self.control_store is not None:
                raise BrokerExecutionError("controlled inference requires profile-output.v2")
            return
        request = decode_output(request_bytes, limit=MAX_REQUEST_BYTES - 1, canonical_required=True)
        result_schema = instantiate_result_schema(request_bytes, contract)
        response_schema = (
            instantiate_decider_schema(request_bytes, contract)
            if profile.kind == "decider"
            else contract.schemas["bonsai-response.schema.json"]
        )
        if (
            request["profile_id"] != profile.profile_id
            or request["deployment_digest"] != profile.deployment_digest
        ):
            raise BrokerExecutionError("request output profile differs")
        fields = {
            "request_id": request["request_id"],
            "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
            "request_json": request_bytes.decode("utf-8"),
            "principal_json": _canonical_json(asdict(peer)),
            "profile_id": profile.profile_id,
            "deployment_digest": profile.deployment_digest,
            "profile_config_sha256": profile.config_sha256,
            "bundle_sha256": contract.bundle_sha256,
            "derived_response_schema_sha256": hashlib.sha256(
                output_canonical(response_schema)
            ).hexdigest(),
            "derived_result_schema_sha256": hashlib.sha256(
                output_canonical(result_schema)
            ).hexdigest(),
        }
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            original = None
            if self.control_store is not None:
                original = connection.execute(
                    "SELECT * FROM aos_control_requests WHERE request_id=?",
                    (request["request_id"],),
                ).fetchone()
                if original is None:
                    raise BrokerExecutionError("output binding has no authorized intent")
                if original["cancel_requested"]:
                    raise ControlCanceled()
                stable = self.control_store._saved_binding(original, require_output_contract=True)
                if any(
                    original[key] != fields[key]
                    for key in (
                        "request_sha256",
                        "principal_json",
                        "profile_id",
                        "deployment_digest",
                        "profile_config_sha256",
                    )
                ) or stable["profile_pin"]["output_contract"] != dict(
                    cast(Mapping[str, object], profile.output_contract)
                ):
                    raise BrokerExecutionError("output binding differs from original admission")
            existing = connection.execute(
                "SELECT * FROM aos_gpu_output_bindings WHERE request_id=?", (request["request_id"],)
            ).fetchone()
            if existing is not None:
                if any(existing[key] != value for key, value in fields.items()):
                    raise LeaseConflict("immutable request output binding changed")
            else:
                if (original is not None and original["state"] != "intent") or connection.execute(
                    "SELECT 1 FROM aos_gpu_turn_results WHERE request_id=?",
                    (request["request_id"],),
                ).fetchone() is not None:
                    raise BrokerExecutionError("unbound legacy output cannot be adopted")
                connection.execute(
                    "INSERT INTO aos_gpu_output_bindings VALUES(?,?,?,?,?,?,?,?,?,?)",
                    tuple(fields.values()),
                )
            connection.commit()

    def _expected_output_generation(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        expected_payload_sha256: str,
    ) -> dict[str, Any]:
        if (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='aos_gpu_child_bindings'"
            ).fetchone()
            is None
        ):
            raise BrokerExecutionError("v2 result lacks an independently bound worker")
        parameters: list[object] = [row["request_id"]]
        query = "SELECT * FROM aos_gpu_child_bindings WHERE owner='aos' AND request_id=?"
        if self.control_store is not None:
            original = connection.execute(
                "SELECT * FROM aos_control_requests WHERE request_id=?", (row["request_id"],)
            ).fetchone()
            if original is None or original["allocation_json"] is None:
                raise BrokerExecutionError("v2 result lacks its original allocation")
            allocation = json.loads(original["allocation_json"])
            if (
                hashlib.sha256(output_canonical(allocation)).hexdigest()
                != original["allocation_sha256"]
            ):
                raise BrokerExecutionError("v2 original allocation digest differs")
            lease = allocation["lease"]
            if lease["owner"] != "aos" or lease["request_id"] != row["request_id"]:
                raise BrokerExecutionError("v2 original allocation target differs")
            query += " AND fencing_token=?"
            parameters.append(lease["fencing_token"])
        matches = connection.execute(query, parameters).fetchall()
        if len(matches) != 1:
            raise BrokerExecutionError("v2 result worker binding is missing or ambiguous")
        child = matches[0]
        if (
            child["launch_state"] not in {"bound", "drained"}
            or child["request_sha256"] != expected_payload_sha256
            or any(
                child[key] != row[key]
                for key in ("profile_id", "deployment_digest")
            )
            or type(child["main_start_ticks"]) is not int
            or child["main_start_ticks"] <= 0
            or not isinstance(child["boot_id"], str)
            or not child["boot_id"]
        ):
            raise BrokerExecutionError("v2 result worker identity is unbound")
        return {
            "unit": child["unit"],
            "invocation_id": child["invocation_id"],
            "main_pid": child["main_pid"],
            "control_group": child["control_group"],
        }

    def _validate_output_receipt(
        self, connection: sqlite3.Connection, row: sqlite3.Row, receipt: TurnReceipt
    ) -> None:
        profile = self._profiles.get(row["profile_id"], row["deployment_digest"])
        contract = _profile_output_contract(profile)
        if contract is None:
            if self.control_store is not None:
                raise BrokerExecutionError("controlled cached output lacks v2 contract")
            return
        saved = connection.execute(
            "SELECT * FROM aos_gpu_output_bindings WHERE request_id=?", (row["request_id"],)
        ).fetchone()
        if (
            saved is None
            or saved["bundle_sha256"] != contract.bundle_sha256
            or saved["profile_config_sha256"] != profile.config_sha256
            or any(
                saved[key] != row[key]
                for key in (
                    "request_sha256",
                    "principal_json",
                    "profile_id",
                    "deployment_digest",
                    "profile_config_sha256",
                )
            )
        ):
            raise BrokerExecutionError("cached output has no exact original schema binding")
        request_bytes = saved["request_json"].encode("utf-8")
        if hashlib.sha256(request_bytes).hexdigest() != row["request_sha256"]:
            raise BrokerExecutionError("original request bytes changed")
        request = decode_output(request_bytes, limit=MAX_REQUEST_BYTES - 1, canonical_required=True)
        if (
            request["request_id"] != row["request_id"]
            or request["profile_id"] != row["profile_id"]
            or request["deployment_digest"] != row["deployment_digest"]
        ):
            raise BrokerExecutionError("persisted request identity differs")
        result_schema = instantiate_result_schema(request_bytes, contract)
        response_schema = (
            instantiate_decider_schema(request_bytes, contract)
            if profile.kind == "decider"
            else contract.schemas["bonsai-response.schema.json"]
        )
        if (
            saved["derived_response_schema_sha256"]
            != hashlib.sha256(output_canonical(response_schema)).hexdigest()
            or saved["derived_result_schema_sha256"]
            != hashlib.sha256(output_canonical(result_schema)).hexdigest()
        ):
            raise BrokerExecutionError("request-derived output schema changed")
        validate_result(
            request_bytes,
            output_canonical(_result_wrapper(receipt)),
            contract,
            context_tokens=profile.context_tokens,
            max_output_tokens=profile.max_output_tokens,
            expected_generation=self._expected_output_generation(
                connection,
                row,
                # The child consumes the validated payload; the durable output
                # binding above authenticates the full original broker frame.
                expected_payload_sha256=hashlib.sha256(
                    _profile_payload(profile, request["payload"])
                ).hexdigest(),
            ),
        )

    def _project_output_receipt(
        self, request_bytes: bytes, profile: AOSProfile, receipt: TurnReceipt
    ) -> TurnReceipt:
        contract = _profile_output_contract(profile)
        if contract is None:
            return receipt
        wrapper = _result_wrapper(receipt)
        result = project_profile_output(
            request_bytes,
            output_canonical(receipt.response),
            wrapper["generation"],
            contract,
            context_tokens=profile.context_tokens,
            max_output_tokens=profile.max_output_tokens,
        )
        if output_canonical(result["usage"]) != output_canonical(receipt.usage):
            raise BrokerExecutionError("runtime usage differs from validated producer usage")
        return TurnReceipt(
            result["response"],
            result["usage"],
            receipt.unit,
            receipt.invocation_id,
            receipt.main_pid,
            receipt.control_group,
        )

    def _intent(
        self,
        request_id: str,
        request_sha256: str,
        profile: AOSProfile,
        peer: PeerGeneration,
    ) -> TurnReceipt | None:
        timestamp = self._clock()
        principal_json = _canonical_json(asdict(peer))
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM aos_gpu_turn_results WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is not None:
                if row["principal_json"] != principal_json:
                    connection.rollback()
                    raise LeaseConflict("AOS request principal generation differs or is unbound")
                if (
                    row["request_sha256"] != request_sha256
                    or row["profile_id"] != profile.profile_id
                    or row["deployment_digest"] != profile.deployment_digest
                    or row["profile_config_sha256"] != profile.config_sha256
                ):
                    connection.rollback()
                    raise LeaseConflict("AOS request id was reused with different immutable input")
                if row["state"] == "completed":
                    result = self._publish_cached_result(connection, row)
                    connection.commit()
                    return result
                if row["state"] == "result_ready":
                    result = self._publish_cached_result(connection, row)
                    if result is not None:
                        connection.commit()
                        return result
                if row["state"] == "intent":
                    # No scheduler side effect is required to adopt this
                    # pre-submit crash window; submit() has exact idempotency.
                    connection.commit()
                    return None
                if row["state"] in {"queued", "running"}:
                    connection.commit()
                    return None
                if row["state"] == "result_ready":
                    connection.commit()
                    raise BrokerExecutionError(
                        "AOS result is waiting for exact-unit drain recovery"
                    )
                connection.commit()
                raise BrokerExecutionError("previous AOS request attempt is durably failed")
            connection.execute(
                "INSERT INTO aos_gpu_turn_results(request_id,request_sha256,profile_id,"
                "deployment_digest,profile_config_sha256,principal_json,state,"
                "created_boottime,updated_boottime) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    request_id,
                    request_sha256,
                    profile.profile_id,
                    profile.deployment_digest,
                    profile.config_sha256,
                    principal_json,
                    "intent",
                    timestamp,
                    timestamp,
                ),
            )
            connection.commit()
        return None

    def _set_state(
        self,
        request_id: str,
        state: str,
        failure_code: str | None = None,
        failure_detail: dict[str, object] | None = None,
    ) -> None:
        if state not in {"queued", "running", "failed"}:
            raise ValueError("invalid durable broker request state")
        with closing(self._connect()) as connection:
            current = connection.execute(
                "SELECT state FROM aos_gpu_turn_results WHERE request_id=?", (request_id,)
            ).fetchone()
            allowed = {
                "queued": {"intent", "queued"},
                "running": {"queued", "running"},
                "failed": {"intent", "queued", "running"},
            }
            if current is None:
                raise LeaseConflict("AOS request intent disappeared")
            if current["state"] == state:
                return
            if current["state"] not in allowed[state]:
                raise LeaseConflict("AOS turn state cannot move backward or overwrite a result")
            cursor = connection.execute(
                "UPDATE aos_gpu_turn_results SET state=?,failure_code=?,failure_detail_json=?,"
                "updated_boottime=? "
                "WHERE request_id=? AND state=?",
                (
                    state,
                    failure_code,
                    None if failure_detail is None else _canonical_json(failure_detail),
                    self._clock(),
                    request_id,
                    current["state"],
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseConflict("AOS request state was changed by another worker")

    def failure_receipt(self, request_id: str) -> dict[str, object] | None:
        """Read the bounded durable error summary without exposing model output."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT state,failure_code,failure_detail_json FROM aos_gpu_turn_results "
                "WHERE request_id=?",
                (request_id,),
            ).fetchone()
        if row is None or row["state"] != "failed":
            return None
        return {
            "state": row["state"],
            "failure_code": row["failure_code"],
            "detail": None
            if row["failure_detail_json"] is None
            else json.loads(row["failure_detail_json"]),
        }

    def _store_result_ready(self, request_id: str, receipt: TurnReceipt) -> None:
        values = (
            _canonical_json(receipt.response),
            _canonical_json(receipt.usage),
            _canonical_json(
                {
                    "unit": receipt.unit,
                    "invocation_id": receipt.invocation_id,
                    "main_pid": receipt.main_pid,
                    "control_group": receipt.control_group,
                }
            ),
        )
        if any(len(value.encode()) > MAX_RESULT_BYTES for value in values):
            raise BrokerExecutionError("bounded AOS model result is too large")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM aos_gpu_turn_results WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None:
                raise BrokerExecutionError("output persistence has no intent")
            self._validate_output_receipt(connection, row, receipt)
            cursor = connection.execute(
                "UPDATE aos_gpu_turn_results SET state='result_ready',response_json=?,usage_json=?,"
                "generation_json=?,failure_code=NULL,updated_boottime=? "
                "WHERE request_id=? AND state='running'",
                (*values, self._clock(), request_id),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise LeaseConflict("AOS result could not be durably bound to its request")
            connection.commit()

    def _publish_cached_result(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> TurnReceipt | None:
        if not connection.in_transaction or row["state"] not in {"result_ready", "completed"}:
            raise BrokerExecutionError("AOS result publication has no transaction or result")
        if self.control_store is not None:
            result = self.control_store.authorize_cached_result(connection, row)
            if result is None:
                if row["state"] == "completed":
                    raise BrokerExecutionError("AOS completed cache has no terminal authority")
                return None
        elif row["state"] == "result_ready":
            ticket = connection.execute(
                "SELECT state FROM gpu_turn_requests WHERE owner='aos' AND request_id=?",
                (row["request_id"],),
            ).fetchone()
            if ticket is None or ticket["state"] != "done":
                return None
        self._validate_output_receipt(connection, row, _receipt_from_row(row))
        if row["state"] == "result_ready":
            cursor = connection.execute(
                "UPDATE aos_gpu_turn_results SET state='completed',updated_boottime=? "
                "WHERE request_id=? AND state='result_ready'",
                (self._clock(), row["request_id"]),
            )
            if cursor.rowcount != 1:
                raise LeaseConflict("AOS completed result receipt changed during drain")
        return _receipt_from_row(row)

    def _complete(self, request_id: str) -> TurnReceipt:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM aos_gpu_turn_results WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise BrokerExecutionError("AOS completion cache disappeared")
            result = self._publish_cached_result(connection, row)
            if result is None:
                raise BrokerExecutionError("AOS result has no completed terminal receipt")
            connection.commit()
            return result

    def _completed_result(self, request_id: str) -> TurnReceipt | None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM aos_gpu_turn_results WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None:
                raise BrokerExecutionError("AOS request state disappeared")
            if row["state"] in {"completed", "result_ready"}:
                result = self._publish_cached_result(connection, row)
                connection.commit()
                return result
            if row["state"] == "failed":
                raise BrokerExecutionError("matching AOS request failed in another handler")
            connection.commit()
            return None

    def run_turn(
        self,
        *,
        peer: PeerGeneration,
        request_id: str,
        request_bytes: bytes,
        request_sha256: str,
        profile_id: str,
        deployment_digest: str,
        payload: dict[str, object],
        deadline: float,
        admission: AdmissionGrant | None = None,
    ) -> TurnReceipt:
        """Idempotently queue, run, drain and retain one fixed-profile turn."""
        if (
            re.fullmatch(r"[0-9a-f]{32}", request_id) is None
            or len(request_bytes) > MAX_REQUEST_BYTES
            or hashlib.sha256(request_bytes).hexdigest() != request_sha256
            or not self._authenticator.still_current(peer)
        ):
            raise BrokerExecutionError("AOS request identity or authenticated peer changed")
        try:
            outer = json.loads(request_bytes)
        except json.JSONDecodeError as exc:
            raise BrokerExecutionError("immutable broker request is malformed") from exc
        if (
            not isinstance(outer, dict)
            or set(outer)
            != {"version", "op", "request_id", "profile_id", "deployment_digest", "payload"}
            or outer["version"] != 1
            or outer["op"] != "infer"
            or outer["request_id"] != request_id
            or outer["profile_id"] != profile_id
            or outer["deployment_digest"] != deployment_digest
            or outer["payload"] != payload
            or _canonical_json(outer).encode("utf-8") != request_bytes
        ):
            raise BrokerExecutionError("broker arguments differ from their hashed wire request")
        profile = self._profiles.get(profile_id, deployment_digest)
        if self.control_store is not None and _profile_output_contract(profile) is None:
            raise BrokerExecutionError("controlled inference requires profile-output.v2")
        # Translate the protocol's monotonic deadline to the scheduler's
        # boottime clock; suspend must consume the same remaining turn budget.
        deadline = self._clock() + max(0.0, deadline - time.monotonic())
        canonical_payload = _profile_payload(profile, payload)
        # The outer request digest is immutable, while independently checking
        # the server-validated model input catches malformed handler plumbing.
        if not canonical_payload:
            raise BrokerExecutionError("empty model input is not allowed")
        if self.control_store is not None:
            response_schema_sha256 = profile.response_schema_sha256
            if response_schema_sha256 is None:
                raise BrokerExecutionError("controlled profile requires a pinned response schema")
            deadline = self.control_store.register_intent(
                peer=asdict(peer),
                request_id=request_id,
                request_sha256=request_sha256,
                profile_id=profile_id,
                deployment_digest=deployment_digest,
                profile_config_sha256=profile.config_sha256,
                response_schema_sha256=response_schema_sha256,
                deadline=deadline,
                budget={
                    **asdict(profile.budgets),
                    "max_output_tokens": profile.max_output_tokens,
                    "context_tokens": profile.context_tokens,
                },
                admission=admission,
            )
        self._bind_output_request(request_bytes, profile, peer)
        replay = self._intent(request_id, request_sha256, profile, peer)
        if replay is not None:
            if not self._authenticator.still_current(peer):
                raise BrokerExecutionError("AOS authenticated peer changed before result replay")
            return replay
        budgets = profile.budgets
        with authenticated_peer(peer):
            ticket = self.scheduler.submit(
                "aos",
                request_id,
                request_bytes,
                activation_seconds=budgets.activation_seconds,
                inference_seconds=budgets.inference_seconds,
                total_seconds=budgets.total_seconds,
                queue_timeout_seconds=budgets.queue_seconds,
            )
            self._set_state(request_id, "queued")
            lease: GpuLease | None = None
            result_ready = False
            drained = False
            try:
                queue_deadline = min(ticket.queue_deadline, deadline)
                while self._clock() < queue_deadline:
                    if not self._authenticator.still_current(peer):
                        self.scheduler.cancel_queued("aos", request_id)
                        raise BrokerExecutionError("AOS service generation changed while queued")
                    lease = self.scheduler.try_acquire("aos", request_id)
                    if lease is not None:
                        self._set_state(request_id, "running")
                        result = self._runtime.execute(
                            lease,
                            profile,
                            request_id,
                            payload,
                            self.scheduler,
                            min(deadline, lease.total_deadline),
                        )
                        if not self._authenticator.still_current(peer):
                            raise BrokerExecutionError(
                                "AOS service generation changed during its turn"
                            )
                        result = self._project_output_receipt(request_bytes, profile, result)
                        self._store_result_ready(request_id, result)
                        if self.control_store is not None:
                            self.control_store.record_result(
                                request_id,
                                result.response,
                                result.usage,
                                {
                                    "unit": result.unit,
                                    "invocation_id": result.invocation_id,
                                    "main_pid": result.main_pid,
                                    "control_group": result.control_group,
                                },
                            )
                        result_ready = True
                        self.scheduler.release(lease)
                        drained = True
                        published = self._complete(request_id)
                        self._runtime.after_release(lease)
                        return published
                    cached = self._completed_result(request_id)
                    if cached is not None:
                        return cached
                    self._sleep(0.2)
                self.scheduler.cancel_queued("aos", request_id)
                raise BrokerExecutionError("AOS request expired in the fair queue")
            except BaseException as exc:
                diagnostic = _failure_detail(exc)
                if lease is None:
                    # No active ticket ever crossed into a child runtime.
                    drained = True
                if lease is not None and not drained:
                    # Release invokes the trusted exact-unit drain callback.
                    # A failed drain leaves the shared queue fenced/quarantined.
                    try:
                        self.scheduler.release(lease)
                        drained = True
                    except (LeaseConflict, BrokerExecutionError, ControlStoreError, OSError):
                        pass
                if drained and not result_ready:
                    diagnostic["worker_stderr"] = (
                        "" if lease is None else self._runtime.failure_diagnostics(lease)
                    )
                    with contextlib.suppress(LeaseConflict):
                        self._set_state(
                            request_id,
                            "failed",
                            type(exc).__name__[:64],
                            diagnostic,
                        )
                if drained and lease is not None:
                    with contextlib.suppress(LeaseConflict, BrokerExecutionError, OSError):
                        self._runtime.after_release(lease)
                raise


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _failure_detail(error: BaseException) -> dict[str, object]:
    message = str(error)
    message = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1[redacted]", message)
    message = re.sub(r"(?i)(api[-_ ]?key\s*[:=]\s*)[^\s,;]+", r"\1[redacted]", message)
    message = "".join(char for char in message if char in "\n\r\t" or char.isprintable())
    return {
        "exception_type": type(error).__name__[:96],
        "message": message[:512],
        "model_response": "not stored",
        "request_body": "not stored",
    }


def _result_wrapper(receipt: TurnReceipt) -> dict[str, Any]:
    return {
        "response": receipt.response,
        "usage": receipt.usage,
        "generation": {
            "unit": receipt.unit,
            "invocation_id": receipt.invocation_id,
            "main_pid": receipt.main_pid,
            "control_group": receipt.control_group,
        },
    }


def _receipt_from_row(row: sqlite3.Row) -> TurnReceipt:
    generation = json.loads(row["generation_json"])
    return TurnReceipt(
        json.loads(row["response_json"]),
        json.loads(row["usage_json"]),
        generation["unit"],
        generation["invocation_id"],
        generation["main_pid"],
        generation["control_group"],
    )


def _model_paths(profile: AOSProfile, manifest: Mapping[str, object]) -> tuple[Path, ...]:
    """Resolve only the model/runtime roots named by the trusted manifest."""
    keys = (
        ("model_path", "code_path") if profile.kind == "decider" else ("model_path", "runtime_path")
    )
    if any(not isinstance(manifest.get(key), str) for key in keys):
        raise BrokerExecutionError("AOS manifest omits fixed artifact roots")
    roots = tuple(Path(str(manifest[key])) for key in keys)
    for root in roots:
        try:
            resolved = root.resolve(strict=True)
        except OSError as exc:
            raise BrokerExecutionError("AOS pinned model root is unavailable") from exc
        if not resolved.is_relative_to(MODEL_ROOT.resolve(strict=True)):
            raise BrokerExecutionError(
                "AOS model artifacts must remain under the pinned model root"
            )
        if root.is_symlink() or not root.is_dir():
            raise BrokerExecutionError("AOS artifact root must be a real directory")
    if profile.model_paths:
        expected = tuple(path.resolve(strict=True) for path in profile.model_paths)
        if expected != tuple(path.resolve(strict=True) for path in roots):
            raise BrokerExecutionError("profile artifact roots differ from the pinned manifest")
    return roots


def _strict_object(data: bytes) -> dict[str, object]:
    if not data or len(data) > MAX_RESULT_BYTES:
        raise BrokerExecutionError("AOS model response is empty or exceeds its bound")
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrokerExecutionError("AOS model response is malformed JSON") from exc
    if not isinstance(value, dict):
        raise BrokerExecutionError("AOS model response must be an object")
    return value


def _extract_usage(profile: AOSProfile, response: Mapping[str, object]) -> dict[str, object]:
    if profile.kind == "decider":
        if response.get("deployment_digest") != profile.deployment_digest:
            raise BrokerExecutionError("Decider response deployment digest differs")
        prediction = response.get("prediction")
        metrics = response.get("metrics")
        if not isinstance(prediction, dict) or not isinstance(metrics, dict):
            raise BrokerExecutionError(
                "Decider response omits prediction or measured runtime metrics"
            )
        selected = prediction.get("selected_option")
        probabilities = prediction.get("probabilities")
        if not isinstance(selected, str) or not isinstance(probabilities, dict):
            raise BrokerExecutionError(
                "Decider prediction does not match the typed decision contract"
            )
        return dict(metrics)
    choices = response.get("choices")
    usage = response.get("usage")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(usage, dict):
        raise BrokerExecutionError("Bonsai response omits its single completion or token usage")
    choice = choices[0]
    if not isinstance(choice, dict) or choice.get("finish_reason") != "stop":
        raise BrokerExecutionError("Bonsai response ended without a complete answer")
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if type(prompt) is not int or prompt < 0 or type(completion) is not int or completion < 0:
        raise BrokerExecutionError("Bonsai response token usage is invalid")
    return {"prompt_tokens": prompt, "completion_tokens": completion}


def _write_private(path: Path, data: bytes) -> None:
    if path.exists() or path.is_symlink() or len(data) > MAX_REQUEST_BYTES:
        raise BrokerExecutionError("refusing to replace an existing or oversized private file")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def _read_private(path: Path, limit: int) -> bytes:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or path.is_symlink()
        or info.st_uid != os.getuid()
        or info.st_size > limit
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise BrokerExecutionError("private AOS turn file identity or size is invalid")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        chunks = bytearray()
        while len(chunks) <= limit:
            piece = os.read(descriptor, min(16 * 1024, limit + 1 - len(chunks)))
            if not piece:
                return bytes(chunks)
            chunks.extend(piece)
        raise BrokerExecutionError("private AOS turn file exceeded its read limit")
    finally:
        os.close(descriptor)
