"""Fixed-argv Scorer reconciliation of existing jobs for a dead Director's restart."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from uuid import UUID

from sqlalchemy import Engine, create_engine, text

from lab.director.recovery import _owner_is_proven_dead, _stored_owner
from lab.scorer.holdout import recover_holdout_run
from lab.scorer.supervisor import _job_lifecycle_lock
from lab.scorer.worker import DEFAULT_DSN_FILE, _secret, verify_systemd_invocation


def reconcile_restart_children(
    engine: Engine, restart_id: UUID, *, recovery_invocation: str, timeout_seconds: int = 120
) -> str:
    """Fail only exact dead-generation jobs; preserve old admission/claim provenance."""
    deadline = time.monotonic() + timeout_seconds
    with engine.connect() as connection:
        request = (
            connection.execute(
                text(
                    "SELECT run_id,expected_generation,execution_sha256,request_json,state "
                    "FROM lab.director_restart_requests WHERE restart_id=:id"
                ),
                {"id": restart_id},
            )
            .mappings()
            .one()
        )
    if request["state"] != "pending":
        return "pending"
    prior = _stored_owner(
        {
            "payload_sha256": request["request_json"]["payload_sha256"],
            **request["request_json"]["prior_owner"],
        }
    )
    if prior is None or not _owner_is_proven_dead(prior, request["run_id"]):
        return "pending"
    with engine.connect() as connection:
        jobs = (
            connection.execute(
                text(
                    "SELECT job_id FROM scorer.score_jobs WHERE run_id=:run AND state "
                    "IN ('queued','running') LIMIT 4097"
                ),
                {"run": request["run_id"]},
            )
            .scalars()
            .all()
        )
    if len(jobs) > 4096:
        raise ValueError("restart job inventory exceeds bound")
    for job_id in jobs:
        remaining = int(deadline - time.monotonic())
        if remaining < 1:
            return "pending"
        # Hold the same process lock as Scorer launch, re-read inside it, then CAS.
        with _job_lifecycle_lock(job_id, timeout_seconds=min(30, remaining)):
            with engine.connect() as connection:
                job = connection.execute(
                    text("SELECT to_jsonb(j) FROM scorer.score_jobs j WHERE job_id=:id"),
                    {"id": job_id},
                ).scalar_one()
            if job["state"] not in {"queued", "running"}:
                continue
            if (
                job["admitted_generation"] != request["expected_generation"]
                or job["execution_sha256"] != request["execution_sha256"]
            ):
                raise ValueError("restart cannot reconcile a differently owned job")
            # The helper locks reentrantly through a distinct FD, so use its locked variant.
            from lab.scorer.supervisor import (
                _cgroup_is_absent_or_empty,
                _expected_unit_cgroup,
                _systemctl_show,
            )

            unit = f"swapp-ai-scientist-scorer-{job_id.hex}.service"
            properties = _systemctl_show(unit)
            if properties.get("LoadState") == "not-found":
                clean = _cgroup_is_absent_or_empty(_expected_unit_cgroup(unit))
            else:
                clean = (
                    properties.get("ActiveState") in {"inactive", "failed"}
                    and properties.get("MainPID") == "0"
                    and properties.get("InvocationID") == job["claim_invocation_id"]
                    and properties.get("ControlGroup", "") in {"", _expected_unit_cgroup(unit)}
                    and _cgroup_is_absent_or_empty(_expected_unit_cgroup(unit))
                )
            if not clean:
                return "pending"
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "SELECT lab.reconcile_restart_score_job(:restart,:job,:expected,:recovery)"
                    ),
                    {
                        "restart": restart_id,
                        "job": job_id,
                        "expected": json.dumps(job, sort_keys=True),
                        "recovery": recovery_invocation,
                    },
                ).scalar_one()
    remaining = int(deadline - time.monotonic())
    if remaining < 1:
        return "pending"
    return recover_holdout_run(engine, request["run_id"], timeout_seconds=min(120, remaining))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile existing children for a Director restart"
    )
    parser.add_argument("--restart-id", required=True, type=UUID)
    parser.add_argument("--recovery-unit-id", required=True, type=UUID)
    parser.add_argument("--total-seconds", required=True, type=int)
    args = parser.parse_args(argv)
    if not 1 <= args.total_seconds <= 120:
        raise ValueError("restart recovery budget must be 1..120 seconds")
    invocation = verify_systemd_invocation(
        f"swapp-ai-scientist-scorer-{args.recovery_unit_id.hex}.service"
    )
    engine = create_engine(
        _secret(Path(DEFAULT_DSN_FILE)), pool_size=1, max_overflow=0, pool_timeout=5
    )
    try:
        state = reconcile_restart_children(
            engine,
            args.restart_id,
            recovery_invocation=invocation.invocation_id,
            timeout_seconds=args.total_seconds,
        )
        print(json.dumps({"restart_id": str(args.restart_id), "state": state}, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"resume-recovery-error-type={type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
