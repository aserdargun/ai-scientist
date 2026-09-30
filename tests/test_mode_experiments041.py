"""Immutable source/grid integration without a service or a model process."""

import hashlib
import json

import pytest

from lab.api.mode_experiments import ModeSnapshotStore, SyntheticSnapshotRequest
from lab.director.fake_llm import AgentContext
from lab.director.journal import canonical_bytes
from lab.director.parameter_grid import ParameterGridProvider, grid_document


def test_snapshot_is_reproducible_and_evaluator_labels_stay_private(tmp_path):
    store = ModeSnapshotStore(tmp_path)
    request = SyntheticSnapshotRequest(scenario="step", seed=8)
    first = store.create_synthetic(request)
    assert store.create_synthetic(request) == first
    digest = first["snapshot_sha256"]
    assert "labels" not in json.dumps(first)
    assert "event_labels" not in (tmp_path / digest / "snapshot.json").read_text()
    assert "event_labels" in (tmp_path / digest / "evaluator.json").read_text()
    assert store.statistics(digest, "train")["rows"] == request.train_rows
    path = tmp_path / digest / "snapshot.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="hash"):
        store.load(digest)


def test_parameter_grid_is_zero_token_source_only_and_hash_bound():
    document = grid_document("a" * 64, [{"method": "lsh"}, {"method": "som"}])
    digest = hashlib.sha256(canonical_bytes(document)).hexdigest()
    provider = ParameterGridProvider(
        document, configuration_sha256=digest, registry_entry_sha256="b" * 64
    )
    context = AgentContext(
        phase="search",
        experiment_number=1,
        task_cards=("synthetic",),
        champion_source="source",
        recent_feedback=(),
    )
    turn = provider.propose(context)
    assert turn.input_tokens == turn.output_tokens == 0
    assert turn.proposal.predicted_delta == 0.0
    assert (
        hashlib.sha256(turn.proposal.candidate_source.encode()).hexdigest()
        == document["entries"][0]["candidate_sha256"]
    )
    assert provider.propose(context) == turn
    document["entries"][0]["configuration"]["k"] = 9
    with pytest.raises(ValueError, match="immutable configuration hash"):
        ParameterGridProvider(document, configuration_sha256=digest, registry_entry_sha256="b" * 64)


def test_scorer_adapter_recomputes_labels_and_applies_train_only_embargo(tmp_path):
    from lab.scorer.mode_snapshot import materialize_synthetic

    store = ModeSnapshotStore(tmp_path)
    digest = store.create_synthetic(SyntheticSnapshotRequest(scenario="step"))["snapshot_sha256"]
    task, registration = materialize_synthetic(store, digest)
    train, evaluation = task.frames()
    assert len(train) == 192
    assert len(evaluation) == 96 - registration.sliding_window
    assert registration.embargo_seconds == registration.sliding_window
    assert len(registration.labels) == len(evaluation)
    assert set(train.columns).isdisjoint({"label", "event_labels", "quality_labels"})


def test_diagnostics_are_bound_to_source_seed_model_and_scalar_values():
    from harness.contracts import FitContext
    from lab.operating_modes import (
        ModeConfig,
        OperatingModeCandidate,
        candidate_source,
        synthetic_snapshot,
    )
    from lab.scorer.mode_diagnostics import validate_mode_diagnostics

    case = synthetic_snapshot("healthy_single")
    config = ModeConfig(method="lsh", lsh_width=1.0)
    candidate = OperatingModeCandidate(config)
    candidate.fit(
        case.snapshot.frame("train"),
        FitContext(
            seed=2, signals=case.snapshot.sensors, regime_signals=(), sampling_s=1, time_budget_s=10
        ),
    )
    prediction = candidate.model.predict(case.snapshot.frame("evaluation"))
    scores = [row.omr_percent for row in prediction.rows]
    assert all(score is not None for score in scores)
    source_sha = hashlib.sha256(candidate_source(config)).hexdigest()
    document = dict(
        schema="candidate-mode-diagnostics.v1",
        candidate_sha256=source_sha,
        configuration=config.model_dump(mode="json"),
        configuration_sha256=hashlib.sha256(
            canonical_bytes(config.model_dump(mode="json"))
        ).hexdigest(),
        repetition_seed=2,
        fit_artifact_sha256="a" * 64,
        model=candidate.model.summary(),
        prediction=prediction.to_dict(),
    )
    assert (
        validate_mode_diagnostics(document, scores, candidate_sha256=source_sha, seed=2) == document
    )
    with pytest.raises(ValueError, match="identity mismatch"):
        validate_mode_diagnostics(document, scores, candidate_sha256="f" * 64, seed=2)
    changed = list(scores)
    changed[0] += 1
    with pytest.raises(ValueError, match="scalar score"):
        validate_mode_diagnostics(document, changed)


