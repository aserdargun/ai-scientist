"""Exact stopped primary-admission closure; preserve partial native evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import Engine, text

from lab.director import recovery
from lab.director.artifacts import (
    read_director_artifact,
    read_registered_calibration,
    store_director_artifact,
)
from lab.director.baselines import calibration_sha256
from lab.director.contracts import (
    AttemptedProposalStopReceipt,
    ExperimentDocument,
    TrajectoryDocument,
)
from lab.director.fake_llm import (
    AgentContext,
    ProposalTurn,
    prompt_context_sha256,
    prompt_messages_sha256,
)
from lab.director.journal import DirectorRunLease, canonical_bytes
from lab.director.ledger import canonical_json_bytes, commit_experiment_record
from lab.director.ownership import ExecutionOwner, owned_execution
from lab.director.resume import _drain_director_sandbox, _prove_terminal_jobs
from lab.director.runner import _candidate_git_tree, _stored_turn
from lab.director.stop_closure import prove_stopped_owner_dead, remaining_stop_closure_seconds
from lab.director.suite_manifest import SuiteManifest
from lab.scorer.holdout_supervisor import run_stopped_director_recovery_process
from lab.scorer.recovery_identity import verify_attempted_stop_recovery_retirement

_NAMESPACE = UUID("0fac846e-d8f8-4c99-8998-d70967764392")


def _proposal_documents(
    *,
    run_id: UUID,
    row: dict[str, Any],
    payload: dict[str, Any],
    turn: ProposalTurn,
    execution: dict[str, Any],
    stop: AttemptedProposalStopReceipt,
    suite_manifest: bytes,
    artifact_root: Path,
) -> tuple[ExperimentDocument, TrajectoryDocument]:
    """Validate original artifacts/provenance; never infer measurements from budget charges."""
    source = read_director_artifact(row["candidate_blob_sha256"], artifact_root=artifact_root)
    context_raw = read_director_artifact(row["inputs_sha256"], artifact_root=artifact_root)
    context = AgentContext.model_validate_json(context_raw, strict=True)
    messages = read_director_artifact(payload["messages_blob_sha256"], artifact_root=artifact_root)
    if (
        row["kind"] != "proposal"
        or row["status"] not in {"primary_running", "abandoned"}
        or row["experiment_id"] != stop.experiment_id
        or stop.run_id != run_id
        or hashlib.sha256(canonical_bytes(payload)).hexdigest() != stop.proposal_sha256
        or payload["experiment_id"] != row["experiment_id"]
        or payload["run_id"] != str(run_id)
        or payload["experiment_number"] != row["experiment_number"]
        or payload["sequence"] != row["sequence"]
        or payload["inputs_sha256"] != row["inputs_sha256"]
        or payload["calibration_sha256"] != row["calibration_sha256"]
        or row["calibration_sha256"] != stop.calibration_sha256
        or payload["candidate_sha256"] != row["candidate_sha256"]
        or hashlib.sha256(source).hexdigest() != row["candidate_sha256"]
        or source != turn.proposal.candidate_source.encode()
        or payload["parent_experiment_id"] != row["parent_experiment_id"]
        or prompt_context_sha256(context) != row["inputs_sha256"]
        or canonical_bytes(context.model_dump(mode="json")) != context_raw
        or json.loads(messages).get("messages") != list(turn.messages)
        or payload["input_tokens"] != turn.input_tokens
        or payload["output_tokens"] != turn.output_tokens
        or payload.get("provider_receipt")
        != (turn.provider_receipt.model_dump(mode="json") if turn.provider_receipt else None)
        or any(
            payload[key] != execution[key]
            for key in ("harness_sha256", "image_sha256", "suite_id", "suite_version")
        )
        or any(
            row["proposal_json"].get(key) != value
            for key, value in turn.proposal.model_dump(mode="json").items()
        )
    ):
        raise ValueError("stopped proposal differs from its original registered artifacts")
    provider = execution["provider"]
    receipt = turn.provider_receipt
    if provider == "local-qwen":
        if (
            receipt is None
            or receipt.context_sha256 != prompt_context_sha256(context)
            or receipt.provider_config_sha256 != execution["provider_config_sha256"]
            or receipt.provider_registry_entry_sha256
            != execution["request"]["provider_registry_entry_sha256"]
            or receipt.requested_system != payload["system"]
            or receipt.requested_system != context.system
            or receipt.input_tokens != turn.input_tokens
            or receipt.output_tokens != turn.output_tokens
            or len(turn.messages) < 3
            or receipt.prompt_sha256
            != prompt_messages_sha256(
                [
                    {"role": "system", "content": turn.messages[0]},
                    {"role": "user", "content": turn.messages[1]},
                ]
            )
            or not turn.provider_attempts
            or receipt.attempts[-1].prompt_sha256 != receipt.prompt_sha256
            or turn.provider_attempts[-1].prompt_sha256 != receipt.prompt_sha256
            or turn.provider_attempts[-1].response_schema_sha256
            != receipt.attempts[-1].response_schema_sha256
            or turn.provider_attempts[-1].response_sha256 != receipt.attempts[-1].response_sha256
            or turn.provider_attempts[-1].response_sha256
            != hashlib.sha256(turn.messages[-1].encode()).hexdigest()
        ):
            raise ValueError("stopped proposal local provider provenance changed")
        for transcript in turn.provider_attempts:
            matches = [
                attempt
                for attempt in receipt.attempts
                if attempt.prompt_sha256 == transcript.prompt_sha256
                and attempt.response_sha256 == transcript.response_sha256
            ]
            if (
                len(matches) != 1
                or matches[0].response_schema_sha256 != transcript.response_schema_sha256
                or hashlib.sha256(transcript.response_text.encode()).hexdigest()
                != transcript.response_sha256
                or prompt_messages_sha256(
                    [item.model_dump(mode="json") for item in transcript.prompt_messages]
                )
                != transcript.prompt_sha256
            ):
                raise ValueError("stopped proposal attempt lacks its original host receipt")
    elif provider == "mode-grid":
        if (
            receipt is not None
            or turn.input_tokens
            or turn.output_tokens
            or payload.get("deterministic_provider_id") != "operating-mode-grid.v1"
            or payload.get("provider_config_sha256") != execution["scenario_sha256"]
            or payload.get("provider_registry_entry_sha256")
            != execution["request"]["provider_registry_entry_sha256"]
        ):
            raise ValueError("stopped proposal grid provenance changed")
    elif (
        provider != "fake-json"
        or receipt is not None
        or payload.get("deterministic_provider_id") is not None
    ):
        raise ValueError("unsupported stopped proposal provider provenance")
    if hashlib.sha256(suite_manifest).hexdigest() != execution["suite_manifest_sha256"]:
        raise ValueError("stopped proposal original suite changed")
    manifest = SuiteManifest.model_validate_json(suite_manifest)
    if (
        manifest.suite_id != execution["suite_id"]
        or manifest.suite_version != execution["suite_version"]
    ):
        raise ValueError("stopped proposal original suite identity changed")
    shared = dict(
        run_id=run_id,
        experiment_id=row["experiment_id"],
        kind="proposal",
        experiment_number=row["experiment_number"],
        baseline_name=None,
        calibration_sha256=stop.calibration_sha256,
        agent_version="director.attempted-stop.v0.37",
        inputs_sha256=row["inputs_sha256"],
        infrastructure_stop=stop,
    )
    experiment = ExperimentDocument.model_validate(
        {
            **shared,
            "schema": "experiment.v1",
            "ordinal": row["sequence"] + 1,
            "parent_experiment_id": row["parent_experiment_id"],
            "candidate_sha256": row["candidate_sha256"],
            "candidate_blob_sha256": row["candidate_blob_sha256"],
            "move_type": turn.proposal.move_type,
            "system": payload["system"],
            "hypothesis": turn.proposal.hypothesis,
            "predicted_delta": turn.proposal.predicted_delta,
            "parent_tree": payload["parent_tree_sha256"],
            "child_tree": _candidate_git_tree(source),
            "harness_sha256": execution["harness_sha256"],
            "image_sha256": execution["image_sha256"],
            "suite_id": execution["suite_id"],
            "suite_version": execution["suite_version"],
            "per_task": (),
            "suite_score": None,
            "guards": {"hardcoding": "not_run", "determinism": "not_run", "causality": "not_run"},
            "decision": None,
            "status": "abandoned",
            "fit_seconds": None,
            "score_seconds": None,
            "llm_input_tokens": turn.input_tokens,
            "llm_output_tokens": turn.output_tokens,
            "wall_seconds": None,
            "provider_receipt_sha256": (
                hashlib.sha256(canonical_bytes(receipt.model_dump(mode="json"))).hexdigest()
                if receipt
                else None
            ),
        },
        strict=True,
    )
    trajectory = TrajectoryDocument.model_validate(
        {
            **shared,
            "schema": "trajectory.v1",
            "trajectory_id": "trj_" + uuid5(_NAMESPACE, "trajectory:" + row["experiment_id"]).hex,
            "model_id": (
                receipt.model_id
                if receipt
                else payload.get("deterministic_provider_id") or "fake_llm.deterministic.v1"
            ),
            "usage_profile": "noncommercial_research",
            "source_provenance": tuple(dict.fromkeys(task.provenance for task in manifest.tasks)),
            "quantization": "fp8_per_tensor" if receipt else "none",
            "adapter": (
                "vllm-local"
                if receipt
                else "parameter-grid"
                if provider == "mode-grid"
                else "fake-provider"
            ),
            "system": payload["system"],
            "thinking": receipt.enable_thinking if receipt else False,
            "temperature": receipt.sampling_temperature if receipt else 0.0,
            "top_p": receipt.sampling_top_p if receipt else 1.0,
            "context_template": receipt.context_template
            if receipt
            else "director.metadata-only.v1",
            "messages_blob_sha256": payload["messages_blob_sha256"],
            "tool_calls": 0,
            "outcome": None,
            "quality_tier": "bronze",
            "secrets_scrubbed": False,
            "people_scrubbed": False,
            "raw_values_scrubbed": False,
            "provider_receipt": receipt.model_dump(mode="json") if receipt else None,
            "exclusions": (
                "Infrastructure stop during primary evaluation; no candidate quality verdict.",
                "Partial candidate measurements remain in the native inventory ledger; "
                "original provider and budget checkpoints "
                "retained unchanged.",
            ),
        },
        strict=True,
    )
    return experiment, trajectory


def stop_envelope(
    proposal: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    owner: ExecutionOwner,
    primary_plan: list[dict[str, Any]],
) -> dict[str, Any]:
    """Freeze exact native rows without copying claim authority into caller context."""
    if not 1 <= len(rows) <= 1024 or proposal.get("run_id") != str(owner.run_id):
        raise recovery.RecoveryPending("attempted stop requires bounded native candidate admission")
    if not 1 <= len(primary_plan) <= 1024:
        raise recovery.RecoveryPending("original primary plan is unavailable or unbounded")
    keys = set()
    for task in primary_plan:
        key = (task.get("task_id"), task.get("seed"))
        if (
            task.get("run_id") != str(owner.run_id)
            or task.get("experiment_id") != proposal.get("experiment_id")
            or task.get("evaluation_kind") != "primary"
            or task.get("seed") != 0
            or not isinstance(task.get("task_id"), str)
            or key in keys
        ):
            raise recovery.RecoveryPending("original primary plan differs from exact admission")
        keys.add(key)
    identifiers: set[str] = set()
    for row in rows:
        identity = str(row.get("job_id"))
        if (
            identity in identifiers
            or (row.get("task_id"), row.get("seed")) not in keys
            or row.get("run_id") != str(owner.run_id)
            or row.get("experiment_id") != proposal.get("experiment_id")
            or row.get("candidate_sha256") != proposal.get("candidate_sha256")
            or row.get("evaluation_kind") != "primary"
            or row.get("seed") != 0
            or row.get("execution_sha256") != owner.execution_sha256
            or row.get("state") not in {"queued", "running", "completed", "failed"}
            or not isinstance(row.get("admitted_generation"), int)
            or not 1 <= row["admitted_generation"] <= owner.generation
            or (
                row["state"] in {"queued", "running"}
                and row["admitted_generation"] != owner.generation
            )
        ):
            raise recovery.RecoveryPending("candidate inventory differs from exact primary owner")
        UUID(identity)
        identifiers.add(identity)
    return {
        "schema": "attempted-proposal-stop.v2",
        "proposal_text": canonical_bytes(proposal).decode(),
        "admitted_inventory_text": json.dumps(
            sorted(rows, key=lambda value: value["job_id"]),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ),
        "original_primary_plan_text": _raw_text(
            sorted(primary_plan, key=lambda t: (t["task_id"], t["seed"]))
        ),
        "expected_owner": {
            "generation": owner.generation,
            "invocation_id": owner.invocation_id,
            "execution_sha256": owner.execution_sha256,
        },
    }


def _raw_text(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _load_marker(director: Engine, recovery_id: UUID) -> dict[str, Any] | None:
    with director.connect() as connection:
        value = (
            connection.execute(
                text(
                    "SELECT p.*,a.state AS closure_state,a.expected_generation,a.execution_sha256,"
                    "a.owner_json AS closure_owner_json,a.created_at AS closure_created_at "
                    "FROM lab.director_stopped_proposals p JOIN lab.director_stop_closures a "
                    "USING(recovery_id) WHERE p.recovery_id=:id"
                ),
                {"id": recovery_id},
            )
            .mappings()
            .one_or_none()
        )
    return dict(value) if value is not None else None


def _validate_marker(
    marker: dict[str, Any],
    *,
    envelope_text: str,
    primary_plan: list[dict[str, Any]],
    owner: ExecutionOwner,
    process_owner: recovery.OwnerGeneration,
) -> None:
    if (
        marker.get("stop_mode") != "primary_admitted"
        or marker.get("stop_protocol_version") != 2
        or marker.get("expected_generation") != owner.generation
        or marker.get("execution_sha256") != owner.execution_sha256
        or marker.get("closure_owner_json") != asdict(process_owner)
    ):
        raise recovery.RecoveryPending("attempted-stop v2 original closure owner differs")
    if marker.get("stop_envelope_sha256") != hashlib.sha256(envelope_text.encode()).hexdigest():
        raise recovery.RecoveryPending("attempted-stop frozen inventory hash changed")
    for prefix in (
        "admitted_inventory",
        "original_primary_plan",
        "child_inventory",
        "missing_primary_inventory",
        "terminal_inventory",
    ):
        raw, digest = marker.get(prefix + "_text"), marker.get(prefix + "_sha256")
        if (
            raw is None
            and digest is None
            and prefix not in {"admitted_inventory", "original_primary_plan"}
        ):
            continue
        if not isinstance(raw, str) or hashlib.sha256(raw.encode()).hexdigest() != digest:
            raise recovery.RecoveryPending(
                "attempted-stop frozen or terminal inventory hash changed"
            )
    try:
        plan = json.loads(marker["original_primary_plan_text"])
    except (ValueError, TypeError) as exc:
        raise recovery.RecoveryPending("original primary plan is unreadable") from exc
    if plan != sorted(primary_plan, key=lambda t: (t["task_id"], t["seed"])):
        raise recovery.RecoveryPending("original primary plan changed")
    if marker.get("closure_state") == "drained" and any(
        marker.get(prefix + "_text") is None
        for prefix in ("child_inventory", "missing_primary_inventory", "terminal_inventory")
    ):
        raise recovery.RecoveryPending("attempted-stop drained final seal is incomplete")

    try:
        child = (
            json.loads(marker["child_inventory_text"])
            if marker["child_inventory_text"] is not None
            else None
        )
        final = (
            json.loads(marker["terminal_inventory_text"])
            if marker["terminal_inventory_text"] is not None
            else None
        )
        missing = (
            json.loads(marker["missing_primary_inventory_text"])
            if marker["missing_primary_inventory_text"] is not None
            else None
        )
        roster = marker["recovery_roster_json"]
        if child is not None and (
            not isinstance(child, dict)
            or child.get("schema") != "attempted-stop-children.v2"
            or child.get("original_primary_plan_sha256") != marker["original_primary_plan_sha256"]
            or child.get("original_primary_plan") != plan
            or child.get("admitted_inventory_sha256") != marker["admitted_inventory_sha256"]
            or not isinstance(child.get("admitted_terminal"), dict)
            or child["recovery_roster_json"]["attempts"] != roster["attempts"]
            or not all(
                item in roster["retirements"]
                for item in child["recovery_roster_json"]["retirements"]
            )
        ):
            raise recovery.RecoveryPending("attempted-stop child seal binding changed")
        if missing is not None and (
            not isinstance(missing, dict)
            or missing.get("schema") != "attempted-stop-missing.v2"
            or missing.get("original_primary_plan_sha256") != marker["original_primary_plan_sha256"]
            or not isinstance(missing.get("outcomes"), list)
        ):
            raise recovery.RecoveryPending("attempted-stop missing primary seal binding changed")
        if final is not None and (
            not isinstance(final, dict)
            or final.get("schema") != "attempted-stop-terminal.v2"
            or final.get("original_primary_plan_sha256") != marker["original_primary_plan_sha256"]
            or final.get("original_primary_plan") != plan
            or final.get("child_inventory_sha256") != marker["child_inventory_sha256"]
            or final.get("missing_primary_inventory") != missing
            or final.get("recovery_roster_json") != roster
            or not isinstance(final.get("terminal_inventory"), dict)
        ):
            raise recovery.RecoveryPending("attempted-stop final seal binding changed")
    except (ValueError, TypeError, KeyError) as exc:
        raise recovery.RecoveryPending("attempted-stop native seal metadata unavailable") from exc


_WORKER_FIELDS = (
    "worker_pid",
    "worker_start_ticks",
    "worker_boot_id",
    "worker_unit",
    "worker_invocation_id",
    "worker_cgroup",
)


def _retire_registered_workers(
    director: Engine,
    *,
    marker: dict[str, Any],
    recovery_id: UUID,
    execution_owner: ExecutionOwner,
    deadline_at: datetime,
    final_replay: bool,
) -> None:
    roster = marker.get("recovery_roster_json")
    if not isinstance(roster, dict) or set(roster) != {"attempts", "retirements"}:
        raise recovery.RecoveryPending("recovery worker retirement roster unavailable")
    attempts, retirements = roster["attempts"], roster["retirements"]
    if (
        not isinstance(attempts, list)
        or not isinstance(retirements, list)
        or len(attempts) > 32
        or ((final_replay or marker.get("child_inventory_text") is not None) and not attempts)
    ):
        raise recovery.RecoveryPending("recovery worker retirement roster invalid")
    if any(
        not isinstance(item, dict)
        or set(item) != {"retired_ordinal", "identity_sha256", "observation_sha256"}
        or type(item.get("retired_ordinal")) is not int
        or any(
            not isinstance(item.get(key), str) or re.fullmatch(r"[0-9a-f]{64}", item[key]) is None
            for key in ("identity_sha256", "observation_sha256")
        )
        for item in retirements
    ):
        raise recovery.RecoveryPending("recovery worker retirement proof malformed")
    ordinals = {item.get("retired_ordinal") for item in retirements if isinstance(item, dict)}
    if len(ordinals) != len(retirements) or not ordinals.issubset(set(range(1, len(attempts) + 1))):
        raise recovery.RecoveryPending("recovery worker retirement ancestry differs")
    for ordinal, attempt in enumerate(attempts, 1):
        if (
            not isinstance(attempt, dict)
            or attempt.get("attempt_ordinal") != ordinal
            or attempt.get("expected_generation") != execution_owner.generation
            or attempt.get("execution_sha256") != execution_owner.execution_sha256
            or attempt.get("original_primary_plan_sha256") != marker["original_primary_plan_sha256"]
            or any(key not in attempt for key in _WORKER_FIELDS)
        ):
            raise recovery.RecoveryPending("recovery worker retirement owner or plan differs")
        if final_replay and ordinal not in ordinals:
            raise recovery.RecoveryPending("recovery worker retirement proof missing")
        identity = {key: attempt[key] for key in _WORKER_FIELDS}
        identity_text, identity_digest = (
            attempt.get("identity_text"),
            attempt.get("identity_sha256"),
        )
        try:
            bound_identity = json.loads(identity_text) if isinstance(identity_text, str) else None
        except (ValueError, TypeError) as exc:
            raise recovery.RecoveryPending(
                "recovery worker retirement identity is unreadable"
            ) from exc
        if (
            bound_identity != identity
            or not isinstance(identity_text, str)
            or hashlib.sha256(identity_text.encode()).hexdigest() != identity_digest
            or any(
                item["identity_sha256"] != identity_digest
                for item in retirements
                if item["retired_ordinal"] == ordinal
            )
        ):
            raise recovery.RecoveryPending("recovery worker retirement identity hash differs")
        try:
            observation = verify_attempted_stop_recovery_retirement(identity)
        except RuntimeError as exc:
            raise recovery.RecoveryPending("exact recovery worker retirement unproven") from exc
        _current_deadline(director, owner=execution_owner, cleanup_deadline=deadline_at)
        if not final_replay and ordinal not in ordinals:
            envelope = {
                "schema": "attempted-stop-retirement.v2",
                "action": "retire",
                "expected_owner": {
                    "generation": execution_owner.generation,
                    "invocation_id": execution_owner.invocation_id,
                    "execution_sha256": execution_owner.execution_sha256,
                },
                "worker_identity": identity,
                "observation_text": _raw_text(observation),
            }
            with director.begin() as connection:
                connection.execute(
                    text("SELECT lab.begin_stopped_proposal_closure(:id,:proposal)"),
                    {"id": recovery_id, "proposal": _raw_text(envelope)},
                ).scalar_one()


def _current_deadline(
    director: Engine, *, owner: ExecutionOwner, cleanup_deadline: datetime | None
) -> datetime:
    with director.connect() as connection:
        target = (
            connection.execute(
                text(
                    "SELECT r.state,r.stop_requested,c.mode,c.current_generation,"
                    "g.worker_invocation_id,g.execution_sha256,e.deadline_at,"
                    "(SELECT min(created_at) FROM lab.run_events WHERE run_id=r.run_id "
                    "AND event_type='run.stop_requested') AS first_stop_at "
                    "FROM lab.runs r JOIN lab.director_execution_control c USING(run_id) "
                    "JOIN lab.director_owner_generations g ON g.run_id=c.run_id "
                    "AND g.generation=c.current_generation JOIN lab.director_execution_contracts e "
                    "ON e.run_id=r.run_id WHERE r.run_id=:run"
                ),
                {"run": owner.run_id},
            )
            .mappings()
            .one()
        )
    if (
        target["state"] != "stop_requested"
        or not target["stop_requested"]
        or target["mode"] != "active"
        or target["current_generation"] != owner.generation
        or target["worker_invocation_id"] != owner.invocation_id
        or target["execution_sha256"] != owner.execution_sha256
    ):
        raise recovery.RecoveryPending("attempted stop current owner changed")
    first_stop, execution_deadline = target["first_stop_at"], target["deadline_at"]
    if not isinstance(first_stop, datetime) or first_stop.tzinfo is None:
        raise recovery.RecoveryPending("original first-stop deadline unavailable")
    if not isinstance(execution_deadline, datetime) or execution_deadline.tzinfo is None:
        raise recovery.RecoveryPending("original execution deadline unavailable")
    deadline = min(execution_deadline, first_stop + timedelta(seconds=120))
    if cleanup_deadline is not None:
        if cleanup_deadline.tzinfo is None:
            raise ValueError("cleanup deadline must be timezone aware")
        deadline = min(deadline, cleanup_deadline)
    if deadline <= datetime.now(UTC):
        raise recovery.RecoveryPending("original attempted-stop deadline exhausted")
    return deadline


def reconcile_stopped_attempted_proposal(
    director: Engine,
    planner: Engine,
    *,
    run_id: UUID,
    recovery_id: UUID,
    owner: recovery.OwnerGeneration,
    execution_owner: ExecutionOwner,
    lease: DirectorRunLease,
    artifact_root: Path,
    deadline_at: datetime | None = None,
) -> None:
    """Only native exact primary-seed-zero shape; SQL independently fences every write."""
    if (
        execution_owner.run_id != run_id
        or owner.worker_invocation_id != execution_owner.invocation_id
    ):
        raise recovery.RecoveryPending("attempted stop process/execution owner differs")
    deadline_at = _current_deadline(director, owner=execution_owner, cleanup_deadline=deadline_at)
    with director.connect() as connection:
        execution = connection.execute(
            text("SELECT execution_json FROM lab.director_execution_contracts WHERE run_id=:run"),
            {"run": run_id},
        ).scalar_one()
        proposals = (
            connection.execute(
                text(
                    "SELECT * FROM lab.experiments WHERE run_id=:run AND kind='proposal' "
                    "ORDER BY sequence LIMIT 2"
                ),
                {"run": run_id},
            )
            .mappings()
            .all()
        )
    marker = _load_marker(director, recovery_id)
    if len(proposals) != 1 or proposals[0]["status"] not in {"primary_running", "abandoned"}:
        raise recovery.RecoveryPending("attempted stop requires one primary-running proposal")
    row = dict(proposals[0])
    calibration = read_registered_calibration(director, run_id=run_id, artifact_root=artifact_root)
    if calibration_sha256(calibration) != row["calibration_sha256"]:
        raise recovery.RecoveryPending("frozen baseline calibration changed")
    stored = _stored_turn(
        run_id=run_id, ordinal=row["experiment_number"], lease=lease, artifact_root=artifact_root
    )
    if stored is None:
        raise recovery.RecoveryPending("original registered proposal checkpoint unavailable")
    turn, payload = stored
    with planner.connect() as connection:
        primary_plan = list(
            connection.execute(
                text(
                    "SELECT to_jsonb(t) FROM scorer.run_tasks t WHERE t.run_id=:run "
                    "AND t.experiment_id=:experiment ORDER BY t.task_id,t.seed"
                ),
                {"run": run_id, "experiment": row["experiment_id"]},
            )
            .scalars()
            .all()
        )
    if marker is None:
        with director.connect() as connection:
            candidate_rows = (
                connection.execute(
                    text(
                        "SELECT to_jsonb(j) FROM scorer.score_jobs j "
                        "WHERE j.run_id=:run AND j.experiment_id=:experiment ORDER BY j.job_id"
                    ),
                    {"run": run_id, "experiment": row["experiment_id"]},
                )
                .scalars()
                .all()
            )
        envelope_text = _raw_text(
            stop_envelope(
                payload, list(candidate_rows), owner=execution_owner, primary_plan=primary_plan
            )
        )
    else:
        if marker["stop_mode"] != "primary_admitted" or marker.get("stop_protocol_version") != 2:
            raise recovery.RecoveryPending("recovery ID already belongs to another stop shape")
        # Replay the exact original admission snapshot, not naturally changing post-drain rows.
        envelope_text = _raw_text(
            {
                "schema": "attempted-proposal-stop.v2",
                "proposal_text": canonical_bytes(payload).decode(),
                "admitted_inventory_text": marker["admitted_inventory_text"],
                "original_primary_plan_text": marker["original_primary_plan_text"],
                "expected_owner": {
                    "generation": execution_owner.generation,
                    "invocation_id": execution_owner.invocation_id,
                    "execution_sha256": execution_owner.execution_sha256,
                },
            }
        )
        _validate_marker(
            marker,
            envelope_text=envelope_text,
            primary_plan=primary_plan,
            owner=execution_owner,
            process_owner=owner,
        )
        deadline_at = min(deadline_at, marker["closure_created_at"] + timedelta(seconds=120))
    if not prove_stopped_owner_dead(owner, run_id):
        raise recovery.RecoveryPending("current attempted-stop Director remains alive")
    _current_deadline(director, owner=execution_owner, cleanup_deadline=deadline_at)
    final_replay = marker is not None and marker["closure_state"] == "drained"
    if final_replay:
        remaining_seconds = remaining_stop_closure_seconds(director, recovery_id)
        if remaining_seconds is None:
            raise recovery.RecoveryPending("original attempted-stop closure unavailable")
        seconds = float(remaining_seconds)
    else:
        with director.begin() as connection:
            seconds = float(
                connection.execute(
                    text("SELECT lab.begin_stopped_proposal_closure(:id,:proposal)"),
                    {"id": recovery_id, "proposal": envelope_text},
                ).scalar_one()
            )
    deadline = time.monotonic() + min(seconds, (deadline_at - datetime.now(UTC)).total_seconds())
    if time.monotonic() >= deadline:
        raise recovery.RecoveryPending("immutable attempted-stop closure window exhausted")
    marker = _load_marker(director, recovery_id)
    if marker is None:
        raise recovery.RecoveryPending("attempted-stop marker unavailable")
    _validate_marker(
        marker,
        envelope_text=envelope_text,
        primary_plan=primary_plan,
        owner=execution_owner,
        process_owner=owner,
    )
    _retire_registered_workers(
        director,
        marker=marker,
        recovery_id=recovery_id,
        execution_owner=execution_owner,
        deadline_at=deadline_at,
        final_replay=final_replay,
    )
    _drain_director_sandbox(run_id, asdict(owner), artifact_root, deadline)
    if not final_replay:
        if marker["child_inventory_text"] is None:
            remaining = int(deadline - time.monotonic())
            if remaining < 1:
                raise recovery.RecoveryPending(
                    "attempted-stop sandbox drain exhausted original deadline"
                )
            result = run_stopped_director_recovery_process(recovery_id, remaining_seconds=remaining)
            if result.state != "children_drained" or result.exit_code != 0:
                raise recovery.RecoveryPending(
                    "attempted-stop exact Scorer inventory remains undrained"
                )
            marker = _load_marker(director, recovery_id)
            if marker is None or marker.get("child_inventory_text") is None:
                raise recovery.RecoveryPending("attempted-stop child seal unavailable")
            _validate_marker(
                marker,
                envelope_text=envelope_text,
                primary_plan=primary_plan,
                owner=execution_owner,
                process_owner=owner,
            )
            _retire_registered_workers(
                director,
                marker=marker,
                recovery_id=recovery_id,
                execution_owner=execution_owner,
                deadline_at=deadline_at,
                final_replay=False,
            )
        _prove_terminal_jobs(planner, run_id, deadline)
        if not prove_stopped_owner_dead(owner, run_id):
            raise recovery.RecoveryPending("attempted-stop Director death proof changed")
        _current_deadline(director, owner=execution_owner, cleanup_deadline=deadline_at)
        with planner.begin() as connection:
            connection.execute(
                text("SELECT lab.close_stopped_unattempted_tasks(:id,:experiment)"),
                {"id": recovery_id, "experiment": row["experiment_id"]},
            ).scalar_one()
    _prove_terminal_jobs(planner, run_id, deadline)
    if not prove_stopped_owner_dead(owner, run_id):
        raise recovery.RecoveryPending("attempted-stop Director death proof changed")
    _current_deadline(director, owner=execution_owner, cleanup_deadline=deadline_at)
    marker = _load_marker(director, recovery_id)
    if marker is None or marker["closure_state"] != "drained":
        raise recovery.RecoveryPending("attempted-stop final closure remains pending")
    _validate_marker(
        marker,
        envelope_text=envelope_text,
        primary_plan=primary_plan,
        owner=execution_owner,
        process_owner=owner,
    )
    _retire_registered_workers(
        director,
        marker=marker,
        recovery_id=recovery_id,
        execution_owner=execution_owner,
        deadline_at=deadline_at,
        final_replay=True,
    )
    stop = AttemptedProposalStopReceipt(
        reason="stopped_during_primary_evaluation",
        recovery_id=recovery_id,
        run_id=run_id,
        experiment_id=row["experiment_id"],
        generation=execution_owner.generation,
        execution_sha256=execution_owner.execution_sha256,
        proposal_sha256=marker["proposal_sha256"],
        reservation_sha256=marker["reservation_sha256"],
        reconciled_sha256=marker["reconciled_sha256"],
        calibration_sha256=row["calibration_sha256"],
        admitted_inventory_sha256=marker["admitted_inventory_sha256"],
        terminal_inventory_sha256=marker["terminal_inventory_sha256"],
    )
    from lab.api.registry import load_suite_registry
    from lab.sandbox.docker_runner import PROJECT_ROOT

    registry_path = os.environ.get("LAB_SUITE_REGISTRY_FILE")
    if not registry_path:
        raise recovery.RecoveryPending("original registered suite unavailable")
    registry = load_suite_registry(Path(registry_path), PROJECT_ROOT / "data/runtime")
    entry = registry.get(execution["suite_id"])
    if registry.entry_sha256(entry) != execution["registry_entry_sha256"]:
        raise recovery.RecoveryPending("original suite registration changed")
    suite_path, _ = registry.verify_entry(entry)
    experiment, trajectory = _proposal_documents(
        run_id=run_id,
        row=row,
        payload=payload,
        turn=turn,
        execution=execution,
        stop=stop,
        suite_manifest=suite_path.read_bytes(),
        artifact_root=artifact_root,
    )
    commit_seconds = remaining_stop_closure_seconds(director, recovery_id)
    if commit_seconds is None or commit_seconds < 1 or time.monotonic() >= deadline:
        raise recovery.RecoveryPending(
            "attempted-stop original deadline expired before ledger commit"
        )
    with owned_execution(execution_owner):
        commit_experiment_record(
            director,
            experiment=experiment,
            trajectory=trajectory,
            experiment_blob_sha256=store_director_artifact(
                canonical_json_bytes(experiment), artifact_root=artifact_root
            ),
            trajectory_blob_sha256=store_director_artifact(
                canonical_json_bytes(trajectory), artifact_root=artifact_root
            ),
        )
    lease.heartbeat()
