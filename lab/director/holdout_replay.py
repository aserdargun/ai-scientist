"""At most three exact holdout suffix checkpoints after validated Director takeover."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text

from lab.director.artifacts import store_director_artifact
from lab.director.budget import RunBudget
from lab.director.holdout import HoldoutReceipt
from lab.director.journal import DirectorRunLease, _event_id, canonical_bytes
from lab.director.ownership import active_execution_owner
from lab.director.resume import assert_historical_receipt


def append_terminal_holdout_suffix(
    engine: Engine,
    lease: DirectorRunLease,
    *,
    receipt: HoldoutReceipt,
    prior_state_sha256: str,
    artifact_root: Path,
) -> tuple[Any, dict[str, Any]]:
    """Reuse all existing bytes; append only a deterministic terminal holdout suffix."""
    from lab.director.loop import (
        DirectorLoop,
        DirectorLoopState,
        _budget_from_json,
        _budget_to_json,
    )

    owner = active_execution_owner()
    if (
        owner is None
        or owner.run_id != lease.run_id
        or receipt.run_id != owner.run_id
        or receipt.admitted_generation is None
        or receipt.execution_sha256 is None
        or receipt.trigger_kind not in {"run_end", "keep_interval"}
        or receipt.trigger_index is None
        or receipt.state not in {"passed", "reverted", "failed", "exhausted"}
    ):
        raise ValueError("holdout replay requires a complete existing terminal identity")
    assert_historical_receipt(
        engine,
        owner=owner,
        admitted_generation=receipt.admitted_generation,
        execution_sha256=receipt.execution_sha256,
    )
    with engine.connect() as connection:
        restart = (
            connection.execute(
                text(
                    "SELECT a.restart_id,o.observation_json,x.execution_json "
                    "FROM lab.director_restart_requests a JOIN "
                    "lab.director_restart_observations o USING(restart_id) "
                    "JOIN lab.director_execution_contracts x USING(run_id) WHERE a.run_id=:run "
                    "AND a.claimant_generation=:generation AND a.state='claimed'"
                ),
                {"run": owner.run_id, "generation": owner.generation},
            )
            .mappings()
            .one()
        )
        events = (
            connection.execute(
                text(
                    "SELECT event_json FROM lab.run_events WHERE run_id=:run AND "
                    "event_type='director.checkpoint' "
                    "ORDER BY (event_json->>'sequence')::integer LIMIT 10001"
                ),
                {"run": owner.run_id},
            )
            .scalars()
            .all()
        )
    if len(events) > 10000:
        raise ValueError("holdout replay checkpoint bound exceeded")
    verified = {
        entry["key"]: lease.read_checkpoint(key=entry["key"], artifact_root=artifact_root)
        for entry in events
    }
    matching = [
        entry
        for entry in verified.values()
        if entry and entry["receipt"]["payload_sha256"] == prior_state_sha256
    ]
    if len(matching) != 1:
        raise ValueError("holdout replay prior state is not one exact checkpoint")
    prior = matching[0]
    state = DirectorLoopState.model_validate_json(
        canonical_bytes(prior["payload"]), strict=True
    ).verify_consistency()
    app_key = f"holdout-application:{receipt.trigger_kind}:{receipt.trigger_index}"
    marker_key = f"holdout-state-application:{receipt.trigger_kind}:{receipt.trigger_index}"
    old_app = verified.get(app_key)
    reservations = [
        entry
        for entry in verified.values()
        if entry
        and entry["receipt"]["phase"] == "holdout_budget_reserved"
        and entry["payload"].get("trigger_kind") == receipt.trigger_kind
        and entry["payload"].get("trigger_index") == receipt.trigger_index
    ]
    if len(reservations) != 1:
        raise ValueError("holdout replay requires one original budget reservation")
    reserved = reservations[0]
    payload = reserved["payload"]
    original = _budget_from_json(payload["budget"])
    reservation = next(
        (
            item
            for item in original.reservations
            if str(item.reservation_id) == payload["reservation_id"]
        ),
        None,
    )
    if reservation is None:
        raise ValueError("holdout replay budget reservation is missing")
    charged = verified.get(f"holdout-budget-reconciled:{reservation.reservation_id}")
    if old_app is not None:
        budget_json = DirectorLoop._validated_application_budget(old_app["payload"])
    elif charged is not None:
        budget_json = charged["payload"]["budget"]
        _budget_from_json(budget_json)
    else:
        limits = restart["execution_json"]["request"]["budget"]
        budget = RunBudget(
            proposal_limit=limits["experiments"],
            wall_limit=limits["wall_seconds"],
            token_limit=limits["model_tokens"],
            snapshot=original,
        )
        budget.reconcile_proposal(
            reservation,
            measured_wall_seconds=float(reservation.wall_seconds),
            measured_model_tokens=0,
        )
        budget_json = _budget_to_json(budget.snapshot())
    expected_state = DirectorLoop._state_after_holdout(
        state,
        receipt,
        trigger_kind=receipt.trigger_kind,
        trigger_index=receipt.trigger_index,
        budget_snapshot=budget_json,
    )
    state_payload = expected_state.model_dump(mode="json", by_alias=True)
    state_sha = hashlib.sha256(canonical_bytes(state_payload)).hexdigest()
    identity = {
        "schema": "director-holdout-application.v1",
        "run_id": str(owner.run_id),
        "trigger_kind": receipt.trigger_kind,
        "trigger_index": receipt.trigger_index,
        "candidate_experiment_id": receipt.candidate_experiment_id,
        "reservation_id": str(receipt.reservation_id),
        "admitted_generation": receipt.admitted_generation,
        "execution_sha256": receipt.execution_sha256,
        "state": receipt.state,
        "bit": receipt.bit,
        "prior_state_sha256": prior_state_sha256,
    }
    app_payload = (
        old_app["payload"]
        if old_app
        else {
            **identity,
            "budget_snapshot": budget_json,
            "budget_snapshot_sha256": DirectorLoop._budget_snapshot_sha256(budget_json),
            "expected_state_sha256": state_sha,
        }
    )
    if any(app_payload.get(key) != value for key, value in identity.items()):
        raise ValueError("existing application conflicts with historical receipt")
    # Normal historical applications bind prior state, receipt and budget, but
    # only failed run-end closure applications required an explicit state hash.
    # Preserve their original bytes while validating every hash that is present.
    digest_required = receipt.trigger_kind == "run_end" and receipt.state == "failed"
    if (digest_required or "expected_state_sha256" in app_payload) and (
        app_payload.get("expected_state_sha256") != state_sha
    ):
        raise ValueError("existing application state digest conflicts with replay")
    app_sha = hashlib.sha256(canonical_bytes(app_payload)).hexdigest()
    state_key = DirectorLoop._state_checkpoint_key(
        expected_state, checkpoint_tag=receipt.trigger_kind
    )
    marker = {
        "schema": "director-holdout-state-application.v1",
        "run_id": str(owner.run_id),
        "application_sha256": app_sha,
        "state_sha256": state_sha,
    }
    sequence = max((entry["sequence"] for entry in events), default=-1) + 1
    entries = []
    state_receipt = None
    for key, phase, document in (
        (app_key, "holdout_application", app_payload),
        (state_key, "director_loop_state", state_payload),
        (marker_key, "holdout_state_application", marker),
    ):
        blob = canonical_bytes(document)
        digest = store_director_artifact(blob, artifact_root=artifact_root)
        existing = verified.get(key)
        if existing is not None:
            if existing["payload"] != document:
                raise ValueError("existing holdout suffix conflicts with replay")
            checkpoint_receipt = existing["receipt"]
        else:
            checkpoint_receipt = {
                "schema": "director-checkpoint.v1",
                "sequence": sequence,
                "key": key,
                "phase": phase,
                "payload_sha256": digest,
                "blob_sha256": digest,
            }
            sequence += 1
        entries.append(
            {
                "event_id": str(_event_id(owner.run_id, key)),
                "receipt": checkpoint_receipt,
                "payload": document,
                "payload_text": blob.decode(),
            }
        )
        if phase == "director_loop_state":
            state_receipt = checkpoint_receipt
    bundle = {
        "schema": "director-holdout-replay-bundle.v1",
        "restart_id": str(restart["restart_id"]),
        "run_id": str(owner.run_id),
        "writer_generation": owner.generation,
        "writer_invocation": owner.invocation_id,
        "execution_sha256": owner.execution_sha256,
        "reservation_id": str(receipt.reservation_id),
        "prior_state": prior,
        "budget_origin": reserved,
        "budget_reconciled": charged,
        "entries": entries,
        "prior_state_text": canonical_bytes(prior["payload"]).decode(),
        "budget_origin_text": canonical_bytes(reserved["payload"]).decode(),
    }
    with engine.begin() as connection:
        connection.execute(
            text("SELECT lab.append_resumed_holdout_suffix(:bundle)"),
            {"bundle": canonical_bytes(bundle).decode()},
        ).scalar_one()
    if state_receipt is None:
        raise RuntimeError("holdout replay produced no state receipt")
    return expected_state, state_receipt


def recover_observed_terminal_holdouts(
    engine: Engine, lease: DirectorRunLease, *, artifact_root: Path
) -> int:
    """Complete interrupted historical prefixes before ordinary loop budget restoration."""
    from lab.director.holdout import read_holdout_request

    owner = active_execution_owner()
    if owner is None or owner.generation == 1:
        return 0
    with engine.connect() as connection:
        events = (
            connection.execute(
                text(
                    "SELECT event_json FROM lab.run_events WHERE run_id=:run AND "
                    "event_type='director.checkpoint' "
                    "ORDER BY (event_json->>'sequence')::integer LIMIT 10001"
                ),
                {"run": owner.run_id},
            )
            .scalars()
            .all()
        )
    if len(events) > 10000:
        raise ValueError("holdout resume inventory exceeds bound")
    recovered = 0
    for event in events:
        if event.get("phase") != "holdout_budget_reserved":
            continue
        reserved = lease.read_checkpoint(key=event["key"], artifact_root=artifact_root)
        if reserved is None:
            raise ValueError("holdout reserved checkpoint disappeared")
        payload = reserved["payload"]
        kind, index = payload["trigger_kind"], payload["trigger_index"]
        app = lease.read_checkpoint(
            key=f"holdout-application:{kind}:{index}", artifact_root=artifact_root
        )
        states = [entry for entry in events if entry.get("phase") == "director_loop_state"]
        if app is not None:
            prior_sha = app["payload"]["prior_state_sha256"]
            candidates = [entry for entry in states if entry["payload_sha256"] == prior_sha]
        else:
            candidates = states[-1:]
        if len(candidates) != 1:
            raise ValueError("historical holdout has no unambiguous prior state")
        prior = lease.read_checkpoint(key=candidates[0]["key"], artifact_root=artifact_root)
        if prior is None:
            raise ValueError("holdout prior state disappeared")
        candidate = prior["payload"]["champion_experiment_id"]
        receipt = read_holdout_request(
            engine,
            run_id=owner.run_id,
            candidate_experiment_id=candidate,
            trigger_kind=kind,
            trigger_index=index,
        )
        if receipt is None or receipt.state in {"reserved", "running"}:
            # Missing admission follows034's existing bitless closure, not invented success.
            continue
        marker = lease.read_checkpoint(
            key=f"holdout-state-application:{kind}:{index}", artifact_root=artifact_root
        )
        if marker is not None:
            continue
        append_terminal_holdout_suffix(
            engine,
            lease,
            receipt=receipt,
            prior_state_sha256=candidates[0]["payload_sha256"],
            artifact_root=artifact_root,
        )
        recovered += 1
    return recovered
