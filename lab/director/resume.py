"""Authenticated, drain-before-CAS takeover of one immutable research execution."""

from __future__ import annotations

import fcntl
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

from lab.director.journal import DirectorRunLease, canonical_bytes
from lab.director.ownership import ExecutionContract, ExecutionOwner, OwnerProcessIdentity
from lab.director.recovery import (
    RecoveryPending,
    _child_blockers,
    _owner_is_proven_dead,
    _stored_owner,
)
from lab.scorer.holdout_supervisor import run_director_restart_recovery_process
from lab.scorer.supervisor import scorer_job_unit_is_quiescent


@dataclass(frozen=True, slots=True)
class ResumeReceipt:
    """Captured new owner; historical receipts remain owned by their original generation."""

    owner: ExecutionOwner
    restart_id: UUID
    started_at: datetime
    deadline_at: datetime
    closure_only: bool
    drain_observation_sha256: str

    @property
    def elapsed_wall_seconds(self) -> float:
        return max(0.0, (datetime.now(UTC) - self.started_at).total_seconds())


def _decode(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("resume RPC returned a malformed object")
    return value


def _read_target(engine: Engine, contract: ExecutionContract) -> dict[str, Any]:
    contract.validate()
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT "
                    "r.state,r.stop_requested,r.payload_sha256,r.request_json,c.current_generation,"
                    "c.mode,x.execution_json,x.execution_sha256,x.started_at,x.deadline_at,"
                    "g.worker_pid,g.worker_start_ticks,g.worker_boot_id,g.worker_unit,"
                    "g.worker_invocation_id,g.worker_cgroup FROM lab.runs r "
                    "JOIN lab.director_execution_contracts x USING(run_id) "
                    "JOIN lab.director_execution_control c USING(run_id) "
                    "JOIN lab.director_owner_generations g ON g.run_id=c.run_id "
                    "AND g.generation=c.current_generation WHERE r.run_id=:run_id"
                ),
                {"run_id": contract.run_id},
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise ValueError("resume requires an existing claimed execution")
    target = dict(row)
    if (
        target["state"] != "running"
        or target["stop_requested"]
        or target["mode"] != "active"
        or target["payload_sha256"] != contract.request_sha256
        or target["execution_sha256"] != contract.execution_sha256
        or target["execution_json"] != contract.execution_json
    ):
        raise ValueError("resume contract changed or run is stopped/terminal")
    return target


def _prior_owner(target: dict[str, Any]) -> dict[str, Any]:
    return {
        key: target[key]
        for key in (
            "worker_pid",
            "worker_start_ticks",
            "worker_boot_id",
            "worker_unit",
            "worker_invocation_id",
            "worker_cgroup",
        )
    }


def verified_checkpoint_manifest(
    engine: Engine, lease: DirectorRunLease, *, artifact_root: Path
) -> list[dict[str, Any]]:
    """Read every bounded checkpoint through the existing receipt/blob/hash validator."""
    with engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT event_json FROM lab.run_events WHERE run_id=:run_id "
                    "AND event_type='director.checkpoint' ORDER BY "
                    "(event_json->>'sequence')::integer LIMIT 10001"
                ),
                {"run_id": lease.run_id},
            )
            .scalars()
            .all()
        )
    if len(rows) > 10000:
        raise ValueError("resume checkpoint inventory exceeds bound")
    result, seen, total = [], set(), 0
    for receipt in rows:
        key = receipt.get("key") if isinstance(receipt, dict) else None
        if not isinstance(key, str) or key in seen:
            raise ValueError("resume checkpoint inventory contains invalid or duplicate keys")
        checkpoint = lease.read_checkpoint(key=key, artifact_root=artifact_root)
        if checkpoint is None or checkpoint["receipt"] != receipt:
            raise ValueError("resume checkpoint inventory changed during validation")
        payload = canonical_bytes(checkpoint["payload"])
        total += len(payload)
        if total > 32 * 1024 * 1024:
            raise ValueError("resume checkpoint inventory exceeds 32MiB")
        result.append(
            {
                "key": key,
                "sequence": receipt["sequence"],
                "payload_sha256": receipt["payload_sha256"],
            }
        )
        seen.add(key)
    return result


