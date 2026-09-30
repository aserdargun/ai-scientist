"""Pure contract tests for private CARE Farm B baseline calibration."""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import numpy as np
import pandas as pd
import pytest

from harness.baselines import BASELINE_SEEDS, BASELINE_VERSIONS
from harness.contracts import AlarmPolicy, FitContext
from lab.director.baselines import baseline_candidate_source
from lab.scorer.care_calibration import (
    CARE_BASELINE_ALGORITHMS,
    CARE_CELL_COUNT,
    CARE_CELL_RESERVATION_SECONDS,
    CARE_TASK_COUNT,
    CARE_TASK_WEIGHT,
    CareBaselineCellClaim,
    CareBaselineCellReceipt,
    CareCalibrationManifest,
    CareCalibrationTask,
    build_care_calibration_manifest,
    freeze_care_baseline_calibration,
    score_care_baseline_cell,
)
from lab.scorer.holdout import _frame_bytes, _semantics_digest


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _tasks() -> tuple[CareCalibrationTask, ...]:
    source_manifest = _sha(b"farm-b-source-manifest")
    source_archive = _sha(b"farm-b-source-archive")
    train = pd.DataFrame({"sensor_a": [0.0, 1.0, 2.0], "sensor_b": [1.0, 3.0, 4.0]})
    evaluation = pd.DataFrame({"sensor_a": [0.1, 1.2, 4.0], "sensor_b": [0.9, 3.1, 6.0]})
    context = FitContext(
        seed=0,
        signals=("sensor_a", "sensor_b"),
        regime_signals=(),
        sampling_s=600,
        time_budget_s=60.0,
    )
    train_sha = _sha(_frame_bytes(train, columns=context.signals))
    evaluation_sha = _sha(_frame_bytes(evaluation, columns=context.signals))
    tasks = []
    for ordinal in range(CARE_TASK_COUNT):
        task_id = f"farm-b-{ordinal:02}"
        family = "PDM" if ordinal < 6 else "NRM"
        task_key = hashlib.sha256(f"{source_manifest}:{task_id}".encode()).hexdigest()[:32]
        tasks.append(
            CareCalibrationTask(
                task_id=task_id,
                dataset_id="care-farm-b-v6",
                split_id="holdout",
                session_id=task_id,
                task_key=task_key,
                profile_sha256=_sha(f"profile:{task_id}".encode()),
                family=family,
                train_sha256=train_sha,
                evaluation_sha256=evaluation_sha,
                labels_sha256=_sha(f"labels:{task_id}".encode()),
                semantics_sha256=_sha(f"semantics:{task_id}".encode()),
                source_member_sha256=_sha(f"member:{task_id}".encode()),
                source_archive_sha256=source_archive,
                source_manifest_sha256=source_manifest,
                sliding_window=20,
                sampling_s=600,
                context=context,
                train=train.copy(deep=True),
                evaluation=evaluation.copy(deep=True),
            )
        )
    return tuple(tasks)


def _manifest(tasks: tuple[CareCalibrationTask, ...]):
    return build_care_calibration_manifest(
        calibration_id="00000000-0000-4000-8000-000000000031",
        suite_id="care-farm-b-measured",
        suite_version=1,
        suite_manifest_sha256=tasks[0].source_manifest_sha256,
        development_manifest_sha256=_sha(b"development-manifest"),
        epsilon=0.01,
        tasks=tasks,
        harness_sha256=_sha(b"harness-identity"),
        image_sha256=_sha(b"pinned-image-identity"),
    )


