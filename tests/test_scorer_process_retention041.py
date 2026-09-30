"""Fault isolation for per-cell process receipts; no Docker, services or database."""

import hashlib
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from lab.director import evaluation_recovery, executor
from lab.director.ownership import ExecutionOwner, owned_execution
from lab.scorer.supervisor import ScorerProcessResult


@dataclass(frozen=True)
class Context:
    seed: int = 0
    time_budget_s: float = 1.0


def _identity():
    return dict(
        candidate_sha256=hashlib.sha256(b"candidate").hexdigest(),
        dataset_id="synthetic",
        split_id="dev",
        session_id="one",
        profile_sha256="c" * 64,
        harness_sha256="d" * 64,
        task_family="EVT",
    )


def _engine(row, process_receipt=None):
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.return_value.mappings.return_value.one_or_none.return_value = row
    connection.execute.return_value.scalar_one.return_value = process_receipt
    return engine


@pytest.mark.parametrize("kind", ["baseline", "primary", "confirmation"])
def test_actual_process_job_and_invocation_survive_measurement_without_field_changes(
    monkeypatch, tmp_path, kind
):
    identity = _identity()
    job = uuid4()
    invocation = "f" * 32
    artifact = b"explicit synthetic candidate score artifact"
    artifact_sha = hashlib.sha256(artifact).hexdigest()
    row = {
        **identity,
        "candidate_output_sha256": artifact_sha,
        "sample_count": 2,
        "task_score": 0.5,
        "vus_pr": 0.5,
        "vus_roc": 0.6,
        "fa_per_day": None,
        "duty_fraction": None,
        "position_bias": None,
    }
    task = SimpleNamespace(
        task_id="task",
        dataset_id="synthetic",
        split_id="dev",
        session_id="one",
        profile_sha256="c" * 64,
        family="EVT",
        frames=lambda: (object(), object()),
        context=Context(),
        task_ids_for_guard=frozenset(),
        evaluation_instants=(),
    )
    guarded = SimpleNamespace(
        evaluation=SimpleNamespace(
            score_document=artifact, scores=[0.1, 0.2], fit_seconds=3.0, score_seconds=4.0
        ),
        hardcoding=SimpleNamespace(code="pass"),
        determinism=SimpleNamespace(code="pass"),
        causality=SimpleNamespace(code="pass"),
    )
    monkeypatch.setattr(executor, "verify_execution_identity", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        evaluation_recovery, "recover_evaluation_measurement", lambda *args, **kwargs: None
    )
    persisted = MagicMock()
    monkeypatch.setattr(evaluation_recovery, "persist_evaluation_evidence", persisted)
    monkeypatch.setattr(executor, "run_guarded_seed_evaluation", lambda *args, **kwargs: guarded)
    monkeypatch.setattr(executor, "enqueue_score_job", lambda *args, **kwargs: job)
    monkeypatch.setattr(
        executor,
        "run_scorer_process",
        lambda *args, **kwargs: ScorerProcessResult(
            job,
            f"swapp-ai-scientist-scorer-{job.hex}.service",
            0,
            {"state": "completed"},
            invocation_id=invocation,
            attempt=3,
        ),
    )
    monkeypatch.setattr(executor.time, "monotonic", lambda: 100.0)
    run = uuid4()
    process_receipt = {
        "schema": "completed-score-job-process-receipt.v1",
        "job_id": str(job),
        "run_id": str(run),
        "experiment_id": "experiment",
        "evaluation_kind": kind,
        "task_id": "task",
        "seed": 2,
        "attempt": 3,
        "worker_unit": f"swapp-ai-scientist-scorer-{job.hex}.service",
        "worker_invocation_id": invocation,
    }
    result = executor.evaluate_and_score_seed(
        _engine(row, process_receipt),
        object(),
        object(),
        run_id=run,
        experiment_id="experiment",
        evaluation_kind=kind,
        task=task,
        candidate_source=b"candidate",
        candidate_sha256=identity["candidate_sha256"],
        seed=2,
        harness_sha256=identity["harness_sha256"],
        image_sha256="e" * 64,
        remaining_seconds=600,
        lease=SimpleNamespace(require_run_active=lambda: None),
        artifact_root=tmp_path,
    )
    expected = {
        "task_id": "task",
        "experiment_id": "experiment",
        "evaluation_kind": kind,
        "seed": 2,
        **identity,
        "candidate_output_sha256": artifact_sha,
        "task_score": 0.5,
        "vus_pr": 0.5,
        "vus_roc": 0.6,
        "fa_per_day": None,
        "duty_fraction": None,
        "position_bias": None,
        "fit_seconds": 3.0,
        "score_seconds": 4.0,
        "guards": {"hardcoding": "pass", "determinism": "pass", "causality": "pass"},
        "systemd_unit": f"swapp-ai-scientist-scorer-{job.hex}.service",
        "scorer_process_exit_code": 0,
        "scorer_process_job_id": str(job),
        "scorer_process_invocation_id": invocation,
        "scorer_process_provenance": "actual_process",
        "scorer_process_attempt": 3,
    }
    assert result == expected
    assert persisted.call_args.kwargs["payload"]["candidate_output_sha256"] == artifact_sha


