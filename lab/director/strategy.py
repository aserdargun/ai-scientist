"""Replayable bounded Thompson policy for Director proposal move types.

The caller persists the state returned by ``select_strategy_move`` before it
asks a provider for a proposal. The nested pending selection is the durable
intent: retrying that ordinal returns it unchanged without advancing RNG.
Infrastructure failures do not resolve a selection; the caller retries the
pending intent or records an explicit candidate/policy abandonment.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, model_validator

from lab.director.contracts import MoveType

_MOVE_TYPES: tuple[MoveType, ...] = (
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
_UINT64_MASK = (1 << 64) - 1
_SPLITMIX_INCREMENT = 0x9E3779B97F4A7C15
_MAX_SELECTIONS = 1_000_000
_MAX_POSTERIOR = 1.0 + _MAX_SELECTIONS
_DECAY = 0.97
_POLICY_VERSION = "beta-thompson-coverage-v1"
_RNG_VERSION = "splitmix64-marsaglia-tsang-box-muller-v1"

StrategyOutcome = Literal[
    "KEEP",
    "KEEP_SIMPLER",
    "DISCARD",
    "CANDIDATE_REJECT",
    "POLICY_REJECT",
    "CANDIDATE_FAILURE",
    "ABANDONED",
]
SelectionReason = Literal["initial_coverage", "rolling_coverage", "thompson"]


class BetaPosterior(BaseModel):
    """Discounted Beta pseudo-counts; both parameters decay toward one."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    alpha: Annotated[StrictFloat, Field(allow_inf_nan=False, ge=1.0, le=_MAX_POSTERIOR)]
    beta: Annotated[StrictFloat, Field(allow_inf_nan=False, ge=1.0, le=_MAX_POSTERIOR)]