def _receipts(
    tasks: tuple[CareCalibrationTask, ...], manifest: CareCalibrationManifest
) -> list[CareBaselineCellReceipt]:
    rows = []
    for task in tasks:
        for algorithm_index, algorithm in enumerate(CARE_BASELINE_ALGORITHMS):
            for seed in BASELINE_SEEDS:
                context = FitContext(
                    seed=seed,
                    signals=task.context.signals,
                    regime_signals=task.context.regime_signals,
                    sampling_s=task.context.sampling_s,
                    time_budget_s=task.context.time_budget_s,
                )
                raw = 0.1 + (algorithm_index * 0.1) + seed * 0.01
                rows.append(
                    CareBaselineCellReceipt(
                        task_id=task.task_id,
                        task_key=task.task_key,
                        algorithm=algorithm,
                        algorithm_version=BASELINE_VERSIONS[algorithm],
                        seed=seed,
                        baseline_source_sha256=manifest.baseline_source_sha256[algorithm],
                        harness_sha256=manifest.harness_sha256,
                        image_sha256=manifest.image_sha256,
                        profile_sha256=task.profile_sha256,
                        train_sha256=task.train_sha256,
                        evaluation_sha256=task.evaluation_sha256,
                        labels_sha256=task.labels_sha256,
                        semantics_sha256=task.semantics_sha256,
                        fit_context_sha256=_sha(
                            json.dumps(
                                {
                                    "seed": context.seed,
                                    "signals": context.signals,
                                    "regime_signals": context.regime_signals,
                                    "sampling_s": context.sampling_s,
                                    "time_budget_s": context.time_budget_s,
                                },
                                sort_keys=True,
                                separators=(",", ":"),
                                ensure_ascii=True,
                                allow_nan=False,
                            ).encode("ascii")
                        ),
                        fit_artifact_sha256=_sha(f"fit:{task.task_id}:{algorithm}:{seed}".encode()),
                        score_document_sha256=_sha(
                            f"score:{task.task_id}:{algorithm}:{seed}".encode()
                        ),
                        policy={"threshold": 1.0, "release": 0.9, "dwell": 1},
                        raw_task_score=raw,
                        auxiliary_metrics={"duty_fraction": 0.01},
                        fit_seconds=0.2,
                        score_seconds=0.1,
                    )
                )
    return rows


def test_complete_grid_freezes_measured_refs_with_equal_farm_b_weights() -> None:
    tasks = _tasks()
    manifest = _manifest(tasks)
    frozen = freeze_care_baseline_calibration(manifest, tasks, _receipts(tasks, manifest))

    assert frozen.task_count == CARE_TASK_COUNT
    assert frozen.cell_count == CARE_CELL_COUNT == 135
    assert {task.task_weight for task in frozen.tasks} == {CARE_TASK_WEIGHT}
    assert all(task.base_score == pytest.approx(0.11) for task in frozen.tasks)
    assert all(task.reference_score == pytest.approx(0.31) for task in frozen.tasks)
    assert (
        frozen.calibration_sha256
        == freeze_care_baseline_calibration(
            manifest, tuple(reversed(tasks)), list(reversed(_receipts(tasks, manifest)))
        ).calibration_sha256
    )


def test_freeze_hash_roundtrips_bound_claim_uuid_receipts() -> None:
    from lab.scorer.care_calibration import _canonical_json

    tasks = _tasks()
    manifest = _manifest(tasks)
    bound_receipts = [
        replace(
            receipt,
            claim_id=UUID(int=index + 1),
            claim_generation=1,
            worker_invocation_id=_sha(f"worker:{index}".encode())[:32],
        )
        for index, receipt in enumerate(_receipts(tasks, manifest))
    ]
    frozen = freeze_care_baseline_calibration(manifest, tasks, bound_receipts)
    encoded_receipts = _canonical_json([receipt.canonical() for receipt in frozen.receipts])
    decoded_receipts = json.loads(encoded_receipts)
    bound_by_cell = {
        (receipt.task_id, receipt.algorithm, receipt.seed): receipt for receipt in bound_receipts
    }
    first_row = decoded_receipts[0]
    assert first_row["claim_id"] == str(
        bound_by_cell[
            (first_row["task_id"], first_row["algorithm"], first_row["seed"])
        ].claim_id
    )

    restored_receipts = [
        CareBaselineCellReceipt(**{**row, "claim_id": UUID(row["claim_id"])})
        for row in decoded_receipts
    ]
    restored = freeze_care_baseline_calibration(manifest, tasks, restored_receipts)
    assert restored.calibration_sha256 == frozen.calibration_sha256
    assert (
        _canonical_json([receipt.canonical() for receipt in restored.receipts])
        == encoded_receipts
    )

    original = bound_receipts[0]
    changed_identities = (
        replace(original, claim_id=UUID(int=10_001)),
        replace(original, claim_generation=2),
        replace(original, worker_invocation_id=_sha(b"different-worker")[:32]),
    )
    for changed in changed_identities:
        changed_receipts = [changed, *bound_receipts[1:]]
        assert (
            freeze_care_baseline_calibration(manifest, tasks, changed_receipts).calibration_sha256
            != frozen.calibration_sha256
        )


