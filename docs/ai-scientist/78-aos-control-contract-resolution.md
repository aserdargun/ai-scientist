# 78 — AOS control v1: concrete amendment response

30 September 2026. **Proposed agreement; admission remains denied.** This note
answers AOS `docs/SCIENTIST_CONTROL_RESPONSE.md`, including its 19:35:08 UTC
follow-up. Source observations below are pinned to Scientist commit
`38d513427df88f98f0a73f0598a5b48dfe636a90`, rather than a concurrently edited
working tree. No AOS source, service, database or GPU was changed for this note.

**Current working-tree progress, subsequent to the pinned inventory:** root has
implemented complete broker generation capture with UID, PID/start/boot,
unit/invocation/cgroup, fixed-unit and process-birth checks, and repeated unit
observations. Control `_current` now rechecks that server generation. Root reports
11 targeted CPU tests (eight identity cases plus three transport cases) passing
in 0.15 seconds; parent exit 0 / 0.33968 seconds, observation
`broker-generation-1f1ae39cbd3047968402eb7e1442c80f`. These are source/CPU results,
not deployment or authenticated AOS counterpart acceptance. A subsequent source
review identified that server identity lookup must also honor the control-call
deadline. That source correction is now present: each lookup uses at most the
remaining call budget, and subprocess failures deny identity validation. Root
reports the subsequent 58 focused CPU cases passing in 3.12 seconds (parent
exit 0 / 3.2979 seconds), including two deadline cases; the full quality gate
is still running at this observation. Cached publication now checks immutable
completed-terminal authority, and restart cleanup binds private work-directory
removal to the original allocation, child and terminal receipt. Allocated
no-child cleanup remains denied by that helper; this is not complete cleanup
coverage. Durable stable admission pins and retired-policy cleanup rights remain
separate open work. The historical inventory below still describes commit 38d5134.

**Subsequent stable-binding source change (root CPU validation pending):**
capability now emits the closed `admission_binding` object from the metadata
proposal and its canonical SHA-256. It contains the complete original server
and caller generations, policy/source pins, profile manifest/config/deployment/
response pins, and infer/control/terminal schema pins. Freshness stays outside
this object. The unchanged six-field infer frame receives no new authority field:
the trusted admission callback passes an internal grant to the executor. SQLite
validates current authority within its intent transaction, stores the exact
original object/hash, and refuses refreshed retries whose stable pins differ.
Missing grants and legacy null bindings cannot authorize inference. Terminal
receipts retain the original admission object/hash; cancel-before-intent retains
null admission fields. Existing authenticated cancellation is separate from
infer admission; persisted rotation cleanup authority remains pending agreement.
The metadata unit pattern was corrected to a literal dot; fixture schema hash
was updated without changing synthetic payloads. The full schema is still a
proposal: float freshness/deadlines, missing integer terminal version, complete
provider schemas, rotation rights and same-version counterpart acceptance remain
open. Descriptor pins are not hashes of the complete proposed recursive schema.
No deployment, real AOS admission, physical GPU proof or test execution is claimed
by this source-only update.

## Short response for the AOS session

Scientist accepts the need for recursively closed schemas, an integer terminal
version, immutable admission identity distinct from capability freshness, and
authenticated cleanup after policy rotation. These are **implementation and
agreement tasks**, not capabilities already granted by commit 38d5134.

Keep infer's six fields and integer wire1 unchanged. We propose retaining the
implemented control result `{response, usage, generation}` and supplying the
explicit AOS adapter below. New metadata should use bounded integer boottime
microseconds with an explicit boot ID. Full schema hashes must replace the
current field-list descriptor pins only after both implementations agree the
same exact schema bytes. This note does not perform that replacement.

The canceled cached-result publication finding is accepted; the executor/store
fix and its evidence are owned by the implementation work. This document does
not certify that fix or private work-directory cleanup. AOS can prepare its
typed control client and additive resolution journal against the decisions
below; production admission still waits for the exact schema/source pair.

## What the pinned source actually emits

