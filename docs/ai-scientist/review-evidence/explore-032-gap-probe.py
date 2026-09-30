"""Reproduce the current EXPLORE gate with in-memory, explicitly synthetic state.

No PostgreSQL, provider, sandbox, model, GPU or AOS process is started. This
invokes the production loop decision on an injected state; it does not execute
the preceding 25 experiments or prove durable restart or scientific outcomes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import runpy
from types import SimpleNamespace
from unittest.mock import patch

from lab.director.budget import BudgetSnapshot, RunBudget
import lab.director.loop as loop_module
from lab.director.loop import _strategy_checkpoint_fields
from lab.director.strategy import resolve_strategy_selection, select_strategy_move


ROOT = Path(__file__).resolve().parents[3]


def main() -> None:
    assert Path(loop_module.__file__).resolve() == ROOT / "lab/director/loop.py"
    source_paths = (
        "lab/director/loop.py",
        "lab/director/budget.py",
        "lab/director/strategy.py",
        "lab/llm/router.py",
        "tests/test_director_loop_strategy.py",
        "docs/ai-scientist/01-ai-scientist-spec.md",
        "docs/ai-scientist/02-luna-goal-brief-m0.md",
    )
    before = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in source_paths
    }
    helpers = runpy.run_path(str(ROOT / "tests/test_director_loop_strategy.py"))
    state = helpers["_state"]()
    strategy = state.load_strategy_state()
    for ordinal in range(1, 26):
        strategy, _selection = select_strategy_move(strategy, ordinal)
        strategy = resolve_strategy_selection(strategy, ordinal, "DISCARD")
    state = state.model_copy(
        update={
            **_strategy_checkpoint_fields(strategy),
            "completed_proposals": 25,
            "next_ordinal": 26,
            "consecutive_non_keep": 25,
            "discard_streak": 25,
        }
    ).verify_consistency()
    lease = helpers["MemoryLease"]()
    lease.heartbeat = lambda: None
    loop = helpers["_loop"](state, lease)
    loop.director_engine = None
    loop.proposal_limit = 35
    loop.budget = RunBudget(
        proposal_limit=35,
        wall_limit=900,
        token_limit=10000,
        snapshot=BudgetSnapshot(proposal_count=25),
    )
    receipt = {"payload_sha256": "a" * 64}
    loop._verify_execution_identity = lambda _calibration: None
    loop._load_or_initialize = lambda _calibration: (state, receipt)
    loop._pending_run_end_terminal_result = lambda *_args: None
    loop._recover_unresolved_holdout_budget = lambda: None
    loop._reconcile_periodic_holdout = lambda current, saved: (current, saved)
    loop._recover_committed_terminal = lambda *_args: None
    provider_calls = []

    def forbid_provider(*args, **kwargs):
        provider_calls.append(True)
        raise AssertionError("gap probe must not invoke a provider")

    loop.provider = SimpleNamespace(provider_id="fake-json", propose=forbid_provider)
    with patch.object(loop_module, "read_registered_calibration", return_value=object()):
        result = loop.run()
    assert result.status == "explore_family_unverified", result.status
    assert result.completed_proposals == 25 and result.next_ordinal == 26
    assert not provider_calls and lease.append_count == 0
    assert before == {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in source_paths
    }
    print(
        json.dumps(
            {
                "schema": "explore-current-gap-probe.v1",
                "scope": "Production loop decision with injected in-memory fixture state; calibration, storage, identity and holdout setup are mocked. No actual experiments, restart, providers, models, GPU, Docker, PostgreSQL or AOS execution.",
                "seeded_consecutive_non_keep": 25,
                "seeded_completed_proposals": 25,
                "proposal_limit": 35,
                "observed_status": result.status,
                "observed_next_ordinal": result.next_ordinal,
                "provider_calls": len(provider_calls),
                "new_checkpoints": lease.append_count,
                "source_sha256": before,
                "source_unchanged": True,
                "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "conclusion": "The EXPLORE gate remains an implementation gap. This successful reproduction is not acceptance success.",
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
