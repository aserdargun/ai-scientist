"""Historical dev handoff: bounded verification, immutable retries and context readback."""

import copy
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, insert, select, update

from lab.api import app as api_module
from lab.api import experience
from lab.api.contracts import StartRunRequest
from lab.api.mode_experiments import ModeAgentRequest, ModeGridRequest
from lab.db.schema import reports, runs
from lab.director.contracts import (
    ReplayManifestDocument,
    SourceProvenance,
    replay_configuration_sha256,
)
from lab.director.fake_llm import AgentContext, prompt_context_sha256
from lab.director.history_context import (
    PriorDevFinding,
    PriorExperienceSelection,
    load_frozen_prior_findings,
    snapshot_bytes,
)
from lab.director.ledger import canonical_json_bytes
from lab.operating_modes import ModeConfig, candidate_source
from lab.reporting import _LedgerExperiment, read_run_pairs
from tests.test_api_and_scorer import _engine, _request
from tests.test_experience_api import LedgerEngine, Result, write_blob
from tests.test_report_replay import _pair


def sha(value):
    return hashlib.sha256(value).hexdigest()


def replay(exp, *, confirmed=False):
    digest = "d" * 64
    seeds = (0, 1) if confirmed else (0,)
    fields = {
        "schema": "referee-replay.v1",
        "decision_stage": "confirmed" if confirmed else "primary",
        "experiment_id": exp.experiment_id,
        "run_id": exp.run_id,
        "parent_experiment_id": exp.parent_experiment_id,
        "parent_tree": exp.parent_tree,
        "child_tree": exp.child_tree,
        "candidate_sha256": exp.candidate_sha256,
        "parent_source_sha256": digest,
        "child_source_sha256": exp.candidate_sha256,
        "calibration_sha256": exp.calibration_sha256,
        "harness_sha256": exp.harness_sha256,
        "image_sha256": exp.image_sha256,
        "suite_id": exp.suite_id,
        "suite_version": exp.suite_version,
        "referee_source_sha256": digest,
        "python_version": "3.12.13",
        "numpy_version": "2.2.6",
        "task_ids": ("task-dev-1",),
        "expected_decision": exp.decision.model_dump(mode="json"),
        "task_families": ("EVT",),
        "profile_sha256": (digest,),
        "normalization_base": (0.1,),
        "normalization_reference": (0.9,),
        "parent": (0.2,),
        "child": (0.4,),
        "weights": (1.0,),
        "parent_raw_scores": ((0.3,),) * len(seeds),
        "child_raw_scores": ((0.5,),) * len(seeds),
        "parent_noise_raw_scores": ((0.3,), (0.4,), (0.5,)),
        "parent_noise_seed_ids": (0, 1, 2),
        "parent_noise_evaluation_kinds": ("baseline",) * 3,
        "parent_noise_output_sha256": ((digest,),) * 3,
        "parent_noise_score_sha256": (digest,) * 3,
        "parent_seed_ids": seeds,
        "parent_seed_evaluation_kinds": ("baseline",) * len(seeds),
        "decision_seeds": seeds,
        "parent_seed_output_sha256": ((digest,),) * len(seeds),
        "parent_seed_score_sha256": (digest,) * len(seeds),
        "child_seed_output_sha256": ((digest,),) * len(seeds),
        "child_seed_score_sha256": (digest,) * len(seeds),
        "eps": 0.01,
        "noise_sd": 0.0,
        "simpler": False,
        "guards_ok": True,
        "guard_results": exp.guards,
        "position_bias_threshold": 0.8,
        "max_task_drop": 0.5,
        "best_suite": 0.2,
        "n_boot": 4000,
        "bootstrap_seed": 0,
    }
    fields["configuration_sha256"] = replay_configuration_sha256(fields)
    return ReplayManifestDocument.model_validate(fields, strict=True)


