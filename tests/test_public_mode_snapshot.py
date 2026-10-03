"""Inert CPU fixtures for the public task authority; no public data, DB grants or services."""

import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import event, select, update

import lab.cli as cli
from lab.api import app as api_module
from lab.api.mode_experiments import (
    ModeGridRequest,
    ModeSnapshotStore,
    atomic_private,
    canonical_document,
    register_grid,
)
from lab.api.registry import ApiPrincipal, PublicDevStudy, SuiteEntry
from lab.db.schema import dataset_labels, dataset_profiles, dataset_task_semantics, runs
from lab.director.contracts import SourceProvenance
from lab.director.public_suite import (
    ScorerTaskRegistration,
    install_scorer_task,
    task_semantics_sha256,
)
from lab.director.suite_manifest import SuiteTaskManifest, load_suite_manifest
from lab.operating_modes import public_snapshot as public_module
from lab.operating_modes.public_snapshot import PublicTaskBinding, PublicTaskSnapshot, digest
from scripts import install_public_mode_snapshot as importer
from tests.test_api_and_scorer import _engine


@pytest.fixture
def public_installation(tmp_path, monkeypatch):
    # Explicit fixture source identity, not proof of acceptance of the real cached public task.
    source = tmp_path / "source-suite.json"
    source.write_bytes(b"fixture-reviewed-source-suite")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(public_module, "PUBLIC_SUITE_SHA256", source_sha)
    provenance = SourceProvenance(
        dataset_id="SKAB",
        split_id="public-benchmark-fixed-source-prefix.v1",
        session_id="fixture",
        source_manifest_sha256="b" * 64,
        source_revision="fixture-revision",
        license_id="GPL-3.0",
        attribution="inert fixture",
        access_terms="research fixture",
        usage_profile="noncommercial_research",
    )
    task = SuiteTaskManifest(
        task_id=public_module.SKAB_TASK_ID,
        dataset_id="SKAB",
        split_id=provenance.split_id,
        session_id=provenance.session_id,
        profile_sha256="c" * 64,
        family="NRM",
        task_weight=0.1,
        independent_family="SKAB:valve:NRM",
        label_tier="silver",
        provenance=provenance,
        context=dict(
            seed=0,
            signals=["pressure", "flow"],
            regime_signals=["pressure"],
            sampling_s=1,
            time_budget_s=30.0,
        ),
        columns=("pressure", "flow"),
        train=tuple((float(i), float(i + 1)) for i in range(26)),
        evaluation=tuple((float(i), float(i + 1)) for i in range(470)),
    )
    times = tuple(range(470))
    masks = (False,) * 470
    semantics = task_semantics_sha256(
        task_family="NRM",
        sampling_s=1,
        evaluation_times=times,
        masked_samples=masks,
        failure_windows=(),
    )
    registration = ScorerTaskRegistration(
        dataset_id="SKAB",
        split_id=task.split_id,
        session_id=task.session_id,
        sample_count=470,
        sliding_window=1,
        embargo_seconds=0,
        profile_sha256=task.profile_sha256,
        task_family="NRM",
        sampling_s=1,
        labels=tuple(i >= 450 for i in range(470)),
        evaluation_times=times,
        masked_samples=masks,
        failure_windows=(),
        semantics_sha256=semantics,
        visibility="dev",
    )
    binding = PublicTaskBinding(
        schema="mode-public-task-binding.v1",
        source_suite_manifest_sha256=source_sha,
        original_task_sha256=digest(task.model_dump(mode="json")),
        original_task=task,
        semantics_sha256=semantics,
        train_matrix_sha256=digest(task.train),
        evaluation_matrix_sha256=digest(task.evaluation),
        source_train_start_index=89,
        source_train_end_index=115,
        source_evaluation_start_index=730,
        source_evaluation_end_index=1200,
    )
    snapshot = PublicTaskSnapshot(schema="public-task-snapshot.v1", binding=binding)
    monkeypatch.setattr(
        importer, "materialize_public_snapshot", lambda *_: (snapshot, registration)
    )
    runtime = tmp_path / "data/runtime"
    store = ModeSnapshotStore(runtime / "mode-snapshots")
    registry_path = runtime / "registry.json"
    atomic_private(
        registry_path, canonical_document({"schema": "lab-suite-registry.v1", "suites": []})
    )
    engine = _engine()
    install_scorer_task(engine, registration)  # Fixture setup only; importer never installs.
    statements = []

    @event.listens_for(engine, "before_cursor_execute")
    def capture_sql(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    result = importer.import_snapshot(
        source_repository_root=tmp_path,
        source_suite=source,
        expected_sha256=source_sha,
        engine=engine,
        store=store,
        owner="field-lab",
        origin="local",
        registry_path=registry_path,
        runtime_root=runtime,
        configurations=[{"method": "lsh"}, {"method": "som"}],
    )
    assert len(statements) == 3 and all(s.lstrip().upper().startswith("SELECT") for s in statements)
    from lab.api.registry import load_suite_registry

    registry = load_suite_registry(registry_path, runtime)
    entry = registry.get(result["suite_id"])
    return SimpleNamespace(
        store=store,
        snapshot=snapshot,
        binding=binding,
        registration=registration,
        runtime=runtime,
        engine=engine,
        registry=registry,
        entry=entry,
        registry_path=registry_path,
        result=result,
        source=source,
    )


def test_public_import_preserves_whole_task_and_excludes_labels_and_utc(public_installation):
    f = public_installation
    description = f.result
    assert description["train_rows"] == 26 and description["evaluation_rows"] == 470
    assert description["first_utc"] is None and description["last_utc"] is None
    assert (
        description["time_axis"] == "source_order_index"
        and description["source_time_kind"] == "naive"
    )
    assert description["source_ranges"] == {"train": [89, 115], "evaluation": [730, 1200]}
    assert description["provenance"] == f.snapshot.provenance.model_dump(mode="json")
    assert description["source_labels_exported"] is False and description["scorer_written"] is False
    directory = f.store.directory(f.snapshot.sha256)
    for path in directory.glob("*.json"):
        content = json.loads(path.read_bytes())
        assert "labels" not in content and "evaluation_times" not in content
    assert f.store.public_snapshot(f.snapshot.sha256) == f.snapshot
    assert len(f.store.public_snapshot(f.snapshot.sha256).frame("train")) == 26
    assert len(f.store.public_snapshot(f.snapshot.sha256).frame("evaluation")) == 470
    with pytest.raises(ValidationError):
        f.store.load(f.snapshot.sha256)  # Public data cannot become synthetic/local-LLM data.


@pytest.mark.parametrize("mutation", ["missing", "labels", "semantics", "holdout"])
def test_importer_rejects_unregistered_or_changed_scorer_without_writes(
    public_installation, mutation
):
    f = public_installation
    with f.engine.begin() as connection:
        if mutation == "missing":
            connection.execute(dataset_profiles.delete())
        elif mutation == "labels":
            connection.execute(
                update(dataset_labels)
                .where(dataset_labels.c.sample_index == 0)
                .values(is_anomaly=True)
            )
        elif mutation == "semantics":
            connection.execute(
                update(dataset_task_semantics).values(evaluation_times_json=list(range(1, 471)))
            )
        else:
            connection.execute(update(dataset_profiles).values(visibility="holdout"))
    before = {p: p.read_bytes() for p in f.runtime.rglob("*.json")}
    with pytest.raises(ValueError, match="registration is unavailable"):
        importer.import_snapshot(
            source_repository_root=f.runtime,
            source_suite=f.source,
            expected_sha256=f.binding.source_suite_manifest_sha256,
            engine=f.engine,
            store=f.store,
            owner="field-lab",
            origin="local",
            registry_path=f.registry_path,
            runtime_root=f.runtime,
            configurations=[{"method": "lsh"}],
        )
    assert before == {p: p.read_bytes() for p in f.runtime.rglob("*.json")}


def test_public_hash_and_original_task_guards(public_installation):
    f = public_installation
    original = f.binding.model_dump(mode="json", by_alias=True)
    for change in (
        {"source_train_end_index": 114},
        {"evaluation_matrix_sha256": "f" * 64},
        {"source_suite_manifest_sha256": "f" * 64},
    ):
        with pytest.raises(ValidationError):
            PublicTaskBinding.model_validate(original | change)
    changed = f.binding.original_task.model_copy(
        update={"context": f.binding.original_task.context | {"seed": 1}}
    )
    with pytest.raises(ValueError, match="preserve"):
        f.binding.verify_study_task(changed.model_copy(update={"task_weight": 1.0}))
    binding_path = f.store.directory(f.snapshot.sha256) / "binding.json"
    binding_path.write_bytes(binding_path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="immutable"):
        f.registry.verify_entry(f.entry)


def test_public_suite_requires_derived_binding_and_trusted_dev_profile(public_installation):
    f = public_installation
    path, _ = f.registry.verify_entry(f.entry)

    @event.listens_for(f.engine, "before_cursor_execute", retval=True)
    def sqlite_profile(connection, cursor, statement, parameters, context, many):
        return statement.replace("scorer.dataset_profiles", "dataset_profiles"), parameters

    with pytest.raises(ValueError, match="snapshot authority"):
        load_suite_manifest(path, f.engine, study_snapshot_sha256=f.snapshot.sha256)
    with pytest.raises(ValueError, match="original task binding"):
        load_suite_manifest(
            path, f.engine, study_snapshot_sha256="f" * 64, public_task_binding=f.binding
        )
    _, tasks, _ = load_suite_manifest(
        path, f.engine, study_snapshot_sha256=f.snapshot.sha256, public_task_binding=f.binding
    )
    assert tasks[0].provenance == f.snapshot.provenance and tasks[0]._train.shape[0] == 26
    with f.engine.begin() as connection:
        connection.execute(update(dataset_profiles).values(visibility="holdout"))
    with pytest.raises(ValueError, match="trusted dev profile"):
        load_suite_manifest(
            path, f.engine, study_snapshot_sha256=f.snapshot.sha256, public_task_binding=f.binding
        )


def test_public_grid_requires_policy_and_exact_budget(public_installation):
    f = public_installation

    def request(configurations, wall=600):
        return ModeGridRequest(
            idempotency_key="public-grid-fixture",
            snapshot_sha256=f.snapshot.sha256,
            configurations=configurations,
            wall_seconds=wall,
        )

    for configurations, wall in (([{"method": "lsh"}] * 3, 600), ([{"method": "lsh"}], 601)):
        with pytest.raises(ValueError, match="CPU policy"):
            register_grid(
                f.store,
                request(configurations, wall),
                f.registry_path,
                f.runtime,
                public_dev_study=f.entry.public_dev_study,
            )
    with pytest.raises(ValueError, match="owner-bound"):
        register_grid(f.store, request([{"method": "lsh"}]), f.registry_path, f.runtime)
    with pytest.raises(ValidationError):
        PublicDevStudy.model_validate(
            f.entry.public_dev_study.model_dump() | {"model_tokens": True}
        )
    with pytest.raises(ValidationError):
        SuiteEntry.model_validate(f.entry.model_dump(mode="json") | {"provider": "local-qwen"})


def test_public_api_owner_generic_budget_grid_agent_and_immutable_dispatch(
    public_installation, monkeypatch
):
    f = public_installation
    monkeypatch.setattr(api_module, "PROJECT_ROOT", f.runtime.parent.parent)
    monkeypatch.setattr(cli, "PROJECT_ROOT", f.runtime.parent.parent)
    monkeypatch.setattr(
        api_module, "compute_harness_hash", lambda *_: SimpleNamespace(sha256="a" * 64)
    )
    monkeypatch.setenv("LAB_SUITE_REGISTRY_FILE", str(f.registry_path))
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
    payload = dict(
        idempotency_key="public-api-fixture",
        track="mode",
        suite=f.entry.suite_id,
        program_version=f.entry.program_version,
        budget=dict(experiments=2, wall_seconds=600, model_tokens=0),
    )
    with TestClient(app) as client:
        assert (
            client.get(f"/v1/mode-snapshots/{f.snapshot.sha256}", headers=headers).json()[
                "source_kind"
            ]
            == "public_dev"
        )
        assert (
            client.get(f"/v1/mode-snapshots/{f.snapshot.sha256}", headers=other).status_code == 404
        )
        assert (
            client.post(
                f"/v1/mode-snapshots/{f.snapshot.sha256}/install", headers=headers
            ).status_code
            == 422
        )
        assert client.post("/v1/runs", headers=other, json=payload).status_code == 403
        for changes in ({"wall_seconds": 601}, {"experiments": 3}, {"model_tokens": 1}):
            assert (
                client.post(
                    "/v1/runs",
                    headers=headers,
                    json=payload | {"budget": payload["budget"] | changes},
                ).status_code
                == 422
            )
        assert (
            client.post(
                "/v1/mode-experiments",
                headers=headers,
                json=dict(
                    idempotency_key="public-api-grid-invalid",
                    snapshot_sha256=f.snapshot.sha256,
                    configurations=[{"method": "lsh"}] * 3,
                ),
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/v1/mode-agent-experiments",
                headers=headers,
                json=dict(
                    idempotency_key="public-api-agent-invalid", snapshot_sha256=f.snapshot.sha256
                ),
            ).status_code
            == 422
        )
        first = client.post("/v1/runs", headers=headers, json=payload)
        assert first.status_code == 202, first.text
        assert client.post("/v1/runs", headers=headers, json=payload).status_code == 200
    with f.engine.connect() as connection:
        request = connection.execute(select(runs.c.request_json)).scalar_one()
    assert request["public_dev_study"] == f.entry.public_dev_study.model_dump(mode="json")
    path, _ = f.registry.verify_entry(f.entry)

    def dispatch(value=request, owner="field-lab"):
        return cli._registered_public_grid_binding(
            value, path, owner_id=owner, origin="local", snapshot_sha256=f.snapshot.sha256
        )

    assert dispatch() == f.binding
    with pytest.raises(ValueError, match="immutable registry"):
        dispatch(owner="other")
    for key in (
        "public_dev_study",
        "provider_registry_entry_sha256",
        "snapshot_sha256",
        "suite_manifest_sha256",
    ):
        with pytest.raises(ValueError, match="immutable registry"):
            dispatch(request | {key: "wrong"})
    with pytest.raises(ValueError, match="CPU policy"):
        dispatch(request | {"budget": request["budget"] | {"model_tokens": 1}})