def test_incomplete_and_duplicate_grids_cannot_be_frozen() -> None:
    tasks = _tasks()
    manifest = _manifest(tasks)
    receipts = _receipts(tasks, manifest)
    with pytest.raises(ValueError, match="incomplete"):
        freeze_care_baseline_calibration(manifest, tasks, receipts[:-1])
    with pytest.raises(ValueError, match="duplicate"):
        freeze_care_baseline_calibration(manifest, tasks, [*receipts, receipts[0]])


def test_receipt_source_or_runtime_identity_must_match_manifest() -> None:
    tasks = _tasks()
    manifest = _manifest(tasks)
    receipts = _receipts(tasks, manifest)
    receipts[0] = CareBaselineCellReceipt(
        **{**receipts[0].canonical(), "baseline_source_sha256": _sha(b"foreign source")}
    )
    with pytest.raises(ValueError, match="identity or value"):
        freeze_care_baseline_calibration(manifest, tasks, receipts)


def test_manifest_binds_exact_farm_b_source_and_grid() -> None:
    tasks = _tasks()
    manifest = _manifest(tasks)
    assert set(manifest.baseline_source_sha256) == set(CARE_BASELINE_ALGORITHMS)
    assert manifest.baseline_source_sha256["robust_z"] == _sha(
        baseline_candidate_source("robust_z")
    )
    assert manifest.task_weight_policy == "farm-b-equal-task-weight-1-over-15.v1"
    assert len(manifest.tasks) == CARE_TASK_COUNT


def test_manifest_rejects_changed_task_family_composition() -> None:
    tasks = list(_tasks())
    original = tasks[5]
    tasks[5] = replace(original, family="NRM")
    with pytest.raises(ValueError, match="six PDM and nine NRM"):
        _manifest(tuple(tasks))


def test_measured_registry_requires_a_frozen_calibration_binding() -> None:
    from lab.scorer.holdout import register_holdout_suite

    with pytest.raises(ValueError, match="requires a frozen baseline calibration"):
        register_holdout_suite(
            object(),
            suite_id="care-farm-b-measured",
            suite_version=1,
            manifest_sha256="a" * 64,
            development_manifest_sha256="b" * 64,
            epsilon=0.0,
            tasks=(),
        )
    with pytest.raises(ValueError, match="only the measured Farm B suite"):
        register_holdout_suite(
            object(),
            suite_id="fixture.synthetic",
            suite_version=1,
            manifest_sha256="a" * 64,
            development_manifest_sha256="b" * 64,
            epsilon=0.0,
            care_calibration_id=UUID("00000000-0000-4000-8000-000000000022"),
            tasks=(),
        )


def test_durable_claim_parser_requires_exact_100_second_claim() -> None:
    from datetime import UTC, datetime

    from lab.scorer.care_calibration import _claim_from_row

    row = {
        "claim_id": UUID("00000000-0000-4000-8000-000000000031"),
        "generation": 1,
        "state": "reserved",
        "reserved_seconds": CARE_CELL_RESERVATION_SECONDS,
        "reserved_at": datetime.now(UTC),
        "deadline_at": datetime.now(UTC),
    }
    claim = _claim_from_row(row, newly_created=True)
    assert claim.state == "reserved"
    assert claim.newly_created is True
    assert claim.reserved_seconds == 100
    with pytest.raises(ValueError, match="invalid durable CARE claim"):
        _claim_from_row({**row, "reserved_seconds": True}, newly_created=False)
    with pytest.raises(ValueError, match="invalid durable CARE claim"):
        _claim_from_row({**row, "generation": True}, newly_created=False)


