"""Preparation must bind actual input bytes and never authorize training."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lab.training.model_plans import (
    AdapterPlanRequest,
    build_adapter_plan,
    canonical,
    catalog_readiness,
    load_catalog,
    sha256,
)


def _fixture(tmp_path: Path, model_id: str = "qwen3.8-27b", method: str = "qlora"):
    catalog, catalog_digest = load_catalog()
    model = next(item for item in catalog.models if item.model_id == model_id)
    source = model.qlora if method == "qlora" else model.lora

    def document(name: str, value: object) -> tuple[Path, str]:
        path = tmp_path / name
        payload = canonical(value)
        path.write_bytes(payload)
        return path, sha256(payload)

    package = tmp_path / "package"
    package.mkdir()
    files = {}
    for name, payload in {
        "train.jsonl": b'{"messages":[]}\n',
        "eval.jsonl": b'{"messages":[]}\n',
        "data-card.json": b"{}\n",
    }.items():
        (package / name).write_bytes(payload)
        files[name] = {"sha256": sha256(payload), "bytes": len(payload)}
    manifest = {
        "schema": "local-sft-manifest.v1",
        "rules_version": "local-sft-reviewed.v2",
        "target": "local_noncommercial_sft",
        "usage_profile": "noncommercial_research",
        "publication_authorized": False,
        "training_executed": False,
        "empty_train": False,
        "empty_eval": False,
        "files": files,
        "policy_sha256": "a" * 64,
        "split_rule": "whole-source grouped review",
        "split_counts": {"train": 1, "eval": 1},
        "records": [
            {"group_id": "b" * 64, "split": "train"},
            {"group_id": "c" * 64, "split": "eval"},
        ],
    }
    manifest_payload = canonical(manifest)
    (package / "manifest.json").write_bytes(manifest_payload)
    dataset_digest = sha256(manifest_payload)
    base, base_digest = document(
        "base.json",
        {
            "schema": "training-base-identity.v1",
            "repository": source.repository,
            "revision": source.revision,
            "format": source.format,
            "architecture": model.architecture,
            "files": [{"name": "model.safetensors", "bytes": 123, "sha256": "d" * 64}],
            "tokenizer_sha256": "e" * 64,
            "chat_template_sha256": "f" * 64,
            "license_id": "Apache-2.0",
            "license_evidence_sha256": "1" * 64,
        },
    )
    lock, lock_digest = document("runtime.lock", {"fixture_only": True})
    permission, permission_digest = document(
        "permission.json",
        {
            "schema": "adapter-training-permission.v1",
            "base_identity_sha256": base_digest,
            "dataset_manifest_sha256": dataset_digest,
            "usage_profile": "noncommercial_research",
            "training_allowed": True,
            "publication_authorized": False,
        },
    )
    evaluation, evaluation_digest = document(
        "evaluation.json",
        {
            "schema": "adapter-evaluation-policy.v1",
            "base_identity_sha256": base_digest,
            "dataset_manifest_sha256": dataset_digest,
            "independent_eval_required": True,
            "compare_base_model": True,
            "evaluation_protocol_sha256": "2" * 64,
            "automatic_promotion": False,
        },
    )
    promotion, promotion_digest = document(
        "promotion.json",
        {
            "schema": "adapter-promotion-policy.v1",
            "base_identity_sha256": base_digest,
            "evaluation_policy_sha256": evaluation_digest,
            "acceptance_policy_sha256": "3" * 64,
            "manual_approval_required": True,
            "automatic_activation": False,
        },
    )
    request = AdapterPlanRequest.model_validate_json(
        canonical(
            {
                "schema": "adapter-plan-request.v1",
                "model_id": model_id,
                "method": method,
                "catalog_sha256": catalog_digest,
                "base_identity_sha256": base_digest,
                "dataset_manifest_sha256": dataset_digest,
                "runtime_lock_sha256": lock_digest,
                "permission_evidence_sha256": permission_digest,
                "evaluation_policy_sha256": evaluation_digest,
                "promotion_policy_sha256": promotion_digest,
                "adapter_rank": 16,
                "target_modules": ["q_proj", "v_proj"],
                "sequence_length": 2048,
                "gpu_vram_gb": 16,
                "output_namespace": "private-adapters",
                "usage_profile": "noncommercial_research",
            }
        )
    )
    paths = {
        "package": package,
        "base_identity": base,
        "runtime_lock": lock,
        "permission_evidence": permission,
        "evaluation_policy": evaluation,
        "promotion_policy": promotion,
    }
    return request, paths


@pytest.mark.parametrize("method,floor", [("qlora", 24), ("lora", 37)])
def test_27b_training_denied_on_16gb(tmp_path, method, floor):
    request, paths = _fixture(tmp_path, method=method)
    result = build_adapter_plan(request, **paths)
    assert result["hardware_status"] == "blocked"
    assert f"at least {floor} GB" in result["blockers"][0]
    assert result["execution_allowed"] is False
    assert result["activation_allowed"] is False
    assert result["training_executed"] is False


@pytest.mark.parametrize("model_id", ["qwen3.5-9b", "qwen3-8b"])
def test_alternative_qlora_remains_unmeasured_and_separate(tmp_path, model_id):
    request, paths = _fixture(tmp_path, model_id)
    result = build_adapter_plan(request, **paths)
    assert result["hardware_status"] == "unmeasured_candidate"
    assert result["execution_allowed"] is False
    assert result["adapter_identity"].startswith(model_id + "/qlora/")
    changed = request.model_copy(update={"adapter_rank": 32})
    assert build_adapter_plan(changed, **paths)["plan_sha256"] != result["plan_sha256"]
    assert build_adapter_plan(request, **paths) == result


def test_changed_input_bytes_are_rejected(tmp_path):
    request, paths = _fixture(tmp_path)
    paths["runtime_lock"].write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        build_adapter_plan(request, **paths)


def test_wrong_model_and_catalog_binding_rejected(tmp_path):
    request, paths = _fixture(tmp_path)
    changed = request.model_copy(update={"model_id": "qwen3-8b"})
    with pytest.raises(ValueError, match="selected model/method"):
        build_adapter_plan(changed, **paths)
    changed = request.model_copy(update={"catalog_sha256": "0" * 64})
    with pytest.raises(ValueError, match="catalogue hash mismatch"):
        build_adapter_plan(changed, **paths)


def test_rehashed_package_group_leakage_rejected(tmp_path):
    request, paths = _fixture(tmp_path)
    path = paths["package"] / "manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["records"][1]["group_id"] = manifest["records"][0]["group_id"]
    payload = canonical(manifest)
    path.write_bytes(payload)
    changed = request.model_copy(update={"dataset_manifest_sha256": sha256(payload)})
    with pytest.raises(ValueError, match="leaks across train/eval"):
        build_adapter_plan(changed, **paths)


def test_catalog_does_not_imply_runtime_bridge_or_local_download():
    result = catalog_readiness()
    assert result["execution_allowed"] is False
    assert result["gguf_native_provider_bridge"] == "not_implemented"
    assert result["models_downloaded_by_this_command"] is False
    assert len(result["models"]) == 3
