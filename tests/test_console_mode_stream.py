"""Console stream controls keep their bounded, unscored upstream contract."""

from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from test_console_upstream import _upstream_config

from console.app import WatchStore, create_app
from lab.operating_modes.contracts import ModeConfig

HEADERS = {"Origin": "http://127.0.0.1:8788", "X-Lab-Console": "1"}
DIGEST = "a" * 64


def client_for(tmp_path, handler):
    upstream = _upstream_config(tmp_path, handler)
    upstream.model_runs_enabled = False
    app = create_app(upstream, watch_path=tmp_path / "watched.json", dist_root=tmp_path / "absent")
    return TestClient(app, base_url="http://127.0.0.1:8788", client=("127.0.0.1", 1234))


def test_stream_start_cpu_watch_persists_and_reopens(tmp_path):
    run_id = str(uuid4())
    live = {
        "run_id": run_id,
        "origin": "local",
        "purpose": "mode-stream",
        "state": "running",
        "created_at": "2026-10-02T00:00:00Z",
        "updated_at": "2026-10-02T00:00:00Z",
        "stop_requested": False,
        "report_sha256": None,
    }
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "service": "lab-api"})
        if request.url.path == "/v1/mode-streams":
            return httpx.Response(202, json={"run_id": run_id, "state": "running", "reused": False})
        return httpx.Response(200, json=live)

    with client_for(tmp_path, handler) as client:
        response = client.post(
            "/console-api/mode-streams",
            headers=HEADERS,
            json={
                "idempotency_key": "stable-request-123456",
                "input_sha256": DIGEST,
                "configuration": ModeConfig().model_dump(),
                "wall_seconds": 1800,
            },
        )
        assert response.status_code == 202
        assert client.get(f"/console-api/runs/{run_id}").json()["purpose"] == "mode-stream"
    assert WatchStore(tmp_path / "watched.json").list()[0]["purpose"] == "mode-stream"
    assert "/v1/mode-streams" in seen


def test_stream_input_bound_and_browser_guard_deny_before_upstream(tmp_path):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(500)

    with client_for(tmp_path, handler) as client:
        body = {
            "scenario": "drift",
            "seed": 0,
            "train_rows": 192,
            "evaluation_rows": 65537,
            "chunk_rows": 64,
        }
        assert (
            client.post(
                "/console-api/mode-stream-inputs/synthetic", headers=HEADERS, json=body
            ).status_code
            == 422
        )
        assert client.post("/console-api/mode-streams", json={}).status_code == 403
        assert (
            client.get(f"/console-api/runs/{uuid4()}/mode-stream/chunks/65536").status_code == 422
        )
    assert not seen


def test_stream_chunk_rejects_wrong_identity_and_unbounded_rows(tmp_path):
    run_id = str(uuid4())
    chunk = {
        "run_id": run_id,
        "chunk_index": 0,
        "row_offset": 0,
        "row_count": 1,
        "input_sha256": DIGEST,
        "model_sha256": DIGEST,
        "fit_artifact_sha256": DIGEST,
        "previous_chunk_sha256": None,
        "artifact_sha256": DIGEST,
        "candidate_derived": True,
        "scoring_available": False,
        "prediction": {
            "contract_version": "operating-modes.omr.v1",
            "model_sha256": DIGEST,
            "rows": [],
            "alarm_state": {"active": False, "pending": 0},
        },
    }

    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "service": "lab-api"})
        return httpx.Response(200, json=chunk)

    with client_for(tmp_path, handler) as client:
        assert client.get(f"/console-api/runs/{run_id}/mode-stream/chunks/0").status_code == 502
        chunk["row_count"] = 65
        assert client.get(f"/console-api/runs/{run_id}/mode-stream/chunks/0").status_code == 502
        chunk["row_count"] = 1
        chunk["run_id"] = str(uuid4())
        assert client.get(f"/console-api/runs/{run_id}/mode-stream/chunks/0").status_code == 502


