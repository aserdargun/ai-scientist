from __future__ import annotations

import json

import pytest

from lab.director.strategy import (
    BetaPosterior,
    StrategyState,
    _beta_sample,
    _next_uint64,
    initial_strategy_state,
    parse_strategy_state_json,
    resolve_strategy_selection,
    select_strategy_move,
    strategy_state_json,
    strategy_state_sha256,
    verify_strategy_state_sha256,
)

MOVES = (
    "hparam",
    "preprocess",
    "features",
    "regime",
    "detector",
    "fusion",
    "alarm_policy",
    "simplify",
    "skill_reuse",
)


def test_initial_coverage_and_pending_retry_are_replayable() -> None:
    state = initial_strategy_state(31)
    initial_digest = strategy_state_sha256(state)
    selected: list[str] = []
    for ordinal in range(1, 10):
        state, intent = select_strategy_move(state, ordinal)
        selected.append(intent.move_type)
        encoded = strategy_state_json(state)
        restored = parse_strategy_state_json(encoded)
        retry_state, retry_intent = select_strategy_move(restored, ordinal)
        assert retry_state == restored
        assert retry_intent == intent
        assert strategy_state_json(retry_state) == encoded
        state = resolve_strategy_selection(state, ordinal, "KEEP" if ordinal % 2 else "DISCARD")
        state = parse_strategy_state_json(strategy_state_json(state))
    assert tuple(selected) == MOVES
    assert state.pending is None
    assert state.completed_experiments == 9
    assert strategy_state_sha256(initial_strategy_state(31)) == initial_digest


def test_same_seed_has_byte_identical_posterior_sampling_trace() -> None:
    def run(seed: int) -> bytes:
        state = initial_strategy_state(seed)
        trace: list[dict[str, object]] = []
        for ordinal in range(1, 45):
            state, intent = select_strategy_move(state, ordinal)
            trace.append(
                {
                    "ordinal": ordinal,
                    "move_type": intent.move_type,
                    "reason": intent.reason,
                    "sampled_scores": intent.sampled_scores,
                    "rng_state_after": intent.rng_state_after,
                }
            )
            outcome = "KEEP_SIMPLER" if ordinal % 5 == 0 else "CANDIDATE_FAILURE"
            state = resolve_strategy_selection(state, ordinal, outcome)
        return json.dumps(trace, sort_keys=True, separators=(",", ":")).encode()

    assert run(0) == run(0)
    assert run(0) != run(1)


def test_splitmix64_vector_and_beta_distribution_sanity() -> None:
    assert _next_uint64(0) == (0x9E3779B97F4A7C15, 0xE220A8397B1DCDAF)

    def draws(alpha: float, beta: float) -> list[float]:
        state = 0x123456789ABCDEF0
        result: list[float] = []
        for _ in range(2048):
            state, sample = _beta_sample(alpha, beta, state)
            result.append(sample)
        return result

    uniform = draws(1.0, 1.0)
    uniform_mean = sum(uniform) / len(uniform)
    uniform_variance = sum((sample - uniform_mean) ** 2 for sample in uniform) / len(uniform)
    assert abs(uniform_mean - 0.5) < 0.04
    assert 0.07 < uniform_variance < 0.10

    asymmetric = draws(5.0, 2.0)
    asymmetric_mean = sum(asymmetric) / len(asymmetric)
    assert abs(asymmetric_mean - (5.0 / 7.0)) < 0.03


def test_rolling_twenty_coverage_under_many_seeded_adaptive_runs() -> None:
    for seed in range(12):
        state = initial_strategy_state(seed)
        for ordinal in range(1, 161):
            state, intent = select_strategy_move(state, ordinal)
            assert intent.move_type in MOVES
            if ordinal >= 20:
                assert set(state.recent_selections[-20:]) == set(MOVES)
            if ordinal % 11 == 0:
                result = "ABANDONED"
            elif (ordinal + seed) % 3:
                result = "KEEP"
            else:
                result = "POLICY_REJECT"
            state = resolve_strategy_selection(state, ordinal, result)
            assert all(1.0 <= parameter.alpha <= 161.0 for parameter in state.posterior.values())
            assert all(1.0 <= parameter.beta <= 161.0 for parameter in state.posterior.values())


def test_oldest_due_move_is_selected_for_next_rolling_window() -> None:
    recent = (*MOVES, *("preprocess" for _ in range(11)))
    last_selected = {move: 0 for move in MOVES}
    for ordinal, move in enumerate(recent, start=1):
        last_selected[move] = ordinal
    state = StrategyState.model_validate(
        {
            "schema": "thompson-strategy.v1",
            "policy_version": "beta-thompson-coverage-v1",
            "rng_version": "splitmix64-marsaglia-tsang-box-muller-v1",
            "seed": 2,
            "rng_state": 2,
            "selection_count": 20,
            "completed_experiments": 20,
            "posterior": {move: {"alpha": 1.0, "beta": 1.0} for move in MOVES},
            "recent_selections": recent,
            "last_selected_ordinal": last_selected,
            "pending": None,
        },
        strict=True,
    )
    _, intent = select_strategy_move(state, 21)
    assert intent.reason == "rolling_coverage"
    assert intent.move_type == "hparam"
    assert intent.sampled_scores == {}


