"""Finite, unscored mode replay under the existing Director execution lease.

Checkpoint payloads contain small immutable references. Predictions and the opaque
fit are separate bounded blobs; only the sandbox ever deserializes a fitted model.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Engine, select, text

from harness.contracts import FitContext
from lab.db.schema import run_events
from lab.director.artifacts import read_director_artifact, store_director_artifact
from lab.director.journal import DirectorRunLease, canonical_bytes
from lab.director.ownership import active_execution_owner, assert_execution_owner_transaction
from lab.operating_modes.candidate import candidate_source
from lab.operating_modes.contracts import AlarmState, ModeConfig, PredictionBatch
from lab.operating_modes.stream import (
    StreamInputManifest,
    configuration_sha256,
    load_input,
    load_train,
    read_chunk,
)
from lab.scorer.jobs import read_artifact_bytes

PROGRAM: Literal["mode-stream.v1"] = "mode-stream.v1"
MAX_CHUNKS = 1024
MAX_CHUNK_BYTES = 2 * 1024 * 1024
MAX_CHECKPOINT_BYTES = 32 * 1024
STREAM_PHASE_SECONDS = 60
FINALIZATION_RESERVE_SECONDS = 30
SHA = r"^[0-9a-f]{64}$"


class ModeStreamPlan(BaseModel):
    """One exact fit configuration and one already ordered finite input sequence."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    schema_version: Literal["director-mode-stream.v1"] = Field(alias="schema")
    suite_id: str = Field(min_length=1, max_length=128)
    suite_version: Literal[1] = 1
    program_version: Literal["mode-stream.v1"] = PROGRAM
    input_sha256: str = Field(pattern=SHA)
    configuration: ModeConfig
    configuration_sha256: str = Field(pattern=SHA)
    candidate_sha256: str = Field(pattern=SHA)
    source_kind: Literal["synthetic", "private_database"] = "synthetic"
    sensors: tuple[str, ...] = Field(min_length=1, max_length=64)
    train_rows: int = Field(ge=16, le=4096)
    total_rows: int = Field(ge=1, le=65536)
    chunk_count: int = Field(ge=1, le=MAX_CHUNKS)
    scoring_available: Literal[False] = False

    @model_validator(mode="after")
    def identities(self) -> ModeStreamPlan:
        """Bind the generated source and suite name to the exact configuration."""
        if (
            self.configuration_sha256 != configuration_sha256(self.configuration)
            or self.candidate_sha256
            != hashlib.sha256(candidate_source(self.configuration)).hexdigest()
            or len(set(self.sensors)) != len(self.sensors)
        ):
            raise ValueError("stream configuration/source identity is inconsistent")
        identity = hashlib.sha256(
            canonical_bytes(
                {
                    "input_sha256": self.input_sha256,
                    "configuration_sha256": self.configuration_sha256,
                }
            )
        ).hexdigest()
        if self.suite_id != f"mode-stream-{identity[:48]}":
            raise ValueError("stream suite identity differs from its input and configuration")
        return self

    def document(self) -> dict[str, Any]:
        """Return the canonical JSON-compatible plan fields."""
        return self.model_dump(mode="json", by_alias=True)

    @property
    def sha256(self) -> str:
        """Hash the complete immutable plan."""
        return hashlib.sha256(canonical_bytes(self.document())).hexdigest()

    def verify_input(self, manifest: StreamInputManifest) -> None:
        """Compare all plan dimensions with its verified ordered input."""
        if (
            manifest.sha256 != self.input_sha256
            or manifest.source_kind != self.source_kind
            or tuple(manifest.sensors) != self.sensors
            or manifest.train.rows != self.train_rows
            or sum(item.rows for item in manifest.chunks) != self.total_rows
            or len(manifest.chunks) != self.chunk_count
            or manifest.scoring_available is not False
        ):
            raise ValueError("stream plan differs from its immutable input manifest")


def make_plan(manifest: StreamInputManifest, configuration: ModeConfig) -> ModeStreamPlan:
    """Derive one reproducible replay plan from a bounded input and frozen fit settings."""
    digest = configuration_sha256(configuration)
    identity = hashlib.sha256(
        canonical_bytes(
            {
                "input_sha256": manifest.sha256,
                "configuration_sha256": digest,
            }
        )
    ).hexdigest()
    return ModeStreamPlan(
        schema="director-mode-stream.v1",
        suite_id=f"mode-stream-{identity[:48]}",
        input_sha256=manifest.sha256,
        source_kind=manifest.source_kind,
        configuration=configuration,
        configuration_sha256=digest,
        candidate_sha256=hashlib.sha256(candidate_source(configuration)).hexdigest(),
        sensors=tuple(manifest.sensors),
        train_rows=manifest.train.rows,
        total_rows=sum(item.rows for item in manifest.chunks),
        chunk_count=len(manifest.chunks),
    )