def test_private_snapshot_api_denies_other_local_owner(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from lab.api import app as api_module
    from lab.api.mode_experiments import canonical_document
    from lab.api.registry import ApiPrincipal
    from tests.test_api_and_scorer import _engine

    store = ModeSnapshotStore(tmp_path / "data/runtime/mode-snapshots")
    digest = store.create_synthetic(SyntheticSnapshotRequest())["snapshot_sha256"]
    directory = store.directory(digest)
    manifest = json.loads((directory / "manifest.json").read_bytes())
    manifest["source_kind"] = "private_database"
    (directory / "manifest.json").write_bytes(canonical_document(manifest))
    owner_hash = hashlib.sha256(b"owner-one").hexdigest()
    (directory / f"access-{owner_hash}.json").write_bytes(
        canonical_document({"owner_id": "owner-one", "snapshot_sha256": digest})
    )
    monkeypatch.setattr(api_module, "PROJECT_ROOT", tmp_path)
    app = api_module.create_app(
        director_engine=_engine(),
        principals=(
            ApiPrincipal(token="a" * 32, origin="local", owner_id="owner-one"),
            ApiPrincipal(token="b" * 32, origin="local", owner_id="owner-two"),
        ),
    )
    with TestClient(app) as client:
        assert (
            client.get(
                f"/v1/mode-snapshots/{digest}", headers={"Authorization": "Bearer " + "a" * 32}
            ).status_code
            == 200
        )
        assert (
            client.get(
                f"/v1/mode-snapshots/{digest}", headers={"Authorization": "Bearer " + "b" * 32}
            ).status_code
            == 404
        )
        assert (
            client.get(
                f"/v1/mode-snapshots/{digest}/statistics",
                headers={"Authorization": "Bearer " + "b" * 32},
            ).status_code
            == 422
        )


def _mock_snapshot_rpc_transport(monkeypatch, engine, store, digest):
    """SQLite fixture only: materialize real data and replace just the SQL RPC transport."""
    from dataclasses import asdict

    from lab.api.mode_experiments import canonical_document
    from lab.director.public_suite import install_scorer_task
    from lab.scorer import mode_snapshot

    _, registration = mode_snapshot.materialize_synthetic(store, digest)

    def transport(connection, *, payload, digest):
        assert payload == canonical_document(
            {"schema": "mode-snapshot-registration.v1", **asdict(registration)}
        )
        assert hashlib.sha256(payload).hexdigest() == digest
        install_scorer_task(engine, registration)
        return dict(
            schema="mode-snapshot-registration-receipt.v1",
            registration_sha256=digest,
            snapshot_sha256=registration.session_id,
            profile_sha256=registration.profile_sha256,
            semantics_sha256=registration.semantics_sha256,
            sample_count=registration.sample_count,
            sliding_window=registration.sliding_window,
        )

    monkeypatch.setattr(engine.dialect, "name", "postgresql")
    monkeypatch.setattr(mode_snapshot, "_execute_registration_rpc", transport)


def test_install_and_grid_registration_use_existing_scorer_and_registry(tmp_path, monkeypatch):
    from lab.api.mode_experiments import ModeGridRequest, canonical_document, register_grid
    from lab.api.registry import SuiteEntry
    from lab.scorer.mode_snapshot import install_snapshot
    from tests.test_api_and_scorer import _engine

    store = ModeSnapshotStore(tmp_path / "mode-snapshots")
    digest = store.create_synthetic(SyntheticSnapshotRequest(scenario="step"))["snapshot_sha256"]
    engine = _engine()
    _mock_snapshot_rpc_transport(monkeypatch, engine, store, digest)
    receipt = install_snapshot(engine, store, digest)
    assert store.describe(digest)["readiness"] == "ready"
    directory = store.directory(digest)
    scenario = tmp_path / "dummy.json"
    scenario.write_bytes(b"{}")
    scenario.chmod(0o600)
    initial = SuiteEntry(
        suite_id=f"mode-snapshot-{digest[:32]}",
        track="mode",
        program_version="fixture",
        suite_manifest_path=str((directory / "suite.json").relative_to(tmp_path)),
        suite_manifest_sha256=receipt["suite_manifest_sha256"],
        provider="fake-json",
        scenario_path="dummy.json",
        scenario_sha256=hashlib.sha256(b"{}").hexdigest(),
        proposal_limit=1,
    )
    registry_path = tmp_path / "registry.json"
    registry_path.write_bytes(
        canonical_document(
            {"schema": "lab-suite-registry.v1", "suites": [initial.model_dump(mode="json")]}
        )
    )
    registry_path.chmod(0o600)
    request = ModeGridRequest(
        idempotency_key="mode-grid-test-001",
        snapshot_sha256=digest,
        configurations=[{"method": "lsh"}, {"method": "som"}],
    )
    registry, entry = register_grid(store, request, registry_path, tmp_path)
    suite_path, grid_path = registry.verify_entry(entry)
    assert json.loads(suite_path.read_bytes())["suite_id"] == entry.suite_id
    provider = ParameterGridProvider.load(
        grid_path,
        configuration_sha256=entry.provider_config_sha256,
        registry_entry_sha256=registry.entry_sha256(entry),
    )
    assert len(provider) == 2
    assert register_grid(store, request, registry_path, tmp_path)[1] == entry


def test_single_snapshot_policy_needs_explicit_verified_authority(tmp_path, monkeypatch):
    from lab.director.suite_manifest import load_suite_manifest, write_suite_manifest
    from lab.scorer.mode_snapshot import install_snapshot, materialize_synthetic
    from tests.test_api_and_scorer import _engine

    store = ModeSnapshotStore(tmp_path)
    digest = store.create_synthetic(SyntheticSnapshotRequest(scenario="step"))["snapshot_sha256"]
    task, _ = materialize_synthetic(store, digest)
    with pytest.raises(ValueError, match="independent families"):
        write_suite_manifest((task,), tmp_path / "ordinary.json")
    engine = _engine()
    _mock_snapshot_rpc_transport(monkeypatch, engine, store, digest)
    install_snapshot(engine, store, digest)
    path = store.directory(digest) / "suite.json"
    with pytest.raises(ValueError, match="verified mode-grid"):
        load_suite_manifest(path, engine)
    with pytest.raises(ValueError, match="verified mode-grid"):
        load_suite_manifest(path, engine, study_snapshot_sha256="f" * 64)
    # The production loader uses a schema-qualified textual Planner read.
    # SQLite's schema_translate_map applies only to expression-built SQL.
    from sqlalchemy import event

    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def sqlite_profile_schema(connection, cursor, statement, parameters, context, many):
        return statement.replace("scorer.dataset_profiles", "dataset_profiles"), parameters

    _, tasks, _ = load_suite_manifest(path, engine, study_snapshot_sha256=digest)
    assert len(tasks) == 1


def test_configured_database_source_enforces_owner_columns_and_typed_recipe(tmp_path, monkeypatch):
    import lab.api.mode_sources as sources
    from lab.api.mode_experiments import canonical_document
    from lab.api.mode_sources import DatabaseSnapshotRequest, SourceCatalog, authorize_snapshot
    from lab.operating_modes import synthetic_snapshot

    source = synthetic_snapshot("healthy_single").snapshot
    secret = tmp_path / "source.dsn"
    secret.write_text("postgresql://configured-read-only")
    secret.chmod(0o600)
    path = tmp_path / "catalog.json"
    selection = source.selection.model_dump(mode="json")
    selection.update(source_id="allowed-view", table="readonly_sensors", row_limit=512)
    path.write_bytes(
        canonical_document(
            {
                "schema": "mode-source-catalog.v1",
                "sources": [
                    {
                        "selection": selection,
                        "sensors": list(source.sensors),
                        "owners": ["one"],
                        "dsn_file": str(secret),
                    }
                ],
            }
        )
    )
    path.chmod(0o600)
    catalog = SourceCatalog(path)
    assert catalog.available("two") == []
    assert "dsn" not in json.dumps(catalog.available("one"))
    request = DatabaseSnapshotRequest(
        source_id="allowed-view",
        sensors=list(source.sensors),
        start_utc="2026-01-01T00:00:00Z",
        end_utc="2026-01-02T00:00:00Z",
    )
    store = ModeSnapshotStore(tmp_path / "snapshots")
    with pytest.raises(ValueError, match="unavailable"):
        catalog.capture(request, owner="two", store=store)
    with pytest.raises(ValueError, match="configured limits"):
        catalog.capture(
            request.model_copy(update={"sensors": ["forbidden"]}), owner="one", store=store
        )

    class FakeEngine:
        def dispose(self):
            pass

    import sqlalchemy

    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *args, **kwargs: FakeEngine())
    seen = []

    def read(engine, recipe, sensors, *, train_rows):
        seen.append((recipe, sensors, train_rows))
        return source

    monkeypatch.setattr(sources, "read_postgres_snapshot", read)
    result = catalog.capture(request, owner="one", store=store)
    assert result["scoring_available"] is False
    assert seen[0][0].table == "readonly_sensors"
    assert seen[0][0].start_utc == request.start_utc
    authorize_snapshot(store, result["snapshot_sha256"], "one")
    with pytest.raises(ValueError, match="unavailable"):
        authorize_snapshot(store, result["snapshot_sha256"], "two")


