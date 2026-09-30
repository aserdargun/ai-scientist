"""Fail-closed local SFT packages from verified ledger pairs and reviewed permissions.

Policy files are trusted operator input, never model output. They attest review of
actual runtime evidence and supply cleaned messages; this exporter cannot infer
licensing or human/private-data clearance from a model receipt alone.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import hmac
import json
import os
import re
import stat
import sys
from collections import Counter
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr
from sqlalchemy import Engine, create_engine, text

from lab.director.artifacts import read_director_artifact
from lab.director.contracts import ExperimentDocument, TrajectoryDocument
from lab.director.fake_llm import ProviderAttemptTranscript, ProviderReceipt, prompt_messages_sha256
from lab.director.local_llm import LocalQwenProposalProvider
from lab.reporting import read_run_pairs

Digest = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
License = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,127}$")]
RULES_VERSION = "local-sft-reviewed.v1"
MAX_RECORDS = 10_000
MAX_PACKAGE_BYTES = 64 * 1024**2


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class CleanMessage(StrictModel):
    role: Literal["system", "user", "assistant"]
    content: Annotated[StrictStr, Field(min_length=1, max_length=262_144)]


class Permission(StrictModel):
    """Explicit training permission for one immutable source/model/code identity."""

    identity_sha256: Digest
    license_id: License
    training_allowed: StrictBool
    usage_profile: Literal["noncommercial_research"]
    permission_evidence_sha256: Digest


class RecordReview(StrictModel):
    """Hash-bound operator review; original ledger scrub flags are never changed."""

    trajectory_sha256: Digest
    messages_blob_sha256: Digest
    runtime_evidence_sha256: Digest
    cleaning_evidence_sha256: Digest
    origin: Literal["real_local_llm", "fixture", "fake", "unknown"]
    secrets_scrubbed: StrictBool
    people_scrubbed: StrictBool
    raw_values_scrubbed: StrictBool
    revoked_or_rolled_back: StrictBool
    redactions: tuple[Annotated[StrictStr, Field(min_length=4, max_length=1_000)], ...] = ()
    clean_messages: tuple[CleanMessage, ...] = Field(min_length=3, max_length=3)


class ExportPolicy(StrictModel):
    schema_version: Literal["training-export-policy.v1"] = Field(alias="schema")
    target: Literal["local_noncommercial_sft"]
    split_secret: Digest  # Private HMAC key; never placed in exported manifest.
    eval_fraction: Annotated[float, Field(gt=0.0, lt=1.0, allow_inf_nan=False)] = 0.2
    data_permissions: tuple[Permission, ...] = ()
    model_permissions: tuple[Permission, ...] = ()
    code_permissions: tuple[Permission, ...] = ()
    reviews: tuple[RecordReview, ...] = ()


class VerifiedRecord(StrictModel):
    """Internal adapter input. Public export entry point obtains it from the ledger."""

    experiment: ExperimentDocument
    trajectory: TrajectoryDocument
    trajectory_sha256: Digest
    transcript: dict[str, Any] | list[Any]


class ReviewedEvidence(StrictModel):
    """Operator-reviewed, hash-bound private evidence; never exported as raw text."""

    schema_version: Literal["training-reviewed-evidence.v1"] = Field(alias="schema")
    kind: Literal[
        "runtime", "cleaning", "dataset_permission", "model_permission", "code_permission"
    ]
    subject_sha256: Digest
    usage_profile: Literal["noncommercial_research"]
    origin: Literal["real_local_llm", "fixture", "fake", "unknown"] = "unknown"
    training_allowed: StrictBool = False
    license_id: License | None = None
    provider_receipt_sha256: Digest | None = None
    observer_receipt_sha256: Digest | None = None
    secrets_scrubbed: StrictBool = False
    people_scrubbed: StrictBool = False
    raw_values_scrubbed: StrictBool = False
    clean_messages_sha256: Digest | None = None
    redactions_sha256: Digest | None = None
    revoked_or_rolled_back: StrictBool = False


def validate_review_evidence(
    records: tuple[VerifiedRecord, ...], policy: ExportPolicy, review_root: Path
) -> None:
    """Require actual immutable proof bytes, not bare hashes claiming review."""

    def evidence(identity: str) -> ReviewedEvidence:
        payload = read_director_artifact(identity, artifact_root=review_root)
        if digest(payload) != identity:
            raise ValueError("review evidence digest mismatch")
        return ReviewedEvidence.model_validate_json(payload, strict=True)

    for kind, permissions in [
        ("dataset_permission", policy.data_permissions),
        ("model_permission", policy.model_permissions),
        ("code_permission", policy.code_permissions),
    ]:
        for permission in permissions:
            proof = evidence(permission.permission_evidence_sha256)
            if (
                proof.kind != kind
                or proof.subject_sha256 != permission.identity_sha256
                or proof.training_allowed != permission.training_allowed
                or proof.license_id != permission.license_id
            ):
                raise ValueError("permission review is unbound")
    by_digest = {record.trajectory_sha256: record for record in records}
    for review in policy.reviews:
        record = by_digest.get(review.trajectory_sha256)
        if record is None:
            raise ValueError("review references a record outside the requested ledger set")
        runtime = evidence(review.runtime_evidence_sha256)
        cleaning = evidence(review.cleaning_evidence_sha256)
        if (
            runtime.kind != "runtime"
            or runtime.subject_sha256 != record.trajectory_sha256
            or runtime.origin != review.origin
            or runtime.provider_receipt_sha256 != record.experiment.provider_receipt_sha256
            or runtime.observer_receipt_sha256 is None
        ):
            raise ValueError("runtime review is unbound")
        observed = read_director_artifact(
            runtime.observer_receipt_sha256, artifact_root=review_root
        )
        if digest(observed) != runtime.observer_receipt_sha256:
            raise ValueError("private observer proof is missing")
        if (
            cleaning.kind != "cleaning"
            or cleaning.subject_sha256 != record.trajectory_sha256
            or cleaning.secrets_scrubbed != review.secrets_scrubbed
            or cleaning.people_scrubbed != review.people_scrubbed
            or cleaning.raw_values_scrubbed != review.raw_values_scrubbed
            or cleaning.clean_messages_sha256
            != digest(canonical([m.model_dump() for m in review.clean_messages]))
            or cleaning.redactions_sha256 != digest(canonical(list(review.redactions)))
            or cleaning.revoked_or_rolled_back != review.revoked_or_rolled_back
        ):
            raise ValueError("cleaning review is unbound")


def _unique(items: tuple[Any, ...], attribute: str) -> dict[str, Any]:
    result = {getattr(item, attribute): item for item in items}
    if len(result) != len(items):
        raise ValueError("duplicate policy identity")
    return result


def _opaque(policy: ExportPolicy, value: str) -> str:
    return hmac.new(bytes.fromhex(policy.split_secret), value.encode(), hashlib.sha256).hexdigest()


def _clean_text(content: str, private_values: set[str]) -> bool:
    # Secondary rejection, not a substitute for the required cleaning review.
    forbidden = re.compile(
        r"(?i)(?:postgres(?:ql)?://|bearer\s+\S+|(?:api[_-]?key|password|secret|token)\s*[:=]\s*\S+"
        r"|[\w.+-]+@[\w.-]+\.[a-z]{2,}|(?:/home/|/run/user/|/proc/|/etc/|/data/runtime/)"
        r"|(?:holdout|sealed|\bREB\b)|(?:\blabels?\b\s*[:=]\s*\[))"
    )
    if forbidden.search(content):
        return False
    return not any(
        len(value) >= 8 and re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", content)
        for value in private_values
    )


def _eligible(record: VerifiedRecord, policy: ExportPolicy) -> tuple[dict[str, Any] | None, str]:
    exp, trajectory = record.experiment, record.trajectory
    if exp.kind != "proposal":
        return None, "not_proposal"
    if (
        exp.status != "scored"
        or exp.decision is None
        or exp.decision.verdict != "KEEP"
        or not exp.per_task
        or exp.suite_score is None
        or exp.infrastructure_stop is not None
        or not exp.guards
        or any(value != "pass" for value in exp.guards.values())
    ):
        return None, "not_confirmed_positive_candidate"
    if (
        trajectory.provider_receipt is None
        or trajectory.adapter != "vllm-local"
        or trajectory.model_id.startswith(("fake", "fixture", "operating-mode", "parameter-grid"))
    ):
        return None, "not_local_llm"
    try:
        receipt = ProviderReceipt.model_validate_json(
            canonical(trajectory.provider_receipt), strict=True
        )
    except ValueError:
        return None, "invalid_provider_receipt"
    if (
        digest(canonical(trajectory.provider_receipt)) != exp.provider_receipt_sha256
        or receipt.model_id != trajectory.model_id
        or receipt.actual_system != exp.system
        or receipt.context_sha256 != trajectory.inputs_sha256
        or trajectory.inputs_sha256 != exp.inputs_sha256
        or receipt.input_tokens != exp.llm_input_tokens
        or receipt.output_tokens != exp.llm_output_tokens
    ):
        return None, "provider_identity_mismatch"
    completed = receipt.attempts[-1]
    if (
        completed.outcome != "completed"
        or completed.unit is None
        or re.fullmatch(r"swapp-lab-gpu-turn-[0-9a-f]{32}\.service", completed.unit) is None
        or completed.invocation_id is None
        or completed.main_pid is None
        or completed.main_start_ticks is None
        or completed.control_group is None
        or completed.inference_seconds is None
        or completed.inference_seconds <= 0
        or completed.peak_gpu_memory_mib is None
        or completed.peak_gpu_memory_mib <= 0
    ):
        return None, "missing_actual_runtime_identity"
    review = _unique(policy.reviews, "trajectory_sha256").get(record.trajectory_sha256)
    if review is None:
        return None, "missing_cleaning_and_runtime_review"
    if (
        review.origin != "real_local_llm"
        or review.messages_blob_sha256 != trajectory.messages_blob_sha256
    ):
        return None, "fixture_or_unbound_review"
    if review.revoked_or_rolled_back:
        return None, "revoked_or_rolled_back"
    if not (review.secrets_scrubbed and review.people_scrubbed and review.raw_values_scrubbed):
        return None, "incomplete_cleaning_review"
    if not trajectory.source_provenance:
        return None, "missing_dataset_provenance"
    data_permissions = _unique(policy.data_permissions, "identity_sha256")
    provenance = []
    for source in trajectory.source_provenance:
        if re.search(r"(?i)(holdout|sealed|\bREB\b)", source.split_id):
            return None, "protected_split"
        grant = data_permissions.get(source.source_manifest_sha256)
        if (
            grant is None
            or not grant.training_allowed
            or grant.license_id != source.license_id
            or grant.usage_profile != source.usage_profile
        ):
            return None, "dataset_training_permission_missing"
        provenance.append(
            {
                "source_manifest_sha256": source.source_manifest_sha256,
                "source_revision_sha256": digest(source.source_revision.encode()),
                "license_id": grant.license_id,
                "usage_profile": grant.usage_profile,
                "permission_evidence_sha256": grant.permission_evidence_sha256,
            }
        )
    model = _unique(policy.model_permissions, "identity_sha256").get(receipt.model_sha256)
    code = _unique(policy.code_permissions, "identity_sha256").get(exp.candidate_sha256)
    if model is None or not model.training_allowed:
        return None, "model_training_permission_missing"
    if code is None or not code.training_allowed:
        return None, "code_training_permission_missing"
    if not isinstance(record.transcript, dict):
        return None, "unsupported_local_transcript_envelope"
    try:
        if digest(canonical(record.transcript)) != trajectory.messages_blob_sha256:
            return None, "transcript_blob_mismatch"
        transcripts = tuple(
            ProviderAttemptTranscript.model_validate_json(canonical(item), strict=True)
            for item in record.transcript.get("provider_attempts", [])
        )
        final = transcripts[-1]
        if (
            digest(final.response_text.encode()) != final.response_sha256
            or final.response_sha256 != completed.response_sha256
            or prompt_messages_sha256([message.model_dump() for message in final.prompt_messages])
            != final.prompt_sha256
            or final.prompt_sha256 != completed.prompt_sha256
            or final.profile_id != completed.profile_id
            or final.profile_id != receipt.profile_id
            or final.attempt_kind != completed.attempt_kind
            or final.response_schema_sha256 != completed.response_schema_sha256
        ):
            return None, "transcript_provider_hash_mismatch"
        original = LocalQwenProposalProvider._parse_proposal(final.response_text, exp.move_type)
        if digest(original.candidate_source.encode()) != exp.candidate_sha256:
            return None, "response_candidate_mismatch"
        if original.hypothesis != exp.hypothesis or original.predicted_delta != exp.predicted_delta:
            return None, "response_proposal_identity_mismatch"
        if [message.role for message in review.clean_messages] != ["system", "user", "assistant"]:
            return None, "clean_message_roles_invalid"
        if [message.role for message in final.prompt_messages] != ["system", "user"]:
            return None, "unsupported_prompt_shape"
        # Exported text must be the observed turn with explicit deterministic redactions,
        # never an invented replacement response or a copied journal.
        expected = [message.content for message in final.prompt_messages] + [final.response_text]
        for value in sorted(set(review.redactions), key=len, reverse=True):
            expected = [content.replace(value, "[REDACTED]") for content in expected]
        if [message.content for message in review.clean_messages] != expected:
            return None, "clean_messages_not_observed_redaction"
        cleaned = LocalQwenProposalProvider._parse_proposal(
            review.clean_messages[-1].content, exp.move_type
        )
        normal_code = ast.dump(ast.parse(cleaned.candidate_source), include_attributes=False)
    except (ValueError, IndexError, TypeError, SyntaxError):
        return None, "missing_or_invalid_attempt_transcript"
    private_values = {
        str(exp.run_id),
        exp.experiment_id,
        trajectory.trajectory_id,
        exp.suite_id,
        completed.unit,
        completed.control_group,
    }
    private_values.update(task.task_id for task in exp.per_task)
    for source in trajectory.source_provenance:
        private_values.update((source.dataset_id, source.split_id, source.session_id))
    if any(not _clean_text(message.content, private_values) for message in review.clean_messages):
        return None, "unclean_export_content"
    messages = [message.model_dump() for message in review.clean_messages]
    return {
        "messages": messages,
        "code_key": digest(normal_code.encode()),
        "content_sha256": digest(canonical(messages)),
        "source_provenance": provenance,
        "model_sha256": receipt.model_sha256,
        "model_license_id": model.license_id,
        "model_revision_sha256": digest(receipt.model_revision.encode()),
        "model_permission_evidence_sha256": model.permission_evidence_sha256,
        "candidate_sha256": exp.candidate_sha256,
        "code_license_id": code.license_id,
        "code_permission_evidence_sha256": code.permission_evidence_sha256,
        "runtime_evidence_sha256": review.runtime_evidence_sha256,
        "cleaning_evidence_sha256": review.cleaning_evidence_sha256,
    }, "eligible"


def build_package(records: tuple[VerifiedRecord, ...], policy: ExportPolicy) -> dict[str, bytes]:
    """Pure package builder for ledger-verified records; no raw IDs in output."""
    if len(records) > MAX_RECORDS:
        raise ValueError("export record quota exceeded")
    for items, attribute in [
        (policy.reviews, "trajectory_sha256"),
        (policy.data_permissions, "identity_sha256"),
        (policy.model_permissions, "identity_sha256"),
        (policy.code_permissions, "identity_sha256"),
    ]:
        _unique(items, attribute)
    parent: dict[str, str] = {}

    def find(key: str) -> str:
        parent.setdefault(key, key)
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(left: str, right: str) -> None:
        a, b = find(left), find(right)
        parent[max(a, b)] = min(a, b)

    selected = []
    excluded = []
    codes: dict[str, str] = {}
    # Group before dedup/split: duplicate code connects runs even if that row is removed.
    for record in sorted(
        records, key=lambda item: (str(item.experiment.run_id), item.experiment.ordinal)
    ):
        run = "run:" + str(record.experiment.run_id)
        find(run)
        union(run, "experiment:" + record.experiment.experiment_id)
        if record.experiment.parent_experiment_id is not None:
            union(run, "experiment:" + record.experiment.parent_experiment_id)
        for source in record.trajectory.source_provenance:
            # v1 has no trustworthy source time bounds. Keep the entire source
            # together across sessions/revisions rather than infer chronology.
            union(run, "source:" + digest(source.dataset_id.encode()))
            union(run, "manifest:" + source.source_manifest_sha256)
        row, reason = _eligible(record, policy)
        identity = _opaque(
            policy, str(record.experiment.run_id) + ":" + record.experiment.experiment_id
        )
        if row is None:
            excluded.append({"record_id": identity, "reason": reason})
        else:
            if row["code_key"] in codes:
                union(run, codes[row["code_key"]])
                excluded.append({"record_id": identity, "reason": "duplicate_normalized_candidate"})
            else:
                codes[row["code_key"]] = run
                selected.append((run, identity, row))
    outputs: dict[str, list[bytes]] = {"train.jsonl": [], "eval.jsonl": []}
    entries = []
    for run, identity, row in selected:
        group = _opaque(policy, "group:" + find(run))
        split = "eval" if int(group[:16], 16) / 2**64 < policy.eval_fraction else "train"
        outputs[split + ".jsonl"].append(canonical({"messages": row["messages"]}) + b"\n")
        entries.append(
            {
                "record_id": identity,
                "group_id": group,
                "split": split,
                **{key: value for key, value in row.items() if key != "messages"},
            }
        )
    files = {name: b"".join(lines) for name, lines in outputs.items()}
    card = {
        "schema": "local-sft-data-card.v1",
        "rules_version": RULES_VERSION,
        "usage_profile": "noncommercial_research",
        "publication_authorized": False,
        "training_executed": False,
        "included_records": len(entries),
        "licenses": sorted(
            {
                license_id
                for row in entries
                for license_id in [
                    row["model_license_id"],
                    row["code_license_id"],
                    *(source["license_id"] for source in row["source_provenance"]),
                ]
            }
        ),
        "selection": "scored KEEP; observed local runtime; reviewed permissions and cleaning",
        "content": "observed system/user/assistant turn with deterministic reviewed redactions",
        "split": "connected families, complete sources/manifests and normalized candidates",
        "limitations": [
            "Operator review required; receipt shape alone cannot prove a real model run.",
            "Whole-source grouping replaces unavailable v1 time bounds; splits may be empty.",
            "No training, adapter quality, commercial use or publication acceptance established.",
        ],
    }
    files["data-card.json"] = canonical(card) + b"\n"
    manifest = {
        "schema": "local-sft-manifest.v1",
        "rules_version": RULES_VERSION,
        "target": policy.target,
        "usage_profile": "noncommercial_research",
        "publication_authorized": False,
        "training_executed": False,
        "policy_sha256": digest(canonical(policy.model_dump(mode="json", by_alias=True))),
        "input_records": len(records),
        "included_records": len(entries),
        "excluded": excluded,
        "exclusion_counts": dict(Counter(x["reason"] for x in excluded)),
        "records": entries,
        "split_rule": "connected run/family/whole-source/normalized-code groups; private HMAC",
        "split_counts": {
            name.removesuffix(".jsonl"): len(lines) for name, lines in outputs.items()
        },
        "empty_eval": not outputs["eval.jsonl"],
        "empty_train": not outputs["train.jsonl"],
        "files": {
            name: {"sha256": digest(payload), "bytes": len(payload)}
            for name, payload in files.items()
        },
    }
    files["manifest.json"] = canonical(manifest) + b"\n"
    if sum(len(payload) for payload in files.values()) > MAX_PACKAGE_BYTES:
        raise ValueError("export package byte quota exceeded")
    return files


def export_runs(
    engine: Engine,
    run_ids: tuple[UUID, ...],
    *,
    artifact_root: Path,
    policy: ExportPolicy,
    review_root: Path,
    output: Path,
    artifact_layout: Literal["flat", "per-run"] = "flat",
) -> dict[str, bytes]:
    """SELECT-only ledger adapter. Corrupt receipt/artifact aborts the whole export."""
    if not run_ids or len(run_ids) > 100 or len(set(run_ids)) != len(run_ids):
        raise ValueError("export requires 1..100 unique run IDs")
    if artifact_layout not in {"flat", "per-run"}:
        raise ValueError("unsupported artifact layout")
    records: list[VerifiedRecord] = []
    for run_id in run_ids:
        run_artifact_root = (
            artifact_root / str(run_id) if artifact_layout == "per-run" else artifact_root
        )
        for pair in read_run_pairs(engine, run_id, artifact_root=run_artifact_root):
            if len(records) >= MAX_RECORDS:
                raise ValueError("export record quota exceeded")
            with engine.connect() as connection:
                receipt = connection.execute(
                    text("SELECT lab.experiment_record_receipt(:id)"),
                    {"id": pair.document.experiment_id},
                ).scalar_one()
            if not isinstance(receipt, dict):
                raise ValueError("invalid immutable pair receipt")
            payload = read_director_artifact(
                pair.trajectory.messages_blob_sha256, artifact_root=run_artifact_root
            )
            if digest(payload) != pair.trajectory.messages_blob_sha256:
                raise ValueError("transcript hash mismatch")
            transcript = json.loads(payload)
            if not isinstance(transcript, dict | list):
                raise ValueError(
                    "transcript envelope must be an object or historical message array"
                )
            records.append(
                VerifiedRecord(
                    experiment=pair.document,
                    trajectory=pair.trajectory,
                    trajectory_sha256=receipt["trajectory_sha256"],
                    transcript=transcript,
                )
            )
    validate_review_evidence(tuple(records), policy, review_root)
    files = build_package(tuple(records), policy)
    # Fresh directory only: no overwrite, symlink following or partial published package.
    if output.exists() or output.is_symlink() or not output.is_absolute():
        raise ValueError("output must be a fresh absolute directory")
    for ancestor in output.parents:
        if ancestor.is_symlink():
            raise ValueError("output directory chain contains a symlink")
    output.mkdir(mode=0o700)
    try:
        for name, content in files.items():
            fd = os.open(output / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
    except BaseException:
        # Only exact paths created here may be removed; do not delete the directory recursively.
        for name in files:
            path = output / name
            if path.is_file() and not path.is_symlink():
                path.unlink()
        output.rmdir()
        raise
    return files


def _private_file(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("policy/DSN requires an owner-only regular file")
        content = stream.read(8 * 1024**2 + 1)
        if len(content) > 8 * 1024**2:
            raise ValueError("private input exceeds 8 MiB")
        return content


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn-file", type=Path, required=True)
    parser.add_argument("--policy-file", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--artifact-layout", choices=("flat", "per-run"), default="flat")
    parser.add_argument("--review-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", type=UUID, action="append", required=True)
    args = parser.parse_args(argv)
    engine = None
    try:
        policy = ExportPolicy.model_validate_json(_private_file(args.policy_file), strict=True)
        dsn = _private_file(args.dsn_file).decode().strip()
        engine = create_engine(
            dsn,
            hide_parameters=True,
            pool_size=1,
            max_overflow=0,
            pool_timeout=5,
            connect_args={
                "options": "-c default_transaction_read_only=on -c statement_timeout=5000"
            },
        )
        if engine.dialect.name != "postgresql":
            raise ValueError("production export requires PostgreSQL")
        files = export_runs(
            engine,
            tuple(args.run_id),
            artifact_root=args.artifact_root,
            policy=policy,
            review_root=args.review_root,
            output=args.output,
            artifact_layout=args.artifact_layout,
        )
        manifest = json.loads(files["manifest.json"])
        print(
            json.dumps(
                {
                    "included_records": manifest["included_records"],
                    "excluded_records": len(manifest["excluded"]),
                    "manifest_sha256": digest(files["manifest.json"]),
                }
            )
        )
        return 0
    except Exception:
        # Stable failure code only; raw SQL, identities, text and credentials never enter stderr.
        print("training export failed closed", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
