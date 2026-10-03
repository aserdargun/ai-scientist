# Runtime policy revocation

Status: implemented; 137 focused CPU checks passed; independent review approved. Full gate passed: 3,753 tests, seven commands, wheel build and import. Production unchanged.

## Reproduced failure

A CPU reproduction used the real private `ControlPolicy` file, `ControlStore`
and `SharedGpuScheduler`, with fixture process generations and clocks. After
registering and queuing a request, disabling the policy made the original
admission verifier reject it. The scheduler nevertheless allocated the queued
request: owner changed from absent to AOS and fencing token from 0 to 1. No
worker or GPU was started. The initial regression failed before the fix.

## Required behavior

- Recheck current enabled policy, source/profile pins and original caller and
  broker generation when submitting, allocating and progressing plan/start/go.
- Use the existing allocation SQLite transaction and connection. Do not create
  another allocator or open a competing transaction to inspect launch rights.
- Keep the original binding, execution budget and BOOTTIME deadlines. The
  current-rights check must not renew a capability or extend the run.
- Reject missing trusted verification in production. Synthetic test verifiers
  remain confined to explicit fixtures.
- Retire a denied, provably unallocated queue entry atomically so the other
  principal can advance; never treat it as an allocated worker's cleanup proof.
- When allocated authority is lost, retain fencing and use the existing
  quarantine/drain/recovery path. Policy revocation alone does not release GPU
  resources. Independent terminal and cleanup operations must remain available.

## Scope

This change closes stale inference-policy admission. It does not implement the
separate shared-launch entered-service binding or its physical closure. Both
remain required before enabling the composed AOS/Scientist runtime. The GPU
acceptance run, worker cleanup proof and resource handoff measurements remain
open; no source or fixture result substitutes for them.

## Focused evidence

137 CPU checks passed. Coverage includes real private policy revocation after
queueing and at submit/plan/start/go/ready/running boundaries; the other
principal progressing without an AOS retry; original-target quarantine and
physical drain; missing verifier; unchanged budget after capability expiry;
callback use of the existing transaction; original envelope and phase expiry
during the callback; and queued verification failure not blocking active
cleanup. Process identity, clocks and physical drain observations are fixtures.
No GPU or model was used, and no running service was changed.

[Curated source and gate evidence](review-evidence/runtime-policy-revocation-20261003.json). The 51 skipped and 177 deselected cases were not executed by this gate; they are not accepted runtime evidence.
