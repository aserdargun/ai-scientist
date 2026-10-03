"""Authenticated admission and live, committed progress for finite CPU mode replay."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import stat
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from harness.fingerprint import compute_harness_hash
from lab.api.contracts import StartRunResponse
from lab.api.mode_experiments import _register_entry, atomic_private
from lab.api.mode_sources import DatabaseStreamRequest, SourceCatalog, _private_bytes
from lab.api.registry import ApiPrincipal, SuiteEntry
from lab.db.schema import run_events, runs
from lab.director.journal import canonical_bytes
from lab.director.mode_stream import (
    MAX_CHUNK_BYTES,
    PROGRAM,
    SHA,
    make_plan,
    plan_from_request,
    read_stream_state,
    validate_chunk,
)
from lab.operating_modes.contracts import ModeConfig
from lab.operating_modes.stream import (
    SyntheticStreamRequest,
    create_synthetic_input,
    load_input,
    read_chunk_timeline,
)
from lab.sandbox.docker_runner import DEFAULT_SANDBOX_IMAGE
from lab.scorer.jobs import read_artifact_bytes


class CaptureCleanupUnverified(RuntimeError):
    """The owned reader has not been reaped; its durable stage keeps admission closed."""


async def _stop_capture(process: asyncio.subprocess.Process) -> None:
    """Reap only the child created for this HTTP request; never target a database server."""
    if process.returncode is not None:
        return
    try:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await asyncio.wait_for(process.wait(), timeout=1.0)
    except (OSError, TimeoutError) as error:
        raise CaptureCleanupUnverified("owned source reader cleanup is unverified") from error


async def _watch_disconnect(request: Request) -> None:
    """Consume disconnect only after FastAPI has finished reading this request's body."""
    while True:
        if (await request.receive())["type"] == "http.disconnect":
            return


def _require_connected(disconnected: asyncio.Task[None]) -> None:
    if disconnected.done():
        disconnected.result()
        raise HTTPException(499, "source capture cancelled")


