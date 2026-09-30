from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from console.app import create_app
from console.upstream import Upstream, validate_loopback_url


def _private(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def _upstream_config(tmp_path: Path, handler, *, principal_token: str = "l" * 48) -> Upstream:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    manifest = _private(runtime_root / "suite.json", b'{"schema":"test"}\n')
    scenario = _private(runtime_root / "scenario.json", b'{"proposals":[]}\n')
    registry_json = {
        "schema": "lab-suite-registry.v1",
        "suites": [
            {
                "suite_id": "fake.smoke",
                "track": "anomaly",
                "program_version": "director.v1",
                "suite_manifest_path": manifest.name,
                "suite_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                "provider": "fake-json",
                "scenario_path": scenario.name,
                "scenario_sha256": hashlib.sha256(scenario.read_bytes()).hexdigest(),
                "provider_config_sha256": None,
                "proposal_limit": 2,
            }
        ],
    }
    registry = _private(tmp_path / "suite-registry.json", json.dumps(registry_json).encode())
    principal = {
        "schema": "lab-api-principals.v1",
        "principals": [{"token": principal_token, "origin": "local", "owner_id": "local:console"}],
    }
    principal_file = _private(tmp_path / "principals.json", json.dumps(principal).encode())
    transport = httpx.MockTransport(handler)
    return Upstream(
        api_url="http://127.0.0.1:8765",
        token_file=str(principal_file),
        suite_registry_file=str(registry),
        runtime_root=runtime_root,
        transport=transport,
    )


def test_loopback_url_rejects_remote_or_arbitrary_api_origins() -> None:
    assert validate_loopback_url("http://127.0.0.1:8765") == "http://127.0.0.1:8765"
    for value in (
        "https://127.0.0.1:8765",
        "http://example.test:8765",
        "http://user:pass@127.0.0.1:8765",
        "http://127.0.0.1:8765/path",
        "http://127.0.0.1",
    ):
        try:
            validate_loopback_url(value)
        except ValueError:
            continue
        raise AssertionError(f"accepted invalid API URL: {value}")


def test_malformed_principal_error_never_echoes_rejected_secret(tmp_path: Path) -> None:
    malformed_secret = "VISIBLE-MALFORMED-SECRET"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "service": "lab-api"})

    upstream = _upstream_config(tmp_path, handler, principal_token=malformed_secret)
    app = create_app(upstream, watch_path=tmp_path / "watched.json", dist_root=tmp_path / "none")
    with TestClient(app, base_url="http://127.0.0.1:8788", client=("127.0.0.1", 50000)) as client:
        response = client.get("/console-api/overview")
    serialized = json.dumps(response.json())
    assert response.status_code == 200
    assert response.json()["lab"]["configured"] is False
    assert malformed_secret not in serialized
    assert "input_value" not in serialized
    assert "Lab API configuration is invalid" in serialized


@pytest.mark.parametrize(
    ("content", "content_type"),
    [
        (b"<html>different service</html>", "text/html"),
        (b'{"status":"ok","service":"wrong-api"}', "application/json"),
        (
            b'{"status":"ok","service":"lab-api"}' + b" " * 5000,
            "application/json",
        ),
    ],
)
def test_invalid_health_never_sends_bearer_authorization(
    tmp_path: Path, content: bytes, content_type: str
) -> None:
    seen_authorization: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_authorization.append(request.headers.get("authorization"))
        return httpx.Response(
            200,
            content=content,
            headers={"content-type": content_type},
        )

    upstream = _upstream_config(tmp_path, handler)
    connected, reason = upstream.health()
    assert connected is False
    assert reason is not None
    with pytest.raises(HTTPException) as error:
        upstream.request("GET", "/v1/runs/00000000-0000-0000-0000-000000000001")
    assert error.value.status_code == 503
    assert len(seen_authorization) == 2
    assert seen_authorization == [None, None]


def test_health_stream_stops_at_bound_and_closes_oversized_response(tmp_path: Path) -> None:
    class UnboundedBody(httpx.SyncByteStream):
        def __init__(self) -> None:
            self.yielded_bytes = 0
            self.closed = False

        def __iter__(self):
            while True:
                self.yielded_bytes += 1
                yield b"x"

        def close(self) -> None:
            self.closed = True

    stream = UnboundedBody()
    seen_authorization: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_authorization.append(request.headers.get("authorization"))
        return httpx.Response(
            200,
            stream=stream,
            headers={"content-type": "application/json"},
        )

    upstream = _upstream_config(tmp_path, handler)
    connected, reason = upstream.health()

    assert connected is False
    assert reason == "Lab API health response exceeded its size limit"
    assert stream.yielded_bytes == 4097
    assert stream.closed is True
    assert seen_authorization == [None]


