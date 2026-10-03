"""Independently verify the committed diagnostic prefix without inventing scores."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Engine, insert, select, text, update

from lab.db.schema import experiments, reports, run_tasks, runs, score_jobs
from lab.db.task_plan import canonical_task_plan_digest
from lab.director.journal import canonical_bytes
from lab.director.mode_stream import (
    MAX_CHUNKS,
    SHA,
    compact_model_summary,
    plan_from_request,
    read_stream_state,
    verify_stream_predictions,
)


class ModeStreamReport(BaseModel):
    """A verified diagnostic prefix with no scalar scores or success ranking."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    schema_version: Literal["lab.mode-stream-report.v1"] = Field(alias="schema")
    run_id: str
    purpose: Literal["mode-stream"]
    program_version: Literal["mode-stream.v1"]
    status: Literal["completed", "stopped", "failed"]
    scoring_available: Literal[False]
    candidate_derived: Literal[True]
    input_sha256: str = Field(pattern=SHA)
    plan_sha256: str = Field(pattern=SHA)
    configuration_sha256: str = Field(pattern=SHA)
    candidate_sha256: str = Field(pattern=SHA)
    model_sha256: str | None = Field(pattern=SHA)
    fit_artifact_sha256: str | None = Field(pattern=SHA)
    total_rows: int = Field(ge=1, le=65536)
    committed_rows: int = Field(ge=0, le=65536)
    committed_chunks: int = Field(ge=0, le=MAX_CHUNKS)
    chunk_sha256: list[str] = Field(max_length=MAX_CHUNKS)
    complete_input: bool
    failure_reason: str | None = Field(max_length=160)
    admitted_generation: int | None = Field(default=None, ge=1)
    execution_sha256: str | None = Field(default=None, pattern=SHA)

    @model_validator(mode="after")
    def coherent(self) -> ModeStreamReport:
        """Reject inconsistent row, frozen-model and execution-owner identities."""
        UUID(self.run_id)
        if (
            self.committed_rows > self.total_rows
            or len(self.chunk_sha256) != self.committed_chunks
            or any(
                len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
                for value in self.chunk_sha256
            )
            or (self.model_sha256 is None) != (self.fit_artifact_sha256 is None)
            or (self.model_sha256 is None and self.committed_rows != 0)
            or (self.admitted_generation is None) != (self.execution_sha256 is None)
            or (self.complete_input != (self.committed_rows == self.total_rows))
            or (self.status == "completed" and not self.complete_input)
            or (
                self.admitted_generation is None
                and (self.status != "stopped" or self.committed_rows != 0)
            )
        ):
            raise ValueError("mode stream report contains an inconsistent prefix or owner")
        return self


def validate_mode_stream_report(value: dict[str, Any]) -> dict[str, Any]:
    """Validate a report and omit nonexistent owner identities for an unstarted stop."""
    parsed = ModeStreamReport.model_validate_json(canonical_bytes(value))
    result = parsed.model_dump(mode="json", by_alias=True)
    if parsed.admitted_generation is None:
        result.pop("admitted_generation")
        result.pop("execution_sha256")
    return result


def _stopped_owner_quiescent(evidence: dict[str, Any], run_id: UUID, artifact_root: Path) -> None:
    from lab.director.recovery import _owner_is_proven_dead, _stored_owner
    from lab.sandbox.docker_runner import DEFAULT_ADMISSION_LOCK

    owner = _stored_owner(evidence["owner"])
    if owner is None or not _owner_is_proven_dead(owner, run_id):
        raise ValueError("stream stop cannot publish while its Director generation is alive")
    marker_path = DEFAULT_ADMISSION_LOCK.with_suffix(".intent")
    if marker_path.exists():
        if marker_path.is_symlink() or marker_path.stat().st_size > 16384:
            raise ValueError("sandbox drain intent is not safely observable")
        marker = json.loads(marker_path.read_bytes())
        owned_root = artifact_root.parent / "sandbox" / str(run_id)
        if marker.get("work_root") == str(owned_root):
            raise ValueError("stream stop still has an unreconciled sandbox intent")


