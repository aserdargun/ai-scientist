"""Serve verified diagnostics only from the authenticated run's committed blob store."""

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert

from harness.contracts import FitContext
from lab.api import app as api_module
from lab.db.schema import reports, runs
from lab.director.journal import canonical_bytes
from lab.operating_modes import (
    ModeConfig,
    OperatingModeCandidate,
    candidate_source,
    synthetic_snapshot,
)
from tests.test_api_and_scorer import _engine


@pytest.fixture
def diagnostic(tmp_path, monkeypatch):
    case = synthetic_snapshot("healthy_single")
    configuration = ModeConfig(method="lsh", lsh_width=1.0)
    candidate = OperatingModeCandidate(configuration)
    candidate.fit(
        case.snapshot.frame("train"),
        FitContext(
            seed=2,
            signals=case.snapshot.sensors,
            regime_signals=(),
            sampling_s=1,
            time_budget_s=10,
        ),
    )
    prediction = candidate.model.predict(case.snapshot.frame("evaluation"))
    source_sha = hashlib.sha256(candidate_source(configuration)).hexdigest()
    document = {
        "schema": "candidate-mode-diagnostics.v1",
        "candidate_sha256": source_sha,
        "configuration": configuration.model_dump(mode="json"),
        "configuration_sha256": hashlib.sha256(
            canonical_bytes(configuration.model_dump(mode="json"))
        ).hexdigest(),
        "repetition_seed": 2,
        "fit_artifact_sha256": "a" * 64,
        "model": candidate.model.summary(),
        "prediction": prediction.to_dict(),
    }
    payload = canonical_bytes(
        {
            "schema": "candidate-scores.v1",
            "sample_indices": list(range(len(prediction.rows))),
            "scores": [row.omr_percent for row in prediction.rows],
            "mode_diagnostics": document,
        }
    )
    digest = hashlib.sha256(payload).hexdigest()
    run = uuid4()
    report = {
        "status": "completed",
        "task_scores": [
            {
                "candidate_sha256": source_sha,
                "seed": 2,
                "mode_diagnostics": {"artifact_sha256": digest},
            }
        ],
    }
    report_sha = hashlib.sha256(canonical_bytes(report)).hexdigest()
    engine = _engine()
    with engine.begin() as connection:
        connection.execute(
            insert(runs).values(
                run_id=run,
                origin="local",
                owner_id="local:default",
                idempotency_key="diagnostics",
                payload_sha256="b" * 64,
                request_json={},
                state="completed",
                report_sha256=report_sha,
            )
        )
        connection.execute(
            insert(reports).values(
                run_id=run,
                report_sha256=report_sha,
                report_json=report,
                verified_at=datetime.now(UTC),
            )
        )
    monkeypatch.setattr(api_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    app = api_module.create_app(director_engine=engine, director_token="u" * 32)
    return dict(
        root=tmp_path,
        run=run,
        app=app,
        payload=payload,
        digest=digest,
        document=document,
        report_sha=report_sha,
    )


def write_blob(root, digest, payload):
    shard = root / digest[:2]
    shard.mkdir(parents=True, mode=0o700)
    shard.chmod(0o700)
    path = shard / (digest + ".json")
    path.write_bytes(payload)
    path.chmod(0o600)
    return path


def request(data, *, run=None, digest=None):
    with TestClient(data["app"]) as client:
        return client.get(
            f"/v1/runs/{run or data['run']}/mode-diagnostics/{digest or data['digest']}",
            headers={"Authorization": "Bearer " + "u" * 32},
        )


def test_existing_run_blob_is_served_without_global_default_store(diagnostic):
    data = diagnostic
    root = data["root"] / "data/runtime/director-artifacts" / str(data["run"])
    write_blob(root, data["digest"], data["payload"])
    response = request(data)
    assert response.status_code == 200
    assert response.json()["artifact_sha256"] == data["digest"]
    assert response.json()["report_sha256"] == data["report_sha"]
    assert response.json()["diagnostics"] == data["document"]
    assert not (data["root"] / "data/runtime/candidate-blobs").exists()


@pytest.mark.parametrize("location", ["default", "different_run"])
def test_missing_run_blob_cannot_be_substituted_from_another_store(diagnostic, location):
    data = diagnostic
    root = data["root"] / "data/runtime"
    root = (
        root / "candidate-blobs"
        if location == "default"
        else root / "director-artifacts" / str(uuid4())
    )
    write_blob(root, data["digest"], data["payload"])
    assert request(data).status_code == 500


def test_changed_blob_still_fails_content_hash_check(diagnostic):
    data = diagnostic
    root = data["root"] / "data/runtime/director-artifacts" / str(data["run"])
    write_blob(root, data["digest"], data["payload"] + b" ")
    response = request(data)
    assert response.status_code == 500
    assert response.json()["detail"] == "diagnostic integrity check failed"


def test_diagnostics_require_authenticated_terminal_report_membership(diagnostic):
    data = diagnostic
    root = data["root"] / "data/runtime/director-artifacts" / str(data["run"])
    write_blob(root, data["digest"], data["payload"])
    assert request(data, run=uuid4()).status_code == 404
    assert request(data, digest="f" * 64).status_code == 404
    with TestClient(data["app"]) as client:
        assert (
            client.get(f"/v1/runs/{data['run']}/mode-diagnostics/{data['digest']}").status_code
            == 401
        )
