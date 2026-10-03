"""Bounded, read-only experiment metadata; this view grants no training authority."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar
from uuid import UUID

from sqlalchemy import select, text

from lab.api.mode_experiments import canonical_document
from lab.db.schema import runs
from lab.director.fake_llm import ProviderReceipt
from lab.director.history_context import (
    PriorDevFinding,
    PriorExperienceSelection,
    PriorFindingsSnapshot,
    load_frozen_prior_findings,
    snapshot_bytes,
)
from lab.director.ledger import canonical_json_bytes
from lab.operating_modes import ModeConfig
from lab.reporting import _parsed_canonical_object, read_run_pairs
from lab.scorer.jobs import read_artifact_bytes

MAX_RECORDS = 200
MAX_ARTIFACT_BYTES = 2 * 1024**2
MAX_READ_BYTES = 16 * 1024**2
MAX_RESPONSE_BYTES = 512 * 1024
READ_SECONDS = 10
_Value = TypeVar("_Value")
_PROTECTED = re.compile(r"holdout|sealed|redacted|\bREB\b", re.IGNORECASE)
_REVIEW_REASONS = ["export-permission-not-checked", "clean-runtime-review-not-checked"]


class ExperienceLimitError(ValueError):
    """Evidence exceeds this metadata view's fixed bounds."""


class ExperienceTimeoutError(TimeoutError):
    """The one read deadline expired."""


class ExperienceReader:
    """Scope all queries and opaque blob verification to one bounded read."""

    def __init__(self, engine: Any, artifact_root: Path):
        self.engine = engine
        self.artifact_root = artifact_root
        self.deadline = time.monotonic() + READ_SECONDS
        self.read_bytes = 0
        self.artifacts: dict[str, bytes] = {}
        self.receipts: dict[str, dict[str, Any]] = {}

    def check(self) -> None:
        if time.monotonic() >= self.deadline:
            raise ExperienceTimeoutError("experience read deadline exceeded")

    def charge(self, size: int) -> None:
        self.check()
        self.read_bytes += size
        if self.read_bytes > MAX_READ_BYTES:
            raise ExperienceLimitError("experience aggregate read bound exceeded")

    def artifact(self, digest: str) -> bytes:
        self.check()
        if digest not in self.artifacts:
            payload = read_artifact_bytes(
                digest, artifact_root=self.artifact_root, max_bytes=MAX_ARTIFACT_BYTES
            )
            self.charge(len(payload))
            self.artifacts[digest] = payload
        return self.artifacts[digest]

    @contextmanager
    def connect(self) -> Iterator[_Connection]:
        self.check()
        with self.engine.connect() as connection, connection.begin():
            if self.engine.dialect.name == "postgresql":
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            yield _Connection(self, connection)
        self.check()


class _Connection:
    def __init__(self, reader: ExperienceReader, connection: Any):
        self.reader, self.connection = reader, connection

    def execute(self, statement: Any, parameters: Any = None) -> _Result:
        self.reader.check()
        if self.reader.engine.dialect.name == "postgresql":
            milliseconds = max(1, int((self.reader.deadline - time.monotonic()) * 1000))
            self.connection.exec_driver_sql(f"SET LOCAL statement_timeout = {milliseconds}")
        result = self.connection.execute(statement, parameters or {})
        self.reader.check()
        return _Result(self.reader, result)


class _Result:
    def __init__(self, reader: ExperienceReader, result: Any) -> None:
        self.reader, self.result = reader, result

    def mappings(self) -> _Result:
        return _Result(self.reader, self.result.mappings())

    def _checked(self, value: _Value) -> _Value:
        self.reader.charge(len(json.dumps(value, default=str, allow_nan=False).encode()))
        return value

    def scalar_one(self) -> Any:
        value = self._checked(self.result.scalar_one())
        if isinstance(value, dict) and isinstance(value.get("experiment_id"), str):
            prior = self.reader.receipts.setdefault(value["experiment_id"], value)
            if prior != value:
                raise ValueError("immutable experiment receipt changed")
        return value

    def first(self) -> dict[str, Any] | None:
        value = self.result.first()
        return self._checked(dict(value)) if value is not None else None

    def all(self) -> list[dict[str, Any]]:
        values = self.result.all()
        if len(values) > MAX_RECORDS:
            raise ExperienceLimitError("experience record bound exceeded")
        return self._checked([dict(value) for value in values])