@pytest.mark.parametrize("indices", [(0, 2), (3, 1), (3, 2, 1, 0)])
def test_database_sensor_subset_and_reorder_project_units(tmp_path, monkeypatch, indices):
    from types import SimpleNamespace

    import sqlalchemy

    import lab.api.mode_sources as sources
    from lab.api.mode_experiments import canonical_document
    from lab.operating_modes import snapshot_from_frame, synthetic_snapshot

    source = synthetic_snapshot("healthy_single").snapshot
    sensors = list(source.sensors)
    units = ["degC", "bar", "rpm", "unitless"]
    assert len(sensors) == len(units)
    secret = tmp_path / "source.dsn"
    secret.write_text("postgresql://fixture-never-connected")
    secret.chmod(0o600)
    selection = source.selection.model_dump(mode="json")
    selection.update(source_id="unit-view", units=units, row_limit=512)
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_bytes(
        canonical_document(
            {
                "schema": "mode-source-catalog.v1",
                "sources": [
                    {
                        "selection": selection,
                        "sensors": sensors,
                        "owners": ["one"],
                        "dsn_file": str(secret),
                    }
                ],
            }
        )
    )
    catalog_path.chmod(0o600)
    monkeypatch.setattr(
        sqlalchemy, "create_engine", lambda *args, **kwargs: SimpleNamespace(dispose=lambda: None)
    )

    def read(_engine, recipe, requested_sensors, *, train_rows):
        assert requested_sensors == tuple(sensors[index] for index in indices)
        assert recipe.units == tuple(units[index] for index in indices)
        return snapshot_from_frame(
            source.frame()[list(requested_sensors)],
            source.timestamps_utc,
            recipe,
            train_rows=train_rows,
        )

    monkeypatch.setattr(sources, "read_postgres_snapshot", read)
    store = ModeSnapshotStore(tmp_path / "snapshots")
    result = sources.SourceCatalog(catalog_path).capture(
        sources.DatabaseSnapshotRequest(
            source_id="unit-view",
            sensors=[sensors[index] for index in indices],
            start_utc=source.timestamps_utc[0],
            end_utc=source.timestamps_utc[-1],
        ),
        owner="one",
        store=store,
    )
    saved = store.load(result["snapshot_sha256"])
    assert saved.sensors == tuple(sensors[index] for index in indices)
    assert saved.selection.units == tuple(units[index] for index in indices)
    assert saved.values[0] == tuple(source.values[0][index] for index in indices)