class StrategySelection(BaseModel):
    """Immutable post-selection intent which can be safely replayed."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    ordinal: Annotated[StrictInt, Field(ge=1, le=_MAX_SELECTIONS)]
    move_type: MoveType
    reason: SelectionReason
    sampled_scores: dict[MoveType, Annotated[StrictFloat, Field(allow_inf_nan=False, ge=0, le=1)]]
    prior_last_selected_ordinal: dict[
        MoveType, Annotated[StrictInt, Field(ge=0, le=_MAX_SELECTIONS)]
    ]
    rng_state_before: Annotated[StrictInt, Field(ge=0, le=_UINT64_MASK)]
    rng_state_after: Annotated[StrictInt, Field(ge=0, le=_UINT64_MASK)]

    @model_validator(mode="after")
    def validate_samples(self) -> StrategySelection:
        if set(self.prior_last_selected_ordinal) != set(_MOVE_TYPES):
            raise ValueError("strategy intent must retain prior move recency")
        if self.reason == "thompson":
            if set(self.sampled_scores) != set(_MOVE_TYPES):
                raise ValueError("Thompson selection must retain one sample per move type")
        elif self.sampled_scores:
            raise ValueError("coverage selections cannot contain fabricated posterior samples")
        if self.reason != "thompson" and self.rng_state_before != self.rng_state_after:
            raise ValueError("deterministic coverage selection cannot advance the RNG")
        return self


class StrategyState(BaseModel):
    """JSON-safe complete policy state, including a preregistered pending move."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal["thompson-strategy.v1"] = Field(alias="schema")
    policy_version: Literal["beta-thompson-coverage-v1"]
    rng_version: Literal["splitmix64-marsaglia-tsang-box-muller-v1"]
    seed: Annotated[StrictInt, Field(ge=0, le=_UINT64_MASK)]
    rng_state: Annotated[StrictInt, Field(ge=0, le=_UINT64_MASK)]
    selection_count: Annotated[StrictInt, Field(ge=0, le=_MAX_SELECTIONS)]
    completed_experiments: Annotated[StrictInt, Field(ge=0, le=_MAX_SELECTIONS)]
    posterior: dict[MoveType, BetaPosterior]
    recent_selections: tuple[MoveType, ...] = Field(max_length=20)
    last_selected_ordinal: dict[MoveType, Annotated[StrictInt, Field(ge=0, le=_MAX_SELECTIONS)]]
    pending: StrategySelection | None

    @model_validator(mode="after")
    def validate_state(self) -> StrategyState:
        if set(self.posterior) != set(_MOVE_TYPES):
            raise ValueError("strategy posterior must contain exactly all nine move types")
        if set(self.last_selected_ordinal) != set(_MOVE_TYPES):
            raise ValueError("strategy history must contain exactly all nine move types")
        if self.selection_count != self.completed_experiments + int(self.pending is not None):
            raise ValueError("strategy selection and completed-experiment counts disagree")
        expected_recent = min(self.selection_count, 20)
        if len(self.recent_selections) != expected_recent:
            raise ValueError("strategy recent selection window has the wrong length")
        if 9 <= self.selection_count <= 20 and self.recent_selections[:9] != _MOVE_TYPES:
            raise ValueError("persisted strategy history does not preserve initial move coverage")
        if self.selection_count >= 20 and set(self.recent_selections) != set(_MOVE_TYPES):
            raise ValueError("persisted rolling-20 strategy window omits a move type")
        first_recent_ordinal = self.selection_count - len(self.recent_selections) + 1
        observed: dict[MoveType, int] = {}
        for index, move in enumerate(self.recent_selections, start=first_recent_ordinal):
            observed[move] = index
        for move in _MOVE_TYPES:
            last = self.last_selected_ordinal[move]
            if not 0 <= last <= self.selection_count:
                raise ValueError("strategy last-selected ordinal is out of range")
            if move in observed:
                if last != observed[move]:
                    raise ValueError("strategy last-selected ordinal disagrees with recent history")
            elif last >= first_recent_ordinal:
                raise ValueError("strategy recent history omits a recent move selection")
        pending = self.pending
        if pending is not None:
            if pending.ordinal != self.selection_count:
                raise ValueError("pending strategy intent does not match latest selection")
            if not self.recent_selections or self.recent_selections[-1] != pending.move_type:
                raise ValueError("pending strategy intent disagrees with selection history")
            if pending.rng_state_after != self.rng_state:
                raise ValueError("pending strategy intent RNG state differs from durable state")
            prior_nineteen = self.recent_selections[:-1][-19:]
            if pending.ordinal <= len(_MOVE_TYPES):
                expected_move = _MOVE_TYPES[pending.ordinal - 1]
                if (
                    pending.reason != "initial_coverage"
                    or pending.move_type != expected_move
                    or pending.sampled_scores
                ):
                    raise ValueError("initial strategy coverage intent is inconsistent")
            else:
                missing = [move for move in _MOVE_TYPES if move not in prior_nineteen]
                if missing:
                    expected_move = min(
                        missing,
                        key=lambda move: (
                            pending.prior_last_selected_ordinal[move],
                            _MOVE_TYPES.index(move),
                        ),
                    )
                    if (
                        pending.reason != "rolling_coverage"
                        or pending.move_type != expected_move
                        or pending.sampled_scores
                    ):
                        raise ValueError("rolling strategy coverage intent is inconsistent")
                else:
                    expected_move, samples, next_rng_state = _sample_thompson_move(
                        self.posterior, pending.rng_state_before
                    )
                    if (
                        pending.reason != "thompson"
                        or pending.move_type != expected_move
                        or pending.sampled_scores != samples
                        or pending.rng_state_after != next_rng_state
                    ):
                        raise ValueError("Thompson strategy intent does not match saved RNG state")
            if any(
                last
                != (
                    pending.ordinal
                    if move == pending.move_type
                    else pending.prior_last_selected_ordinal[move]
                )
                for move, last in self.last_selected_ordinal.items()
            ):
                raise ValueError("pending strategy prior recency differs from durable recency")
        return self


def initial_strategy_state(seed: int) -> StrategyState:
    """Create a deterministic, unobserved Beta(1, 1) policy state."""
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= _UINT64_MASK:
        raise ValueError("strategy seed must be an unsigned 64-bit integer")
    return StrategyState.model_validate(
        {
            "schema": "thompson-strategy.v1",
            "policy_version": _POLICY_VERSION,
            "rng_version": _RNG_VERSION,
            "seed": seed,
            "rng_state": seed,
            "selection_count": 0,
            "completed_experiments": 0,
            "posterior": {move: {"alpha": 1.0, "beta": 1.0} for move in _MOVE_TYPES},
            "recent_selections": (),
            "last_selected_ordinal": {move: 0 for move in _MOVE_TYPES},
            "pending": None,
        },
        strict=True,
    )


