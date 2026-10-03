# Never-received request: proposed observation contract

Status: **semantic agreement; Scientist CPU implementation verified; not deployed or
accepted as terminal evidence**.
Feature: `no-admission-observation.v1`. This does not reopen native work.

## Existing constraints

Original request `3ccf93764f4c4f389864cc2edbdbcbfe` has an immutable AOS
pending intent and admission capture. Canonical Scientist admission, GPU queue
and child rows are absent. Original caller and broker processes, cgroups and
container have closed. These observations alone are not an accepted resolution.
The original desktop is now stopped/PAUSED at generation 1; the original AGENT
generation 0 cannot authorize new calls.

The existing retained reconcile requires a canonical admission row. Its budget
and terminal verifiers require an independently retained admitted time and
envelope deadline. Neither may be invented for a never-received frame. Keep
the existing admission, budget, terminal and release contracts intact.

## Proposed producer placement

Extend the **trusted composition of the existing canonical ControlStore** with
an observation operation. There is no such operation or deployed endpoint yet.
It must default to deny without an independently configured observer authority,
exact original target and independently retained capture/source verification.
It is not a new scheduler, resource allocator or general historical grant.

In one bounded canonical SQLite write transaction:

1. Verify the authenticated current observer and exact cleanup-only target.
2. Independently validate original request bytes, capture and complete binding.
3. Read admission, queue, allocation/child/deferred-execution provenance for the
   exact request. Any contradictory, incomplete or ambiguous provenance rejects.
4. Independently prove the old caller and broker generations and their original
   groups are physically gone, with no children capable of delayed dispatch.
5. Append an immutable **observation/tombstone**, without inserting an admission,
   reservation, budget, GPU token or release record.
6. Recheck observer/source authority and physical provenance before committing.
   Revocation, timeout or failed cleanup rolls back the entire transaction.

The normal admission transaction must check this tombstone and reject later
admission or reuse of the closed request ID. An absence snapshot without that
atomic late-admission fence is insufficient. Same-target, same-proof retry is
idempotent; conflicting bytes or authority reject. Historical reads grant no
fresh observation authority.

## Proposed closed wire shape

All objects reject extra fields; hashes are lowercase SHA-256; generation
objects carry boot ID, PID, start ticks, UID, unit, invocation ID and cgroup.
Use the existing canonical JSON/hash rules consistently on both sides.

| Field | Required meaning |
| --- | --- |
| `schema`, `version` | `aos-scientist-no-admission-observation.v1`, integer `1` |
| `target` | Exact original request ID, request SHA and original principal-generation SHA |
| `original` | Complete independently verified admission binding, binding/capture SHA, profile/deployment/config/output/source pins; original caller and broker generations |
| `observer` | Actual current authenticated observer generation, reviewed source/config and capability SHA; purpose exactly `observe_no_admission` |
| `canonical` | Fixed canonical store identity, exact missing-row/queue/child/deferred provenance, committed observation/tombstone identity and proof SHA |
| `physical` | Independently retained old-generation/group/child absence and late-dispatch-fence proof SHA; no global-idle shortcut |
| `cleanup_scope` | Original store/session/runtime identity plus independently verified current lease ID and stopped/PAUSED generation; current generation is greater than the immutable original generation. This case is 0 → 1 and the lease changed. |
| `outcome` | Exactly `never_received`; not `released`, `completed` or a learned model result |
| `admission_budget` | Exactly `null`; no fabricated admitted time, assigned deadline or reservation |
| `freshness` | Current observer boot, observed/expiry boottime under an explicitly reviewed finite bound; never rebase an original deadline |
| `observation_sha256` | Canonical proof digest; hash equality alone is insufficient authentication or physical evidence |

The agreed observer freshness bound is 60 seconds, with integer microseconds
under CLOCK_BOOTTIME and the current observer boot. The original capture retains
its existing finite seconds representation. An expired original inference
deadline does not prohibit bounded cleanup observation and is never renewed.
The closed recursive schema is now frozen at canonical SHA-256
`92791f45ef6a319a27a5aade363832b737978080826537b8a1ffa7eef700cd63` in
[observation.schema.json](../../lab/llm/contracts/no_admission_observation_v1/observation.schema.json).
Its standalone shape/hash validator passed 72 CPU tests. The legacy admission
record schema remains exactly
`f2a3d671f8f56aa70833e59a51783254fd962dd4721d6a315199c8f219ddbe06`.
These are metadata tests, not real closure or GPU acceptance. No current
capability may claim this feature before its producer and consumer are wired
and verified.

## Parallel ownership and direct coordination

