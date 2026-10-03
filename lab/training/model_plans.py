"""CPU-only model and adapter preparation; deliberately contains no execution path."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

Digest = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
Revision = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{40}$")]
Name = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
CATALOG_PATH = Path(__file__).resolve().parents[2] / "ops/model-preparation-catalog.json"
MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_PACKAGE_FILE_BYTES = 64 * 1024**2
TARGET_MODULES = frozenset(
    {"q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"}
)


class PlanDocument(BaseModel):
    """Strict operator input, never an executable model response."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class InferenceArtifact(PlanDocument):
    """Remote artifact metadata; its presence does not assert a local download."""

    repository: StrictStr
    revision: Revision
    filename: Name
    bytes: Annotated[StrictInt, Field(gt=0)]
    sha256: Digest
    backend_bridge: Literal["not_implemented"]
    local_verified: Literal[False]
    full_16gb_gpu_fit: Literal["unsupported", "unmeasured_candidate"]


class TrainingSource(PlanDocument):
    """Separate training base; GGUF cannot be a training base."""

    repository: StrictStr
    revision: Revision | None
    format: Literal["safetensors", "safetensors-bnb4"]
    load_in_4bit: StrictBool
    minimum_vram_gb: Annotated[StrictInt, Field(gt=0)]
    resource_basis: StrictStr


class ModelPreparation(PlanDocument):
    """One finite candidate with independently pinned inference and training sources."""

    model_id: Name
    upstream_repository: StrictStr
    architecture: Literal["qwen3_5", "qwen3"]
    inference: InferenceArtifact
    qlora: TrainingSource
    lora: TrainingSource
    documentation: tuple[StrictStr, ...]


class PreparationCatalog(PlanDocument):
    """Inert catalogue, excluded from the existing runtime provider authority."""

    schema_version: Literal["model-preparation-catalog.v1"] = Field(alias="schema")
    execution_allowed: Literal[False]
    activation_allowed: Literal[False]
    models: tuple[ModelPreparation, ...]


class BaseFile(PlanDocument):
    """A hash-bound inventory item, not a claim that weights were locally verified."""

    name: StrictStr
    bytes: Annotated[StrictInt, Field(gt=0)]
    sha256: Digest


class TrainingBaseIdentity(PlanDocument):
    """Operator-reviewed immutable identity for the distinct training artifact."""

    schema_version: Literal["training-base-identity.v1"] = Field(alias="schema")
    repository: StrictStr
    revision: Revision
    format: Literal["safetensors", "safetensors-bnb4"]
    architecture: Literal["qwen3_5", "qwen3"]
    files: tuple[BaseFile, ...] = Field(min_length=1)
    tokenizer_sha256: Digest
    chat_template_sha256: Digest
    license_id: Name
    license_evidence_sha256: Digest


class AdapterPlanRequest(PlanDocument):
    """All mutable choices contribute to a model-specific adapter identity."""

    schema_version: Literal["adapter-plan-request.v1"] = Field(alias="schema")
    model_id: Name
    method: Literal["qlora", "lora"]
    catalog_sha256: Digest
    base_identity_sha256: Digest
    dataset_manifest_sha256: Digest
    runtime_lock_sha256: Digest
    permission_evidence_sha256: Digest
    evaluation_policy_sha256: Digest
    promotion_policy_sha256: Digest
    adapter_rank: Annotated[StrictInt, Field(ge=1, le=64)]
    target_modules: tuple[Name, ...] = Field(min_length=1, max_length=7)
    sequence_length: Annotated[StrictInt, Field(ge=128, le=4096)]
    gpu_vram_gb: Annotated[StrictInt, Field(ge=1, le=256)]
    output_namespace: Name
    usage_profile: Literal["noncommercial_research"]


class TrainingPermission(PlanDocument):
    """Explicit local permission binding; no publication permission is inferred."""

    schema_version: Literal["adapter-training-permission.v1"] = Field(alias="schema")
    base_identity_sha256: Digest
    dataset_manifest_sha256: Digest
    usage_profile: Literal["noncommercial_research"]
    training_allowed: Literal[True]
    publication_authorized: Literal[False]


class EvaluationPolicy(PlanDocument):
    """Model-specific independent evaluation requirements before manual promotion."""

    schema_version: Literal["adapter-evaluation-policy.v1"] = Field(alias="schema")
    base_identity_sha256: Digest
    dataset_manifest_sha256: Digest
    independent_eval_required: Literal[True]
    compare_base_model: Literal[True]
    evaluation_protocol_sha256: Digest
    automatic_promotion: Literal[False]