class DevEngine(LedgerEngine):
    """SQL transport stub; source document/blob verification is production code."""

    def execute(self, statement, params):
        sql = str(statement)
        if "lab.dev_task_results" not in sql:
            return super().execute(statement, params)
        self.statements.append(sql)
        assert params == {
            "run_id": self.run,
            "experiment_id": self.receipt["experiment_id"],
            "candidate_sha256": self.candidate_sha,
            "harness_sha256": self.harness_sha,
        }
        assert "seed IN (0,1)" in sql and "LIMIT 129" in sql
        # The noise seed is not part of the Referee decision evidence scope.
        return Result([row for row in self.dev_rows if row["seed"] in (0, 1)])


@pytest.fixture
def source(tmp_path):
    pair = _pair(uuid4(), baseline=False, sequence=1)
    candidate = candidate_source(ModeConfig(method="lsh"))
    exp = pair.document.model_copy(
        update={
            "candidate_sha256": sha(candidate),
            "candidate_blob_sha256": sha(candidate),
            "per_task": (pair.document.per_task[0].model_copy(update={"task_id": "task-dev-1"}),),
            "guards": {
                "hardcoding": "pass",
                "determinism": "pass",
                "causality": "pass",
                "position_bias": "pass",
            },
        }
    )
    provenance = SourceProvenance(
        dataset_id="synthetic",
        split_id="dev",
        session_id="session-1",
        source_manifest_sha256="a" * 64,
        source_revision="fixture.v1",
        license_id="research-fixture",
        attribution="NEVER-PROMPT-THIS",
        access_terms="NEVER-PROMPT-THIS",
        usage_profile="noncommercial_research",
    )
    trajectory = pair.trajectory.model_copy(
        update={
            "source_provenance": (provenance,),
            "replay_manifests": (replay(exp),),
        }
    )
    payloads = {
        "experiment": canonical_json_bytes(exp.model_dump(mode="json", by_alias=True)),
        "trajectory": canonical_json_bytes(trajectory.model_dump(mode="json", by_alias=True)),
        "candidate": candidate,
        "messages": b"messages",
    }
    digests = {key: sha(raw) for key, raw in payloads.items()}
    for name, raw in payloads.items():
        write_blob(tmp_path, digests[name], raw)
    receipt = {
        "experiment_id": exp.experiment_id,
        "run_id": str(exp.run_id),
        "status": exp.status,
        "experiment_sha256": digests["experiment"],
        "experiment_blob_sha256": digests["experiment"],
        "trajectory_sha256": digests["trajectory"],
        "trajectory_blob_sha256": digests["trajectory"],
        "messages_blob_sha256": digests["messages"],
    }
    engine = DevEngine(
        exp.run_id,
        [{"experiment_id": exp.experiment_id, "sequence": 1, "status": "scored"}],
        receipt,
        postgres=True,
    )
    engine.candidate_sha, engine.harness_sha = exp.candidate_sha256, exp.harness_sha256
    engine.dev_rows = [
        {
            "task_id": "task-dev-1",
            "evaluation_kind": "primary",
            "seed": 0,
            "dataset_id": "synthetic",
            "split_id": "dev",
            "session_id": "session-1",
            "profile_sha256": "d" * 64,
            "candidate_sha256": exp.candidate_sha256,
            "harness_sha256": exp.harness_sha256,
        }
    ]
    reader = experience.ExperienceReader(engine, tmp_path)
    verified = {
        "run_id": str(exp.run_id),
        "report_sha256": "c" * 64,
        "report": {"status": "completed", "provider": "mode-grid"},
    }
    source_request = {"request": {"suite_manifest_sha256": "b" * 64}, "sha256": "e" * 64}
    refs = {
        "source_run_id": str(exp.run_id),
        "source_report_sha256": "c" * 64,
        "records": [
            {
                "experiment_id": exp.experiment_id,
                "experiment_sha256": digests["experiment"],
                "trajectory_sha256": digests["trajectory"],
            }
        ],
    }
    selection = PriorExperienceSelection.model_validate(refs, strict=True)
    snapshot = experience.freeze_prior_findings(reader, selection, verified, source_request)
    return SimpleNamespace(
        root=tmp_path,
        reader=reader,
        engine=engine,
        pair=_LedgerExperiment(1, exp, trajectory),
        verified=verified,
        source_request=source_request,
        selection=selection,
        snapshot=snapshot,
        digests=digests,
    )