def test_supervisor_refuses_to_relaunch_existing_or_expired_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime, timedelta

    import lab.scorer.care_calibration_supervisor as supervisor

    calibration_id = UUID("00000000-0000-4000-8000-000000000031")
    claim_id = UUID("00000000-0000-4000-8000-000000000032")
    now = datetime.now(UTC)
    row = {
        "calibration_id": calibration_id,
        "generation": 1,
        "state": "running",
        "control_state": "running",
        "deadline_at": now + timedelta(seconds=80),
        "job_deadline": now + timedelta(seconds=900),
        "claim_seconds_left": 79,
        "job_seconds_left": 899,
    }
    monkeypatch.setattr(supervisor, "_claim_row", lambda *_args: row)
    monkeypatch.setattr(
        supervisor,
        "_ensure_aggregate_slice",
        lambda: pytest.fail("existing worker generation must not launch again"),
    )
    with pytest.raises(RuntimeError, match="only the exact newly reserved"):
        supervisor.run_care_baseline_cell_process(
            object(),
            calibration_id=calibration_id,
            claim_id=claim_id,
            generation=1,
            remaining_seconds=100,
        )

    row["state"] = "reserved"
    row["deadline_at"] = now - timedelta(seconds=1)
    row["claim_seconds_left"] = -1
    with pytest.raises(TimeoutError, match="deadline expired"):
        supervisor.run_care_baseline_cell_process(
            object(),
            calibration_id=calibration_id,
            claim_id=claim_id,
            generation=1,
            remaining_seconds=100,
        )


def test_recovery_leaves_mismatched_live_generation_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import nullcontext

    import lab.scorer.care_calibration_supervisor as supervisor

    calibration_id = UUID("00000000-0000-4000-8000-000000000031")
    claim_id = UUID("00000000-0000-4000-8000-000000000033")
    invocation_id = "a" * 32
    claim = CareBaselineCellClaim(
        claim_id,
        1,
        "running",
        100,
        None,
        None,
        False,
    )
    row = {
        "claim_id": claim_id,
        "calibration_id": calibration_id,
        "generation": 1,
        "state": "running",
        "worker_pid": 12345,
        "worker_start_ticks": "1",
        "worker_boot_id": "0" * 36,
        "worker_unit": f"swapp-ai-scientist-scorer-{claim_id.hex}.service",
        "worker_invocation_id": invocation_id,
        "worker_cgroup": f"/user.slice/{claim_id.hex}.service",
    }
    monkeypatch.setattr(supervisor, "_job_lifecycle_lock", lambda *_a, **_kw: nullcontext())
    monkeypatch.setattr(supervisor, "_claim_row", lambda *_args: row)
    monkeypatch.setattr(
        supervisor,
        "_systemctl_show",
        lambda _unit: {
            "LoadState": "loaded",
            "ActiveState": "active",
            "InvocationID": "b" * 32,
            "ControlGroup": row["worker_cgroup"],
            "MainPID": str(row["worker_pid"]),
        },
    )
    monkeypatch.setattr(supervisor, "_fail_drained_claim", lambda *_a, **_kw: pytest.fail())

    result = supervisor.reconcile_care_baseline_claim(object(), claim, claim_id, 1)
    assert result.state == "pending"


def test_missing_boot_identity_does_not_prove_recorded_pid_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lab.scorer.care_calibration_supervisor as supervisor

    monkeypatch.setattr(
        supervisor, "_boot_id", lambda: (_ for _ in ()).throw(OSError("unavailable"))
    )
    monkeypatch.setattr(
        supervisor,
        "_start_ticks",
        lambda _pid: pytest.fail("PID must not be checked without a validated boot id"),
    )
    assert supervisor._recorded_worker_is_gone(
        {"worker_boot_id": "0" * 36, "worker_pid": 12345, "worker_start_ticks": "1"}
    ) is False


