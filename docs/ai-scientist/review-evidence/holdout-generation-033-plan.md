# Holdout execution-generation binding plan for integration-033

Status: source review only. No code, database, service, or worker was changed or run for this note. The current integration tree is based on `9ad6208` and contains moving, unapproved ownership/baseline changes. This plan does not assert that those changes pass PostgreSQL or runtime acceptance.

## Existing holdout path and missing binding

The Director execution owner created by `lab.director.ownership.claim_initial_execution()` is the immutable tuple `(run_id, generation, invocation_id, execution_sha256)`. The run keeps an execution contract and current-generation pointer. The holdout path currently does not carry that tuple into its durable records or Scorer worker protocol.

| Stage | Current entrypoint | Current authority/identity | Gap |
| --- | --- | --- | --- |
| Periodic or run-end request | `lab.director.holdout.evaluate_holdout_check()` | Director caller has the current execution-owner context; `reserve_holdout_check()` takes plan advisory lock and calls `assert_execution_owner_transaction()` before `lab.reserve_holdout_check(...)` | The SQL RPC locks the run itself and stores no generation/hash. A direct granted RPC call can bypass the Python assertion and its lock order. |
| Run-end intent | `register_run_end_intent()` → `lab.register_holdout_run_end_intent(run_id,intent)` | Python writes a hash-verified lease checkpoint, then plan-locks and asserts current owner | Intent JSON/table bind run, candidate, state/application and budget receipts, but not the execution owner pair. The SQL function does not establish that pair itself. |
| Unavailable run-end fence | `fence_run_end_unavailable()` → `lab.fence_run_end_unavailable(...)` | Python plan-locks and asserts active current owner | The durable fence binds candidate/prior-state/budget/intent checkpoint identity, but not generation/hash. The RPC should make the owner assertion authoritative in the transaction. |
| Missing-reservation run-end fence | `fence_missing_run_end_admission()` → `lab.fence_missing_holdout_run_end(...)` | Python plan-locks and asserts current owner | The bitless 0020 fence binds intent and reconciled-budget checkpoint identity, not owner generation/hash. |
| Worker claim | `holdout_supervisor.run_holdout_process(UUID)` → fixed-argv `holdout_worker` → `evaluate_holdout_reservation()` → `lab.claim_holdout_reservation(...)` | Reservation UUID plus systemd PID/start/boot/unit/invocation/cgroup tuple | Reservation and worker tuple do not bind the Director generation that admitted the reservation. Worker argv has no owner pair and SQL reads current run state only. |
| Per-container admission | `admission_check()` → `lab.check_holdout_admission(reservation_id,run_id,worker tuple)` | Exact reservation and worker tuple; verifies run active | Does not compare the reservation to the captured Director generation/hash. |
| Result/failure | `lab.publish_holdout_result(...)`, `lab.fail_holdout_reservation(...)` | Exact reservation worker tuple; publish writes private metrics/result and may advance `holdout_approvals`; fail records operational failure | No admitted owner pair check, so a stale-but-live worker can publish after ownership changes if run state remains permissive. |
| Recovery | 0020 `read_holdout_recovery_target`, `recover_holdout_failure`, `recover_unclaimed_holdout_failure`, `list_holdout_recovery_targets`; Python `recover_holdout_reservation()` / `recover_holdout_run()` | Exact prior worker tuple plus unit/cgroup/container drain proof; run-level supervisor receives run UUID only | Recovery has no immutable parent owner pair. It must retain the reservation's admitted pair and must never read the current pointer to upgrade a stale reservation. |
| Receipt read | `read_holdout_bit(run_id,reservation_id)` | Durable reservation and result bit | Read response has no owner pair for Director-side identity comparison. |

Relevant frozen migrations are `0019_bounded_holdout.py`, `0020_holdout_recovery.py`, and `0021_run_end_unavailable.py`. 0019 defines the reservation, intent, approval, quota, and result path. 0020 adds exact worker recovery and the missing-run-end fence. 0021 adds fresh-zero-budget / pre-registration run-end-unavailable receipts and blocks later admissions. None stores a Director owner tuple. The current code already has valuable worker-generation and artifact/checkpoint identity protections; generation binding should be additive and preserve them.