def test_abandonment_decays_without_reward_and_infrastructure_does_not_resolve() -> None:
    state = initial_strategy_state(4)
    state, first = select_strategy_move(state, 1)
    state = resolve_strategy_selection(state, 1, "KEEP")
    assert state.posterior[first.move_type] == BetaPosterior(alpha=2.0, beta=1.0)

    state, second = select_strategy_move(state, 2)
    before_infra_failure = strategy_state_json(state)
    with pytest.raises(ValueError, match="candidate, policy, or explicit abandonment"):
        resolve_strategy_selection(state, 2, "INFRASTRUCTURE_FAILURE")  # type: ignore[arg-type]
    assert strategy_state_json(state) == before_infra_failure
    replayed, replayed_intent = select_strategy_move(
        parse_strategy_state_json(before_infra_failure), 2
    )
    assert replayed_intent == second
    assert strategy_state_json(replayed) == before_infra_failure

    state = resolve_strategy_selection(state, 2, "ABANDONED")
    assert state.posterior[first.move_type].alpha == pytest.approx(1.0 + 0.97)
    assert state.posterior[second.move_type].beta == pytest.approx(1.0)
    assert state.completed_experiments == 2
    assert state.pending is None


def test_candidate_and_policy_failures_are_negative_but_infrastructure_is_not() -> None:
    state = initial_strategy_state(5)
    state, intent = select_strategy_move(state, 1)
    state = resolve_strategy_selection(state, 1, "CANDIDATE_FAILURE")
    assert state.posterior[intent.move_type].alpha == 1.0
    assert state.posterior[intent.move_type].beta == 2.0
    state, next_intent = select_strategy_move(state, 2)
    state = resolve_strategy_selection(state, 2, "CANDIDATE_REJECT")
    assert state.posterior[next_intent.move_type].beta >= 1.97


def test_keep_simpler_is_a_positive_posterior_reward() -> None:
    state = initial_strategy_state(54)
    state, intent = select_strategy_move(state, 1)
    state = resolve_strategy_selection(state, 1, "KEEP_SIMPLER")
    assert state.posterior[intent.move_type] == BetaPosterior(alpha=2.0, beta=1.0)
    assert state.completed_experiments == 1


def test_canonical_parser_rejects_duplicates_noncanonical_and_tampered_state() -> None:
    state = initial_strategy_state(7)
    canonical = strategy_state_json(state)
    with pytest.raises(ValueError, match="invalid"):
        parse_strategy_state_json(canonical.replace(b'"seed":7', b'"seed":7,"seed":7'))
    with pytest.raises(ValueError, match="canonical"):
        parse_strategy_state_json(canonical + b" ")
    payload = json.loads(canonical)
    payload["rng_state"] = True
    with pytest.raises(ValueError, match="invalid"):
        parse_strategy_state_json(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    payload = json.loads(canonical)
    payload["posterior"].pop("regime")
    with pytest.raises(ValueError, match="invalid"):
        parse_strategy_state_json(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    pending, _ = select_strategy_move(state, 1)
    pending_payload = json.loads(strategy_state_json(pending))
    pending_payload["pending"]["move_type"] = "simplify"
    with pytest.raises(ValueError, match="invalid"):
        parse_strategy_state_json(
            json.dumps(pending_payload, sort_keys=True, separators=(",", ":"))
        )


def test_persisted_rolling_window_cannot_omit_a_move_type() -> None:
    state = initial_strategy_state(21)
    for ordinal in range(1, 21):
        state, _ = select_strategy_move(state, ordinal)
        state = resolve_strategy_selection(state, ordinal, "ABANDONED")
    payload = json.loads(strategy_state_json(state))
    missing = payload["recent_selections"][-1]
    payload["recent_selections"][-1] = payload["recent_selections"][-2]
    payload["last_selected_ordinal"][missing] = 0
    with pytest.raises(ValueError, match="invalid"):
        parse_strategy_state_json(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def test_nested_state_mutation_breaks_persisted_digest() -> None:
    state = initial_strategy_state(31)
    persisted_sha256 = strategy_state_sha256(state)
    verified_copy = verify_strategy_state_sha256(state, persisted_sha256)
    assert verified_copy == state
    state.posterior["hparam"] = BetaPosterior(alpha=2.0, beta=1.0)
    with pytest.raises(ValueError, match="digest does not match"):
        verify_strategy_state_sha256(state, persisted_sha256)


def test_stale_or_conflicting_ordinal_cannot_resample_or_resolve() -> None:
    state = initial_strategy_state(9)
    state, intent = select_strategy_move(state, 1)
    with pytest.raises(ValueError, match="different strategy intent"):
        select_strategy_move(state, 2)
    with pytest.raises(ValueError, match="does not match pending"):
        resolve_strategy_selection(state, 2, "KEEP")
    resolved = resolve_strategy_selection(state, 1, "KEEP")
    with pytest.raises(ValueError, match="next unselected"):
        select_strategy_move(resolved, 1)
    # The selected intent is not discarded while infrastructure work is pending.
    assert state.pending == intent
