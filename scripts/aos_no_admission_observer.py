"""Bounded cleanup-only observer; default inspection cannot mutate either DB.

Issuance requires the separately reviewed real providers and their exact live
Scientist-owned service generation. It writes no AOS records, runs no models
and claims no GPU release. A committed observation remains immutable on timeout
or crash; this command never renews it or changes the original inference ID.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import time
from pathlib import Path
from typing import Any

from lab.llm.aos_gpu_control_store import ControlStore, control_deadline
from lab.llm.aos_no_admission_observation import canonical
from lab.llm.aos_no_admission_store import NoAdmissionObservationStore

OPERATION_SECONDS = 30.0
PUBLIC_DENIAL_CODES = frozenset(
    {
        "canonical_inventory_unknown",
        "canonical_provenance_present",
        "observer_not_current_process",
        "observer_cgroup_mismatch",
        "observer_changed",
        "source_changed",
        "config_changed",
        "cleanup_scope_changed",
        "deadline_exceeded",
    }
)


def _providers(config: Path, expected_sha256: str) -> Any:
    # Inspection and issuance use the same exact reviewed configuration. This
    # lazy import makes no service or database change.
    from lab.llm.aos_no_admission_providers import ReadOnlyObservationProviders

    return ReadOnlyObservationProviders(config, expected_sha256)


def _output(path: Path) -> int:
    """Reserve one new private output; never overwrite a previous proof."""
    if not path.is_absolute() or ".." in path.parts or path.parent.resolve() != path.parent:
        raise ValueError("unsafe proof destination")
    parent = path.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) != 0o700
    ):
        raise ValueError("private proof directory required")
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)


def execute(config: Path, expected_sha256: str, *, issue: bool, output: Path | None) -> dict:
    """Inspect by default, or issue once and wait for exact AOS closure."""
    previous = control_deadline.get()
    deadline = min(
        time.monotonic() + OPERATION_SECONDS,
        previous if previous is not None else float("inf"),
    )
    token = control_deadline.set(deadline)
    try:
        providers = _providers(config, expected_sha256)
        summary = providers.inspect()
        if not issue:
            if output is not None:
                raise ValueError("inspection has no proof output")
            return {"mode": "inspect", "runtime_authorized": False, "inspection": summary}
        if output is None:
            raise ValueError("issuance requires a new private proof destination")
        target = providers.target
        # No canonical constructor/table write until current live authority and
        # physical/original evidence have independently passed.
        providers.observer_authority(target)
        original = providers.original_reader(target)
        providers.physical_reader(target, original)
        descriptor = _output(output)
        try:
            store = NoAdmissionObservationStore(
                ControlStore(providers.canonical_database),
                observer_authority=providers.observer_authority,
                original_reader=providers.original_reader,
                physical_reader=providers.physical_reader,
            )
            observation = store.observe(target, deadline_monotonic=deadline)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(canonical(observation))
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if descriptor != -1:
                os.close(descriptor)
        # Existing consumer callbacks independently authenticate this actual
        # live observer. Keep it live under the original minted proof's expiry;
        # neither a polling interval nor a readback renews that deadline.
        expiry = observation["freshness"]["expires_boottime_us"]
        confirmed = False
        wait_deadline = time.monotonic() + max(
            0, expiry / 1_000_000 - time.clock_gettime(time.CLOCK_BOOTTIME)
        )
        if previous is not None:
            wait_deadline = min(wait_deadline, previous)
        wait_token = control_deadline.set(wait_deadline)
        try:
            while (
                time.monotonic() < wait_deadline
                and int(time.clock_gettime(time.CLOCK_BOOTTIME) * 1_000_000) < expiry
            ):
                if providers.read_closure(observation["observation_sha256"]):
                    confirmed = True
                    break
                time.sleep(0.1)
        finally:
            control_deadline.reset(wait_token)
        return {
            "mode": "issue",
            "request_id": target["request_id"],
            "observation_sha256": observation["observation_sha256"],
            "observation_committed": True,
            "aos_closure_confirmed": confirmed,
            "gpu_release_proven": False,
            "original_deadline_renewed": False,
            "reissue_permitted": False,
        }
    finally:
        control_deadline.reset(token)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--expected-config-sha256", required=True)
    parser.add_argument("--issue", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = execute(
            args.config, args.expected_config_sha256, issue=args.issue, output=args.output
        )
    except (OSError, ValueError, RuntimeError) as error:
        from lab.llm.aos_no_admission_providers import ProviderDenied

        code = (
            str(error)
            if isinstance(error, ProviderDenied) and str(error) in PUBLIC_DENIAL_CODES
            else "unclassified"
        )
        print(
            json.dumps(
                {
                    "result": "denied_or_uncertain",
                    "diagnostic_code": code,
                    "reissue_permitted": False,
                }
            )
        )
        raise SystemExit(2) from None
    print(json.dumps(result, sort_keys=True))
    if args.issue and not result["aos_closure_confirmed"]:
        raise SystemExit(3)


if __name__ == "__main__":
    main()