def finalize_mode_stream(
    engine: Engine,
    *,
    run_id: UUID,
    artifact_root: Path,
    admitted_generation: int | None,
    execution_sha256: str | None,
    empty_stop: bool = False,
) -> tuple[dict[str, Any], str] | None:
    """Use the same Scorer process/transaction pair; diagnostic rows are never scores."""
    with engine.begin() as connection:
        row = connection.execute(select(runs).where(runs.c.run_id == run_id)).mappings().one()
        plan = plan_from_request(row["request_json"])
        existing = (
            connection.execute(select(reports).where(reports.c.run_id == run_id))
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            document = validate_mode_stream_report(existing["report_json"])
            if (
                document.get("admitted_generation") != admitted_generation
                or document.get("execution_sha256") != execution_sha256
                or document["input_sha256"] != plan.input_sha256
                or document["plan_sha256"] != plan.sha256
                or hashlib.sha256(canonical_bytes(document)).hexdigest()
                != existing["report_sha256"]
                or existing["report_sha256"] != row["report_sha256"]
                or document["status"] != row["state"]
            ):
                raise ValueError("stream report replay differs from its admitted identity")
            return document, existing["report_sha256"]
        if row["state"] not in {"running", "stop_requested"}:
            return None
        ownerless = empty_stop and admitted_generation is None and execution_sha256 is None
        if empty_stop != ownerless or (
            not ownerless and (admitted_generation is None or execution_sha256 is None)
        ):
            raise ValueError("stream finalization requires its original execution pair")
        evidence = None
        if engine.dialect.name == "postgresql":
            if ownerless:
                connection.execute(
                    text("SELECT lab.prepare_unstarted_baseline_stop(:run,:sha)"),
                    {"run": run_id, "sha": row["payload_sha256"]},
                )
            evidence = connection.execute(
                text("SELECT lab.mode_stream_report_evidence(:run,:generation,:sha,:empty)"),
                {
                    "run": run_id,
                    "generation": admitted_generation,
                    "sha": execution_sha256,
                    "empty": ownerless,
                },
            ).scalar_one()
            if not isinstance(evidence, dict) or evidence.get("run_id") != str(run_id):
                raise ValueError("stream report evidence belongs to another run")
            row = (
                connection.execute(select(runs).where(runs.c.run_id == run_id).with_for_update())
                .mappings()
                .one()
            )
            if row["state"] == "stop_requested" and not ownerless:
                _stopped_owner_quiescent(evidence, run_id, artifact_root)
        else:
            # SQLite fixtures test document semantics; they do not model PostgreSQL roles.
            for table in (experiments, run_tasks, score_jobs):
                if (
                    connection.execute(
                        select(table.c.run_id).where(table.c.run_id == run_id).limit(1)
                    ).first()
                    is not None
                ):
                    raise ValueError("diagnostic stream unexpectedly contains research work")
            if ownerless and row["state"] != "stop_requested":
                raise ValueError("ownerless stream finalization requires a stop")
        state = read_stream_state(
            connection,
            run_id=run_id,
            request=row["request_json"],
            artifact_root=artifact_root,
            verify_predictions=True,
            evidence=evidence,
        )
        if ownerless and (state["fit"] or state["chunks"] or state["next_sequence"]):
            raise ValueError("unstarted stream stop has execution evidence")
        fit = state["fit"]
        if fit is not None:
            from lab.director.artifacts import read_director_artifact
            from lab.sandbox.mode_stream import StreamFit

            fitted = StreamFit(
                fit_artifact=read_director_artifact(
                    fit["fit_artifact_sha256"], artifact_root=artifact_root
                ),
                document=read_director_artifact(
                    fit["document_sha256"], artifact_root=artifact_root
                ),
            )
            if (
                fitted.model_sha256 != fit["model_sha256"]
                or fitted.input_sha256 != plan.input_sha256
                or fitted.configuration_sha256 != plan.configuration_sha256
                or fitted.candidate_sha256 != plan.candidate_sha256
                or compact_model_summary(fitted.model_summary) != fit["model_summary"]
            ):
                raise ValueError("stream frozen fit does not match its independently read evidence")
            from lab.operating_modes.stream import load_input

            input_root = artifact_root.parent.parent / "mode-stream-inputs"
            manifest = load_input(input_root, plan.input_sha256)
            verify_stream_predictions(
                state,
                run_id=run_id,
                fit=fitted,
                manifest=manifest,
                input_root=input_root,
                artifact_root=artifact_root,
            )
        if row["state"] == "running" and not (state["finished"] or state["failure_reason"]):
            return None
        if row["state"] == "running" and (
            row["task_plan_sha256"] != canonical_task_plan_digest([]) or row["task_plan_count"] != 0
        ):
            raise ValueError("stream completion requires its sealed empty score plan")
        outcome = (
            "stopped"
            if row["state"] == "stop_requested"
            else ("failed" if state["failure_reason"] else "completed")
        )
        report = validate_mode_stream_report(
            {
                "schema": "lab.mode-stream-report.v1",
                "run_id": str(run_id),
                "purpose": "mode-stream",
                "program_version": "mode-stream.v1",
                "status": outcome,
                "scoring_available": False,
                "candidate_derived": True,
                "input_sha256": plan.input_sha256,
                "plan_sha256": plan.sha256,
                "configuration_sha256": plan.configuration_sha256,
                "candidate_sha256": plan.candidate_sha256,
                "model_sha256": fit["model_sha256"] if fit else None,
                "fit_artifact_sha256": fit["fit_artifact_sha256"] if fit else None,
                "total_rows": plan.total_rows,
                "committed_rows": state["committed_rows"],
                "committed_chunks": len(state["chunks"]),
                "chunk_sha256": [item["artifact_sha256"] for item in state["chunks"]],
                "complete_input": state["committed_rows"] == plan.total_rows,
                "failure_reason": state["failure_reason"],
                "admitted_generation": admitted_generation,
                "execution_sha256": execution_sha256,
            }
        )
        digest, now = hashlib.sha256(canonical_bytes(report)).hexdigest(), datetime.now(UTC)
        connection.execute(
            insert(reports).values(
                run_id=run_id, report_json=report, report_sha256=digest, verified_at=now
            )
        )
        changed = connection.execute(
            update(runs)
            .where(runs.c.run_id == run_id, runs.c.state == row["state"])
            .values(
                state=outcome,
                stop_requested=False,
                report_sha256=digest,
                updated_at=now,
            )
        ).rowcount
        if changed != 1:
            raise ValueError("stream state changed during report publication")
        return report, digest
