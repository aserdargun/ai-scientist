"""Inert stop document/provenance fixtures; no workers, database, or process inspection."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from lab.director import recovery
from lab.director.artifacts import store_director_artifact
from lab.director.contracts import (
    CandidateProposal,
    ExperimentDocument,
    InfrastructureStopReceipt,
    TrajectoryDocument,
)
from lab.director.fake_llm import AgentContext, ProposalTurn
from lab.director.journal import canonical_bytes
from lab.director.ledger import canonical_json_bytes, commit_experiment_record
from lab.director.stopped_proposal import _proposal_documents
from lab.reporting import render_run_report
from tests.test_stopped_baseline_closure048 import prepared


@pytest.fixture(autouse=True)
def fixture_disk_reserve(monkeypatch):
    monkeypatch.setattr("lab.scorer.jobs.MIN_FREE_DISK_BYTES", 0)


def documents_fixture(tmp_path):
    original = prepared(tmp_path)
    run = original["run_id"]
    context = AgentContext(
        phase="proposal",
        experiment_number=1,
        move_type="hparam",
        task_cards=("Synthetic fixture",),
        champion_source="pass",
        recent_feedback=(),
    )
    turn = ProposalTurn(
        proposal=CandidateProposal(
            hypothesis="Unexecuted original proposal",
            move_type="hparam",
            candidate_source="def build_candidate(): pass\n",
            predicted_delta=0.1,
        ),
        messages=("original provider transcript",),
        input_tokens=12,
        output_tokens=7,
    )
    source = store_director_artifact(
        turn.proposal.candidate_source.encode(), artifact_root=tmp_path
    )
    context_sha = store_director_artifact(
        canonical_bytes(context.model_dump(mode="json")), artifact_root=tmp_path
    )
    messages = store_director_artifact(
        canonical_bytes({"messages": list(turn.messages)}), artifact_root=tmp_path
    )
    exp = "exp_" + uuid4().hex
    payload = dict(
        schema="director-proposal-checkpoint.v1",
        run_id=str(run),
        experiment_id=exp,
        experiment_number=1,
        sequence=3,
        proposal=turn.proposal.model_dump(mode="json"),
        candidate_sha256=source,
        candidate_blob_sha256=source,
        inputs_sha256=context_sha,
        context_blob_sha256=context_sha,
        context_canonical_json=canonical_bytes(context.model_dump(mode="json")).decode(),
        messages_blob_sha256=messages,
        calibration_sha256="c" * 64,
        parent_experiment_id="exp_" + "a" * 32,
        parent_tree_sha256="b" * 40,
        system="S1",
        provider_receipt=None,
        deterministic_provider_id=None,
        provider_config_sha256=None,
        provider_registry_entry_sha256=None,
        input_tokens=12,
        output_tokens=7,
        **{
            k: original["execution"][k]
            for k in ("suite_id", "suite_version", "harness_sha256", "image_sha256")
        },
    )
    row = dict(
        kind="proposal",
        status="proposed",
        experiment_id=exp,
        experiment_number=1,
        sequence=3,
        candidate_sha256=source,
        candidate_blob_sha256=source,
        inputs_sha256=context_sha,
        calibration_sha256="c" * 64,
        parent_experiment_id=payload["parent_experiment_id"],
        proposal_json=payload["proposal"],
    )
    stop = InfrastructureStopReceipt(
        reason="stopped_before_candidate_admission",
        recovery_id=uuid4(),
        run_id=run,
        experiment_id=exp,
        generation=2,
        execution_sha256="e" * 64,
        proposal_sha256=hashlib.sha256(canonical_bytes(payload)).hexdigest(),
        reservation_sha256="d" * 64,
        reconciled_sha256="f" * 64,
        calibration_sha256="c" * 64,
    )
    return dict(
        run_id=run,
        row=row,
        payload=payload,
        turn=turn,
        execution={**original["execution"], "provider": "fake-json", "request": {}},
        stop=stop,
        suite_manifest=original["suite_manifest"],
        artifact_root=tmp_path,
    )


def test_abandoned_proposal_preserves_provenance_without_negative_verdict(tmp_path):
    args = documents_fixture(tmp_path)
    experiment, trajectory = _proposal_documents(**args)
    assert experiment.status == "abandoned"
    assert experiment.decision is trajectory.outcome is experiment.wall_seconds is None
    assert experiment.fit_seconds is experiment.score_seconds is experiment.suite_score is None
    assert experiment.per_task == ()
    assert (experiment.llm_input_tokens, experiment.llm_output_tokens) == (12, 7)
    assert trajectory.messages_blob_sha256 == args["payload"]["messages_blob_sha256"]
    assert trajectory.infrastructure_stop == experiment.infrastructure_stop == args["stop"]
    assert _proposal_documents(**args) == (experiment, trajectory)
    report = render_run_report(
        args["run_id"], (SimpleNamespace(sequence=3, document=experiment),), baseline_score=0.5
    )
    assert report.summary["reject_count"] == report.summary["discard_count"] == 0
    assert b"infrastructure abandonment" in report.html


def test_plain_proposal_cannot_claim_unknown_time_or_null_verdict(tmp_path):
    experiment, trajectory = _proposal_documents(**documents_fixture(tmp_path))
    for model, document in ((ExperimentDocument, experiment), (TrajectoryDocument, trajectory)):
        payload = json.loads(document.model_dump_json(by_alias=True))
        payload.pop("infrastructure_stop")
        with pytest.raises(ValueError):
            model.model_validate_json(json.dumps(payload), strict=True)


@pytest.mark.parametrize(
    "field,value", [("status", "scored"), ("wall_seconds", 0.0), ("suite_score", 0.1)]
)
def test_infrastructure_receipt_cannot_carry_scoring(tmp_path, field, value):
    experiment, _ = _proposal_documents(**documents_fixture(tmp_path))
    payload = json.loads(experiment.model_dump_json(by_alias=True))
    payload[field] = value
    with pytest.raises(ValueError):
        ExperimentDocument.model_validate_json(json.dumps(payload), strict=True)


def test_historical_canonical_documents_do_not_gain_optional_field(tmp_path):
    from lab.director.stop_closure import _baseline_documents

    pair = _baseline_documents(**prepared(tmp_path))
    for model, document in zip((ExperimentDocument, TrajectoryDocument), pair, strict=True):
        original = canonical_json_bytes(document)
        assert b"infrastructure_stop" not in original
        assert canonical_json_bytes(model.model_validate_json(original, strict=True)) == original


def test_pair_commit_rejects_different_stop_receipts_before_sql(tmp_path):
    experiment, trajectory = _proposal_documents(**documents_fixture(tmp_path))
    assert trajectory.infrastructure_stop is not None
    wrong = trajectory.model_copy(
        update={
            "infrastructure_stop": trajectory.infrastructure_stop.model_copy(
                update={"recovery_id": uuid4()}
            )
        }
    )
    engine = MagicMock()
    with pytest.raises(ValueError, match="registered identity"):
        commit_experiment_record(
            engine,
            experiment=experiment,
            trajectory=wrong,
            experiment_blob_sha256="a" * 64,
            trajectory_blob_sha256="b" * 64,
        )
    engine.begin.assert_not_called()


def test_current_generation_owner_never_reads_legacy_row():
    connection = MagicMock()
    current = dict(worker_invocation_id="c" * 32, generation=2)
    connection.execute.return_value.mappings.return_value.one_or_none.return_value = current
    assert recovery._current_owner_row(connection, uuid4()) is current
    assert connection.execute.call_count == 1
    assert "current_generation" in str(connection.execute.call_args.args[0])


def test_legacy_fallback_excludes_all_generation_contracts():
    connection = MagicMock()
    connection.execute.return_value.mappings.return_value.one_or_none.return_value = None
    assert recovery._current_owner_row(connection, uuid4()) is None
    query = str(connection.execute.call_args.args[0])
    assert query.count("NOT EXISTS") == 3
    assert "director_execution_contracts" in query


@pytest.mark.parametrize("invocation_matches", [True, False])
def test_ancestor_worker_requires_original_score_invocation(monkeypatch, invocation_matches):
    from contextlib import nullcontext
    from dataclasses import asdict

    from lab.scorer import stop_recovery
    from tests.test_stopped_baseline_closure048 import owner

    run, job, stop = uuid4(), uuid4(), uuid4()
    receipt = dict(
        run_id=run,
        remaining=119.0,
        expected_generation=2,
        execution_sha256="a" * 64,
        owner_json=asdict(owner()),
        proposal_closure=True,
    )
    original_job = dict(
        state="completed",
        admitted_generation=1,
        execution_sha256="a" * 64,
        claim_invocation_id="b" * 32,
    )
    calls = []
    connection = MagicMock()

    def execute(statement, params):
        sql = str(statement)
        calls.append(sql)
        result = MagicMock()
        if "SELECT *,extract" in sql:
            result.mappings.return_value.one.return_value = receipt
        elif "SELECT job_id" in sql:
            result.scalars.return_value.all.return_value = [job]
        elif "stopped_proposal_job_receipt" in sql:
            result.scalar_one.return_value = original_job
        elif "finish_stopped_proposal_children" in sql:
            result.scalar_one.return_value = "drained"
        else:
            raise AssertionError(sql)
        return result

    connection.execute.side_effect = execute
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value = connection
    engine.begin.return_value.__enter__.return_value = connection
    monkeypatch.setattr(stop_recovery, "prove_stopped_owner_dead", lambda *_: True)
    monkeypatch.setattr(stop_recovery, "_job_lifecycle_lock", lambda *_, **__: nullcontext())
    monkeypatch.setattr(stop_recovery, "_expected_unit_cgroup", lambda unit: "/exact/" + unit)
    monkeypatch.setattr(stop_recovery, "_cgroup_is_absent_or_empty", lambda _: True)
    monkeypatch.setattr(
        stop_recovery,
        "_systemctl_show",
        lambda unit: dict(
            LoadState="loaded",
            ActiveState="inactive",
            MainPID="0",
            ControlGroup="",
            InvocationID=("b" if invocation_matches else "c") * 32,
        ),
    )
    result = stop_recovery.reconcile_stopped_children(engine, stop, recovery_invocation="d" * 32)
    assert result == ("drained" if invocation_matches else "pending")
    assert any("finish_stopped_proposal_children" in sql for sql in calls) is invocation_matches
    assert not any("reconcile_stopped_score_job" in sql for sql in calls)


def local_documents_fixture(tmp_path, monkeypatch, *, fallback=False):
    """Build original local-provider receipts using an inert runtime fixture."""
    from lab.director.fake_llm import prompt_context_sha256
    from lab.director.local_llm import LocalQwenProposalProvider
    from tests.test_local_qwen_provider import _context, _FakeRuntime, _proposal_json, _provider

    monkeypatch.setattr("lab.director.local_llm.OwnedVllmRuntime", _FakeRuntime)
    monkeypatch.setattr(
        LocalQwenProposalProvider,
        "_count_prompt_variants",
        lambda _self, variants, _profile, **_kwargs: tuple(100 for _ in variants),
    )
    monkeypatch.setattr(_FakeRuntime, "fail_systems", set())
    monkeypatch.setattr(_FakeRuntime, "response_text", _proposal_json())
    monkeypatch.setattr(_FakeRuntime, "response_queue", [])
    monkeypatch.setattr(_FakeRuntime, "completion_queue", [])
    monkeypatch.setattr(_FakeRuntime, "truncate_first", fallback)
    context = _context(system="S2" if fallback else "S1")
    turn = _provider().propose(context)
    assert turn.provider_receipt is not None
    args = documents_fixture(tmp_path)
    source = store_director_artifact(
        turn.proposal.candidate_source.encode(), artifact_root=tmp_path
    )
    inputs = store_director_artifact(
        canonical_bytes(context.model_dump(mode="json")), artifact_root=tmp_path
    )
    assert inputs == prompt_context_sha256(context)
    transcript = store_director_artifact(
        canonical_bytes(
            {
                "messages": list(turn.messages),
                "provider_attempts": [
                    item.model_dump(mode="json") for item in turn.provider_attempts
                ],
            }
        ),
        artifact_root=tmp_path,
    )
    args["turn"] = turn
    args["row"].update(
        candidate_sha256=source,
        candidate_blob_sha256=source,
        inputs_sha256=inputs,
        proposal_json=turn.proposal.model_dump(mode="json"),
    )
    args["payload"].update(
        candidate_sha256=source,
        candidate_blob_sha256=source,
        inputs_sha256=inputs,
        messages_blob_sha256=transcript,
        proposal=turn.proposal.model_dump(mode="json"),
        provider_receipt=turn.provider_receipt.model_dump(mode="json"),
        input_tokens=turn.input_tokens,
        output_tokens=turn.output_tokens,
        system=context.system,
    )
    args["stop"] = args["stop"].model_copy(
        update={"proposal_sha256": hashlib.sha256(canonical_bytes(args["payload"])).hexdigest()}
    )
    args["execution"].update(
        provider="local-qwen",
        provider_config_sha256=turn.provider_receipt.provider_config_sha256,
        request={
            "provider_registry_entry_sha256": turn.provider_receipt.provider_registry_entry_sha256
        },
    )
    return args


def test_stopped_local_proposal_retains_recorded_s2_to_s1_fallback(tmp_path, monkeypatch):
    args = local_documents_fixture(tmp_path, monkeypatch, fallback=True)
    experiment, trajectory = _proposal_documents(**args)
    receipt = args["turn"].provider_receipt
    assert receipt.requested_system == experiment.system == "S2"
    assert receipt.actual_system == "S1"
    assert trajectory.provider_receipt == receipt.model_dump(mode="json")


@pytest.mark.parametrize("mutation", [None, "schema", "host_response", "earlier_attempt", "text"])
def test_local_stop_retains_exact_host_attempt_receipts(tmp_path, monkeypatch, mutation):
    args = local_documents_fixture(tmp_path, monkeypatch)
    turn = args["turn"]
    if mutation in {"schema", "text", "earlier_attempt"}:
        transcript = turn.provider_attempts[-1]
        field = "response_schema_sha256" if mutation == "schema" else "response_text"
        changed = transcript.model_copy(update={field: "f" * 64})
        attempts = (changed, transcript) if mutation == "earlier_attempt" else (changed,)
        args["turn"] = turn.model_copy(update={"provider_attempts": attempts})
    elif mutation == "host_response":
        receipt = turn.provider_receipt
        host = receipt.attempts[-1].model_copy(update={"response_sha256": "f" * 64})
        receipt = receipt.model_copy(update={"attempts": (host,)})
        args["turn"] = turn.model_copy(update={"provider_receipt": receipt})
        args["payload"]["provider_receipt"] = receipt.model_dump(mode="json")
        args["stop"] = args["stop"].model_copy(
            update={"proposal_sha256": hashlib.sha256(canonical_bytes(args["payload"])).hexdigest()}
        )
    if mutation is None:
        experiment, trajectory = _proposal_documents(**args)
        assert trajectory.provider_receipt == turn.provider_receipt.model_dump(mode="json")
        assert experiment.llm_input_tokens == turn.input_tokens
    else:
        with pytest.raises(ValueError, match="provenance|host receipt"):
            _proposal_documents(**args)