def test_stream_progress_preserves_stop_request_and_checks_row_bounds(tmp_path):
    run_id = str(uuid4())
    progress = {
        "run_id": run_id,
        "state": "stop_requested",
        "stop_requested": True,
        "input_sha256": DIGEST,
        "model_sha256": None,
        "fit_artifact_sha256": None,
        "total_rows": 8192,
        "committed_rows": 0,
        "committed_chunks": 0,
        "latest_chunk_index": None,
        "finished": False,
        "scoring_available": False,
        "failure_reason": None,
        "program_version": "mode-stream.v1",
        "source_kind": "synthetic",
        "configuration": ModeConfig().model_dump(),
        "sensors": ["sensor1"],
        "alarm_threshold": None,
    }

    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "service": "lab-api"})
        return httpx.Response(200, json=progress)

    with client_for(tmp_path, handler) as client:
        response = client.get(f"/console-api/runs/{run_id}/mode-stream")
        assert response.status_code == 200
        assert response.json()["stop_requested"] is True
        assert response.json()["finished"] is False
        progress["sensors"] = [f"sensor{index}" for index in range(64)]
        assert client.get(f"/console-api/runs/{run_id}/mode-stream").status_code == 200
        progress["sensors"].append("sensor64")
        assert client.get(f"/console-api/runs/{run_id}/mode-stream").status_code == 502
        progress["sensors"] = ["sensor1"]
        progress["committed_rows"] = 8193
        assert client.get(f"/console-api/runs/{run_id}/mode-stream").status_code == 502
        progress["committed_rows"] = 0
        progress["scoring_available"] = True
        assert client.get(f"/console-api/runs/{run_id}/mode-stream").status_code == 502


def test_stream_input_exact_partition_validation(tmp_path):
    created = {
        "input_sha256": DIGEST,
        "source_kind": "synthetic",
        "sensors": ["sensor1"],
        "train_rows": 192,
        "evaluation_rows": 8192,
        "chunk_count": 128,
        "scoring_available": False,
    }

    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "service": "lab-api"})
        return httpx.Response(200, json=created)

    body = {
        "scenario": "drift",
        "seed": 0,
        "train_rows": 192,
        "evaluation_rows": 8192,
        "chunk_rows": 64,
    }
    with client_for(tmp_path, handler) as client:
        path = "/console-api/mode-stream-inputs/synthetic"
        assert client.post(path, headers=HEADERS, json=body).status_code == 200
        created["sensors"] = [f"sensor{index}" for index in range(64)]
        assert client.post(path, headers=HEADERS, json=body).status_code == 200
        created["sensors"].append("sensor64")
        assert client.post(path, headers=HEADERS, json=body).status_code == 502
        created["sensors"] = ["sensor1"]
        created["chunk_count"] = 127
        assert client.post(path, headers=HEADERS, json=body).status_code == 502


def _source_summary():
    return {
        "source_id": "plant",
        "source_version": "v1",
        "entity": None,
        "units": ["C"],
        "first_utc": "2026-10-02T00:00:00Z",
        "train_end_utc": "2026-10-02T00:00:30Z",
        "last_utc": "2026-10-02T00:01:20Z",
        "sampling_seconds": 2.0,
        "timeline_sha256": DIGEST,
        "source_definition_sha256": DIGEST,
        "gap_count": 1,
        "alarm_basis": "observed_rows",
        "local_export_allowed": False,
    }


def test_database_capture_keeps_key_bounds_provenance_and_conflict(tmp_path):
    import json

    created = {
        "input_sha256": DIGEST,
        "source_kind": "private_database",
        "sensors": ["sensor1"],
        "train_rows": 16,
        "evaluation_rows": 17,
        "chunk_count": 1,
        "scoring_available": False,
        "source": _source_summary(),
    }
    seen = []
    conflict = False

    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "service": "lab-api"})
        seen.append(json.loads(request.content))
        assert request.headers["authorization"].startswith("Bearer ")
        return httpx.Response(
            409 if conflict else 201, json={"detail": "conflict"} if conflict else created
        )

    body = {
        "idempotency_key": "stable-capture-123456",
        "source_id": "plant",
        "sensors": ["sensor1"],
        "start_utc": "2026-10-02T00:00:00Z",
        "end_utc": "2026-10-02T00:02:00Z",
        "entity": None,
        "row_limit": 33,
        "train_rows": 16,
        "chunk_rows": 64,
    }
    with client_for(tmp_path, handler) as client:
        path = "/console-api/mode-stream-inputs/database"
        for _ in range(2):
            response = client.post(path, headers=HEADERS, json=body)
            assert response.status_code == 200
            assert response.json()["source"]["local_export_allowed"] is False
        assert seen[0] == seen[1] == body
        created["evaluation_rows"] = 18
        assert client.post(path, headers=HEADERS, json=body).status_code == 502
        conflict = True
        assert client.post(path, headers=HEADERS, json=body).status_code == 409
        count = len(seen)
        assert (
            client.post(path, headers=HEADERS, json={**body, "row_limit": 65537}).status_code == 422
        )
        assert len(seen) == count


