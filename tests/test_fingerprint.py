"""Harness fingerprint tamper fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.fingerprint import (
    HarnessHashMismatch,
    compute_harness_hash,
    enforce_harness_hash,
)


def _source_tree(root: Path) -> None:
    (root / "harness").mkdir(parents=True)
    (root / "vendor/tsb_ad_eval").mkdir(parents=True)
    (root / "lab/sandbox").mkdir(parents=True)
    (root / "lab/scorer").mkdir(parents=True)
    (root / "lab/referee").mkdir(parents=True)
    (root / "lab/director").mkdir(parents=True)
    (root / "lab/llm").mkdir(parents=True)
    (root / "lab/api").mkdir(parents=True)
    (root / "lab/training").mkdir(parents=True)
    (root / "lab/operating_modes").mkdir(parents=True)
    (root / "lab/analytics").mkdir(parents=True)
    (root / "docker/sandbox").mkdir(parents=True)
    (root / "harness/VERSION").write_text("0.5.0\n", encoding="utf-8")
    (root / "harness/contracts.py").write_bytes(b"trusted\n")
    (root / "vendor/tsb_ad_eval/basic_metrics.py").write_bytes(b"metrics\n")
    (root / "lab/sandbox/evaluation.py").write_bytes(b"guard\n")
    (root / "lab/scorer/worker.py").write_bytes(b"scorer\n")
    (root / "lab/referee/referee.py").write_bytes(b"referee\n")
    (root / "lab/director/ledger.py").write_bytes(b"director\n")
    (root / "lab/llm/native_runtime.py").write_bytes(b"runtime\n")
    (root / "lab/api/registry.py").write_bytes(b"trusted registry\n")
    (root / "lab/training/worker.py").write_bytes(b"bounded maintenance\n")
    (root / "lab/operating_modes/model.py").write_bytes(b"mode model\n")
    (root / "lab/analytics/statistics.py").write_bytes(b"statistics\n")
    (root / "lab/suite_limits.py").write_bytes(b"MAX_SUITE_MANIFEST_BYTES = 100\n")
    (root / "lab/cli.py").write_bytes(b"trusted dispatcher\n")
    (root / "lab/reporting.py").write_bytes(b"verified report reader\n")
    (root / "lab/replay.py").write_bytes(b"exact decision replay\n")
    (root / "ops").mkdir()
    (root / "ops/sandbox-image.lock").write_text("sha256:" + "a" * 64 + "\n", encoding="ascii")
    (root / "Dockerfile.sandbox").write_bytes(b"image build\n")
    (root / "docker/sandbox/requirements.txt").write_bytes(b"pinned deps\n")
    (root / "pyproject.toml").write_bytes(b"package metadata\n")
    (root / "uv.lock").write_bytes(b"host dependency lock\n")


def test_hash_is_deterministic_and_detects_single_byte_change(tmp_path: Path) -> None:
    _source_tree(tmp_path)
    expected = compute_harness_hash(tmp_path)
    assert enforce_harness_hash(expected.sha256, tmp_path) == expected
    source = tmp_path / "harness/contracts.py"
    source.write_bytes(b"trustee\n")
    with pytest.raises(HarnessHashMismatch, match="harness_hash_mismatch"):
        enforce_harness_hash(expected.sha256, tmp_path)


def test_hash_covers_new_trusted_files(tmp_path: Path) -> None:
    _source_tree(tmp_path)
    expected = compute_harness_hash(tmp_path)
    (tmp_path / "harness/new_guard.py").write_text("pass\n", encoding="utf-8")
    assert compute_harness_hash(tmp_path).sha256 != expected.sha256


@pytest.mark.parametrize(
    "relative_path",
    [
        "lab/sandbox/evaluation.py",
        "ops/sandbox-image.lock",
        "Dockerfile.sandbox",
        "docker/sandbox/requirements.txt",
        "pyproject.toml",
        "uv.lock",
        "lab/scorer/worker.py",
        "lab/referee/referee.py",
        "lab/director/ledger.py",
        "lab/llm/native_runtime.py",
        "lab/api/registry.py",
        "lab/training/worker.py",
        "lab/operating_modes/model.py",
        "lab/analytics/statistics.py",
        "lab/suite_limits.py",
        "lab/cli.py",
        "lab/reporting.py",
        "lab/replay.py",
    ],
)
def test_hash_binds_evaluator_image_and_dependencies(
    tmp_path: Path, relative_path: str
) -> None:
    _source_tree(tmp_path)
    expected = compute_harness_hash(tmp_path)
    target = tmp_path / relative_path
    target.write_bytes(target.read_bytes() + b"changed\n")
    assert compute_harness_hash(tmp_path).sha256 != expected.sha256


def test_symlink_inside_trusted_tree_is_rejected(tmp_path: Path) -> None:
    _source_tree(tmp_path)
    (tmp_path / "harness/link.py").symlink_to(tmp_path / "harness/contracts.py")
    with pytest.raises(ValueError, match="symlink is forbidden"):
        compute_harness_hash(tmp_path)


def test_symlinked_source_root_is_rejected(tmp_path: Path) -> None:
    physical = tmp_path / "physical"
    _source_tree(physical)
    (tmp_path / "harness").symlink_to(physical / "harness", target_is_directory=True)
    (tmp_path / "vendor").symlink_to(physical / "vendor", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink source root"):
        compute_harness_hash(tmp_path)


def test_symlinked_sandbox_source_root_is_rejected(tmp_path: Path) -> None:
    physical = tmp_path / "physical"
    _source_tree(physical)
    (tmp_path / "harness").symlink_to(physical / "harness", target_is_directory=True)
    (tmp_path / "vendor").symlink_to(physical / "vendor", target_is_directory=True)
    (tmp_path / "lab").mkdir()
    (tmp_path / "lab/sandbox").symlink_to(physical / "lab/sandbox", target_is_directory=True)
    (tmp_path / "Dockerfile.sandbox").write_bytes(b"image build\n")
    (tmp_path / "docker/sandbox/requirements.txt").parent.mkdir(parents=True)
    (tmp_path / "docker/sandbox/requirements.txt").write_bytes(b"pinned deps\n")
    with pytest.raises(ValueError, match="symlink source root"):
        compute_harness_hash(tmp_path)
