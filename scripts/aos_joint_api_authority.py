"""Verify the existing pinned API context's original authority; never renew it."""

from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path

from scripts.check_aos_model_environment import read_regular


def _unique_fields(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate original API authority field")
        result[name] = value
    return result


def verify_joint_api_authority(review, startup, deadline):
    """Return a monotonic deadline bounded by the original boot-time API authority."""
    if time.monotonic() >= deadline:
        raise TimeoutError("Joint Lab authority observation deadline expired")
    matches = [
        name
        for name, checksum in review.source_inputs.items()
        if checksum == startup.authorization_context_sha256
    ]
    if len(matches) != 1:
        raise ValueError("Exactly one pinned original API authority context required")
    path = Path(matches[0])
    if (
        not path.is_absolute()
        or ".." in path.parts
        or str(path) != matches[0]
        or any(ord(character) < 32 or ord(character) == 127 for character in matches[0])
    ):
        raise ValueError("Canonical original API authority context path required")
    raw = read_regular(path, 65536)
    if hashlib.sha256(raw).hexdigest() != startup.authorization_context_sha256:
        raise ValueError("Pinned original API authority context changed/revoked")
    context = json.loads(raw, object_pairs_hook=_unique_fields)
    if type(context) is not dict:
        raise ValueError("Original API authority context must be a document")
    if (
        context.get("schema") != "scientist.joint-api-authorization-context.v1"
        or context.get("owner_id") != startup.principal_id
        or context.get("origin") != "aos"
        or context.get("authority_url") != startup.authority_url
        or context.get("suite_id") not in startup.allowed_suites
        or startup.allowed_suites != frozenset({context.get("suite_id")})
        or context.get("program_version") != startup.program_version
        or context.get("token_sha256") != review.token_sha256
        or context.get("api_unit") != review.api_generation.unit
        or context.get("automatic_dispatch") is not False
        or context.get("native_inference_authorized") is not False
        or context.get("gpu_release_authorized") is not False
    ):
        raise ValueError("Original API authority scope differs from the reviewed startup")
    budget = context.get("budget")
    maximums = {
        "experiments": review.max_experiments,
        "wall_seconds": review.max_wall_seconds,
        "model_tokens": review.max_model_tokens,
    }
    if (
        type(budget) is not dict
        or set(budget) != set(maximums)
        or any(
            type(budget[name]) is not int or not 1 <= maximum <= budget[name]
            for name, maximum in maximums.items()
        )
    ):
        raise ValueError("Reviewed research budget exceeds original API authority")
    issued, expires = context.get("issued_boottime"), context.get("expires_boottime")
    if (
        any(
            type(value) not in (int, float) or not math.isfinite(value)
            for value in (issued, expires)
        )
        or not 0 <= issued < expires <= issued + 3600
    ):
        raise ValueError("Invalid original API authority lifetime")
    boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    # Sample monotonic first so conversion cannot extend expiry by time spent reading clocks.
    monotonic_now = time.monotonic()
    boottime_now = time.clock_gettime(time.CLOCK_BOOTTIME)
    if (
        context.get("boot_id") != boot
        or review.api_generation.boot_id != boot
        or not issued <= boottime_now < expires
    ):
        raise ValueError("Original API authority boot/lifetime expired or differs")
    deadline = min(deadline, monotonic_now + expires - boottime_now)
    if time.monotonic() >= deadline:
        raise TimeoutError("Original API authority observation deadline expired")
    return deadline