def test_recovered_score_is_marked_without_fabricating_process_exit_or_invocation(
    monkeypatch, tmp_path
):
    run = uuid4()
    job = uuid4()
    identity = _identity()
    owner = ExecutionOwner(run, 2, "a" * 32, "b" * 64)
    artifact_sha = "e" * 64
    guards = {"hardcoding": "pass", "determinism": "pass", "causality": "pass"}
    evidence = {
        "schema": "director-evaluation-evidence.v1",
        "run_id": str(run),
        "experiment_id": "experiment",
        "evaluation_kind": "primary",
        "task_id": "task",
        "seed": 2,
        **identity,
        "candidate_output_sha256": artifact_sha,
        "sample_count": 2,
        "fit_seconds": 3.0,
        "score_seconds": 4.0,
        "guards": guards,
        "admitted_generation": 1,
        "execution_sha256": "b" * 64,
    }
    row = {
        **identity,
        "candidate_output_sha256": artifact_sha,
        "sample_count": 2,
        "job_id": job,
        "job_state": "completed",
        "admitted_generation": 1,
        "execution_sha256": "b" * 64,
        "artifact_sha256": artifact_sha,
        "claim_unit": "scorer-historical.service",
        "task_score": 0.5,
        "vus_pr": 0.5,
        "vus_roc": 0.6,
        "fa_per_day": None,
        "duty_fraction": None,
        "position_bias": None,
    }
    lease = SimpleNamespace(
        read_checkpoint=lambda **kwargs: {
            "payload": evidence,
            "receipt": {"payload_sha256": "f" * 64},
        }
    )
    monkeypatch.setattr(
        "lab.director.resume.assert_historical_receipt", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "lab.scorer.jobs.read_candidate_artifact", lambda *args, **kwargs: b"fixture"
    )
    with owned_execution(owner):
        result = evaluation_recovery.recover_evaluation_measurement(
            _engine(row),
            lease,
            run_id=run,
            experiment_id="experiment",
            kind="primary",
            task_id="task",
            seed=2,
            expected=identity,
            artifact_root=tmp_path,
        )
    expected = {
        "task_id": "task",
        "experiment_id": "experiment",
        "evaluation_kind": "primary",
        "seed": 2,
        **identity,
        "candidate_output_sha256": artifact_sha,
        "task_score": 0.5,
        "vus_pr": 0.5,
        "vus_roc": 0.6,
        "fa_per_day": None,
        "duty_fraction": None,
        "position_bias": None,
        "fit_seconds": 3.0,
        "score_seconds": 4.0,
        "guards": guards,
        "systemd_unit": "scorer-historical.service",
        "scorer_process_exit_code": None,
        "scorer_process_job_id": str(job),
        "scorer_process_invocation_id": None,
        "scorer_process_provenance": "recovered",
        "recovered_from_evidence_sha256": "f" * 64,
    }
    assert result == expected
    assert result["scorer_process_provenance"] != "actual_process"
    assert result["scorer_process_exit_code"] != 0


def _process_identity_case():
    job, run = uuid4(), uuid4()
    invocation = "f" * 32
    unit = f"swapp-ai-scientist-scorer-{job.hex}.service"
    result = ScorerProcessResult(
        job, unit, 0, {"state": "completed"}, invocation_id=invocation, attempt=3
    )
    receipt = {
        "schema": "completed-score-job-process-receipt.v1",
        "job_id": str(job),
        "run_id": str(run),
        "experiment_id": "experiment",
        "evaluation_kind": "primary",
        "task_id": "task",
        "seed": 2,
        "attempt": 3,
        "worker_unit": unit,
        "worker_invocation_id": invocation,
    }
    arguments = dict(
        job_id=job,
        run_id=run,
        experiment_id="experiment",
        evaluation_kind="primary",
        task_id="task",
        seed=2,
    )
    return result, receipt, arguments