def test_async_capture_cancellation_closes_only_owned_request(tmp_path):
    import asyncio

    cancelled = []
    entered = None

    async def capture(request):
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            cancelled.append(request.url.path)

    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "service": "lab-api"})
        return capture(request)

    upstream = _upstream_config(tmp_path, handler)

    async def run():
        nonlocal entered
        entered = asyncio.Event()
        task = asyncio.create_task(
            upstream.capture_request({"idempotency_key": "stable-capture-123456"})
        )
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)

    asyncio.run(run())
    assert cancelled == ["/v1/mode-stream-inputs/database"]
    assert upstream.health() == (True, None)
    upstream.close()


def test_stream_chunk_keeps_real_utc_gaps_and_rejects_bad_timeline(tmp_path):
    from lab.operating_modes.contracts import PredictionRow

    run_id = str(uuid4())
    row = PredictionRow(
        index=0,
        state="invalid_input",
        reason="missing sensors",
        residuals=(),
        mode_id=None,
        reference_mode_id=None,
        mode_distance=None,
        mode_tolerance=None,
        omr_percent=None,
        alarm=None,
    )
    chunk = {
        "run_id": run_id,
        "chunk_index": 0,
        "row_offset": 0,
        "row_count": 1,
        "input_sha256": DIGEST,
        "model_sha256": DIGEST,
        "fit_artifact_sha256": DIGEST,
        "previous_chunk_sha256": None,
        "artifact_sha256": DIGEST,
        "candidate_derived": True,
        "scoring_available": False,
        "timestamps_utc": ["2026-10-02T00:01:20Z"],
        "gaps_before": [True],
        "prediction": {
            "contract_version": "operating-modes.omr.v1",
            "model_sha256": DIGEST,
            "rows": [row.model_dump(mode="json")],
            "alarm_state": {"active": False, "pending": 0},
        },
    }

    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "service": "lab-api"})
        return httpx.Response(200, json=chunk)

    with client_for(tmp_path, handler) as client:
        path = f"/console-api/runs/{run_id}/mode-stream/chunks/0"
        response = client.get(path)
        assert response.status_code == 200
        assert response.json()["timestamps_utc"] == chunk["timestamps_utc"]
        assert response.json()["gaps_before"] == [True]
        chunk["timestamps_utc"] = ["2026-10-02T03:01:20+03:00"]
        assert client.get(path).status_code == 502
        chunk["timestamps_utc"] = ["2026-10-02T00:01:20Z"]
        chunk["gaps_before"] = []
        assert client.get(path).status_code == 502


def test_database_capture_browser_disconnect_cancels_upstream(tmp_path):
    import asyncio
    import json

    cancelled = []
    entered = None

    async def capture(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(request.url.path)

    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "service": "lab-api"})
        return capture(request)

    upstream = _upstream_config(tmp_path, handler)
    app = create_app(upstream, watch_path=tmp_path / "watched.json", dist_root=tmp_path / "absent")
    body = {
        "idempotency_key": "stable-capture-123456",
        "source_id": "plant",
        "sensors": ["sensor1"],
        "start_utc": "2026-10-02T00:00:00Z",
        "end_utc": "2026-10-02T00:02:00Z",
        "row_limit": 33,
        "train_rows": 16,
    }

    async def run():
        nonlocal entered
        entered = asyncio.Event()
        sent = []
        received = []
        payload = json.dumps(body).encode()
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/console-api/mode-stream-inputs/database",
            "raw_path": b"/console-api/mode-stream-inputs/database",
            "query_string": b"",
            "root_path": "",
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8788),
            "headers": [
                (b"host", b"127.0.0.1:8788"),
                (b"origin", b"http://127.0.0.1:8788"),
                (b"x-lab-console", b"1"),
                (b"content-type", b"application/json"),
            ],
        }

        async def receive():
            # A real middleware-wrapped ASGI receive checkpoints even when a
            # disconnect has become available. Do not monkeypatch Request polling.
            await asyncio.sleep(0)
            if not received:
                received.append("body")
                return {"type": "http.request", "body": payload, "more_body": False}
            await entered.wait()
            received.append("disconnect")
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        before = asyncio.all_tasks()
        await asyncio.wait_for(app(scope, receive, send), timeout=2)
        await asyncio.sleep(0)
        assert any(item["type"] == "http.response.start" and item["status"] == 499 for item in sent)
        assert "disconnect" in received
        assert not (asyncio.all_tasks() - before)

    try:
        asyncio.run(run())
        assert cancelled == ["/v1/mode-stream-inputs/database"]
        assert upstream.health() == (True, None)
    finally:
        upstream.close()