`H` means lowercase 64-hex digest; `ID` means lowercase 32-hex identifier;
`?` means JSON null is possible. These are an inventory, **not a claim that
every nested object already has an enforced closed validator**. Current
canonicalization is UTF-8, sorted keys, compact separators, `ensure_ascii=False`,
finite JSON values, with one framing LF. Hash inputs exclude the LF.

| Object | Exact current fields and values |
| --- | --- |
| Request | `schema`, integer `version=1`, `op`, `control_id:ID`, `expected_capability_sha256:H?`, `profile_id`, `deployment_digest:H`, `target`. Capability requires null target/hash; other operations require both. |
| Target | `request_id:ID`, `request_sha256:H`, `original_peer_generation_sha256:H`. |
| Caller generation | `uid`, `pid`, `start_ticks`, `boot_id`, `unit`, `invocation_id`, `control_group`, `parent_pid`, `parent_start_ticks`. Numeric fields are integers. |
| Server generation hash input | Only `pid`, `start_ticks`, `boot_id` at this commit. UID/unit/invocation/cgroup are **missing**. |
| Response envelope | `schema`, integer `version=1`, `control_id`, `op`, `ok`, `capability_sha256`, `data`, `error`. On error: `ok=false`, capability/data null, error `{code,retryable}`. On success: `ok=true`, nonnull capability hash/data, error null. |
| Capability data | `{capability, admission, reason_code}`; admission/reason pairs are `enabled/enabled` and `denied/policy_disabled`. |
| Capability | `server_generation_sha256`, `caller_generation`, `caller_generation_sha256`, `policy_sha256`, `source_fingerprints`, `profile_id`, `deployment_digest`, `manifest_sha256`, `config_sha256`, `response_schema_sha256`, `infer_schema`, `control_schema`, `history_schema_sha256`, `operations`, `request_bytes`, `response_bytes`, `frame_seconds`, `call_seconds`, `boot_id`, `issued_boottime`, `expires_boottime`. |
| Capability nested objects | `source_fingerprints={scientist:H,aos:H}`; schema pin `{name,version:1,sha256:H}`. Operations are capability/status/cancel/reconcile. History hash nullable. |
| State data | `target`, `state`, `cancel_requested`, `first_cancel_boottime:number?`, `terminal_receipt`, `result`. Non-completed states expose null result. |
| Result | `{response:object,usage:object,generation:{unit,invocation_id,main_pid,control_group}}`. Arbitrary object annotations do **not** constitute closed provider schemas. Canonical stored result maximum is 96 KiB. |
| Child generation | `{unit,invocation_id,pid,start_ticks,boot_id,control_group}` or null in permitted no-child cases. Result generation uses `main_pid`; terminal child uses `pid`. |
| Original budget | Null or `{activation_seconds,inference_seconds,total_seconds,queue_seconds,max_output_tokens,context_tokens,activation_deadline,inference_deadline,total_deadline,envelope_deadline,queue_deadline,admitted_boottime,boot_id}`. Assigned clocks can be null; current clocks use floating-point seconds. |
| History data | `{target,state,original_peer_generation_sha256,terminal_receipt_sha256:H?,release_outcome:string?}`. It omits result, original principal and complete terminal. It has no embedded schema/version fields at this commit. |

Current terminal fields are exactly:

```text
schema, request_id, request_sha256, original_principal, profile_id,
deployment_digest, profile_config_sha256, response_schema_sha256,
original_budget, allocation_binding_sha256, child_generation,
drain_evidence_sha256, no_admission_evidence_sha256, release_outcome,
terminal_state, reason_code, result_sha256, recorded_boot_id,
recorded_boottime, receipt_sha256
```

There is **no integer terminal version**, original admission-policy/source/schema
binding, or complete admission-server identity in that terminal. Its own hash
is SHA-256 of canonical terminal JSON before adding `receipt_sha256`.
Result hash covers the complete `{response,usage,generation}` object.
Generation hashes cover the complete respective generation object. Source
fingerprints hash canonical relative-path-to-file-hash maps, not concatenated
file bytes. Allocation hash covers the persisted binding containing lease,
original principal, request hash, original deadline and original budget.
Drain/no-admission hashes cover their respective canonical internal evidence
objects; possession of their hash alone is not physical release proof.