## Smallest coherent additive change

Add a new migration after the integrated ownership migration (currently planned as `0026_holdout_execution_owner`, with the parent chosen after branch reconciliation). Do not edit 0019–0021. Add nullable pair columns for historical compatibility, but require the pair on every new reservation, intent, run-end fence, and unavailable receipt:

- `admitted_generation INTEGER` and `execution_sha256 CHAR(64)` on `lab.holdout_reservations`, `lab.holdout_run_end_intents`, `lab.holdout_run_end_fences`, and `lab.holdout_run_end_unavailable`.
- Enforce paired nullability, positive generation, lowercase SHA-256 shape, immutable values, and a foreign key or equivalent validated join to the immutable execution-generation/contract history. Historical null-pair records remain readable but cannot be newly claimed, published, promoted, or used as evidence for a new run-end operation.
- Do not duplicate pair fields on `holdout_approvals`: its immutable `reservation_id` must resolve to the pair on the successful reservation. Receipt readers should return that pair from the joined reservation.

The database functions must own the assertion and use one lock order: discover `run_id` with a non-locking lookup; acquire the canonical run-plan advisory lock; lock `lab.runs`; lock the execution-control/current-generation row; then lock the reservation/intent/fence row. Re-read the child after parent locks and reject if its run ID changed. Never lock a child and then acquire the plan lock. This makes the SQL API safe even when invoked directly through the existing grants.

Minimal API shape:

1. Director-side reservation/intent/fence functions accept the captured expected pair as explicit arguments, acquire the ordered locks, require the pair to equal the already claimed current owner and immutable contract, then persist the pair. The Director wrapper must pass its captured `ExecutionOwner`; it must not fetch a new current owner to make an old operation succeed. Apply this to `reserve_holdout_check`, `register_holdout_run_end_intent`, `fence_missing_holdout_run_end`, and `fence_run_end_unavailable`.
2. A Scorer-only `assert_holdout_execution(reservation_id, expected_generation, expected_execution_sha256, operation)` reads the reservation's stored pair, then takes plan/run/control/reservation locks in order, compares the supplied pair and immutable contract, and sets transaction-local Scorer assertion context only after validation. `operation` must be a closed enum, not a caller-controlled bypass. Claim/admission/publication use active mode; cleanup uses the separate stopped-recovery mode described below.
3. Extend the existing 0019 claim/admission/publish/fail and 0020 recovery/list/read RPC return payloads with the stored pair. The Scorer Python path may carry the pair in typed internal objects, but outward Director/API status remains limited to state/bit/operational status. Private metrics stay inside Scorer.
4. `holdout_supervisor` and worker argv continue to identify the reservation (and exact systemd generation); they do not take an arbitrary owner tuple as authority. The worker obtains the admitted pair from the authoritative Scorer claim RPC and carries that immutable value through `admission_check`, result publication, and failure. This avoids turning transport arguments into an owner-selection mechanism.

Normal evaluation requires `run.state='running'`, the reservation pair to match the current owner/control tuple, and the exact worker identity to remain valid. Once stop is requested, a distinct cleanup operation may fail a reservation with `result_bit=NULL` only after the existing PID/start/boot, unit/cgroup and claim-owned sandbox/marker drain proof. That operation may not publish a score, set a passing bit, or change approval. If a successor generation is eventually supported, old-generation cleanup must be an explicit fenced recovery authority for that exact reservation; it must not accept a newly read current pair. Continuation/takeover remains unavailable until such a complete protocol is separately implemented and proven.

Run-end intent, 0020 missing-reservation fence, and 0021 unavailable fence must all persist the same captured pair as the state/application checkpoint they authorize. Exact retry compares the entire tuple and checkpoint identity. Read RPCs expose the pair only to Director/Scorer roles that need it. Bitless terminal receipts remain bitless; no synthetic reservation UUID or quota mutation is introduced.

## Caller changes and proof matrix

Director code needs a captured-owner argument/context at `evaluate_holdout_check`, `check_at_run_end`, `register_run_end_intent`, `fence_missing_run_end_admission`, and `fence_run_end_unavailable`. Existing helper assertions are useful defense in depth but cannot replace the SQL checks. When a holdout retry sees an existing reservation, it must recover/read against that reservation's original pair; it must not create a new owner pair from the latest pointer.

