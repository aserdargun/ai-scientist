"""Pinned host review + actual authenticated API oracle; no GPU authority."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import time
from pathlib import Path
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from lab.api.aos_capability import LabCapability
from lab.llm.aos_gpu_control_store import control_deadline
from lab.llm.gpu_scheduler import _process_cgroup, _read_process_identity
from scripts.aos_joint_api_authority import verify_joint_api_authority
from scripts.check_aos_model_environment import read_regular


class ApiGeneration(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    pid: int = Field(gt=1)
    start_ticks: int = Field(gt=0)
    boot_id: str
    unit: str = Field(pattern=r"^lab-[a-z0-9-]+\.service$|^swapp-ai-scientist-[a-z0-9-]+\.service$")
    control_group: str
    invocation_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class JointLabReview(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: str = Field(alias="schema", pattern=r"^scientist.joint-lab-review.v1$")
    enabled: bool
    startup: dict
    api_generation: ApiGeneration
    expected_capabilities: dict[str, LabCapability] = Field(min_length=1, max_length=20)
    token_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_inputs: dict[str, str] = Field(min_length=1, max_length=256)
    source_roots: dict[str, str]
    max_experiments: int = Field(ge=1, le=35)
    max_wall_seconds: int = Field(ge=1, le=14400)
    max_model_tokens: int = Field(ge=1, le=350000)


class JointLabCapability:
    def __init__(self, review_path, expected_review_sha256):
        from aos.scientist_lab_service import ScientistLabStartup, prepare_scientist_lab_startup

        self.path = Path(review_path)
        self.expected_hash = expected_review_sha256
        self.review = self._review()
        if set(self.review.source_roots) != {"scientist", "aos"}:
            raise ValueError("Exact Scientist/AOS review roots required")
        for root in self.review.source_roots.values():
            candidate = Path(root)
            if not candidate.is_absolute() or candidate.resolve(strict=True) != candidate:
                raise ValueError("Canonical independently reviewed source roots required")
        required = {
            "scientist": {
                "lab/api/app.py",
                "lab/api/registry.py",
                "lab/api/aos_capability.py",
                "scripts/aos_joint_lab_hooks.py",
                "scripts/aos_joint_api_authority.py",
                "scripts/aos_native_launch.py",
            },
            "aos": {
                "src/aos/scientist_lab.py",
                "src/aos/scientist_lab_service.py",
                "src/aos/scientist_lab_journal.py",
                "src/aos/scientist_lab_readbacks.py",
                "src/aos/scientist_protocol.py",
            },
        }
        expected_files = {
            str(Path(self.review.source_roots[name]) / relative)
            for name, paths in required.items()
            for relative in paths
        }
        if not expected_files <= set(self.review.source_inputs):
            raise ValueError("Joint Lab authority/provider/oracle source pins are incomplete")
        self.startup = ScientistLabStartup.model_validate_json(
            json.dumps(self.review.startup), strict=True
        )
        if set(self.review.expected_capabilities) != set(self.startup.allowed_suites):
            raise ValueError("Reviewed capability allowlist differs from original startup DTO")
        for suite, capability in self.review.expected_capabilities.items():
            if (
                suite != capability.suite_id
                or capability.owner_id != self.startup.principal_id
                or capability.program_version != self.startup.program_version
            ):
                raise ValueError("Reviewed suite/principal/program capability differs")
        deadline = time.monotonic() + 3
        inherited = control_deadline.get()
        if inherited is not None:
            deadline = min(deadline, inherited)
        if time.monotonic() >= deadline:
            raise TimeoutError("Joint Lab startup observation deadline expired")
        deadline = verify_joint_api_authority(self.review, self.startup, deadline)
        _, self.client = prepare_scientist_lab_startup(self.startup)
        self._current(deadline)

    def _review(self):
        raw = read_regular(self.path, 512 * 1024)
        if hashlib.sha256(raw).hexdigest() != self.expected_hash:
            raise ValueError("Joint Lab review was changed/revoked")
        value = JointLabReview.model_validate_json(raw, strict=True)
        if not value.enabled:
            raise ValueError("Joint Lab capability is not reviewed enabled")
        return value

    def _current(self, deadline):
        review = self._review()
        if review != self.review:
            raise ValueError("Joint Lab current review differs")
        deadline = verify_joint_api_authority(review, self.startup, deadline)
        generation = review.api_generation
        identity = _read_process_identity(generation.pid)
        if Path(f"/proc/{generation.pid}").stat().st_uid != os.getuid():
            raise ValueError("Reviewed API process is not same-user owned")
        if (
            identity is None
            or identity.start_ticks != generation.start_ticks
            or identity.boot_id != generation.boot_id
            or _process_cgroup(generation.pid) != generation.control_group
            or not generation.control_group.endswith("/" + generation.unit)
        ):
            raise ValueError("Original API generation is no longer current")
        # Actual API process environment is read privately and never emitted.
        with Path(f"/proc/{generation.pid}/environ").open("rb") as stream:
            environ = stream.read(65537)
        if len(environ) > 65536:
            raise ValueError("Original API environment exceeds observation bound")
        invocation = b"INVOCATION_ID=" + generation.invocation_id.encode()
        if invocation not in environ.split(b"\0"):
            raise ValueError("Original API invocation differs")
        if not hmac.compare_digest(
            hashlib.sha256(self.client._token().encode()).hexdigest(), review.token_sha256
        ):
            raise ValueError("Reviewed API token changed")
        total = 0
        for name, expected in review.source_inputs.items():
            if time.monotonic() >= deadline:
                raise TimeoutError("Joint Lab source observation deadline expired")
            raw = read_regular(Path(name), 8 * 1024**2)
            total += len(raw)
            if total > 32 * 1024**2 or hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError("Reviewed Lab source/config identity changed")
        if time.monotonic() >= deadline:
            raise TimeoutError("Joint Lab capability observation deadline expired")
        return verify_joint_api_authority(review, self.startup, deadline)

    def __call__(self, task, action):
        from aos.scientist_lab import ScientistLabAction, ScientistLabPolicy, ScientistLabTask
        from aos.scientist_protocol import ScientistRunStatus

        if not isinstance(task, ScientistLabTask) or not isinstance(action, ScientistLabAction):
            raise ValueError("Original typed Lab task/action required")
        task = ScientistLabTask.model_validate(task.model_dump(), strict=True)
        action = ScientistLabAction.model_validate(action.model_dump(), strict=True)
        if type(action.deadline) not in (int, float) or not math.isfinite(action.deadline):
            raise ValueError("Finite wall-clock action deadline required")
        remaining = action.deadline - time.time()
        if remaining <= 0:
            raise TimeoutError("Original action wall-clock deadline expired")
        deadline = time.monotonic() + min(3.0, remaining)
        deadline = self._current(deadline)
        if (
            task.binding.authorization_context_sha256 != self.startup.authorization_context_sha256
            or task.request.program_version != self.startup.program_version
        ):
            raise ValueError("Task authorization context/program differs from review")
        ScientistLabPolicy.check(
            task,
            action,
            authority_url=self.client.authority_url,
            principal_id=self.client.principal_id,
            allowed_suites=self.client.allowed_suites,
        )
        expected = self.review.expected_capabilities.get(task.request.suite)
        if expected is None or task.request.track != expected.track:
            raise ValueError("Unreviewed suite/track")
        budget = task.request.budget
        if (
            budget.experiments > min(self.review.max_experiments, expected.proposal_limit)
            or budget.wall_seconds > self.review.max_wall_seconds
            or not 1 <= budget.model_tokens <= self.review.max_model_tokens
        ):
            raise ValueError("Task exceeds independently reviewed research budget")
        route = "/v1/aos-capability/" + quote(task.request.suite, safe="")
        deadline = verify_joint_api_authority(self.review, self.startup, deadline)
        raw = self.client._request("GET", route, b"", deadline, bound=65536)
        observed = LabCapability.model_validate_json(raw, strict=True)
        if observed != expected:
            raise ValueError("Actual authenticated API capability differs from independent review")
        if task.lab_run_id is not None:
            deadline = verify_joint_api_authority(self.review, self.startup, deadline)
            raw = self.client._request(
                "GET", "/v1/runs/" + task.lab_run_id, b"", deadline, bound=65536
            )
            status = ScientistRunStatus.model_validate_json(raw, strict=True)
            if (
                status.run_id != task.lab_run_id
                or status.origin != "aos"
                or status.purpose != "research"
            ):
                raise ValueError("Actual owner-filtered API research identity differs")
            # Original AOS client additionally brackets terminal report readback and hash.
        self._current(deadline)
