"""FastAPI routes and browser-origin guard for the local console BFF."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from console.acceptance import (
    evidence_content,
    read_acceptance,
)
from console.checks import CheckManager
from console.host import system_info
from console.upstream import ROOT, Upstream
from lab.api.contracts import (
    RunStatusResponse,
    StartBaselineRequest,
    StartRunRequest,
    StartRunResponse,
)

DIST_ROOT = ROOT / "console/web/dist"
VERSION_FILE = ROOT / "harness/VERSION"
MAX_WATCHED_RUNS = 64


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _version() -> str:
    try:
        value = VERSION_FILE.read_text(encoding="ascii").strip()
    except OSError:
        return "unknown"
    return value if value and len(value) <= 32 else "unknown"


def _uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError:
        raise HTTPException(status_code=422, detail="run_id must be a UUID") from None


class WatchStore:
    """Persist only upstream-verified local run statuses in a private state file."""

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
            path = state_home / "swapp-ai-scientist-console/watched-runs.json"
        self.path = path
        self.rows: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()
        try:
            if self.path.is_symlink() or not self.path.is_file():
                return
            metadata = self.path.stat()
            if metadata.st_mode & 0o077 or metadata.st_size > 64 * 1024:
                return
            document = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                return
            for key, value in document.items():
                try:
                    parsed = str(UUID(key))
                    status = RunStatusResponse.model_validate_json(json.dumps(value), strict=True)
                except (ValueError, ValidationError, TypeError):
                    continue
                if str(status.run_id) == parsed:
                    self.rows[parsed] = status.model_dump(mode="json")
                    if value.get("purpose") in {"baseline", "research", "mode-grid"}:
                        self.rows[parsed]["purpose"] = value["purpose"]
            self.rows = dict(list(self.rows.items())[-MAX_WATCHED_RUNS:])
        except (OSError, ValueError, TypeError):
            self.rows = {}

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(row) for row in self.rows.values()]

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.rows.get(run_id)
            return dict(row) if row else None

    def put(
        self, run_id: str, value: dict[str, Any], *, purpose: str | None = None
    ) -> dict[str, Any]:
        try:
            status = RunStatusResponse.model_validate_json(json.dumps(value), strict=True)
        except ValidationError:
            raise HTTPException(
                status_code=502, detail="Lab API returned invalid run status"
            ) from None
        if str(status.run_id) != run_id:
            raise HTTPException(
                status_code=502, detail="Lab API returned a mismatched run identity"
            )
        with self.lock:
            previous = self.rows.get(run_id, {})
            row = status.model_dump(mode="json")
            operation = purpose or previous.get("purpose")
            if operation in {"baseline", "research", "mode-grid"}:
                row["purpose"] = operation
            self.rows[run_id] = row
            self.rows = dict(list(self.rows.items())[-MAX_WATCHED_RUNS:])
            self._persist()
            return dict(row)

    def _persist(self) -> None:
        parent = self.path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if parent.is_symlink():
            raise OSError("console state directory must not be a symlink")
        parent.chmod(0o700)
        content = json.dumps(self.rows, sort_keys=True, separators=(",", ":"))
        fd, temporary_name = tempfile.mkstemp(prefix=".watched-runs-", dir=parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, self.path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)


def _status_response(value: dict[str, Any], expected_id: str | None = None) -> dict[str, Any]:
    try:
        result = RunStatusResponse.model_validate_json(json.dumps(value), strict=True)
    except ValidationError:
        raise HTTPException(status_code=502, detail="Lab API returned invalid run status") from None
    run_id = str(result.run_id)
    if expected_id is not None and run_id != expected_id:
        raise HTTPException(status_code=502, detail="Lab API returned a mismatched run identity")
    return result.model_dump(mode="json")


def _json_response(upstream: Upstream, method: str, path: str, **kwargs: Any) -> Any:
    response = upstream.request(method, path, **kwargs)
    try:
        return response.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="Lab API returned invalid JSON") from None


def create_app(
    upstream: Upstream | None = None,
    *,
    watch_path: Path | None = None,
    dist_root: Path = DIST_ROOT,
) -> FastAPI:
    api = upstream or Upstream()
    checks = CheckManager()
    watches = WatchStore(watch_path)
    evidence_paths: dict[str, Path] = {}
    evidence_lock = threading.Lock()
    app = FastAPI(title="SWAPP AI Scientist Console", docs_url=None, redoc_url=None)
    app.state.console_upstream = api

    @app.middleware("http")
    async def browser_and_loopback_guard(request: Request, call_next: Any) -> Response:
        host = request.headers.get("host", "").lower()
        allowed_hosts = {"127.0.0.1:8788", "localhost:8788", "[::1]:8788"}
        if host not in allowed_hosts:
            return JSONResponse(status_code=403, content={"detail": "Host is not allowed"})
        client_host = request.client.host if request.client else None
        if client_host not in {"127.0.0.1", "::1", "localhost"}:
            return JSONResponse(status_code=403, content={"detail": "Loopback clients only"})
        if request.url.path.startswith("/console-api/") and request.method not in {
            "GET",
            "HEAD",
            "OPTIONS",
        }:
            if request.headers.get("origin") not in {
                "http://127.0.0.1:8788",
                "http://localhost:8788",
                "http://[::1]:8788",
            }:
                return JSONResponse(status_code=403, content={"detail": "Origin is not allowed"})
            if request.headers.get("x-lab-console") != "1":
                return JSONResponse(
                    status_code=403, content={"detail": "X-Lab-Console header is required"}
                )
            media_type = (
                request.headers.get("content-type", "").split(";", maxsplit=1)[0].strip().lower()
            )
            if media_type != "application/json":
                return JSONResponse(
                    status_code=415, content={"detail": "application/json is required"}
                )
        return cast(Response, await call_next(request))

    @app.get("/console-api/overview")
    def overview() -> dict[str, Any]:
        acceptance, current_evidence = read_acceptance()
        with evidence_lock:
            evidence_paths.update(current_evidence)
        connected, reason = api.health()
        suites = api.suites() if connected else []
        return {
            "version": _version(),
            "generated_at": _now(),
            "acceptance": acceptance,
            "system": system_info(),
            "lab": {
                "configured": api.configured,
                "connected": connected,
                "reason": reason,
                "model_runs_enabled": api.model_runs_enabled,
                "suites": suites,
            },
        }

    @app.get("/console-api/evidence/{evidence_id}")
    def get_evidence(evidence_id: str) -> dict[str, Any]:
        with evidence_lock:
            path = evidence_paths.get(evidence_id)
        if path is None:
            raise HTTPException(status_code=404, detail="evidence not found")
        try:
            return evidence_content(path)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @app.post("/console-api/checks", status_code=202)
    def submit_check(payload: dict[str, Any], background: BackgroundTasks) -> dict[str, str]:
        if payload:
            raise HTTPException(status_code=422, detail="check body must be an empty object")
        return checks.submit(background)

    @app.get("/console-api/checks")
    def list_checks() -> dict[str, list[dict[str, Any]]]:
        return checks.list()

    @app.get("/console-api/checks/{check_id}")
    def get_check(check_id: str) -> dict[str, Any]:
        return checks.get(check_id)

    @app.get("/console-api/runs")
    def list_runs() -> dict[str, list[dict[str, Any]]]:
        cached = watches.list()
        if not cached:
            return {"items": []}
        connected, reason = api.health()
        result: list[dict[str, Any]] = []
        for last in cached:
            run_id = str(last["run_id"])
            if not connected:
                result.append({**last, "stale": True, "unavailable_reason": reason})
                continue
            try:
                live = _status_response(
                    _json_response(api, "GET", f"/v1/runs/{run_id}", _skip_health=True), run_id
                )
                live = watches.put(run_id, live)
                result.append({**live, "stale": False, "unavailable_reason": None})
            except HTTPException as exc:
                result.append({**last, "stale": True, "unavailable_reason": str(exc.detail)})
        return {"items": result}

    @app.post("/console-api/runs/watch")
    def watch_run(payload: dict[str, Any]) -> dict[str, Any]:
        if set(payload) != {"run_id"}:
            raise HTTPException(status_code=422, detail="watch accepts only run_id")
        run_id = _uuid(str(payload["run_id"]))
        live = _status_response(_json_response(api, "GET", f"/v1/runs/{run_id}"), run_id)
        return watches.put(run_id, live)

    @app.get("/console-api/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        parsed = _uuid(run_id)
        value = _json_response(api, "GET", f"/v1/runs/{parsed}")
        live = _status_response(value, parsed)
        return watches.put(parsed, live)

    @app.post("/console-api/runs")
    def start_run(payload: dict[str, Any], response: Response) -> Any:
        return start_operation(payload, response, baseline=False)

    def snapshot_digest(digest: str) -> str:
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise HTTPException(status_code=422, detail="invalid snapshot identity")
        return digest

    @app.get("/console-api/mode-sources")
    def mode_sources() -> Any:
        return _json_response(api, "GET", "/v1/mode-sources")

    @app.post("/console-api/mode-snapshots/database")
    def database_snapshot(payload: dict[str, Any]) -> Any:
        from lab.api.mode_sources import DatabaseSnapshotRequest

        request = DatabaseSnapshotRequest.model_validate(payload, strict=True)
        return _json_response(
            api,
            "POST",
            "/v1/mode-snapshots/database",
            json=request.model_dump(mode="json"),
            timeout=10.0,
        )

    @app.post("/console-api/mode-snapshots")
    def create_snapshot(payload: dict[str, Any]) -> Any:
        from lab.api.mode_experiments import SyntheticSnapshotRequest

        request = SyntheticSnapshotRequest.model_validate(payload, strict=True)
        return _json_response(
            api, "POST", "/v1/mode-snapshots", json=request.model_dump(mode="json")
        )

    @app.get("/console-api/mode-snapshots/{digest}/statistics")
    def mode_statistics(digest: str, partition: str = "all") -> Any:
        if partition not in {"all", "train", "evaluation"}:
            raise HTTPException(status_code=422, detail="invalid partition")
        return _json_response(
            api,
            "GET",
            f"/v1/mode-snapshots/{snapshot_digest(digest)}/statistics",
            params={"partition": partition},
        )

    @app.post("/console-api/mode-snapshots/{digest}/install")
    def install_snapshot(digest: str, payload: dict[str, Any]) -> Any:
        if payload:
            raise HTTPException(status_code=422, detail="installation accepts an empty body")
        return _json_response(
            api,
            "POST",
            f"/v1/mode-snapshots/{snapshot_digest(digest)}/install",
            json={},
            timeout=50.0,
        )

    @app.post("/console-api/mode-experiments")
    def start_mode_grid(payload: dict[str, Any], response: Response) -> Any:
        from lab.api.mode_experiments import ModeGridRequest

        request = ModeGridRequest.model_validate(payload, strict=True)
        if len(watches.list()) >= MAX_WATCHED_RUNS:
            raise HTTPException(status_code=507, detail="console is already watching 64 runs")
        value = _json_response(
            api, "POST", "/v1/mode-experiments", json=request.model_dump(mode="json")
        )
        started = StartRunResponse.model_validate_json(json.dumps(value), strict=True)
        run_id = str(started.run_id)
        live = _status_response(_json_response(api, "GET", f"/v1/runs/{run_id}"), run_id)
        watches.put(run_id, live, purpose="mode-grid")
        response.status_code = 200 if started.reused else 202
        return started.model_dump(mode="json")

    @app.post("/console-api/baselines")
    def start_baseline(payload: dict[str, Any], response: Response) -> Any:
        return start_operation(payload, response, baseline=True)

    def start_operation(payload: dict[str, Any], response: Response, *, baseline: bool) -> Any:
        if not api.configured:
            raise HTTPException(status_code=503, detail="Lab API is not configured")
        if any(key.startswith("external_") for key in payload):
            raise HTTPException(
                status_code=422, detail="external identity fields are not accepted for local runs"
            )
        try:
            contract = StartBaselineRequest if baseline else StartRunRequest
            request = contract.model_validate(payload, strict=True)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors(include_input=False)) from None
        entry = api.registry.entries.get(request.suite) if api.registry is not None else None
        if entry is None:
            raise HTTPException(status_code=422, detail="suite is not registered")
        try:
            assert api.registry is not None
            api.registry.verify_entry(entry)
        except ValueError:
            raise HTTPException(
                status_code=503, detail="registered suite verification failed"
            ) from None
        if (
            isinstance(request, StartRunRequest) and request.track != entry.track
        ) or request.program_version != entry.program_version:
            raise HTTPException(status_code=422, detail="run does not match registered suite")
        if request.budget.experiments > entry.proposal_limit:
            raise HTTPException(
                status_code=422, detail="experiment count exceeds registered suite limit"
            )
        if not baseline and entry.provider == "local-qwen" and not api.model_runs_enabled:
            raise HTTPException(status_code=503, detail="local model runs are disabled")
        if len(watches.list()) >= MAX_WATCHED_RUNS:
            raise HTTPException(status_code=507, detail="console is already watching 64 runs")
        endpoint = "/v1/baselines" if baseline else "/v1/runs"
        upstream_response = api.request("POST", endpoint, json=request.model_dump(mode="json"))
        try:
            value = upstream_response.json()
        except ValueError:
            raise HTTPException(status_code=502, detail="Lab API returned invalid JSON") from None
        try:
            start_response = StartRunResponse.model_validate_json(json.dumps(value), strict=True)
        except ValidationError:
            raise HTTPException(
                status_code=502, detail="Lab API returned invalid run identity"
            ) from None
        run_id = str(start_response.run_id)
        if start_response.state not in {
            "queued",
            "running",
            "stop_requested",
            "completed",
            "stopped",
            "failed",
        }:
            raise HTTPException(status_code=502, detail="Lab API returned an unknown run state")
        if upstream_response.status_code not in {200, 202}:
            raise HTTPException(
                status_code=502, detail="Lab API returned an unexpected start status"
            )
        # A start is accepted only after upstream has durably returned its run identity.
        live = _status_response(_json_response(api, "GET", f"/v1/runs/{run_id}"), run_id)
        watches.put(run_id, live, purpose="baseline" if baseline else "research")
        response.status_code = upstream_response.status_code
        return start_response.model_dump(mode="json")

    @app.get("/console-api/runs/{run_id}/mode-diagnostics/{digest}")
    def get_mode_diagnostics(run_id: str, digest: str) -> Any:
        return _json_response(
            api, "GET", f"/v1/runs/{_uuid(run_id)}/mode-diagnostics/{snapshot_digest(digest)}"
        )

    @app.post("/console-api/runs/{run_id}/stop")
    def stop_run(run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload:
            raise HTTPException(status_code=422, detail="stop body must be an empty object")
        parsed = _uuid(run_id)
        value = _json_response(api, "POST", f"/v1/runs/{parsed}/stop", json={})
        live = _status_response(value, parsed)
        return watches.put(parsed, live)

    @app.get("/console-api/runs/{run_id}/report")
    def run_report(run_id: str) -> Any:
        parsed = _uuid(run_id)
        return _json_response(api, "GET", f"/v1/runs/{parsed}/report")

    if dist_root.is_dir():
        assets = dist_root / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def frontend(path: str) -> Response:
            if path.startswith("console-api/"):
                raise HTTPException(status_code=404, detail="not found")
            candidate = (dist_root / path).resolve()
            try:
                candidate.relative_to(dist_root.resolve())
            except ValueError:
                raise HTTPException(status_code=404, detail="not found") from None
            if candidate.is_file() and not candidate.is_symlink():
                return FileResponse(candidate)
            index = dist_root / "index.html"
            if index.is_file():
                return FileResponse(index)
            raise HTTPException(status_code=404, detail="frontend is not built")

    return app


def main() -> None:
    import uvicorn

    with socket.socket() as listener_probe:
        listener_probe.settimeout(0.2)
        if listener_probe.connect_ex(("127.0.0.1", 8788)) == 0:
            raise SystemExit("port 8788 is already occupied")
    uvicorn.run(create_app(), host="127.0.0.1", port=8788, access_log=False)


if __name__ == "__main__":
    main()
