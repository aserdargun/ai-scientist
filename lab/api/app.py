"""Bearer-authenticated local API for durable, idempotent run control."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from uuid import UUID, uuid4

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Response, status
from pydantic import ValidationError
from sqlalchemy import Engine, create_engine, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from harness.fingerprint import compute_harness_hash
from lab.api.contracts import (
    RunStatusResponse,
    StartBaselineRequest,
    StartRunRequest,
    StartRunResponse,
)
from lab.api.mode_experiments import ModeGridRequest, ModeSnapshotStore, SyntheticSnapshotRequest
from lab.api.mode_sources import DatabaseSnapshotRequest, SourceCatalog, authorize_snapshot
from lab.api.registry import ApiPrincipal, SuiteRegistry, load_principals, load_suite_registry
from lab.db.schema import reports, run_events, runs
from lab.sandbox.docker_runner import DEFAULT_SANDBOX_IMAGE

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAX_RUN_REQUEST_BYTES = 32 * 1024
_LOGGER = logging.getLogger(__name__)


def _recover_holdout_after_stop(
    director: Engine,
    run_id: UUID,
    purpose: str | None,
    admitted_generation: int | None,
    execution_sha256: str | None,
    deadline_at: datetime | str | None,
) -> None:
    """Dispatch bounded stop cleanup using the identity captured by the stop RPC."""
    try:
        if isinstance(deadline_at, str):
            try:
                deadline_at = datetime.fromisoformat(deadline_at)
            except ValueError:
                deadline_at = None
        if not isinstance(deadline_at, datetime) or deadline_at.tzinfo is None:
            _LOGGER.warning("stop cleanup has no immutable execution deadline for run %s", run_id)
            return
        monotonic_deadline = time.monotonic() + max(
            0.0, (deadline_at.astimezone(UTC) - datetime.now(UTC)).total_seconds()
        )

        def remaining_seconds() -> int:
            return max(0, min(30, int(monotonic_deadline - time.monotonic())))

        if purpose == "baseline" and admitted_generation is None and execution_sha256 is None:
            remaining = remaining_seconds()
            if remaining < 1:
                _LOGGER.warning(
                    "baseline stop finalization is pending after its deadline for %s", run_id
                )
                return
            from lab.scorer.supervisor import run_scorer_empty_baseline_stop_process

            finalized = run_scorer_empty_baseline_stop_process(run_id, remaining_seconds=remaining)
        elif admitted_generation is not None and execution_sha256 is not None:
            if purpose != "baseline":
                remaining = remaining_seconds()
                if remaining < 1:
                    _LOGGER.warning(
                        "holdout stop cleanup is pending after its deadline for %s", run_id
                    )
                    return
                try:
                    from lab.scorer.holdout_supervisor import run_holdout_run_recovery_process

                    run_holdout_run_recovery_process(run_id, remaining_seconds=remaining)
                except Exception as exc:
                    _LOGGER.warning("holdout stop cleanup did not finish (%s)", type(exc).__name__)
            remaining = remaining_seconds()
            if remaining < 1:
                _LOGGER.warning(
                    "Scorer stop finalization is pending after its deadline for %s", run_id
                )
                return
            from lab.scorer.supervisor import run_scorer_finalize_process

            finalized = run_scorer_finalize_process(
                run_id,
                admitted_generation=admitted_generation,
                execution_sha256=execution_sha256,
                remaining_seconds=remaining,
            )
        else:
            _LOGGER.warning("stopped run has no finalization owner for run %s", run_id)
            return
        payload = finalized.result
        if finalized.exit_code != 0 or not isinstance(payload, dict):
            _LOGGER.warning("Scorer stop finalization remains pending for run %s", run_id)
            return
        with director.connect() as connection:
            row = (
                connection.execute(
                    select(runs.c.state, runs.c.report_sha256, reports.c.report_json)
                    .join(reports, reports.c.run_id == runs.c.run_id)
                    .where(runs.c.run_id == run_id)
                )
                .mappings()
                .one_or_none()
            )
        report_json = row["report_json"] if row is not None else None
        report_sha = row["report_sha256"] if row is not None else None
        if (
            row is None
            or row["state"] != "stopped"
            or not isinstance(report_json, dict)
            or report_json.get("status") != "stopped"
            or report_sha is None
            or report_sha
            != hashlib.sha256(
                json.dumps(
                    report_json,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
        ):
            _LOGGER.warning("Scorer stop finalization remains pending for run %s", run_id)
            return
        if admitted_generation is not None and (
            report_json.get("admitted_generation") != admitted_generation
            or report_json.get("execution_sha256") != execution_sha256
        ):
            _LOGGER.warning("Scorer stop report owner differs for run %s", run_id)
            return
        _LOGGER.info("verified stopped report for run %s (%s)", run_id, report_sha)
    except Exception as exc:
        _LOGGER.warning("Scorer stop finalization did not finish (%s)", type(exc).__name__)


class RequestBodyLimitMiddleware:
    """Reject oversized run-control bodies before Pydantic materializes them."""

    def __init__(self, app: Any, *, maximum_bytes: int) -> None:
        self.app = app
        self.maximum_bytes = maximum_bytes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        limited = (
            scope.get("type") == "http"
            and scope.get("method") == "POST"
            and scope.get("path") in {"/v1/runs", "/v1/baselines"}
        )
        if not limited:
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                if int(declared) > self.maximum_bytes:
                    await self._too_large(send)
                    return
            except ValueError:
                await self._too_large(send)
                return
        total = 0

        async def bounded_receive() -> dict[str, Any]:
            nonlocal total
            message = await receive()
            if message.get("type") == "http.request":
                total += len(message.get("body", b""))
                if total > self.maximum_bytes:
                    raise BodyTooLarge
            return cast(dict[str, Any], message)

        try:
            await self.app(scope, bounded_receive, send)
        except BodyTooLarge:
            await self._too_large(send)

    async def _too_large(self, send: Any) -> None:
        body = b'{"detail":"request body too large"}'
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class BodyTooLarge(Exception):
    """Internal control flow for bounded request-body reads."""


def _read_secret(path_value: str | None, default: Path) -> str:
    path = Path(path_value) if path_value else default
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"required local credential file is empty: {path.name}")
    return value


def _engine_from_file(path_value: str | None, default: Path) -> Engine:
    dsn = _read_secret(path_value, default)
    if not dsn.startswith("postgresql+psycopg://"):
        raise RuntimeError("database DSN must use the psycopg PostgreSQL driver")
    return create_engine(dsn, pool_pre_ping=True, pool_size=2, max_overflow=0)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _request_purpose(request: object) -> Literal["baseline", "research", "mode-grid"] | None:
    """Classify only an admitted request shape; legacy research has no purpose field."""
    if not isinstance(request, dict):
        return None
    purpose = request.get("purpose", "research")
    if not isinstance(purpose, str) or purpose not in {"baseline", "research"}:
        return None
    try:
        if purpose == "baseline":
            StartBaselineRequest.model_validate(
                {key: request[key] for key in StartBaselineRequest.model_fields if key in request},
                strict=True,
            )
            return "baseline"
        StartRunRequest.model_validate(
            {key: request[key] for key in StartRunRequest.model_fields if key in request},
            strict=True,
        )
    except ValidationError:
        return None
    provider = request.get("provider")
    if provider == "mode-grid":
        return "mode-grid"
    if provider is None or (isinstance(provider, str) and provider in {"fake-json", "local-qwen"}):
        return "research"
    return None


def _status(row: Any) -> RunStatusResponse:
    return RunStatusResponse(
        run_id=row["run_id"],
        origin=row["origin"],
        purpose=_request_purpose(row["request_json"]),
        state=row["state"],
        created_at=_iso(row["created_at"]),
        updated_at=_iso(row["updated_at"]),
        stop_requested=row["stop_requested"],
        report_sha256=row["report_sha256"],
    )


def create_app(
    *,
    director_engine: Engine | None = None,
    director_token: str | None = None,
    owner_id: str = "local:default",
    origin: Literal["local", "aos"] = "local",
    principals: tuple[ApiPrincipal, ...] | None = None,
    suite_registry: SuiteRegistry | None = None,
    allow_unregistered_suites: bool = False,
) -> FastAPI:
    """Build the director API; scorer credentials are never loaded in this process."""
    owned_engines: list[Engine] = []
    if director_engine is None:
        director_engine = _engine_from_file(
            os.environ.get("LAB_DIRECTOR_DSN_FILE"),
            PROJECT_ROOT / "data/runtime/postgres/director.dsn",
        )
        owned_engines.append(director_engine)
    if principals is None:
        configured_principals = os.environ.get("LAB_API_PRINCIPALS_FILE")
        if configured_principals:
            principals = load_principals(Path(configured_principals))
        else:
            director_token = director_token or _read_secret(
                os.environ.get("LAB_DIRECTOR_TOKEN_FILE"),
                PROJECT_ROOT / "data/runtime/postgres/director.token",
            )
            principals = (ApiPrincipal(token=director_token, origin=origin, owner_id=owner_id),)
    if suite_registry is None:
        configured_registry = os.environ.get("LAB_SUITE_REGISTRY_FILE")
        if configured_registry:
            suite_registry = load_suite_registry(
                Path(configured_registry), PROJECT_ROOT / "data/runtime"
            )
    trusted_principals = principals
    app = FastAPI(title="SWAPP AI Scientist Lab API", version="0.1.0")
    app.state.director_engine = director_engine
    app.state.api_principals = principals
    app.state.suite_registry = suite_registry
    app.state.allow_unregistered_suites = allow_unregistered_suites
    app.state.owned_engines = owned_engines
    app.add_middleware(RequestBodyLimitMiddleware, maximum_bytes=MAX_RUN_REQUEST_BYTES)

    @app.on_event("shutdown")
    def dispose_engines() -> None:
        for engine in app.state.owned_engines:
            engine.dispose()

    def principal_from_authorization(authorization: str | None) -> ApiPrincipal:
        if authorization is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
        for principal in trusted_principals:
            if hmac.compare_digest(authorization, f"Bearer {principal.token}"):
                return principal
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "lab-api"}

    @app.post(
        "/v1/runs",
        response_model=StartRunResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_run(
        request: StartRunRequest,
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> StartRunResponse:
        principal = principal_from_authorization(authorization)
        request_json = request.model_dump(mode="json")
        external_ids = (
            request.external_task_id,
            request.external_run_id,
            request.external_action_id,
        )
        if principal.origin == "aos" and any(value is None for value in external_ids):
            raise HTTPException(status_code=422, detail="AOS task/run/action identity is required")
        if principal.origin == "local" and any(value is not None for value in external_ids):
            raise HTTPException(status_code=422, detail="external identity is reserved for AOS")
        entry = None
        registry = app.state.suite_registry
        if registry is None:
            if not app.state.allow_unregistered_suites:
                raise HTTPException(status_code=503, detail="trusted suite registry unavailable")
        else:
            try:
                entry = registry.get(request.suite)
                registry.verify_entry(entry)
            except (KeyError, ValueError):
                raise HTTPException(status_code=422, detail="suite is not available") from None
            if "research" not in entry.allowed_purposes:
                raise HTTPException(status_code=422, detail="suite does not allow research runs")
            if (
                entry.track != request.track
                or entry.program_version != request.program_version
                or request.budget.experiments > entry.proposal_limit
            ):
                raise HTTPException(status_code=422, detail="run request exceeds registered suite")
            if entry.provider == "mode-grid" and (
                request.track != "mode" or request.budget.model_tokens != 0
            ):
                raise HTTPException(
                    status_code=422, detail="mode-grid requires mode track and zero model tokens"
                )
            request_json.update(
                {
                    "suite_manifest_sha256": entry.suite_manifest_sha256,
                    "scenario_sha256": entry.scenario_sha256,
                    "provider": entry.provider,
                    "provider_config_sha256": entry.provider_config_sha256,
                    "provider_registry_entry_sha256": registry.entry_sha256(entry),
                    "proposal_limit": request.budget.experiments,
                    **(
                        {"study_kind": "single_snapshot_study"}
                        if entry.provider == "mode-grid"
                        else {}
                    ),
                }
            )
        if entry is None:
            request_json.update({"proposal_limit": request.budget.experiments})
        payload_json = json.dumps(
            {key: value for key, value in request_json.items() if key != "idempotency_key"},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        run_id = uuid4()
        created_at = datetime.now(UTC)
        try:
            with app.state.director_engine.begin() as connection:
                connection.execute(
                    insert(runs).values(
                        run_id=run_id,
                        origin=principal.origin,
                        owner_id=principal.owner_id,
                        external_task_id=request.external_task_id,
                        external_run_id=request.external_run_id,
                        external_action_id=request.external_action_id,
                        idempotency_key=request.idempotency_key,
                        payload_sha256=payload_hash,
                        request_json=request_json,
                        state="queued",
                        created_at=created_at,
                        updated_at=created_at,
                        stop_requested=False,
                    )
                )
                connection.execute(
                    insert(run_events).values(
                        event_id=uuid4(),
                        run_id=run_id,
                        event_type="run.accepted",
                        event_json={
                            "origin": principal.origin,
                            "payload_sha256": payload_hash,
                            "suite_manifest_sha256": request_json.get("suite_manifest_sha256"),
                            "proposal_limit": request_json["proposal_limit"],
                        },
                        created_at=created_at,
                    )
                )
            return StartRunResponse(run_id=run_id, state="queued", reused=False)
        except IntegrityError:
            with app.state.director_engine.connect() as connection:
                existing = (
                    connection.execute(
                        select(runs).where(
                            runs.c.origin == principal.origin,
                            runs.c.owner_id == principal.owner_id,
                            runs.c.idempotency_key == request.idempotency_key,
                        )
                    )
                    .mappings()
                    .first()
                )
            if existing is None or existing["payload_sha256"] != payload_hash:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="idempotency key was used with a different request",
                ) from None
            response.status_code = status.HTTP_200_OK
            return StartRunResponse(run_id=existing["run_id"], state=existing["state"], reused=True)

    def mode_store() -> ModeSnapshotStore:
        from lab.api.mode_experiments import ModeSnapshotStore

        return ModeSnapshotStore(PROJECT_ROOT / "data/runtime/mode-snapshots")

    def mode_principal(authorization: str | None) -> ApiPrincipal:
        principal = principal_from_authorization(authorization)
        if principal.origin != "local":
            raise HTTPException(status_code=403, detail="local mode-study access required")
        return principal

    def source_catalog() -> SourceCatalog:
        configured = os.environ.get("LAB_MODE_SOURCE_CATALOG_FILE")
        return SourceCatalog(Path(configured) if configured else None)

    @app.get("/v1/mode-sources")
    def mode_sources(authorization: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        principal = mode_principal(authorization)
        try:
            return {"items": source_catalog().available(principal.owner_id)}
        except (ValueError, OSError):
            raise HTTPException(status_code=503, detail="source catalog unavailable") from None

    @app.post("/v1/mode-snapshots/database")
    def database_snapshot(
        request: DatabaseSnapshotRequest, authorization: Annotated[str | None, Header()] = None
    ) -> dict[str, Any]:
        principal = mode_principal(authorization)
        try:
            return source_catalog().capture(request, owner=principal.owner_id, store=mode_store())
        except Exception:
            # Database exceptions can contain hostnames, SQL and source values.
            raise HTTPException(
                status_code=422, detail="configured source snapshot unavailable"
            ) from None

    @app.post("/v1/mode-snapshots")
    def create_mode_snapshot(
        request: SyntheticSnapshotRequest, authorization: Annotated[str | None, Header()] = None
    ) -> dict[str, Any]:
        mode_principal(authorization)
        try:
            return mode_store().create_synthetic(request)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @app.get("/v1/mode-snapshots/{digest}")
    def describe_mode_snapshot(
        digest: str, authorization: Annotated[str | None, Header()] = None
    ) -> dict[str, Any]:
        principal = mode_principal(authorization)
        try:
            authorize_snapshot(mode_store(), digest, principal.owner_id)
            return mode_store().describe(digest)
        except (ValueError, OSError):
            raise HTTPException(status_code=404, detail="snapshot unavailable") from None

    @app.get("/v1/mode-snapshots/{digest}/statistics")
    def snapshot_statistics(
        digest: str,
        partition: Literal["all", "train", "evaluation"] = "all",
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        principal = mode_principal(authorization)
        try:
            authorize_snapshot(mode_store(), digest, principal.owner_id)
            return mode_store().statistics(digest, partition)
        except (ValueError, OSError):
            raise HTTPException(status_code=422, detail="snapshot statistics unavailable") from None

    @app.post("/v1/mode-snapshots/{digest}/install")
    def install_mode_snapshot(
        digest: str, authorization: Annotated[str | None, Header()] = None
    ) -> dict[str, Any]:
        principal = mode_principal(authorization)
        from lab.scorer.supervisor import run_mode_snapshot_install

        try:
            store = mode_store()
            authorize_snapshot(store, digest, principal.owner_id)
            store.load(digest)
            result = run_mode_snapshot_install(digest)
            if result["state"] == "capacity_busy":
                raise HTTPException(status_code=409, detail="Scorer is busy; retry installation")
            return store.describe(digest)
        except (ValueError, RuntimeError, OSError):
            raise HTTPException(
                status_code=422, detail="Scorer snapshot installation unavailable"
            ) from None

    @app.post("/v1/mode-experiments", response_model=StartRunResponse, status_code=202)
    def start_mode_experiment(
        request: ModeGridRequest,
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> StartRunResponse:
        principal = mode_principal(authorization)
        from lab.api.contracts import RunBudget
        from lab.api.mode_experiments import register_grid

        configured = os.environ.get("LAB_SUITE_REGISTRY_FILE")
        if not configured:
            raise HTTPException(status_code=503, detail="persistent suite registry unavailable")
        try:
            principal = mode_principal(authorization)
            authorize_snapshot(mode_store(), request.snapshot_sha256, principal.owner_id)
            registry, entry = register_grid(
                mode_store(), request, Path(configured), PROJECT_ROOT / "data/runtime"
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        app.state.suite_registry = registry
        return start_run(
            StartRunRequest(
                idempotency_key=request.idempotency_key,
                track=entry.track,
                suite=entry.suite_id,
                program_version=entry.program_version,
                budget=RunBudget(
                    experiments=entry.proposal_limit,
                    wall_seconds=request.wall_seconds,
                    model_tokens=0,
                ),
            ),
            response,
            authorization,
        )

    @app.post(
        "/v1/baselines",
        response_model=StartRunResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_baseline(
        request: StartBaselineRequest,
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> StartRunResponse:
        """Admit an authenticated baseline intent into the common owner/key namespace."""
        principal = principal_from_authorization(authorization)
        if principal.origin != "local":
            raise HTTPException(status_code=422, detail="baseline operations are local only")
        registry = app.state.suite_registry
        if registry is None:
            raise HTTPException(status_code=503, detail="trusted suite registry unavailable")
        try:
            entry = registry.get(request.suite)
            registry.verify_entry(entry)
        except (KeyError, ValueError):
            raise HTTPException(status_code=422, detail="suite is not available") from None
        if "baseline" not in entry.allowed_purposes:
            raise HTTPException(status_code=422, detail="suite does not allow baseline operations")
        if entry.program_version != request.program_version:
            raise HTTPException(status_code=422, detail="baseline differs from registered suite")
        request_json: dict[str, Any] = {
            "purpose": "baseline",
            "track": entry.track,
            "suite": request.suite,
            "budget": request.budget.model_dump(mode="json"),
            "program_version": request.program_version,
            "idempotency_key": request.idempotency_key,
            "suite_manifest_sha256": entry.suite_manifest_sha256,
            "scenario_sha256": entry.scenario_sha256,
            "provider": entry.provider,
            "provider_config_sha256": entry.provider_config_sha256,
            "provider_registry_entry_sha256": registry.entry_sha256(entry),
            "proposal_limit": 0,
            "harness_sha256": compute_harness_hash(PROJECT_ROOT).sha256,
            "image_sha256": DEFAULT_SANDBOX_IMAGE.rsplit("@sha256:", 1)[-1].removeprefix("sha256:"),
        }
        payload_json = json.dumps(
            {key: value for key, value in request_json.items() if key != "idempotency_key"},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        run_id = uuid4()
        now = datetime.now(UTC)
        try:
            with app.state.director_engine.begin() as connection:
                connection.execute(
                    insert(runs).values(
                        run_id=run_id,
                        origin=principal.origin,
                        owner_id=principal.owner_id,
                        idempotency_key=request.idempotency_key,
                        payload_sha256=payload_hash,
                        request_json=request_json,
                        state="queued",
                        created_at=now,
                        updated_at=now,
                        stop_requested=False,
                    )
                )
                connection.execute(
                    insert(run_events).values(
                        event_id=uuid4(),
                        run_id=run_id,
                        event_type="run.accepted",
                        event_json={
                            "origin": principal.origin,
                            "purpose": "baseline",
                            "payload_sha256": payload_hash,
                            "suite_manifest_sha256": entry.suite_manifest_sha256,
                            "proposal_limit": 0,
                        },
                        created_at=now,
                    )
                )
            return StartRunResponse(run_id=run_id, state="queued", reused=False)
        except IntegrityError:
            with app.state.director_engine.connect() as connection:
                existing = (
                    connection.execute(
                        select(runs).where(
                            runs.c.origin == principal.origin,
                            runs.c.owner_id == principal.owner_id,
                            runs.c.idempotency_key == request.idempotency_key,
                        )
                    )
                    .mappings()
                    .first()
                )
            if existing is None or existing["payload_sha256"] != payload_hash:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="idempotency key was used with a different request",
                ) from None
            response.status_code = status.HTTP_200_OK
            return StartRunResponse(run_id=existing["run_id"], state=existing["state"], reused=True)

    @app.get(
        "/v1/runs/{run_id}",
        response_model=RunStatusResponse,
    )
    def get_run(
        run_id: UUID, authorization: Annotated[str | None, Header()] = None
    ) -> RunStatusResponse:
        principal = principal_from_authorization(authorization)
        with app.state.director_engine.connect() as connection:
            row = (
                connection.execute(
                    select(runs).where(
                        runs.c.run_id == run_id,
                        runs.c.owner_id == principal.owner_id,
                        runs.c.origin == principal.origin,
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise HTTPException(status_code=404, detail="run not found")
        return _status(row)

    @app.post(
        "/v1/runs/{run_id}/stop",
        response_model=RunStatusResponse,
    )
    def stop_run(
        run_id: UUID,
        background_tasks: BackgroundTasks,
        authorization: Annotated[str | None, Header()] = None,
    ) -> RunStatusResponse:
        principal = principal_from_authorization(authorization)
        dispatch_cleanup = False
        cleanup_purpose: str | None = None
        admitted_generation: int | None = None
        execution_sha256: str | None = None
        cleanup_deadline: datetime | str | None = None
        with app.state.director_engine.begin() as connection:
            postgresql = connection.dialect.name == "postgresql"
            row_query = select(runs).where(
                runs.c.run_id == run_id,
                runs.c.owner_id == principal.owner_id,
                runs.c.origin == principal.origin,
            )
            row = (
                connection.execute(row_query if postgresql else row_query.with_for_update())
                .mappings()
                .first()
            )
            if row is None:
                raise HTTPException(status_code=404, detail="run not found")
            if not postgresql and isinstance(row["request_json"], dict):
                cleanup_purpose = row["request_json"].get("purpose")
            if row["state"] in {"queued", "running", "stop_requested"}:
                if postgresql:
                    stop_receipt = connection.execute(
                        text("SELECT lab.request_director_run_stop(:run_id, :owner_id, :origin)"),
                        {
                            "run_id": run_id,
                            "owner_id": principal.owner_id,
                            "origin": principal.origin,
                        },
                    ).scalar_one()
                    if not isinstance(stop_receipt, dict):
                        raise RuntimeError("Director stop RPC returned an invalid receipt")
                    cleanup_purpose = stop_receipt.get("purpose")
                    receipt_generation = stop_receipt.get("admitted_generation")
                    receipt_sha = stop_receipt.get("execution_sha256")
                    receipt_deadline = stop_receipt.get("deadline_at")
                    if receipt_deadline is not None and not isinstance(receipt_deadline, str):
                        raise RuntimeError("Director stop RPC returned an invalid deadline")
                    cleanup_deadline = receipt_deadline
                    if receipt_generation is not None and (
                        isinstance(receipt_generation, bool)
                        or not isinstance(receipt_generation, int)
                        or not isinstance(receipt_sha, str)
                    ):
                        raise RuntimeError("Director stop RPC returned an invalid owner pair")
                    admitted_generation = receipt_generation
                    execution_sha256 = receipt_sha
                    row = (
                        connection.execute(select(runs).where(runs.c.run_id == run_id))
                        .mappings()
                        .one()
                    )
                else:
                    now = datetime.now(UTC)
                    changed = connection.execute(
                        update(runs)
                        .where(
                            runs.c.run_id == run_id,
                            runs.c.owner_id == principal.owner_id,
                            runs.c.origin == principal.origin,
                            runs.c.state.in_(("queued", "running")),
                        )
                        .values(state="stop_requested", stop_requested=True, updated_at=now)
                    ).rowcount
                    if changed:
                        connection.execute(
                            insert(run_events).values(
                                event_id=uuid4(),
                                run_id=run_id,
                                event_type="run.stop_requested",
                                event_json={},
                                created_at=now,
                            )
                        )
                        row = dict(row)
                        row.update(state="stop_requested", stop_requested=True, updated_at=now)
                    else:
                        row = (
                            connection.execute(select(runs).where(runs.c.run_id == run_id))
                            .mappings()
                            .one()
                        )
            dispatch_cleanup = row["state"] == "stop_requested"
        if dispatch_cleanup:
            background_tasks.add_task(
                _recover_holdout_after_stop,
                app.state.director_engine,
                run_id,
                cleanup_purpose,
                admitted_generation,
                execution_sha256,
                cleanup_deadline,
            )
        return _status(row)

    @app.get("/v1/runs/{run_id}/report")
    def get_report(
        run_id: UUID, authorization: Annotated[str | None, Header()] = None
    ) -> dict[str, Any]:
        principal = principal_from_authorization(authorization)
        with app.state.director_engine.connect() as connection:
            row = (
                connection.execute(
                    select(reports, runs.c.state.label("run_state"))
                    .join(runs, runs.c.run_id == reports.c.run_id)
                    .where(
                        reports.c.run_id == run_id,
                        runs.c.owner_id == principal.owner_id,
                        runs.c.origin == principal.origin,
                        runs.c.state.in_(("completed", "stopped", "failed")),
                        runs.c.report_sha256 == reports.c.report_sha256,
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise HTTPException(status_code=404, detail="verified report not found")
        canonical = json.dumps(
            row["report_json"],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        actual_sha256 = hashlib.sha256(canonical).hexdigest()
        if actual_sha256 != row["report_sha256"]:
            raise HTTPException(status_code=500, detail="stored report integrity check failed")
        if row["report_json"].get("status") != row["run_state"]:
            raise HTTPException(
                status_code=500, detail="report status does not match terminal run state"
            )
        return {
            "run_id": str(row["run_id"]),
            "report_sha256": row["report_sha256"],
            "report": row["report_json"],
            "verified_at": _iso(row["verified_at"]),
        }

    @app.get("/v1/runs/{run_id}/mode-diagnostics/{digest}")
    def get_mode_diagnostics(
        run_id: UUID, digest: str, authorization: Annotated[str | None, Header()] = None
    ) -> dict[str, Any]:
        verified = get_report(run_id, authorization)
        rows = verified["report"].get("task_scores", [])
        matching = [
            row
            for row in rows
            if isinstance(row, dict)
            and isinstance(row.get("mode_diagnostics"), dict)
            and row["mode_diagnostics"].get("artifact_sha256") == digest
        ]
        if not matching:
            raise HTTPException(status_code=404, detail="report diagnostic not found")
        from lab.scorer.jobs import read_candidate_artifact
        from lab.scorer.mode_diagnostics import validate_mode_diagnostics
        from lab.scorer.service import parse_candidate_score

        try:
            artifact = parse_candidate_score(read_candidate_artifact(digest))
            if artifact.mode_diagnostics is None:
                raise ValueError("missing diagnostics")
            row = matching[0]
            diagnostics = validate_mode_diagnostics(
                artifact.mode_diagnostics,
                artifact.scores,
                candidate_sha256=row["candidate_sha256"],
                seed=row["seed"],
            )
            return {
                "artifact_sha256": digest,
                "report_sha256": verified["report_sha256"],
                "candidate_derived": True,
                "diagnostics": diagnostics,
            }
        except (ValueError, OSError):
            raise HTTPException(
                status_code=500, detail="diagnostic integrity check failed"
            ) from None

    return app
