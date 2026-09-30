"""Content-address the trusted harness and its vendored metric code."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


class HarnessHashMismatch(RuntimeError):
    """The trusted harness no longer matches the suite's pinned fingerprint."""


@dataclass(frozen=True, slots=True)
class HarnessFingerprint:
    """Stable digest and file count for a harness source tree."""

    sha256: str
    file_count: int


def compute_harness_hash(repository_root: Path) -> HarnessFingerprint:
    """Hash harness, sandbox execution code, image and its pinned dependencies."""
    source_roots = (
        repository_root / "harness",
        repository_root / "vendor",
        repository_root / "lab/sandbox",
        repository_root / "lab/scorer",
        repository_root / "lab/referee",
        repository_root / "lab/director",
        repository_root / "lab/llm",
        repository_root / "lab/api",
        repository_root / "lab/training",
        repository_root / "lab/operating_modes",
        repository_root / "lab/analytics",
    )
    source_files = (
        repository_root / "Dockerfile.sandbox",
        repository_root / "docker/sandbox/requirements.txt",
        repository_root / "ops/sandbox-image.lock",
        repository_root / "pyproject.toml",
        repository_root / "uv.lock",
        repository_root / "harness/VERSION",
        repository_root / "lab/cli.py",
        repository_root / "lab/suite_limits.py",
        repository_root / "lab/reporting.py",
        repository_root / "lab/replay.py",
    )
    files: list[Path] = []
    for source_root in source_roots:
        if source_root.is_symlink():
            raise ValueError(f"symlink source root is forbidden: {source_root}")
        if not source_root.is_dir():
            raise FileNotFoundError(f"required source directory is missing: {source_root}")
        for path in source_root.rglob("*"):
            if "__pycache__" in path.parts or path.suffix == ".pyc" or path.name.startswith("."):
                continue
            if path.is_symlink():
                raise ValueError(f"symlink is forbidden inside trusted source: {path}")
            if path.is_file():
                files.append(path)
    for path in source_files:
        if path.is_symlink():
            raise ValueError(f"symlink is forbidden in trusted source files: {path}")
        if not path.is_file():
            raise FileNotFoundError(f"required trusted source file is missing: {path}")
        files.append(path)
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda item: item.relative_to(repository_root).as_posix()):
        relative = path.relative_to(repository_root).as_posix().encode("utf-8")
        contents = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(contents)
    return HarnessFingerprint(digest.hexdigest(), len(files))


def enforce_harness_hash(expected_sha256: str, repository_root: Path) -> HarnessFingerprint:
    """Fail closed when trusted source differs from the suite's pinned digest."""
    actual = compute_harness_hash(repository_root)
    if not expected_sha256 or actual.sha256 != expected_sha256:
        raise HarnessHashMismatch(
            f"harness_hash_mismatch: expected={expected_sha256 or 'missing'} actual={actual.sha256}"
        )
    return actual
