"""Bounded baseline request, authenticated admission and HTTP client contracts."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from lab import cli
from lab.api import app as api_module
from lab.api.app import create_app
from lab.api.contracts import StartBaselineRequest
from lab.api.registry import SuiteEntry, SuiteRegistry
from lab.cli import _submit_baseline
from lab.db.schema import metadata, runs
from lab.director.baselines import BASELINE_NAMES
from lab.reporting import render_baseline_report
from lab.scorer.baseline_report import validate_baseline_report


def _registered_suite(tmp_path: Path) -> SuiteRegistry:
    manifest = b'{"schema":"director-suite.v1","suite_id":"synthetic.baseline.v1"}'
    scenario = b'{"schema":"review-fake-provider-input.v1","items":[]}'
    (tmp_path / "suite.json").write_bytes(manifest)
    (tmp_path / "scenario.json").write_bytes(scenario)
    (tmp_path / "suite.json").chmod(0o600)
    (tmp_path / "scenario.json").chmod(0o600)
    return SuiteRegistry(
        (
            SuiteEntry(
                suite_id="synthetic.baseline.v1",
                track="anomaly",
                program_version="fixture-v1",
                suite_manifest_path="suite.json",
                suite_manifest_sha256=hashlib.sha256(manifest).hexdigest(),
                provider="fake-json",
                scenario_path="scenario.json",
                scenario_sha256=hashlib.sha256(scenario).hexdigest(),
                proposal_limit=2,
            ),
        ),
        tmp_path,
    )


def _engine():
    raw = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    engine = raw.execution_options(schema_translate_map={"lab": None, "scorer": None})
    metadata.create_all(engine)
    return engine


def test_baseline_request_is_strictly_zero_use() -> None:
    valid = {
        "idempotency_key": "baseline-idempotency-key-001",
        "suite": "synthetic.baseline.v1",
        "program_version": "fixture-v1",
        "budget": {"experiments": 0, "wall_seconds": 60, "model_tokens": 0},
    }
    assert StartBaselineRequest.model_validate(valid, strict=True).budget.experiments == 0
    for field, value in (("experiments", 1), ("model_tokens", 1)):
        invalid = {**valid, "budget": {**valid["budget"], field: value}}
        with pytest.raises(ValueError):
            StartBaselineRequest.model_validate(invalid, strict=True)


def test_baseline_api_admission_binds_owner_and_shared_idempotency_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        api_module,
        "compute_harness_hash",
        lambda _root: SimpleNamespace(sha256="a" * 64),
    )
    engine = _engine()
    app = create_app(
        director_engine=engine,
        director_token="u" * 32,
        suite_registry=_registered_suite(tmp_path),
    )
    headers = {"Authorization": f"Bearer {'u' * 32}"}
    body = {
        "idempotency_key": "baseline-idempotency-key-001",
        "suite": "synthetic.baseline.v1",
        "program_version": "fixture-v1",
        "budget": {"experiments": 0, "wall_seconds": 60, "model_tokens": 0},
    }
    with TestClient(app) as client:
        assert client.post("/v1/baselines", json=body).status_code == 401
        accepted = client.post("/v1/baselines", headers=headers, json=body)
        assert accepted.status_code == 202
        repeated = client.post("/v1/baselines", headers=headers, json=body)
        assert repeated.status_code == 200
        assert repeated.json()["run_id"] == accepted.json()["run_id"]
        changed = {**body, "budget": {**body["budget"], "wall_seconds": 61}}
        assert client.post("/v1/baselines", headers=headers, json=changed).status_code == 409
        research = {
            **body,
            "track": "anomaly",
            "budget": {"experiments": 1, "wall_seconds": 60, "model_tokens": 0},
        }
        assert client.post("/v1/runs", headers=headers, json=research).status_code == 409
    with engine.connect() as connection:
        row = connection.execute(select(runs.c.request_json)).one()
    request = row.request_json
    assert request["purpose"] == "baseline"
    assert request["proposal_limit"] == request["budget"]["experiments"] == 0
    assert request["budget"]["model_tokens"] == 0
    assert request["harness_sha256"] == "a" * 64


def test_baseline_cli_authenticates_and_verifies_terminal_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("u" * 32, encoding="utf-8")
    token_file.chmod(0o600)
    report = {
        "schema": "lab.baseline-report.v1",
        "run_id": "00000000-0000-0000-0000-000000000001",
        "purpose": "baseline",
        "status": "completed",
        "calibration_complete": True,
        "budget_verified": True,
        "request_sha256": "a" * 64,
        "suite_id": "fixture.suite",
        "suite_version": 1,
        "suite_manifest_sha256": "b" * 64,
        "harness_sha256": "c" * 64,
        "image_sha256": "d" * 64,
        "task_plan_sha256": "e" * 64,
        "task_plan_count": 9,
        "calibration_sha256": "f" * 64,
        "algorithms": ["robust_z", "iforest", "ecod_train_frozen"],
        "seeds": [0, 1, 2],
        "task_count": 1,
        "score_count": 9,
        "budget_receipt": {"proposal_count": 0, "model_tokens": 0},
        "model_usage": {"input_tokens": 0, "output_tokens": 0, "calls": 0},
        "baseline_records": [],
        "experiment_records": [
            {
                "experiment_id": f"exp_{index:032x}",
                "baseline_name": name,
                "status": "scored",
                "experiment_sha256": f"{index + 1:064x}",
                "trajectory_sha256": f"{index + 11:064x}",
            }
            for index, name in enumerate(("robust_z", "iforest", "ecod_train_frozen"))
        ],
        "task_terminal_outcomes": [],
    }
    report_hash = hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    seen: list[tuple[str, str, dict[str, str]]] = []

    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    class FakeOpener:
        def open(self, request, timeout):
            assert timeout == 10
            seen.append((request.method, request.full_url, dict(request.header_items())))
            if request.method == "POST":
                return FakeResponse(
                    b'{"run_id":"00000000-0000-0000-0000-000000000001","state":"queued","reused":false}'
                )
            if request.full_url.endswith("/report"):
                return FakeResponse(
                    json.dumps({"report_sha256": report_hash, "report": report}).encode()
                )
            return FakeResponse(b'{"state":"completed","stop_requested":false}')

    def fake_build_opener(*_handlers):
        return FakeOpener()

    monkeypatch.setattr("urllib.request.build_opener", fake_build_opener)
    result = _submit_baseline(
        SimpleNamespace(
            token_file=token_file,
            api_url="http://127.0.0.1:8766",
            idempotency_key="baseline-idempotency-key-002",
            suite="synthetic.baseline.v1",
            program_version="fixture-v1",
            wall_seconds=60,
            wait=True,
        )
    )
    assert result["state"] == "completed"
    assert result["report_sha256"] == report_hash
    assert len(seen) == 3
    assert all(headers.get("Authorization") == f"Bearer {'u' * 32}" for _, _, headers in seen)
    with pytest.raises(ValueError, match="loopback"):
        _submit_baseline(
            SimpleNamespace(
                token_file=token_file,
                api_url="http://example.com:8766",
                idempotency_key="baseline-idempotency-key-003",
                suite="synthetic.baseline.v1",
                program_version="fixture-v1",
                wall_seconds=60,
                wait=False,
            )
        )


def test_baseline_http_rejects_redirects_and_ignores_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class RedirectingOpener:
        def open(self, request, timeout):
            calls.append(request.full_url)
            assert timeout == 10
            assert request.get_header("Authorization") == "Bearer dummy-token-for-test"
            raise HTTPError(
                request.full_url,
                302,
                "Found",
                {"Location": "http://192.0.2.1:8766/steal"},
                None,
            )

    def fake_build_opener(*handlers):
        assert any(
            isinstance(handler, ProxyHandler) and handler.proxies == {} for handler in handlers
        )
        redirect = next(
            handler
            for handler in handlers
            if isinstance(handler, HTTPRedirectHandler) and type(handler) is not HTTPRedirectHandler
        )
        assert (
            redirect.redirect_request(
                Request("http://127.0.0.1:8766/start"),
                None,
                302,
                "Found",
                {"Location": "http://192.0.2.1:8766/steal"},
                "http://192.0.2.1:8766/steal",
            )
            is None
        )
        return RedirectingOpener()

    monkeypatch.setattr("urllib.request.build_opener", fake_build_opener)
    with pytest.raises(RuntimeError, match="redirects are not supported"):
        cli._baseline_http_json(
            api_url="http://127.0.0.1:8766",
            token="dummy-token-for-test",
            path="/v1/baselines",
            method="POST",
            body={},
        )
    assert calls == ["http://127.0.0.1:8766/v1/baselines"]
    with pytest.raises(ValueError, match="explicit loopback"):
        cli._baseline_http_json(
            api_url="http://secret@127.0.0.1:8766",
            token="dummy-token-for-test",
            path="/v1/baselines",
            method="POST",
        )


def test_baseline_html_renders_typed_report_without_research_claim() -> None:
    run_id = uuid4()
    report = {
        "schema": "lab.baseline-report.v1",
        "run_id": str(run_id),
        "purpose": "baseline",
        "status": "completed",
        "calibration_complete": True,
        "algorithms": ["robust_z", "iforest", "ecod_train_frozen"],
        "seeds": [0, 1, 2],
        "task_count": 1,
        "score_count": 9,
        "calibration_sha256": "a" * 64,
        "baseline_records": [
            {"experiment_id": "exp-baseline-1", "task_id": "fixture.task", "seed": 0}
        ],
    }
    report_sha = hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    rendered = render_baseline_report(
        run_id, report, report_sha256=report_sha, run_state="completed"
    )
    html = rendered.html.decode("utf-8")
    assert "Baseline kalibrasyon raporu" in html
    assert "araştırma sonucu veya model başarısı değildir" in html
    assert "fixture.task" in html
    assert rendered.sha256 == hashlib.sha256(rendered.html).hexdigest()


def test_baseline_report_requires_verified_full_matrix_for_success() -> None:
    report = {
        "schema": "lab.baseline-report.v1",
        "run_id": "00000000-0000-0000-0000-000000000001",
        "purpose": "baseline",
        "status": "completed",
        "calibration_complete": True,
        "budget_verified": True,
        "request_sha256": "a" * 64,
        "suite_id": "fixture.suite",
        "suite_version": 1,
        "suite_manifest_sha256": "b" * 64,
        "harness_sha256": "c" * 64,
        "image_sha256": "d" * 64,
        "task_plan_sha256": "e" * 64,
        "task_plan_count": 9,
        "calibration_sha256": "f" * 64,
        "algorithms": ["robust_z", "iforest", "ecod_train_frozen"],
        "seeds": [0, 1, 2],
        "task_count": 1,
        "score_count": 9,
        "budget_receipt": {"proposal_count": 0, "model_tokens": 0},
        "model_usage": {"input_tokens": 0, "output_tokens": 0, "calls": 0},
        "baseline_records": [{}] * 9,
        "experiment_records": [
            {
                "experiment_id": f"exp_{index:032x}",
                "baseline_name": name,
                "status": "scored",
                "experiment_sha256": f"{index + 1:064x}",
                "trajectory_sha256": f"{index + 11:064x}",
            }
            for index, name in enumerate(BASELINE_NAMES)
        ],
        "task_terminal_outcomes": [],
    }
    validated = validate_baseline_report(report)
    assert validated["schema"] == "lab.baseline-report.v1"
    assert (
        tuple(item["baseline_name"] for item in validated["experiment_records"]) == BASELINE_NAMES
    )
    invalid = {**report, "score_count": 8}
    with pytest.raises(ValueError, match="verified matrix"):
        validate_baseline_report(invalid)
    for records in (
        report["experiment_records"][:-1],
        [
            report["experiment_records"][0],
            report["experiment_records"][0],
            report["experiment_records"][2],
        ],
    ):
        with pytest.raises(ValueError, match="verified matrix"):
            validate_baseline_report({**report, "experiment_records": records})


def test_stopped_baseline_with_partial_scores_never_claims_complete_calibration() -> None:
    report = {
        "schema": "lab.baseline-report.v1",
        "run_id": "00000000-0000-0000-0000-000000000001",
        "purpose": "baseline",
        "status": "stopped",
        "calibration_complete": False,
        "budget_verified": False,
        "request_sha256": "a" * 64,
        "suite_id": "fixture.suite",
        "suite_version": 1,
        "suite_manifest_sha256": "b" * 64,
        "harness_sha256": "c" * 64,
        "image_sha256": "d" * 64,
        "task_plan_sha256": "e" * 64,
        "task_plan_count": 9,
        "calibration_sha256": None,
        "algorithms": ["robust_z", "iforest", "ecod_train_frozen"],
        "seeds": [0, 1, 2],
        "task_count": 1,
        "score_count": 1,
        "budget_receipt": {"status": "unverified_incomplete", "requested": {}},
        "model_usage": {"input_tokens": 0, "output_tokens": 0, "calls": 0},
        "baseline_records": [{"experiment_id": "exp_partial", "task_id": "task-a", "seed": 0}],
        "experiment_records": [
            {
                "experiment_id": "exp_partial",
                "baseline_name": "robust_z",
                "status": "abandoned",
                "experiment_sha256": "f" * 64,
                "trajectory_sha256": "1" * 64,
            }
        ],
        "task_terminal_outcomes": [
            {"experiment_id": "exp_partial", "outcome": "cancelled"} for _ in range(8)
        ],
    }
    validated = validate_baseline_report(report)
    assert validated["status"] == "stopped"
    assert validated["calibration_complete"] is False
    assert validated["budget_verified"] is False
    with pytest.raises(ValueError, match="only a completed"):
        validate_baseline_report({**report, "calibration_complete": True})


def test_unstarted_stopped_baseline_accepts_only_sealed_empty_plan() -> None:
    report = {
        "schema": "lab.baseline-report.v1",
        "run_id": "00000000-0000-0000-0000-000000000001",
        "purpose": "baseline",
        "status": "stopped",
        "calibration_complete": False,
        "budget_verified": False,
        "request_sha256": "a" * 64,
        "suite_id": "fixture.suite",
        "suite_version": None,
        "suite_manifest_sha256": "b" * 64,
        "harness_sha256": "c" * 64,
        "image_sha256": "d" * 64,
        "task_plan_sha256": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
        "task_plan_count": 0,
        "calibration_sha256": None,
        "algorithms": ["robust_z", "iforest", "ecod_train_frozen"],
        "seeds": [0, 1, 2],
        "task_count": 0,
        "score_count": 0,
        "budget_receipt": {"status": "unverified_incomplete", "requested": {}},
        "model_usage": {"input_tokens": 0, "output_tokens": 0, "calls": 0},
        "baseline_records": [],
        "experiment_records": [],
        "task_terminal_outcomes": [],
    }
    validated = validate_baseline_report(report)
    assert validated["task_plan_count"] == 0
    assert validated["calibration_complete"] is False
    with pytest.raises(ValueError, match="does not resolve"):
        validate_baseline_report({**report, "task_plan_count": 1})
    with pytest.raises(ValueError, match="sealed task plan"):
        validate_baseline_report({**report, "task_plan_sha256": None})
    with pytest.raises(ValueError, match="all zero model-usage"):
        validate_baseline_report({**report, "model_usage": {}})
