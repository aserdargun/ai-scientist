"""Fenced inspection and stop/finalize recovery for interrupted Director runs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess  # nosec B404 -- fixed systemctl command and validated unit names
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import Engine, text

from lab.director.artifacts import read_director_artifact
from lab.director.contracts import ExperimentDocument, TrajectoryDocument
from lab.director.journal import DirectorRunLease, canonical_bytes
from lab.director.ledger import canonical_json_bytes
from lab.director.ownership import ExecutionOwner, owned_execution
from lab.director.task_plan import seal_run_task_plan
from lab.scorer.baseline_report import validate_baseline_report
from lab.scorer.supervisor import run_scorer_finalize_process

OWNER_DRAIN_UNIT = "swapp-ai-scientist-director-drain.service"
OWNER_DISPATCH_PREFIX = "swapp-ai-scientist-director-dispatch-"
SYSTEMCTL = "/usr/bin/systemctl"
RECOVERY_NAMESPACE = UUID("a4cd0b45-a1a8-4a58-b956-fb598dc0cc30")
RECOVERY_EVENT_NAMESPACE = UUID("61bc64ad-94b3-4d3c-b42b-141ebaf81c30")
TERMINAL_EXPERIMENTS = {"scored", "crashed", "abandoned", "rejected"}
TERMINAL_RUNS = {"completed", "failed", "stopped"}


class RecoveryPending(RuntimeError):
    """Recovery cannot proceed until a live or unproven child is resolved."""


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant rejected: {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _validate_canonical_document(payload: bytes, model: type[Any]) -> Any:
    """Validate canonical stored bytes before model defaults are materialized.

    Historical documents may omit optional fields introduced by a later schema
    revision. Hash and canonical checks must cover the stored object as written,
    while the strict model validator may supply those newer defaults afterward.
    """
    parsed = json.loads(
        payload.decode("utf-8"),
        parse_constant=_reject_json_constant,
        object_pairs_hook=_unique_json_object,
    )
    if not isinstance(parsed, dict) or canonical_json_bytes(parsed) != payload:
        raise ValueError("stored document is not canonical JSON")
    return model.model_validate_json(payload, strict=True)


@dataclass(frozen=True, slots=True)
class OwnerGeneration:
    """Process and exact systemd generation that claimed the run."""

    payload_sha256: str
    worker_pid: int
    worker_start_ticks: int
    worker_boot_id: str
    worker_unit: str
    worker_invocation_id: str
    worker_cgroup: str


def _systemctl_show(unit: str) -> dict[str, str]:
    if not _valid_owner_unit(unit):
        raise ValueError("Director owner unit is outside the fixed service identity")
    completed = subprocess.run(  # nosec B603 -- fixed binary and one fixed unit
        [
            SYSTEMCTL,
            "--user",
            "show",
            "--no-pager",
            "--property=LoadState",
            "--property=ActiveState",
            "--property=InvocationID",
            "--property=ControlGroup",
            "--property=MainPID",
            unit,
        ],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    if completed.returncode != 0:
        raise RecoveryPending("cannot inspect the Director systemd generation")
    return {
        key: value
        for line in completed.stdout.splitlines()
        if "=" in line
        for key, value in (line.split("=", 1),)
    }


def _current_process_start_ticks(pid: int) -> int | None:
    try:
        row = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RecoveryPending("cannot verify the recorded Director process") from exc
    fields = row.rsplit(")", maxsplit=1)[1].split()
    if fields[0] == "Z":
        return None
    try:
        return int(fields[19])
    except (IndexError, ValueError) as exc:
        raise RecoveryPending("recorded Director process identity is unreadable") from exc


def _current_boot_id() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError as exc:
        raise RecoveryPending("cannot verify the current host boot identity") from exc
    if re.fullmatch(r"[0-9a-f-]{36}", value) is None:
        raise RecoveryPending("current host boot identity is malformed")
    return value


def _current_cgroup() -> str:
    try:
        rows = Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise RecoveryPending("cannot inspect the Director cgroup") from exc
    matches = [row[3:] for row in rows if row.startswith("0::")]
    if len(matches) != 1 or not matches[0].startswith("/"):
        raise RecoveryPending("Director is not in one unified cgroup")
    return matches[0]


def is_current_dispatch_owner(run_id: UUID) -> bool:
    """Return whether this process is in the fixed run or queue dispatcher unit.

    This is only a launch-routing hint. ``capture_current_owner`` performs the
    authoritative PID, boot, invocation, unit and cgroup checks before claim.
    """
    try:
        unit = _current_cgroup().rsplit("/", maxsplit=1)[-1]
    except RecoveryPending:
        return False
    return unit == OWNER_DRAIN_UNIT or _valid_owner_unit(unit, run_id)


def _valid_owner_unit(unit: str, run_id: UUID | None = None) -> bool:
    if unit == OWNER_DRAIN_UNIT:
        return True
    match = re.fullmatch(
        r"swapp-ai-scientist-director-(?:dispatch-([0-9a-f]{32})|resume-([0-9a-f]{32})-[0-9a-f]{32})[.]service",
        unit,
    )
    return match is not None and (
        run_id is None or (match.group(1) or match.group(2)) == run_id.hex
    )


def capture_current_owner(payload_sha256: str, run_id: UUID) -> OwnerGeneration:
    """Capture a systemd-verified dispatcher generation before run work begins."""
    if re.fullmatch(r"[0-9a-f]{64}", payload_sha256) is None:
        raise ValueError("queued run payload digest is invalid")
    pid = os.getpid()
    start_ticks = _current_process_start_ticks(pid)
    if start_ticks is None:
        raise RecoveryPending("Director dispatcher process identity is unavailable")
    cgroup = _current_cgroup()
    unit = cgroup.rsplit("/", maxsplit=1)[-1]
    if not _valid_owner_unit(unit, run_id):
        raise RecoveryPending("Director process cgroup is outside its fixed service identity")
    environment_unit = os.environ.get("SYSTEMD_UNIT", "")
    if environment_unit and environment_unit != unit:
        raise RecoveryPending("Director service environment differs from its actual cgroup")
    properties = _systemctl_show(unit)
    invocation = properties.get("InvocationID", "").lower()
    environment_invocation = os.environ.get("INVOCATION_ID", "").lower()
    if (
        properties.get("LoadState") != "loaded"
        or properties.get("ActiveState") != "active"
        or re.fullmatch(r"[0-9a-f]{32}", invocation) is None
        or (environment_invocation and environment_invocation != invocation)
        or properties.get("MainPID") != str(pid)
        or properties.get("ControlGroup") != cgroup
    ):
        raise RecoveryPending("Director dispatcher does not match its live systemd generation")
    return OwnerGeneration(
        payload_sha256,
        pid,
        start_ticks,
        _current_boot_id(),
        unit,
        invocation,
        cgroup,
    )


def _stored_owner(row: Any) -> OwnerGeneration | None:
    if row is None:
        return None
    return OwnerGeneration(
        str(row["payload_sha256"]),
        int(row["worker_pid"]),
        int(row["worker_start_ticks"]),
        str(row["worker_boot_id"]),
        str(row["worker_unit"]),
        str(row["worker_invocation_id"]),
        str(row["worker_cgroup"]),
    )


def _owner_is_proven_dead(owner: OwnerGeneration, run_id: UUID) -> bool:
    if not _valid_owner_unit(owner.worker_unit, run_id):
        raise RecoveryPending("recorded Director owner unit is not trusted")
    current_boot = _current_boot_id()
    start_ticks = _current_process_start_ticks(owner.worker_pid)
    if current_boot == owner.worker_boot_id and start_ticks == owner.worker_start_ticks:
        return False
    properties = _systemctl_show(owner.worker_unit)
    if properties.get("LoadState") == "not-found":
        if start_ticks is not None and current_boot == owner.worker_boot_id:
            raise RecoveryPending("recorded Director PID remains after its unit disappeared")
        _require_cgroup_empty(owner.worker_cgroup)
        return True
    if properties.get("LoadState") != "loaded":
        raise RecoveryPending("Director unit state is not authoritative")
    current_invocation = properties.get("InvocationID", "").lower()
    current_cgroup = properties.get("ControlGroup", "")
    if properties.get("ActiveState") in {"active", "activating", "reloading"}:
        raise RecoveryPending(
            "Director service has an active generation; recovery will not touch it"
        )
    if current_cgroup and current_cgroup != owner.worker_cgroup:
        raise RecoveryPending("inactive Director unit cgroup differs from the recorded generation")
    if start_ticks is not None and current_boot == owner.worker_boot_id:
        raise RecoveryPending("recorded Director PID remains but its process generation differs")
    if (
        current_invocation == owner.worker_invocation_id
        and properties.get("ActiveState") != "inactive"
    ):
        raise RecoveryPending("recorded Director generation state is not conclusively inactive")
    _require_cgroup_empty(owner.worker_cgroup)
    return True


def _require_cgroup_empty(control_group: str) -> None:
    if not _cgroup_proven_empty(control_group):
        raise RecoveryPending("recorded Director cgroup still contains a process")


def _cgroup_proven_empty(control_group: str) -> bool:
    """Require the exact persisted cgroup and all descendants to be drained."""
    if not control_group.startswith("/") or ".." in Path(control_group).parts:
        raise RecoveryPending("recorded Director cgroup path is invalid")
    path = Path("/sys/fs/cgroup") / control_group.lstrip("/")
    if path.is_symlink():
        raise RecoveryPending("recorded Director cgroup path is a symlink")
    if not path.exists():
        return True
    if not path.is_dir():
        raise RecoveryPending("recorded Director cgroup path is not a directory")
    try:
        processes = (path / "cgroup.procs").read_text(encoding="ascii").split()
        events = dict(
            line.split()
            for line in (path / "cgroup.events").read_text(encoding="ascii").splitlines()
        )
    except (OSError, ValueError) as exc:
        raise RecoveryPending("cannot verify the recorded Director cgroup drain") from exc
    if processes or events.get("populated") != "0":
        raise RecoveryPending("recorded Director cgroup still contains a process")
    return True


def _recovery_id(run_id: UUID, request_sha256: str) -> UUID:
    return uuid5(RECOVERY_NAMESPACE, f"{run_id}:{request_sha256}")


def _request_sha256(run_id: UUID, action: str) -> str:
    return hashlib.sha256(canonical_bytes({"action": action, "run_id": str(run_id)})).hexdigest()


def _current_owner_row(connection: Any, run_id: UUID) -> Any:
    """Read current generation; legacy fallback only without any generation contract."""
    current = (
        connection.execute(
            text(
                "SELECT g.*,r.payload_sha256 FROM lab.director_execution_control c "
                "JOIN lab.director_owner_generations g ON g.run_id=c.run_id "
                "AND g.generation=c.current_generation JOIN lab.runs r ON r.run_id=c.run_id "
                "WHERE c.run_id=:run"
            ),
            {"run": run_id},
        )
        .mappings()
        .one_or_none()
    )
    if current is not None:
        return current
    return (
        connection.execute(
            text(
                "SELECT o.* FROM lab.director_run_owners o WHERE o.run_id=:run "
                "AND NOT EXISTS(SELECT 1 FROM lab.director_execution_control WHERE run_id=:run) "
                "AND NOT EXISTS(SELECT 1 FROM lab.director_owner_generations WHERE run_id=:run) "
                "AND NOT EXISTS(SELECT 1 FROM lab.director_execution_contracts WHERE run_id=:run)"
            ),
            {"run": run_id},
        )
        .mappings()
        .one_or_none()
    )


def inspect_recovery(
    director: Engine,
    planner: Engine,
    run_id: UUID,
    *,
    allow_retired_shared: bool = False,
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    """Read run, owner, child and terminal-ledger state without mutating it."""
    with director.connect() as connection:
        run = (
            connection.execute(
                text(
                    "SELECT state,payload_sha256,report_sha256,"
                    "request_json->>'purpose' AS purpose FROM lab.runs WHERE run_id=:run_id"
                ),
                {"run_id": run_id},
            )
            .mappings()
            .one_or_none()
        )
        if run is None:
            raise ValueError("run does not exist")
        owner_row = _current_owner_row(connection, run_id)
        experiments = (
            connection.execute(
                text(
                    "SELECT experiment_id,status FROM lab.experiments "
                    "WHERE run_id=:run_id ORDER BY sequence"
                ),
                {"run_id": run_id},
            )
            .mappings()
            .all()
        )
        report_row = (
            connection.execute(
                text("SELECT report_json,report_sha256 FROM lab.reports WHERE run_id=:run_id"),
                {"run_id": run_id},
            )
            .mappings()
            .one_or_none()
        )
    records: list[dict[str, Any]] = []
    for experiment in experiments:
        try:
            with director.connect() as receipt_connection:
                receipt = receipt_connection.execute(
                    text("SELECT lab.experiment_record_receipt(:id)"),
                    {"id": experiment["experiment_id"]},
                ).scalar_one()
        except Exception:
            records.append({"experiment_id": experiment["experiment_id"]})
        else:
            if isinstance(receipt, dict):
                records.append(receipt)
            else:
                records.append({"experiment_id": experiment["experiment_id"]})
    with planner.connect() as connection:
        jobs = (
            connection.execute(
                text(
                    "SELECT state,count(*) AS count FROM scorer.score_jobs "
                    "WHERE run_id=:run_id GROUP BY state"
                ),
                {"run_id": run_id},
            )
            .mappings()
            .all()
        )
        task_count = connection.execute(
            text("SELECT count(*) FROM scorer.run_tasks WHERE run_id=:run_id"),
            {"run_id": run_id},
        ).scalar_one()
    owner = _stored_owner(owner_row)
    open_jobs = [str(row["state"]) for row in jobs if row["state"] in {"queued", "running"}]
    incomplete_experiments = [
        str(row["experiment_id"])
        for row in experiments
        if row["status"] not in TERMINAL_EXPERIMENTS
    ]
    expected_status = {str(row["experiment_id"]): str(row["status"]) for row in experiments}
    missing_pairs: list[str] = []
    invalid_pairs: list[str] = []
    artifact_root = artifact_root or Path("data/runtime/director-artifacts") / str(run_id)
    record_ids = {str(row["experiment_id"]) for row in records}
    missing_pairs.extend(
        str(row["experiment_id"])
        for row in experiments
        if str(row["experiment_id"]) not in record_ids
    )
    for row in records:
        experiment_id = str(row["experiment_id"])
        try:
            required = (
                "run_id",
                "status",
                "experiment_sha256",
                "experiment_blob_sha256",
                "trajectory_sha256",
                "trajectory_blob_sha256",
                "messages_blob_sha256",
            )
            if any(row.get(key) is None for key in required):
                missing_pairs.append(experiment_id)
                continue
            if (
                str(row["run_id"]) != str(run_id)
                or row["status"] not in TERMINAL_EXPERIMENTS
                or row["status"] != expected_status.get(experiment_id)
            ):
                raise ValueError("terminal receipt identity")
            experiment_bytes = read_director_artifact(
                str(row["experiment_blob_sha256"]), artifact_root=artifact_root
            )
            trajectory_bytes = read_director_artifact(
                str(row["trajectory_blob_sha256"]), artifact_root=artifact_root
            )
            if hashlib.sha256(experiment_bytes).hexdigest() != row["experiment_sha256"]:
                raise ValueError("experiment blob digest")
            if hashlib.sha256(trajectory_bytes).hexdigest() != row["trajectory_sha256"]:
                raise ValueError("trajectory blob digest")
            experiment_doc = _validate_canonical_document(experiment_bytes, ExperimentDocument)
            trajectory = _validate_canonical_document(trajectory_bytes, TrajectoryDocument)
            if (
                experiment_doc.run_id != run_id
                or experiment_doc.experiment_id != experiment_id
                or trajectory.run_id != run_id
                or trajectory.experiment_id != experiment_id
                or experiment_doc.status not in TERMINAL_EXPERIMENTS
                or experiment_doc.status != row["status"]
                or trajectory.kind != experiment_doc.kind
                or trajectory.experiment_number != experiment_doc.experiment_number
                or trajectory.baseline_name != experiment_doc.baseline_name
                or trajectory.calibration_sha256 != experiment_doc.calibration_sha256
                or trajectory.infrastructure_stop != experiment_doc.infrastructure_stop
                or trajectory.outcome != experiment_doc.decision
                or trajectory.messages_blob_sha256 != row["messages_blob_sha256"]
            ):
                raise ValueError("document identity")
            candidate_bytes = read_director_artifact(
                experiment_doc.candidate_blob_sha256, artifact_root=artifact_root
            )
            if hashlib.sha256(candidate_bytes).hexdigest() != experiment_doc.candidate_sha256:
                raise ValueError("candidate source digest")
            messages = read_director_artifact(
                str(row["messages_blob_sha256"]), artifact_root=artifact_root
            )
            if hashlib.sha256(messages).hexdigest() != row["messages_blob_sha256"]:
                raise ValueError("messages artifact hash")
        except Exception:
            invalid_pairs.append(experiment_id)
    reasons: list[str] = []
    if owner is None and run["state"] not in TERMINAL_RUNS:
        reasons.append("owner_proof_missing")
    if open_jobs:
        reasons.append("score_jobs_active")
    if incomplete_experiments:
        reasons.append("experiments_nonterminal")
    if missing_pairs:
        reasons.append("experiment_trajectory_pair_missing")
    if invalid_pairs:
        reasons.append("experiment_trajectory_pair_invalid")
    report_valid = False
    if report_row is not None:
        report_json = report_row["report_json"]
        try:
            report_digest = hashlib.sha256(canonical_bytes(report_json)).hexdigest()
            schema_valid = False
            if isinstance(report_json, dict):
                if run["purpose"] == "baseline":
                    validate_baseline_report(report_json)
                    schema_valid = True
                elif run["purpose"] == "mode-stream":
                    from lab.scorer.mode_stream_report import validate_mode_stream_report

                    validate_mode_stream_report(report_json)
                    schema_valid = True
                else:
                    schema_valid = report_json.get("schema") == "lab.report.v1"
            report_valid = (
                report_digest == report_row["report_sha256"] == run["report_sha256"]
                and schema_valid
                and report_json.get("run_id") == str(run_id)
                and report_json.get("status") == run["state"]
            )
        except (TypeError, ValueError):
            report_valid = False
    elif run["report_sha256"] is not None:
        report_valid = False
    if run["state"] in TERMINAL_RUNS and not report_valid:
        reasons.append("terminal_report_missing_or_invalid")
    owner_dead: bool | None = None
    if owner is not None and run["state"] not in TERMINAL_RUNS:
        try:
            if allow_retired_shared:
                from lab.director.stop_closure import prove_stopped_owner_dead

                owner_dead = prove_stopped_owner_dead(owner, run_id)
            else:
                owner_dead = _owner_is_proven_dead(owner, run_id)
        except RecoveryPending:
            owner_dead = None
        if owner_dead is False:
            reasons.append("owner_generation_active")
        elif owner_dead is None:
            reasons.append("owner_generation_unverified")
    return {
        "run_id": str(run_id),
        "recovery_id": str(_recovery_id(run_id, _request_sha256(run_id, "stop_and_finalize"))),
        "run_state": str(run["state"]),
        "payload_sha256": str(run["payload_sha256"]),
        "report_sha256": run["report_sha256"],
        "report_valid": report_valid,
        "owner": asdict(owner) if owner is not None else None,
        "owner_proven_dead": owner_dead,
        "experiment_count": len(experiments),
        "terminal_experiments": len(experiments) - len(incomplete_experiments),
        "incomplete_experiment_ids": incomplete_experiments,
        "missing_pair_ids": missing_pairs,
        "invalid_pair_ids": invalid_pairs,
        "active_score_job_states": open_jobs,
        "score_task_count": int(task_count),
        "score_completion_count": None,
        "recovery_blockers": reasons,
        "can_finalize": (
            report_valid
            if run["state"] in TERMINAL_RUNS
            else owner is not None and owner_dead is True and not reasons
        ),
    }


def _persist_receipt(
    director: Engine,
    *,
    recovery_id: UUID,
    run_id: UUID,
    request_sha256: str,
    observed_state: str,
    owner: OwnerGeneration | None,
    state: str,
    result: dict[str, Any],
    execution_owner: ExecutionOwner | None = None,
) -> dict[str, Any]:
    result_bytes = canonical_bytes(result)
    digest = hashlib.sha256(result_bytes).hexdigest()
    values = {
        "recovery_id": recovery_id,
        "run_id": run_id,
        "request_sha256": request_sha256,
        "observed_run_state": observed_state,
        "owner_pid": owner.worker_pid if owner else None,
        "owner_start_ticks": owner.worker_start_ticks if owner else None,
        "owner_boot_id": owner.worker_boot_id if owner else None,
        "owner_unit": owner.worker_unit if owner else None,
        "owner_invocation_id": owner.worker_invocation_id if owner else None,
        "owner_cgroup": owner.worker_cgroup if owner else None,
        "state": state,
        "result_json": result_bytes.decode("utf-8"),
        "result_sha256": digest,
    }
    with director.begin() as connection:
        existing = (
            connection.execute(
                text("SELECT * FROM lab.director_recoveries WHERE recovery_id=:id FOR UPDATE"),
                {"id": recovery_id},
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            if (
                existing["run_id"] != run_id
                or existing["request_sha256"] != request_sha256
                or existing["action"] != "stop_and_finalize"
            ):
                raise ValueError("recovery ID is already bound to a different request")
            expected_owner = (
                owner.worker_pid if owner else None,
                owner.worker_start_ticks if owner else None,
                owner.worker_boot_id if owner else None,
                owner.worker_unit if owner else None,
                owner.worker_invocation_id if owner else None,
                owner.worker_cgroup if owner else None,
            )
            stored_owner = (
                existing["owner_pid"],
                existing["owner_start_ticks"],
                existing["owner_boot_id"],
                existing["owner_unit"],
                existing["owner_invocation_id"],
                existing["owner_cgroup"],
            )
            if stored_owner != expected_owner:
                raise ValueError("recovery retry owner-generation binding changed")
            if existing["state"] in {"completed", "failed"}:
                old = existing["result_json"]
                old_bytes = canonical_bytes(old)
                if hashlib.sha256(old_bytes).hexdigest() != existing["result_sha256"]:
                    raise ValueError("stored terminal recovery receipt digest is invalid")
                return {**old, "recovery_state": existing["state"], "replayed": True}
        if execution_owner is not None:
            # Evidence belongs to this captured generation, not a later worker.
            # A terminal run cannot use the active-work assertion RPC. Lock and
            # compare its canonical rows only; grant no new mutation capability.
            identity = (
                connection.execute(
                    text(
                        "SELECT r.state,r.report_sha256,c.current_generation,"
                        "o.worker_invocation_id,o.execution_sha256 "
                        "FROM lab.runs r JOIN lab.director_execution_control c USING(run_id) "
                        "JOIN lab.director_owner_generations o ON o.run_id=c.run_id "
                        "AND o.generation=c.current_generation WHERE r.run_id=:run_id "
                        "FOR UPDATE OF r"
                    ),
                    {"run_id": run_id},
                )
                .mappings()
                .one_or_none()
            )
            if (
                execution_owner.run_id != run_id
                or identity is None
                or identity["state"] != "stopped"
                or identity["report_sha256"] != result.get("report_sha256")
                or identity["current_generation"] != execution_owner.generation
                or identity["worker_invocation_id"] != execution_owner.invocation_id
                or identity["execution_sha256"] != execution_owner.execution_sha256
            ):
                raise ValueError("terminal cancellation audit execution generation changed")
        if existing is None:
            values["state"] = "started"
            values["result_json"] = None
            values["result_sha256"] = None
            connection.execute(
                text(
                    "INSERT INTO lab.director_recoveries(recovery_id,run_id,request_sha256,action,"
                    "observed_run_state,owner_pid,owner_start_ticks,owner_boot_id,owner_unit,"
                    "owner_invocation_id,owner_cgroup,state,result_json,result_sha256) VALUES "
                    "(:recovery_id,:run_id,:request_sha256,'stop_and_finalize',:observed_run_state,"
                    ":owner_pid,:owner_start_ticks,:owner_boot_id,:owner_unit,:owner_invocation_id,"
                    ":owner_cgroup,:state,CAST(:result_json AS jsonb),:result_sha256)"
                ),
                values,
            )
        if state != "started":
            connection.execute(
                text(
                    "UPDATE lab.director_recoveries SET state=:state,"
                    "result_json=CAST(:result AS jsonb),"
                    "result_sha256=:digest,updated_at=now() WHERE recovery_id=:id"
                ),
                {
                    "state": state,
                    "result": result_bytes.decode("utf-8"),
                    "digest": digest,
                    "id": recovery_id,
                },
            )
    return {**result, "recovery_state": state, "replayed": False}


def _ensure_recovery_intent(
    director: Engine,
    *,
    recovery_id: UUID,
    run_id: UUID,
    request_sha256: str,
    observed_state: str,
    owner: OwnerGeneration | None,
) -> dict[str, Any] | None:
    """Durably bind the operator request before attempting fenced side effects."""
    with director.begin() as connection:
        existing = (
            connection.execute(
                text("SELECT * FROM lab.director_recoveries WHERE recovery_id=:id FOR UPDATE"),
                {"id": recovery_id},
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            expected_owner = (
                owner.worker_pid if owner else None,
                owner.worker_start_ticks if owner else None,
                owner.worker_boot_id if owner else None,
                owner.worker_unit if owner else None,
                owner.worker_invocation_id if owner else None,
                owner.worker_cgroup if owner else None,
            )
            stored_owner = (
                existing["owner_pid"],
                existing["owner_start_ticks"],
                existing["owner_boot_id"],
                existing["owner_unit"],
                existing["owner_invocation_id"],
                existing["owner_cgroup"],
            )
            if (
                existing["run_id"] != run_id
                or existing["request_sha256"] != request_sha256
                or existing["action"] != "stop_and_finalize"
                or stored_owner != expected_owner
            ):
                raise ValueError("recovery ID is bound to different immutable request state")
            if existing["state"] in {"completed", "failed"}:
                return dict(existing)
            return None
        connection.execute(
            text(
                "INSERT INTO lab.director_recoveries(recovery_id,run_id,request_sha256,action,"
                "observed_run_state,owner_pid,owner_start_ticks,owner_boot_id,owner_unit,"
                "owner_invocation_id,owner_cgroup,state) VALUES (:id,:run_id,:request_sha,"
                "'stop_and_finalize',:observed,:pid,:start,:boot,:unit,:invocation,:cgroup,'started')"
            ),
            {
                "id": recovery_id,
                "run_id": run_id,
                "request_sha": request_sha256,
                "observed": observed_state,
                "pid": owner.worker_pid if owner else None,
                "start": owner.worker_start_ticks if owner else None,
                "boot": owner.worker_boot_id if owner else None,
                "unit": owner.worker_unit if owner else None,
                "invocation": owner.worker_invocation_id if owner else None,
                "cgroup": owner.worker_cgroup if owner else None,
            },
        )
    return None


def _stopped_proposal_requires_attempted_closure(
    director: Engine, run_id: UUID, recovery_id: UUID
) -> bool:
    """Route by the actual proposal stage or its durable stop marker, never fabricate a stage."""
    with director.connect() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT e.status,(SELECT to_jsonb(s)->>'stop_mode' "
                    "FROM lab.director_stopped_proposals s WHERE s.recovery_id=:id "
                    "AND s.experiment_id=e.experiment_id) AS stop_mode "
                    "FROM lab.experiments e WHERE e.run_id=:run AND e.kind='proposal' "
                    "ORDER BY e.sequence LIMIT 2"
                ),
                {"id": recovery_id, "run": run_id},
            )
            .mappings()
            .all()
        )
    if len(rows) != 1:
        raise RecoveryPending("proposal stop requires one exact registered proposal")
    row = rows[0]
    if row["status"] == "primary_running" or row["stop_mode"] == "primary_admitted":
        return True
    if row["status"] in {"proposed", "abandoned"} and row["stop_mode"] in {None, "unattempted"}:
        return False
    raise RecoveryPending("proposal stop stage is unsupported; original admission is retained")


def apply_stop_and_finalize(
    director: Engine,
    planner: Engine,
    *,
    run_id: UUID,
    recovery_id: UUID,
    remaining_seconds: int = 600,
    reconcile_interrupted_baseline: bool = False,
    reconcile_interrupted_proposal: bool = False,
    artifact_root: Path | None = None,
    expected_owner: ExecutionOwner | None = None,
    cleanup_deadline: datetime | None = None,
) -> dict[str, Any]:
    """Stop a proven-dead Director owner and finalize only fully verified work."""
    if isinstance(remaining_seconds, bool) or not 1 <= remaining_seconds <= 600:
        raise ValueError("recovery finalization deadline must be in 1..600 seconds")
    if cleanup_deadline is not None:
        if not isinstance(cleanup_deadline, datetime) or cleanup_deadline.tzinfo is None:
            raise ValueError("stop cleanup deadline must be timezone aware")
        remaining_seconds = min(
            remaining_seconds,
            int((cleanup_deadline.astimezone(UTC) - datetime.now(UTC)).total_seconds()),
        )
        if remaining_seconds < 1:
            raise RecoveryPending("original automatic stop cleanup deadline expired")
    request_sha = _request_sha256(run_id, "stop_and_finalize")
    expected_recovery_id = _recovery_id(run_id, request_sha)
    if recovery_id != expected_recovery_id:
        raise ValueError("recovery ID does not match this immutable recovery request")
    with director.connect() as connection:
        run = (
            connection.execute(
                text(
                    "SELECT state,payload_sha256,report_sha256,request_json,owner_id,origin "
                    "FROM lab.runs WHERE run_id=:id"
                ),
                {"id": run_id},
            )
            .mappings()
            .one_or_none()
        )
        if run is None:
            raise ValueError("run does not exist")
        owner_row = _current_owner_row(connection, run_id)
    owner = _stored_owner(owner_row)
    with director.connect() as connection:
        execution_row = (
            connection.execute(
                text(
                    "SELECT generation.generation,generation.worker_invocation_id,"
                    "generation.execution_sha256 "
                    "FROM lab.director_execution_control AS control "
                    "JOIN lab.director_owner_generations AS generation "
                    "ON generation.run_id=control.run_id "
                    "AND generation.generation=control.current_generation "
                    "WHERE control.run_id=:id AND control.mode='active'"
                ),
                {"id": run_id},
            )
            .mappings()
            .one_or_none()
        )
    execution_owner = None
    if execution_row is not None:
        execution_owner = ExecutionOwner(
            run_id=run_id,
            generation=execution_row["generation"],
            invocation_id=execution_row["worker_invocation_id"],
            execution_sha256=execution_row["execution_sha256"],
        )
        if owner is None or execution_owner.invocation_id != owner.worker_invocation_id:
            raise ValueError("current execution generation differs from the proven owner")
    if expected_owner is not None and execution_owner != expected_owner:
        raise RecoveryPending("automatic stop execution generation changed")
    if owner is not None and owner.payload_sha256 != run["payload_sha256"]:
        raise ValueError("persisted Director owner payload differs from the run request")
    if run["state"] not in TERMINAL_RUNS | {"running", "stop_requested"}:
        raise ValueError("only a claimed or terminal run can enter Director recovery")
    terminal_receipt = _ensure_recovery_intent(
        director,
        recovery_id=recovery_id,
        run_id=run_id,
        request_sha256=request_sha,
        observed_state=str(run["state"]),
        owner=owner,
    )
    if terminal_receipt is not None:
        result = terminal_receipt["result_json"]
        if (
            not isinstance(result, dict)
            or hashlib.sha256(canonical_bytes(result)).hexdigest()
            != terminal_receipt["result_sha256"]
        ):
            raise ValueError("stored terminal recovery receipt digest is invalid")
        return {**result, "recovery_state": terminal_receipt["state"], "replayed": True}
    existing = inspect_recovery(
        director,
        planner,
        run_id,
        allow_retired_shared=reconcile_interrupted_baseline or reconcile_interrupted_proposal,
        artifact_root=artifact_root,
    )
    if run["state"] in TERMINAL_RUNS:
        result = {**existing, "action": "already_terminal"}
        return _persist_receipt(
            director,
            recovery_id=recovery_id,
            run_id=run_id,
            request_sha256=request_sha,
            observed_state=str(run["state"]),
            owner=owner,
            state="completed" if existing["report_valid"] else "pending",
            result=result,
        )
    if owner is None:
        result = {**existing, "action": "pending", "reason": "owner_proof_missing"}
        return _persist_receipt(
            director,
            recovery_id=recovery_id,
            run_id=run_id,
            request_sha256=request_sha,
            observed_state=str(run["state"]),
            owner=None,
            state="pending",
            result=result,
        )
    if existing["owner_proven_dead"] is not True:
        reason = (
            "owner_generation_active"
            if "owner_generation_active" in existing["recovery_blockers"]
            else "owner_generation_unverified"
        )
        result = {**existing, "action": "pending", "reason": reason}
        return _persist_receipt(
            director,
            recovery_id=recovery_id,
            run_id=run_id,
            request_sha256=request_sha,
            observed_state=str(run["state"]),
            owner=owner,
            state="pending",
            result=result,
        )
    try:
        lease = DirectorRunLease(director, run_id)
        lease.__enter__()
    except RuntimeError as exc:
        if "capacity_busy" not in str(exc):
            raise
        result = {**existing, "action": "pending", "reason": "director_lease_active"}
        return _persist_receipt(
            director,
            recovery_id=recovery_id,
            run_id=run_id,
            request_sha256=request_sha,
            observed_state=str(run["state"]),
            owner=owner,
            state="pending",
            result=result,
        )
    try:
        if reconcile_interrupted_baseline or reconcile_interrupted_proposal:
            from lab.director.stop_closure import prove_stopped_owner_dead

            owner_dead = prove_stopped_owner_dead(owner, run_id)
        else:
            owner_dead = _owner_is_proven_dead(owner, run_id)
        if not owner_dead:
            result = {**existing, "action": "pending", "reason": "owner_generation_active"}
            return _persist_receipt(
                director,
                recovery_id=recovery_id,
                run_id=run_id,
                request_sha256=request_sha,
                observed_state=str(run["state"]),
                owner=owner,
                state="pending",
                result=result,
            )
        with director.connect() as connection:
            run_request = connection.execute(
                text("SELECT request_json FROM lab.runs WHERE run_id=:id"), {"id": run_id}
            ).scalar_one()
        requires_gpu = isinstance(run_request, dict) and run_request.get("provider") == "local-qwen"
        if (reconcile_interrupted_baseline or reconcile_interrupted_proposal) and (
            run["state"] == "stop_requested"
        ):
            from lab.director.stop_closure import reconcile_stopped_baseline

            try:
                if execution_owner is None or artifact_root is None:
                    raise RecoveryPending(
                        "stopped baseline needs its original execution and artifacts"
                    )
                from lab.director.stopped_proposal import reconcile_stopped_proposal

                if reconcile_interrupted_proposal and _stopped_proposal_requires_attempted_closure(
                    director, run_id, recovery_id
                ):
                    from lab.director.attempted_proposal_stop import (
                        reconcile_stopped_attempted_proposal,
                    )

                    proposal_reconcile = reconcile_stopped_attempted_proposal
                else:
                    proposal_reconcile = reconcile_stopped_proposal
                reconcile = (
                    proposal_reconcile
                    if reconcile_interrupted_proposal
                    else reconcile_stopped_baseline
                )
                reconcile(
                    director,
                    planner,
                    run_id=run_id,
                    recovery_id=recovery_id,
                    owner=owner,
                    execution_owner=execution_owner,
                    lease=lease,
                    artifact_root=artifact_root,
                    **({"deadline_at": cleanup_deadline} if cleanup_deadline is not None else {}),
                )
            except RecoveryPending as exc:
                return _persist_receipt(
                    director,
                    recovery_id=recovery_id,
                    run_id=run_id,
                    request_sha256=request_sha,
                    observed_state="stop_requested",
                    owner=owner,
                    state="pending",
                    result={**existing, "action": "pending", "reason": str(exc)},
                )
        blockers = _child_blockers(planner, run_id, requires_gpu=requires_gpu)
        if blockers:
            with director.begin() as connection:
                # The owner-bound RPC supplies the API stop context and skips
                # the guarded UPDATE entirely when stop was already requested.
                connection.execute(
                    text("SELECT lab.request_director_run_stop(:id,:owner_id,:origin)"),
                    {"id": run_id, "owner_id": run["owner_id"], "origin": run["origin"]},
                )
                _insert_event(
                    connection,
                    run_id,
                    recovery_id,
                    "run.recovery_stop_requested",
                    {
                        "recovery_id": str(recovery_id),
                        "reason": "unresolved_children",
                        "blockers": blockers,
                    },
                )
            result = {
                **existing,
                "action": "pending",
                "reason": "unresolved_children",
                "child_blockers": blockers,
            }
            return _persist_receipt(
                director,
                recovery_id=recovery_id,
                run_id=run_id,
                request_sha256=request_sha,
                observed_state=str(run["state"]),
                owner=owner,
                state="pending",
                result=result,
            )
        with director.begin() as connection:
            current = connection.execute(
                text("SELECT state FROM lab.runs WHERE run_id=:id FOR UPDATE"), {"id": run_id}
            ).scalar_one()
            if current == "running":
                connection.execute(
                    text("SELECT lab.request_director_run_stop(:id,:owner_id,:origin)"),
                    {"id": run_id, "owner_id": run["owner_id"], "origin": run["origin"]},
                )
                _insert_event(
                    connection,
                    run_id,
                    recovery_id,
                    "run.recovery_stop_requested",
                    {"recovery_id": str(recovery_id), "reason": "operator_recovery"},
                )
            elif current != "stop_requested":
                result = {
                    **existing,
                    "action": "pending",
                    "reason": "run_state_changed",
                    "current_state": current,
                }
                return _persist_receipt(
                    director,
                    recovery_id=recovery_id,
                    run_id=run_id,
                    request_sha256=request_sha,
                    observed_state=str(current),
                    owner=owner,
                    state="pending",
                    result=result,
                )
        # Re-verify all terminal documents while the Director run lease is held.
        verified = inspect_recovery(
            director,
            planner,
            run_id,
            allow_retired_shared=reconcile_interrupted_baseline or reconcile_interrupted_proposal,
            artifact_root=artifact_root,
        )
        if verified["recovery_blockers"]:
            result = {**verified, "action": "pending", "reason": "ledger_not_terminal"}
            return _persist_receipt(
                director,
                recovery_id=recovery_id,
                run_id=run_id,
                request_sha256=request_sha,
                observed_state="stop_requested",
                owner=owner,
                state="pending",
                result=result,
            )
        if reconcile_interrupted_baseline or reconcile_interrupted_proposal:
            from lab.director.stop_closure import remaining_stop_closure_seconds

            remaining = remaining_stop_closure_seconds(director, recovery_id)
            if remaining is not None:
                if remaining < 1:
                    return _persist_receipt(
                        director,
                        recovery_id=recovery_id,
                        run_id=run_id,
                        request_sha256=request_sha,
                        observed_state="stop_requested",
                        owner=owner,
                        state="pending",
                        result={
                            **verified,
                            "action": "pending",
                            "reason": "original_stop_cleanup_deadline_expired",
                        },
                    )
                remaining_seconds = min(remaining_seconds, remaining)
        canceled_budget_audit = None
        if requires_gpu and execution_owner is not None and artifact_root is not None:
            from lab.director.cancelled_attempt_audit import cancelled_attempt_audit

            canceled_budget_audit = cancelled_attempt_audit(
                director,
                lease=lease,
                owner=execution_owner,
                request=run_request,
                artifact_root=artifact_root,
            )
        try:
            if cleanup_deadline is not None:
                remaining_seconds = min(
                    remaining_seconds,
                    int((cleanup_deadline.astimezone(UTC) - datetime.now(UTC)).total_seconds()),
                )
                if remaining_seconds < 1:
                    raise RecoveryPending("original automatic stop cleanup deadline expired")
            if execution_owner is None:
                raise ValueError("stop seal requires its historical execution owner")
            with owned_execution(execution_owner):
                seal_run_task_plan(planner, run_id=run_id)
        except Exception as exc:
            result = {
                **verified,
                "action": "pending",
                "reason": "task_plan_seal_failed",
                "error_type": type(exc).__name__,
            }
            return _persist_receipt(
                director,
                recovery_id=recovery_id,
                run_id=run_id,
                request_sha256=request_sha,
                observed_state="stop_requested",
                owner=owner,
                state="pending",
                result=result,
            )
        try:
            if cleanup_deadline is not None:
                remaining_seconds = min(
                    remaining_seconds,
                    int((cleanup_deadline.astimezone(UTC) - datetime.now(UTC)).total_seconds()),
                )
                if remaining_seconds < 1:
                    raise RecoveryPending("original automatic stop cleanup deadline expired")
            if execution_owner is None:
                raise ValueError("Scorer finalization requires a captured execution generation")
            if reconcile_interrupted_baseline or reconcile_interrupted_proposal:
                remaining = remaining_stop_closure_seconds(director, recovery_id)
                if remaining is not None:
                    if remaining < 1:
                        raise RecoveryPending(
                            "original stopped-closure deadline expired before finalizer"
                        )
                    remaining_seconds = min(remaining_seconds, remaining)
            finalized = run_scorer_finalize_process(
                run_id,
                admitted_generation=execution_owner.generation,
                execution_sha256=execution_owner.execution_sha256,
                remaining_seconds=remaining_seconds,
                artifact_root=artifact_root,
            )
        except Exception as exc:
            result = {
                **verified,
                "action": "pending",
                "reason": "scorer_finalizer_unavailable",
                "error_type": type(exc).__name__,
            }
            return _persist_receipt(
                director,
                recovery_id=recovery_id,
                run_id=run_id,
                request_sha256=request_sha,
                observed_state="stop_requested",
                owner=owner,
                state="pending",
                result=result,
            )
        if finalized.exit_code != 0 or (finalized.result or {}).get("state") != "finalized":
            result = {
                **verified,
                "action": "pending",
                "reason": (
                    "scorer_finalizer_not_ready"
                    if finalized.exit_code == 0
                    else "scorer_finalizer_failed"
                ),
                "finalizer_exit_code": finalized.exit_code,
                "finalizer_state": (finalized.result or {}).get("state", "unknown"),
            }
            return _persist_receipt(
                director,
                recovery_id=recovery_id,
                run_id=run_id,
                request_sha256=request_sha,
                observed_state="stop_requested",
                owner=owner,
                state="pending",
                result=result,
            )
        with director.connect() as connection:
            final = (
                connection.execute(
                    text(
                        "SELECT r.state,r.report_sha256,p.report_json,"
                        "p.report_sha256 AS stored_report_sha "
                        "FROM lab.runs r LEFT JOIN lab.reports p ON p.run_id=r.run_id "
                        "WHERE r.run_id=:id"
                    ),
                    {"id": run_id},
                )
                .mappings()
                .one()
            )
        report = final["report_json"]
        report_bytes = canonical_bytes(report)
        report_digest = hashlib.sha256(report_bytes).hexdigest()
        if (
            final["state"] != "stopped"
            or not isinstance(report, dict)
            or report.get("run_id") != str(run_id)
            or report.get("status") != "stopped"
            or final["report_sha256"] != report_digest
            or final["stored_report_sha"] != report_digest
        ):
            raise RuntimeError(
                "Scorer recovery finalizer did not produce a verified stopped report"
            )
        result = {
            **verified,
            "action": "finalized_stopped",
            "report_sha256": report_digest,
            "finalizer_unit": finalized.unit,
            "finalizer_exit_code": finalized.exit_code,
        }
        if canceled_budget_audit is not None:
            result["cancelled_model_budget_audit"] = canceled_budget_audit
        return _persist_receipt(
            director,
            recovery_id=recovery_id,
            run_id=run_id,
            request_sha256=request_sha,
            observed_state="stop_requested",
            owner=owner,
            state="completed",
            result=result,
            execution_owner=execution_owner,
        )
    finally:
        lease.__exit__(None, None, None)


def _child_blockers(planner: Engine, run_id: UUID, *, requires_gpu: bool) -> list[str]:
    blockers: list[str] = []
    with planner.connect() as connection:
        active_jobs = connection.execute(
            text(
                "SELECT count(*) FROM scorer.score_jobs WHERE run_id=:id "
                "AND state IN ('queued','running')"
            ),
            {"id": run_id},
        ).scalar_one()
    if active_jobs:
        blockers.append("active_score_jobs")
    runtime_db_value = os.environ.get("SWAPP_GPU_RUNTIME_DB")
    if requires_gpu and not runtime_db_value:
        blockers.append("gpu_runtime_state_unavailable")
    if runtime_db_value:
        import sqlite3

        path = Path(runtime_db_value)
        if path.is_symlink() or not path.is_file():
            blockers.append("gpu_runtime_state_unavailable")
        else:
            try:
                with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2) as runtime_db:
                    rows = runtime_db.execute(
                        "SELECT state FROM gpu_turn_requests WHERE owner='lab' "
                        "AND state IN ('queued','active')"
                    ).fetchall()
                if rows:
                    blockers.append("lab_gpu_turns_active")
            except sqlite3.Error:
                blockers.append("gpu_runtime_state_unavailable")
    # Docker's candidate phase has a project-specific label. Any remaining
    # running labelled container blocks recovery; we never remove it here.
    try:
        completed = subprocess.run(  # nosec B603 -- fixed docker inspect query
            [
                "/usr/bin/docker",
                "container",
                "ls",
                "--quiet",
                "--filter=label=swapp.ai-scientist.sandbox.owner",
            ],
            capture_output=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        blockers.append("sandbox_state_unavailable")
    else:
        if completed.returncode != 0:
            blockers.append("sandbox_state_unavailable")
        elif completed.stdout.strip():
            blockers.append("sandbox_containers_active")
    return blockers


def _insert_event(
    connection: Any, run_id: UUID, recovery_id: UUID, event_type: str, payload: dict[str, Any]
) -> None:
    event_id = uuid5(RECOVERY_EVENT_NAMESPACE, f"{run_id}:{recovery_id}:{event_type}")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    existing = (
        connection.execute(
            text("SELECT event_type,event_json FROM lab.run_events WHERE event_id=:id"),
            {"id": event_id},
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        if existing["event_type"] != event_type or existing["event_json"] != payload:
            raise ValueError("recovery event identity conflicts with durable event")
        return
    connection.execute(
        text(
            "INSERT INTO lab.run_events(event_id,run_id,event_type,event_json,created_at) "
            "VALUES (:event_id,:run_id,:event_type,CAST(:payload AS jsonb),now())"
        ),
        {"event_id": event_id, "run_id": run_id, "event_type": event_type, "payload": encoded},
    )