def _validated_state_copy(state: StrategyState) -> StrategyState:
    """Deep-copy nested mappings through strict validation before transitions."""
    if not isinstance(state, StrategyState):
        raise ValueError("strategy state has an unsupported runtime type")
    try:
        pending_payload: dict[str, object] | None = None
        if state.pending is not None:
            pending_payload = {
                "ordinal": state.pending.ordinal,
                "move_type": state.pending.move_type,
                "reason": state.pending.reason,
                "sampled_scores": dict(state.pending.sampled_scores),
                "prior_last_selected_ordinal": dict(state.pending.prior_last_selected_ordinal),
                "rng_state_before": state.pending.rng_state_before,
                "rng_state_after": state.pending.rng_state_after,
            }
        return StrategyState.model_validate(
            {
                "schema": state.schema_version,
                "policy_version": state.policy_version,
                "rng_version": state.rng_version,
                "seed": state.seed,
                "rng_state": state.rng_state,
                "selection_count": state.selection_count,
                "completed_experiments": state.completed_experiments,
                "posterior": {
                    move: {"alpha": parameters.alpha, "beta": parameters.beta}
                    for move, parameters in state.posterior.items()
                },
                "recent_selections": tuple(state.recent_selections),
                "last_selected_ordinal": dict(state.last_selected_ordinal),
                "pending": pending_payload,
            },
            strict=True,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("strategy state failed boundary revalidation") from exc


def select_strategy_move(
    state: StrategyState, ordinal: int
) -> tuple[StrategyState, StrategySelection]:
    """Return the durable intent for the next ordinal, idempotently on retry."""
    state = _validated_state_copy(state)
    if isinstance(ordinal, bool) or not isinstance(ordinal, int):
        raise ValueError("strategy ordinal must be an integer")
    if state.pending is not None:
        if ordinal != state.pending.ordinal:
            raise ValueError("a different strategy intent is already pending")
        return state, state.pending
    expected = state.selection_count + 1
    if ordinal != expected or ordinal > _MAX_SELECTIONS:
        raise ValueError("strategy ordinal must be the next unselected experiment")

    rng_before = state.rng_state
    sampled_scores: dict[MoveType, float] = {}
    if ordinal <= len(_MOVE_TYPES):
        move = _MOVE_TYPES[ordinal - 1]
        reason: SelectionReason = "initial_coverage"
    else:
        last_nineteen = state.recent_selections[-19:]
        missing = [candidate for candidate in _MOVE_TYPES if candidate not in last_nineteen]
        if missing:
            move = min(
                missing,
                key=lambda candidate: (
                    state.last_selected_ordinal[candidate],
                    _MOVE_TYPES.index(candidate),
                ),
            )
            reason = "rolling_coverage"
        else:
            move, sampled_scores, rng_after = _sample_thompson_move(
                state.posterior, state.rng_state
            )
            reason = "thompson"
            state = state.model_copy(update={"rng_state": rng_after})

    selection = StrategySelection(
        ordinal=ordinal,
        move_type=move,
        reason=reason,
        sampled_scores=sampled_scores,
        prior_last_selected_ordinal=dict(state.last_selected_ordinal),
        rng_state_before=rng_before,
        rng_state_after=state.rng_state,
    )
    history = (*state.recent_selections, move)[-20:]
    last_selected = dict(state.last_selected_ordinal)
    last_selected[move] = ordinal
    updated = state.model_copy(
        update={
            "selection_count": ordinal,
            "recent_selections": history,
            "last_selected_ordinal": last_selected,
            "pending": selection,
        }
    )
    # model_copy deliberately skips validation; revalidation protects the durable wire.
    updated = StrategyState.model_validate(
        updated.model_dump(mode="python", by_alias=True), strict=True
    )
    return updated, selection


def resolve_strategy_selection(
    state: StrategyState, ordinal: int, outcome: StrategyOutcome
) -> StrategyState:
    """Decay once and resolve a candidate/policy outcome or explicit abandonment.

    Infrastructure failures have no outcome in this API. The caller must keep
    the pending state intact so a retry cannot draw a different move or invent
    a reward. A policy/candidate rejection is distinct from infrastructure.
    """
    state = _validated_state_copy(state)
    if isinstance(ordinal, bool) or not isinstance(ordinal, int):
        raise ValueError("strategy resolution ordinal must be an integer")
    pending = state.pending
    if pending is None or pending.ordinal != ordinal:
        raise ValueError("strategy resolution does not match pending intent")
    valid_outcomes = {
        "KEEP",
        "KEEP_SIMPLER",
        "DISCARD",
        "CANDIDATE_REJECT",
        "POLICY_REJECT",
        "CANDIDATE_FAILURE",
        "ABANDONED",
    }
    if not isinstance(outcome, str) or outcome not in valid_outcomes:
        raise ValueError(
            "strategy outcome is not a candidate, policy, or explicit abandonment result"
        )

    posterior = {
        move: {
            "alpha": 1.0 + (parameters.alpha - 1.0) * _DECAY,
            "beta": 1.0 + (parameters.beta - 1.0) * _DECAY,
        }
        for move, parameters in state.posterior.items()
    }
    if outcome in {"KEEP", "KEEP_SIMPLER"}:
        posterior[pending.move_type]["alpha"] += 1.0
    elif outcome != "ABANDONED":
        posterior[pending.move_type]["beta"] += 1.0
    if state.completed_experiments >= _MAX_SELECTIONS:
        raise ValueError("strategy completed-experiment budget is exhausted")
    payload = state.model_dump(mode="python", by_alias=True)
    payload.update(
        {
            "completed_experiments": state.completed_experiments + 1,
            "posterior": posterior,
            "pending": None,
        }
    )
    return StrategyState.model_validate(payload, strict=True)


def strategy_state_json(state: StrategyState) -> bytes:
    """Encode a canonical strict-JSON state document for durable checkpointing."""
    payload = _validated_state_copy(state).model_dump(mode="json", by_alias=True)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )


def strategy_state_sha256(state: StrategyState) -> str:
    """Digest the exact canonical JSON checkpoint bytes."""
    return hashlib.sha256(strategy_state_json(state)).hexdigest()


def verify_strategy_state_sha256(state: StrategyState, expected_sha256: str) -> StrategyState:
    """Return a deep validated copy only when it matches its durable hash."""
    if (
        not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
        or strategy_state_sha256(state) != expected_sha256
    ):
        raise ValueError("strategy checkpoint digest does not match canonical state")
    return _validated_state_copy(state)


def parse_strategy_state_json(raw: bytes | str) -> StrategyState:
    """Reject duplicate keys, noncanonical encodings, and invalid state values."""
    try:
        text = raw.decode("utf-8", errors="strict") if isinstance(raw, bytes) else raw
        value = json.loads(
            text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant
        )
        if not isinstance(value, dict) or not isinstance(value.get("recent_selections"), list):
            raise ValueError("strategy state JSON has invalid selection history shape")
        value["recent_selections"] = tuple(value["recent_selections"])
        state = StrategyState.model_validate(value, strict=True)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("strategy state JSON is invalid") from exc
    if strategy_state_json(state).decode("utf-8") != text:
        raise ValueError("strategy state JSON is not in canonical form")
    return state


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("strategy state JSON contains duplicate object keys")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"strategy state JSON contains non-finite constant: {value}")


def _next_uint64(state: int) -> tuple[int, int]:
    state = (state + _SPLITMIX_INCREMENT) & _UINT64_MASK
    value = state
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & _UINT64_MASK
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & _UINT64_MASK
    return state, (value ^ (value >> 31)) & _UINT64_MASK


def _uniform(state: int) -> tuple[int, float]:
    state, value = _next_uint64(state)
    # Midpoint conversion excludes exactly zero and one for logarithms.
    sample = ((value >> 11) + 0.5) / (1 << 53)
    return state, min(sample, math.nextafter(1.0, 0.0))


def _normal(state: int) -> tuple[int, float]:
    state, first = _uniform(state)
    state, second = _uniform(state)
    return state, math.sqrt(-2.0 * math.log(first)) * math.cos(2.0 * math.pi * second)


def _gamma_unit_scale(shape: float, state: int) -> tuple[int, float]:
    if not math.isfinite(shape) or shape < 1.0:
        raise ValueError("Beta posterior shape must be finite and at least one")
    d = shape - (1.0 / 3.0)
    c = 1.0 / math.sqrt(9.0 * d)
    for _ in range(128):
        state, normal = _normal(state)
        base = 1.0 + c * normal
        if base <= 0.0:
            continue
        value = base * base * base
        state, uniform = _uniform(state)
        normal_square = normal * normal
        if uniform < 1.0 - 0.0331 * normal_square * normal_square or math.log(uniform) < (
            0.5 * normal_square + d * (1.0 - value + math.log(value))
        ):
            return state, d * value
    raise RuntimeError("bounded Gamma sampler failed to converge")


def _beta_sample(alpha: float, beta: float, state: int) -> tuple[int, float]:
    state, first = _gamma_unit_scale(alpha, state)
    state, second = _gamma_unit_scale(beta, state)
    total = first + second
    if not math.isfinite(total) or total <= 0.0:
        raise RuntimeError("Beta sampler produced an invalid total")
    sample = first / total
    if not math.isfinite(sample) or not 0.0 <= sample <= 1.0:
        raise RuntimeError("Beta sampler produced a non-finite draw")
    return state, sample


def _sample_thompson_move(
    posterior: dict[MoveType, BetaPosterior], rng_state: int
) -> tuple[MoveType, dict[MoveType, float], int]:
    sampled_scores: dict[MoveType, float] = {}
    for move in _MOVE_TYPES:
        parameters = posterior[move]
        rng_state, sample = _beta_sample(parameters.alpha, parameters.beta, rng_state)
        sampled_scores[move] = sample
    selected = max(
        _MOVE_TYPES,
        key=lambda candidate: (sampled_scores[candidate], -_MOVE_TYPES.index(candidate)),
    )
    return selected, sampled_scores, rng_state