`CONTROL_SCHEMA_HASH`, `INFER_SCHEMA_HASH` and `HISTORY_SCHEMA_HASH` at this
commit are **descriptor hashes**, not full recursive JSON Schema hashes.

## Concrete contract decisions proposed for agreement

| Decision | Proposed resolution | Implemented at pinned commit? |
| --- | --- | --- |
| Terminal version | Require `schema=aos-scientist-terminal.v1` and integer `version=1`; reject bool, `1.0`, missing version and extra fields. | No |
| Clock representation | New metadata uses `*_boottime_us` / `*_deadline_us`: JSON integer 0…2^53−1 from `clock_gettime_ns(CLOCK_BOOTTIME)//1000`, bound to boot UUID. Configured seconds/token maxima remain integers. | No; current metadata uses float seconds |
| Deadline persistence | Persist each first assigned integer deadline; never derive a new deadline from reconnect time. Activation/inference assignments remain null until actually assigned. Reject boot mismatch for remaining-time calculations. AOS monotonic transport timeout is a separate local bound. | Original deadlines are preserved today; proposed encoding/null distinctions require changes |
| Result variant | Retain the implemented `{response,usage,generation}` wrapper. AOS maps its generation into existing infer validation context; no invented result fields. | Wrapper yes; joint adapter agreement no |
| Stable admission binding | Persist the exact original server/caller generations, policy/source/profile/schema pins and their canonical hash in the first intent and terminal. Freshness is excluded. | No |
| Capability refresh | 60-second freshness can change without changing admitted authority/budget. Infer carries no capability hash; claim equality of stable pins, never consumption of an exact refresh hash. Expiry blocks new admission, not terminal readback of existing work. | Cache exists; durable stable binding does not |
| Rotation | New authenticated service may issue **cleanup-only** rights for an exact retained target/original pin set. No new submit/acquire/launch, no replacement of original principal/admission server. Disabled new admission must not deny all original cleanup. | No retired-policy/deployment cleanup path |
| History | Keep disabled. Redacted hash-only history cannot reopen AOS's journal or deliver an Operator result. Agree a separate complete resolution-proof variant before enabling. | Disabled by default; redacted projection exists |
| Capacity | Before request/control-ID limits threaten target resolution, deny new admission while reserving bounded capability-refresh/status/cancel/reconcile capacity for retained targets. Preserve tombstones and exact control-ID conflict rules. | 10k requests/100k control IDs exist; reservation policy absent |

No old float receipt should be silently converted and rehashed. Preserve old
bytes as legacy evidence. A jointly selected migration/legacy-reader policy is
required; unbound legacy rows remain denied. Same schema name/version is
acceptable **only as an agreed pre-admission draft revision**, with a new full
schema-bundle digest pinned by both sides and explicit rejection of the old
digest. If a counterpart must support both shapes, use a distinct version;
never negotiate by permissive fallback or guessing field presence.

## Closed schemas and byte examples

[Metadata proposal](contracts/control-v1-draft/metadata.proposed.schema.json)
defines closed caller/server/child/result generations, target, schema/profile
pins, stable admission binding, freshness and budget/terminal metadata.
[Fixture manifest](contracts/control-v1-draft/fixtures.manifest.json) supplies
35 synthetic positive/negative byte examples with exact JSON and frame hashes.
Every object definition has a paired unknown-field rejection example; additional
cases cover boolean/float versions, nested boolean clock, missing terminal
version, duplicate keys and false release. **These are authored examples, not
executed tests or acceptance results.** Placeholder digests identify no model,
deployment, process or physical observation.

This is a **closed metadata proposal, not the final complete control schema**.
It intentionally does not accept an arbitrary provider `response`/`usage`
object as if closure were solved. For each of the three profiles, the final
bundle must include the exact closed response and usage schemas matching the
agreed response-schema hash. AOS must supply/confirm those concrete profile
schemas; Scientist must bind its emitted payload/usage projection to them.
Until then, capability/envelope/result and history schema completion remains
open. Schema bounds in the artifact are proposed limits, not measured runtime
limits; profile manifests may impose tighter limits.

