# 81 — Durable original admission identity

30 September 2026. Scientist source baseline `64b766220ce2c5486a8fe715198ee34d1acb013b`;
AOS observed HEAD `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444` remains dirty with
required runtime files untracked. No AOS files, services, GPU allocations or
model artifacts were changed. The sole GPU allocation authority remains the
existing SQLite `SharedGpuScheduler`.

## Implemented source

Capability freshness is now separate from a stable admission binding containing
the complete original server and caller generations, policy hash, source-map
fingerprints, fixed profile and manifest/config/deployment/response/schema pins.
Capabilities expose both that canonical binding and its SHA-256. Refresh may
change freshness and capability hash without changing the stable identity.

The six-field infer frame is unchanged. The trusted broker admission callback
passes an internal immutable `AdmissionGrant` to the executor; this is metadata,
not a lease or new allocation authority. No caller can add that object to the
wire frame. Its authority is rechecked inside the same SQLite transaction as
the first intent and immediately before committing that intent.

The first intent stores canonical original binding bytes and hash. Exact retries
compare those original pins and preserve the original deadline and budgets;
changed policy/source/server/caller/profile/schema pins fail closed. Controlled
execution without a grant or with a legacy null binding is denied. Terminal
receipts retain the original binding and hash. Cancel-before-infer tombstones
keep their admission binding null and prevent late admission; they are not
fabricated infer admissions.

Infer cannot prove consumption of a particular freshness-bearing capability
hash because that hash is not transmitted. The correlation is equality of the
stable admitted identity with AOS's pre-send journal, followed by authenticated
terminal readback of the same immutable pins. AOS still needs to implement that
journal and validation path.

## Failure and focused verification

Critical review found a blocking peer lookup after the final capability expiry
check. A deterministic CPU regression advanced the synthetic clock from 100 to
161 during the final peer observation, after capability expiry 160. Before the
fix it incorrectly committed an intent: **1 failed / 0.09 seconds**, bounded
parent **exit 1 / 0.269777621 seconds**, source unchanged.

The final expiry check now follows that peer lookup and is the callback's last
step. The focused chain passed **123 tests / 4.19 seconds**, bounded parent
**exit 0 / 4.370273320 seconds**, source unchanged. Coverage includes refresh,
original budgets/deadlines, changed stable pins, transaction rollback, legacy
null denial, tombstones, terminal identity, cache publication, cleanup and
source preflight. These are synthetic peer/process/GPU observations against real
CPU SQLite code, not native GPU or AOS runtime acceptance.

## Exact remaining joint decisions

- Integer boot-scoped clock encoding/migration and integer terminal version.
- Complete recursively closed control/profile schemas and their agreed hashes.
- Authenticated cleanup rights after policy/source rotation, retained-target
  control capacity, and allocated no-child directory cleanup.
- AOS typed control client and additive original-identity/resolution journal.
- Clean, reviewed actual AOS source/configuration pins and scheduler reservation
  before the single coordinated real GPU cancellation/fairness/release run.

[80](80-aos-profile-schema-source-map.md) supplies source-derived profile schema
artifacts and the concrete proposed Bonsai projection. Historical
`--shared-gpu-turns`/`--lab-external-*` isolated-patch flags are absent from the
actual AOS entry point; the current default-deny path uses `--engine scientist`
and an explicitly confirmed broker socket. The read-only source preflight
still returns **exit 2 / unsupported**, not permission to run.

The final mandatory gate passed all seven commands: **1521 passed**,
7 opt-in skipped, 120 GPU/live deselected; strict mypy checked 148 source files.
Bounded parent **exit 0 / 83.714203130 seconds**, source unchanged; wheel build
and wheel-only import succeeded. [Machine summary](review-evidence/aos-stable-admission-source-delivery.json)
retains the source pair, local source-diff hashes and counterexamples.
M0 acceptance totals remain unchanged until the required real acceptance items
are independently proven.
