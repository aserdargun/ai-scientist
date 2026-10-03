# Coordinated retained closure recovery

2026-10-01. Scientist and the active AOS session communicate directly through
their task inboxes. Scientist owns its source and the integrated GPU acceptance
run; AOS owns its verifier, journal, database and private consumer. Neither
session changes the other's checkout or processes.

## Actual result

### Coordinated runtime attempts, 2026-10-01 21:18 UTC

The first retained recovery authorization expired before launch; no context
was published and AOS made zero consume attempts. A separately authorized V2
witness was then started exactly once. It exited2 before publishing a context.
Read-only diagnosis reproduced a real provider defect: the old CPU observation
unit was passed to the GPU service namespace checker, which rejected its name.
This is a failed runtime attempt, not a successful closure or GPU release.
The new service is inactive with MainPID0 and an empty ControlGroup.

AOS independently recorded that its same bounded V2 waiter expired with exit3
and zero consume attempts. Its database remains schema27, the closure table is
absent, and the original intent is pending with no receipt. Neither session
restarted the witness, renewed the old authority, or reissued the retained proof.

Scientist is preparing a narrowly scoped read-only CPU observer absence check
and a regression for the actual namespace failure. The GPU namespace and
allocation authority remain unchanged. AOS continues independent work while
Scientist prepares this fix and the experiment phase optimization. A subsequent
runtime attempt requires reviewed new source pins and a separate finite
close-only authorization; no previous deadline is extended.

The narrow fix passed166 focused static/provider/CLI tests. Its regression first
failed against the actual GPU namespace restriction. Root then independently
ran the new fixed old-observer absence checker against the real retained
generation; the read-only check passed. Replacement units, PID/cgroup presence,
ambiguous metadata and permission failures deny. Provider SHA is
`a25f259f9d74b9f834da3d26853dd452bb2300304495a95bcf89d5b3d7803a26`.
This independently verifies the corrected absence check, not a fresh runtime
authority or closure. The old81-source configuration is invalid after this
source change and cannot be reused for a launch.

The final combined Scientist source gate completed with all seven commands
exit0:2781 passed /7 skipped /121 deselected /54 warnings,255.08s for pytest;
strict typing covered164 files and the actual wheel import passed. The initial
gate's import/type errors remain in its private log; the successful rerun used
the corrected stable source. Full read-only checks also passed against the real
canonical row, original AOS scope,76 retained and81 current sources, and physical
absence before/after. A distinct V3 close-only plan is prepared for a fixed
60-second window; its actual outcome is recorded below.

### V3 actual close-only attempt, 21:38–21:39 UTC

Scientist launched the approved plan once at BOOTTIME19860444782us. The real
witness PID193931 /InvocationID6d49c1009c3c46c3aa11153dbe4f2a6a published a
context with raw SHA
`105d9376fc27907247fa1f75c3fc903b5c0710fc49e003c3cb559cc251acb65d`.
AOS consumed once and exited2 with a redacted denial before migration. Root's
independent readback confirms schema27, no closure table, original pending
intent and absent receipt. The exact AOS denial cause remains under its
session's investigation; this is not a successful closure.

The witness expired on its original deadline with exit3 and confirmed closure
false. Actual journal:59.980s wall /24.760s CPU /38.6M memory peak. Root verified
its PID is gone and its unit inactive/MainPID0/empty ControlGroup. No retry,
old observation reissue, original deadline renewal, model call or GPU release
occurred. The old proof raw SHA remains77e555000ae10a46fdbea1f7de089e90124d3ff70b28783c28ac29555a8c0ea6.
Both failed actual attempts are retained; AOS owns its next consumer diagnosis
and fix while Scientist can continue independent research preparation.

The reviewed V3 Scientist observer minted exactly one immutable observation.
Its canonical observation SHA is
`a904d3aa2fad3c220b48e1ba304429668a44143e5772fc4968ca514c1284ecef`;
the retained raw proof SHA is
`77e555000ae10a46fdbea1f7de089e90124d3ff70b28783c28ac29555a8c0ea6`.
The canonical row matches the retained proof bytes. This supersedes the
pre-mint preparation described in [111](111-runtime-binding-cleanup-fix.md).

AOS's waiter initially treated the reserved empty output as a terminal error.
One subsequent consumer attempt failed before migration: Docker returned an
empty JSON array with its exact missing-container error, whereas the private
predicate required empty stdout. These failures are preserved. AOS reports
schema27 and no closure. The original request remains pending. The observer
finished its original 60-second lifetime without confirmed closure; its process
is gone. No proof renewal, inference retry or GPU release followed.

Both sessions independently verified the retained 76 source files. Historical
source attestation is accepted with its recorded limits; direct original
runtime composition capture remains unverified.

## Agreed division of work

- Scientist: separate closed static recovery schema/validator and a bounded,
  read-only current authority and evidence provider.
- AOS: dedicated retained verifier and atomic closure journal with independent
  current authority, retained-source, canonical and physical callbacks.
- Existing observation wire92791, original proof, tombstone, observer generation
  and expired observation lifetime stay immutable. Normal live verification
  remains strict.

The agreed feature is `no-admission-observation-recovery.v1`, with purpose
`close_retained_no_admission`. A separate context binds a fresh recovery request,
the exact target, retained proof and schema hashes, canonical store/schema,
historical source/config/observer, current principal/source/config, and the
**same** paused cleanup scope. Its fresh BOOTTIME authority lasts at most
60 seconds. Inference, GPU release and observation reissue permissions are all
false. The external authority pin hashes the entire canonical context; there
is no embedded self-hash. Static consistency alone grants no authority.

