"""Read-only metadata and bounded immutable evidence; fixtures are not runtime acceptance."""

import hashlib
import json
from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, update

from console.app import create_app as console_app
from lab.api import app as api_module
from lab.api import experience
from lab.db.schema import reports, runs
from lab.director.contracts import SourceProvenance
from lab.director.ledger import canonical_json_bytes
from lab.reporting import _LedgerExperiment
from tests.test_api_and_scorer import _engine
from tests.test_console_upstream import _upstream_config
from tests.test_report_replay import _pair
from tests.test_training_export059 import fixture_record


def write_blob(root, digest, payload):
    shard = root / digest[:2]
    shard.mkdir(parents=True, mode=0o700, exist_ok=True)
    shard.chmod(0o700)
    path = shard / (digest + ".json")
    path.write_bytes(payload)
    path.chmod(0o600)
    return path


class Result:
    def __init__(self, value):
        self.value = value

    def mappings(self):
        return self

    def all(self):
        return self.value

    def scalar_one(self):
        return self.value


class LedgerEngine:
    """Mock SQL transport only; documents go through the production hash/schema verifier."""

    def __init__(self, run, rows, receipt, *, postgres=False):
        self.run, self.rows, self.receipt = run, rows, receipt
        self.dialect = SimpleNamespace(name="postgresql" if postgres else "sqlite")
        self.statements = []

    def connect(self):
        return nullcontext(self)

    def begin(self):
        return nullcontext()

    def exec_driver_sql(self, statement):
        self.statements.append(statement)

    def execute(self, statement, params):
        self.statements.append(str(statement))
        if "experiment_record_receipt" in str(statement):
            assert params["experiment_id"] == self.receipt["experiment_id"]
            return Result(self.receipt)
        assert params["run_id"] == self.run
        assert ("LIMIT 201" in str(statement)) == getattr(self, "require_limit", True)
        return Result(self.rows[:201])


def ledger(tmp_path, pair=None, *, protected=False, postgres=False):
    run = pair.document.run_id if pair else uuid4()
    pair = pair or _pair(run, baseline=False, sequence=1)
    if protected:
        source = SourceProvenance(
            dataset_id="PRIVATE-DATASET",
            split_id="sealed-holdout-PRIVATE",
            session_id="PRIVATE",
            source_manifest_sha256="a" * 64,
            source_revision="PRIVATE",
            license_id="PRIVATE",
            attribution="PRIVATE",
            access_terms="PRIVATE",
            usage_profile="noncommercial_research",
        )
        pair = _LedgerExperiment(
            pair.sequence,
            pair.document,
            pair.trajectory.model_copy(update={"source_provenance": (source,)}),
        )
    exp, trajectory = pair.document, pair.trajectory
    payloads = {
        "experiment": canonical_json_bytes(exp.model_dump(mode="json", by_alias=True)),
        "trajectory": canonical_json_bytes(trajectory.model_dump(mode="json", by_alias=True)),
        "candidate": f"pipeline-{pair.sequence}".encode(),
        "messages": b"messages",
    }
    if exp.llm_input_tokens:
        source = b"def build_candidate():\n    return None\n"
        payloads["candidate"] = source
        payloads["messages"] = canonical_json_bytes(fixture_record(pair.sequence).transcript)
    digests = {name: hashlib.sha256(raw).hexdigest() for name, raw in payloads.items()}
    for name, raw in payloads.items():
        write_blob(tmp_path, digests[name], raw)
    receipt = {
        "experiment_id": exp.experiment_id,
        "run_id": str(run),
        "status": exp.status,
        "experiment_sha256": digests["experiment"],
        "experiment_blob_sha256": digests["experiment"],
        "trajectory_sha256": digests["trajectory"],
        "trajectory_blob_sha256": digests["trajectory"],
        "messages_blob_sha256": digests["messages"],
    }
    rows = [{"experiment_id": exp.experiment_id, "sequence": pair.sequence, "status": exp.status}]
    engine = LedgerEngine(run, rows, receipt, postgres=postgres)
    reader = experience.ExperienceReader(engine, tmp_path)
    verified = {
        "report_sha256": "c" * 64,
        "report": {"status": "completed", "provider": "mode-grid"},
    }
    return run, pair, reader, verified, digests


@pytest.fixture
def terminal_api(tmp_path, monkeypatch):
    engine = _engine()
    run = uuid4()
    report = {"status": "stopped", "purpose": "mode-stream"}
    sha = hashlib.sha256(canonical_json_bytes(report)).hexdigest()
    with engine.begin() as conn:
        conn.execute(
            insert(runs).values(
                run_id=run,
                origin="local",
                owner_id="local:default",
                idempotency_key="experience",
                payload_sha256=hashlib.sha256(canonical_json_bytes({})).hexdigest(),
                request_json={},
                state="stopped",
                report_sha256=sha,
            )
        )
        conn.execute(
            insert(reports).values(
                run_id=run, report_sha256=sha, report_json=report, verified_at=datetime.now(UTC)
            )
        )
    monkeypatch.setattr(api_module, "PROJECT_ROOT", tmp_path)
    reads = []
    monkeypatch.setattr(experience, "read_run_pairs", lambda *a, **kw: reads.append(kw) or ())
    app = api_module.create_app(director_engine=engine, director_token="u" * 32)
    return SimpleNamespace(engine=engine, run=run, app=app, reads=reads, sha=sha)


