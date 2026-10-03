"""Opt-in genuine-role SQL probes with explicitly synthetic native observations.

Only LAB_HOLDOUT_TEST_DSN_DIR's prefix-guarded disposable database is accepted.
No processes, containers, GPU, model, or original failed run are touched. Native
proof JSON is deliberately a SQL fixture; these tests are not OS-drain acceptance.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_postgres_holdout030_recovery import SUITE_VERSION, _engines
from test_postgres_run_end_unavailable import _seed_current_profile_and_suite

from lab.director.artifacts import store_director_artifact
from lab.director.contracts import ExperimentDocument, TrajectoryDocument
from lab.director.ledger import canonical_json_bytes, commit_experiment_record, register_experiment
from lab.director.ownership import (
    ExecutionContract,
    OwnerProcessIdentity,
    bind_execution_owner,
    canonical_execution_bytes,
    claim_initial_execution,
    reset_execution_owner,
)
from lab.director.recovery import OwnerGeneration, _ensure_recovery_intent, _request_sha256
from lab.director.task_plan import RunTaskAssignment, plan_run_tasks, seal_run_task_plan
from lab.scorer.service import IndependentScorer

pytestmark = pytest.mark.live
RPC = text("SELECT lab.record_empty_baseline_stop(:stop,:phase,:evidence)")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _seed(engines, tmp_path):
    migrator, director, planner, scorer = engines
    run, stop = uuid4(), uuid4()
    suite = f"synthetic.empty-stop.{run.hex[:12]}"
    dataset, split, session, manifest = _seed_current_profile_and_suite(migrator, scorer, suite)
    request = dict(
        purpose="baseline",
        track="anomaly",
        suite=suite,
        suite_manifest_sha256=manifest,
        provider="fake-json",
        provider_config_sha256=None,
        provider_registry_entry_sha256="a" * 64,
        harness_sha256="b" * 64,
        image_sha256="c" * 64,
        program_version="synthetic-native-proof-fixture",
        proposal_limit=0,
        budget=dict(experiments=0, wall_seconds=120, model_tokens=0),
    )
    request_sha = _sha(canonical_execution_bytes(request))
    with migrator.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO lab.runs(run_id,origin,owner_id,idempotency_key,payload_sha256,"
                "request_json,state) VALUES(:run,'local','fixture:empty-stop',:key,:sha,"
                "CAST(:request AS jsonb),'queued')"
            ),
            dict(
                run=run, key=f"empty-stop-{run.hex}", sha=request_sha, request=json.dumps(request)
            ),
        )
    unit = f"swapp-ai-scientist-director-dispatch-{run.hex}.service"
    process = OwnerProcessIdentity(
        request_sha, 101, 123, str(uuid4()), unit, uuid4().hex, "/user.slice/fixture/" + unit
    )
    contract = ExecutionContract.build(
        run_id=run,
        request=request,
        request_sha256=request_sha,
        suite_id=suite,
        suite_version=SUITE_VERSION,
        suite_manifest_sha256=manifest,
        registry_entry_sha256="a" * 64,
        harness_sha256="b" * 64,
        image_sha256="c" * 64,
    )
    owner = claim_initial_execution(director, contract=contract, process=process)
    token = bind_execution_owner(owner)
    experiment = f"exp_{uuid4().hex}"
    source_sha = store_director_artifact(
        b"synthetic source; never executed", artifact_root=tmp_path
    )
    inputs_sha = store_director_artifact(b"synthetic original inputs", artifact_root=tmp_path)
    register_experiment(
        director,
        experiment_id=experiment,
        run_id=str(run),
        sequence=0,
        experiment_number=None,
        kind="baseline",
        baseline_name="robust_z",
        parent_experiment_id=None,
        candidate_sha256=source_sha,
        candidate_blob_sha256=source_sha,
        inputs_sha256=inputs_sha,
        move_type="detector",
        system="S1",
        hypothesis="synthetic zero-job SQL fixture",
        predicted_delta=None,
        proposal={"schema": "baseline.registration.v1", "fixture_only": True},
    )
    plan_run_tasks(
        planner,
        run_id=run,
        assignments=tuple(
            RunTaskAssignment(
                experiment_id=experiment,
                evaluation_kind="baseline",
                task_id=dataset,
                seed=seed,
                candidate_sha256=source_sha,
                dataset_id=dataset,
                split_id=split,
                session_id=session,
            )
            for seed in (0, 1, 2)
        ),
    )
    with director.begin() as connection:
        result = connection.execute(
            text("SELECT lab.request_director_run_stop(:run,'fixture:empty-stop','local')"),
            {"run": run},
        ).scalar_one()
        assert result["state"] == "stop_requested"
    historical = OwnerGeneration(**asdict(process))
    _ensure_recovery_intent(
        director,
        recovery_id=stop,
        run_id=run,
        request_sha256=_request_sha256(run, "stop_and_finalize"),
        observed_state="stop_requested",
        owner=historical,
    )
    with director.begin() as connection:
        assert (
            connection.execute(
                text("SELECT lab.begin_stopped_baseline_closure(:stop)"), {"stop": stop}
            ).scalar_one()
            > 0
        )
    with scorer.begin() as connection:
        context = connection.execute(
            text("SELECT lab.empty_baseline_stop_context(:stop)"), {"stop": stop}
        ).scalar_one()
    worker_unit = f"swapp-ai-scientist-scorer-{uuid4().hex}.service"
    worker = dict(
        worker_pid=202,
        worker_start_ticks=456,
        worker_boot_id=str(uuid4()),
        worker_unit=worker_unit,
        worker_invocation_id=uuid4().hex,
        worker_cgroup="/user.slice/swapp-ai-scientist-scorer.slice/" + worker_unit,
    )
    native = dict(
        schema="empty-baseline-native-observation.v1",
        run_id=str(run),
        owner_json=asdict(historical),
        observed_at=datetime.now(UTC).isoformat(),
        observed_boot_id=historical.worker_boot_id,
        process_retired=True,
        native_children_empty=True,
        sandbox_container_ids=[],
        unit_properties=dict(
            LoadState="loaded",
            ActiveState="inactive",
            MainPID="0",
            InvocationID=historical.worker_invocation_id,
            ControlGroup=historical.worker_cgroup,
        ),
    )
    evidence = dict(context=context, worker_identity=worker, observation=native)
    return dict(
        run=run,
        stop=stop,
        owner=owner,
        token=token,
        evidence=evidence,
        experiment=experiment,
        source_sha=source_sha,
        inputs_sha=inputs_sha,
        suite=suite,
        tmp_path=tmp_path,
        contract=contract,
    )


@pytest.fixture
def native_sql(tmp_path, monkeypatch):
    monkeypatch.setattr("lab.scorer.jobs.MIN_FREE_DISK_BYTES", 0)
    engines = _engines()
    fixture = None
    try:
        fixture = _seed(engines, tmp_path)
        yield engines, fixture
    finally:
        if fixture is not None:
            reset_execution_owner(fixture["token"])
        for engine in engines:
            engine.dispose()


def _record(engine, fixture, phase, evidence=None):
    with engine.begin() as connection:
        return connection.execute(
            RPC,
            dict(
                stop=fixture["stop"],
                phase=phase,
                evidence=json.dumps(fixture["evidence"] if evidence is None else evidence),
            ),
        ).scalar_one()


def _retirement(fixture):
    original = fixture["evidence"]
    worker = original["worker_identity"]
    return dict(
        context=original["context"],
        worker_identity=worker,
        observation=dict(
            schema="attempted-stop-retirement-observation.v2",
            worker_identity=worker,
            observed_at=datetime.now(UTC).isoformat(),
            observed_boot_id=worker["worker_boot_id"],
            process_retired=True,
            cgroup_empty=True,
            unit_properties=dict(
                LoadState="loaded",
                ActiveState="inactive",
                MainPID="0",
                InvocationID=worker["worker_invocation_id"],
                ControlGroup=worker["worker_cgroup"],
            ),
        ),
    )


def _close(planner, fixture):
    with planner.begin() as connection:
        return connection.execute(
            text("SELECT lab.close_stopped_unattempted_tasks(:stop,:exp)"),
            dict(stop=fixture["stop"], exp=fixture["experiment"]),
        ).scalar_one()


def _documents(director, fixture):
    shared = dict(
        run_id=fixture["run"],
        experiment_id=fixture["experiment"],
        kind="baseline",
        experiment_number=None,
        baseline_name="robust_z",
        calibration_sha256=None,
        agent_version="synthetic-empty-stop.v1",
        inputs_sha256=fixture["inputs_sha"],
        system="S1",
    )
    experiment = ExperimentDocument.model_validate(
        dict(
            **shared,
            schema="experiment.v1",
            ordinal=1,
            parent_experiment_id=None,
            candidate_sha256=fixture["source_sha"],
            candidate_blob_sha256=fixture["source_sha"],
            move_type="detector",
            hypothesis="synthetic zero-job SQL fixture",
            predicted_delta=None,
            parent_tree="e" * 40,
            child_tree="e" * 40,
            harness_sha256="b" * 64,
            image_sha256="c" * 64,
            suite_id=fixture["suite"],
            suite_version=SUITE_VERSION,
            per_task=(),
            suite_score=None,
            guards={"fixture_only": "not_run"},
            decision=None,
            status="abandoned",
            fit_seconds=None,
            score_seconds=None,
            llm_input_tokens=0,
            llm_output_tokens=0,
            wall_seconds=None,
        ),
        strict=True,
    )
    trajectory = TrajectoryDocument.model_validate(
        dict(
            **shared,
            schema="trajectory.v1",
            trajectory_id=f"trj_{uuid4().hex}",
            # This is the exact no-model contract marker checked by SQL.
            # Fixture provenance stays in agent_version/template/exclusions.
            model_id="baseline/no-llm.v1",
            usage_profile="noncommercial_research",
            source_provenance=(),
            quantization="none",
            adapter="none",
            thinking=False,
            temperature=0.0,
            top_p=1.0,
            context_template="synthetic.empty.stop.sql.v1",
            messages_blob_sha256=store_director_artifact(b"[]", artifact_root=fixture["tmp_path"]),
            tool_calls=0,
            outcome=None,
            quality_tier="bronze",
            secrets_scrubbed=False,
            people_scrubbed=False,
            raw_values_scrubbed=False,
            exclusions=("Synthetic SQL/native observation fixture; no measured model or OS proof",),
        ),
        strict=True,
    )
    commit_experiment_record(
        director,
        experiment=experiment,
        trajectory=trajectory,
        experiment_blob_sha256=store_director_artifact(
            canonical_json_bytes(experiment), artifact_root=fixture["tmp_path"]
        ),
        trajectory_blob_sha256=store_director_artifact(
            canonical_json_bytes(trajectory), artifact_root=fixture["tmp_path"]
        ),
    )


def test_empty_job_stop_requires_native_seal_and_retirement_then_finalizes(native_sql):
    (_, director, planner, scorer), fixture = native_sql
    with scorer.begin() as connection, pytest.raises(DBAPIError):
        connection.execute(
            text("SELECT lab.finish_stopped_children(:stop,:inv)"),
            dict(stop=fixture["stop"], inv=fixture["owner"].invocation_id),
        )
    assert _record(scorer, fixture, "register") == "registered"
    assert _record(scorer, fixture, "register") == "registered"
    with pytest.raises(DBAPIError):
        _close(planner, fixture)
    assert _record(scorer, fixture, "seal") == "drained"
    assert _record(scorer, fixture, "seal") == "drained"
    with pytest.raises(DBAPIError):
        _close(planner, fixture)
    retired = _retirement(fixture)
    assert _record(director, fixture, "retire", retired) == "retired"
    assert _record(director, fixture, "retire", retired) == "retired"
    assert _close(planner, fixture) and _close(planner, fixture)
    _documents(director, fixture)
    seal_run_task_plan(planner, run_id=fixture["run"])
    service = IndependentScorer(scorer, harness_sha256="b" * 64, artifact_root=fixture["tmp_path"])
    result = service.finalize_if_ready(
        run_id=fixture["run"],
        admitted_generation=1,
        execution_sha256=fixture["owner"].execution_sha256,
    )
    assert result is not None
    report, report_sha = result
    assert report["status"] == "stopped" and report["score_count"] == 0
    assert report["task_plan_count"] == 3 and len(report["task_terminal_outcomes"]) == 3
    assert report["model_usage"] == dict(input_tokens=0, output_tokens=0, calls=0)
    with director.connect() as connection:
        state, stored = connection.execute(
            text("SELECT state,report_sha256 FROM lab.runs WHERE run_id=:run"), fixture
        ).one()
        assert (state, stored) == ("stopped", report_sha)
        assert (
            connection.execute(
                text("SELECT count(*) FROM scorer.score_jobs WHERE run_id=:run"), fixture
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM lab.director_stop_job_drains WHERE recovery_id=:stop"),
                fixture,
            ).scalar_one()
            == 0
        )


@pytest.mark.parametrize(
    "change",
    [
        "owner",
        "generation",
        "plan",
        "worker",
        "request",
        "native_live",
        "native_null_cgroup",
        "native_nonempty",
    ],
)
def test_empty_proof_rejects_wrong_context_actor_and_native_evidence(native_sql, change):
    (_, _, _, scorer), fixture = native_sql
    assert _record(scorer, fixture, "register") == "registered"
    changed = deepcopy(fixture["evidence"])
    if change == "owner":
        changed["context"]["owner_json"]["worker_pid"] += 1
    elif change == "generation":
        changed["context"]["expected_generation"] += 1
    elif change == "plan":
        changed["context"]["original_plan"] = []
    elif change == "worker":
        changed["worker_identity"]["worker_start_ticks"] += 1
    elif change == "request":
        changed["context"]["request_json"]["provider_config_sha256"] = "f" * 64
    elif change == "native_live":
        changed["observation"]["unit_properties"]["MainPID"] = "101"
    elif change == "native_null_cgroup":
        changed["observation"]["unit_properties"]["ControlGroup"] = None
    else:
        changed["observation"]["native_children_empty"] = False
    with pytest.raises(DBAPIError):
        _record(scorer, fixture, "seal", changed)


def test_empty_stop_rpc_role_and_append_only_authorities(native_sql):
    engines, fixture = native_sql
    migrator, director, planner, scorer = engines
    for engine in (migrator, director, planner):
        with pytest.raises(DBAPIError):
            _record(engine, fixture, "register")
    assert _record(scorer, fixture, "register") == "registered"
    for engine in engines:
        with engine.begin() as connection, pytest.raises(DBAPIError):
            connection.execute(
                text(
                    "UPDATE lab.director_empty_baseline_stop_evidence "
                    "SET evidence_json='{}'::jsonb "
                    "WHERE recovery_id=:stop"
                ),
                fixture,
            )
        with engine.begin() as connection, pytest.raises(DBAPIError):
            connection.execute(
                text(
                    "DELETE FROM lab.director_empty_baseline_stop_evidence WHERE recovery_id=:stop"
                ),
                fixture,
            )
    with scorer.begin() as connection, pytest.raises(DBAPIError):
        connection.execute(text("SELECT lab.lock_run_plan(:run)"), fixture)


def test_original_deadline_cannot_be_renewed_by_retry_or_new_stop(native_sql):
    (migrator, director, _, scorer), fixture = native_sql
    assert _record(scorer, fixture, "register") == "registered"
    # Disposable fixture fault injection only: no live/original recovery is selected.
    with migrator.begin() as connection:
        connection.execute(
            text(
                "UPDATE lab.director_stop_closures "
                "SET created_at=clock_timestamp()-interval '121 seconds' "
                "WHERE recovery_id=:stop"
            ),
            fixture,
        )
    for phase in ("register", "seal"):
        with pytest.raises(DBAPIError):
            _record(scorer, fixture, phase)
    with director.begin() as connection:
        assert (
            connection.execute(
                text("SELECT lab.begin_stopped_baseline_closure(:stop)"), fixture
            ).scalar_one()
            == 0
        )
    other = uuid4()
    _ensure_recovery_intent(
        director,
        recovery_id=other,
        run_id=fixture["run"],
        request_sha256=_request_sha256(fixture["run"], "stop_and_finalize"),
        observed_state="stop_requested",
        owner=OwnerGeneration(**fixture["evidence"]["context"]["owner_json"]),
    )
    with director.begin() as connection, pytest.raises(DBAPIError):
        connection.execute(
            text("SELECT lab.begin_stopped_baseline_closure(:stop)"), {"stop": other}
        )
    with scorer.connect() as connection:
        assert connection.execute(
            text(
                "SELECT phase FROM lab.director_empty_baseline_stop_evidence "
                "WHERE recovery_id=:stop"
            ),
            fixture,
        ).scalars().all() == ["register"]


def test_scorer_empty_proof_uses_same_lifecycle_lock_as_planner(native_sql):
    (_, _, planner, scorer), fixture = native_sql
    with planner.begin() as locked:
        locked.execute(text("SELECT lab.lock_run_plan(:run)"), fixture)
        with scorer.begin() as competing:
            competing.execute(text("SET LOCAL lock_timeout='100ms'"))
            with pytest.raises(DBAPIError) as error:
                competing.execute(
                    RPC,
                    dict(
                        stop=fixture["stop"],
                        phase="register",
                        evidence=json.dumps(fixture["evidence"]),
                    ),
                )
            assert getattr(error.value.orig, "sqlstate", None) == "55P03"
    assert _record(scorer, fixture, "register") == "registered"


def test_sealed_exact_replay_does_not_accept_changed_native_receipt(native_sql):
    (_, _, _, scorer), fixture = native_sql
    _record(scorer, fixture, "register")
    _record(scorer, fixture, "seal")
    changed = deepcopy(fixture["evidence"])
    changed["observation"]["observed_at"] = datetime.now(UTC).isoformat()
    with pytest.raises(DBAPIError):
        _record(scorer, fixture, "seal", changed)


def test_empty_context_does_not_depend_on_role_session_timezone(native_sql):
    (_, director, _, scorer), fixture = native_sql
    for engine, zone in ((scorer, "Europe/Istanbul"), (director, "America/Los_Angeles")):
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('TimeZone',:zone,true)"), {"zone": zone})
            assert (
                connection.execute(
                    text("SELECT lab.empty_baseline_stop_context(:stop)"), fixture
                ).scalar_one()
                == fixture["evidence"]["context"]
            )