def plan_from_request(request: dict[str, Any]) -> ModeStreamPlan:
    """Reject a research/baseline request even if it contains stream-like fields."""
    plan = ModeStreamPlan.model_validate_json(canonical_bytes(request.get("mode_stream", {})))
    budget = request.get("budget")
    if (
        request.get("purpose") != "mode-stream"
        or request.get("provider") != "mode-stream"
        or request.get("track") != "mode"
        or request.get("program_version") != PROGRAM
        or request.get("suite") != plan.suite_id
        or request.get("suite_manifest_sha256") != plan.sha256
        or request.get("provider_config_sha256") != plan.configuration_sha256
        or request.get("proposal_limit") != 0
        or not isinstance(budget, dict)
        or type(budget.get("experiments")) is not int
        or budget["experiments"] != 0
        or type(budget.get("model_tokens")) is not int
        or budget["model_tokens"] != 0
        or type(budget.get("wall_seconds")) is not int
        or not 1 <= budget["wall_seconds"] <= 14400
    ):
        raise ValueError("run is not an admitted finite diagnostic stream")
    return plan


def _checkpoint_payload(receipt: Any, artifact_root: Path) -> dict[str, Any]:
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema") != "director-checkpoint.v1"
        or receipt.get("payload_sha256") != receipt.get("blob_sha256")
    ):
        raise ValueError("stream checkpoint receipt is malformed")
    raw = read_artifact_bytes(
        receipt["blob_sha256"],
        artifact_root=artifact_root,
        max_bytes=MAX_CHECKPOINT_BYTES,
    )
    value = json.loads(raw)
    if not isinstance(value, dict) or canonical_bytes(value) != raw:
        raise ValueError("stream checkpoint is not canonical JSON")
    return value