def test_unconfigured_console_remains_useful_and_run_actions_explain_unavailable(
    tmp_path: Path,
) -> None:
    app = create_app(watch_path=tmp_path / "watched.json", dist_root=tmp_path / "no-dist")
    with TestClient(app, base_url="http://127.0.0.1:8788", client=("127.0.0.1", 50000)) as client:
        overview = client.get("/console-api/overview")
        assert overview.status_code == 200
        assert overview.json()["acceptance"]["total"] == 22
        assert overview.json()["lab"]["connected"] is False
        start = client.post(
            "/console-api/runs",
            json={"idempotency_key": "x" * 24},
            headers={"Origin": "http://127.0.0.1:8788", "X-Lab-Console": "1"},
        )
        assert start.status_code == 503
        assert "not configured" in start.json()["detail"]
        assert client.get("/console-api/runs").json() == {"items": []}


def test_api_write_requires_origin_and_custom_header(tmp_path: Path) -> None:
    app = create_app(watch_path=tmp_path / "watched.json", dist_root=tmp_path / "none")
    with TestClient(app, base_url="http://127.0.0.1:8788", client=("127.0.0.1", 50000)) as client:
        missing = client.post("/console-api/checks", json={})
        assert missing.status_code == 403
        bad_origin = client.post(
            "/console-api/checks",
            json={},
            headers={"Origin": "https://evil.example", "X-Lab-Console": "1"},
        )
        assert bad_origin.status_code == 403
        bad_type = client.post(
            "/console-api/checks",
            content=b"{}",
            headers={
                "Origin": "http://127.0.0.1:8788",
                "X-Lab-Console": "1",
                "Content-Type": "text/plain",
            },
        )
        assert bad_type.status_code == 415


def test_mocked_real_wire_proxy_keeps_token_server_side_and_validates_status(
    tmp_path: Path,
) -> None:
    run_id = str(uuid4())
    seen_authorization: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_authorization.append(request.headers.get("authorization"))
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "service": "lab-api"})
        if request.url.path == "/v1/runs" and request.method == "POST":
            return httpx.Response(202, json={"run_id": run_id, "state": "queued", "reused": False})
        if request.url.path == f"/v1/runs/{run_id}" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "run_id": run_id,
                    "origin": "local",
                    "state": "queued",
                    "created_at": "2026-09-26T00:00:00+00:00",
                    "updated_at": "2026-09-26T00:00:00+00:00",
                    "stop_requested": False,
                    "report_sha256": None,
                },
            )
        if request.url.path == f"/v1/runs/{run_id}/stop":
            return httpx.Response(
                200,
                json={
                    "run_id": run_id,
                    "origin": "local",
                    "state": "stop_requested",
                    "created_at": "2026-09-26T00:00:00+00:00",
                    "updated_at": "2026-09-26T00:00:01+00:00",
                    "stop_requested": True,
                    "report_sha256": None,
                },
            )
        if request.url.path == f"/v1/runs/{run_id}/report":
            return httpx.Response(
                200,
                json={
                    "run_id": run_id,
                    "report_sha256": "a" * 64,
                    "report": {},
                    "verified_at": "now",
                },
            )
        return httpx.Response(404, json={"detail": "not found"})

    upstream = _upstream_config(tmp_path, handler)
    app = create_app(upstream, watch_path=tmp_path / "watched.json", dist_root=tmp_path / "none")
    write_headers = {"Origin": "http://127.0.0.1:8788", "X-Lab-Console": "1"}
    payload = {
        "idempotency_key": "console-test-key-00001",
        "track": "anomaly",
        "suite": "fake.smoke",
        "budget": {"experiments": 2, "wall_seconds": 60, "model_tokens": 1000},
        "program_version": "director.v1",
    }
    with TestClient(app, base_url="http://127.0.0.1:8788", client=("127.0.0.1", 50000)) as client:
        overview = client.get("/console-api/overview").json()
        assert overview["lab"]["connected"] is True
        assert overview["lab"]["suites"][0]["suite_id"] == "fake.smoke"
        started = client.post("/console-api/runs", json=payload, headers=write_headers)
        assert started.status_code == 202
        assert started.json()["run_id"] == run_id
        assert (
            client.post(
                "/console-api/runs/watch", json={"run_id": run_id}, headers=write_headers
            ).status_code
            == 200
        )
        assert client.get("/console-api/runs").json()["items"][0]["stale"] is False
        assert (
            client.post(f"/console-api/runs/{run_id}/stop", json={}, headers=write_headers).json()[
                "state"
            ]
            == "stop_requested"
        )
        assert client.get(f"/console-api/runs/{run_id}/report").status_code == 200
        evidence_ref = next(
            evidence for item in overview["acceptance"]["items"] for evidence in item["evidence"]
        )
        evidence = client.get(evidence_ref["url"])
        assert evidence.status_code == 200
        assert set(evidence.json()) == {"name", "kind", "content", "sha256", "truncated"}
        assert "l" * 48 not in json.dumps(overview)
        assert "l" * 48 not in json.dumps(started.json())
    assert "Bearer " + "l" * 48 in seen_authorization