The Scientist session owns the wire/schema, canonical observation producer and
the late-admission fence inside the existing admission transaction. The AOS
session owns its consumer, migration and explicit new source-profile review.
Both develop CPU paths behind default-deny composition while exchanging actual
schema/source hashes and terminal test results directly. AOS source is read-only
to this session. Neither source review nor matching hashes grant runtime access.

The old pending request remains retained. No native/GPU work restarts until both
projects independently accept its trusted closure. Scientist remains the sole
integrated GPU-test executor; normal user services are not interrupted.

## Scientist CPU implementation

`NoAdmissionObservationStore` composes the existing ControlStore database. Its
observation transaction calls separately configured current-authority,
immutable-original and physical readers before and after insertion. All are
required; absent readers deny. Deferred/quarantine inventory is an independently
retained internal preimage, not an incoming observation claim. The store writes
only its append-only observation table; it never creates an admission, budget,
reservation or GPU token.

`ControlStore.register_intent` checks the tombstone after BEGIN IMMEDIATE and
before any admission mutation. The 64 CPU tests include both admission/observation
race orderings, real isolated process exit before/after commit, revocation,
immutable retry, malformed SQL/view impersonation and unchanged other tables.
These fixtures are not actual host cleanup providers. Production composition,
AOS consumer acceptance and real closure of the pending request remain open.

The explicit `no_admission_observation_candidate_v1` source profile requires the
previous 63 files plus the AOS consumer, frozen schema and migration 0028.
It checks inherited methods, the new synchronous verifier/journal API and exact
wire version/schema pins. An explicitly required profile rejects an older
receipt before preparation and is rechecked after preparation. Static review
still sets admission/feature acceptance false. The related 151 CPU tests passed.

The complete Scientist quality gate passed all seven commands, including
**2,447 passed / 7 skipped / 121 deselected / 54 warnings** in the CPU suite
(118.05 seconds). GPU/live tests were not run. Full evidence:
[quality-gate-latest.json](evidence/quality-gate-latest.json).

Source review base pair: Scientist
`5389903fac198780234aa87b8b6091d020601c4d` → this CPU delivery; AOS
`ed6e857b0e61e9c19c8ba63933e2cc9f318fe444` with ongoing local changes.
Scientist staged source diff SHA-256 (scope `lab scripts tests`, against the
base Scientist commit) is
`5b9e3eaf2cd7901f6fcd0993b4f788a547e062e21e0a67ceffdd1d4d87bcb29a`.
The 10-file source manifest SHA is
`06cbec5aca07a93b44261856f18b4057a1e2683f987ca8bf8361bf90cc35ddcb`.
The final AOS source66 snapshot was subsequently checked against the actual
checkout with the new Scientist preflight: exact 66-file map, canonical schema
SHA and public API/version markers passed. Selected source SHA:
`0af19f00929de75ca8cd4b1cedfeaa9c54156a81f720028a766765011b208f1f`;
tracked diff SHA:
`02fd4cbc8920cd1d0b6b912150f01e279f918a2fd2723d8d2e0deed972d355bf`.
The AOS session reported 71 targeted CPU checks and 5,294 package checks passed;
they were not rerun by Scientist. Admission and feature acceptance remain false,
and real runtime capability confirmation is still pending. Independent review:
[source66 evidence](review-evidence/aos-source66-no-admission-independent-review.json).

No models ran in this CPU delivery; model settings,
VRAM peaks and native wait/handoff latencies were not remeasured.

## AOS consumer placement

Use a separate observational verifier and append-only historical closure path,
not a relaxed `ScientistBudgetWitnessVerifier` or running-task intent binding.
The AOS session owns the consumer and its migrations; Scientist does not write
the AOS database or restart the old runtime.

Independent callbacks must verify the actual observer/source/capability, original
capture, canonical tombstone and physical proof. A distinct, explicitly reviewed
cleanup-only recovery authority binds the same original store/session/runtime
and its current stopped generation. It does not authorize inference, a new
runtime, old-generation mutation or adoption of a different pending request.

After exact-target verification, append the closure while retaining original
intent/capture bytes. Recheck authority after insertion, rolling back on
revocation. Duplicate identical observation is idempotent; conflicting proof
cannot overwrite it. No reopening until both projects accept this real path.

## Required evidence

Wrong principal/request/hash/source/version; stale/reused generation; duplicate
or conflicting proof; row/child/deferred admission appearance; cancel/timeout;
crash before/after commit; failed cleanup; post-insert revocation; late admission
after tombstone; no budget or GPU authority creation; immutable originals.

CPU/mock success is not actual observational closure. Real closure must bind the
retained original request and actual canonical/physical evidence. Only then may
Scientist coordinate a new bounded native request under fresh source/runtime pins.