class PromotionPolicy(PlanDocument):
    """Future manual approval identity, never an activation grant."""

    schema_version: Literal["adapter-promotion-policy.v1"] = Field(alias="schema")
    base_identity_sha256: Digest
    evaluation_policy_sha256: Digest
    acceptance_policy_sha256: Digest
    manual_approval_required: Literal[True]
    automatic_activation: Literal[False]


def canonical(value: object) -> bytes:
    """Stable identities without paths or timestamps."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


def sha256(payload: bytes) -> str:
    """Return a content identity."""
    return hashlib.sha256(payload).hexdigest()


def read_document(path: Path, maximum: int = MAX_DOCUMENT_BYTES) -> bytes:
    """Bounded read; never follows an explicitly supplied symlink."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise ValueError("preparation input must be a bounded regular file")
    payload = path.read_bytes()
    if len(payload) > maximum:
        raise ValueError("preparation input exceeds its byte bound")
    return payload


def read_bound(path: Path, expected_sha256: str, maximum: int = MAX_DOCUMENT_BYTES) -> bytes:
    """Reject stale or substituted plan inputs before producing an identity."""
    payload = read_document(path, maximum)
    if sha256(payload) != expected_sha256:
        raise ValueError("preparation input hash mismatch")
    return payload


def load_catalog(path: Path = CATALOG_PATH) -> tuple[PreparationCatalog, str]:
    """Load finite metadata only; no native runtime or training packages are imported."""
    payload = read_document(path)
    catalog = PreparationCatalog.model_validate_json(payload)
    ids = [model.model_id for model in catalog.models]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate model preparation identity")
    return catalog, sha256(payload)


def verify_package(  # pylint: disable=too-many-branches
    path: Path, expected_sha256: str
) -> dict[str, object]:
    """Reuse the reviewed SFT export manifest and verify all package content hashes."""
    if path.is_symlink() or not path.is_dir():
        raise ValueError("training package must be a regular directory")
    manifest = json.loads(read_bound(path / "manifest.json", expected_sha256))
    expected_values = {
        "schema": "local-sft-manifest.v1",
        "rules_version": "local-sft-reviewed.v2",
        "target": "local_noncommercial_sft",
        "usage_profile": "noncommercial_research",
    }
    false_flags = ("publication_authorized", "training_executed", "empty_train", "empty_eval")
    if (
        not isinstance(manifest, dict)
        or any(manifest.get(key) != value for key, value in expected_values.items())
        or any(manifest.get(key) is not False for key in false_flags)
    ):
        raise ValueError("reviewed nonempty train/eval SFT export is required")
    file_metadata = manifest.get("files")
    if not isinstance(file_metadata, dict) or set(file_metadata) != {
        "train.jsonl",
        "eval.jsonl",
        "data-card.json",
    }:
        raise ValueError("unexpected training package file inventory")
    for name, metadata in file_metadata.items():
        if not isinstance(metadata, dict) or set(metadata) != {"sha256", "bytes"}:
            raise ValueError("invalid training package file metadata")
        payload = read_bound(path / name, metadata["sha256"], MAX_PACKAGE_FILE_BYTES)
        if (
            not isinstance(metadata["bytes"], int)
            or isinstance(metadata["bytes"], bool)
            or len(payload) != metadata["bytes"]
        ):
            raise ValueError("training package size mismatch")
    records = manifest.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("training package has no reviewed record identities")
    groups: dict[str, str] = {}
    counts = {"train": 0, "eval": 0}
    for record in records:
        if not isinstance(record, dict) or record.get("split") not in counts:
            raise ValueError("invalid training split identity")
        group, split = record.get("group_id"), record["split"]
        if not isinstance(group, str) or len(group) != 64:
            raise ValueError("invalid training group identity")
        if group in groups and groups[group] != split:
            raise ValueError("training group leaks across train/eval")
        groups[group] = split
        counts[split] += 1
    if manifest.get("split_counts") != counts or min(counts.values()) < 1:
        raise ValueError("training split counts are inconsistent or empty")
    if not isinstance(manifest.get("policy_sha256"), str) or not isinstance(
        manifest.get("split_rule"), str
    ):
        raise ValueError("training export policy or split rule is missing")
    return manifest


