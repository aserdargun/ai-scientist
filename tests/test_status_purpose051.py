"""Status purpose survives authenticated API reads and existing console watch storage."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, insert
from sqlalchemy.pool import StaticPool

from console.app import WatchStore
from lab.api.app import _request_purpose, create_app
from lab.api.contracts import RunStatusResponse
from lab.api.registry import ApiPrincipal
from lab.db.schema import metadata, runs


def request(purpose="research", provider="fake-json"):
    return {
        "purpose": purpose,
        "provider": provider,
        "track": "anomaly",
        "suite": "public.example",
        "program_version": "test.v1",
        "idempotency_key": "status-purpose051-fixed-key",
        "budget": {
            "experiments": 0 if purpose == "baseline" else 1,
            "wall_seconds": 60,
            "model_tokens": 0,
        },
    }


@pytest.mark.parametrize(
    "payload,expected",
    [
        (request("baseline"), "baseline"),
        (request("baseline", "mode-grid"), "baseline"),
        (request(), "research"),
        (request(provider="local-qwen"), "research"),
        (request(provider="mode-grid"), "mode-grid"),
        ({k: v for k, v in request().items() if k not in {"purpose", "provider"}}, "research"),
        ({}, None),
        (None, None),
        ({"purpose": "research"}, None),
        (request("unknown"), None),
        (request(provider="unknown"), None),
        ({**request(), "budget": {"experiments": "1"}}, None),
    ],
)
def test_classification_requires_admitted_request_shape(payload, expected):
    assert _request_purpose(payload) == expected


@pytest.mark.parametrize(
    "purpose,provider,expected",
    [
        ("baseline", "fake-json", "baseline"),
        ("research", "local-qwen", "research"),
        ("research", "mode-grid", "mode-grid"),
    ],
)
def test_owner_status_reaches_existing_console_watch(tmp_path: Path, purpose, provider, expected):
    raw = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    engine = raw.execution_options(schema_translate_map={"lab": None, "scorer": None})
    metadata.create_all(engine)
    run_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(
            insert(runs).values(
                run_id=run_id,
                origin="local",
                owner_id="owner-one",
                state="running",
                idempotency_key="status-purpose051-fixed-key",
                payload_sha256="a" * 64,
                request_json=request(purpose, provider),
                created_at=now,
                updated_at=now,
                stop_requested=False,
            )
        )
    principals = (
        ApiPrincipal(token="a" * 32, origin="local", owner_id="owner-one"),
        ApiPrincipal(token="b" * 32, origin="local", owner_id="owner-two"),
        ApiPrincipal(token="c" * 32, origin="aos", owner_id="owner-one"),
    )
    app = create_app(director_engine=engine, principals=principals)
    with TestClient(app) as client:
        assert client.get(f"/v1/runs/{run_id}").status_code == 401
        for token in ("b", "c"):
            denied = client.get(
                f"/v1/runs/{run_id}", headers={"Authorization": f"Bearer {token * 32}"}
            )
            assert denied.status_code == 404
            assert "purpose" not in denied.json()
        response = client.get(f"/v1/runs/{run_id}", headers={"Authorization": f"Bearer {'a' * 32}"})
        assert response.status_code == 200
        body = response.json()
        assert body["purpose"] == expected
        assert "request_json" not in body and "provider" not in body
    watched = WatchStore(tmp_path / "watched.json").put(str(run_id), body)
    assert watched["purpose"] == expected
    assert WatchStore(tmp_path / "watched.json").get(str(run_id))["purpose"] == expected
    legacy = {k: v for k, v in body.items() if k != "purpose"}
    assert RunStatusResponse.model_validate(legacy).purpose is None
    engine.dispose()
