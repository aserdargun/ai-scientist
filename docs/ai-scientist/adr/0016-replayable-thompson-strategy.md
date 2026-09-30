# ADR 0016: Replayable Thompson proposal strategy

Status: pure policy implementation prepared; Director loop integration remains
separate.

## Policy contract

`lab.director.strategy` selects among the nine spec move types using Beta
Thompson sampling. Every move starts at `Beta(1, 1)`. After each completed
candidate or policy outcome, all excess pseudo-counts decay by `gamma = 0.97`
toward that neutral prior. This keeps old evidence influential without letting
the posterior collapse into a permanent choice. `KEEP` and `KEEP_SIMPLER`
increment alpha. A candidate or policy failure/rejection increments beta.
Explicit `ABANDONED` consumes a completed experiment and applies decay without
a reward. Infrastructure failure is not a policy outcome: it has no resolver
value, so the checkpoint keeps its pending intent and posterior unchanged.

The first nine ordinals visit the nine move types in the spec order. Thereafter,
if a move is absent from the preceding 19 selections, the policy selects the
oldest due such move, breaking ties by spec order. Otherwise it samples the nine
posterior distributions and selects the largest draw, again breaking exact
ties by spec order. Thus every rolling window of 20 selections contains every
move type while all unconstrained selections use Thompson sampling.

## Durable checkpoint and retry

The state stores the policy and PRNG algorithm versions, seed and current
SplitMix64 state, all nine Beta parameters, the rolling selection window and
last-selected ordinals, plus a pending selection. The pending record includes
the proposal ordinal, chosen move, selection reason, all sampled scores when
Thompson sampling ran, and RNG state before and after selection.

The Director must persist the post-selection state before invoking a provider.
Retrying the same pending ordinal returns the stored move and scores without
advancing the PRNG. A stale ordinal or conflicting selection/resolution fails
closed. Canonical JSON rejects duplicate keys, non-finite values, extra fields,
and noncanonical encodings; no pickle or process-global RNG is used.

Before calling a provider, the caller must persist the canonical bytes and
their SHA-256 as the preregistered intent, then reload those bytes and call
`verify_strategy_state_sha256` with the stored digest. This check is the
durable boundary against mutation of the nested Python mappings in a frozen
Pydantic model. The strategy API deep-copies and revalidates mappings at each
transition, but in-memory state alone is not a durable integrity proof.

The minimal future loop integration is: store `StrategyState` as a nested
`DirectorLoopState` field; call `select_strategy_move(state, next_ordinal)`;
persist the returned state as the preregistered intent before proposal work;
resolve only after a durable terminal candidate/policy result; on infrastructure
failure preserve the pending state for retry. The strategy API deliberately
does not own the experiment budget, loop checkpoint, or ledger transaction.

## EXPLORE family proof is separate

Move types describe edit operations, not detector families. A move selection
cannot satisfy the spec's mandatory EXPLORE family change. That later feature
must classify the trusted phase-entry champion and candidate source using a
canonical detector-family registry and verified AST/import lineage. The
required change is relative to the phase-entry family; the 10-experiment S2
phase does not require ten distinct families or a new family for every
proposal. General research proposals remain possible; unsupported or ambiguous
classification stays explicitly `explore_family_unverified` and follows
bounded abandonment. A model-provided family label is never evidence.

## Validation boundary

Pure tests cover exact checkpoint/retry parity, same-seed trace parity, a fixed
SplitMix64 vector, bounded Beta distribution sanity, randomized rolling-20
coverage, an aging oldest-due selection, decay and reward semantics, explicit
abandonment, infrastructure no-op behavior, stale/conflicting ordinals,
hostile JSON, persisted-window rejection, and state-mutation digest detection.
This policy-only slice does not claim Director loop, database, or end-to-end
run acceptance.
