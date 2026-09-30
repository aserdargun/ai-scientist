"""Shared hash-bound blob bytes for Director evidence without loosening Scorer parsing."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from lab.director.artifacts import read_director_artifact, store_director_artifact
from lab.scorer.service import CandidateOutputError, parse_candidate_score


def test_generic_director_artifacts_round_trip_exact_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("lab.scorer.jobs.MIN_FREE_DISK_BYTES", 0)
    root = tmp_path / "artifacts"
    payloads = (
        b"candidate source\nprint('candidate')\n",
        b'{"schema":"experiment.v1","measured":true}',
        b'{"schema":"trajectory.v1","messages_blob_sha256":"abc"}',
        b'{"messages":[{"role":"assistant","content":"safe"}]}',
    )
    for payload in payloads:
        digest = store_director_artifact(payload, artifact_root=root)
        assert digest == hashlib.sha256(payload).hexdigest()
        assert read_director_artifact(digest, artifact_root=root) == payload


def test_generic_artifacts_do_not_change_candidate_score_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("lab.scorer.jobs.MIN_FREE_DISK_BYTES", 0)
    root = tmp_path / "artifacts"
    invalid_candidate_score = b"not a candidate score document"
    digest = store_director_artifact(invalid_candidate_score, artifact_root=root)
    assert read_director_artifact(digest, artifact_root=root) == invalid_candidate_score
    with pytest.raises(CandidateOutputError):
        parse_candidate_score(invalid_candidate_score)


def test_generic_artifact_reader_rejects_changed_bytes_and_unsafe_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("lab.scorer.jobs.MIN_FREE_DISK_BYTES", 0)
    root = tmp_path / "artifacts"
    payload = b'{"schema":"experiment.v1"}'
    digest = store_director_artifact(payload, artifact_root=root)
    artifact = root / digest[:2] / f"{digest}.json"
    artifact.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="ownership or mode|digest mismatch"):
        read_director_artifact(digest, artifact_root=root)