def api_get(data, auth=True):
    with TestClient(data.app) as client:
        return client.get(
            f"/v1/runs/{data.run}/experience",
            headers={"Authorization": "Bearer " + "u" * 32} if auth else {},
        )


def test_api_requires_owner_then_verified_terminal_report(terminal_api):
    data = terminal_api
    assert api_get(data, False).status_code == 401
    with data.engine.begin() as conn:
        conn.execute(update(runs).values(owner_id="foreign-owner"))
    assert api_get(data).status_code == 404
    assert data.reads == []


@pytest.mark.parametrize("state", ["queued", "running", "stop_requested"])
def test_nonterminal_report_never_exposes_memory(terminal_api, state):
    data = terminal_api
    with data.engine.begin() as conn:
        conn.execute(update(runs).values(state=state))
    assert api_get(data).status_code == 404
    assert data.reads == []


def test_changed_report_hash_never_reaches_ledger(terminal_api):
    data = terminal_api
    with data.engine.begin() as conn:
        conn.execute(
            update(reports).values(report_json={"status": "stopped", "PRIVATE": "tampered"})
        )
    assert api_get(data).status_code == 500
    assert data.reads == []


def test_cpu_stream_is_a_report_reference_with_training_blocked(terminal_api):
    response = api_get(terminal_api)
    assert response.status_code == 200
    body = response.json()
    assert body["report_sha256"] == terminal_api.sha
    record = body["records"][0]
    assert record["model_id"] is None
    assert record["model_receipt_status"] == "not-applicable-cpu"
    assert record["training_eligibility"]["eligible"] is False
    assert body["training_started"] is body["holdout_included"] is False
    assert body["feedback"] == {"same_run_recent_limit": 30, "cross_run_reuse": False}


def test_verified_pairs_only_expose_allowlisted_metadata(tmp_path):
    run, pair, reader, verified, digests = ledger(tmp_path)
    result = experience.build_experience(reader, run, verified)
    record = result["records"][0]
    assert record["experiment_sha256"] == digests["experiment"]
    assert record["trajectory_sha256"] == digests["trajectory"]
    assert record["score"] == pair.document.suite_score
    assert record["decision"] == "KEEP"
    assert record["model_receipt_status"] == "not-applicable-cpu"
    serialized = json.dumps(result)
    for canary in (
        "private prompt text",
        "holdout_canary",
        "sealed-canary-task",
        "messages",
        "pipeline-1",
    ):
        assert canary not in serialized


@pytest.mark.parametrize("blob", ["experiment", "trajectory", "candidate", "messages"])
def test_each_blob_hash_is_still_verified(tmp_path, blob):
    run, _, reader, verified, digests = ledger(tmp_path)
    digest = digests[blob]
    (tmp_path / digest[:2] / (digest + ".json")).write_bytes(b"tampered PRIVATE")
    with pytest.raises(ValueError):
        experience.build_experience(reader, run, verified)


def test_hash_valid_noncanonical_experiment_is_rejected(tmp_path):
    run, _, reader, verified, digests = ledger(tmp_path)
    raw = reader.artifact(digests["experiment"]) + b" "
    digest = hashlib.sha256(raw).hexdigest()
    write_blob(tmp_path, digest, raw)
    reader.engine.receipt.update(experiment_sha256=digest, experiment_blob_sha256=digest)
    with pytest.raises(ValueError, match="canonical"):
        experience.build_experience(reader, run, verified)


def test_cross_run_receipt_cannot_substitute_a_pair(tmp_path):
    run, _, reader, verified, _ = ledger(tmp_path)
    reader.engine.receipt["run_id"] = str(uuid4())
    with pytest.raises(ValueError, match="requested run"):
        experience.build_experience(reader, run, verified)


def test_oversized_artifact_and_aggregate_budget_are_rejected(tmp_path, monkeypatch):
    run, _, reader, verified, digests = ledger(tmp_path)
    digest = digests["messages"]
    with (tmp_path / digest[:2] / (digest + ".json")).open("wb") as stream:
        stream.truncate(experience.MAX_ARTIFACT_BYTES + 1)
    with pytest.raises(ValueError, match="bounded regular"):
        experience.build_experience(reader, run, verified)
    monkeypatch.setattr(experience, "MAX_READ_BYTES", 1)
    with pytest.raises(experience.ExperienceLimitError):
        experience.ExperienceReader(reader.engine, tmp_path).artifact(digests["candidate"])