def test_only_registered_grid_class_receives_zero_token_proposal_reservation(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from uuid import uuid4

    from lab.director import runner as module
    from lab.director.budget import RunBudget

    document = grid_document("a" * 64, [{"method": "lsh"}])
    provider = ParameterGridProvider(
        document,
        configuration_sha256=hashlib.sha256(canonical_bytes(document)).hexdigest(),
        registry_entry_sha256="b" * 64,
    )
    context = AgentContext(
        phase="proposal",
        experiment_number=1,
        task_cards=("task",),
        champion_source="source",
        recent_feedback=(),
    )
    lease = SimpleNamespace(require_run_active=lambda: None, read_checkpoint=lambda **kwargs: None)

    class Reserved(Exception):
        pass

    receipts = []

    def checkpoint(*args, **kwargs):
        receipts.append(kwargs["payload"])
        raise Reserved()

    monkeypatch.setattr(module, "_append_next_checkpoint", checkpoint)
    budget = RunBudget(proposal_limit=1, wall_limit=1000, token_limit=0)
    kwargs = dict(
        lease=lease,
        runner=None,
        run_id=uuid4(),
        ordinal=1,
        parent_experiment_id="parent",
        parent_tree_sha256="f" * 64,
        parent_source=b"source",
        suite_id="study",
        suite_version=1,
        calibration=None,
        harness_sha256="d" * 64,
        image_sha256="e" * 64,
        system="S1",
        context=context,
        provider=provider,
        tasks=(),
        budget=budget,
        artifact_root=tmp_path,
        best_suite=0.0,
    )
    with pytest.raises(Reserved):
        module.run_one_proposal(None, None, **kwargs)
    assert receipts[0]["model_tokens"] == 0
    assert receipts[0]["wall_seconds"] == 10
    assert budget.proposal_count == 1
    kwargs.update(
        provider=SimpleNamespace(provider_id="operating-mode-grid.v1"),
        budget=RunBudget(proposal_limit=1, wall_limit=1000, token_limit=0),
    )
    with pytest.raises(RuntimeError, match="token_budget_exhausted"):
        module.run_one_proposal(None, None, **kwargs)
    assert len(receipts) == 1
