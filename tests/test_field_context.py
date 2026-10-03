"""Durable field intent admission and context readback, using inert CPU fixtures."""

import hashlib

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from lab.api import app as api_module
from lab.api import experience
from lab.api.contracts import StartRunRequest
from lab.api.mode_experiments import ModeAgentRequest, ModeGridRequest
from lab.api.registry import ApiPrincipal
from lab.db.schema import runs
from lab.director.fake_llm import AgentContext, prompt_context_sha256
from lab.director.field_context import FieldContext, FieldIntent, load_frozen_field_context
from lab.director.journal import canonical_bytes
from lab.reporting import _LedgerExperiment
from tests import test_mode_agent_admission as agent_fixtures
from tests import test_prior_findings as history_fixtures
from tests import test_public_mode_snapshot as public_fixtures
from tests.test_api_and_scorer import _engine, _request
from tests.test_experience_api import write_blob

installation = agent_fixtures.installation
source = history_fixtures.source
context = history_fixtures.context
public_installation = public_fixtures.public_installation

INTENT = {
    "asset_id": "pump-17",
    "goal_kind": "predictive_maintenance",
    "objective": "Study operating modes before a maintenance review",
}


def frozen(snapshot_sha="a" * 64):
    field = FieldContext(
        schema="field-study-context.v1", intent=FieldIntent(**INTENT), snapshot_sha256=snapshot_sha
    )
    request = dict(
        track="mode",
        provider="mode-grid",
        field_intent=field.intent.model_dump(mode="json"),
        field_context=field.model_dump(mode="json", by_alias=True),
        field_context_sha256=field.sha256,
    )
    return field, request


@pytest.mark.parametrize(
    "bad",
    [
        {"asset_id": " "},
        {"objective": "\ntext"},
        {"objective": "x\x00y"},
        {"asset_id": 17},
        {"objective": "x" * 601},
        {"goal_kind": "stop-pump"},
        {"authority": True},
    ],
)
def test_intent_is_plain_bounded_metadata_not_authority(bad):
    with pytest.raises(ValidationError):
        FieldIntent.model_validate(INTENT | bad)
    assert FieldIntent(**(INTENT | {"asset_id": " pump-17 "})).asset_id == "pump-17"


def test_absent_intent_preserves_request_and_context_bytes():
    for model in (
        StartRunRequest(**_request()),
        ModeGridRequest(
            idempotency_key="field-empty-fixture",
            snapshot_sha256="a" * 64,
            configurations=[{"method": "lsh"}],
        ),
        ModeAgentRequest(idempotency_key="field-empty-fixture", snapshot_sha256="a" * 64),
    ):
        assert "field_intent" not in model.model_dump(mode="json")
    legacy = context()
    assert "field_context" not in legacy.model_dump(mode="json")
    field, request = frozen()
    current = legacy.model_copy(
        update={"field_context": field, "field_context_sha256": field.sha256}
    )
    raw = canonical_bytes(current.model_dump(mode="json"))
    assert AgentContext.model_validate_json(raw, strict=True) == current
    assert prompt_context_sha256(current) != prompt_context_sha256(legacy)
    assert (
        load_frozen_field_context(request, expected_snapshot_sha256=field.snapshot_sha256) == field
    )
    for changes in (
        {"field_context_sha256": "0" * 64},
        {"field_intent": INTENT | {"asset_id": "other"}},
        {"field_intent": None},
        {"track": "anomaly"},
    ):
        with pytest.raises(ValueError):
            load_frozen_field_context(request | changes)
    with pytest.raises(ValueError):
        load_frozen_field_context(request, expected_snapshot_sha256="b" * 64)


def test_public_grid_admits_exact_context_and_rejects_changed_idempotent_intent(
    public_installation, monkeypatch
):
    f = public_installation
    monkeypatch.setattr(api_module, "PROJECT_ROOT", f.runtime.parent.parent)
    monkeypatch.setenv("LAB_SUITE_REGISTRY_FILE", str(f.registry_path))
    app = api_module.create_app(
        director_engine=f.engine,
        suite_registry=f.registry,
        principals=(ApiPrincipal(token="a" * 32, origin="local", owner_id="field-lab"),),
    )
    payload = dict(
        idempotency_key="field-public-grid-01",
        snapshot_sha256=f.snapshot.sha256,
        configurations=[{"method": "lsh"}],
        field_intent=INTENT,
    )
    headers = {"Authorization": "Bearer " + "a" * 32}
    with TestClient(app) as client:
        first = client.post("/v1/mode-experiments", headers=headers, json=payload)
        assert first.status_code == 202, first.text
        assert client.post("/v1/mode-experiments", headers=headers, json=payload).status_code == 200
        assert (
            client.post(
                "/v1/mode-experiments",
                headers=headers,
                json=payload | {"field_intent": INTENT | {"objective": "Different objective"}},
            ).status_code
            == 409
        )
    with f.engine.connect() as connection:
        row = connection.execute(select(runs.c.request_json, runs.c.payload_sha256)).one()
    bound = load_frozen_field_context(row.request_json, expected_snapshot_sha256=f.snapshot.sha256)
    assert bound.intent.objective == INTENT["objective"] and bound.asset_identity == "user_supplied"
    assert (
        hashlib.sha256(
            canonical_bytes({k: v for k, v in row.request_json.items() if k != "idempotency_key"})
        ).hexdigest()
        == row.payload_sha256
    )
    assert row.request_json["budget"] == dict(experiments=1, wall_seconds=600, model_tokens=0)