def frozen_request(data):
    return {
        "prior_experience": data.selection.model_dump(mode="json"),
        "prior_findings": data.snapshot.model_dump(mode="json", by_alias=True),
        "prior_findings_sha256": sha(snapshot_bytes(data.snapshot)),
    }


def context(data=None):
    return AgentContext(
        phase="proposal",
        experiment_number=1,
        task_cards=("target-dev-task",),
        champion_source="class Champion: pass",
        recent_feedback=(),
        **(
            {
                "prior_findings": data.snapshot,
                "prior_findings_sha256": sha(snapshot_bytes(data.snapshot)),
            }
            if data
            else {}
        ),
    )


def test_verified_numeric_snapshot_is_namespaced_and_metadata_only(source):
    raw = snapshot_bytes(source.snapshot)
    assert source.snapshot.records[0].dev_suite_score == 0.7
    assert source.snapshot.records[0].method == "lsh"
    assert source.snapshot.scope == "historical-advisory-only"
    for forbidden in (b"NEVER-PROMPT", b"private prompt", b"holdout_canary", b"pipeline"):
        assert forbidden not in raw
    assert load_frozen_prior_findings(frozen_request(source)) == source.snapshot
    assert any(s == "SET TRANSACTION READ ONLY" for s in source.engine.statements)
    assert any(s.startswith("SET LOCAL statement_timeout") for s in source.engine.statements)


def test_confirmed_keep_accepts_decision_rows_and_ignores_separate_noise_seed(source):
    primary = source.pair.trajectory.replay_manifests[0]
    confirmed = replay(source.pair.document, confirmed=True)
    trajectory = source.pair.trajectory.model_copy(
        update={"replay_manifests": (primary, confirmed)}
    )
    source.engine.dev_rows.extend(
        [
            dict(source.engine.dev_rows[0], evaluation_kind="confirmation", seed=1),
            dict(source.engine.dev_rows[0], evaluation_kind="confirmation", seed=2),
        ]
    )
    finding = experience.history_finding(
        source.reader, _LedgerExperiment(1, source.pair.document, trajectory)
    )
    assert finding.decision == "KEEP"
    source.engine.dev_rows.pop(1)
    with pytest.raises(ValueError, match="coverage-unavailable"):
        experience.history_finding(
            source.reader, _LedgerExperiment(1, source.pair.document, trajectory)
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("profile_sha256", "f" * 64),
        ("session_id", "other-session"),
        ("task_id", "other-task"),
        ("split_id", "holdout"),
    ],
)
def test_dev_rows_require_exact_complete_profile_and_provenance_coverage(source, field, value):
    source.engine.dev_rows[0][field] = value
    with pytest.raises(ValueError, match="coverage-unavailable"):
        experience.history_finding(source.reader, source.pair)


def test_guards_protected_metadata_and_unknown_code_are_never_historical_lessons(source):
    exp, trj = source.pair.document, source.pair.trajectory
    failed = exp.model_copy(update={"guards": {"causality": "fail"}})
    with pytest.raises(ValueError, match="not-measured"):
        experience.history_finding(source.reader, _LedgerExperiment(1, failed, trj))
    hidden = trj.source_provenance[0].model_copy(update={"split_id": "sealed"})
    with pytest.raises(ValueError, match="provenance-not-approved"):
        experience.history_finding(
            source.reader,
            _LedgerExperiment(1, exp, trj.model_copy(update={"source_provenance": (hidden,)})),
        )
    raw = b"print('untrusted code')"
    write_blob(source.root, sha(raw), raw)
    unknown = exp.model_copy(update={"candidate_blob_sha256": sha(raw)})
    with pytest.raises(ValueError, match="method-not-approved"):
        experience.history_finding(source.reader, _LedgerExperiment(1, unknown, trj))