def _configuration(payload: bytes) -> ModeConfig | None:
    """Recognize only the exact trusted operating-mode source template, without execution."""
    from lab.operating_modes import ModeConfig, candidate_source

    prefix = (
        b"from lab.operating_modes import ModeConfig, OperatingModeCandidate\n\n"
        b"def build_candidate():\n"
        b"    return OperatingModeCandidate(ModeConfig.model_validate_json(\n        "
    )
    if len(payload) > 8192 or not payload.startswith(prefix) or not payload.endswith(b"))\n"):
        return None
    try:
        raw = ast.literal_eval(payload[len(prefix) : -3].decode())
        if not isinstance(raw, str):
            return None
        config = ModeConfig.model_validate_json(raw, strict=True)
    except (ValueError, SyntaxError):
        return None
    return config if candidate_source(config) == payload else None


def _method(payload: bytes) -> str | None:
    config = _configuration(payload)
    return config.method if config is not None else None


def _model_status(experiment: Any, trajectory: Any, cpu: bool) -> str:
    if cpu:
        return "not-applicable-cpu"
    if trajectory.adapter != "vllm-local" or trajectory.model_id.startswith(
        ("fake", "fixture", "operating-mode", "parameter-grid")
    ):
        return "fixture-or-unverified"
    if trajectory.provider_receipt is None:
        return "absent"
    try:
        raw = canonical_json_bytes(trajectory.provider_receipt)
        receipt = ProviderReceipt.model_validate_json(raw, strict=True)
    except ValueError:
        return "invalid"
    if (
        hashlib.sha256(raw).hexdigest() != experiment.provider_receipt_sha256
        or receipt.model_id != trajectory.model_id
        or receipt.actual_system != experiment.system
        or receipt.context_sha256 != trajectory.inputs_sha256
        or trajectory.inputs_sha256 != experiment.inputs_sha256
        or receipt.input_tokens != experiment.llm_input_tokens
        or receipt.output_tokens != experiment.llm_output_tokens
    ):
        return "identity-mismatch"
    # A reported identity is not independently reviewed runtime or cleaning evidence.
    return "reported-local-identity-matched"


