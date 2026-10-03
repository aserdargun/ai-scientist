"""Capture authority/commit/cancellation tests; PostgreSQL remains a separate live gate."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from lab.api import mode_stream as api
from lab.api.mode_sources import DatabaseStreamRequest, SourceCatalog
from lab.db.schema import runs
from lab.director.mode_stream import make_plan
from lab.operating_modes.stream import create_database_input, load_input
from tests.test_mode_stream_lifecycle import admitted as admitted


@pytest.fixture
def capture_case(admitted, monkeypatch):
    ctx = admitted
    secret = ctx.runtime / "source.dsn"
    secret.write_text("postgresql+psycopg://never-connected-cpu-fixture")
    secret.chmod(0o600)
    path = ctx.runtime / "source-catalog.json"
    catalog = {
        "schema": "mode-source-catalog.v1",
        "sources": [
            {
                "selection": {
                    "source_id": "database-fixture",
                    "source_version": "fixture.v1",
                    "table": "telemetry",
                    "timestamp_column": "observed_at",
                    "entity_column": "asset",
                    "sampling_seconds": 2.5,
                },
                "owners": ["owner-one"],
                "sensors": ["a", "b"],
                "dsn_file": str(secret),
                "stream": {
                    "enabled": True,
                    "schema": "public",
                    "row_limit": 65536,
                    "capture_seconds": 20,
                    "local_export_allowed": False,
                },
            }
        ],
    }
    path.write_text(json.dumps(catalog))
    path.chmod(0o600)
    monkeypatch.setenv("LAB_MODE_SOURCE_CATALOG_FILE", str(path))
    recipe = {
        "idempotency_key": "database-capture-fixture-01",
        "source_id": "database-fixture",
        "sensors": ["b", "a"],
        "start_utc": "2026-01-01T00:00:00Z",
        "end_utc": "2026-01-01T01:00:00Z",
        "entity": "asset-1",
        "row_limit": 90,
        "train_rows": 16,
        "chunk_rows": 64,
    }
    calls = []

    async def reader(job, _request, _root):
        calls.append(job)
        epoch = pd.Timestamp(recipe["start_utc"])
        rows = [
            (
                epoch
                + pd.Timedelta(seconds=2.5 * i + (0.125 if i % 2 else 0) + (60 if i >= 80 else 0)),
                (float(i), float(i + 2)),
            )
            for i in range(86)
        ]
        return create_database_input(
            Path(job["input_root"]),
            source_definition=job["definition"],
            request=job["request"],
            rows=rows,
        ).sha256

    capture_process = api._capture_process
    monkeypatch.setattr(api, "_capture_process", reader)
    return SimpleNamespace(
        ctx=ctx,
        recipe=recipe,
        path=path,
        catalog=catalog,
        calls=calls,
        reader=reader,
        capture_process=capture_process,
    )


def test_capture_timeline_owner_retry_and_live_catalog_change_do_not_recapture(capture_case):
    case, ctx = capture_case, capture_case.ctx
    route = "/v1/mode-stream-inputs/database"
    response = ctx.client.post(route, headers=ctx.headers, json=case.recipe)
    assert response.status_code == 201, response.text
    value = response.json()
    assert value["source_kind"] == "private_database" and value["evaluation_rows"] == 70
    assert value["source"]["sampling_seconds"] == 2.5
    assert value["source"]["units"] == [] and value["source"]["gap_count"] > 0
    assert value["source"]["local_export_allowed"] is False
    manifest = load_input(ctx.runtime / "mode-stream-inputs", value["input_sha256"])
    assert manifest.context_sampling_seconds is None
    plan = make_plan(manifest, ctx.config)
    assert plan.source_kind == "private_database"
    plan.verify_input(manifest)
    # Committed input is an owner-held snapshot, not a second live query or a
    # silent grant to capture a now-revoked source.
    case.catalog["sources"][0]["stream"]["enabled"] = False
    case.path.write_text(json.dumps(case.catalog))
    repeat = ctx.client.post(route, headers=ctx.headers, json=case.recipe)
    assert repeat.status_code == 200 and repeat.json() == value and len(case.calls) == 1
    mismatch = ctx.client.post(route, headers=ctx.headers, json={**case.recipe, "train_rows": 17})
    assert mismatch.status_code == 409 and len(case.calls) == 1
    new_capture = ctx.client.post(
        route,
        headers=ctx.headers,
        json={**case.recipe, "idempotency_key": "different-capture-key-02"},
    )
    assert new_capture.status_code == 422 and len(case.calls) == 1
    other = {"Authorization": "Bearer " + "b" * 32}
    denied = ctx.client.post(
        "/v1/mode-streams",
        headers=other,
        json={
            **ctx.request,
            "input_sha256": value["input_sha256"],
        },
    )
    assert denied.status_code == 404
    started = ctx.client.post(
        "/v1/mode-streams",
        headers=ctx.headers,
        json={
            **ctx.request,
            "idempotency_key": "database-stream-run-01",
            "input_sha256": value["input_sha256"],
        },
    )
    assert started.status_code == 202, started.text
    status = ctx.client.get(f"/v1/runs/{started.json()['run_id']}/mode-stream", headers=ctx.headers)
    assert status.json()["source"] == value["source"]
    assert status.json()["source_kind"] == "private_database"
    assert not list((ctx.runtime / "mode-stream-captures").glob(".stage-*"))


def test_changed_source_scope_discards_stage_before_any_access_receipt(capture_case, monkeypatch):
    case, ctx = capture_case, capture_case.ctx

    async def changed(job, request, root):
        digest = await case.reader(job, request, root)
        case.catalog["sources"][0]["stream"]["local_export_allowed"] = True
        case.path.write_text(json.dumps(case.catalog))
        return digest

    monkeypatch.setattr(api, "_capture_process", changed)
    before = sorted(path.name for path in (ctx.runtime / "mode-stream-inputs").iterdir())
    response = ctx.client.post(
        "/v1/mode-stream-inputs/database", headers=ctx.headers, json=case.recipe
    )
    assert response.status_code == 422
    assert sorted(path.name for path in (ctx.runtime / "mode-stream-inputs").iterdir()) == before
    captures = ctx.runtime / "mode-stream-captures"
    assert list(captures.iterdir()) == [captures / ".capture.lock"]


def test_scope_opt_in_owner_limits_and_no_timestamp_sensor_before_connection(capture_case):
    case = capture_case
    catalog = SourceCatalog(case.path)
    request = DatabaseStreamRequest.model_validate(case.recipe)
    with pytest.raises(ValueError, match="unavailable"):
        catalog.stream_binding(request, owner="owner-two")
    case.catalog["sources"][0]["stream"]["row_limit"] = 80
    case.path.write_text(json.dumps(case.catalog))
    with pytest.raises(ValueError, match="scope"):
        SourceCatalog(case.path).stream_binding(request, owner="owner-one")
    del case.catalog["sources"][0]["stream"]
    case.path.write_text(json.dumps(case.catalog))
    with pytest.raises(ValueError, match="scope"):
        SourceCatalog(case.path).stream_binding(request, owner="owner-one")
    assert case.calls == []


def test_unverified_owned_cleanup_keeps_stage_and_closes_fresh_capture_admission(
    capture_case, monkeypatch
):
    case, ctx = capture_case, capture_case.ctx

    async def unverified(job, *_args):
        Path(job["stage_root"], "reader.json").write_text('{"fixture":"unreaped"}')
        raise api.CaptureCleanupUnverified("fixture owned reap timeout")

    monkeypatch.setattr(api, "_capture_process", unverified)
    before_runs = None
    with ctx.engine.connect() as connection:
        before_runs = connection.execute(select(runs.c.run_id)).all()
    first = ctx.client.post(
        "/v1/mode-stream-inputs/database", headers=ctx.headers, json=case.recipe
    )
    assert first.status_code == 503
    stages = list((ctx.runtime / "mode-stream-captures").glob(".stage-*"))
    assert len(stages) == 1 and (stages[0] / "reader.json").is_file()
    monkeypatch.setattr(api, "_capture_process", case.reader)
    second = ctx.client.post(
        "/v1/mode-stream-inputs/database", headers=ctx.headers, json=case.recipe
    )
    assert second.status_code == 503 and case.calls == []
    with ctx.engine.connect() as connection:
        assert connection.execute(select(runs.c.run_id)).all() == before_runs


class OwnedProcess:
    def __init__(self):
        self.pid, self.returncode, self.signals = os.getpid(), None, []
        self.done = asyncio.Event()

    async def communicate(self, payload):
        assert b"dsn" in payload
        await self.done.wait()
        return b"", b""

    def terminate(self):
        self.signals.append("term")
        self.returncode = -15
        self.done.set()

    def kill(self):
        self.signals.append("kill")
        self.returncode = -9
        self.done.set()

    async def wait(self):
        await self.done.wait()
        return self.returncode


@pytest.mark.parametrize("phase", ["reader", "admission", "connected"])
def test_asgi_disconnect_reaps_reader_and_watcher_without_committing_input(
    capture_case, monkeypatch, phase
):
    case, ctx = capture_case, capture_case.ctx
    calls = []
    before_inputs = sorted(path.name for path in (ctx.runtime / "mode-stream-inputs").iterdir())
    with ctx.engine.connect() as connection:
        before_runs = connection.execute(select(runs.c.run_id)).all()

    async def scenario():
        process, disconnect = OwnedProcess(), asyncio.Event()

        async def spawn(*args, **kwargs):
            calls.append((args, kwargs))
            disconnect.set()
            return process

        if phase == "reader":
            monkeypatch.setattr(api.asyncio, "create_subprocess_exec", spawn)
            monkeypatch.setattr(api, "_capture_process", case.capture_process)
        elif phase == "admission":
            original_load = api.load_input

            def load_with_disconnect(root, *args, **kwargs):
                value = original_load(root, *args, **kwargs)
                if root.parent.name.startswith(".stage-"):
                    disconnect.set()
                return value

            monkeypatch.setattr(api, "load_input", load_with_disconnect)

        body = json.dumps(case.recipe).encode()
        path = "/v1/mode-stream-inputs/database"
        sent, received = [], []
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8788),
            "headers": [
                (b"content-type", b"application/json"),
                (b"authorization", ctx.headers["Authorization"].encode()),
            ],
        }

        async def receive():
            # Real ASGI transport with a cancellation checkpoint. Polling with an
            # already-cancelled scope cannot consume this available disconnect.
            await asyncio.sleep(0)
            if not received:
                received.append("body")
                return {"type": "http.request", "body": body, "more_body": False}
            await disconnect.wait()
            received.append("disconnect")
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        before_tasks = asyncio.all_tasks()
        # Use the product app, including its real request body middleware and
        # FastAPI parsing. Only PostgreSQL/subprocess transport is a CPU fixture.
        await asyncio.wait_for(ctx.client.app(scope, receive, send), timeout=2)
        await asyncio.sleep(0)
        expected = 201 if phase == "connected" else 499
        assert any(
            item["type"] == "http.response.start" and item["status"] == expected
            for item in sent
        ), sent
        assert not (asyncio.all_tasks() - before_tasks)
        if phase == "reader":
            assert process.signals == ["term"] and process.returncode == -15
            assert "never-connected-cpu-fixture" not in str(calls)
        else:
            assert process.signals == []

    asyncio.run(scenario())
    captures = ctx.runtime / "mode-stream-captures"
    assert not list(captures.glob(".stage-*"))
    if phase != "connected":
        assert list(captures.iterdir()) == [captures / ".capture.lock"]
        assert (
            sorted(path.name for path in (ctx.runtime / "mode-stream-inputs").iterdir())
            == before_inputs
        )
    with ctx.engine.connect() as connection:
        assert connection.execute(select(runs.c.run_id)).all() == before_runs


def test_expired_capture_never_spawns_reader(tmp_path, monkeypatch):
    async def spawn(*_args, **_kwargs):
        pytest.fail("expired capture must not start a reader")

    monkeypatch.setattr(api.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(api._capture_process({"deadline": time.monotonic() - 1}, None, tmp_path))
    assert caught.value.status_code == 504


def test_unreaped_child_is_not_reported_as_cleanup_success():
    async def scenario():
        process = OwnedProcess()

        async def timeout():
            raise TimeoutError

        process.wait = timeout
        with pytest.raises(api.CaptureCleanupUnverified):
            await api._stop_capture(process)
        assert process.signals == ["term", "kill"]

    asyncio.run(scenario())


def test_repeated_asgi_cancellation_during_reap_stays_unverified(tmp_path, monkeypatch):
    async def scenario():
        process = OwnedProcess()
        waiting = asyncio.Event()

        async def wait():
            waiting.set()
            await process.done.wait()
            return process.returncode

        def terminate():
            process.signals.append("term")

        process.wait, process.terminate = wait, terminate
        ready = asyncio.Event()
        disconnected = asyncio.create_task(ready.wait())

        async def spawn(*_args, **_kwargs):
            ready.set()
            return process

        monkeypatch.setattr(api.asyncio, "create_subprocess_exec", spawn)
        captured = asyncio.create_task(
            api._capture_process(
                {
                    "deadline": time.monotonic() + 20,
                    "capture_id": "a" * 32,
                    "stage_root": str(tmp_path),
                    "dsn": "private-dsn",
                },
                disconnected,
                tmp_path,
            )
        )
        try:
            await asyncio.wait_for(waiting.wait(), timeout=1)
            captured.cancel()
            await asyncio.sleep(0)
            captured.cancel()
            with pytest.raises(api.CaptureCleanupUnverified):
                await asyncio.wait_for(captured, timeout=1)
            assert process.signals == ["term"] and process.returncode is None
            assert (tmp_path / "reader.json").is_file()
        finally:
            process.returncode = -15
            process.done.set()
            await asyncio.gather(disconnected, return_exceptions=True)
            await asyncio.sleep(0)

    asyncio.run(scenario())