@pytest.mark.parametrize("mutation", ["digest", "source", "reference", "internal-only"])
def test_frozen_loader_rejects_corruption_or_rebinding_without_history_queries(source, mutation):
    request = frozen_request(source)
    if mutation == "digest":
        request["prior_findings_sha256"] = "0" * 64
    elif mutation == "source":
        request["prior_experience"]["source_report_sha256"] = "0" * 64
    elif mutation == "reference":
        request["prior_experience"]["records"][0]["trajectory_sha256"] = "0" * 64
    else:
        del request["prior_experience"]
    with pytest.raises(ValueError):
        load_frozen_prior_findings(request)


def test_snapshot_bounds_no_client_scores_and_legacy_serialization(source):
    refs = source.selection.model_dump(mode="json")
    refs["records"][0]["score"] = 0.99
    with pytest.raises(ValueError):
        PriorExperienceSelection.model_validate(refs, strict=True)
    refs = source.selection.model_dump(mode="json")
    refs["records"] *= 2
    with pytest.raises(ValueError, match="duplicate"):
        PriorExperienceSelection.model_validate(refs, strict=True)
    fields = source.snapshot.records[0].model_dump(mode="json")
    fields["dev_suite_score"] = float("nan")
    with pytest.raises(ValueError):
        PriorDevFinding.model_validate_json(json.dumps(fields), strict=True)
    large = source.snapshot.records[0].model_copy(
        update={"provenance_manifest_sha256s": ("a" * 64,) * 64}
    )
    records = tuple(large.model_copy(update={"experiment_id": f"exp_{n:032x}"}) for n in range(8))
    with pytest.raises(ValueError, match="byte bound"):
        snapshot_bytes(source.snapshot.model_copy(update={"records": records}))
    for model in (
        StartRunRequest.model_validate(_request(), strict=True),
        ModeGridRequest(
            idempotency_key="key-12345678901234", snapshot_sha256="a" * 64, configurations=[{}]
        ),
        ModeAgentRequest(idempotency_key="key-12345678901234", snapshot_sha256="a" * 64),
    ):
        assert "prior_experience" not in model.model_dump(mode="json")
    legacy = context().model_dump(mode="json")
    assert set(legacy) == {
        "phase",
        "experiment_number",
        "system",
        "move_type",
        "task_cards",
        "champion_source",
        "recent_feedback",
    }
    current = context(source)
    encoded = canonical_json_bytes(current.model_dump(mode="json"))
    assert b'"schema":"prior-dev-findings.v1"' in encoded
    assert AgentContext.model_validate_json(encoded, strict=True) == current
    assert prompt_context_sha256(current) != prompt_context_sha256(context())


@pytest.fixture
def admission(source, monkeypatch):
    engine = _engine()
    report = source.verified["report"]
    report_sha = sha(canonical_json_bytes(report))
    selection = source.selection.model_copy(update={"source_report_sha256": report_sha})
    snapshot = source.snapshot.model_copy(update={"source_report_sha256": report_sha})
    request = source.source_request["request"]
    with engine.begin() as connection:
        connection.execute(
            insert(runs).values(
                run_id=source.pair.document.run_id,
                origin="local",
                owner_id="local:default",
                idempotency_key="source-idempotency-key",
                request_json=request,
                payload_sha256=sha(canonical_json_bytes(request)),
                state="completed",
                report_sha256=report_sha,
            )
        )
        connection.execute(
            insert(reports).values(
                run_id=source.pair.document.run_id,
                report_json=report,
                report_sha256=report_sha,
                verified_at=datetime.now(UTC),
            )
        )
    frozen_calls = []
    monkeypatch.setattr(
        experience, "freeze_prior_findings", lambda *a: frozen_calls.append(a) or snapshot
    )
    app = api_module.create_app(
        director_engine=engine, director_token="u" * 32, allow_unregistered_suites=True
    )
    payload = _request()
    payload["prior_experience"] = selection.model_dump(mode="json")
    return SimpleNamespace(
        engine=engine,
        app=app,
        payload=payload,
        frozen_calls=frozen_calls,
        source_run=source.pair.document.run_id,
        snapshot=snapshot,
    )


