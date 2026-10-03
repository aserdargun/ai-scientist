"""Reviewed in-unit client for consumed shared launches; no launch or allocation."""

from __future__ import annotations

import math
import os
import socket
import stat
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lab.llm.aos_gpu_control import strict_json
from lab.llm.aos_gpu_control_store import canonical
from lab.llm.shared_launch_authority import _read, broker_generation_current
from lab.llm.shared_launch_ledger import ID, SHA, LaunchBinding, ServiceGeneration
from lab.llm.shared_launch_transport import WIRE_SCHEMA_SHA256, SharedLaunchClient

SHARED_UNIT = "swapp-aos-gpu-shared-desktop-default.service"


class SharedRuntimeReview(BaseModel):
    """Pinned launch input selects original authority; it cannot mint one."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)
    schema_id: Literal["scientist.shared-launch-runtime.v1"] = Field(alias="schema")
    request_id: ID
    binding_path: str = Field(min_length=1, max_length=4096)
    transport_schema_sha256: SHA
    socket_path: str = Field(min_length=1, max_length=107)

    @model_validator(mode="after")
    def original_scope(self):
        for value in (self.socket_path, self.binding_path):
            path = Path(value)
            if (
                not path.is_absolute()
                or str(path) != value
                or ".." in path.parts
                or any(ord(c) < 32 for c in value)
            ):
                raise ValueError("Shared launch runtime requires canonical paths")
        if self.transport_schema_sha256 != WIRE_SCHEMA_SHA256:
            raise ValueError("Shared launch runtime transport version is incompatible")
        return self


class SharedLaunchRuntime:
    """One authenticated exchange per check; no automatic retry after lost ACK."""

    def __init__(self, review, binding, intent_sha256, expected_broker):
        self.review = review
        self.binding = binding
        self.intent_sha256 = intent_sha256
        self.client = SharedLaunchClient(
            self._connect,
            expected_broker=expected_broker,
            broker_current=broker_generation_current,
        )

    def _connect(self, deadline):
        path = Path(self.review.socket_path)
        if path.resolve(strict=True) != path:
            raise ValueError("Shared launch socket path changed")
        parent, before = path.parent.stat(), path.lstat()
        if (
            parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) & 0o077
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) & 0o077
            or not stat.S_ISSOCK(before.st_mode)
        ):
            raise ValueError("Shared launch socket requires its private owner directory")
        remaining = deadline - time.clock_gettime(time.CLOCK_BOOTTIME)
        if remaining <= 0:
            raise ValueError("Shared launch runtime deadline expired")
        channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            channel.settimeout(min(3.0, remaining))
            channel.connect(str(path))
            after = path.lstat()
            parent_after = path.parent.stat()
            if (
                path.resolve(strict=True) != path
                or (
                    parent_after.st_dev,
                    parent_after.st_ino,
                    parent_after.st_uid,
                    parent_after.st_mode,
                )
                != (parent.st_dev, parent.st_ino, parent.st_uid, parent.st_mode)
                or (after.st_dev, after.st_ino, after.st_uid, after.st_mode)
                != (before.st_dev, before.st_ino, before.st_uid, before.st_mode)
            ):
                raise ValueError("Shared launch socket changed during connection")
            return channel
        except BaseException:
            channel.close()
            raise

    def _request(self, operation, deadline):
        now = time.clock_gettime(time.CLOCK_BOOTTIME)
        remaining = deadline - time.monotonic()
        if (
            not math.isfinite(remaining)
            or remaining <= 0
            or not self.binding.issued_boottime <= now < self.binding.expires_boottime
        ):
            raise ValueError("Original shared launch runtime window expired")
        end = min(now + min(remaining, 3.0), self.binding.expires_boottime)
        self.client.request(
            operation,
            self.review.request_id,
            self.binding.sha256(),
            self.intent_sha256,
            deadline=end,
        )
        if time.monotonic() >= deadline or time.clock_gettime(time.CLOCK_BOOTTIME) >= end:
            raise ValueError("Shared launch runtime response arrived after its deadline")

    def enter(self, deadline):
        """Register the broker-observed MainPID before any native work starts."""
        self._request("enter", deadline)

    def verify(self, deadline):
        """Require the same consumed, entered and currently authorized service."""
        self._request("verify_runtime", deadline)


def build_shared_launch_runtime(cfg, bindings, expected_input_sha256, verified, deadline):
    """Read a binding hint; only the independently authenticated broker grants entry.

    The document's hash must not occur in its upstream config closure: it contains
    this launch config's hash. The broker's private policy independently pins the
    original binding. Replacing the hint cannot change that authority.
    """
    if cfg["caller_unit"] != SHARED_UNIT or "shared_launch_runtime" not in cfg:
        raise ValueError("Shared caller requires its reviewed consumed-launch binding")
    review = SharedRuntimeReview.model_validate(cfg["shared_launch_runtime"])
    args, scope = cfg["factory_arguments"], cfg["shared_scope"]
    path = Path(review.binding_path)
    if path.is_relative_to(Path(verified["session_directory"])) or any(
        review.binding_path in paths
        for paths in (args.get("config_files", {}), verified["activation_config_paths"])
    ):
        raise ValueError("Binding hint must be outside its upstream hashed config and session")
    remaining = deadline - time.monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        raise ValueError("Shared launch runtime deadline expired")
    remaining = min(3.0, remaining)
    raw = _read(path, time.clock_gettime(time.CLOCK_BOOTTIME) + remaining, private=True)
    binding = LaunchBinding.model_validate(strict_json(raw))
    if raw != canonical(binding.model_dump(mode="json", by_alias=True)).encode():
        raise ValueError("Shared launch binding document must be canonical")
    expected_brokers = [
        ServiceGeneration.model_validate(value["server_generation"]) for value in bindings.values()
    ]
    if (
        not expected_brokers
        or any(value != expected_brokers[0] for value in expected_brokers)
        or any(
            value["caller_generation"]["unit"] != SHARED_UNIT
            or value["caller_generation"]["pid"] != os.getpid()
            for value in bindings.values()
        )
    ):
        raise ValueError("Shared launch runtime broker or MainPID differs from captured bindings")
    if (
        binding.broker != expected_brokers[0]
        or binding.broker.unit != "swapp-lab-gpu-broker.service"
        or binding.request_id != review.request_id
        or binding.unit != SHARED_UNIT
        or binding.uid != os.getuid()
        or binding.pins.contract_sha256 != review.transport_schema_sha256
        or binding.pins.reviewed_launch_input_sha256 != expected_input_sha256
        or binding.pins.scientist_root != args["source_roots"]["scientist"]
        or binding.pins.aos_root != args["source_roots"]["aos"]
        or binding.plan_sha256 != scope["plan_sha256"]
        or binding.provision_sha256 != scope["provision_sha256"]
        or any(
            getattr(binding, name) != verified[name]
            for name in (
                "workspace",
                "workspace_device",
                "workspace_inode",
                "workspace_uid",
                "app_session",
                "boot_id",
                "issued_boottime",
                "expires_boottime",
            )
        )
    ):
        raise ValueError("Shared launch hint differs from independently reviewed scope")
    if time.monotonic() >= deadline:
        raise ValueError("Shared launch runtime deadline expired")
    return SharedLaunchRuntime(review, binding, verified["intent_sha256"], expected_brokers[0])
