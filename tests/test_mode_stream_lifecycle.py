"""CPU product composition; SQL ownership and Docker remain explicit fixture transports."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update

from lab.api import app as api_module
from lab.api.registry import ApiPrincipal, SuiteEntry
from lab.db.schema import run_events, runs
from lab.db.task_plan import canonical_task_plan_digest
from lab.director import mode_stream as operation
from lab.director.journal import canonical_bytes
from lab.director.ownership import ExecutionOwner, owned_execution
from lab.operating_modes.contracts import ModeConfig
from lab.operating_modes.model import fit_model
from lab.operating_modes.stream import load_input, load_train
from lab.sandbox import mode_stream as sandbox
from lab.sandbox.docker_runner import SandboxProfile, SandboxResult
from lab.scorer.jobs import store_artifact_bytes
from lab.scorer.mode_stream_report import finalize_mode_stream
from tests.test_api_and_scorer import _engine
from tests.test_mode_stream_sandbox import output


@pytest.fixture
def admitted(tmp_path, monkeypatch):
    from lab.api import mode_stream as stream_api

    monkeypatch.setattr(api_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        stream_api, "compute_harness_hash", lambda _p: SimpleNamespace(sha256="c" * 64)
    )
    runtime = tmp_path / "data/runtime"
    runtime.mkdir(parents=True, mode=0o700)
    seed_path = runtime / "fixture.json"
    seed_path.write_bytes(b"{}")
    seed_path.chmod(0o600)
    digest = hashlib.sha256(b"{}").hexdigest()
    entry = SuiteEntry(
        suite_id="fixture.seed",
        track="mode",
        program_version="fixture.v1",
        suite_manifest_path="fixture.json",
        suite_manifest_sha256=digest,
        provider="fake-json",
        scenario_path="fixture.json",
        scenario_sha256=digest,
        proposal_limit=1,
    )
    registry_path = runtime / "registry.json"
    registry_path.write_bytes(
        canonical_bytes(
            {"schema": "lab-suite-registry.v1", "suites": [entry.model_dump(mode="json")]}
        )
    )
    registry_path.chmod(0o600)
    monkeypatch.setenv("LAB_SUITE_REGISTRY_FILE", str(registry_path))
    engine = _engine()
    app = api_module.create_app(
        director_engine=engine,
        principals=(
            ApiPrincipal(token="a" * 32, origin="local", owner_id="owner-one"),
            ApiPrincipal(token="b" * 32, origin="local", owner_id="owner-two"),
            ApiPrincipal(token="z" * 32, origin="aos", owner_id="aos-fixture"),
        ),
    )
    headers = {"Authorization": "Bearer " + "a" * 32}
    with TestClient(app) as client:
        source = client.post(
            "/v1/mode-stream-inputs/synthetic",
            headers=headers,
            json={
                "scenario": "step",
                "seed": 8,
                "train_rows": 32,
                "evaluation_rows": 130,
                "chunk_rows": 64,
            },
        )
        assert source.status_code == 201, source.text
        config = ModeConfig(
            lsh_width=10.0, lsh_projections=1, lsh_tables=1, lsh_merge_tables=1, dwell=3
        )
        request = {
            "input_sha256": source.json()["input_sha256"],
            "configuration": config.model_dump(mode="json"),
            "wall_seconds": 600,
            "idempotency_key": "stream-product-fixture-001",
        }
        response = client.post("/v1/mode-streams", headers=headers, json=request)
        assert response.status_code == 202, response.text
        run_id = UUID(response.json()["run_id"])
        with engine.connect() as connection:
            record = (
                connection.execute(select(runs).where(runs.c.run_id == run_id)).mappings().one()
            )
        yield SimpleNamespace(
            engine=engine,
            client=client,
            headers=headers,
            request=request,
            run_id=run_id,
            record=record,
            runtime=runtime,
            registry_path=registry_path,
            config=config,
        )


def test_authenticated_start_idempotency_live_progress_and_narrow_console_wire(admitted):
    from console.app import StreamStatus

    ctx = admitted
    repeat = ctx.client.post("/v1/mode-streams", headers=ctx.headers, json=ctx.request)
    assert repeat.status_code == 200 and repeat.json()["reused"]
    changed = ctx.client.post(
        "/v1/mode-streams", headers=ctx.headers, json={**ctx.request, "wall_seconds": 601}
    )
    assert changed.status_code == 409
    other = {"Authorization": "Bearer " + "b" * 32}
    assert ctx.client.post("/v1/mode-streams", headers=other, json=ctx.request).status_code == 404
    path = f"/v1/runs/{ctx.run_id}/mode-stream"
    assert ctx.client.get(path, headers=other).status_code == 404
    progress = ctx.client.get(path, headers=ctx.headers)
    StreamStatus.model_validate_json(progress.content)
    assert progress.json()["committed_rows"] == 0 and progress.json()["model_sha256"] is None
    assert (
        ctx.client.get(f"/v1/runs/{ctx.run_id}", headers=ctx.headers).json()["purpose"]
        == "mode-stream"
    )
    assert ctx.client.get(path + "/chunks/0", headers=ctx.headers).status_code == 404
    assert ctx.client.get(f"/v1/runs/{ctx.run_id}/report", headers=ctx.headers).status_code == 404
    assert (
        ctx.client.post(
            "/v1/mode-streams", headers={"Authorization": "Bearer " + "z" * 32}, json=ctx.request
        ).status_code
        == 403
    )


def test_registered_stream_dispatch_revalidates_plan_input_and_original_runtime(
    admitted, monkeypatch
):
    import lab.cli as cli
    from lab.api.registry import load_suite_registry

    ctx = admitted
    monkeypatch.setattr(cli, "PROJECT_ROOT", ctx.runtime.parent.parent)
    monkeypatch.setattr(cli, "compute_harness_hash", lambda _p: SimpleNamespace(sha256="c" * 64))
    registry = load_suite_registry(ctx.registry_path, ctx.runtime)
    target = cli._validate_dispatch_target(ctx.run_id, dict(ctx.record), registry)
    assert target.purpose == "mode-stream" and target.proposal_limit == 0
    assert target.contract.wall_seconds == 600 and target.profile_set is None
    monkeypatch.setattr(cli, "compute_harness_hash", lambda _p: SimpleNamespace(sha256="f" * 64))
    with pytest.raises(ValueError, match="originally admitted"):
        cli._validate_dispatch_target(ctx.run_id, dict(ctx.record), registry)


@pytest.mark.parametrize("dead", [True, False])
def test_stream_stop_recovery_proves_exact_dead_owner_before_sandbox_drain(monkeypatch, dead):
    import lab.cli as cli
    from lab.director import recovery, resume
    from tests.test_automatic_stop043 import setup_recovery

    run, row, _director, _planner, apply, owner = setup_recovery(monkeypatch)
    row.update(
        request_json={"purpose": "mode-stream"},
        payload_sha256="c" * 64,
        worker_pid=123,
        worker_start_ticks=99,
        worker_boot_id=str(uuid4()),
        worker_unit=f"swapp-ai-scientist-director-dispatch-{run.hex}.service",
        worker_cgroup=f"/fixture/swapp-ai-scientist-director-dispatch-{run.hex}.service",
    )
    events = []
    monkeypatch.setattr(
        recovery, "_owner_is_proven_dead", lambda *_a: events.append("owner") or dead
    )
    monkeypatch.setattr(resume, "_drain_director_sandbox", lambda *_a: events.append("drain"))
    result = cli._automatic_stopped_baseline_recovery(run, owner)
    assert events == (["owner", "drain"] if dead else ["owner"])
    if dead:
        assert result["state"] == "stopped"
        assert apply.call_args.kwargs["reconcile_interrupted_baseline"] is False
        assert apply.call_args.kwargs["reconcile_interrupted_proposal"] is False
        assert apply.call_args.kwargs["cleanup_deadline"] == row["deadline_at"]
    else:
        assert result["state"] == "stop_requested"
        apply.assert_not_called()


class FixtureLease:
    def __init__(self, ctx, artifacts):
        self.ctx, self.artifacts = ctx, artifacts
        self.run_id = ctx.run_id

    def require_run_active(self):
        with self.ctx.engine.connect() as connection:
            state = connection.execute(
                select(runs.c.state).where(runs.c.run_id == self.run_id)
            ).scalar_one()
        if state != "running":
            raise RuntimeError("director_run_is_not_active")

    def append_checkpoint(self, *, sequence, key, phase, payload, artifact_root):
        self.require_run_active()
        digest = store_artifact_bytes(canonical_bytes(payload), artifact_root=artifact_root)
        receipt = {
            "schema": "director-checkpoint.v1",
            "sequence": sequence,
            "key": key,
            "phase": phase,
            "payload_sha256": digest,
            "blob_sha256": digest,
        }
        with self.ctx.engine.begin() as connection:
            prior = (
                connection.execute(
                    select(run_events.c.event_json).where(
                        run_events.c.run_id == self.run_id,
                        run_events.c.event_type == "director.checkpoint",
                    )
                )
                .scalars()
                .all()
            )
            assert sequence == len(prior)
            assert key not in [item["key"] for item in prior]
            connection.execute(
                insert(run_events).values(
                    event_id=uuid4(),
                    run_id=self.run_id,
                    event_type="director.checkpoint",
                    event_json=receipt,
                )
            )
        return receipt


class DeadlineFixture:
    """Only the PostgreSQL deadline query is substituted; row/checkpoint reads use SQLite."""

    def __init__(self, ctx):
        self.ctx, self.deadline = ctx, datetime.now(UTC) + timedelta(seconds=600)

    @contextmanager
    def connect(self):
        with self.ctx.engine.connect() as connection:
            original = connection.execute

            def execute(statement, *args, **kwargs):
                if str(statement).startswith("SELECT r.request_json,x.deadline_at"):
                    row = {
                        "request_json": self.ctx.record["request_json"],
                        "deadline_at": self.deadline,
                        "execution_sha256": "d" * 64,
                    }
                    return SimpleNamespace(mappings=lambda: SimpleNamespace(one=lambda: row))
                return original(statement, *args, **kwargs)

            connection.execute = execute
            yield connection

    def begin(self):
        return self.ctx.engine.begin()


class PipelineFixture:
    profile = SandboxProfile(timeout_seconds=60)

    def __init__(self, ctx, manifest):
        self.ctx, self.manifest = ctx, manifest
        self.model = fit_model(load_train(ctx.runtime / "mode-stream-inputs", manifest), ctx.config)
        self.calls, self.after_phase = [], lambda *_: None
        self.phase_budgets = []

    def run_phase(self, **kwargs):
        self.calls.append(kwargs["phase"])
        self.phase_budgets.append((kwargs["deadline"], kwargs["timeout_seconds"]))
        binding = sandbox.StreamPhaseContext.model_validate(kwargs["stream_context"])
        if kwargs["phase"] == "mode_stream_fit":
            opaque = b"fixture-only-opaque-fit-not-a-pickle"
            document = {
                "schema_version": "mode-stream-fit.v1",
                "scoring_available": False,
                "input_sha256": binding.input_sha256,
                "candidate_sha256": binding.candidate_sha256,
                "configuration": self.ctx.config.model_dump(mode="json"),
                "configuration_sha256": binding.configuration_sha256,
                "repetition_seed": 0,
                "sampling_seconds": 1,
                "fit_artifact_sha256": hashlib.sha256(opaque).hexdigest(),
                "model": self.model.summary(),
            }
            artifacts = (
                output("model.bin", opaque),
                output("stream-fit.json", canonical_bytes(document)),
            )
        else:
            frame = ipc.open_stream(kwargs["arrow_input"]).read_all().to_pandas()
            prediction = self.model.predict(
                frame, row_offset=binding.row_offset, alarm_state=binding.alarm_state
            )
            document = {
                "schema": "mode-stream-prediction.v1",
                "scoring_available": False,
                "input_sha256": binding.input_sha256,
                "candidate_sha256": binding.candidate_sha256,
                "configuration_sha256": binding.configuration_sha256,
                "fit_artifact_sha256": binding.fit_artifact_sha256,
                "model_sha256": binding.model_sha256,
                "row_offset": binding.row_offset,
                "alarm_in": binding.alarm_state.model_dump(mode="json"),
                "prediction": prediction.to_dict(),
            }
            artifacts = (output("stream-prediction.json", canonical_bytes(document)),)
        self.after_phase(kwargs["phase"], binding.row_offset)
        return SandboxResult(kwargs["phase"], 0, b"", b"", artifacts, 0.01, "fixture-only")


@pytest.fixture
def execution(admitted, monkeypatch):
    import lab.director.task_plan as task_plan
    import lab.sandbox.docker_runner as docker
    import lab.scorer.supervisor as supervisor

    ctx = admitted
    with ctx.engine.begin() as connection:
        connection.execute(update(runs).where(runs.c.run_id == ctx.run_id).values(state="running"))
    manifest = load_input(ctx.runtime / "mode-stream-inputs", ctx.request["input_sha256"])
    plan = operation.plan_from_request(ctx.record["request_json"])
    artifacts = ctx.runtime / "director-artifacts" / str(ctx.run_id)
    artifacts.mkdir(mode=0o700, parents=True)
    pipeline, deadline = PipelineFixture(ctx, manifest), DeadlineFixture(ctx)
    lease = FixtureLease(ctx, artifacts)

    def runner(**kwargs):
        pipeline.profile = kwargs.get("profile", SandboxProfile())
        return pipeline

    monkeypatch.setattr(docker, "LocalDockerRunner", runner)
    monkeypatch.setattr(operation, "assert_execution_owner_transaction", lambda *_a, **_kw: None)

    def seal(engine, *, run_id):
        with engine.begin() as connection:
            connection.execute(
                update(runs)
                .where(runs.c.run_id == run_id)
                .values(
                    task_plan_sha256=canonical_task_plan_digest([]),
                    task_plan_count=0,
                )
            )

    def finalize(run_id, **kwargs):
        report = finalize_mode_stream(
            ctx.engine,
            run_id=run_id,
            artifact_root=artifacts,
            admitted_generation=kwargs["admitted_generation"],
            execution_sha256=kwargs["execution_sha256"],
        )
        return SimpleNamespace(
            exit_code=0, result={"state": "finalized" if report else "not_ready"}
        )

    monkeypatch.setattr(task_plan, "seal_run_task_plan", seal)
    monkeypatch.setattr(supervisor, "run_scorer_finalize_process", finalize)

    def execute(generation=1):
        with owned_execution(ExecutionOwner(ctx.run_id, generation, "e" * 32, "d" * 64)):
            return operation.run_mode_stream(
                deadline,
                ctx.engine,
                run_id=ctx.run_id,
                plan=plan,
                input_root=ctx.runtime / "mode-stream-inputs",
                artifact_root=artifacts,
                lease=lease,
            )

    return SimpleNamespace(
        ctx=ctx,
        pipeline=pipeline,
        execute=execute,
        artifacts=artifacts,
        deadline=deadline,
        plan=plan,
        manifest=manifest,
    )


def test_full_finite_replay_one_fit_progress_chunk_and_unscored_report(execution):
    from console.app import StreamChunk, StreamStatus

    work, ctx = execution, execution.ctx
    result = work.execute()
    assert result["committed_rows"] == 130 and result["committed_chunks"] == 3
    assert work.pipeline.calls == ["mode_stream_fit"] + ["mode_stream_predict"] * 3
    response = ctx.client.get(f"/v1/runs/{ctx.run_id}/mode-stream", headers=ctx.headers)
    status = StreamStatus.model_validate_json(response.content)
    assert status.finished and status.state == "completed" and status.committed_rows == 130
    previous = None
    for index, offset in enumerate((0, 64, 128)):
        chunk = ctx.client.get(
            f"/v1/runs/{ctx.run_id}/mode-stream/chunks/{index}", headers=ctx.headers
        )
        parsed = StreamChunk.model_validate_json(chunk.content)
        assert parsed.row_offset == offset and parsed.previous_chunk_sha256 == previous
        previous = parsed.artifact_sha256
    report = ctx.client.get(f"/v1/runs/{ctx.run_id}/report", headers=ctx.headers).json()["report"]
    assert report["status"] == "completed" and report["scoring_available"] is False
    assert report["schema"] == "lab.mode-stream-report.v1" and "task_scores" not in report
    assert report["committed_rows"] == 130 and len(report["chunk_sha256"]) == 3


def test_interrupted_prefix_resumes_same_fit_and_alarm_without_restarting_input(execution):
    work = execution

    def crash(phase, offset):
        if phase == "mode_stream_predict" and offset == 64:
            raise RuntimeError("fixture process interrupted after first committed chunk")

    work.pipeline.after_phase = crash
    with pytest.raises(RuntimeError, match="interrupted"):
        work.execute()
    progress = work.ctx.client.get(
        f"/v1/runs/{work.ctx.run_id}/mode-stream", headers=work.ctx.headers
    ).json()
    assert progress["committed_rows"] == 64
    work.pipeline.after_phase = lambda *_: None
    work.execute(generation=2)
    assert work.pipeline.calls.count("mode_stream_fit") == 1
    with work.ctx.engine.connect() as connection:
        state = operation.read_stream_state(
            connection,
            run_id=work.ctx.run_id,
            request=work.ctx.record["request_json"],
            artifact_root=work.artifacts,
            verify_predictions=True,
        )
    assert [item["admitted_generation"] for item in state["chunks"]] == [1, 2, 2]
    assert state["committed_rows"] == 130


def test_stop_after_phase_discards_late_chunk_and_keeps_only_committed_prefix(execution):
    work, ctx = execution, execution.ctx

    def stop(phase, offset):
        if phase == "mode_stream_predict" and offset == 64:
            with ctx.engine.begin() as connection:
                connection.execute(
                    update(runs)
                    .where(runs.c.run_id == ctx.run_id)
                    .values(state="stop_requested", stop_requested=True)
                )

    work.pipeline.after_phase = stop
    with pytest.raises(RuntimeError, match="not_active"):
        work.execute()
    response = ctx.client.get(f"/v1/runs/{ctx.run_id}/mode-stream", headers=ctx.headers).json()
    assert response["committed_rows"] == 64 and response["state"] == "stop_requested"
    report = finalize_mode_stream(
        ctx.engine,
        run_id=ctx.run_id,
        artifact_root=work.artifacts,
        admitted_generation=1,
        execution_sha256="d" * 64,
    )
    assert report[0]["status"] == "stopped" and report[0]["committed_rows"] == 64
    with pytest.raises(RuntimeError, match="not_active"):
        work.execute(generation=2)
    assert work.pipeline.calls.count("mode_stream_fit") == 1


def test_expired_original_budget_never_starts_or_refits(execution):
    execution.deadline.deadline = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(TimeoutError, match="original"):
        execution.execute()
    assert execution.pipeline.calls == []


def test_phase_timeout_preserves_first_stop_window_and_original_cleanup_reserve(execution):
    before = operation.time.monotonic()
    original_remaining = (execution.deadline.deadline - datetime.now(UTC)).total_seconds()
    execution.execute()
    assert execution.pipeline.profile.timeout_seconds == 60
    budgets = execution.pipeline.phase_budgets
    assert len({deadline for deadline, _timeout in budgets}) == 1
    assert all(1 <= timeout <= 60 for _deadline, timeout in budgets)
    assert budgets[0][0] == pytest.approx(before + original_remaining - 30, abs=0.05)


def test_cleanup_reserve_does_not_become_new_prediction_time(execution):
    execution.deadline.deadline = datetime.now(UTC) + timedelta(seconds=29)
    with pytest.raises(TimeoutError, match="deadline|budget"):
        execution.execute()
    assert execution.pipeline.calls == []


def test_unstarted_stop_report_has_no_fabricated_execution_pair(admitted):
    ctx = admitted
    with ctx.engine.begin() as connection:
        connection.execute(
            update(runs)
            .where(runs.c.run_id == ctx.run_id)
            .values(state="stop_requested", stop_requested=True)
        )
    report = finalize_mode_stream(
        ctx.engine,
        run_id=ctx.run_id,
        artifact_root=ctx.runtime / "director-artifacts" / str(ctx.run_id),
        admitted_generation=None,
        execution_sha256=None,
        empty_stop=True,
    )
    assert report[0]["committed_rows"] == 0 and report[0]["status"] == "stopped"
    assert "admitted_generation" not in report[0] and "execution_sha256" not in report[0]


def test_corrupt_committed_artifact_fails_closed_on_resume_and_api(execution):
    work = execution

    def crash(phase, offset):
        if phase == "mode_stream_predict" and offset == 64:
            raise RuntimeError("interrupted")

    work.pipeline.after_phase = crash
    with pytest.raises(RuntimeError):
        work.execute()
    with work.ctx.engine.connect() as connection:
        state = operation.read_stream_state(
            connection,
            run_id=work.ctx.run_id,
            request=work.ctx.record["request_json"],
            artifact_root=work.artifacts,
        )
    from lab.scorer.jobs import _blob_path

    path = _blob_path(work.artifacts, state["chunks"][0]["artifact_sha256"], create=False)
    payload = json.loads(path.read_bytes())
    payload["prediction"]["rows"][0]["index"] = 999
    path.write_bytes(canonical_bytes(payload))
    response = work.ctx.client.get(
        f"/v1/runs/{work.ctx.run_id}/mode-stream/chunks/0", headers=work.ctx.headers
    )
    assert response.status_code == 500
    work.pipeline.after_phase = lambda *_: None
    with pytest.raises(ValueError, match="digest|hash"):
        work.execute(generation=2)
    assert work.pipeline.calls.count("mode_stream_fit") == 1