def post(data, payload=None):
    with TestClient(data.app) as client:
        return client.post(
            "/v1/runs",
            json=payload or data.payload,
            headers={"Authorization": "Bearer " + "u" * 32},
        )


def test_admission_freezes_snapshot_hash_and_retries_when_old_history_is_unavailable(admission):
    first = post(admission)
    assert first.status_code == 202
    with admission.engine.connect() as connection:
        row = (
            connection.execute(
                select(runs).where(runs.c.idempotency_key == admission.payload["idempotency_key"])
            )
            .mappings()
            .one()
        )
    assert load_frozen_prior_findings(row["request_json"]) == admission.snapshot
    payload = {k: v for k, v in row["request_json"].items() if k != "idempotency_key"}
    assert row["payload_sha256"] == sha(canonical_json_bytes(payload))
    with admission.engine.begin() as connection:
        connection.execute(delete(reports))
    second = post(admission)
    assert second.status_code == 200 and second.json()["reused"] is True
    assert second.json()["run_id"] == first.json()["run_id"]
    assert len(admission.frozen_calls) == 1
    changed = copy.deepcopy(admission.payload)
    changed["prior_experience"]["records"][0]["trajectory_sha256"] = "0" * 64
    assert post(admission, changed).status_code == 409


@pytest.mark.parametrize(
    "mutation,status",
    [
        ("owner", 404),
        ("running", 404),
        ("stop_requested", 404),
        ("report", 500),
        ("request", 422),
        ("client-snapshot", 422),
    ],
)
def test_admission_source_authority_or_integrity_failure_never_queues(admission, mutation, status):
    with admission.engine.begin() as connection:
        if mutation == "owner":
            connection.execute(update(runs).values(owner_id="other-owner"))
        elif mutation in {"running", "stop_requested"}:
            connection.execute(update(runs).values(state=mutation))
        elif mutation == "report":
            connection.execute(update(reports).values(report_json={"status": "completed"}))
        elif mutation == "request":
            connection.execute(update(runs).values(payload_sha256="0" * 64))
    if mutation == "client-snapshot":
        admission.payload["prior_findings"] = {}
    assert post(admission).status_code == status
    assert admission.frozen_calls == []
    with admission.engine.connect() as connection:
        assert len(connection.execute(select(runs.c.run_id)).all()) == 1


def test_retry_checks_stored_snapshot_integrity(admission):
    assert post(admission).status_code == 202
    with admission.engine.begin() as connection:
        connection.execute(
            update(runs)
            .where(runs.c.idempotency_key == admission.payload["idempotency_key"])
            .values(payload_sha256="0" * 64)
        )
    assert post(admission).status_code == 500