def test_record_limit_rejects_overflow_before_artifact_reads(tmp_path):
    run, _, reader, verified, _ = ledger(tmp_path)
    reader.engine.rows *= 201
    with pytest.raises(experience.ExperienceLimitError):
        experience.build_experience(reader, run, verified)
    assert reader.artifacts == {}


def test_legacy_ledger_reader_defaults_preserve_complete_verification(tmp_path):
    run, pair, reader, _, _ = ledger(tmp_path)
    reader.engine.require_limit = False
    pairs = experience.read_run_pairs(reader.engine, run, artifact_root=tmp_path)
    assert pairs == (pair,)
    assert not any("LIMIT" in statement for statement in reader.engine.statements)


def test_expired_deadline_reads_nothing(tmp_path):
    run, _, reader, verified, _ = ledger(tmp_path)
    reader.deadline = 0
    with pytest.raises(experience.ExperienceTimeoutError):
        experience.build_experience(reader, run, verified)
    assert reader.engine.statements == []


def test_protected_source_names_hashes_and_scores_are_withheld(tmp_path):
    run, _, reader, verified, _ = ledger(tmp_path, protected=True)
    result = experience.build_experience(reader, run, verified)
    assert result["records"] == []
    assert result["protected_records_excluded"] is True
    assert "PRIVATE" not in json.dumps(result)
    assert "a" * 64 not in json.dumps(result)
    assert "exp_" not in json.dumps(result)
    assert "trj_" not in json.dumps(result)


def test_reported_local_identity_is_distinct_from_fixture_and_training_authority(tmp_path):
    record = fixture_record(1)
    pair = _LedgerExperiment(1, record.experiment, record.trajectory)
    run, _, reader, verified, _ = ledger(tmp_path, pair)
    verified["report"]["provider"] = "local-qwen"
    result = experience.build_experience(reader, run, verified)
    item = result["records"][0]
    assert item["model_receipt_status"] == "reported-local-identity-matched"
    assert item["model_id"] == "local.test-model"
    assert item["training_eligibility"] == {
        "eligible": False,
        "reasons": ["export-permission-not-checked", "clean-runtime-review-not-checked"],
    }
    fake = pair.trajectory.model_copy(update={"model_id": "fixture.model"})
    assert experience._model_status(pair.document, fake, False) == "fixture-or-unverified"
    mismatch = pair.trajectory.model_copy(update={"model_id": "different-local-model"})
    assert experience._model_status(pair.document, mismatch, False) == "identity-mismatch"


@pytest.mark.parametrize("baseline", [True, False])
def test_sft_first_stage_rejects_baselines_and_nonpositive_candidates(tmp_path, baseline):
    run = uuid4()
    pair = _pair(run, baseline=baseline, sequence=1)
    if not baseline:
        decision = pair.document.decision.model_copy(update={"verdict": "DISCARD"})
        pair = _LedgerExperiment(
            pair.sequence,
            pair.document.model_copy(update={"decision": decision}),
            pair.trajectory.model_copy(update={"outcome": decision}),
        )
    run, _, reader, verified, _ = ledger(tmp_path, pair)
    result = experience.build_experience(reader, run, verified)
    eligibility = result["records"][0]["training_eligibility"]
    assert eligibility["eligible"] is False
    reason = "not_proposal" if baseline else "not_confirmed_positive_candidate"
    assert reason in eligibility["reasons"]


def test_postgres_timeout_is_local_to_each_read_only_transaction(tmp_path):
    run, _, reader, verified, _ = ledger(tmp_path, postgres=True)
    experience.build_experience(reader, run, verified)
    assert "SET TRANSACTION READ ONLY" in reader.engine.statements
    assert any(s.startswith("SET LOCAL statement_timeout = ") for s in reader.engine.statements)
    assert not any(s.startswith("SET statement_timeout") for s in reader.engine.statements)


def test_trusted_mode_source_method_is_recognized_without_execution():
    from lab.operating_modes import ModeConfig, candidate_source

    assert experience._method(candidate_source(ModeConfig(method="som"))) == "som"
    assert experience._method(b"raise RuntimeError('do not execute')") is None


def test_console_proxies_only_get_for_valid_uuid(tmp_path):
    run = uuid4()
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "service": "lab-api"})
        assert request.headers["authorization"].startswith("Bearer ")
        return httpx.Response(200, json={"run_id": str(run), "schema": "run-experience.v1"})

    api = _upstream_config(tmp_path, handler)
    app = console_app(api, watch_path=tmp_path / "watched.json", dist_root=tmp_path / "none")
    with TestClient(app, base_url="http://127.0.0.1:8788", client=("127.0.0.1", 50000)) as client:
        assert client.get(f"/console-api/runs/{run}/experience").status_code == 200
        assert client.get("/console-api/runs/not-a-uuid/experience").status_code == 422
    assert ("GET", f"/v1/runs/{run}/experience") in seen
    assert all(method == "GET" for method, _ in seen)
    assert not (tmp_path / "watched.json").exists()