def test_failed_worker_waits_for_exact_private_sandbox_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import nullcontext

    import lab.scorer.care_calibration_supervisor as supervisor

    calibration_id = UUID("00000000-0000-4000-8000-000000000031")
    claim_id = UUID("00000000-0000-4000-8000-000000000034")
    invocation_id = "a" * 32
    claim = CareBaselineCellClaim(
        claim_id,
        1,
        "running",
        100,
        None,
        None,
        False,
    )
    row = {
        "claim_id": claim_id,
        "calibration_id": calibration_id,
        "generation": 1,
        "state": "running",
        "worker_pid": 12345,
        "worker_start_ticks": "1",
        "worker_boot_id": "0" * 36,
        "worker_unit": f"swapp-ai-scientist-scorer-{claim_id.hex}.service",
        "worker_invocation_id": invocation_id,
        "worker_cgroup": f"/user.slice/{claim_id.hex}.service",
    }
    monkeypatch.setattr(supervisor, "_job_lifecycle_lock", lambda *_a, **_kw: nullcontext())
    monkeypatch.setattr(supervisor, "_claim_row", lambda *_args: row)
    monkeypatch.setattr(
        supervisor,
        "_systemctl_show",
        lambda _unit: {
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "InvocationID": invocation_id,
            "ControlGroup": row["worker_cgroup"],
            "MainPID": "0",
        },
    )
    monkeypatch.setattr(supervisor, "_owned_unit_cgroup", lambda *_args: row["worker_cgroup"])
    monkeypatch.setattr(supervisor, "_cgroup_is_empty", lambda _group: True)
    monkeypatch.setattr(supervisor, "_recorded_worker_is_gone", lambda _row: True)
    monkeypatch.setattr(supervisor, "_drain_exact_claim_sandbox", lambda _claim: False)
    monkeypatch.setattr(
        supervisor,
        "_fail_drained_claim",
        lambda *_a, **_kw: pytest.fail("failure CAS requires the private sandbox to drain"),
    )

    result = supervisor.reconcile_care_baseline_claim(object(), claim, claim_id, 1)
    assert result.state == "pending"


def test_worker_reports_capacity_busy_before_opening_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import lab.scorer.care_calibration_worker as worker
    import lab.scorer.worker as common_worker

    monkeypatch.setattr(
        common_worker,
        "verify_systemd_invocation",
        lambda unit: SimpleNamespace(
            unit=unit, invocation_id="a" * 32, control_group=f"/user.slice/{unit}"
        ),
    )
    monkeypatch.setattr(common_worker, "_try_process_admission_lock", lambda: None)
    monkeypatch.setattr(worker, "_acquire_process_admission_lock", lambda *_args: None)
    monkeypatch.setattr(
        common_worker,
        "_secret",
        lambda *_args: pytest.fail("busy Scorer must not read its DSN"),
    )

    result = worker.main(
        [
            "--calibration-id",
            "00000000-0000-4000-8000-000000000031",
            "--claim-id",
            "00000000-0000-4000-8000-000000000032",
            "--generation",
            "1",
            "--total-seconds",
            "100",
        ]
    )

    assert result == 75
    assert json.loads(capsys.readouterr().out) == {
        "calibration_id": "00000000-0000-4000-8000-000000000031",
        "claim_id": "00000000-0000-4000-8000-000000000032",
        "generation": 1,
        "state": "capacity_busy",
    }


def test_worker_waits_for_host_admission_only_inside_original_deadline() -> None:
    from lab.scorer.care_calibration_worker import _acquire_process_admission_lock

    now = [0.0]
    attempts = [0]

    def busy_then_available() -> int | None:
        attempts[0] += 1
        return 73 if attempts[0] == 3 else None

    def advance(seconds: float) -> None:
        now[0] += seconds

    acquired = _acquire_process_admission_lock(
        1.0, busy_then_available, monotonic=lambda: now[0], sleep=advance
    )
    assert acquired == 73
    assert attempts[0] == 3
    assert now[0] == pytest.approx(0.2)

    now[0] = 0.0
    attempts[0] = 0

    def stay_busy() -> None:
        attempts[0] += 1

    timed_out = _acquire_process_admission_lock(
        0.25,
        lambda: (stay_busy() or None),
        monotonic=lambda: now[0],
        sleep=advance,
    )
    assert timed_out is None
    assert attempts[0] == 3
    assert now[0] == pytest.approx(0.25)


