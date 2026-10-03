# 79 — Canceled result publication and broker identity

30 September 2026. Source changes only; no AOS files, live services, GPU
allocation, model download or training were changed. Baseline Scientist commit:
`38d513427df88f98f0a73f0598a5b48dfe636a90`.

## Failure captured before the fix

The AOS review's source-derived cancellation race was reproduced with synthetic
process/principal/drain observations against the real SQLite scheduler and
control store. A duplicate handler registers before cancel, pauses, and resumes
after a canceled terminal and scheduler `done` ticket commit.

Three paths incorrectly published/promoted that cached result: `_intent`,
`_completed_result`, and `_complete`. A fourth test showed that cancellation
after result-ready skipped private work-directory cleanup. All four assertions
failed before production changes: pytest **4 failed / 0.35 seconds**;
bounded parent **exit 1 / 0.524990269 seconds**, source unchanged.
An earlier attempt failed in fixture setup; it is not the race reproduction.

## Implemented publication boundary

Every controlled cache promotion and read now uses the same SQLite transaction
to check an immutable `completed` terminal, original principal/request/profile
identity, terminal receipt hash, allocation/drain bindings and persisted result
hash. A scheduler `done` ticket alone cannot authorize a controlled result.
The direct completion branch returns the verified persisted result. Cancellation
that wins the terminal transaction denies all three paths; cancellation after
immutable completion preserves that completed terminal fact.

The live-handler cancellation path performs cleanup after proven release even
when result-ready was already stored. Repeated removal of the same owned
directory is idempotent; missing cleanup evidence still does not grant release.
Restart cleanup now scans at most eight retained terminal allocations per cycle.
It validates the original lease, committed receipt, allocation/drain hashes and
exact drained child binding before deleting an owned directory. A separate
mutable cleanup marker preserves the immutable terminal; attempt ordering prevents
the first eight failed cleanups from starving later entries. Quarantined,
nonterminal, foreign and partial-remove cases remain unmarked. Allocated
no-child directory cleanup is conservatively denied by this helper and remains
an open edge; none of these CPU results prove native process/GPU cleanup.

## Broker identity

Service composition now authenticates the fixed `swapp-lab-gpu-broker.service`
against actual UID, PID, process start, boot ID, systemd invocation and cgroup.
It checks process birth and unit snapshot twice and rechecks its original full
generation before authorizing a control/infer peer. A restarted broker cannot
claim the old generation hash. Persisting the original admission pins in each
turn remains open; this source fix alone does not solve that requirement.
Both unit lookups obey the remaining original control deadline; exhausted
deadlines and subprocess timeout fail closed.

## Focused CPU evidence

- First publication fix plus adjacent executor/store/transport/broker identity:
  **50 passed / 2.31 seconds**, bounded parent **exit 0 / 2.487829902 seconds**,
  source unchanged. Includes successful completed-result cases and corrupt
  identity/result/receipt rejection, rather than denying all cached results.
- Broker identity plus control transport: **11 passed / 0.15 seconds**,
  bounded parent **exit 0 / 0.339680319 seconds**, source unchanged.
- Final publication/restart-cleanup/deadline cases plus adjacent components:
  **58 passed / 3.12 seconds**, bounded parent **exit 0 / 3.297917412 seconds**,
  source unchanged. This adds restart/idempotence, unsafe-directory retention,
  cleanup fairness and broker control-deadline checks.

Private receipts retain exact commands and pre/post source hashes. These are
CPU fixtures, not native child cleanup, physical GPU release, trained adapter
or joint AOS acceptance. The prior quality gate applies to commit 38d5134;
the final changed source passed all seven mandatory commands: bounded parent
**exit 0 / 81.403258620 seconds**, source unchanged; **1506 passed**,
7 opt-in skipped and 120 GPU/live deselected; strict mypy checked 148 files.
Wheel build and wheel-only import succeeded. The first gate failed only on
the newly imported subprocess exception type; that import is now explicitly
limited to the exception class with a local justified security annotation.

## Remaining shared acceptance

The proposed contract resolution and metadata fixtures are in [78](78-aos-control-contract-resolution.md).
Full nested profile schemas, integer clock migration, durable admission pins,
cleanup rights after policy/source rotation, retained-target capacity and AOS's
typed client/resolution journal remain open. Neither side is admitted for a
real shared GPU turn until the agreed source/configuration/reservation is verified.
