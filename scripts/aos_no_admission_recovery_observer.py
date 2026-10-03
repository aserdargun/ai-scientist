"""Read-only retained-proof recovery witness; never mint an observation.

Default inspection has no runtime authority. Explicit witnessing requires a
separately reviewed finite authorization for this exact current service. Neither
database is written here; AOS independently verifies and records its closure.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import time
from pathlib import Path
from typing import Any

from lab.llm.aos_gpu_control_store import control_deadline
from lab.llm.aos_no_admission_observation import decode


def _providers(config: Path, expected_sha256: str) -> Any:
    from lab.llm.aos_no_admission_recovery_providers import ReadOnlyRecoveryProviders

    return ReadOnlyRecoveryProviders(config, expected_sha256)


def _publish(path: Path, raw: bytes) -> None:
    """Reserve a new private context; a consumer must wait while it is empty."""
    if not path.is_absolute() or ".." in path.parts or path.parent.resolve() != path.parent:
        raise ValueError("unsafe context destination")
    parent = path.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) != 0o700
    ):
        raise ValueError("private context directory required")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def execute(
    config: Path, expected_sha256: str, *, witness: bool, output: Path | None
) -> dict[str, Any]:
    """Keep the authenticated current witness alive within its fixed expiry."""
    previous = control_deadline.get()
    token = control_deadline.set(
        min(time.monotonic() + 30, previous if previous is not None else float("inf"))
    )
    try:
        providers = _providers(config, expected_sha256)
        summary = providers.inspect()
        if not witness:
            if output is not None:
                raise ValueError("inspection has no context output")
            return {"mode": "inspect", "runtime_authorized": False, "inspection": summary}
        if output is None:
            raise ValueError("witnessing requires a new private context destination")
        raw = providers.context()
        context = decode(raw)
        _publish(output, raw)
        expiry = context["freshness"]["expires_boottime_us"]
        remaining = max(0, expiry / 1_000_000 - time.clock_gettime(time.CLOCK_BOOTTIME))
        deadline = time.monotonic() + remaining
        if previous is not None:
            deadline = min(deadline, previous)
        wait_token = control_deadline.set(deadline)
        confirmed = False
        try:
            while (
                time.monotonic() < deadline
                and int(time.clock_gettime(time.CLOCK_BOOTTIME) * 1_000_000) < expiry
            ):
                if providers.read_closure(raw):
                    if (
                        time.monotonic() >= deadline
                        or int(time.clock_gettime(time.CLOCK_BOOTTIME) * 1_000_000) >= expiry
                    ):
                        break
                    confirmed = True
                    break
                time.sleep(0.1)
        finally:
            control_deadline.reset(wait_token)
        return {
            "mode": "witness",
            "aos_closure_confirmed": confirmed,
            "observation_minted": False,
            "observation_reissued": False,
            "original_deadline_renewed": False,
            "gpu_release_proven": False,
        }
    finally:
        control_deadline.reset(token)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--expected-config-sha256", required=True)
    parser.add_argument("--witness", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = execute(
            args.config, args.expected_config_sha256, witness=args.witness, output=args.output
        )
    except Exception:  # Redact all provider errors; interrupts still propagate.
        print(json.dumps({"result": "denied_or_uncertain", "observation_reissued": False}))
        raise SystemExit(2) from None
    print(json.dumps(result, sort_keys=True))
    if args.witness and not result["aos_closure_confirmed"]:
        raise SystemExit(3)


if __name__ == "__main__":
    main()
