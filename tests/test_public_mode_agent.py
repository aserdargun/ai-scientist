"""Inert DEV fixtures: separate local-agent authority, never GPU or real model acceptance."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import event, select, update

import lab.cli as cli
from lab.api import app as api_module
from lab.api.mode_experiments import ModeAgentRequest, register_agent
from lab.api.registry import ApiPrincipal, PublicDevAgentStudy, SuiteEntry
from lab.db.schema import dataset_profiles, runs
from lab.director.local_llm import provider_config_sha256
from lab.director.suite_manifest import load_suite_manifest
from scripts.register_public_mode_agent import prepare_policy
from tests import test_public_mode_snapshot

public_installation = test_public_mode_snapshot.public_installation


def _policy(f, **changes):
    grant = prepare_policy(
        f.store,
        snapshot_sha256=f.snapshot.sha256,
        owner_id="field-lab",
        profile_set="research",
        experiments=3,
        wall_seconds=1200,
        model_tokens=180000,
    )
    return PublicDevAgentStudy.model_validate(grant.model_dump(mode="json") | changes)


def _request(f, **changes):
    return ModeAgentRequest.model_validate(
        dict(
            idempotency_key="public-agent-fixture",
            snapshot_sha256=f.snapshot.sha256,
            experiments=2,
            wall_seconds=900,
            model_tokens=120000,
            profile_set="research",
        )
        | changes
    )


def _register(f, policy=None):
    return register_agent(
        f.store,
        _request(f),
        f.registry_path,
        f.runtime,
        public_dev_agent_study=policy or _policy(f),
        operator_grant=True,
    )


def test_separate_grant_preserves_cpu_grant_and_registration(public_installation):
    f = public_installation
    old = f.registry.entry_sha256(f.entry)
    with pytest.raises(ValueError, match="separate registry grant"):
        register_agent(f.store, _request(f), f.registry_path, f.runtime)
    registry, entry = _register(f)
    assert entry.public_dev_study is None
    assert registry.entry_sha256(f.entry) == old
    assert f.entry.public_dev_study.model_tokens == 0
    assert f.entry.public_dev_study.max_wall_seconds == 600
    assert entry.provider_config_sha256 == provider_config_sha256(
        "research", "operating-mode-config.v1", public_fit=True
    )
    assert entry.provider_config_sha256 != provider_config_sha256(
        "research", "operating-mode-config.v1"
    )
    assert _register(f)[1] == entry
    assert registry.public_agent_policy(f.snapshot.sha256, owner_id="other", origin="local") is None
    assert (
        registry.public_agent_policy(f.snapshot.sha256, owner_id="field-lab", origin="aos") is None
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"max_experiments": 36},
        {"max_wall_seconds": 14401},
        {"model_tokens": 350001},
        {"model_tokens": 0},
        {"model_tokens": True},
        {"origin": "aos"},
    ],
)
def test_grant_rejects_unbounded_or_nonlocal_authority(public_installation, changes):
    with pytest.raises(ValidationError):
        _policy(public_installation, **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"experiments": 4},
        {"wall_seconds": 1201},
        {"model_tokens": 180001},
        {"profile_set": "smoke"},
    ],
)
def test_registration_rejects_budget_or_profile_escape(public_installation, changes):
    f = public_installation
    before = f.registry_path.read_bytes()
    with pytest.raises(ValueError):
        register_agent(
            f.store,
            _request(f, **changes),
            f.registry_path,
            f.runtime,
            public_dev_agent_study=_policy(f),
        )
    assert f.registry_path.read_bytes() == before


@pytest.mark.parametrize(
    "field",
    [
        "snapshot_sha256",
        "binding_sha256",
        "source_suite_manifest_sha256",
        "original_task_sha256",
        "profile_sha256",
        "provider_config_sha256",
    ],
)
def test_registration_rejects_changed_source_or_provider_pins(public_installation, field):
    f = public_installation
    with pytest.raises(ValueError):
        _register(f, _policy(f, **{field: "f" * 64}))


def test_grant_cannot_become_cpu_or_aos_authority(public_installation):
    f = public_installation
    _, entry = _register(f)
    for changes in (
        {"provider": "mode-grid"},
        {"public_dev_study": f.entry.public_dev_study.model_dump(mode="json")},
        {"proposal_contract": "candidate-python.v1"},
        {"allowed_purposes": ["research", "baseline"]},
    ):
        with pytest.raises(ValidationError):
            SuiteEntry.model_validate(entry.model_dump(mode="json") | changes)
    with pytest.raises(ValueError, match="provider differs"):
        _register(
            f,
            _policy(
                f,
                provider_config_sha256=provider_config_sha256(
                    "research", "operating-mode-config.v1"
                ),
            ),
        )


def test_api_metadata_admission_owner_cpu_fence_and_retries(public_installation, monkeypatch):
    f = public_installation
    monkeypatch.setattr(api_module, "PROJECT_ROOT", f.runtime.parent.parent)
    monkeypatch.setattr(
        api_module, "compute_harness_hash", lambda *_: SimpleNamespace(sha256="a" * 64)
    )
    monkeypatch.setenv("LAB_SUITE_REGISTRY_FILE", str(f.registry_path))
    monkeypatch.delenv("LAB_CPU_ONLY", raising=False)
    app = api_module.create_app(
        director_engine=f.engine,
        suite_registry=f.registry,
        principals=(
            ApiPrincipal(token="a" * 32, origin="local", owner_id="field-lab"),
            ApiPrincipal(token="b" * 32, origin="local", owner_id="other"),
        ),
    )
    headers = {"Authorization": "Bearer " + "a" * 32}
    other = {"Authorization": "Bearer " + "b" * 32}
    payload = _request(f).model_dump(mode="json")
    url = f"/v1/mode-snapshots/{f.snapshot.sha256}"
    with TestClient(app) as client:
        assert client.get(url, headers=headers).json()["local_agent_study"] is None
        assert (
            client.post("/v1/mode-agent-experiments", headers=headers, json=payload).status_code
            == 422
        )
        registry, entry = _register(f)
        app.state.suite_registry = registry
        assert client.get(url, headers=headers).json()["local_agent_study"] == dict(
            schema="public-dev-local-agent-study.v1",
            profile_set="research",
            max_experiments=3,
            max_wall_seconds=1200,
            max_model_tokens=180000,
        )
        assert client.get(url, headers=other).status_code == 404
        generic = dict(
            idempotency_key="public-agent-other",
            track="mode",
            suite=entry.suite_id,
            program_version=entry.program_version,
            budget=dict(experiments=2, wall_seconds=900, model_tokens=120000),
        )
        assert client.post("/v1/runs", headers=other, json=generic).status_code == 403
        monkeypatch.setenv("LAB_CPU_ONLY", "true")
        assert (
            client.post("/v1/mode-agent-experiments", headers=headers, json=payload).status_code
            == 403
        )
        monkeypatch.setenv("LAB_CPU_ONLY", "false")
        for change in ({"experiments": 4}, {"wall_seconds": 1201}, {"model_tokens": 180001}):
            assert (
                client.post(
                    "/v1/mode-agent-experiments", headers=headers, json=payload | change
                ).status_code
                == 422
            )
        first = client.post("/v1/mode-agent-experiments", headers=headers, json=payload)
        assert first.status_code == 202, first.text
        repeated = client.post("/v1/mode-agent-experiments", headers=headers, json=payload)
        assert repeated.status_code == 200 and repeated.json()["run_id"] == first.json()["run_id"]
        assert (
            client.post(
                "/v1/mode-agent-experiments",
                headers=headers,
                json=payload | {"model_tokens": 119000},
            ).status_code
            == 409
        )
    with f.engine.connect() as connection:
        frozen = connection.execute(select(runs.c.request_json)).scalar_one()
    assert frozen["public_dev_agent_study"] == _policy(f).model_dump(mode="json")
    assert "public_dev_study" not in frozen


def test_cli_binding_and_planner_reject_ungranted_changed_or_holdout(
    public_installation, monkeypatch
):
    f = public_installation
    registry, entry = _register(f)
    monkeypatch.setattr(cli, "PROJECT_ROOT", f.runtime.parent.parent)
    monkeypatch.setenv("LAB_SUITE_REGISTRY_FILE", str(f.registry_path))
    request = dict(
        suite=entry.suite_id,
        provider=entry.provider,
        track=entry.track,
        proposal_contract=entry.proposal_contract,
        snapshot_sha256=f.snapshot.sha256,
        study_kind="single_snapshot_study",
        provider_config_sha256=entry.provider_config_sha256,
        provider_registry_entry_sha256=registry.entry_sha256(entry),
        suite_manifest_sha256=entry.suite_manifest_sha256,
        public_dev_agent_study=_policy(f).model_dump(mode="json"),
        budget=dict(experiments=2, wall_seconds=900, model_tokens=120000),
    )
    path = f.runtime / entry.suite_manifest_path

    def binding(value=request, owner="field-lab"):
        cli._registered_mode_agent_snapshot(value, path)
        return cli._registered_public_grid_binding(
            value,
            path,
            owner_id=owner,
            origin="local",
            snapshot_sha256=f.snapshot.sha256,
        )

    assert binding() == f.binding
    with pytest.raises(ValueError):
        binding(owner="other")
    for field in (
        "public_dev_agent_study",
        "proposal_contract",
        "provider_registry_entry_sha256",
        "snapshot_sha256",
    ):
        with pytest.raises(ValueError):
            binding(request | {field: "wrong"})
    with pytest.raises(ValueError):
        binding(request | {"public_dev_study": f.entry.public_dev_study.model_dump(mode="json")})
    with pytest.raises(ValueError):
        binding(request | {"budget": request["budget"] | {"model_tokens": 0}})

    @event.listens_for(f.engine, "before_cursor_execute", retval=True)
    def sqlite_profile(connection, cursor, statement, parameters, context, many):
        return statement.replace("scorer.dataset_profiles", "dataset_profiles"), parameters

    _, tasks, _ = load_suite_manifest(
        path,
        f.engine,
        study_snapshot_sha256=f.snapshot.sha256,
        public_task_binding=binding(),
    )
    assert tasks[0]._train.shape == (26, 2) and tasks[0]._evaluation.shape == (470, 2)
    raw = json.loads(path.read_bytes())
    assert hashlib.sha256(path.read_bytes()).hexdigest() == entry.suite_manifest_sha256
    assert "labels" not in raw and "evaluation_times" not in raw
    with f.engine.begin() as connection:
        connection.execute(update(dataset_profiles).values(visibility="holdout"))
    with pytest.raises(ValueError, match="trusted dev profile"):
        load_suite_manifest(
            path, f.engine, study_snapshot_sha256=f.snapshot.sha256, public_task_binding=binding()
        )


def test_dispatcher_carries_verified_public_prompt_and_source_without_model(
    public_installation, monkeypatch
):
    f = public_installation
    registry, entry = _register(f)
    monkeypatch.setattr(cli, "PROJECT_ROOT", f.runtime.parent.parent)
    monkeypatch.setenv("LAB_SUITE_REGISTRY_FILE", str(f.registry_path))
    monkeypatch.setattr(cli, "compute_harness_hash", lambda *_: SimpleNamespace(sha256="a" * 64))
    run_id = uuid4()
    request = dict(
        suite=entry.suite_id,
        provider=entry.provider,
        track=entry.track,
        program_version=entry.program_version,
        proposal_limit=2,
        proposal_contract=entry.proposal_contract,
        snapshot_sha256=f.snapshot.sha256,
        study_kind="single_snapshot_study",
        scenario_sha256=None,
        provider_config_sha256=entry.provider_config_sha256,
        provider_registry_entry_sha256=registry.entry_sha256(entry),
        suite_manifest_sha256=entry.suite_manifest_sha256,
        public_dev_agent_study=_policy(f).model_dump(mode="json"),
        budget=dict(experiments=2, wall_seconds=900, model_tokens=120000),
    )
    row = dict(
        state="queued",
        request_json=request,
        origin="local",
        owner_id="field-lab",
        payload_sha256=hashlib.sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest(),
    )
    target = cli._validate_dispatch_target(run_id, row, registry)
    assert target.public_fit and target.profile_set == "research"
    for field, value in (("owner_id", "other"), ("origin", "aos")):
        with pytest.raises(ValueError):
            cli._validate_dispatch_target(run_id, row | {field: value}, registry)
    with pytest.raises(ValueError):
        cli._validate_dispatch_target(
            run_id,
            row
            | {
                "request_json": request
                | {
                    "public_dev_agent_study": None,
                }
            },
            registry,
        )
    director = MagicMock()
    connection = director.connect.return_value.__enter__.return_value
    connection.execute.return_value.mappings.return_value.one_or_none.return_value = row
    monkeypatch.setattr(cli, "_director_engine", lambda: director)
    observation = SimpleNamespace(
        payload_sha256=row["payload_sha256"],
        worker_pid=100,
        worker_start_ticks=200,
        worker_boot_id="00000000-0000-0000-0000-000000000001",
        worker_unit="fixture.service",
        worker_invocation_id="b" * 32,
        worker_cgroup="/fixture/fixture.service",
    )
    monkeypatch.setattr(cli, "capture_current_owner", lambda *_: observation)
    owner = SimpleNamespace(
        run_id=run_id,
        generation=1,
        invocation_id="b" * 32,
        execution_sha256=target.contract.execution_sha256,
    )
    monkeypatch.setattr(cli, "claim_initial_execution", lambda *_, **kw: owner)
    provider_calls = []

    def provider(*args, **kwargs):
        provider_calls.append(kwargs)
        assert kwargs["public_fit"] is True and kwargs["profile_set"] == "research"
        return SimpleNamespace(configuration_sha256=entry.provider_config_sha256)

    monkeypatch.setattr(cli, "_local_qwen_provider", provider)

    def execute(args):
        assert args.provider_instance.configuration_sha256 == entry.provider_config_sha256
        assert cli._registered_mode_agent_snapshot(request, args.suite_file) == f.snapshot.sha256
        bound = cli._registered_public_grid_binding(
            request,
            args.suite_file,
            owner_id="field-lab",
            origin="local",
            snapshot_sha256=f.snapshot.sha256,
        )
        assert bound == f.binding
        return {"state": "fixture-completed"}

    monkeypatch.setattr(cli, "_run_director", execute)
    monkeypatch.setattr(cli, "_completed_dispatch_result", lambda *args: args[-1])
    result = cli._dispatch_director_run(run_id)
    assert result["state"] == "fixture-completed" and len(provider_calls) == 1
    assert cli.active_execution_owner() is None


def test_cached_api_cannot_restore_revoked_agent_or_cpu_grants(public_installation, monkeypatch):
    f = public_installation
    registry, _ = _register(f)
    monkeypatch.setattr(api_module, "PROJECT_ROOT", f.runtime.parent.parent)
    monkeypatch.setenv("LAB_SUITE_REGISTRY_FILE", str(f.registry_path))
    monkeypatch.delenv("LAB_CPU_ONLY", raising=False)
    app = api_module.create_app(
        director_engine=f.engine,
        suite_registry=registry,
        principals=(ApiPrincipal(token="a" * 32, origin="local", owner_id="field-lab"),),
    )
    headers = {"Authorization": "Bearer " + "a" * 32}
    stale_policy = _policy(f)
    with TestClient(app) as client:
        # Revoke every policy-bearing entry while the API retains its old registry object.
        raw = b'{"schema":"lab-suite-registry.v1","suites":[]}'
        f.registry_path.write_bytes(raw)
        assert (
            client.get(f"/v1/mode-snapshots/{f.snapshot.sha256}", headers=headers).json()[
                "local_agent_study"
            ]
            is None
        )
        assert (
            client.post(
                "/v1/mode-agent-experiments",
                headers=headers,
                json=_request(f).model_dump(mode="json"),
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/v1/mode-experiments",
                headers=headers,
                json=dict(
                    idempotency_key="revoked-public-cpu-fixture",
                    snapshot_sha256=f.snapshot.sha256,
                    configurations=[{"method": "lsh"}],
                    wall_seconds=600,
                ),
            ).status_code
            == 422
        )
        # Exercise publication directly to model revocation between lookup and the lock.
        with pytest.raises(ValueError, match="revoked or changed"):
            register_agent(
                f.store,
                _request(f),
                f.registry_path,
                f.runtime,
                public_dev_agent_study=stale_policy,
            )
        assert f.registry_path.read_bytes() == raw
    with f.engine.connect() as connection:
        assert not connection.execute(select(runs.c.run_id)).all()
