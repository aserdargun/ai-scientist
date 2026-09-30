"""Live role-separated PostgreSQL contract for synthetic CARE calibration storage.

This fixture writes synthetic receipts only. It does not load the Farm B archive,
run a baseline candidate, or claim measured calibration acceptance.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pandas as pd
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from harness.baselines import BASELINE_SEEDS, BASELINE_VERSIONS
from harness.contracts import FitContext
from lab.scorer.care_calibration import (
    CARE_BASELINE_ALGORITHMS,
    CARE_CELL_RESERVATION_SECONDS,
    CareBaselineCellReceipt,
    CareCalibrationTask,
    _canonical_json,
    _persist_frozen_care_calibration,
    bind_care_baseline_worker,
    build_care_calibration_manifest,
    finalize_care_calibration,
    persist_care_baseline_cell,
    provision_care_calibration,
    reserve_care_baseline_cell,
)
from lab.scorer.holdout import _frame_bytes, _semantics_digest


def _engines() -> dict[str, Engine]:
    raw = os.environ.get("LAB_HOLDOUT_TEST_DSN_DIR")
    if not raw:
        pytest.skip("set LAB_HOLDOUT_TEST_DSN_DIR to the disposable calibration database")
    root = Path(raw).resolve(strict=True)
    return {
        role: create_engine((root / f"{role}.dsn").read_text().strip(), pool_pre_ping=True)
        for role in ("migrator", "director", "planner", "scorer")
    }


def _fixture(calibration_id: str):
    source_manifest = hashlib.sha256(b"synthetic-care-source-manifest").hexdigest()
    source_archive = hashlib.sha256(b"synthetic-care-source-archive").hexdigest()
    dataset_id = f"synthetic-care-{calibration_id.replace('-', '')}"
    train = pd.DataFrame({"sensor_a": [0.0, 1.0, 2.0]})
    evaluation = pd.DataFrame({"sensor_a": [0.1, 1.2, 2.4]})
    contexts = tuple(FitContext(0, ("sensor_a",), (), 600, 60.0) for _ in range(15))
    tasks = []
    semantics_by_task: dict[str, dict[str, object]] = {}
    labels_by_task: dict[str, list[int]] = {}
    for index in range(15):
        task_id = f"synthetic-care-task-{index:02}"
        family = "PDM" if index < 6 else "NRM"
        task_key = hashlib.sha256(f"{source_manifest}:{task_id}".encode()).hexdigest()[:32]
        profile_sha = hashlib.sha256(f"profile:{index}".encode()).hexdigest()
        labels = [0, 1, 1] if family == "PDM" else [0, 0, 0]
        times = [0, 600, 1200]
        masks = [False, False, False]
        failures = [[1, 2]] if family == "PDM" else []
        semantics_sha = _semantics_digest(
            family=family, sampling_s=600, times=times, masks=masks, failures=failures
        )
        labels_sha = hashlib.sha256(_canonical_json(labels)).hexdigest()
        task = CareCalibrationTask(
            task_id=task_id,
            dataset_id=dataset_id,
            split_id="holdout",
            session_id=task_id,
            task_key=task_key,
            profile_sha256=profile_sha,
            family=family,
            train_sha256=hashlib.sha256(_frame_bytes(train, columns=("sensor_a",))).hexdigest(),
            evaluation_sha256=hashlib.sha256(
                _frame_bytes(evaluation, columns=("sensor_a",))
            ).hexdigest(),
            labels_sha256=labels_sha,
            semantics_sha256=semantics_sha,
            source_member_sha256=hashlib.sha256(f"member:{index}".encode()).hexdigest(),
            source_archive_sha256=source_archive,
            source_manifest_sha256=source_manifest,
            sliding_window=2,
            sampling_s=600,
            context=contexts[index],
            train=train.copy(deep=True),
            evaluation=evaluation.copy(deep=True),
        )
        tasks.append(task)
        semantics_by_task[task_id] = {
            "family": family,
            "times": times,
            "masks": masks,
            "failures": failures,
            "sha": semantics_sha,
        }
        labels_by_task[task_id] = labels
    manifest = build_care_calibration_manifest(
        calibration_id=calibration_id,
        suite_id="care-farm-b-measured",
        suite_version=1,
        suite_manifest_sha256=source_manifest,
        development_manifest_sha256=hashlib.sha256(b"synthetic-development").hexdigest(),
        epsilon=0.01,
        tasks=tasks,
        harness_sha256=hashlib.sha256(b"synthetic-harness").hexdigest(),
        image_sha256=hashlib.sha256(b"synthetic-image").hexdigest(),
        wall_limit_seconds=14_400,
    )
    return dataset_id, train, evaluation, tasks, semantics_by_task, labels_by_task, manifest


def _receipt(
    task: CareCalibrationTask, manifest, algorithm: str, seed: int
) -> CareBaselineCellReceipt:
    source_sha = manifest.baseline_source_sha256[algorithm]
    fit_context = asdict(task.context) | {"seed": seed}
    fit_context_sha = hashlib.sha256(_canonical_json(fit_context)).hexdigest()
    return CareBaselineCellReceipt(
        task_id=task.task_id,
        task_key=task.task_key,
        algorithm=algorithm,
        algorithm_version=BASELINE_VERSIONS[algorithm],
        seed=seed,
        baseline_source_sha256=source_sha,
        harness_sha256=manifest.harness_sha256,
        image_sha256=manifest.image_sha256,
        profile_sha256=task.profile_sha256,
        train_sha256=task.train_sha256,
        evaluation_sha256=task.evaluation_sha256,
        labels_sha256=task.labels_sha256,
        semantics_sha256=task.semantics_sha256,
        fit_context_sha256=fit_context_sha,
        fit_artifact_sha256=hashlib.sha256(
            f"fit:{task.task_id}:{algorithm}:{seed}".encode()
        ).hexdigest(),
        score_document_sha256=hashlib.sha256(
            f"score:{task.task_id}:{algorithm}:{seed}".encode()
        ).hexdigest(),
        policy={"threshold": 1.0, "release": 0.9, "dwell": 1},
        raw_task_score=0.1 + CARE_BASELINE_ALGORITHMS.index(algorithm) * 0.1 + seed * 0.01,
        auxiliary_metrics={"duty_fraction": 0.01},
        fit_seconds=0.2,
        score_seconds=0.1,
    )


def _receipt_rpc_payload(
    receipt: CareBaselineCellReceipt, calibration_id: str
) -> dict[str, object]:
    return {
        "baseline_source_sha256": receipt.baseline_source_sha256,
        "harness_sha256": receipt.harness_sha256,
        "image_sha256": receipt.image_sha256,
        "profile_sha256": receipt.profile_sha256,
        "train_sha256": receipt.train_sha256,
        "evaluation_sha256": receipt.evaluation_sha256,
        "labels_sha256": receipt.labels_sha256,
        "semantics_sha256": receipt.semantics_sha256,
        "fit_context_sha256": receipt.fit_context_sha256,
        "fit_artifact_sha256": receipt.fit_artifact_sha256,
        "score_document_sha256": receipt.score_document_sha256,
        "calibration_id": calibration_id,
        "policy_json": dict(receipt.policy),
        "raw_task_score": receipt.raw_task_score,
        "auxiliary_metrics_json": dict(receipt.auxiliary_metrics),
        "fit_seconds": receipt.fit_seconds,
        "score_seconds": receipt.score_seconds,
    }


def _claim_snapshot(engine: Engine, claim_id, calibration_id):
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT claim.state,claim.failure_code,claim.generation,"
                "claim.worker_invocation_id,control.reserved_wall_seconds,"
                "(SELECT count(*) FROM scorer.care_baseline_calibration_cells cell "
                "WHERE cell.calibration_id=claim.calibration_id) "
                "FROM scorer.care_baseline_calibration_claims claim "
                "JOIN scorer.care_baseline_calibration_control control USING(calibration_id) "
                "WHERE claim.claim_id=:claim AND claim.calibration_id=:calibration"
            ),
            {"claim": claim_id, "calibration": calibration_id},
        ).one()


def _assert_sqlstate(error: DBAPIError, expected: str, message_fragment: str | None = None) -> None:
    original = error.orig
    sqlstate = getattr(original, "sqlstate", getattr(original, "pgcode", None))
    assert sqlstate == expected, f"expected SQLSTATE {expected}, got {sqlstate}: {original}"
    if message_fragment is not None:
        assert message_fragment in str(original).lower()


def _synthetic_bound_receipt(engine, claim, task, manifest, algorithm, seed, ordinal):
    assert claim.claim_id is not None and claim.generation is not None
    unit = f"swapp-ai-scientist-scorer-{claim.claim_id.hex}.service"
    invocation = hashlib.sha256(f"synthetic-invocation:{ordinal}".encode()).hexdigest()[:32]
    bind_care_baseline_worker(
        engine,
        claim.claim_id,
        claim.generation,
        pid=50_000 + ordinal,
        start_ticks=str(1_000_000 + ordinal),
        boot_id="00000000-0000-4000-8000-000000000001",
        unit=unit,
        invocation_id=invocation,
        cgroup=f"/user.slice/swapp-ai-scientist-scorer.slice/{unit}",
    )
    return replace(
        _receipt(task, manifest, algorithm, seed),
        claim_id=claim.claim_id,
        claim_generation=claim.generation,
        worker_invocation_id=invocation,
    )


def _commit_synthetic_cell(engine, calibration_id, task, manifest, algorithm, seed, ordinal):
    claim = reserve_care_baseline_cell(
        engine,
        calibration_id,
        task_key=task.task_key,
        algorithm=algorithm,
        seed=seed,
    )
    assert claim.newly_created is True
    assert claim.state == "reserved"
    return _synthetic_bound_receipt(engine, claim, task, manifest, algorithm, seed, ordinal)


@pytest.mark.live
def test_scorer_private_calibration_receipts_freeze_and_registry_binding() -> None:
    engines = _engines()
    calibration_id = str(uuid4())
    dataset_id, _train, _evaluation, tasks, semantics_by_task, labels_by_task, manifest = _fixture(
        calibration_id
    )
    suite_id = f"synthetic.ordinary-{uuid4().hex[:8]}"
    try:
        with engines["migrator"].connect() as connection:
            database = connection.execute(text("SELECT current_database()")).scalar_one()
        assert database.startswith("swapp_lab_m0_calibration_031_")

        with engines["migrator"].begin() as connection:
            for task in tasks:
                semantics = semantics_by_task[task.task_id]
                connection.execute(
                    text(
                        "INSERT INTO scorer.dataset_profiles "
                        "(dataset_id,split_id,session_id,sample_count,sliding_window,profile_sha256,"
                        "task_family,visibility) VALUES "
                        "(:dataset,:split,:session,3,2,:profile,:family,'holdout')"
                    ),
                    {
                        "dataset": task.dataset_id,
                        "split": task.split_id,
                        "session": task.session_id,
                        "profile": task.profile_sha256,
                        "family": task.family,
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO scorer.dataset_task_semantics "
                        "(dataset_id,split_id,session_id,task_family,sampling_s,evaluation_times_json,"
                        "masked_samples_json,failure_windows_json,semantics_sha256) "
                        "VALUES (:dataset,:split,:session,:family,600,CAST(:times AS jsonb),"
                        "CAST(:masks AS jsonb),CAST(:failures AS jsonb),:sha)"
                    ),
                    {
                        "dataset": task.dataset_id,
                        "split": task.split_id,
                        "session": task.session_id,
                        "family": task.family,
                        "times": json.dumps(semantics["times"]),
                        "masks": json.dumps(semantics["masks"]),
                        "failures": json.dumps(semantics["failures"]),
                        "sha": task.semantics_sha256,
                    },
                )
                for sample_index, label in enumerate(labels_by_task[task.task_id]):
                    connection.execute(
                        text(
                            "INSERT INTO scorer.dataset_labels "
                            "(dataset_id,split_id,session_id,sample_index,is_anomaly) "
                            "VALUES (:dataset,:split,:session,:index,:label)"
                        ),
                        {
                            "dataset": task.dataset_id,
                            "split": task.split_id,
                            "session": task.session_id,
                            "index": sample_index,
                            "label": bool(label),
                        },
                    )

        original_deadline = provision_care_calibration(engines["migrator"], manifest, tasks)
        assert original_deadline is not None
        lock_connection = engines["scorer"].connect()
        lock_transaction = lock_connection.begin()
        lock_connection.execute(
            text("SELECT lab.lock_care_calibration_execution(:id)"), {"id": calibration_id}
        )
        claim_started = threading.Event()
        task_started = threading.Event()
        claim_finished = threading.Event()
        task_finished = threading.Event()
        raced_claims = []
        race_errors = []

        def reserve_first_claim() -> None:
            claim_started.set()
            try:
                raced_claims.append(
                    reserve_care_baseline_cell(
                        engines["scorer"],
                        calibration_id,
                        task_key=tasks[0].task_key,
                        algorithm=CARE_BASELINE_ALGORITHMS[0],
                        seed=0,
                    )
                )
            except BaseException as exc:
                race_errors.append(exc)
            finally:
                claim_finished.set()

        def race_extra_task() -> None:
            task_started.set()
            try:
                with engines["migrator"].begin() as connection:
                    connection.execute(
                        text(
                            "INSERT INTO scorer.care_baseline_calibration_tasks "
                            "SELECT calibration_id,'f' || substr(task_key,2),task_id || '-racing',"
                            "dataset_id,split_id,session_id,profile_sha256,family,task_weight,"
                            "train_sha256,evaluation_sha256,evaluation_rows,labels_sha256,"
                            "semantics_sha256,source_member_sha256,sliding_window,sampling_s,"
                            "context_json FROM scorer.care_baseline_calibration_tasks "
                            "WHERE calibration_id=:id LIMIT 1"
                        ),
                        {"id": calibration_id},
                    )
            except BaseException as exc:
                race_errors.append(exc)
            finally:
                task_finished.set()

        claim_thread = threading.Thread(target=reserve_first_claim, name="care-claim-race")
        task_thread = threading.Thread(target=race_extra_task, name="care-task-race")
        try:
            claim_thread.start()
            task_thread.start()
            assert claim_started.wait(timeout=2) and task_started.wait(timeout=2)
            time.sleep(0.1)
            assert not claim_finished.is_set() and not task_finished.is_set()
        finally:
            lock_transaction.commit()
            lock_connection.close()
            claim_thread.join(timeout=20)
            task_thread.join(timeout=20)
        assert not claim_thread.is_alive() and not task_thread.is_alive()
        assert len(raced_claims) == 1
        first_claim = raced_claims[0]
        assert first_claim.newly_created is True
        assert len(race_errors) == 1 and isinstance(race_errors[0], DBAPIError)
        _assert_sqlstate(race_errors[0], "P0001", "sealed")
        assert first_claim.claim_id is not None and first_claim.generation is not None
        first_unit = f"swapp-ai-scientist-scorer-{first_claim.claim_id.hex}.service"
        first_invocation = hashlib.sha256(b"synthetic-invocation:0").hexdigest()[:32]
        first_cgroup = f"/user.slice/swapp-ai-scientist-scorer.slice/{first_unit}"
        with pytest.raises(DBAPIError) as null_bind_generation:
            with engines["scorer"].begin() as connection:
                connection.execute(
                    text(
                        "SELECT lab.bind_care_baseline_worker(:claim,NULL,50000,'1000000',"
                        "'00000000-0000-4000-8000-000000000001',:unit,:invocation,:cgroup)"
                    ),
                    {
                        "claim": first_claim.claim_id,
                        "unit": first_unit,
                        "invocation": first_invocation,
                        "cgroup": first_cgroup,
                    },
                )
        _assert_sqlstate(null_bind_generation.value, "P0001", "identity is incomplete")
        assert _claim_snapshot(engines["scorer"], first_claim.claim_id, calibration_id) == (
            "reserved",
            None,
            1,
            None,
            CARE_CELL_RESERVATION_SECONDS,
            0,
        )
        first = _synthetic_bound_receipt(
            engines["scorer"],
            first_claim,
            tasks[0],
            manifest,
            CARE_BASELINE_ALGORITHMS[0],
            BASELINE_SEEDS[0],
            0,
        )
        assert first.claim_id is not None and first.claim_generation is not None
        assert first.worker_invocation_id is not None
        with pytest.raises(DBAPIError) as null_fail_generation:
            with engines["scorer"].begin() as connection:
                connection.execute(
                    text(
                        "SELECT lab.fail_care_baseline_cell(:claim,NULL,'worker_crash',:invocation)"
                    ),
                    {"claim": first.claim_id, "invocation": first.worker_invocation_id},
                )
        _assert_sqlstate(null_fail_generation.value, "P0001", "failure code is not allowed")
        assert _claim_snapshot(engines["scorer"], first.claim_id, calibration_id) == (
            "running",
            None,
            1,
            first.worker_invocation_id,
            CARE_CELL_RESERVATION_SECONDS,
            0,
        )
        with pytest.raises(DBAPIError) as null_complete_generation:
            with engines["scorer"].begin() as connection:
                connection.execute(
                    text(
                        "SELECT lab.complete_care_baseline_cell(:claim,NULL,:invocation,"
                        "CAST(:receipt AS jsonb))"
                    ),
                    {
                        "claim": first.claim_id,
                        "invocation": first.worker_invocation_id,
                        "receipt": _canonical_json(
                            _receipt_rpc_payload(first, calibration_id)
                        ).decode("ascii"),
                    },
                )
        _assert_sqlstate(null_complete_generation.value, "P0001", "identity is incomplete")
        assert _claim_snapshot(engines["scorer"], first.claim_id, calibration_id) == (
            "running",
            None,
            1,
            first.worker_invocation_id,
            CARE_CELL_RESERVATION_SECONDS,
            0,
        )
        persist_care_baseline_cell(engines["scorer"], calibration_id, first)
        persist_care_baseline_cell(engines["scorer"], calibration_id, first)
        assert _claim_snapshot(engines["scorer"], first.claim_id, calibration_id) == (
            "succeeded",
            None,
            1,
            first.worker_invocation_id,
            CARE_CELL_RESERVATION_SECONDS,
            1,
        )

        with pytest.raises(DBAPIError) as receipt_conflict:
            persist_care_baseline_cell(
                engines["scorer"],
                calibration_id,
                replace(
                    first,
                    score_document_sha256=hashlib.sha256(
                        b"conflicting synthetic receipt"
                    ).hexdigest(),
                ),
            )
        _assert_sqlstate(receipt_conflict.value, "P0001", "conflicts")

        ordinal = 1
        for task in tasks:
            for algorithm in CARE_BASELINE_ALGORITHMS:
                for seed in BASELINE_SEEDS:
                    if (
                        task.task_id == tasks[0].task_id
                        and algorithm == CARE_BASELINE_ALGORITHMS[0]
                        and seed == 0
                    ):
                        continue
                    receipt = _commit_synthetic_cell(
                        engines["scorer"],
                        calibration_id,
                        task,
                        manifest,
                        algorithm,
                        seed,
                        ordinal,
                    )
                    persist_care_baseline_cell(engines["scorer"], calibration_id, receipt)
                    ordinal += 1
        with engines["scorer"].begin() as connection:
            stored_count = connection.execute(
                text(
                    "SELECT count(*) FROM scorer.care_baseline_calibration_cells "
                    "WHERE calibration_id=:calibration"
                ),
                {"calibration": calibration_id},
            ).scalar_one()
        assert stored_count == 135

        # The exact 15 input rows seal as soon as any claim is admitted. A
        # Migrator task insertion after the first claim must fail closed.
        with pytest.raises(DBAPIError) as task_sealed:
            with engines["migrator"].begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO scorer.care_baseline_calibration_tasks "
                        "SELECT calibration_id,'f' || substr(task_key,2),task_id || '-racing',"
                        "dataset_id,split_id,session_id,profile_sha256,family,task_weight,"
                        "train_sha256,evaluation_sha256,evaluation_rows,labels_sha256,"
                        "semantics_sha256,source_member_sha256,sliding_window,sampling_s,"
                        "context_json FROM scorer.care_baseline_calibration_tasks "
                        "WHERE calibration_id=:calibration LIMIT 1"
                    ),
                    {"calibration": calibration_id},
                )
        _assert_sqlstate(task_sealed.value, "P0001", "sealed")
        frozen = finalize_care_calibration(engines["scorer"], manifest, tasks)
        assert frozen.cell_count == 135 and frozen.task_count == 15
        # Exact replay is checked here; the separately source-bound 0022 PG
        # evidence covers the historical postdeadline freeze replay boundary.
        assert finalize_care_calibration(engines["scorer"], manifest, tasks) == frozen
        with pytest.raises(DBAPIError) as freeze_conflict:
            _persist_frozen_care_calibration(
                engines["scorer"],
                replace(
                    frozen,
                    calibration_sha256=hashlib.sha256(b"conflicting synthetic freeze").hexdigest(),
                ),
            )
        _assert_sqlstate(freeze_conflict.value, "P0001", "freeze conflicts")

        # The exact receipt replay after the cell deadline is exercised by a
        # separate one-claim job below, without fitting a model.
        late_id = str(uuid4())
        late_manifest = build_care_calibration_manifest(
            calibration_id=late_id,
            suite_id=manifest.suite_id,
            suite_version=2,
            suite_manifest_sha256=manifest.suite_manifest_sha256,
            development_manifest_sha256=manifest.development_manifest_sha256,
            epsilon=manifest.epsilon,
            tasks=tasks,
            harness_sha256=manifest.harness_sha256,
            image_sha256=manifest.image_sha256,
            wall_limit_seconds=120,
        )
        provision_care_calibration(engines["migrator"], late_manifest, tasks)
        late_claim = reserve_care_baseline_cell(
            engines["scorer"],
            late_id,
            task_key=tasks[0].task_key,
            algorithm=CARE_BASELINE_ALGORITHMS[0],
            seed=0,
        )
        assert late_claim.newly_created is True
        assert late_claim.claim_id is not None and late_claim.generation is not None
        assert late_claim.reserved_seconds == CARE_CELL_RESERVATION_SECONDS
        late_retry = reserve_care_baseline_cell(
            engines["scorer"],
            late_id,
            task_key=tasks[0].task_key,
            algorithm=CARE_BASELINE_ALGORITHMS[0],
            seed=0,
        )
        assert late_retry.newly_created is False
        assert late_retry.claim_id == late_claim.claim_id
        with engines["scorer"].connect() as connection:
            charged = connection.execute(
                text(
                    "SELECT reserved_wall_seconds FROM "
                    "scorer.care_baseline_calibration_control WHERE calibration_id=:id"
                ),
                {"id": late_id},
            ).scalar_one()
        assert charged == CARE_CELL_RESERVATION_SECONDS
        unit = f"swapp-ai-scientist-scorer-{late_claim.claim_id.hex}.service"
        invocation = hashlib.sha256(b"late-synthetic-invocation").hexdigest()[:32]
        bind_care_baseline_worker(
            engines["scorer"],
            late_claim.claim_id,
            late_claim.generation,
            pid=60_001,
            start_ticks="2000001",
            boot_id="00000000-0000-4000-8000-000000000001",
            unit=unit,
            invocation_id=invocation,
            cgroup=f"/user.slice/swapp-ai-scientist-scorer.slice/{unit}",
        )
        late_receipt = replace(
            _receipt(tasks[0], late_manifest, CARE_BASELINE_ALGORITHMS[0], 0),
            claim_id=late_claim.claim_id,
            claim_generation=late_claim.generation,
            worker_invocation_id=invocation,
        )
        persist_care_baseline_cell(engines["scorer"], late_id, late_receipt)
        assert late_claim.deadline_at is not None
        remaining = max(0.0, (late_claim.deadline_at - datetime.now(UTC)).total_seconds())
        if remaining:
            time.sleep(remaining + 0.1)
        persist_care_baseline_cell(engines["scorer"], late_id, late_receipt)

        budget_id = str(uuid4())
        budget_manifest = build_care_calibration_manifest(
            calibration_id=budget_id,
            suite_id=manifest.suite_id,
            suite_version=3,
            suite_manifest_sha256=manifest.suite_manifest_sha256,
            development_manifest_sha256=manifest.development_manifest_sha256,
            epsilon=manifest.epsilon,
            tasks=tasks,
            harness_sha256=manifest.harness_sha256,
            image_sha256=manifest.image_sha256,
            wall_limit_seconds=120,
        )
        provision_care_calibration(engines["migrator"], budget_manifest, tasks)
        first_budget_claim = reserve_care_baseline_cell(
            engines["scorer"],
            budget_id,
            task_key=tasks[0].task_key,
            algorithm=CARE_BASELINE_ALGORITHMS[0],
            seed=0,
        )
        assert first_budget_claim.newly_created is True
        exhausted = reserve_care_baseline_cell(
            engines["scorer"],
            budget_id,
            task_key=tasks[0].task_key,
            algorithm=CARE_BASELINE_ALGORITHMS[0],
            seed=1,
        )
        assert exhausted.state == "incomplete"
        with engines["scorer"].connect() as connection:
            charged, claim_count = connection.execute(
                text(
                    "SELECT control.reserved_wall_seconds,count(claim.claim_id) "
                    "FROM scorer.care_baseline_calibration_control control "
                    "LEFT JOIN scorer.care_baseline_calibration_claims claim USING(calibration_id) "
                    "WHERE control.calibration_id=:id GROUP BY control.reserved_wall_seconds"
                ),
                {"id": budget_id},
            ).one()
        assert charged == CARE_CELL_RESERVATION_SECONDS and claim_count == 1

        # Frozen inputs and cells reject additions or altered duplicate rows.
        with pytest.raises(DBAPIError) as sealed_cell_error:
            with engines["scorer"].begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO scorer.care_baseline_calibration_cells "
                        "(calibration_id,task_key,algorithm,seed,claim_id,claim_generation,"
                        "baseline_source_sha256,harness_sha256,image_sha256,profile_sha256,"
                        "train_sha256,evaluation_sha256,"
                        "labels_sha256,semantics_sha256,fit_context_sha256,fit_artifact_sha256,"
                        "score_document_sha256,policy_json,raw_task_score,auxiliary_metrics_json,"
                        "fit_seconds,score_seconds,created_at) SELECT calibration_id,task_key,"
                        "algorithm,seed,NULL,NULL,baseline_source_sha256,harness_sha256,"
                        "image_sha256,profile_sha256,train_sha256,evaluation_sha256,labels_sha256,"
                        "semantics_sha256,fit_context_sha256,fit_artifact_sha256,repeat('0',64),"
                        "policy_json,raw_task_score,auxiliary_metrics_json,fit_seconds,score_seconds,"
                        "now() FROM "
                        "scorer.care_baseline_calibration_cells "
                        "WHERE calibration_id=:calibration LIMIT 1"
                    ),
                    {"calibration": calibration_id},
                )
        _assert_sqlstate(sealed_cell_error.value, "42501")

        # Director and Planner cannot inspect calibration tables or call the
        # Scorer-only lock function.
        for role in ("director", "planner"):
            with pytest.raises(DBAPIError) as table_error:
                with engines[role].begin() as connection:
                    connection.execute(
                        text(
                            "SELECT calibration_id FROM scorer.care_baseline_calibration_jobs "
                            "WHERE calibration_id=:calibration"
                        ),
                        {"calibration": calibration_id},
                    )
            _assert_sqlstate(table_error.value, "42501")
            with pytest.raises(DBAPIError) as function_error:
                with engines[role].begin() as connection:
                    connection.execute(
                        text("SELECT lab.lock_care_calibration_execution(:calibration)"),
                        {"calibration": calibration_id},
                    )
            _assert_sqlstate(function_error.value, "42501")

        # Ordinary suites remain registrable; the measured Farm B version must
        # bind to this completed immutable calibration.
        with engines["scorer"].begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO scorer.holdout_suite_versions "
                    "(suite_id,suite_version,manifest_sha256,development_manifest_sha256,"
                    "task_count,epsilon) VALUES (:suite,1,:manifest,:development,1,0.01)"
                ),
                {
                    "suite": suite_id,
                    "manifest": hashlib.sha256(b"ordinary-manifest").hexdigest(),
                    "development": hashlib.sha256(b"ordinary-development").hexdigest(),
                },
            )
            connection.execute(
                text(
                    "INSERT INTO scorer.holdout_suite_versions "
                    "(suite_id,suite_version,manifest_sha256,development_manifest_sha256,"
                    "task_count,epsilon,care_calibration_id) "
                    "VALUES (:suite,1,:manifest,:development,15,0.01,:calibration)"
                ),
                {
                    "suite": "care-farm-b-measured",
                    "manifest": manifest.suite_manifest_sha256,
                    "development": manifest.development_manifest_sha256,
                    "calibration": calibration_id,
                },
            )
            task = tasks[0]
            measured_summary = next(item for item in frozen.tasks if item.task_key == task.task_key)
            for registered_suite, base_score, reference_score in (
                (suite_id, 0.0, 1.0),
                (
                    "care-farm-b-measured",
                    measured_summary.base_score,
                    measured_summary.reference_score,
                ),
            ):
                connection.execute(
                    text(
                        "INSERT INTO scorer.holdout_suite_tasks "
                        "(suite_id,suite_version,task_key,dataset_id,split_id,session_id,"
                        "profile_sha256,family,task_weight,base_score,reference_score,"
                        "sliding_window,sampling_s,train_sha256,evaluation_sha256,"
                        "semantics_sha256,context_json) VALUES (:suite,1,:task_key,:dataset,"
                        ":split,:session,:profile,:family,:weight,:base,:reference,:window,"
                        ":sampling,:train,:evaluation,:semantics,CAST(:context AS jsonb))"
                    ),
                    {
                        "suite": registered_suite,
                        "task_key": task.task_key,
                        "dataset": task.dataset_id,
                        "split": task.split_id,
                        "session": task.session_id,
                        "profile": task.profile_sha256,
                        "family": task.family,
                        "weight": measured_summary.task_weight,
                        "base": base_score,
                        "reference": reference_score,
                        "window": task.sliding_window,
                        "sampling": task.sampling_s,
                        "train": task.train_sha256,
                        "evaluation": task.evaluation_sha256,
                        "semantics": task.semantics_sha256,
                        "context": _canonical_json(asdict(task.context)).decode("ascii"),
                    },
                )
        with engines["director"].connect() as connection:
            denied = connection.execute(
                text(
                    "SELECT has_table_privilege(current_user, "
                    "'scorer.care_baseline_calibration_jobs', 'SELECT')"
                )
            ).scalar_one()
        assert not denied
        # 0032 explicit public binding uses the existing immutable 135 receipts.
        # These are synthetic SQL contracts, never real Farm B measurement evidence.
        binding_sql = text(
            "INSERT INTO scorer.holdout_suite_versions "
            "(suite_id,suite_version,manifest_sha256,development_manifest_sha256,"
            "task_count,epsilon,care_calibration_id) "
            "VALUES ('public-ad-v1',:version,:manifest,:development,15,:epsilon,:calibration)"
        )
        binding = {
            "version": 3,
            "manifest": manifest.suite_manifest_sha256,
            "development": manifest.development_manifest_sha256,
            "epsilon": manifest.epsilon,
            "calibration": calibration_id,
        }
        for invalid in (
            {**binding, "development": "f" * 64},
            {**binding, "version": 2},
            {**binding, "calibration": late_id},
        ):
            with pytest.raises(DBAPIError) as refused:
                with engines["scorer"].begin() as connection:
                    connection.execute(binding_sql, invalid)
            _assert_sqlstate(refused.value, "P0001", "differs from its calibration")
        with engines["scorer"].begin() as connection:
            connection.execute(binding_sql, binding)
            connection.execute(
                text(
                    "INSERT INTO scorer.holdout_suite_tasks "
                    "(suite_id,suite_version,task_key,dataset_id,split_id,session_id,"
                    "profile_sha256,family,task_weight,base_score,reference_score,sliding_window,"
                    "sampling_s,train_sha256,evaluation_sha256,semantics_sha256,context_json) "
                    "SELECT 'public-ad-v1',3,t.task_key,t.dataset_id,t.split_id,t.session_id,"
                    "t.profile_sha256,t.family,t.task_weight,s.base_score,s.reference_score,"
                    "t.sliding_window,t.sampling_s,t.train_sha256,t.evaluation_sha256,"
                    "t.semantics_sha256,t.context_json "
                    "FROM scorer.care_baseline_calibration_tasks t "
                    "JOIN scorer.care_baseline_calibration_task_summaries s "
                    "USING(calibration_id,task_key) WHERE t.calibration_id=:calibration"
                ),
                {"calibration": calibration_id},
            )
        # A calibrated alias cannot substitute its measured numerical references.
        with pytest.raises(DBAPIError) as altered_reference:
            with engines["scorer"].begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO scorer.holdout_suite_tasks "
                        "(suite_id,suite_version,task_key,dataset_id,split_id,session_id,"
                        "profile_sha256,family,task_weight,base_score,reference_score,"
                        "sliding_window,sampling_s,train_sha256,evaluation_sha256,"
                        "semantics_sha256,context_json) "
                        "SELECT suite_id,suite_version,task_key,dataset_id,split_id,session_id,"
                        "profile_sha256,family,task_weight,base_score+0.001,reference_score,"
                        "sliding_window,sampling_s,train_sha256,evaluation_sha256,"
                        "semantics_sha256,context_json FROM scorer.holdout_suite_tasks "
                        "WHERE suite_id='public-ad-v1' AND suite_version=3 LIMIT 1"
                    )
                )
        _assert_sqlstate(
            altered_reference.value, "P0001", "differs from its calibrated private input"
        )
        with engines["scorer"].connect() as connection:
            alias_count = connection.execute(
                text(
                    "SELECT count(*) FROM scorer.holdout_suite_tasks "
                    "WHERE suite_id='public-ad-v1' AND suite_version=3"
                )
            ).scalar_one()
            current_deadline = connection.execute(
                text(
                    "SELECT deadline_at FROM scorer.care_baseline_calibration_jobs "
                    "WHERE calibration_id=:calibration"
                ),
                {"calibration": calibration_id},
            ).scalar_one()
            current_cells = connection.execute(
                text(
                    "SELECT count(*) FROM scorer.care_baseline_calibration_cells "
                    "WHERE calibration_id=:calibration"
                ),
                {"calibration": calibration_id},
            ).scalar_one()
        assert alias_count == 15 and current_cells == 135 and current_deadline == original_deadline
        assert finalize_care_calibration(engines["scorer"], manifest, tasks) == frozen

        with engines["scorer"].connect() as connection:
            scorer_can_update_jobs = connection.execute(
                text(
                    "SELECT has_table_privilege(current_user, "
                    "'scorer.care_baseline_calibration_jobs', 'UPDATE')"
                )
            ).scalar_one()
        assert not scorer_can_update_jobs
    finally:
        for engine in engines.values():
            engine.dispose()
