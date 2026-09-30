"""CPU-only proposal crash boundary: real registration/artifacts, inert SQL and seeds."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from lab.director import runner as module
from lab.director.artifacts import read_director_artifact, store_director_artifact
from lab.director.budget import RunBudget
from lab.director.fake_llm import AgentContext
from lab.director.journal import canonical_bytes
from lab.director.parameter_grid import ParameterGridProvider, grid_document
from lab.scorer import jobs


class Crash(BaseException):
    """Simulated process loss bypasses in-process exception reconciliation."""


class SeedBoundary(BaseException):
    """Stop before sandbox/task admission; this file cannot launch a worker."""


class MemoryLease:
    def __init__(self, root: Path):
        self.root = root
        self.rows: dict[str, dict[str, Any]] = {}
        self.crash_phase: str | None = "proposal_registered"

    def require_run_active(self) -> None:
        pass

    def read_checkpoint(self, *, key: str, artifact_root: Path) -> dict[str, Any] | None:
        receipt = self.rows.get(key)
        if receipt is None:
            return None
        raw = read_director_artifact(receipt["payload_sha256"], artifact_root=artifact_root)
        assert hashlib.sha256(raw).hexdigest() == receipt["payload_sha256"]
        return {"receipt": receipt, "payload": json.loads(raw)}

    def append_checkpoint(self, *, key: str, phase: str, payload: dict[str, Any],
                          artifact_root: Path, sequence: int) -> dict[str, Any]:
        assert key not in self.rows
        receipt = {"key": key, "phase": phase, "sequence": sequence,
                   "payload_sha256": store_director_artifact(
                       canonical_bytes(payload), artifact_root=artifact_root)}
        self.rows[key] = receipt
        if phase == self.crash_phase:
            raise Crash
        return receipt

    def replace_payload(self, key: str, payload: dict[str, Any]) -> None:
        # Fault injection represents a malformed host receipt, not an unhashed blob edit.
        self.rows[key]["payload_sha256"] = store_director_artifact(
            canonical_bytes(payload), artifact_root=self.root
        )


class SequenceEngine:
    def connect(self) -> SequenceEngine:
        return self

    def __enter__(self) -> SequenceEngine:
        return self

    def __exit__(self, *_: Any) -> None:
        pass

    def execute(self, *_: Any) -> SequenceEngine:
        return self

    def scalar_one(self) -> None:
        return None


def _fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    # Tiny tmpfs test blobs exercise the real hash store, without its host disk quota.
    monkeypatch.setattr(jobs, "MIN_FREE_DISK_BYTES", 0)
    clock = [100.0]
    calls: list[int] = []
    admissions: list[dict[str, Any]] = []
    lease = MemoryLease(tmp_path)
    document = grid_document("a" * 64, [{"method": "lsh", "seed": 0}])
    provider = ParameterGridProvider(
        document, configuration_sha256=hashlib.sha256(canonical_bytes(document)).hexdigest(),
        registry_entry_sha256="b" * 64,
    )
    original_propose = provider.propose

    def propose(context: AgentContext):
        calls.append(context.experiment_number)
        clock[0] += 2.0
        return original_propose(context)

    def seed(*_: Any, **__: Any):
        raise SeedBoundary

    monkeypatch.setattr(provider, "propose", propose)
    monkeypatch.setattr(module, "boottime", lambda: clock[0])
    monkeypatch.setattr(module, "calibration_sha256", lambda _: "c" * 64)
    monkeypatch.setattr(module, "register_experiment", lambda *_, **kw: admissions.append(kw))
    monkeypatch.setattr(module, "execute_candidate_seed", seed)
    budget = RunBudget(wall_limit=1000, token_limit=0, monotonic=lambda: clock[0])
    run_id = uuid4()
    context = AgentContext(
        phase="loop", experiment_number=1, system="S1", move_type="regime",
        task_cards=("Synthetic metadata only",), champion_source="def build_candidate(): pass",
        recent_feedback=(),
    )
    arguments = dict(
        director_engine=SequenceEngine(), planner_engine=object(), lease=lease, runner=object(),
        run_id=run_id, ordinal=1, parent_experiment_id="exp_" + "1" * 32,
        parent_tree_sha256="2" * 40, parent_source=b"def build_candidate(): pass\n",
        suite_id="synthetic.completion054", suite_version=1, calibration=object(),
        harness_sha256="d" * 64, image_sha256="e" * 64, system="S1", context=context,
        provider=provider, tasks=(), budget=budget, artifact_root=tmp_path, best_suite=0.0,
    )
    return SimpleNamespace(args=arguments, lease=lease, clock=clock, calls=calls,
                           admissions=admissions, budget=budget,
                           reservation_key=f"proposal-budget-reservation:{run_id}:1",
                           reconciled_key=f"proposal-budget-reconciled:{run_id}:1")


def _crash_and_restore(fixture: SimpleNamespace) -> None:
    with pytest.raises(Crash):
        module.run_one_proposal(**fixture.args)
    assert fixture.calls == [1]
    assert not fixture.admissions
    assert fixture.reconciled_key not in fixture.lease.rows
    snapshot = fixture.budget.snapshot()
    fixture.clock[0] += 30.0
    fixture.budget = RunBudget(wall_limit=1000, token_limit=0, snapshot=snapshot,
                              monotonic=lambda: fixture.clock[0])
    # Production resume observes SQL elapsed from the immutable run start separately.
    fixture.budget.observe_elapsed(32.0)
    fixture.args["budget"] = fixture.budget
    fixture.lease.crash_phase = None


def test_completed_proposal_replays_after_downtime_once(monkeypatch, tmp_path):
    fixture = _fixture(monkeypatch, tmp_path)
    _crash_and_restore(fixture)
    proposal_before = dict(fixture.lease.rows["proposal:1"])
    reservation_before = dict(fixture.lease.rows[fixture.reservation_key])
    with pytest.raises(SeedBoundary):
        module.run_one_proposal(**fixture.args)
    assert fixture.calls == [1]  # Zero provider calls after the interrupted process.
    assert fixture.budget.wall_seconds == 2.0
    assert fixture.budget.model_tokens == fixture.budget.reserved_model_tokens == 0
    assert fixture.budget.proposal_count == 1
    assert fixture.budget.reserved_wall_seconds == 0
    assert fixture.budget.snapshot().elapsed_wall_seconds == 32.0
    assert fixture.budget.remaining_wall_seconds == 968.0
    assert fixture.lease.rows["proposal:1"] == proposal_before
    assert fixture.lease.rows[fixture.reservation_key] == reservation_before
    assert len(fixture.admissions) == 1
    checkpoint_count = len(fixture.lease.rows)
    charged = fixture.budget.snapshot()
    fixture.clock[0] += 20.0
    fixture.args["budget"] = RunBudget(wall_limit=1000, token_limit=0, snapshot=charged,
                                      monotonic=lambda: fixture.clock[0])
    fixture.args["budget"].observe_elapsed(52.0)
    with pytest.raises(SeedBoundary):
        module.run_one_proposal(**fixture.args)
    assert fixture.calls == [1]
    assert fixture.args["budget"].wall_seconds == 2.0
    assert fixture.args["budget"].proposal_count == 1
    assert fixture.args["budget"].remaining_wall_seconds == 948.0
    assert len(fixture.lease.rows) == checkpoint_count
    assert fixture.admissions[0] == fixture.admissions[1]  # Same idempotent ledger identity.


@pytest.mark.parametrize("field,value", [
    ("reservation_id", "00000000-0000-0000-0000-000000000001"),
    ("run_id", "00000000-0000-0000-0000-000000000001"),
    ("ordinal", 2), ("boot_id", "different-original-boot"),
    ("wall_seconds", 11), ("candidate_sha256", "f" * 64),
    ("inputs_sha256", "f" * 64), ("provider_config_sha256", "f" * 64),
    ("completed_boottime", 9999.0), ("measured_wall_seconds", -1.0),
    ("measured_wall_seconds", True),
])
def test_invalid_completion_cannot_admit_or_reconcile(monkeypatch, tmp_path, field, value):
    fixture = _fixture(monkeypatch, tmp_path)
    _crash_and_restore(fixture)
    payload = fixture.lease.read_checkpoint(key="proposal:1", artifact_root=tmp_path)["payload"]
    payload["completion_receipt"][field] = value
    fixture.lease.replace_payload("proposal:1", payload)
    before = fixture.budget.snapshot()
    with pytest.raises(module.ProposalCompletionUnverified):
        module.run_one_proposal(**fixture.args)
    assert fixture.budget.snapshot() == before
    assert not fixture.admissions
    assert fixture.calls == [1]
    assert fixture.reconciled_key not in fixture.lease.rows


def test_missing_legacy_completion_is_explicitly_unsupported(monkeypatch, tmp_path):
    fixture = _fixture(monkeypatch, tmp_path)
    _crash_and_restore(fixture)
    payload = fixture.lease.read_checkpoint(key="proposal:1", artifact_root=tmp_path)["payload"]
    del payload["completion_receipt"]
    fixture.lease.replace_payload("proposal:1", payload)
    with pytest.raises(module.ProposalCompletionUnsupported):
        module.run_one_proposal(**fixture.args)
    assert fixture.calls == [1]
    assert not fixture.admissions
    assert fixture.reconciled_key not in fixture.lease.rows


def test_expired_reservation_without_proposal_never_calls_grid(monkeypatch, tmp_path):
    fixture = _fixture(monkeypatch, tmp_path)
    fixture.lease.crash_phase = "proposal_budget_reserved"
    with pytest.raises(Crash):
        module.run_one_proposal(**fixture.args)
    fixture.clock[0] += 30.0
    fixture.lease.crash_phase = None
    with pytest.raises(RuntimeError, match="no remaining provider time"):
        module.run_one_proposal(**fixture.args)
    assert fixture.calls == []
    assert not fixture.admissions
    assert "proposal:1" not in fixture.lease.rows
    assert fixture.budget.proposal_count == 1
    assert fixture.budget.wall_limit == 1000


def test_fresh_completion_overrun_rejected_before_experiment(monkeypatch, tmp_path):
    fixture = _fixture(monkeypatch, tmp_path)
    fixture.lease.crash_phase = None
    original = fixture.args["provider"].propose

    def too_slow(context):
        result = original(context)
        fixture.clock[0] += 10.0
        return result

    monkeypatch.setattr(fixture.args["provider"], "propose", too_slow)
    with pytest.raises(module.ProposalCompletionUnverified, match="timing_invalid"):
        module.run_one_proposal(**fixture.args)
    assert fixture.calls == [1]
    assert not fixture.admissions
    assert fixture.budget.wall_seconds == 12.0
    assert fixture.budget.reserved_wall_seconds == 0


def test_replay_uses_original_boot_completion_without_current_clock(monkeypatch, tmp_path):
    fixture = _fixture(monkeypatch, tmp_path)
    _crash_and_restore(fixture)
    # Simulates the new process's clock epoch; no host reboot or process work is performed.
    for key in (fixture.reservation_key, "proposal:1"):
        payload = fixture.lease.read_checkpoint(key=key, artifact_root=tmp_path)["payload"]
        if key == "proposal:1":
            payload["completion_receipt"]["boot_id"] = "original-boot"
        else:
            payload["boot_id"] = "original-boot"
        fixture.lease.replace_payload(key, payload)
    fixture.clock[0] = 1.0
    with pytest.raises(SeedBoundary):
        module.run_one_proposal(**fixture.args)
    assert fixture.budget.wall_seconds == 2.0
    assert fixture.calls == [1]


def test_future_completion_in_current_boot_is_rejected(monkeypatch, tmp_path):
    fixture = _fixture(monkeypatch, tmp_path)
    _crash_and_restore(fixture)
    # Consistent, in-budget interval is still impossible before completion on this boot.
    fixture.clock[0] = 101.0
    with pytest.raises(module.ProposalCompletionUnverified, match="timing_invalid"):
        module.run_one_proposal(**fixture.args)
    assert not fixture.admissions
    assert fixture.calls == [1]
    assert fixture.reconciled_key not in fixture.lease.rows


def test_valid_completion_never_bypasses_original_run_deadline(monkeypatch, tmp_path):
    real_seed = module.execute_candidate_seed
    fixture = _fixture(monkeypatch, tmp_path)
    _crash_and_restore(fixture)
    fixture.budget.observe_elapsed(1000.0)
    fixture.args["tasks"] = (SimpleNamespace(
        task_id="task", dataset_id="synthetic", split_id="dev", session_id="session",
    ),)
    monkeypatch.setattr(module, "execute_candidate_seed", real_seed)
    monkeypatch.setattr(module, "verify_execution_identity", lambda *_, **__: None)
    monkeypatch.setattr(module, "_read_completed_seed", lambda *_, **__: None)
    monkeypatch.setattr(module, "plan_run_tasks", lambda *_, **__: None)
    monkeypatch.setattr(module, "_advance_experiment_if_needed", lambda *_, **__: None)

    def no_measurement(*_: Any, **__: Any):
        pytest.fail("expired research budget reached sandbox/Scorer admission")

    monkeypatch.setattr(module, "evaluate_and_score_seed", no_measurement)
    with pytest.raises(RuntimeError, match="run_wall_budget_exhausted"):
        module.run_one_proposal(**fixture.args)
    assert fixture.calls == [1]
    assert len(fixture.admissions) == 1
    assert fixture.budget.wall_seconds == 2.0
    assert fixture.budget.wall_limit == 1000
    assert fixture.budget.remaining_wall_seconds == 0
    assert fixture.budget.proposal_count == 1
    assert not any(key.startswith("seed-reservation:") for key in fixture.lease.rows)


def test_loop_preserves_pending_intent_and_typed_completion_error(monkeypatch, tmp_path):
    from lab.director import holdout_replay
    from lab.director import loop as loop_module
    from tests.test_director_loop_strategy import MemoryLease as StrategyLease
    from tests.test_director_loop_strategy import _loop, _state

    state = _state()
    lease = StrategyLease()
    loop = _loop(state, lease)
    loop.artifact_root = tmp_path
    loop.director_engine = object()
    loop.proposal_limit = 1
    loop.budget = RunBudget(wall_limit=1000, token_limit=1000)
    monkeypatch.setattr(lease, "heartbeat", lambda: None, raising=False)
    monkeypatch.setattr(loop_module, "read_registered_calibration", lambda *_, **__: object())
    monkeypatch.setattr(holdout_replay, "recover_observed_terminal_holdouts", lambda *_, **__: None)
    monkeypatch.setattr(loop, "_verify_execution_identity", lambda _: None)
    monkeypatch.setattr(loop, "_load_or_initialize", lambda _: (state, {}))
    monkeypatch.setattr(loop, "_pending_run_end_terminal_result", lambda *_: None)
    monkeypatch.setattr(loop, "_recover_unresolved_holdout_budget", lambda: None)
    monkeypatch.setattr(loop, "_reconcile_periodic_holdout", lambda s, cp: (s, cp))
    monkeypatch.setattr(loop, "_recover_committed_terminal", lambda *_: None)
    monkeypatch.setattr(loop, "_task_cards", lambda *_: ("Synthetic metadata only",))
    monkeypatch.setattr(loop, "_champion_source", lambda _: b"def build_candidate(): pass")
    error = module.ProposalCompletionUnsupported("proposal_completion_missing: unsupported replay")

    def unsupported(**_: Any):
        raise error

    monkeypatch.setattr(loop, "_run_one", unsupported)
    with pytest.raises(module.ProposalCompletionUnsupported) as caught:
        loop.run()
    assert caught.value is error
    intent = lease.checkpoints["director-state:0:strategy-intent-1"]["payload"]
    failure = lease.checkpoints["director-state:0:provider-failure-1"]["payload"]
    assert failure["strategy_state_json"] == intent["strategy_state_json"]
    assert failure["next_ordinal"] == 1
    assert failure["completed_proposals"] == 0
