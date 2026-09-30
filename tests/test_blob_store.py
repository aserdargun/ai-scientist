from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

import pytest

from lab.scorer.jobs import _recover_orphan_blob_temps, store_candidate_artifact


def _valid_payload(value: float) -> bytes:
    return (
        '{"schema":"candidate-scores.v1","sample_indices":[0,1],'
        f'"scores":[{value},{value + 1}]}}'
    ).encode()


def _orphan(root: Path, payload: bytes, nonce: str = "0123456789abcdef") -> Path:
    digest = hashlib.sha256(payload).hexdigest()
    shard = root / digest[:2]
    shard.mkdir(parents=True, mode=0o700, exist_ok=True)
    path = shard / f".{digest}.{nonce}.tmp"
    path.write_bytes(payload)
    path.chmod(0o600)
    return path


def test_blob_store_recovers_crashed_temp_before_next_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orphan_payload = _valid_payload(1.0)
    orphan = _orphan(tmp_path, orphan_payload)
    finalized_payload = _valid_payload(4.0)
    finalized_digest = hashlib.sha256(finalized_payload).hexdigest()
    finalized = tmp_path / finalized_digest[:2] / f"{finalized_digest}.json"
    finalized.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    finalized.write_bytes(finalized_payload)
    finalized.chmod(0o600)
    usage = shutil.disk_usage(tmp_path)
    monkeypatch.setattr(
        "lab.scorer.jobs.shutil.disk_usage",
        lambda _path: usage._replace(free=usage.free + 21 * 1024**3),
    )

    stored_digest = store_candidate_artifact(_valid_payload(7.0), artifact_root=tmp_path)

    assert not orphan.exists()
    assert finalized.read_bytes() == finalized_payload
    assert (tmp_path / stored_digest[:2] / f"{stored_digest}.json").is_file()


def test_orphan_recovery_preserves_nonmatching_entries(tmp_path: Path) -> None:
    orphan = _orphan(tmp_path, _valid_payload(1.0))
    unrelated = orphan.parent / "operator-note.txt"
    unrelated.write_text("preserve")

    assert _recover_orphan_blob_temps(tmp_path) == 1
    assert not orphan.exists()
    assert unrelated.read_text() == "preserve"


def test_orphan_recovery_rejects_hardlinked_temp_without_unlinking(tmp_path: Path) -> None:
    orphan = _orphan(tmp_path, _valid_payload(1.0))
    second_link = orphan.with_name("second-link")
    os.link(orphan, second_link)

    with pytest.raises(ValueError, match="unsafe ownership or type"):
        _recover_orphan_blob_temps(tmp_path)

    assert orphan.exists() and second_link.exists()


def test_orphan_recovery_rejects_temp_in_wrong_shard(tmp_path: Path) -> None:
    payload = _valid_payload(1.0)
    digest = hashlib.sha256(payload).hexdigest()
    shard = tmp_path / ("00" if digest[:2] != "00" else "01")
    shard.mkdir(mode=0o700)
    wrong = shard / f".{digest}.0123456789abcdef.tmp"
    wrong.write_bytes(payload)
    wrong.chmod(0o600)

    with pytest.raises(ValueError, match="wrong shard"):
        _recover_orphan_blob_temps(tmp_path)

    assert wrong.exists()
