"""Scorer-only closure of exact orphan jobs for one durable operator stop intent."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, create_engine, text

from lab.director.recovery import _stored_owner
from lab.director.stop_closure import prove_stopped_owner_dead
from lab.scorer.supervisor import (
    _cgroup_is_absent_or_empty,
    _expected_unit_cgroup,
    _job_lifecycle_lock,
    _systemctl_show,
)
from lab.scorer.worker import DEFAULT_DSN_FILE, _secret, verify_systemd_invocation


def _completed_job_invocation(job: dict[str, Any], score: dict[str, Any]) -> str:
    """Read the immutable committed claim, since completion clears live claim fields."""
    invocation = score.get("worker_invocation_id")
    claim = score.get("claim_token")
    metrics = score.get("score")
    if (
        job.get("state") != "completed"
        or str(score.get("score_job_id")) != str(job["job_id"])
        or any(
            str(score.get(field)) != str(job[field])
            for field in ("run_id", "experiment_id", "evaluation_kind", "task_id", "seed")
        )
        or not isinstance(metrics, dict)
        or metrics.get("candidate_sha256") != job["candidate_sha256"]
        or metrics.get("candidate_output_sha256") != job["artifact_sha256"]
        or not isinstance(invocation, str)
        or re.fullmatch(r"[0-9a-f]{32}", invocation) is None
        or claim is not None
    ):
        raise ValueError("completed job differs from its immutable score claim")
    return invocation


def reconcile_stopped_children(
    engine: Engine, stop_id: UUID, *, recovery_invocation: str, timeout_seconds: int = 120
) -> str:
    with engine.connect() as connection:
        request = (
            connection.execute(
                text(
                    "SELECT *,extract(epoch FROM created_at+interval '120 seconds'"
                    "-clock_timestamp()) "
                    "AS remaining,EXISTS(SELECT 1 FROM lab.director_stopped_proposals "
                    "WHERE recovery_id=:id) AS proposal_closure "
                    "FROM lab.director_stop_closures WHERE recovery_id=:id"
                ),
                {"id": stop_id},
            )
            .mappings()
            .one()
        )
        proposal_closure = request.get("proposal_closure", False)
        job_bound = 4096 if proposal_closure else 9216
        jobs = (
            connection.execute(
                text(
                    "SELECT job_id FROM scorer.score_jobs WHERE run_id=:run "
                    "ORDER BY job_id LIMIT :inventory_limit"
                ),
                {"run": request["run_id"], "inventory_limit": job_bound + 1},
            )
            .scalars()
            .all()
        )
    if len(jobs) > job_bound or request["remaining"] <= 0:
        return "pending"
    deadline = time.monotonic() + min(timeout_seconds, float(request["remaining"]))
    owner = _stored_owner(request["owner_json"])
    if owner is None or not prove_stopped_owner_dead(owner, request["run_id"]):
        return "pending"
    for job_id in jobs:
        remaining = int(deadline - time.monotonic())
        if remaining < 1:
            return "pending"
        with _job_lifecycle_lock(job_id, timeout_seconds=min(30, remaining)):
            with engine.connect() as connection:
                if proposal_closure:
                    job = connection.execute(
                        text("SELECT lab.stopped_proposal_job_receipt(:stop,:job)"),
                        {"stop": stop_id, "job": job_id},
                    ).scalar_one()
                else:
                    job = connection.execute(
                        text("SELECT to_jsonb(j) FROM scorer.score_jobs j WHERE job_id=:id"),
                        {"id": job_id},
                    ).scalar_one()
            if not proposal_closure and (
                job["admitted_generation"] != request["expected_generation"]
                or job["execution_sha256"] != request["execution_sha256"]
            ):
                raise ValueError("stopped job belongs to a different execution")
            expected_invocation = job["claim_invocation_id"]
            if not proposal_closure and job["state"] == "completed":
                with engine.connect() as connection:
                    score = connection.execute(
                        text("SELECT to_jsonb(s) FROM scorer.task_scores s WHERE score_job_id=:id"),
                        {"id": job_id},
                    ).scalar_one()
                expected_invocation = _completed_job_invocation(job, score)
            unit = f"swapp-ai-scientist-scorer-{job_id.hex}.service"
            properties = _systemctl_show(unit)
            group = _expected_unit_cgroup(unit)
            if properties.get("LoadState") == "not-found":
                clean = (
                    properties.get("ActiveState") == "inactive"
                    and properties.get("MainPID") == "0"
                    and properties.get("InvocationID") == ""
                    and properties.get("ControlGroup") in {"", group}
                    and _cgroup_is_absent_or_empty(group)
                )
            else:
                clean = (
                    properties.get("LoadState") == "loaded"
                    and properties.get("ActiveState") in {"inactive", "failed"}
                    and properties.get("MainPID") == "0"
                    and properties.get("InvocationID") == expected_invocation
                    and properties.get("ControlGroup", "") in {"", group}
                    and _cgroup_is_absent_or_empty(group)
                )
            if not clean:
                return "pending"
            if job["state"] in {"queued", "running"}:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "SELECT lab.reconcile_stopped_score_job(:stop,:job,:expected,:recovery)"
                        ),
                        {
                            "stop": stop_id,
                            "job": job_id,
                            "expected": json.dumps(job, sort_keys=True),
                            "recovery": recovery_invocation,
                        },
                    ).scalar_one()
    if not prove_stopped_owner_dead(owner, request["run_id"]):
        return "pending"
    with engine.begin() as connection:
        finish = (
            "SELECT lab.finish_stopped_proposal_children(:id,:invocation)"
            if proposal_closure
            else "SELECT lab.finish_stopped_children(:id,:invocation)"
        )
        return str(
            connection.execute(
                text(finish),
                {"id": stop_id, "invocation": recovery_invocation},
            ).scalar_one()
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Close existing jobs after a Director stop")
    parser.add_argument("--stop-id", required=True, type=UUID)
    parser.add_argument("--recovery-unit-id", required=True, type=UUID)
    parser.add_argument("--total-seconds", required=True, type=int)
    args = parser.parse_args(argv)
    if not 1 <= args.total_seconds <= 120:
        raise ValueError("stop recovery budget must be 1..120 seconds")
    invocation = verify_systemd_invocation(
        f"swapp-ai-scientist-scorer-{args.recovery_unit_id.hex}.service"
    )
    engine = create_engine(
        _secret(Path(DEFAULT_DSN_FILE)), pool_size=1, max_overflow=0, pool_timeout=5
    )
    try:
        state = reconcile_stopped_children(
            engine,
            args.stop_id,
            recovery_invocation=invocation.invocation_id,
            timeout_seconds=args.total_seconds,
        )
        print(json.dumps({"stop_id": str(args.stop_id), "state": state}, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"stop-recovery-error-type={type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