Scorer changes belong in `lab/scorer/holdout.py`, `holdout_worker.py`, `holdout_supervisor.py`, and `holdout_recovery.py`. Claim should return the durable admitted pair. Before each fit/score container and before any result/failure write, check the same pair and existing worker identity. Recovery should compare the captured request pair against the reservation pair, prove exact old worker/sandbox drain, and only then issue the failure CAS. A healthy worker stays pending; retry must not stop it or reserve/quota-charge again.

Focused live PostgreSQL tests should verify:

- Current generation: reserve → claim → per-container admission → publish succeeds, with the pair unchanged at each receipt.
- Wrong, missing, null, cross-run, and stale generation/hash arguments fail atomically through both Python wrappers and direct role-authorized SQL calls; quotas, reservation state, result rows and approvals stay unchanged.
- After the current owner changes or stop is requested, an old worker cannot pass admission or publish/promote. Exact drained recovery may write only failed/null-bit, and never refunds quota or reruns the query.
- Run-end intent, 0020 fence and 0021 unavailable records require the same pair and exact checkpoint identity; idempotent exact retries succeed while changed-pair retries fail.
- The lock path follows plan → run → control → reservation for concurrent reserve/stop/recovery tests. Existing historical null-pair rows remain readable only.

These tests establish SQL/RPC generation binding only. They do not establish a full host-death/cgroup recovery acceptance, real holdout latency, Farm B calibration, or M0 end-to-end acceptance.

## Separate baseline / ownership dependencies

These are prerequisites in the moving 0024/0025 integration, not reasons to broaden the holdout migration:

- The ownerless 0024 `prepare_unstarted_baseline_stop` path needs its own narrow Scorer assertion and same-state empty-plan seal permission; it must not become a general ownerless run mutation.
- Planner `task_completions` and queued score-job cancellation need narrowly scoped stop-closure assertions. Active-work assertions cannot be reused after `stop_requested`, and the row guard must permit only the matching same-generation cancellation receipt.
- `register_baseline_operation` needs plan → run → control ownership validation inside its granted SQL path. Its claim/update and retry semantics must preserve the immutable original deadline.
- API stop must carry the pair and original deadline captured by the stop RPC into background cleanup/finalization. An owned baseline uses its paired Scorer finalizer; only the verified ownerless, empty baseline uses the dedicated empty-stop finalizer. Baseline runs must never dispatch holdout work.
- Current generation transition/continuation remains unavailable until every late-write path (Director ledger, task plan, Scorer job/result/report, holdout, recovery and run terminalization) is fenced. Passing a subset of the positive path is not approval to resume or take over a run.

## Astra/high review refinements — 2026-09-27

The next coherent implementation is an additive 0026 covering the full existing
first-generation holdout chain: reserve, run-end intents and bitless fences,
Scorer claim/admission/publish/fail/recovery, and joined receipt reads. A pair on
reservations alone would leave late writes and approvals unfenced.

- Closure must allow the same admitted/current generation to record a drained
  failed/null-bit outcome after its original deadline expires, including a run
  still marked running. Active admission remains prohibited. This closure cannot
  refund quota, issue another query, publish a passing bit or change approval.
- The migration must reject pre-existing reserved/running rows with null owner
  pairs unless a separately proven legacy cleanup protocol exists. It must not
  attach the latest owner to historical work. Both fields must be explicitly
  nonnull in the nonnull branch of the pair CHECK, avoiding SQL UNKNOWN.
- Generation takeover tests stay deferred while 0025 supports only immutable
  generation 1. Wrong supplied generation tests are valid now. An admin-mutated
  pointer would establish synthetic fencing behavior only.
- Every legacy granted overload must be fenced or made private and revoked.
  SQL owns the plan → run → control → reservation lock order. Scorer captures
  the reservation's admitted pair once; it never repairs stale work by reading
  a newer current owner. Existing worker identity, raw drain, quota and private
  metric boundaries stay part of the contract.

This is source review, not an implementation or PostgreSQL acceptance result.
