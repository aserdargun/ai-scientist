"""Reading a missing ledger artifact never changes the source store layout."""

from pathlib import Path

import pytest

from lab.scorer.jobs import read_artifact_bytes, store_artifact_bytes


@pytest.mark.parametrize("existing_root", [False, True])
def test_missing_artifact_read_does_not_create_directories(
    tmp_path: Path, existing_root: bool
) -> None:
    root = tmp_path / "blobs"
    if existing_root:
        root.mkdir(mode=0o700)
    with pytest.raises(FileNotFoundError):
        read_artifact_bytes("a" * 64, artifact_root=root)
    assert root.exists() is existing_root
    assert not (root / "aa").exists()


def test_stored_artifact_remains_readable_without_directory_mutation(tmp_path: Path) -> None:
    root = tmp_path / "blobs"
    payload = b"verified private ledger record"
    digest = store_artifact_bytes(payload, artifact_root=root)
    before = sorted(path.relative_to(root) for path in root.rglob("*"))
    assert read_artifact_bytes(digest, artifact_root=root) == payload
    assert sorted(path.relative_to(root) for path in root.rglob("*")) == before