def read_run_request(reader: ExperienceReader, run_id: UUID) -> dict[str, Any]:
    """Check the immutable request digest before exposing or loading context refs."""
    with reader.connect() as connection:
        row = (
            connection.execute(
                select(runs.c.request_json, runs.c.payload_sha256).where(runs.c.run_id == run_id)
            )
            .mappings()
            .first()
        )
    if row is None or not isinstance(row["request_json"], dict):
        raise ValueError("immutable run request unavailable")
    request = row["request_json"]
    raw = json.dumps(
        {k: v for k, v in request.items() if k != "idempotency_key"},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    if hashlib.sha256(raw).hexdigest() != row["payload_sha256"]:
        raise ValueError("immutable request integrity check failed")
    return {"request": request, "sha256": row["payload_sha256"]}


def history_finding(reader: ExperienceReader, pair: Any) -> PriorDevFinding:
    """Verify measured source facts and complete positive dev/replay/profile coverage."""
    exp, trajectory = pair.document, pair.trajectory
    if exp.inputs_sha256 != trajectory.inputs_sha256:
        raise ValueError("dev-context-binding-unavailable")
    if (
        exp.kind != "proposal"
        or exp.status != "scored"
        or exp.suite_score is None
        or exp.infrastructure_stop is not None
        or not exp.per_task
        or not exp.guards
        or any(v != "pass" for v in exp.guards.values())
        or exp.decision is None
        or exp.decision.verdict not in {"KEEP", "KEEP_SIMPLER", "DISCARD"}
    ):
        raise ValueError("not-measured-guarded-dev-proposal")
    if not trajectory.source_provenance or any(
        any(_PROTECTED.search(value) for value in source.model_dump().values())
        for source in trajectory.source_provenance
    ):
        raise ValueError("provenance-not-approved-for-history")
    config = _configuration(reader.artifact(exp.candidate_blob_sha256))
    if config is None:
        raise ValueError("method-not-approved-for-history")
    primary = next((r for r in trajectory.replay_manifests if r.decision_stage == "primary"), None)
    if primary is None or set(primary.task_ids) != {item.task_id for item in exp.per_task}:
        raise ValueError("dev-replay-coverage-unavailable")
    profiles = dict(zip(primary.task_ids, primary.profile_sha256, strict=True))
    if any(
        dict(zip(r.task_ids, r.profile_sha256, strict=True)) != profiles
        or r.candidate_sha256 != exp.candidate_sha256
        or r.harness_sha256 != exp.harness_sha256
        or r.image_sha256 != exp.image_sha256
        or r.calibration_sha256 != exp.calibration_sha256
        or r.suite_id != exp.suite_id
        or r.suite_version != exp.suite_version
        for r in trajectory.replay_manifests
    ):
        raise ValueError("dev-replay-coverage-unavailable")
    seeds = {seed for replay in trajectory.replay_manifests for seed in replay.decision_seeds}
    expected = {
        (task, "primary" if seed == 0 else "confirmation", seed)
        for task in profiles
        for seed in seeds
    }
    if not expected or len(expected) > 128:
        raise ValueError("dev-replay-coverage-unavailable")
    with reader.connect() as connection:
        rows = (
            connection.execute(
                text("""SELECT task_id, evaluation_kind, seed, dataset_id,
            split_id, session_id, profile_sha256, candidate_sha256, harness_sha256
            FROM lab.dev_task_results WHERE run_id=:run_id AND experiment_id=:experiment_id
              AND candidate_sha256=:candidate_sha256 AND harness_sha256=:harness_sha256
              AND evaluation_kind IN ('primary','confirmation')
              AND seed IN (0,1)
            ORDER BY task_id,evaluation_kind,seed LIMIT 129"""),
                {
                    "run_id": exp.run_id,
                    "experiment_id": exp.experiment_id,
                    "candidate_sha256": exp.candidate_sha256,
                    "harness_sha256": exp.harness_sha256,
                },
            )
            .mappings()
            .all()
        )
    observed = {(r["task_id"], r["evaluation_kind"], r["seed"]) for r in rows}
    source_ids = {(s.dataset_id, s.split_id, s.session_id) for s in trajectory.source_provenance}
    row_sources = {(r["dataset_id"], r["split_id"], r["session_id"]) for r in rows}
    if (
        observed != expected
        or len(rows) != len(expected)
        or row_sources != source_ids
        or any(r["profile_sha256"] != profiles[r["task_id"]] for r in rows)
    ):
        raise ValueError("dev-profile-provenance-coverage-unavailable")
    receipt = reader.receipts[exp.experiment_id]
    provenance = sorted(
        (s.model_dump(mode="json") for s in trajectory.source_provenance),
        key=lambda s: (s["dataset_id"], s["split_id"], s["session_id"]),
    )
    return PriorDevFinding(
        experiment_id=exp.experiment_id,
        experiment_sha256=receipt["experiment_sha256"],
        trajectory_sha256=receipt["trajectory_sha256"],
        candidate_sha256=exp.candidate_sha256,
        configuration=config,
        configuration_sha256=hashlib.sha256(
            canonical_json_bytes(config.model_dump(mode="json"))
        ).hexdigest(),
        method=config.method,
        dev_suite_score=exp.suite_score,
        decision=exp.decision.verdict,
        source_harness_sha256=exp.harness_sha256,
        source_image_sha256=exp.image_sha256,
        dev_task_scope_sha256=hashlib.sha256(canonical_document(rows)).hexdigest(),
        provenance_sha256=hashlib.sha256(canonical_document(provenance)).hexdigest(),
        provenance_manifest_sha256s=tuple(
            sorted({s.source_manifest_sha256 for s in trajectory.source_provenance})
        ),
    )


def freeze_prior_findings(
    reader: ExperienceReader,
    selection: PriorExperienceSelection,
    verified: dict[str, Any],
    source_request: dict[str, Any],
) -> PriorFindingsSnapshot:
    """Select source identities after the principal/report gate, never accept client findings."""
    if (
        verified["run_id"] != selection.source_run_id
        or verified["report_sha256"] != selection.source_report_sha256
    ):
        raise ValueError("selected source report identity differs")
    pairs = read_run_pairs(
        reader,
        UUID(selection.source_run_id),
        artifact_root=reader.artifact_root,
        max_records=MAX_RECORDS,
        artifact_reader=reader.artifact,
    )
    by_id = {pair.document.experiment_id: pair for pair in pairs}
    findings = []
    for ref in selection.records:
        pair = by_id.get(ref.experiment_id)
        if pair is None:
            raise ValueError("selected source record unavailable")
        finding = history_finding(reader, pair)
        if (
            finding.experiment_sha256 != ref.experiment_sha256
            or finding.trajectory_sha256 != ref.trajectory_sha256
        ):
            raise ValueError("selected source record identity differs")
        findings.append(finding)
    snapshot = PriorFindingsSnapshot(
        schema="prior-dev-findings.v1",
        source_run_id=selection.source_run_id,
        source_report_sha256=selection.source_report_sha256,
        source_request_sha256=source_request["sha256"],
        source_suite_manifest_sha256=source_request["request"]["suite_manifest_sha256"],
        scope="historical-advisory-only",
        records=tuple(findings),
    )
    snapshot_bytes(snapshot)
    reader.check()
    return snapshot


def build_experience(
    reader: ExperienceReader,
    run_id: UUID,
    verified: dict[str, Any],
    source_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Expose allowlisted dev metadata after the authenticated terminal report gate."""
    reader.check()
    report = verified["report"]
    provider = report.get("provider")
    purpose = "mode-grid" if provider == "mode-grid" else report.get("purpose", "research")
    purpose = (
        purpose if purpose in {"mode-grid", "mode-stream", "baseline", "research"} else "research"
    )
    cpu = purpose in {"mode-grid", "mode-stream"}
    pairs = read_run_pairs(
        reader,
        run_id,
        artifact_root=reader.artifact_root,
        max_records=MAX_RECORDS,
        artifact_reader=reader.artifact,
    )
    records = []
    protected_excluded = False
    for pair in pairs:
        reader.check()
        exp, trajectory = pair.document, pair.trajectory
        receipt = reader.receipts[exp.experiment_id]
        protected = any(
            any(_PROTECTED.search(value) for value in source.model_dump().values())
            for source in trajectory.source_provenance
        )
        if protected:
            protected_excluded = True
            continue
        sources = trajectory.source_provenance
        record_cpu = cpu or (
            exp.kind == "baseline" and exp.llm_input_tokens == 0 and exp.llm_output_tokens == 0
        )
        status = _model_status(exp, trajectory, record_cpu)
        reasons = list(_REVIEW_REASONS)
        if exp.kind != "proposal":
            reasons.insert(0, "not_proposal")
        elif (
            exp.status != "scored"
            or exp.decision is None
            or exp.decision.verdict != "KEEP"
            or not exp.per_task
            or exp.suite_score is None
            or exp.infrastructure_stop is not None
            or not exp.guards
            or any(value != "pass" for value in exp.guards.values())
        ):
            reasons.insert(0, "not_confirmed_positive_candidate")
        if status != "reported-local-identity-matched":
            reasons.insert(0, "cpu-not-local-llm" if record_cpu else "model-receipt-" + status)
        if not sources:
            reasons.append("dataset-provenance-unavailable")
        history_reasons = ["source-request-not-verified"]
        if source_request is not None and re.fullmatch(
            r"[a-f0-9]{64}", str(source_request["request"].get("suite_manifest_sha256", ""))
        ):
            try:
                history_finding(reader, pair)
                history_reasons = []
            except ValueError as error:
                if isinstance(error, ExperienceLimitError):
                    raise
                # Only fixed verifier codes may be returned, never raw parser/path errors.
                allowed = {
                    "not-measured-guarded-dev-proposal",
                    "provenance-not-approved-for-history",
                    "method-not-approved-for-history",
                    "dev-replay-coverage-unavailable",
                    "dev-profile-provenance-coverage-unavailable",
                    "dev-context-binding-unavailable",
                }
                history_reasons = [
                    str(error) if str(error) in allowed else "history-proof-unavailable"
                ]
        verdict = exp.decision.verdict if exp.decision is not None else None
        records.append(
            {
                "record_id": trajectory.trajectory_id,
                "experiment_id": exp.experiment_id,
                "sequence": pair.sequence,
                "kind": exp.kind,
                "status": exp.status,
                "method": exp.baseline_name or _method(reader.artifact(exp.candidate_blob_sha256)),
                "move": exp.move_type,
                "decision": verdict,
                "reason": "referee-" + verdict.lower() if verdict else None,
                "score": exp.suite_score,
                "score_kind": "dev-suite",
                "model_id": None if record_cpu else trajectory.model_id,
                "model_receipt_status": status,
                "experiment_sha256": receipt["experiment_sha256"],
                "trajectory_sha256": receipt["trajectory_sha256"],
                "provenance_summary": {
                    "source_count": len(sources),
                    "manifest_sha256s": sorted(
                        {source.source_manifest_sha256 for source in sources}
                    ),
                    "usage_profile": "noncommercial_research",
                },
                "training_eligibility": {"eligible": False, "reasons": reasons},
                "history_eligibility": {
                    "eligible": not history_reasons,
                    "reasons": history_reasons,
                },
            }
        )
    if not pairs and purpose == "mode-stream":
        records.append(
            {
                "record_id": "report:" + verified["report_sha256"],
                "sequence": 0,
                "kind": "cpu-mode-stream",
                "status": report["status"],
                "method": None,
                "move": None,
                "decision": None,
                "reason": "cpu-dataset-evidence",
                "score": None,
                "score_kind": "not-scored",
                "model_id": None,
                "model_receipt_status": "not-applicable-cpu",
                "provenance_summary": {
                    "source_count": 0,
                    "manifest_sha256s": [],
                    "usage_profile": "noncommercial_research",
                },
                "history_eligibility": {"eligible": False, "reasons": ["no-experiment-trajectory"]},
                "training_eligibility": {
                    "eligible": False,
                    "reasons": ["cpu-not-local-llm", "no-experiment-trajectory", *_REVIEW_REASONS],
                },
            }
        )
    result = {
        "schema": "run-experience.v1",
        "run_id": str(run_id),
        "report_sha256": verified["report_sha256"],
        "purpose": purpose,
        "verification": "report-and-ledger-hash-verified",
        "record_limit": MAX_RECORDS,
        "records": records,
        "feedback": {"same_run_recent_limit": 30, "cross_run_reuse": False},
        "training_started": False,
        "holdout_included": False,
        "protected_records_excluded": protected_excluded,
    }
    if source_request is not None:
        from lab.director.field_context import load_frozen_field_context

        field_context = load_frozen_field_context(source_request["request"])
        if field_context is not None:
            from lab.director.fake_llm import AgentContext

            for record in records:
                record["training_eligibility"]["reasons"].append(
                    "field_intent_permission_not_reviewed"
                )
            field_bound_count = 0
            for pair in pairs:
                if pair.document.kind != "proposal":
                    continue
                if pair.document.inputs_sha256 != pair.trajectory.inputs_sha256:
                    raise ValueError("proposal/trajectory field context identity differs")
                raw = reader.artifact(pair.document.inputs_sha256)
                _parsed_canonical_object(raw)
                context = AgentContext.model_validate_json(raw, strict=True)
                if (
                    context.field_context != field_context
                    or context.field_context_sha256 != field_context.sha256
                ):
                    raise ValueError("proposal field context differs from frozen admission")
                field_bound_count += 1
            result["field_context_usage"] = {
                "field_context_sha256": field_context.sha256,
                "snapshot_sha256": field_context.snapshot_sha256,
                "intent": field_context.intent.model_dump(mode="json"),
                "asset_identity": field_context.asset_identity,
                "use": field_context.use,
                "status": "context-bound" if field_bound_count else "admitted-only",
                "context_bound_proposal_count": field_bound_count,
            }
        snapshot = load_frozen_prior_findings(source_request["request"])
        if snapshot is not None:
            from lab.director.fake_llm import AgentContext

            bound_count = 0
            expected_sha = hashlib.sha256(snapshot_bytes(snapshot)).hexdigest()
            for pair in pairs:
                if pair.document.kind != "proposal":
                    continue
                if pair.document.inputs_sha256 != pair.trajectory.inputs_sha256:
                    raise ValueError("proposal/trajectory context identity differs")
                raw = reader.artifact(pair.document.inputs_sha256)
                _parsed_canonical_object(raw)
                context = AgentContext.model_validate_json(raw, strict=True)
                if (
                    context.prior_findings != snapshot
                    or context.prior_findings_sha256 != expected_sha
                ):
                    raise ValueError("proposal context differs from frozen admitted history")
                bound_count += 1
            result["prior_findings_usage"] = {
                "snapshot_sha256": expected_sha,
                "source_run_id": snapshot.source_run_id,
                "source_report_sha256": snapshot.source_report_sha256,
                "record_count": len(snapshot.records),
                "context_bound_proposal_count": bound_count,
                "status": "context-bound" if bound_count else "admitted-only",
                "scope": "historical-advisory-only",
            }
    if len(canonical_json_bytes(result)) > MAX_RESPONSE_BYTES:
        raise ExperienceLimitError("experience response bound exceeded")
    reader.check()
    return result
