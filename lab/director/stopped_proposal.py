"""Opt-in stop closure for one calibrated proposal with zero candidate admission."""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict
from datetime import UTC, datetime
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
    ExperimentDocument,
    InfrastructureStopReceipt,
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

_NAMESPACE = UUID("0fac846e-d8f8-4c99-8998-d70967764392")


def _proposal_documents(
    *,
    run_id: UUID,
    row: dict[str, Any],
    payload: dict[str, Any],
    turn: ProposalTurn,
    execution: dict[str, Any],
    stop: InfrastructureStopReceipt,
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
        or row["status"] not in {"proposed", "abandoned"}
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
        agent_version="director.stop.v0.36.3",
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
                "Infrastructure stop before candidate admission; no candidate quality verdict.",
                "Candidate timings unavailable; original provider and budget checkpoints "
                "retained unchanged.",
            ),
        },
        strict=True,
    )
    return experiment, trajectory


def reconcile_stopped_proposal(
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
    """Close this exact no-candidate-job shape after validation, under one fixed window."""
    with director.connect() as connection:
        execution = connection.execute(
            text("SELECT execution_json FROM lab.director_execution_contracts WHERE run_id=:run"),
            {"run": run_id},
        ).scalar_one()
        rows = (
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
    if len(rows) != 1 or rows[0]["status"] not in {"proposed", "abandoned"}:
        raise recovery.RecoveryPending("stop closure requires one unattempted registered proposal")
    row = dict(rows[0])
    calibration = read_registered_calibration(director, run_id=run_id, artifact_root=artifact_root)
    if calibration_sha256(calibration) != row["calibration_sha256"]:
        raise recovery.RecoveryPending("original calibration artifact changed")
    stored = _stored_turn(
        run_id=run_id, ordinal=row["experiment_number"], lease=lease, artifact_root=artifact_root
    )
    if stored is None:
        raise recovery.RecoveryPending("original proposal checkpoint unavailable")
    turn, payload = stored
    proposal_sha = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    reservation = lease.read_checkpoint(
        key=f"proposal-budget-reservation:{run_id}:{row['experiment_number']}",
        artifact_root=artifact_root,
    )
    reconciled = lease.read_checkpoint(
        key=f"proposal-budget-reconciled:{run_id}:{row['experiment_number']}",
        artifact_root=artifact_root,
    )
    if reservation is None:
        raise recovery.RecoveryPending("original proposal reservation unavailable")
    stop = InfrastructureStopReceipt(
        reason="stopped_before_candidate_admission",
        recovery_id=recovery_id,
        run_id=run_id,
        experiment_id=row["experiment_id"],
        generation=execution_owner.generation,
        execution_sha256=execution_owner.execution_sha256,
        proposal_sha256=proposal_sha,
        reservation_sha256=reservation["receipt"]["payload_sha256"],
        reconciled_sha256=reconciled["receipt"]["payload_sha256"] if reconciled else None,
        calibration_sha256=row["calibration_sha256"],
    )
    from lab.api.registry import load_suite_registry
    from lab.sandbox.docker_runner import PROJECT_ROOT

    registry_path = os.environ.get("LAB_SUITE_REGISTRY_FILE")
    if not registry_path:
        raise recovery.RecoveryPending("original suite registry is unavailable")
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
    if not prove_stopped_owner_dead(owner, run_id):
        raise recovery.RecoveryPending("current Director generation is still alive")
    with director.begin() as connection:
        seconds = float(
            connection.execute(
                text("SELECT lab.begin_stopped_proposal_closure(:id,:proposal)"),
                {"id": recovery_id, "proposal": canonical_bytes(payload).decode()},
            ).scalar_one()
        )
    if seconds <= 0:
        raise recovery.RecoveryPending("original stopped-closure cleanup deadline expired")
    if deadline_at is not None:
        if not isinstance(deadline_at, datetime) or deadline_at.tzinfo is None:
            raise ValueError("stop cleanup deadline must be timezone aware")
        seconds = min(seconds, (deadline_at.astimezone(UTC) - datetime.now(UTC)).total_seconds())
        if seconds <= 0:
            raise recovery.RecoveryPending("original automatic stop cleanup deadline expired")
    deadline = time.monotonic() + min(120.0, seconds)
    _drain_director_sandbox(run_id, asdict(owner), artifact_root, deadline)
    remaining = int(deadline - time.monotonic())
    if remaining < 1:
        raise recovery.RecoveryPending("original stopped-closure cleanup deadline expired")
    result = run_stopped_director_recovery_process(recovery_id, remaining_seconds=remaining)
    if result.state != "drained" or result.exit_code != 0:
        raise recovery.RecoveryPending("stopped proposal children proof incomplete")
    _prove_terminal_jobs(planner, run_id, deadline)
    if not prove_stopped_owner_dead(owner, run_id):
        raise recovery.RecoveryPending("current Director identity no longer proves dead")
    commit_seconds = remaining_stop_closure_seconds(director, recovery_id)
    if deadline_at is not None and commit_seconds is not None:
        commit_seconds = min(
            commit_seconds, int((deadline_at.astimezone(UTC) - datetime.now(UTC)).total_seconds())
        )
    if commit_seconds is None or commit_seconds < 1:
        raise recovery.RecoveryPending("original stopped-closure deadline expired before commit")
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