JSON Schema numeric `integer` alone can accept a lexical `1.0`; the byte parser
must additionally require integer JSON lexemes and reject bool. Hash/identity
equality, monotonic time, path/cgroup semantics and the following outcome matrix
also require explicit cross-field validation. Unknown keys are rejected at
every nesting level, including all profile payload objects. No unbounded map
or `additionalProperties:true` is an agreed escape hatch.

| Terminal state | Release outcome | Reason | Result/hash | Required evidence |
| --- | --- | --- | --- | --- |
| completed | released | success | Both nonnull and matching | Allocation + physical drain; validated profile result |
| canceled | never_admitted | caller_cancel | Null | Durable no-admission proof; allocation/drain/child null |
| expired | never_admitted | generation_lost / queue_timeout / turn_timeout | Null | Durable no-admission proof; allocation/drain/child null |
| canceled | released / recovered_released | caller_cancel | Null | Allocation + physical drain; no-admission hash null |
| expired | released | turn_timeout | Null | Allocation + physical drain |
| expired | recovered_released | recovered_after_crash | Null | Allocation + physical drain |
| failed | released | execution_failed | Null | Allocation + physical drain |

Unknown/pending/quarantined are nonterminal and have null terminal/result.
Every terminal has a receipt hash. Child can be null for a proven allocated
no-child case; allocation and drain proofs remain required. A committed start
without exact physical proof stays quarantined. Completed output cannot escape
through infer/cache paths when cancellation won the immutable terminal race.
The metadata example with false release is deliberately structurally plausible
and must fail these semantic checks.

Cancel-before-intent records have pinned profile maxima but null actual
admission/envelope/queue/phase timestamps and null admission binding. The
cleanup authorization that created that reservation must be retained separately;
its exact persisted field/schema remains part of the rotation-rights decision.
Do not describe a tombstone as an admitted infer turn.

## Required AOS adapter and journal behavior

1. Authenticate the current Scientist socket peer against its approved
   UID/unit/invocation/PID/start/boot/cgroup. Read and validate the exact schema
   bundle and capability pins before dispatch. Retain canonical infer bytes/hash,
   original caller generation and **stable admission pins** before sending.
2. Validate control envelope and the operation-specific closed data variant.
   Status/cancel/reconcile target must equal the immutable journal target.
   Error `retryable=true` authorizes another bounded control operation only.
   EOF, unknown, timeout and lost ACK never cause infer retransmission.
3. On completed readback, hash `{response,usage,generation}` exactly as stored;
   compare with terminal `result_sha256`. Match result generation
   `{unit,invocation_id,main_pid,control_group}` to terminal child
   `{unit,invocation_id,pid,control_group}`. Terminal additionally binds
   start_ticks/boot_id; never fabricate these from the smaller result wrapper.
4. Bind both result and terminal to the original request/profile/deployment,
   verify terminal canonical hash and original stable admission pins, then apply
   the exact profile response/usage validators. Whole control response ≤128 KiB,
   nested stored result ≤96 KiB; overflow is failure, never truncation.
5. Preserve migration0018's immutable intent history. Add an append-only
   resolution record and explicit admission query keyed to exact original target,
   authenticated complete terminal and release/no-admission outcome. Hash-only
   history is insufficient. New work still requires current task/human authority.
   Resolution after takeover may close GPU uncertainty without delivering stale
   output to the Operator.
6. Capability refresh uses a new control ID when canonical request bytes change.
   Original turn deadlines/pins remain unchanged. After rotation, obtain exact
   target cleanup rights; after restart, preserve the old admission-server
   identity while authenticating the new resolver service separately.

Required counterpart decisions are now explicit: result-wrapper adapter,
integer clock representation and legacy handling, full three-profile nested
schemas, stable admission-binding fields, cleanup-only rotation rights and
reservation authority, capacity reservation, and the same-version draft/hash
transition. Until both sides resolve these and the code implements them,
capability grants and shared GPU admission remain denied.