def test_terminal_claim_waits_for_its_exact_worker_and_sandbox_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import nullcontext

    import lab.scorer.care_calibration_supervisor as supervisor

    calibration_id = UUID("00000000-0000-4000-8000-000000000031")
    claim_id = UUID("00000000-0000-4000-8000-000000000035")
    invocation_id = "a" * 32
    cgroup = f"/user.slice/{claim_id.hex}.service"
    claim = CareBaselineCellClaim(claim_id, 1, "succeeded", 100, None, None, False)
    row = {
        "claim_id": claim_id,
        "calibration_id": calibration_id,
        "generation": 1,
        "state": "succeeded",
        "worker_pid": 12345,
        "worker_start_ticks": "1",
        "worker_boot_id": "0" * 36,
        "worker_unit": f"swapp-ai-scientist-scorer-{claim_id.hex}.service",
        "worker_invocation_id": invocation_id,
        "worker_cgroup": cgroup,
    }
    unit_state = {
        "LoadState": "loaded",
        "ActiveState": "active",
        "InvocationID": invocation_id,
        "ControlGroup": cgroup,
        "MainPID": "12345",
    }
    monkeypatch.setattr(supervisor, "_job_lifecycle_lock", lambda *_a, **_kw: nullcontext())
    monkeypatch.setattr(supervisor, "_claim_row", lambda *_args: row)
    monkeypatch.setattr(supervisor, "_systemctl_show", lambda _unit: unit_state)
    monkeypatch.setattr(supervisor, "_boot_id", lambda: "0" * 36)
    monkeypatch.setattr(supervisor, "_start_ticks", lambda _pid: "1")
    monkeypatch.setattr(supervisor, "_stop_owned_scorer_unit_locked", lambda *_a, **_kw: False)
    monkeypatch.setattr(
        supervisor,
        "_drain_exact_claim_sandbox",
        lambda _claim: pytest.fail("sandbox cleanup cannot precede exact unit drain"),
    )

    result = supervisor.reconcile_care_baseline_claim(object(), claim, claim_id, 1)
    assert result.state == "pending"

    unit_state.update(ActiveState="inactive", MainPID="0")
    monkeypatch.setattr(supervisor, "_owned_unit_cgroup", lambda *_args: cgroup)
    monkeypatch.setattr(supervisor, "_cgroup_is_empty", lambda _group: True)
    monkeypatch.setattr(supervisor, "_recorded_worker_is_gone", lambda _row: True)
    monkeypatch.setattr(supervisor, "_drain_exact_claim_sandbox", lambda _claim: True)
    result = supervisor.reconcile_care_baseline_claim(object(), claim, claim_id, 1)
    assert result.state == "succeeded"


def test_worker_result_binds_calibration_identity() -> None:
    import json

    from lab.scorer.care_calibration_supervisor import _parse_worker_result

    calibration_id = UUID("00000000-0000-4000-8000-000000000031")
    claim_id = UUID("00000000-0000-4000-8000-000000000032")
    payload = {
        "calibration_id": str(calibration_id),
        "claim_id": str(claim_id),
        "generation": 1,
        "state": "succeeded",
    }
    assert _parse_worker_result(json.dumps(payload), calibration_id, claim_id, 1) == "succeeded"
    payload["calibration_id"] = str(UUID("00000000-0000-4000-8000-000000000099"))
    with pytest.raises(RuntimeError, match="identity"):
        _parse_worker_result(json.dumps(payload), calibration_id, claim_id, 1)