Before and after the AOS closure transaction, independent checks must prove
current authorization, unchanged paused owner/generation/lease, the exact old
canonical row, absence at all ten request-bearing surfaces, and physical
absence of the old caller, broker, observer and owned resources. Missing evidence,
revocation, expiry or scope drift denies closure. Migration0028 already supports
the retained context in `recovery_scope_json`; its original scope constraints
must be preserved.

An existing immutable closure is read back after a lost reply. If no closure
exists, a retry needs a separately authorized fresh close-only context for the
same retained proof. Neither case repeats the producer or revives inference.

## Acceptance still pending

The reviewed Scientist implementation included a separate static validator,
readonly provider, bounded witness CLI and source67 preflight. Its prior gate:
**2677 passed /7 skipped /121 deselected /54 warnings**,203.36s; all seven
commands exit0. New modules and the recursive schema are verified in the wheel.
This is CPU/source evidence, not a real closure. The subsequent V2 runtime
failure above requires a provider fix and a new gate; the prior passing gate
does not validate that fix.

The agreed recovery schema canonical SHA is
`6d173eb1231989b3b3304ae08455f9b10c192dc0aa37afa54acccb223615690b`.
AOS HEAD remains `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444`, with selected67
SHA `a8ac2cda5b19d0ce977e47b306758303776aa42d977b3277063f87b64ca43b16`.
Root independently verified the actual dirty checkout using reviewed-snapshot
mode and all three selected/tracked/untracked pins. Source readiness passed;
runtime admission remains false. An initial clean-checkout mode correctly
rejected the dirty tree; it is retained separately. AOS reports176 focused and
5297 package checks passed.

Readonly inspection of an81-source private configuration draft passed. Its
authorization is intentionally expired and cannot start recovery. Canonical
schema pin4d4c remains in its original table/trigger convention; the separate
AOS27/28 full-catalog pins were prepared through an in-memory migration only.
Neither live database was migrated or written during this preparation.

Fresh reviewed authority, the AOS consumer/waiter composition and one coordinated
close-only runtime operation remain pending. Native GPU coexistence,
independent AOS result verification and public research/holdout acceptance have
not passed. Synthetic and baseline results remain separate. Training, license
selection and general CI work are tracked separately.

## Direct peer coordination and V4 outcome

Scientist now sends coordination messages directly to the existing AOS session
and reads its handoff. AOS owns its source, private approval, consumer and DB;
Scientist owns the witness and is the sole integrated GPU executor. Independent
Scientist CPU work continues while AOS investigates its own rejection.

Scientist independently reviewed the AOS waiter encoding correction:
`87d36286aa912c2b2bf0ffa75f8fc4950e95f7de94d2d0af17f4801a1fd63e66`.
The unchanged consumer and new waiter both read the exact canonical expired
approval; the historical unsorted approval is rejected before READY. This
source-only review did not grant runtime authority.

For a distinct V4 plan, AOS created and verified a new canonical approval;
Scientist verified its exact bytes, live waiter PID/start ticks/UID/cwd/arguments,
unchanged81 source pins and absent prior witness before launching once.
The consumer attempted once and exited2. Witness invocation
`c53b09f32c94409f923d68e6731182ac` expired normally with exit3, closure false,
58.592s wall,24.191s CPU,38M peak. Its PID is absent and unit inactive with
MainPID0 and empty cgroup. No inference, model, GPU release, observation reissue
or deadline renewal occurred. AOS is responsible for the rejection diagnosis
and independent DB readback; no automatic retry was performed.

Scientist's independent readback found DB migration27, original intent still
pending, receiptNULL and no closure table. AOS diagnosed a second encoding
failure in the historical cleanup metadata. The new private consumer
`ac91285e8071048a8f213c6218391c2d75e5af8566473e71ffa967d8fac39162`
uses a bounded strict decoder for that exact raw-hash-pinned metadata only;
observation/context and other canonical guards remain strict. Scientist reviewed
the narrow diff and read the actual config, authorization, retained config and
cleanup evidence without modifying their bytes. AOS is gathering all metadata,
source and readonly DB checks before proposing another finite runtime attempt.

## V5: actual close-only acceptance passed

After both sessions verified the complete readonly preflight, Scientist launched
one distinct V5 witness at BOOTTIME22512058741us. AOS's one consumer attempt
exited0 and committed the immutable closure. Witness invocation
`9f2735321c7142d79326b3a2e81d8d38` independently confirmed it and exited0.

Scientist's direct readonly DB readback verified schema28, the exact historical
observation bytes/hash and the exact V5 context in
`recovery_scope.retained_recovery`. Context SHA:
`9d431fc892f73940a5afe6ae4b4c314f8c0c0aa43dab5757157811d6277de66a`.
The original intent remains pending with receiptNULL by design; the separate
immutable closure resolves this close-only path without rewriting that intent.
Witness PID240150 is absent; unit inactive/MainPID0/cgroup empty. AOS independently
confirmed the persistent record and clean exit. Private receipts retain both
sessions' exact source/config/authorization/approval and original failed attempts.

This closes the retained no-admission recovery blocker. It does **not** prove GPU
release, native model execution or fair coexistence. AOS is preparing an updated
reviewable native policy/factory package because historical runnable policy pins
drifted; no inference was authorized by V5 and no new GPU runtime was started.