def test_synthetic_agent_forwards_intent_without_changing_source_or_budget(
    installation, monkeypatch
):
    store, snapshot_sha, registry_path, runtime, _, _ = installation
    monkeypatch.setattr(api_module, "PROJECT_ROOT", runtime.parent.parent)
    monkeypatch.setenv("LAB_SUITE_REGISTRY_FILE", str(registry_path))
    monkeypatch.delenv("LAB_CPU_ONLY", raising=False)
    engine = _engine()
    app = api_module.create_app(
        director_engine=engine,
        principals=(ApiPrincipal(token="a" * 32, origin="local", owner_id="fixture"),),
    )
    with TestClient(app) as client:
        result = client.post(
            "/v1/mode-agent-experiments",
            headers={"Authorization": "Bearer " + "a" * 32},
            json=dict(
                idempotency_key="field-synthetic-agent",
                snapshot_sha256=snapshot_sha,
                experiments=2,
                wall_seconds=120,
                model_tokens=1000,
                field_intent=INTENT,
            ),
        )
        assert result.status_code == 202, result.text
    with engine.connect() as connection:
        request = connection.execute(select(runs.c.request_json)).scalar_one()
    assert load_frozen_field_context(request).snapshot_sha256 == snapshot_sha
    assert store.load(snapshot_sha).sha256 == snapshot_sha
    assert request["budget"] == dict(experiments=2, wall_seconds=120, model_tokens=1000)


def test_unsupported_intent_and_client_internal_context_are_rejected():
    app = api_module.create_app(
        director_engine=_engine(), director_token="a" * 32, allow_unregistered_suites=True
    )
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer " + "a" * 32}
        assert (
            client.post(
                "/v1/runs", headers=headers, json=_request() | {"field_intent": INTENT}
            ).status_code
            == 422
        )
        for field in ("field_context", "field_context_sha256"):
            assert (
                client.post("/v1/runs", headers=headers, json=_request() | {field: {}}).status_code
                == 422
            )


def test_verified_terminal_readback_distinguishes_admitted_and_context_bound(source, monkeypatch):
    field, request = frozen()
    raw = canonical_bytes(
        context()
        .model_copy(update={"field_context": field, "field_context_sha256": field.sha256})
        .model_dump(mode="json")
    )
    sha = hashlib.sha256(raw).hexdigest()
    write_blob(source.root, sha, raw)
    exp = source.pair.document.model_copy(update={"inputs_sha256": sha})
    trajectory = source.pair.trajectory.model_copy(update={"inputs_sha256": sha})
    pair = _LedgerExperiment(1, exp, trajectory)
    monkeypatch.setattr(experience, "read_run_pairs", lambda *a, **kw: (pair,))
    result = experience.build_experience(
        source.reader, exp.run_id, source.verified, {"request": request}
    )
    usage = result["field_context_usage"]
    assert usage["status"] == "context-bound" and usage["context_bound_proposal_count"] == 1
    assert usage["intent"] == INTENT and usage["field_context_sha256"] == field.sha256
    assert result["report_sha256"] == source.verified["report_sha256"]
    assert (
        "field_intent_permission_not_reviewed"
        in result["records"][0]["training_eligibility"]["reasons"]
    )
    monkeypatch.setattr(
        experience,
        "read_run_pairs",
        lambda *a, **kw: (
            _LedgerExperiment(1, exp, trajectory.model_copy(update={"inputs_sha256": "0" * 64})),
        ),
    )
    with pytest.raises(ValueError):
        experience.build_experience(
            source.reader, exp.run_id, source.verified, {"request": request}
        )
    write_blob(source.root, sha, b"{}")
    # Each HTTP request gets a new reader; an already verified per-request cache is immutable.
    fresh_reader = experience.ExperienceReader(source.engine, source.root)
    fresh_reader.receipts = dict(source.reader.receipts)
    monkeypatch.setattr(experience, "read_run_pairs", lambda *a, **kw: (pair,))
    with pytest.raises(ValueError):
        experience.build_experience(fresh_reader, exp.run_id, source.verified, {"request": request})
    monkeypatch.setattr(experience, "read_run_pairs", lambda *a, **kw: ())
    result = experience.build_experience(
        source.reader, exp.run_id, source.verified, {"request": request}
    )
    assert result["field_context_usage"]["status"] == "admitted-only"