async def _capture_process(
    job: dict[str, Any], disconnected: asyncio.Task[None], project_root: Path
) -> str:
    """One finite source reader with disconnect cancellation and bounded connection cleanup."""
    if time.monotonic() >= job["deadline"]:
        raise HTTPException(504, "original source capture deadline expired")
    _require_connected(disconnected)
    env = dict(os.environ)
    env.update(PYTHONPATH=str(project_root), OPENBLAS_NUM_THREADS="2", OMP_NUM_THREADS="2")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "lab.operating_modes.database_stream",
        cwd=project_root,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    communication = asyncio.create_task(process.communicate(canonical_bytes(job)))
    try:
        proc = Path(f"/proc/{process.pid}/stat").read_text()
        start_ticks = int(proc[proc.rfind(")") + 2 :].split()[19])
        atomic_private(
            Path(job["stage_root"]) / "reader.json",
            canonical_bytes(
                {
                    "schema": "mode-stream-reader.v1",
                    "capture_id": job["capture_id"],
                    "pid": process.pid,
                    "start_ticks": start_ticks,
                    "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                    "deadline_monotonic": job["deadline"],
                }
            ),
        )
        while True:
            _require_connected(disconnected)
            remaining = job["deadline"] - time.monotonic()
            if remaining <= 0:
                raise HTTPException(504, "original source capture deadline expired")
            done, _pending = await asyncio.wait(
                {communication, disconnected},
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Disconnect wins even when the reader finishes in the same loop turn.
            _require_connected(disconnected)
            if communication in done:
                output, _stderr = communication.result()
                if process.returncode != 0 or len(output) > 4096:
                    raise HTTPException(422, "configured source capture unavailable")
                value = json.loads(output)
                if not isinstance(value, dict) or set(value) != {"input_sha256"}:
                    raise ValueError("invalid source reader response")
                return str(value["input_sha256"])
    finally:
        cleanup = asyncio.create_task(_stop_capture(process))
        try:
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # ASGI task cancellation is distinct from the observed HTTP
                # disconnect; finish the same bounded reader cleanup in either case.
                try:
                    await asyncio.wait_for(asyncio.shield(cleanup), timeout=3.1)
                except (TimeoutError, asyncio.CancelledError) as error:
                    raise CaptureCleanupUnverified(
                        "owned reader cleanup is still pending"
                    ) from error
                raise
        finally:
            if not communication.done():
                communication.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await communication


def _input_description(manifest: Any) -> dict[str, Any]:
    return {
        "input_sha256": manifest.sha256,
        "source_kind": manifest.source_kind,
        "sensors": list(manifest.sensors),
        "train_rows": manifest.train.rows,
        "evaluation_rows": sum(item.rows for item in manifest.chunks),
        "chunk_count": len(manifest.chunks),
        "scoring_available": False,
        "source": manifest.source_summary,
    }


class StartModeStreamRequest(BaseModel):
    """One finite, owner-bound replay admission without research scoring budgets."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    idempotency_key: str = Field(min_length=16, max_length=128)
    input_sha256: str = Field(pattern=SHA)
    configuration: ModeConfig = Field(default_factory=ModeConfig)
    wall_seconds: int = Field(default=1800, ge=1, le=14400)


def install_mode_stream_routes(
    app: FastAPI,
    authenticate: Callable[[str | None], ApiPrincipal],
    project_root: Path,
) -> None:
    """Expose immutable input creation, queued execution and committed diagnostics."""
    runtime_root = project_root / "data/runtime"
    input_root = runtime_root / "mode-stream-inputs"

    def principal(authorization: str | None) -> ApiPrincipal:
        value = authenticate(authorization)
        if value.origin != "local":
            raise HTTPException(403, "mode streams require an authenticated local owner")
        return value

    def access_file(owner: str, digest: str) -> Path:
        owner_hash = hashlib.sha256(owner.encode("utf-8")).hexdigest()
        return runtime_root / "mode-stream-access" / owner_hash / f"{digest}.json"

    def require_input(owner: str, digest: str) -> None:
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise HTTPException(404, "stream input not found")
        path = access_file(owner, digest)
        expected = canonical_bytes({"owner_id": owner, "input_sha256": digest})
        if path.is_symlink() or not path.is_file() or path.stat().st_size != len(expected):
            raise HTTPException(404, "stream input not found")
        if path.read_bytes() != expected:
            raise HTTPException(404, "stream input not found")

    @app.post("/v1/mode-stream-inputs/database", status_code=201)
    async def create_database_input(
        payload: DatabaseStreamRequest,
        request: Request,
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        owner = principal(authorization)
        issued = time.monotonic()
        disconnected = asyncio.create_task(_watch_disconnect(request))
        captures = runtime_root / "mode-stream-captures"
        descriptor, stage = None, None
        stage_identity = None
        preserve_stage = False
        try:
            # Arm the sole receive consumer before performing any source admission.
            await asyncio.sleep(0)
            _require_connected(disconnected)
            captures.mkdir(mode=0o700, parents=True, exist_ok=True)
            if any(path.is_symlink() for path in (captures, *captures.parents)):
                raise ValueError("invalid source capture directory")
            descriptor = os.open(
                captures / ".capture.lock",
                os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
                raise ValueError("invalid source capture lock")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise HTTPException(409, "source capture is already in progress") from None
            recipe = payload.recipe()
            request_sha = hashlib.sha256(canonical_bytes(recipe)).hexdigest()
            owner_hash = hashlib.sha256(owner.owner_id.encode()).hexdigest()
            key_hash = hashlib.sha256(payload.idempotency_key.encode()).hexdigest()
            receipt_path = captures / owner_hash / f"{key_hash}.json"
            if receipt_path.exists():
                receipt = json.loads(_private_bytes(receipt_path, 4096)[0])
                if (
                    receipt.get("schema") != "mode-stream-capture.v1"
                    or receipt.get("owner_id") != owner.owner_id
                    or receipt.get("request_sha256") != request_sha
                ):
                    raise HTTPException(409, "capture key was used with a different request")
                digest = receipt["input_sha256"]
                manifest = load_input(input_root, digest)
                atomic_private(
                    access_file(owner.owner_id, digest),
                    canonical_bytes({"owner_id": owner.owner_id, "input_sha256": digest}),
                )
                response.status_code = 200
                return _input_description(manifest)
            if any(captures.glob(".stage-*")):
                raise HTTPException(503, "previous source capture cleanup requires verification")
            configured = os.environ.get("LAB_MODE_SOURCE_CATALOG_FILE")
            catalog = SourceCatalog(Path(configured) if configured else None)
            binding = catalog.stream_binding(payload, owner=owner.owner_id)
            deadline = issued + binding["capture_seconds"]

            def active() -> None:
                _require_connected(disconnected)
                if time.monotonic() >= deadline:
                    raise HTTPException(504, "original source capture deadline expired")

            active()
            if shutil.disk_usage(captures).free < 20 * 1024**3:
                raise HTTPException(507, "minimum 20 GiB disk reserve is not available")
            if input_root.is_symlink():
                raise ValueError("stream input root cannot be a symlink")
            input_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if len([path for path in input_root.iterdir() if not path.name.startswith(".")]) >= 64:
                raise HTTPException(507, "bounded stream input registry is full")
            capture_id = uuid4().hex
            stage = captures / f".stage-{capture_id}"
            stage.mkdir(mode=0o700)
            stage_stat = stage.stat()
            stage_identity = (stage_stat.st_dev, stage_stat.st_ino)
            staged_inputs = stage / "inputs"
            digest = await _capture_process(
                {
                    "deadline": deadline,
                    "capture_id": capture_id,
                    "input_root": str(staged_inputs),
                    "stage_root": str(stage),
                    "request": recipe,
                    "definition": binding["definition"],
                    "relation": binding["relation"],
                    "dsn": binding["dsn"],
                },
                disconnected,
                project_root,
            )
            active()
            manifest = load_input(staged_inputs, digest, admission_check=active)
            if (
                manifest.source_kind != "private_database"
                or manifest.source_definition_sha256
                != binding["definition"]["source_definition_sha256"]
                or manifest.request.model_dump(mode="json") != recipe
            ):
                raise ValueError("captured input differs from the authorized source recipe")
            catalog.verify_stream_binding(
                payload, owner=owner.owner_id, expected=binding["private_binding"]
            )
            # Validation above is synchronous and bounded by active(). Yield so
            # the still-live watcher can observe a disconnect before publication.
            await asyncio.sleep(0)
            active()
            target = input_root / digest
            if target.exists():
                if load_input(input_root, digest, admission_check=active) != manifest:
                    raise ValueError("immutable source input differs")
            else:
                (staged_inputs / digest).rename(target)
                directory = os.open(input_root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            await asyncio.sleep(0)
            active()
            # This immutable receipt is the capture commit. A retry repairs an
            # interrupted following access write without reading the live source again.
            atomic_private(
                receipt_path,
                canonical_bytes(
                    {
                        "schema": "mode-stream-capture.v1",
                        "owner_id": owner.owner_id,
                        "request_sha256": request_sha,
                        "input_sha256": digest,
                    }
                ),
            )
            atomic_private(
                access_file(owner.owner_id, digest),
                canonical_bytes({"owner_id": owner.owner_id, "input_sha256": digest}),
            )
            return _input_description(manifest)
        except HTTPException:
            raise
        except CaptureCleanupUnverified:
            preserve_stage = True
            raise HTTPException(503, "owned source reader cleanup is unverified") from None
        except Exception:
            raise HTTPException(422, "configured source capture unavailable") from None
        finally:
            disconnected.cancel()
            await asyncio.gather(disconnected, return_exceptions=True)
            if stage is not None and stage.exists() and not preserve_stage:
                current = stage.lstat()
                if (
                    stat.S_ISDIR(current.st_mode)
                    and (current.st_dev, current.st_ino) == stage_identity
                ):
                    shutil.rmtree(stage)
            if descriptor is not None:
                os.close(descriptor)

    @app.post("/v1/mode-stream-inputs/synthetic", status_code=201)
    def create_input(
        request: SyntheticStreamRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        owner = principal(authorization)
        try:
            manifest = create_synthetic_input(input_root, request)
            atomic_private(
                access_file(owner.owner_id, manifest.sha256),
                canonical_bytes(
                    {
                        "owner_id": owner.owner_id,
                        "input_sha256": manifest.sha256,
                    }
                ),
            )
        except (OSError, ValueError) as error:
            raise HTTPException(422, "bounded stream input could not be created") from error
        return _input_description(manifest)

    @app.post("/v1/mode-streams", response_model=StartRunResponse, status_code=202)
    def start_stream(
        request: StartModeStreamRequest,
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ) -> StartRunResponse:
        owner = principal(authorization)
        require_input(owner.owner_id, request.input_sha256)
        registry_value = os.environ.get("LAB_SUITE_REGISTRY_FILE")
        if not registry_value:
            raise HTTPException(503, "stream admission requires a trusted suite registry")
        try:
            manifest = load_input(input_root, request.input_sha256)
            plan = make_plan(manifest, request.configuration)
            plan_path = runtime_root / "mode-stream-plans" / f"{plan.sha256}.json"
            atomic_private(plan_path, canonical_bytes(plan.document()))
            entry = SuiteEntry(
                suite_id=plan.suite_id,
                track="mode",
                program_version=PROGRAM,
                suite_manifest_path=str(plan_path.relative_to(runtime_root)),
                suite_manifest_sha256=plan.sha256,
                provider="mode-stream",
                provider_config_sha256=plan.configuration_sha256,
                proposal_limit=1,
                allowed_purposes=("mode-stream",),
            )
            registry, entry = _register_entry(entry, Path(registry_value), runtime_root)
            app.state.suite_registry = registry
        except (OSError, ValueError, KeyError) as error:
            raise HTTPException(422, "stream input or registry verification failed") from error
        admitted = {
            "idempotency_key": request.idempotency_key,
            "purpose": "mode-stream",
            "provider": "mode-stream",
            "track": "mode",
            "program_version": PROGRAM,
            "suite": plan.suite_id,
            "suite_manifest_sha256": plan.sha256,
            "provider_config_sha256": plan.configuration_sha256,
            "provider_registry_entry_sha256": registry.entry_sha256(entry),
            "scenario_sha256": None,
            "proposal_limit": 0,
            "budget": {"experiments": 0, "model_tokens": 0, "wall_seconds": request.wall_seconds},
            "mode_stream": plan.document(),
            "harness_sha256": compute_harness_hash(project_root).sha256,
            "image_sha256": DEFAULT_SANDBOX_IMAGE.rsplit("@sha256:", 1)[-1].removeprefix("sha256:"),
        }
        plan_from_request(admitted)
        digest = hashlib.sha256(
            canonical_bytes(
                {key: value for key, value in admitted.items() if key != "idempotency_key"}
            )
        ).hexdigest()
        run_id, now = uuid4(), datetime.now(UTC)
        try:
            with app.state.director_engine.begin() as connection:
                connection.execute(
                    insert(runs).values(
                        run_id=run_id,
                        origin=owner.origin,
                        owner_id=owner.owner_id,
                        idempotency_key=request.idempotency_key,
                        payload_sha256=digest,
                        request_json=admitted,
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
                        created_at=now,
                        event_json={
                            "origin": owner.origin,
                            "purpose": "mode-stream",
                            "payload_sha256": digest,
                            "input_sha256": plan.input_sha256,
                            "scoring_available": False,
                        },
                    )
                )
            return StartRunResponse(run_id=run_id, state="queued", reused=False)
        except IntegrityError:
            with app.state.director_engine.connect() as connection:
                previous = (
                    connection.execute(
                        select(runs).where(
                            runs.c.origin == owner.origin,
                            runs.c.owner_id == owner.owner_id,
                            runs.c.idempotency_key == request.idempotency_key,
                        )
                    )
                    .mappings()
                    .first()
                )
            if previous is None or previous["payload_sha256"] != digest:
                raise HTTPException(
                    409, "idempotency key was used with a different request"
                ) from None
            response.status_code = 200
            return StartRunResponse(run_id=previous["run_id"], state=previous["state"], reused=True)

    def observe(run_id: UUID, authorization: str | None) -> tuple[Any, dict[str, Any], Path]:
        owner = principal(authorization)
        artifact_root = runtime_root / "director-artifacts" / str(run_id)
        with app.state.director_engine.connect() as connection:
            row = (
                connection.execute(
                    select(runs).where(
                        runs.c.run_id == run_id,
                        runs.c.owner_id == owner.owner_id,
                        runs.c.origin == owner.origin,
                    )
                )
                .mappings()
                .first()
            )
            if row is None or row["request_json"].get("purpose") != "mode-stream":
                raise HTTPException(404, "mode stream not found")
            try:
                state = read_stream_state(
                    connection,
                    run_id=run_id,
                    request=row["request_json"],
                    artifact_root=artifact_root,
                )
            except (OSError, ValueError, KeyError) as error:
                raise HTTPException(500, "committed stream evidence failed verification") from error
        return row, state, artifact_root

    @app.get("/v1/runs/{run_id}/mode-stream")
    def progress(
        run_id: UUID, authorization: Annotated[str | None, Header()] = None
    ) -> dict[str, Any]:
        row, state, _ = observe(run_id, authorization)
        plan, fit = state["plan"], state["fit"]
        from lab.operating_modes.stream import _manifest

        _, manifest = _manifest(input_root, plan.input_sha256)
        return {
            "run_id": str(run_id),
            "state": row["state"],
            "stop_requested": row["stop_requested"],
            "program_version": PROGRAM,
            "source_kind": plan.source_kind,
            "source": manifest.source_summary,
            "configuration": plan.configuration.model_dump(mode="json"),
            "sensors": list(plan.sensors),
            "input_sha256": plan.input_sha256,
            "model_sha256": fit["model_sha256"] if fit else None,
            "fit_artifact_sha256": fit["fit_artifact_sha256"] if fit else None,
            "alarm_threshold": fit["model_summary"].get("alarm_threshold") if fit else None,
            "total_rows": plan.total_rows,
            "committed_rows": state["committed_rows"],
            "committed_chunks": len(state["chunks"]),
            "latest_chunk_index": len(state["chunks"]) - 1 if state["chunks"] else None,
            "finished": row["state"] == "completed" and state["finished"],
            "scoring_available": False,
            "failure_reason": state["failure_reason"]
            or ("execution_failed" if row["state"] == "failed" else None),
        }

    @app.get("/v1/runs/{run_id}/mode-stream/chunks/{index}")
    def chunk(
        run_id: UUID, index: int, authorization: Annotated[str | None, Header()] = None
    ) -> dict[str, Any]:
        _, state, artifact_root = observe(run_id, authorization)
        if not 0 <= index < len(state["chunks"]):
            raise HTTPException(404, "committed stream chunk not found")
        metadata = state["chunks"][index]
        try:
            raw = read_artifact_bytes(
                metadata["artifact_sha256"], artifact_root=artifact_root, max_bytes=MAX_CHUNK_BYTES
            )
            value = validate_chunk(raw, plan=state["plan"], run_id=run_id, metadata=metadata)
            from lab.operating_modes.stream import _manifest

            _, manifest = _manifest(input_root, state["plan"].input_sha256)
            timestamps, gaps = read_chunk_timeline(input_root, manifest, index)
        except (OSError, ValueError, KeyError) as error:
            raise HTTPException(500, "committed stream chunk failed verification") from error
        return {
            **{
                key: value[key]
                for key in (
                    "run_id",
                    "chunk_index",
                    "row_offset",
                    "row_count",
                    "input_sha256",
                    "model_sha256",
                    "fit_artifact_sha256",
                    "previous_chunk_sha256",
                    "candidate_derived",
                    "scoring_available",
                    "prediction",
                )
            },
            "artifact_sha256": metadata["artifact_sha256"],
            "timestamps_utc": timestamps,
            "gaps_before": gaps,
        }