def _drain_director_sandbox(
    run_id: UUID, prior: dict[str, Any], artifact_root: Path, deadline: float
) -> None:
    """Reuse the sandbox's label/inode/PID cleanup only after exact old-owner binding."""
    from lab.sandbox.docker_runner import DEFAULT_SANDBOX_IMAGE, LocalDockerRunner

    runner = LocalDockerRunner(
        image=DEFAULT_SANDBOX_IMAGE, work_root=artifact_root.parent / "sandbox" / str(run_id)
    )
    descriptor = os.open(
        runner.admission_lock, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if runner.admission_marker.exists():
            marker = json.loads(runner.admission_marker.read_text(encoding="ascii"))
            if (
                marker.get("owner_pid") != prior["worker_pid"]
                or marker.get("owner_start") != str(prior["worker_start_ticks"])
                or marker.get("boot_id") != prior["worker_boot_id"]
                or marker.get("work_root") != str(runner.work_root)
            ):
                raise RecoveryPending("sandbox intent belongs to another process or run")
            runner._reconcile_owned_containers(deadline=deadline)
    finally:
        os.close(descriptor)


def _prove_terminal_jobs(planner: Engine, run_id: UUID, deadline: float) -> None:
    with planner.connect() as connection:
        jobs = (
            connection.execute(
                text(
                    "SELECT job_id,state,claim_invocation_id FROM scorer.score_jobs "
                    "WHERE run_id=:run_id LIMIT 4097"
                ),
                {"run_id": run_id},
            )
            .mappings()
            .all()
        )
    if len(jobs) > 4096:
        raise RecoveryPending("resume score-job inventory exceeds bound")
    for job in jobs:
        remaining = int(deadline - time.monotonic())
        if remaining < 1 or job["state"] in {"queued", "running"}:
            raise RecoveryPending("resume child reconciliation is incomplete")
        if not scorer_job_unit_is_quiescent(
            job["job_id"],
            expected_invocation_id=job["claim_invocation_id"],
            timeout_seconds=min(30, remaining),
        ):
            raise RecoveryPending("resume exact Scorer generation is not quiescent")


def resume_execution(
    director: Engine,
    planner: Engine,
    *,
    contract: ExecutionContract,
    process: OwnerProcessIdentity,
    restart_id: UUID,
    artifact_root: Path,
    timeout_seconds: int = 120,
) -> ResumeReceipt:
    """Caller holds global dispatch slot and passes freshly verified runtime pins/process.

    No candidate/LLM admission occurs here. Scorer recovery may only reconcile existing
    old-generation children. All host proofs are independently repeated before CAS.
    """
    if isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= 120:
        raise ValueError("resume cleanup bound must be 1..120 seconds")
    expected_unit = (
        f"swapp-ai-scientist-director-resume-{contract.run_id.hex}-{restart_id.hex}.service"
    )
    if process.payload_sha256 != contract.request_sha256 or process.worker_unit != expected_unit:
        raise ValueError("resume process is not bound to this exact request and restart unit")
    deadline = time.monotonic() + timeout_seconds
    with DirectorRunLease(director, contract.run_id) as lease:
        target = _read_target(director, contract)
        with director.connect() as connection:
            stored = (
                connection.execute(
                    text(
                        "SELECT request_json,state,observation_sha256 FROM "
                        "lab.director_restart_requests WHERE restart_id=:id"
                    ),
                    {"id": restart_id},
                )
                .mappings()
                .one_or_none()
            )
        if stored is not None:
            request = stored["request_json"]
            generation = request["expected_generation"]
            if stored["state"] == "claimed":
                # Idempotent post-CAS replay can return only to the exact captured process.
                return _claim(director, contract, process, restart_id, stored["observation_sha256"])
            prior = request["prior_owner"]
            if generation != target["current_generation"] or prior != _prior_owner(target):
                raise RecoveryPending("restart intent belongs to a different current owner")
        else:
            generation, prior = target["current_generation"], _prior_owner(target)
            request = {
                "schema": "director-resume-request.v1",
                "run_id": str(contract.run_id),
                "expected_generation": generation,
                "payload_sha256": contract.request_sha256,
                "execution_sha256": contract.execution_sha256,
                "prior_owner": prior,
            }
        old = _stored_owner({"payload_sha256": contract.request_sha256, **prior})
        if old is None or not _owner_is_proven_dead(old, contract.run_id):
            raise RecoveryPending("prior Director process is still alive")
        with director.begin() as connection:
            intent = _decode(
                connection.execute(
                    text(
                        "SELECT "
                        "lab.begin_director_resume(:id,:run,:generation,:payload,:execution,:request)"
                    ),
                    {
                        "id": restart_id,
                        "run": contract.run_id,
                        "generation": generation,
                        "payload": contract.request_sha256,
                        "execution": contract.execution_sha256,
                        "request": canonical_bytes(request).decode(),
                    },
                ).scalar_one()
            )
        created = datetime.fromisoformat(intent["created_at"])
        remaining_attempt = 120 - (datetime.now(UTC) - created).total_seconds()
        if remaining_attempt <= 0:
            raise RecoveryPending("immutable restart cleanup window expired")
        deadline = min(deadline, time.monotonic() + remaining_attempt)
        _drain_director_sandbox(contract.run_id, prior, artifact_root, deadline)
        if intent["state"] == "pending":
            remaining = int(deadline - time.monotonic())
            if remaining < 1:
                raise RecoveryPending("resume cleanup deadline exhausted")
            result = run_director_restart_recovery_process(restart_id, remaining_seconds=remaining)
            if result.state != "drained":
                raise RecoveryPending("prior Scorer/holdout children are not drained")
        _prove_terminal_jobs(planner, contract.run_id, deadline)
        blockers = _child_blockers(
            planner,
            contract.run_id,
            requires_gpu=contract.execution_json.get("provider") == "local-qwen",
        )
        if blockers or not _owner_is_proven_dead(old, contract.run_id):
            raise RecoveryPending("resume independent drain proof failed: " + ",".join(blockers))
        manifest = verified_checkpoint_manifest(director, lease, artifact_root=artifact_root)
        observation = {
            "schema": "director-resume-drain.v1",
            "restart_id": str(restart_id),
            "run_id": str(contract.run_id),
            "generation": generation,
            "execution_sha256": contract.execution_sha256,
            "prior_owner": prior,
            "owner_dead": True,
            "children_drained": True,
            "sandbox_drained": True,
            "checkpoints": manifest,
        }
        with director.begin() as connection:
            digest = connection.execute(
                text("SELECT lab.record_director_resume_drain(:id,:proof)"),
                {"id": restart_id, "proof": canonical_bytes(observation).decode()},
            ).scalar_one()
        if time.monotonic() >= deadline:
            raise RecoveryPending("resume cleanup deadline exhausted before CAS")
        lease.heartbeat()
        return _claim(director, contract, process, restart_id, digest)


def _claim(
    engine: Engine,
    contract: ExecutionContract,
    process: OwnerProcessIdentity,
    restart_id: UUID,
    digest: str,
) -> ResumeReceipt:
    with engine.begin() as connection:
        value = _decode(
            connection.execute(
                text("SELECT lab.claim_resumed_director(:id,:proof,:process)"),
                {
                    "id": restart_id,
                    "proof": digest,
                    "process": canonical_bytes(asdict(process)).decode(),
                },
            ).scalar_one()
        )
    return ResumeReceipt(
        ExecutionOwner(
            contract.run_id,
            value["generation"],
            value["worker_invocation_id"],
            value["execution_sha256"],
        ),
        restart_id,
        datetime.fromisoformat(value["started_at"]),
        datetime.fromisoformat(value["deadline_at"]),
        value["closure_only"],
        digest,
    )


def assert_historical_receipt(
    engine: Engine, *, owner: ExecutionOwner, admitted_generation: int, execution_sha256: str
) -> None:
    """Explicit read/replay authority; this never grants old-owner mutation or admission."""
    if execution_sha256 != owner.execution_sha256:
        raise ValueError("historical receipt belongs to another immutable execution")
    with engine.begin() as connection:
        connection.execute(
            text(
                "SELECT "
                "lab.assert_director_receipt_history(:run,:generation,:invocation,:execution,:historical)"
            ),
            {
                "run": owner.run_id,
                "generation": owner.generation,
                "invocation": owner.invocation_id,
                "execution": execution_sha256,
                "historical": admitted_generation,
            },
        )