def test_readback_requires_exact_context_blob_and_inputs_chain(source, monkeypatch):
    request = frozen_request(source)
    raw = canonical_json_bytes(context(source).model_dump(mode="json"))
    write_blob(source.root, sha(raw), raw)
    exp = source.pair.document.model_copy(update={"inputs_sha256": sha(raw)})
    trj = source.pair.trajectory.model_copy(update={"inputs_sha256": sha(raw)})
    pair = _LedgerExperiment(1, exp, trj)
    monkeypatch.setattr(experience, "read_run_pairs", lambda *a, **kw: (pair,))
    result = experience.build_experience(
        source.reader, exp.run_id, source.verified, {"request": request}
    )
    usage = result["prior_findings_usage"]
    assert usage["status"] == "context-bound" and usage["context_bound_proposal_count"] == 1
    assert usage["snapshot_sha256"] == request["prior_findings_sha256"]
    assert usage["scope"] == "historical-advisory-only"
    broken = _LedgerExperiment(1, exp, trj.model_copy(update={"inputs_sha256": "0" * 64}))
    monkeypatch.setattr(experience, "read_run_pairs", lambda *a, **kw: (broken,))
    with pytest.raises(ValueError, match="context identity"):
        experience.build_experience(
            source.reader, exp.run_id, source.verified, {"request": request}
        )
    monkeypatch.setattr(experience, "read_run_pairs", lambda *a, **kw: ())
    result = experience.build_experience(
        source.reader, exp.run_id, source.verified, {"request": request}
    )
    assert result["prior_findings_usage"]["status"] == "admitted-only"


def test_source_hash_verifier_rejects_tampered_selected_artifact(source):
    digest = source.digests["trajectory"]
    write_blob(source.root, digest, b"{}")
    reader = experience.ExperienceReader(source.engine, source.root)
    with pytest.raises(ValueError):
        read_run_pairs(
            reader,
            source.pair.document.run_id,
            artifact_root=source.root,
            max_records=200,
            artifact_reader=reader.artifact,
        )


def test_discard_is_historical_observation_with_training_still_blocked(source, monkeypatch):
    decision = source.pair.document.decision.model_copy(update={"verdict": "DISCARD"})
    exp = source.pair.document.model_copy(update={"decision": decision, "suite_score": 0.0})
    trj = source.pair.trajectory.model_copy(update={"outcome": decision})
    pair = _LedgerExperiment(1, exp, trj)
    monkeypatch.setattr(experience, "read_run_pairs", lambda *a, **kw: (pair,))
    result = experience.build_experience(
        source.reader, exp.run_id, source.verified, source.source_request
    )
    record = result["records"][0]
    assert record["history_eligibility"] == {"eligible": True, "reasons": []}
    assert record["score"] == 0.0 and record["decision"] == "DISCARD"
    assert record["training_eligibility"]["eligible"] is False
    assert "not_confirmed_positive_candidate" in record["training_eligibility"]["reasons"]


@pytest.mark.parametrize("mutation", ["report", "record"])
def test_selected_source_reference_hash_is_verified_not_just_experiment_id(source, mutation):
    fields = source.selection.model_dump(mode="json")
    if mutation == "report":
        fields["source_report_sha256"] = "0" * 64
    else:
        fields["records"][0]["experiment_sha256"] = "0" * 64
    selected = PriorExperienceSelection.model_validate(fields, strict=True)
    with pytest.raises(ValueError, match="identity differs"):
        experience.freeze_prior_findings(
            source.reader, selected, source.verified, source.source_request
        )


@pytest.mark.parametrize(
    "error,status",
    [
        (experience.ExperienceLimitError("PRIVATE-PATH"), 413),
        (experience.ExperienceTimeoutError("PRIVATE-PATH"), 503),
    ],
)
def test_admission_deadline_and_byte_limits_fail_with_generic_response(
    admission, monkeypatch, error, status
):
    def unavailable(*args):
        raise error

    monkeypatch.setattr(experience, "freeze_prior_findings", unavailable)
    response = post(admission)
    assert response.status_code == status and "PRIVATE-PATH" not in response.text
    with admission.engine.connect() as connection:
        assert len(connection.execute(select(runs.c.run_id)).all()) == 1