def build_adapter_plan(  # pylint: disable=too-many-arguments,too-many-locals
    request: AdapterPlanRequest,
    *,
    package: Path,
    base_identity: Path,
    runtime_lock: Path,
    permission_evidence: Path,
    evaluation_policy: Path,
    promotion_policy: Path,
    catalog_path: Path = CATALOG_PATH,
) -> dict[str, object]:
    """Verify local planning inputs; return a blocked, separately named adapter plan."""
    catalog, catalog_digest = load_catalog(catalog_path)
    if request.catalog_sha256 != catalog_digest:
        raise ValueError("model preparation catalogue hash mismatch")
    model = next((item for item in catalog.models if item.model_id == request.model_id), None)
    if model is None:
        raise ValueError("unknown model preparation identity")
    source = model.qlora if request.method == "qlora" else model.lora
    base = TrainingBaseIdentity.model_validate_json(
        read_bound(base_identity, request.base_identity_sha256)
    )
    if (
        base.repository != source.repository
        or base.format != source.format
        or base.architecture != model.architecture
        or (source.revision is not None and base.revision != source.revision)
    ):
        raise ValueError("training base differs from the selected model/method profile")
    names = [item.name for item in base.files]
    if len(set(names)) != len(names) or any(
        Path(name).is_absolute() or ".." in Path(name).parts or name.endswith(".gguf")
        for name in names
    ):
        raise ValueError("training base file inventory is unsafe or contains GGUF")
    if not any(name.endswith(".safetensors") for name in names):
        raise ValueError("training base requires safetensors weights")
    if len(set(request.target_modules)) != len(request.target_modules) or not set(
        request.target_modules
    ).issubset(TARGET_MODULES):
        raise ValueError("unsupported or duplicate adapter target modules")
    manifest = verify_package(package, request.dataset_manifest_sha256)
    read_bound(runtime_lock, request.runtime_lock_sha256, MAX_PACKAGE_FILE_BYTES)
    permission = TrainingPermission.model_validate_json(
        read_bound(permission_evidence, request.permission_evidence_sha256)
    )
    evaluation = EvaluationPolicy.model_validate_json(
        read_bound(evaluation_policy, request.evaluation_policy_sha256)
    )
    promotion = PromotionPolicy.model_validate_json(
        read_bound(promotion_policy, request.promotion_policy_sha256)
    )
    if any(
        (
            permission.base_identity_sha256 != request.base_identity_sha256,
            permission.dataset_manifest_sha256 != request.dataset_manifest_sha256,
            evaluation.base_identity_sha256 != request.base_identity_sha256,
            evaluation.dataset_manifest_sha256 != request.dataset_manifest_sha256,
            promotion.base_identity_sha256 != request.base_identity_sha256,
            promotion.evaluation_policy_sha256 != request.evaluation_policy_sha256,
        )
    ):
        raise ValueError("training permission/evaluation/promotion identity mismatch")
    identity = {
        "schema": "adapter-preparation-identity.v1",
        "request": request.model_dump(mode="json", by_alias=True),
        "base": base.model_dump(mode="json", by_alias=True),
        "export_policy_sha256": manifest["policy_sha256"],
        "split_rule_sha256": sha256(canonical(manifest["split_rule"])),
        "dataset_files": manifest["files"],
        "model_profile": model.model_dump(mode="json"),
    }
    plan_digest = sha256(canonical(identity))
    hardware_sufficient = request.gpu_vram_gb >= source.minimum_vram_gb
    blockers = [
        "GPU HOLD: no execution grant",
        "model-specific adapter runner is not implemented; fixed M0 dry-run unchanged",
        "local training weights/tokenizer/template and runtime compatibility unverified",
        "bounded training resource measurements and independent evaluation pending",
        "manual promotion approval and provider pin registration required",
    ]
    if source.revision is None:
        blockers.append("catalogue training source revision still requires independent review")
    if not hardware_sufficient:
        blockers.insert(0, f"selected recipe requires at least {source.minimum_vram_gb} GB VRAM")
    return {
        "schema": "adapter-preparation-plan.v1",
        "plan_sha256": plan_digest,
        "adapter_identity": f"{request.model_id}/{request.method}/{plan_digest}",
        "output_subdirectory": f"{request.output_namespace}/{request.model_id}/{plan_digest}",
        "identity": identity,
        "hardware_status": "unmeasured_candidate" if hardware_sufficient else "blocked",
        "execution_allowed": False,
        "activation_allowed": False,
        "training_executed": False,
        "adapter_saved": False,
        "publication_authorized": False,
        "blockers": blockers,
    }


def catalog_readiness() -> dict[str, object]:
    """Expose useful preparation metadata without consulting GPU, DB or model packages."""
    catalog, digest = load_catalog()
    return {
        "catalog_sha256": digest,
        **catalog.model_dump(mode="json", by_alias=True),
        "current_native_provider": "pinned Qwen3.5-9B vLLM fp8_per_tensor only",
        "gguf_native_provider_bridge": "not_implemented",
        "fixed_m0_training_profile": "Qwen3.5-9B synthetic 3-step dry-run; no saved adapter",
        "models_downloaded_by_this_command": False,
    }
