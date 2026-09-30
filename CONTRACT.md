# Harness contract

Harness changes are versioned by `harness/VERSION`. Candidate-facing contracts use immutable typed inputs and bounded numeric outputs. The candidate never receives labels or holdout metadata.

The production Referee is deterministic for a pinned input manifest, NumPy version, bootstrap count and seed. Guard failure returns a strict JSON-safe `REJECT` with unmeasured values set to `null`; it never writes NaN/Infinity. `KEEP` means selection on the current development suite only, not a population-level improvement or promotion.

Review corrections applied at this boundary:

- alarm thresholds must be finite and dwell must be a positive integer;
- score vectors must be finite, one-dimensional, non-empty and length-matched;
- weights must be finite and strictly positive;
- a scoring invocation receives a fresh sandbox instance restored from the frozen fit artifact; candidate pickle is never opened by trusted Scorer/host code;
- masked periods retain their original time positions; only unmasked onsets inside PDM windows earn credit; zero healthy exposure raises `InvalidMetricError` rather than producing NaN metrics.
- `FitContext.sampling_s=None` explicitly represents unverified physical cadence for EVT sample-order tasks; PDM/NRM require a verified positive integer cadence. Unknown cadence is never silently reported as one second.

`harness/contracts.py` and `harness/referee.py` are versioned implementation contracts. Appendix C remains immutable source material; parity tests record intentional safety/type corrections.