def test_fresh_and_resume_dispatch_keep_frozen_history_in_execution_contract(
    source, monkeypatch, tmp_path
):
    from lab import cli
    from tests.test_director_resume_entrypoint047 import validated_target

    row, registry = validated_target(monkeypatch, tmp_path)
    row["request_json"].update(frozen_request(source))
    row["payload_sha256"] = sha(canonical_json_bytes(row["request_json"]))
    initial = cli._validate_dispatch_target(source.pair.document.run_id, row, registry)
    resumed = cli._validate_dispatch_target(source.pair.document.run_id, row, registry)
    assert initial.contract == resumed.contract
    assert initial.contract.execution_json["request"]["prior_findings_sha256"] == sha(
        snapshot_bytes(source.snapshot)
    )
    row["request_json"]["prior_findings"]["records"][0]["dev_suite_score"] = 0.99
    row["payload_sha256"] = sha(canonical_json_bytes(row["request_json"]))
    with pytest.raises(ValueError, match="frozen findings digest"):
        cli._validate_dispatch_target(source.pair.document.run_id, row, registry)


def test_director_loop_passes_frozen_context_to_provider_before_any_execution(source, monkeypatch):
    from lab.director import holdout_replay
    from lab.director import loop as loop_module
    from lab.director.budget import RunBudget
    from lab.director.fake_llm import FakeLLM
    from tests.test_director_loop_core import _turn
    from tests.test_director_loop_strategy import MemoryLease, _state

    class ObservedContext(Exception):
        pass

    from lab.director.field_context import FieldContext, FieldIntent

    field_context = FieldContext(
        schema="field-study-context.v1",
        intent=FieldIntent(
            asset_id="fixture-pump", goal_kind="predictive_maintenance", objective="Study modes"
        ),
        snapshot_sha256="a" * 64,
    )
    state, lease = _state(), MemoryLease()
    lease.heartbeat = lambda: None
    provider = FakeLLM((_turn("historical-advisory-bound"),))
    loop = loop_module.DirectorLoop(
        object(),
        object(),
        lease=lease,
        runner=object(),
        run_id=state.run_id,
        tasks=(),
        suite_manifest_sha256="a" * 64,
        provider=provider,
        budget=RunBudget(proposal_limit=1, wall_limit=60, token_limit=1000),
        artifact_root=source.root,
        proposal_limit=1,
        prior_findings=source.snapshot,
        field_context=field_context,
    )
    monkeypatch.setattr(loop_module, "read_registered_calibration", lambda *a, **kw: object())
    monkeypatch.setattr(holdout_replay, "recover_observed_terminal_holdouts", lambda *a, **kw: None)
    monkeypatch.setattr(loop, "_verify_execution_identity", lambda *a: None)
    monkeypatch.setattr(loop, "_load_or_initialize", lambda *a: (state, {}))
    monkeypatch.setattr(loop, "_pending_run_end_terminal_result", lambda *a: None)
    monkeypatch.setattr(loop, "_recover_unresolved_holdout_budget", lambda: None)
    monkeypatch.setattr(loop, "_reconcile_periodic_holdout", lambda s, c: (s, c))
    monkeypatch.setattr(loop, "_recover_committed_terminal", lambda *a: None)
    monkeypatch.setattr(loop, "_next_checkpoint_sequence", lambda: lease.append_count + 1)
    monkeypatch.setattr(loop, "_task_cards", lambda *a: ("target dataset dev task",))
    monkeypatch.setattr(loop, "_champion_source", lambda *a: b"class Champion: pass")
    observed = []

    def capture(**kwargs):
        prompt = kwargs["context"]
        observed.append(prompt)
        assert provider.propose(prompt).proposal.hypothesis == "historical-advisory-bound"
        raise ObservedContext

    monkeypatch.setattr(loop, "_run_one", capture)
    with pytest.raises(RuntimeError, match="proposal infrastructure failed") as caught:
        loop.run()
    assert isinstance(caught.value.__cause__, ObservedContext)
    assert len(observed) == 1
    assert observed[0].field_context == field_context
    assert observed[0].field_context_sha256 == field_context.sha256
    assert observed[0].prior_findings == source.snapshot
    assert observed[0].prior_findings_sha256 == sha(snapshot_bytes(source.snapshot))