def validate_chunk(
    raw: bytes,
    *,
    plan: ModeStreamPlan,
    run_id: UUID,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """A diagnostic envelope cannot be mistaken for a scalar Scorer artifact."""
    if len(raw) > MAX_CHUNK_BYTES:
        raise ValueError("stream chunk exceeds the 2 MiB bound")
    value = json.loads(raw)
    if not isinstance(value, dict) or canonical_bytes(value) != raw:
        raise ValueError("stream chunk is not canonical JSON")
    if set(value) != {
        "schema",
        "run_id",
        "chunk_index",
        "row_offset",
        "row_count",
        "input_sha256",
        "input_chunk_sha256",
        "plan_sha256",
        "model_sha256",
        "fit_artifact_sha256",
        "previous_chunk_sha256",
        "candidate_derived",
        "scoring_available",
        "alarm_in",
        "prediction",
        "start_utc",
        "end_utc",
        "sampling_seconds",
    }:
        raise ValueError("stream chunk has an invalid envelope")
    if (
        value["schema"] != "candidate-mode-stream-chunk.v1"
        or value["candidate_derived"] is not True
        or value["scoring_available"] is not False
        or value["run_id"] != str(run_id)
        or value["input_sha256"] != plan.input_sha256
        or value["plan_sha256"] != plan.sha256
        or hashlib.sha256(raw).hexdigest() != metadata["artifact_sha256"]
        or any(
            value[key] != metadata[key]
            for key in (
                "chunk_index",
                "row_offset",
                "row_count",
                "model_sha256",
                "fit_artifact_sha256",
                "input_chunk_sha256",
                "previous_chunk_sha256",
                "alarm_in",
            )
        )
    ):
        raise ValueError("stream chunk binding differs from its committed checkpoint")
    prediction = PredictionBatch.model_validate_json(canonical_bytes(value["prediction"]))
    if (
        prediction.model_sha256 != metadata["model_sha256"]
        or len(prediction.rows) != metadata["row_count"]
        or not 1 <= len(prediction.rows) <= 64
        or prediction.alarm_state.model_dump(mode="json") != metadata["alarm_out"]
        or any(
            row.index != metadata["row_offset"] + index
            or tuple(item.sensor for item in row.residuals) != plan.sensors
            for index, row in enumerate(prediction.rows)
        )
    ):
        raise ValueError("stream prediction has a gap, changed model, sensors or alarm cursor")
    return value


def read_stream_state(
    connection: Any,
    *,
    run_id: UUID,
    request: dict[str, Any],
    artifact_root: Path,
    verify_predictions: bool = False,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read one committed prefix; unreferenced blobs never advance progress.

    Polling reads compact checkpoint metadata. Resume/finalization independently
    read every bounded prediction blob and verify the historical owner identities.
    """
    plan = plan_from_request(request)
    receipts = (
        evidence["checkpoints"]
        if evidence is not None
        else connection.execute(
            select(run_events.c.event_json)
            .where(
                run_events.c.run_id == run_id,
                run_events.c.event_type == "director.checkpoint",
            )
            .limit(MAX_CHUNKS + 4)
        )
        .scalars()
        .all()
    )
    if len(receipts) > MAX_CHUNKS + 2:
        raise ValueError("stream checkpoint count exceeds its finite plan")
    if any(
        not isinstance(item, dict) or type(item.get("sequence")) is not int for item in receipts
    ):
        raise ValueError("stream checkpoint sequence is malformed")
    receipts.sort(key=lambda item: item["sequence"])
    fit: dict[str, Any] | None = None
    finished = False
    failure: str | None = None
    previous: str | None = None
    chunks: list[dict[str, Any]] = []
    alarm, offset = AlarmState().model_dump(mode="json"), 0
    owners: set[tuple[int, str]] = set()
    for sequence, receipt in enumerate(receipts):
        if receipt["sequence"] != sequence:
            raise ValueError("stream checkpoint sequence has a gap")
        payload = _checkpoint_payload(receipt, artifact_root)
        if (
            payload.get("run_id") != str(run_id)
            or payload.get("plan_sha256") != plan.sha256
            or payload.get("input_sha256") != plan.input_sha256
            or type(payload.get("admitted_generation")) is not int
            or payload["admitted_generation"] < 1
            or not isinstance(payload.get("execution_sha256"), str)
            or len(payload["execution_sha256"]) != 64
            or finished
            or failure is not None
        ):
            raise ValueError("stream checkpoint identity or terminal ordering is invalid")
        owners.add((payload["admitted_generation"], payload["execution_sha256"]))
        if sequence == 0 and receipt.get("key") == "mode-stream-fit":
            if (
                receipt.get("phase") != "mode_stream_fit"
                or payload.get("schema") != "director-mode-stream-fit.v1"
            ):
                raise ValueError("stream fit receipt is invalid")
            if (
                payload.get("candidate_sha256") != plan.candidate_sha256
                or payload.get("configuration_sha256") != plan.configuration_sha256
                or payload.get("model_summary", {}).get("model_sha256")
                != payload.get("model_sha256")
                or payload.get("model_summary", {}).get("sensors") != list(plan.sensors)
            ):
                raise ValueError("stream fit differs from the frozen plan")
            fit = payload
        elif receipt.get("key") == f"mode-stream-chunk:{len(chunks)}" and fit is not None:
            frozen_identity = dict(fit)
            if (
                receipt.get("phase") != "mode_stream_chunk"
                or payload.get("schema") != "director-mode-stream-chunk.v1"
                or payload.get("chunk_index") != len(chunks)
                or type(payload.get("row_count")) is not int
                or not 1 <= payload["row_count"] <= 64
                or payload.get("row_offset") != offset
                or payload.get("alarm_in") != alarm
                or payload.get("previous_chunk_sha256") != previous
                or payload.get("model_sha256") != frozen_identity["model_sha256"]
                or payload.get("fit_artifact_sha256") != frozen_identity["fit_artifact_sha256"]
                or len(chunks) >= plan.chunk_count
            ):
                raise ValueError("stream committed prefix has a gap or changed frozen identity")
            alarm = AlarmState.model_validate(payload["alarm_out"]).model_dump(mode="json")
            previous, offset = payload["artifact_sha256"], offset + payload["row_count"]
            if offset > plan.total_rows:
                raise ValueError("stream committed rows exceed the finite input")
            if verify_predictions:
                raw = read_artifact_bytes(
                    previous, artifact_root=artifact_root, max_bytes=MAX_CHUNK_BYTES
                )
                validate_chunk(raw, plan=plan, run_id=run_id, metadata=payload)
            chunks.append(payload)
        elif receipt.get("key") == "mode-stream-end":
            if (
                receipt.get("phase") != "mode_stream_end"
                or payload.get("schema") != "director-mode-stream-end.v1"
                or payload.get("committed_rows") != offset
                or payload.get("committed_chunks") != len(chunks)
                or payload.get("last_chunk_sha256") != previous
            ):
                raise ValueError("stream terminal checkpoint differs from its prefix")
            if payload.get("outcome") == "completed":
                if fit is None or offset != plan.total_rows or len(chunks) != plan.chunk_count:
                    raise ValueError("completed stream is missing part of the finite input")
                finished = True
            elif payload.get("outcome") == "failed" and isinstance(
                payload.get("failure_reason"), str
            ):
                failure = payload["failure_reason"]
            else:
                raise ValueError("stream terminal outcome is invalid")
        else:
            raise ValueError("stream checkpoint order or key is invalid")
    if verify_predictions and connection.dialect.name == "postgresql":
        for generation, digest in owners:
            if evidence is not None:
                if {"generation": generation, "execution_sha256": digest} not in evidence["owners"]:
                    raise ValueError("stream prefix has no matching historical execution owner")
                continue
            present = connection.execute(
                text(
                    "SELECT 1 FROM lab.director_owner_generations g "
                    "JOIN lab.director_execution_contracts x USING(run_id) "
                    "JOIN lab.director_execution_control c USING(run_id) "
                    "WHERE g.run_id=:run AND g.generation=:generation "
                    "AND g.generation<=c.current_generation AND g.execution_sha256=:sha "
                    "AND x.execution_sha256=:sha"
                ),
                {"run": run_id, "generation": generation, "sha": digest},
            ).scalar_one_or_none()
            if present != 1:
                raise ValueError("stream prefix has no matching historical execution owner")
    return {
        "plan": plan,
        "fit": fit,
        "chunks": chunks,
        "committed_rows": offset,
        "alarm_state": alarm,
        "last_chunk_sha256": previous,
        "finished": finished,
        "failure_reason": failure,
        "next_sequence": len(receipts),
    }


def compact_model_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Polling metadata stays small even when a fitted model has many centers."""
    return {
        key: summary[key]
        for key in (
            "model_sha256",
            "sensors",
            "config",
            "training_rows",
            "alarm_threshold",
            "alarm_release",
        )
    }


def verify_stream_predictions(
    state: dict[str, Any],
    *,
    run_id: UUID,
    fit: Any,
    manifest: StreamInputManifest,
    input_root: Path,
    artifact_root: Path,
    admission_check: Any = None,
) -> None:
    """Recheck the recorded values and alarm chain against each actual pinned frame."""
    from lab.sandbox.mode_stream import validate_prediction_batch

    plan = state["plan"]
    plan.verify_input(manifest)
    for index, metadata in enumerate(state["chunks"]):
        if admission_check is not None:
            admission_check()
        ref = manifest.chunks[index]
        if (
            ref.sha256 != metadata["input_chunk_sha256"]
            or ref.rows != metadata["row_count"]
            or ref.row_offset != metadata["row_offset"]
        ):
            raise ValueError("committed stream chunk differs from its ordered input frame")
        raw = read_artifact_bytes(
            metadata["artifact_sha256"], artifact_root=artifact_root, max_bytes=MAX_CHUNK_BYTES
        )
        value = validate_chunk(raw, plan=plan, run_id=run_id, metadata=metadata)
        if (
            value["start_utc"] != ref.start_utc
            or value["end_utc"] != ref.end_utc
            or value["sampling_seconds"] != manifest.sampling_seconds
        ):
            raise ValueError("stream chunk time coordinates differ from its input")
        validate_prediction_batch(
            PredictionBatch.model_validate_json(canonical_bytes(value["prediction"])),
            fit=fit,
            frame=read_chunk(input_root, manifest, index),
            row_offset=ref.row_offset,
            alarm_state=AlarmState.model_validate(metadata["alarm_in"]),
        )


def run_mode_stream(
    director: Engine,
    planner: Engine,
    *,
    run_id: UUID,
    plan: ModeStreamPlan,
    input_root: Path,
    artifact_root: Path,
    lease: DirectorRunLease,
) -> dict[str, Any]:
    """One frozen fit, sequential P1 chunks, original SQL deadline and owned commits."""
    from lab.director.task_plan import seal_run_task_plan
    from lab.sandbox.docker_runner import DEFAULT_SANDBOX_IMAGE, LocalDockerRunner, SandboxProfile
    from lab.sandbox.mode_stream import StreamFit, run_stream_chunk, run_stream_fit
    from lab.scorer.supervisor import run_scorer_finalize_process

    owner = active_execution_owner()
    if owner is None or owner.run_id != run_id:
        raise RuntimeError("stream operation lacks its captured Director owner")
    with director.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT r.request_json,x.deadline_at,x.execution_sha256 FROM lab.runs r "
                    "JOIN lab.director_execution_contracts x USING(run_id) WHERE r.run_id=:run"
                ),
                {"run": run_id},
            )
            .mappings()
            .one()
        )
    if (
        row["execution_sha256"] != owner.execution_sha256
        or plan_from_request(row["request_json"]) != plan
    ):
        raise ValueError("stream request or execution changed after admission")
    deadline_at = row["deadline_at"]
    if not isinstance(deadline_at, datetime) or deadline_at.tzinfo is None:
        raise ValueError("stream original execution deadline is unavailable")
    deadline = time.monotonic() + max(0.0, (deadline_at - datetime.now(UTC)).total_seconds())
    # The runner observes stop at phase boundaries. Its existing bounded cleanup
    # must leave room inside first-stop + 120 s and the original wall deadline.
    work_deadline = deadline - FINALIZATION_RESERVE_SECONDS

    def admit() -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("original stream wall deadline expired")
        lease.require_run_active()
        with director.begin() as connection:
            assert_execution_owner_transaction(connection, owner, run_id=run_id)

    def admit_phase() -> None:
        admit()
        if time.monotonic() >= work_deadline:
            raise TimeoutError("original stream work budget exhausted before cleanup reserve")

    admit()
    manifest = load_input(input_root, plan.input_sha256)
    plan.verify_input(manifest)
    with director.connect() as connection:
        state = read_stream_state(
            connection,
            run_id=run_id,
            request=row["request_json"],
            artifact_root=artifact_root,
            verify_predictions=True,
        )
    # A resumed stream never obtains a fresh wall budget or a new model identity.
    runner = LocalDockerRunner(
        image=DEFAULT_SANDBOX_IMAGE,
        work_root=artifact_root.parent / "sandbox" / str(run_id),
        profile=SandboxProfile(timeout_seconds=STREAM_PHASE_SECONDS),
    )
    context = FitContext(
        seed=0,
        signals=manifest.sensors,
        regime_signals=(),
        sampling_s=manifest.context_sampling_seconds,
        time_budget_s=max(
            1, min(STREAM_PHASE_SECONDS, math.floor(work_deadline - time.monotonic()))
        ),
    )
    common = {
        "run_id": str(run_id),
        "input_sha256": plan.input_sha256,
        "plan_sha256": plan.sha256,
        "admitted_generation": owner.generation,
        "execution_sha256": owner.execution_sha256,
    }
    if state["fit"] is None and not state["failure_reason"]:
        admit_phase()
        fitted = run_stream_fit(
            runner,
            config=plan.configuration,
            train=load_train(input_root, manifest),
            context=context,
            input_sha256=plan.input_sha256,
            deadline=work_deadline,
            admission_check=admit_phase,
        )
        admit()
        fit_blob = store_director_artifact(fitted.fit_artifact, artifact_root=artifact_root)
        document_blob = store_director_artifact(fitted.document, artifact_root=artifact_root)
        payload = {
            **common,
            "schema": "director-mode-stream-fit.v1",
            "fit_artifact_sha256": fit_blob,
            "document_sha256": document_blob,
            "model_sha256": fitted.model_sha256,
            "model_summary": compact_model_summary(fitted.model_summary),
            "candidate_sha256": fitted.candidate_sha256,
            "configuration_sha256": fitted.configuration_sha256,
        }
        lease.append_checkpoint(
            sequence=0,
            key="mode-stream-fit",
            phase="mode_stream_fit",
            payload=payload,
            artifact_root=artifact_root,
        )
        state["fit"], state["next_sequence"] = payload, 1
    elif state["fit"] is not None:
        fitted = StreamFit(
            fit_artifact=read_director_artifact(
                state["fit"]["fit_artifact_sha256"], artifact_root=artifact_root
            ),
            document=read_director_artifact(
                state["fit"]["document_sha256"], artifact_root=artifact_root
            ),
        )
        if (
            fitted.model_sha256 != state["fit"]["model_sha256"]
            or fitted.input_sha256 != plan.input_sha256
            or fitted.configuration_sha256 != plan.configuration_sha256
            or fitted.candidate_sha256 != plan.candidate_sha256
            or compact_model_summary(fitted.model_summary) != state["fit"]["model_summary"]
        ):
            raise ValueError("persisted frozen fit differs from its committed identity")
        verify_stream_predictions(
            state,
            run_id=run_id,
            fit=fitted,
            manifest=manifest,
            input_root=input_root,
            artifact_root=artifact_root,
            admission_check=admit,
        )
    if not state["finished"] and state["failure_reason"] is None:
        for index in range(len(state["chunks"]), plan.chunk_count):
            admit_phase()
            frame_ref = manifest.chunks[index]
            if frame_ref.row_offset != state["committed_rows"]:
                raise ValueError("ordered input offset differs from committed stream cursor")
            alarm_in = dict(state["alarm_state"])
            prediction = run_stream_chunk(
                runner,
                config=plan.configuration,
                frame=read_chunk(input_root, manifest, index),
                context=context,
                input_sha256=plan.input_sha256,
                fit=fitted,
                row_offset=state["committed_rows"],
                alarm_state=AlarmState.model_validate(alarm_in),
                deadline=work_deadline,
                admission_check=admit_phase,
            )
            admit()
            value = {
                "schema": "candidate-mode-stream-chunk.v1",
                "run_id": str(run_id),
                "chunk_index": index,
                "row_offset": state["committed_rows"],
                "row_count": frame_ref.rows,
                "input_sha256": plan.input_sha256,
                "input_chunk_sha256": frame_ref.sha256,
                "plan_sha256": plan.sha256,
                "model_sha256": fitted.model_sha256,
                "fit_artifact_sha256": fitted.fit_artifact_sha256,
                "previous_chunk_sha256": state["last_chunk_sha256"],
                "candidate_derived": True,
                "scoring_available": False,
                "alarm_in": alarm_in,
                "prediction": prediction.to_dict(),
                "start_utc": frame_ref.start_utc,
                "end_utc": frame_ref.end_utc,
                "sampling_seconds": manifest.sampling_seconds,
            }
            raw = canonical_bytes(value)
            digest = hashlib.sha256(raw).hexdigest()
            metadata = {
                **common,
                "schema": "director-mode-stream-chunk.v1",
                **{
                    key: value[key]
                    for key in (
                        "chunk_index",
                        "row_offset",
                        "row_count",
                        "input_chunk_sha256",
                        "model_sha256",
                        "fit_artifact_sha256",
                        "previous_chunk_sha256",
                        "alarm_in",
                    )
                },
                "alarm_out": prediction.alarm_state.model_dump(mode="json"),
                "artifact_sha256": digest,
            }
            validate_chunk(raw, plan=plan, run_id=run_id, metadata=metadata)
            store_director_artifact(raw, artifact_root=artifact_root)
            lease.append_checkpoint(
                sequence=state["next_sequence"],
                key=f"mode-stream-chunk:{index}",
                phase="mode_stream_chunk",
                payload=metadata,
                artifact_root=artifact_root,
            )
            state["chunks"].append(metadata)
            state["committed_rows"] += frame_ref.rows
            state["alarm_state"], state["last_chunk_sha256"] = metadata["alarm_out"], digest
            state["next_sequence"] += 1
        admit()
        lease.append_checkpoint(
            sequence=state["next_sequence"],
            key="mode-stream-end",
            phase="mode_stream_end",
            payload={
                **common,
                "schema": "director-mode-stream-end.v1",
                "outcome": "completed",
                "committed_rows": state["committed_rows"],
                "committed_chunks": len(state["chunks"]),
                "last_chunk_sha256": state["last_chunk_sha256"],
            },
            artifact_root=artifact_root,
        )
    admit()
    seal_run_task_plan(planner, run_id=run_id)
    remaining = min(600, math.floor(deadline - time.monotonic()))
    if remaining < 1:
        raise TimeoutError("original stream deadline expired before report publication")
    result = run_scorer_finalize_process(
        run_id,
        admitted_generation=owner.generation,
        execution_sha256=owner.execution_sha256,
        remaining_seconds=remaining,
        artifact_root=artifact_root,
    )
    if result.exit_code != 0 or (result.result or {}).get("state") != "finalized":
        raise RuntimeError("stream report finalization is pending")
    return {
        "run_id": str(run_id),
        "purpose": "mode-stream",
        "scoring_available": False,
        "committed_rows": state["committed_rows"],
        "committed_chunks": len(state["chunks"]),
    }