@pytest.mark.parametrize("family", ["PDM", "NRM"])
def test_cell_scorer_keeps_raw_semantics_for_real_family_metric(
    family: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lab.scorer.care_calibration as calibration
    from lab.sandbox.docker_runner import DEFAULT_SANDBOX_IMAGE

    train = pd.DataFrame({"sensor_a": [0.0, 1.0, 2.0, 3.0]})
    evaluation = pd.DataFrame({"sensor_a": [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]})
    context = FitContext(
        seed=0, signals=("sensor_a",), regime_signals=(), sampling_s=600, time_budget_s=60.0
    )
    labels = np.asarray([0, 0, 0, 1, 1, 0, 0, 0] if family == "PDM" else [0] * 8, dtype=np.int8)
    times = [index * 600 for index in range(8)]
    masks = [False] * 8
    failures = [[3, 4]] if family == "PDM" else []
    semantics = {
        "task_family": family,
        "sampling_s": 600,
        "evaluation_times_json": times,
        "masked_samples_json": masks,
        "failure_windows_json": failures,
    }
    semantics["semantics_sha256"] = _semantics_digest(
        family=family,
        sampling_s=600,
        times=times,
        masks=masks,
        failures=failures,
    )
    task_id = f"metric-{family.lower()}"
    task = CareCalibrationTask(
        task_id=task_id,
        dataset_id="care-farm-b-private",
        split_id="holdout",
        session_id=task_id,
        task_key=hashlib.sha256(task_id.encode()).hexdigest()[:32],
        profile_sha256=_sha(b"metric-profile"),
        family=family,
        train_sha256=_sha(_frame_bytes(train, columns=context.signals)),
        evaluation_sha256=_sha(_frame_bytes(evaluation, columns=context.signals)),
        labels_sha256=_sha(
            json.dumps(labels.astype(int).tolist(), separators=(",", ":")).encode("ascii")
        ),
        semantics_sha256=semantics["semantics_sha256"],
        source_member_sha256=_sha(b"metric-member"),
        source_archive_sha256=_sha(b"metric-archive"),
        source_manifest_sha256=_sha(b"metric-manifest"),
        sliding_window=2,
        sampling_s=600,
        context=context,
        train=train,
        evaluation=evaluation,
    )
    harness_sha = _sha(b"metric-harness")
    image_sha = DEFAULT_SANDBOX_IMAGE.rsplit("@sha256:", 1)[-1].removeprefix("sha256:")
    monkeypatch.setattr(
        calibration, "compute_harness_hash", lambda _root: SimpleNamespace(sha256=harness_sha)
    )
    # Exercise the production label reader: it checks the registered window
    # against the task row before returning the private label coverage.
    profile = {
        "sample_count": len(evaluation),
        "sliding_window": task.sliding_window,
        "visibility": "holdout",
        "profile_sha256": task.profile_sha256,
        "task_family": family,
    }

    def read_only_query(statement, _parameters):
        if "dataset_profiles" in str(statement):
            return SimpleNamespace(mappings=lambda: SimpleNamespace(one=lambda: profile))
        assert "dataset_labels" in str(statement)
        return SimpleNamespace(all=lambda: list(enumerate(labels)))

    trusted_engine = SimpleNamespace(
        connect=lambda: nullcontext(SimpleNamespace(execute=read_only_query))
    )
    monkeypatch.setattr(calibration, "_read_task_semantics", lambda *_args: dict(semantics))
    monkeypatch.setattr(
        calibration,
        "run_candidate_fit_score",
        lambda *_args, **_kwargs: SimpleNamespace(
            scores=(0.0,) * len(evaluation),
            policy=AlarmPolicy(threshold=1.0, release=0.9, dwell=1),
            fit_artifact_sha256=_sha(b"fit"),
            score_document=b"score document",
            fit_seconds=0.1,
            score_seconds=0.1,
        ),
    )

    receipt = score_care_baseline_cell(
        trusted_engine,
        SimpleNamespace(image=DEFAULT_SANDBOX_IMAGE),
        task=task,
        algorithm="robust_z",
        seed=0,
        harness_sha256=harness_sha,
        image_sha256=image_sha,
        remaining_seconds=90,
    )

    assert np.isfinite(receipt.raw_task_score)
    if family == "PDM":
        assert {"fa_per_day", "duty_fraction"} <= set(receipt.auxiliary_metrics)
    else:
        assert {"fa_per_day", "duty_fraction"} <= set(receipt.auxiliary_metrics)