def test_start_rejects_aos_identity_and_suite_budget_over_registry_cap(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(500)

    app = create_app(_upstream_config(tmp_path, handler), watch_path=tmp_path / "watched.json")
    headers = {"Origin": "http://127.0.0.1:8788", "X-Lab-Console": "1"}
    payload = {
        "idempotency_key": "console-test-key-00002",
        "track": "anomaly",
        "suite": "fake.smoke",
        "budget": {"experiments": 3, "wall_seconds": 60, "model_tokens": 1000},
        "program_version": "director.v1",
    }
    with TestClient(app, base_url="http://127.0.0.1:8788", client=("127.0.0.1", 50000)) as client:
        too_many = client.post("/console-api/runs", json=payload, headers=headers)
        assert too_many.status_code == 422
        payload["budget"]["experiments"] = 1
        payload["external_run_id"] = "run-" + "a" * 32
        external = client.post("/console-api/runs", json=payload, headers=headers)
        assert external.status_code == 422


def test_baseline_zero_model_lifecycle_and_operation_survive_refresh(tmp_path: Path) -> None:
    """Transport fixture checks wiring; it does not claim CPU experiment evidence."""
    from console.app import WatchStore

    run_id = str(uuid4())
    submitted = []
    status = {
        "run_id": run_id,
        "origin": "local",
        "state": "running",
        "created_at": "2026-09-26T00:00:00+00:00",
        "updated_at": "2026-09-26T00:00:00+00:00",
        "stop_requested": False,
        "report_sha256": None,
    }
    report = {"run_id": run_id, "report": {"purpose": "baseline"}, "report_sha256": "a" * 64}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "service": "lab-api"})
        if request.url.path == "/v1/baselines":
            submitted.append(json.loads(request.content))
            return httpx.Response(202, json={"run_id": run_id, "state": "queued", "reused": False})
        if request.url.path == f"/v1/runs/{run_id}/stop":
            status.update(state="stop_requested", stop_requested=True)
        if request.url.path == f"/v1/runs/{run_id}/report":
            return httpx.Response(200, json=report)
        if request.url.path.startswith(f"/v1/runs/{run_id}"):
            return httpx.Response(200, json=status)
        raise AssertionError(f"unexpected upstream route: {request.method} {request.url.path}")

    upstream = _upstream_config(tmp_path, handler)
    # A baseline must remain available with a registered local model provider disabled.
    entry = upstream.registry.entries["fake.smoke"]
    upstream.registry.entries["fake.smoke"] = entry.model_copy(
        update={
            "provider": "local-qwen",
            "scenario_path": None,
            "scenario_sha256": None,
            "provider_config_sha256": "b" * 64,
        }
    )
    upstream.model_runs_enabled = False
    watch_path = tmp_path / "watched.json"
    app = create_app(upstream, watch_path=watch_path, dist_root=tmp_path / "none")
    headers = {"Origin": "http://127.0.0.1:8788", "X-Lab-Console": "1"}
    payload = {
        "idempotency_key": "console-baseline-00001",
        "suite": "fake.smoke",
        "program_version": "director.v1",
        "budget": {"experiments": 0, "wall_seconds": 60, "model_tokens": 0},
    }
    with TestClient(app, base_url="http://127.0.0.1:8788", client=("127.0.0.1", 50000)) as client:
        for field in ("experiments", "model_tokens"):
            bad = {**payload, "budget": {**payload["budget"], field: 1}}
            assert (
                client.post("/console-api/baselines", json=bad, headers=headers).status_code == 422
            )
        assert submitted == []
        started = client.post("/console-api/baselines", json=payload, headers=headers)
        assert started.status_code == 202
        assert submitted == [payload]
        assert client.get(f"/console-api/runs/{run_id}").json()["purpose"] == "baseline"
        assert client.get("/console-api/runs").json()["items"][0]["purpose"] == "baseline"
        stopped = client.post(f"/console-api/runs/{run_id}/stop", json={}, headers=headers)
        assert stopped.json()["state"] == "stop_requested"
        assert stopped.json()["purpose"] == "baseline"
        assert client.get(f"/console-api/runs/{run_id}/report").json() == report
        research = {
            **payload,
            "track": "anomaly",
            "budget": {**payload["budget"], "experiments": 1},
        }
        assert client.post("/console-api/runs", json=research, headers=headers).status_code == 503
    assert WatchStore(watch_path).get(run_id)["purpose"] == "baseline"