@pytest.mark.parametrize(
    "field,value",
    [
        ("job_id", "wrong-job"),
        ("run_id", "wrong-run"),
        ("task_id", "wrong-task"),
        ("worker_unit", "wrong.service"),
        ("worker_invocation_id", "a" * 32),
        ("attempt", 4),
        ("attempt", True),
    ],
)
def test_committed_identity_mismatch_never_becomes_actual_process(field, value):
    result, receipt, arguments = _process_identity_case()
    receipt[field] = value
    with pytest.raises(RuntimeError):
        executor._scorer_process_provenance(_engine({}, receipt), result, **arguments)


@pytest.mark.parametrize(
    "field,value",
    [
        ("job_id", None),
        ("unit", "wrong.service"),
        ("invocation_id", "a" * 32),
        ("attempt", 4),
    ],
)
def test_actual_waiter_wrong_identity_rejected(field, value):
    from dataclasses import replace

    result, receipt, arguments = _process_identity_case()
    changed = replace(result, **{field: value if value is not None else uuid4()})
    with pytest.raises(RuntimeError):
        executor._scorer_process_provenance(_engine({}, receipt), changed, **arguments)


@pytest.mark.parametrize("field", ["invocation_id", "attempt"])
def test_legacy_unknown_waiter_is_not_filled_from_committed_score(field):
    from dataclasses import replace

    result, receipt, arguments = _process_identity_case()
    result = replace(result, **{field: None})
    engine = _engine({}, receipt)
    assert executor._scorer_process_provenance(engine, result, **arguments) == {
        "scorer_process_provenance": "unknown",
        "scorer_process_attempt": None,
    }
    engine.connect.assert_not_called()


def test_new_rpc_is_provenance_only_scoped_and_reversible():
    import ast
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "lab/db/migrations/versions/0035_scorer_process_provenance.py"
    )
    if not path.exists():
        path = (
            Path(__file__).resolve().parents[2]
            / "lab/db/migrations/versions/0035_scorer_process_provenance.py"
        )
    module = ast.parse(path.read_text())

    def statements(name):
        function = next(
            node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == name
        )
        return [
            ast.literal_eval(node.value.args[0])
            for node in function.body
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        ]

    upgrade = "\n".join(statements("upgrade"))
    downgrade = "\n".join(statements("downgrade"))
    assert upgrade.count("CREATE FUNCTION") == 1 and "CREATE OR REPLACE" not in upgrade
    assert "session_user <> 'swapp_lab_director'" in upgrade
    assert "SECURITY DEFINER SET search_path=pg_catalog,lab,scorer" in upgrade
    assert "OWNER TO swapp_lab_migrator" in upgrade and "FROM PUBLIC" in upgrade
    assert "IN ('baseline','primary','confirmation')" in upgrade
    assert "p.visibility='dev'" in upgrade and "c.completion_kind='scored'" in upgrade
    assert "s.worker_invocation_id" in upgrade and "j.attempt" in upgrade
    assert "j.claimed_by IS NULL" in upgrade and "j.lease_until IS NULL" in upgrade
    import re

    assert re.search(r"s\.score\b", upgrade) is None
    assert "SELECT *" not in upgrade and "GRANT SELECT" not in upgrade
    assert "UPDATE " not in upgrade and "INSERT " not in upgrade and "ALTER TABLE" not in upgrade
    assert "exit_code" not in upgrade and "exit_status" not in upgrade
    assert downgrade == "DROP FUNCTION lab.completed_score_job_process_receipt(uuid)"


@pytest.mark.parametrize(
    "field,value", [("job_id", None), ("unit", "wrong.service"), ("exit_code", 1)]
)
def test_legacy_unknown_cannot_bypass_requested_job_unit_or_exit_validation(field, value):
    from dataclasses import replace

    result, receipt, arguments = _process_identity_case()
    result = replace(
        result, invocation_id=None, attempt=None, **{field: value if value is not None else uuid4()}
    )
    engine = _engine({}, receipt)
    with pytest.raises(RuntimeError):
        executor._scorer_process_provenance(engine, result, **arguments)
    engine.connect.assert_not_called()
