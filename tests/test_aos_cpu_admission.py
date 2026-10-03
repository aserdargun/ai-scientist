"""CPU source-only admission evidence; no AOS runtime, model or GPU acceptance."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from lab.api import app as api_module
from lab.api.mode_experiments import ModeGridRequest, register_grid
from lab.api.registry import ApiPrincipal, load_suite_registry
from lab.db.schema import runs
from scripts.register_aos_cpu_study import register_study
from tests.test_mode_agent_admission import installation  # noqa: F401


@pytest.fixture
def cpu_api(installation, monkeypatch):  # noqa: F811
    store, digest, path, runtime, engine, _old = installation
    _, local = register_grid(
        store,
        ModeGridRequest(
            idempotency_key="aos-cpu-local-grid-fixture",
            snapshot_sha256=digest,
            configurations=[{"method": "lsh"}, {"method": "som"}],
        ),
        path,
        runtime,
    )
    kwargs = dict(
        runtime_root=runtime,
        registry_path=path,
        source_suite=local.suite_id,
        owner_id="aos:cpu-fixture",
        max_experiments=1,
        max_wall_seconds=600,
    )
    entry = register_study(**kwargs)
    registry = load_suite_registry(path, runtime)
    monkeypatch.setattr(api_module, "PROJECT_ROOT", runtime.parent.parent)
    monkeypatch.setattr(
        api_module, "compute_harness_hash", lambda *_: SimpleNamespace(sha256="a" * 64)
    )
    principals = (
        ApiPrincipal(token="a" * 32, origin="aos", owner_id="aos:cpu-fixture"),
        ApiPrincipal(token="b" * 32, origin="aos", owner_id="aos:another-owner"),
        ApiPrincipal(token="c" * 32, origin="local", owner_id="aos:cpu-fixture"),
    )
    app = api_module.create_app(
        director_engine=engine, principals=principals, suite_registry=registry
    )
    payload = dict(
        idempotency_key="aos-cpu-admission-fixture",
        track="mode",
        suite=entry.suite_id,
        program_version=entry.program_version,
        budget=dict(experiments=1, wall_seconds=600, model_tokens=0),
        external_task_id="task-" + "1" * 32,
        external_run_id="run-" + "2" * 32,
        external_action_id="action-" + "3" * 32,
        field_intent=dict(
            asset_id="synthetic-pump",
            goal_kind="predictive_maintenance",
            objective="Compare operating modes on synthetic data",
        ),
    )
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client,
            entry=entry,
            registry=registry,
            local=local,
            engine=engine,
            payload=payload,
            kwargs=kwargs,
            runtime=runtime,
            headers={"Authorization": "Bearer " + "a" * 32},
        )
    engine.dispose()


def test_explicit_registration_is_idempotent_and_preserves_local_grid(cpu_api):
    f = cpu_api
    assert register_study(**f.kwargs) == f.entry
    reloaded = load_suite_registry(f.kwargs["registry_path"], f.runtime)
    assert reloaded.get(f.local.suite_id) == f.local
    assert f.entry.suite_id != f.local.suite_id
    assert f.entry.proposal_limit == 1  # Bounded prefix of a two-configuration grid.


def test_aos_cpu_start_freezes_policy_and_intent_and_retries_one_run(cpu_api):
    f = cpu_api
    first = f.client.post("/v1/runs", headers=f.headers, json=f.payload)
    assert first.status_code == 202, first.text
    retry = f.client.post("/v1/runs", headers=f.headers, json=f.payload)
    assert retry.status_code == 200 and retry.json()["run_id"] == first.json()["run_id"]
    with f.engine.connect() as conn:
        row = conn.execute(select(runs)).mappings().one()
    grant = f.entry.aos_cpu_study
    assert row["origin"] == "aos" and row["owner_id"] == grant.owner_id
    assert row["request_json"]["aos_cpu_study"] == grant.model_dump(mode="json")
    assert row["request_json"]["aos_cpu_study_sha256"] == grant.sha256
    assert row["request_json"]["snapshot_sha256"] == grant.snapshot_sha256
    assert row["request_json"]["field_context"]["intent"] == f.payload["field_intent"]
    changed = f.payload | {"field_intent": f.payload["field_intent"] | {"asset_id": "other"}}
    assert f.client.post("/v1/runs", headers=f.headers, json=changed).status_code == 409


@pytest.mark.parametrize(
    "budget",
    [
        dict(experiments=1, wall_seconds=600, model_tokens=1),
        dict(experiments=2, wall_seconds=600, model_tokens=0),
        dict(experiments=1, wall_seconds=601, model_tokens=0),
    ],
)
def test_aos_cpu_rejects_budget_before_queue_insert(cpu_api, budget):
    f = cpu_api
    assert (
        f.client.post(
            "/v1/runs", headers=f.headers, json=f.payload | {"budget": budget}
        ).status_code
        == 422
    )
    with f.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(runs)).scalar_one() == 0


@pytest.mark.parametrize("token", ["b", "c"])
def test_owner_and_origin_cannot_reuse_grant(cpu_api, token):
    f = cpu_api
    payload = dict(f.payload)
    if token == "c":
        for key in ("external_task_id", "external_run_id", "external_action_id"):
            payload.pop(key)
    assert (
        f.client.post(
            "/v1/runs", headers={"Authorization": "Bearer " + token * 32}, json=payload
        ).status_code
        == 403
    )


def test_aos_cannot_start_ungranted_local_grid(cpu_api):
    f = cpu_api
    assert (
        f.client.post(
            "/v1/runs", headers=f.headers, json=f.payload | {"suite": f.local.suite_id}
        ).status_code
        == 403
    )


def test_changed_grid_file_rejected_before_queue_insert(cpu_api):
    f = cpu_api
    (f.runtime / f.entry.scenario_path).write_bytes(b"{}")
    assert f.client.post("/v1/runs", headers=f.headers, json=f.payload).status_code == 422
    with f.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(runs)).scalar_one() == 0
