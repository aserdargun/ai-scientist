"""Fresh actual run boundaries: Scorer source visibility and fenced failed closure."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_director_loop_strategy import _state

from lab.director.baselines import (
    BASELINE_NAMES,
    baseline_candidate_sha256,
    baseline_candidate_source,
    build_task_calibration,
    freeze_calibration_document,
)
from lab.director.budget import RunBudget
from lab.director.holdout import HoldoutReceipt
from lab.director.loop import DirectorLoop, DirectorLoopResult
from lab.director.ownership import ExecutionOwner, owned_execution
from lab.scorer.jobs import read_artifact_bytes, store_artifact_bytes


def test_initial_measured_baseline_source_is_available_to_independent_holdout(
    tmp_path, monkeypatch
):
    import lab.scorer.jobs as jobs

    root = tmp_path / "scorer-blobs"
    monkeypatch.setattr(
        jobs.shutil, "disk_usage", lambda _path: SimpleNamespace(free=100 * 1024**3)
    )
    monkeypatch.setattr(
        jobs,
        "store_artifact_bytes",
        lambda source: store_artifact_bytes(source, artifact_root=root),
    )
    task = build_task_calibration(
        task_id="evt-small",
        dataset_id="fixture",
        split_id="dev",
        session_id="session-1",
        profile_sha256="a" * 64,
        family="EVT",
        baseline_seed_scores={name: {0: 0.1, 1: 0.2, 2: 0.3} for name in BASELINE_NAMES},
        scorer_artifact_sha256={
            name: {0: "b" * 64, 1: "c" * 64, 2: "d" * 64} for name in BASELINE_NAMES
        },
        baseline_candidate_sha256={
            name: baseline_candidate_sha256(name) for name in BASELINE_NAMES
        },
        task_weight=1.0,
    )
    run_id = uuid4()
    calibration = freeze_calibration_document(
        run_id=run_id,
        suite_id="fixture-suite",
        suite_version=1,
        harness_sha256="e" * 64,
        image_sha256="f" * 64,
        expected_task_identities=(("fixture", "dev", "session-1", "evt-small"),),
        tasks=(task,),
        champion_experiment_id="exp_" + "0" * 32,
        champion_baseline_name="robust_z",
        champion_suite_scores={0: 0.0, 1: 0.0, 2: 0.0},
    )
    loop = object.__new__(DirectorLoop)
    loop.run_id = run_id
    loop.artifact_root = tmp_path / "director"
    loop.holdout_enabled = True
    loop.suite_manifest_sha256 = "a" * 64
    loop.proposal_limit = 1
    loop.budget = RunBudget(proposal_limit=1, wall_limit=120, token_limit=0)
    state = loop._initial_state(calibration)
    assert read_artifact_bytes(
        state.champion_source_sha256, artifact_root=root
    ) == baseline_candidate_source("robust_z")


@pytest.mark.parametrize("status,bit,closure", [("failed", None, True), ("passed", True, False)])
def test_fresh_run_end_failure_has_exact_owner_reconciled_budget(
    tmp_path, monkeypatch, status, bit, closure
):
    import lab.director.holdout as holdout

    state = _state()
    run_id = state.run_id
    owner = ExecutionOwner(
        run_id=run_id, generation=2, invocation_id="a" * 32, execution_sha256="b" * 64
    )
    receipt = HoldoutReceipt(
        reservation_id=uuid4(),
        state=status,
        bit=bit,
        run_id=run_id,
        candidate_experiment_id=state.champion_experiment_id,
        trigger_kind="run_end",
        trigger_index=1,
        admitted_generation=owner.generation,
        execution_sha256=owner.execution_sha256,
    )
    records = {}
    reconciled = []

    def read_checkpoint(**kwargs):
        return records.get(kwargs["key"])

    def append(closure=False, **kwargs):
        r = {**kwargs, "payload_sha256": "c" * 64}
        if closure:
            r["holdout_closure_owner"] = {
                "admitted_generation": owner.generation,
                "execution_sha256": owner.execution_sha256,
            }
        records[kwargs["key"]] = {"payload": kwargs["payload"], "receipt": r}
        if kwargs["phase"] == "holdout_budget_reconciled":
            reconciled.append(r)
        return r

    loop = object.__new__(DirectorLoop)
    loop.run_id = run_id
    loop.director_engine = object()
    loop.artifact_root = tmp_path
    loop.budget = RunBudget(proposal_limit=35, wall_limit=120, token_limit=0)
    loop.lease = SimpleNamespace(
        read_checkpoint=read_checkpoint,
        append_checkpoint=append,
        append_holdout_closure_checkpoint=lambda **kw: append(closure=True, **kw),
    )
    loop.restore_run_end_holdout = lambda result: None
    loop._run_end_reservation_checkpoint = lambda: None
    loop._next_checkpoint_sequence = lambda: len(records) + 1
    loop._state_key_for_digest = lambda digest: "state"
    records["state"] = {
        "payload": state.model_dump(mode="json", by_alias=True),
        "receipt": {"payload_sha256": "d" * 64},
    }
    loop._persist_state = lambda s, **kw: {"payload_sha256": "d" * 64}
    loop._result = lambda s, c, status: result

    def apply(result, receipt):
        assert bool(reconciled[-1].get("holdout_closure_owner")) is closure
        if closure:
            assert reconciled[-1]["holdout_closure_owner"] == {
                "admitted_generation": owner.generation,
                "execution_sha256": owner.execution_sha256,
            }
        assert loop.budget.snapshot().reservations == ()
        return result

    loop.apply_run_end_holdout = apply
    for name in [
        "read_run_end_unavailable",
        "read_run_end_admission_failure",
        "read_holdout_request",
    ]:
        monkeypatch.setattr(holdout, name, lambda *_a, **_k: None)
    monkeypatch.setattr(holdout, "check_at_run_end", lambda *_a, **_k: receipt)
    result = DirectorLoopResult(
        run_id=run_id,
        status="proposal_limit_reached",
        completed_proposals=0,
        next_ordinal=1,
        champion_experiment_id=state.champion_experiment_id,
        best_suite=0.0,
        checkpoint_sha256="d" * 64,
    )
    with owned_execution(owner):
        assert loop.check_run_end_holdout(result) is result
