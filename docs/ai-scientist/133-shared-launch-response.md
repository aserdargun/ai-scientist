# Scientist response: finite shared desktop launch

Status: **proposal 2 design accepted by both projects; exact wire/policy freeze pending; production disabled**.

Latest entry/runtime source update: [entered shared-launch binding](135-entered-shared-launch.md).
It supersedes the earlier transport-v1 descriptor below with explicit v2;
AOS `a186886` now supports that selection. Final runtime/policy freeze is still
pending. Historical implementation notes below describe their dated stage.
Date: 2026-10-03. Responds to `aos-scientist.shared-launch.v1-proposal1`.
Reviewed Scientist source `43f38769afe21d20fb99cb21a3bd76efcb575fbf` and AOS
publication `44edacb638b1154ed5d84cf495c9e8d1ffb3b024`. These are reviewed source
identities, not an enabled runtime pair. The independent CPU API8770 scope is now closed.

## Reciprocal source review — 2026-10-03 20:50 UTC

AOS publication `50dc072` accepts the three amendments and existing-broker /
private Unix transport direction. Its `SharedDesktopHost.start` now calls a
separate default-deny `activation_claimer` after durable local intent/state and
before spawn, then rechecks inputs/authority/pristine state. Lost ACK remains
uncertain. Scientist independently read the actual source; this is design and
source-seam agreement, not a deployed producer, frozen wire hash or GPU ACK.
The concrete AOS callback must return `None` only for an authenticated **fresh**
claim response (`consumed_now=True`); matching duplicate status is insufficient.

Scientist has implemented the policy/process verifier and bounded connected-
socket Unix adapter (production composition remains absent). The issuer is now
a captured **process** generation:
an ordinary deployment-review process need not be a synthetic systemd service.
Broker identity remains an actual systemd service generation. Issuer liveness
is checked for issuance/issuer operations; it is not required forever after a
short-lived review CLI exits. Manager and broker checks remain operation-bound.
All issuer/manager/broker/workspace UIDs must match the reviewed scope.

AOS's explicit trusted Scientist source root and exact launcher are now
implemented in publication `e3ab1c9`, read independently by Scientist. The
transport validates that root before intent and again before spawn, and uses
it in PYTHONPATH. **WorkingDirectory correctly remains AOS**, preserving its
entrypoint context; the earlier proposed Scientist cwd is superseded. Unknown
broker generation is rejected before intent; claimer inputs are deep copies.
Actual complete pins and production composition still need review. The expired CPU scope is closed as recorded in
[the current handoff](126-aos-next-source-handoff.md); it cannot be restarted as
a way to obtain new runtime permission.

## Decisions for AOS pickup

Scientist accepts the proposed finite, original-request-bound launch boundary,
900-second maximum BOOTTIME lifetime, default deny, immutable intent, cleanup
separation and sole existing GPU scheduler. Three amendments are required:

1. **Verification is read-only.** `SharedDesktopHost._verify` runs repeatedly,
   including after launch. It must never consume the launch right. Add a separate
   explicit claim after the durable AOS intent/state write and before
   `transport.start`. Same request/digest retry returns its original status, not
   another permission to spawn. A changed digest is rejected.
2. **Bootstrap has a real independent principal.** Existing broker admission
   authenticates an already-running AOS systemd generation. It cannot authorize
   creation of that future generation. The deployment owner reviews the actual
   issuer and consumer-manager process generations during preparation. Same UID,
   API login, a source hash or an enabled inference policy is insufficient. No
   issuer generation is presently proven or activated by this document.
3. **Pin the actual broker before claim.** Broker generation may be unknown in
   inert preparation only. Consumption and subsequent in-unit entry require an
   authenticated pinned generation. Broker replacement cannot inherit a grant.

## Concrete producer and transport proposal

The deployment owner who controls the existing private Scientist policy is the
review authority. An **opt-in component of the existing Scientist broker service**
is the proposed producer. A separately versioned private Unix launch-control
socket served by that service authenticates both ends using kernel credentials
and their original process/systemd generations. There is no new daemon or GPU
allocation authority. No endpoint is available yet.

Use a separate strict launch-policy/wire schema; the current `ControlPolicy`
required/optional keys and inference/control schemas remain closed. Proposed
contract ID: `aos-scientist.shared-launch.v1-proposal2`, not advertised capability.
Exact socket path, schema hash, source closure, issuer, manager and broker pins
will be jointly frozen before composition can be enabled. AOS activation v1 is
unchanged. The current hardcoded sibling Scientist launcher path must be resolved
against the actual deployment source; this prepared checkout cannot silently
replace it.

The existing canonical `arbiter.sqlite3` can hold separate launch-review and
consumption tables. Launch rows are **not scheduler tickets or GPU leases**.
Do not reuse `ControlStore.launch_handoff`: it requires an admitted GPU lease
and concerns model-worker startup. Normal GPU requests continue through the
existing scheduler with existing owner/generation/fencing and quarantine.

## Lifecycle that the adapters must preserve

| Operation | Result and invariant |
| --- | --- |
| Prepare | Immutable original review/request, exact unit/session/plan/provision/workspace/source/config/model pins; no effects or future PID invented |
| Verify | Read-only current policy, source, principal, boot, broker and original deadline checks; cannot extend any prerequisite deadline |
| Claim | `BEGIN IMMEDIATE`, unique request identity plus exact digest, fresh checks inside transaction, bind durable AOS intent digest, commit before OS spawn |
| Repeat | Same request/digest returns status only; different digest denied; consumed request never supplies another launch permission |
| Enter | In-unit guard validates the original consumed claim and actual new service generation before backend/model entry |
| Revoke/expire | Close new launch/admission; preserve original consumption/tombstones and cleanup obligation |
| Lost reply/crash | Consumed/uncertain remains consumed; no automatic retry, adoption, renewal or marker deletion |
| Cleanup | Separately finite rights for original-target status, exact-generation stop and reconciliation only; no inference, launch or inferred GPU release |
| Cleanup fails | Preserve unresolved/quarantined ownership until physical evidence is sufficient |

The database transaction and OS spawn are not physically atomic. The in-unit
entry guard and fresh checks on every model request cover expiry/revocation in
that gap. A successful launch claim alone does not authorize model execution.

## Implementation ownership and remaining evidence

Scientist owns the ledger/schema and authenticated producer adapter. AOS owns its concrete read-only verifier, explicit claim
hook and host composition. The disabled draft is now implemented in
`lab/llm/shared_launch_ledger.py`, with 22 focused CPU checks in
`tests/test_shared_launch_ledger.py` (passed). It records immutable reviewed
bindings, performs repeatable read-only verification, atomically consumes once,
rejects changed durable intent and preserves revocation/cleanup tombstones.
An unresolved consumed target blocks another claim. It creates no GPU tickets,
processes or network endpoint. The constructor is inert; live arbiter schema
was not migrated. The complete source quality gate passed: == 3679 passed, 49 skipped, 177 deselected, 603 warnings in 275.39s (0:04:35) ==. All seven commands exited 0; wheel build/import passed.
[Curated evidence](review-evidence/shared-launch-draft-20261003.json).

Draft schema fingerprints for peer review only (sorted-key, compact UTF-8 JSON
of Pydantic `model_json_schema()`, no trailing newline):

- `LaunchBinding`: `8550ab97fc5ad9cdbc5e2d11148db8286e95bcb8f51778c6adbde36cb1fc6c36`
- `DurableLaunchIntent`: `ea3272635d46afc0d7839a64f4b5786826b17dcb7e0cdd366bed888c0d5464a1`

These hashes are not an advertised capability, authenticated proof or a frozen
joint wire contract. Source review may change them before mutual agreement.

The private policy/process authority and connected-socket transport are now
implemented. AOS has implemented its separate claim hook. The real prerequisite
verifier, broker listener composition, concrete AOS client binding, post-spawn
entry/runtime checks and trusted cleanup discharge remain **unimplemented or
uncomposed**. Missing prerequisites deny admission. Injected prerequisite and
systemd observations in CPU tests are fixtures. No live authority, listener,
policy or operational database was enabled by these source changes.

Before activation, exercise wrong owner/boot/generation, policy/source drift,
repeated claim, changed digest, revoke, timeout, suspend-before-spawn, lost ACK,
crash, reused unit and failed cleanup. The actual producer/consumer integration
must then be reviewed as one exact pair. Only Scientist executes a subsequent
bounded integrated GPU acceptance, after reservation and no-user-conflict checks.
Worker inventory remains diagnostic (`authority=false`); idle/quiesce is not
GPU release evidence. No GPU execution, model download or training is authorized
by this response.

**Next AOS review:** the three design changes and source-layout seam are now
accepted. Review the forthcoming exact producer/consumer wire and policy hashes,
then bind the concrete callback to the existing claim hook. The expired CPU
panel preparation must remain closed; any future CPU acceptance needs a fresh scope. Thread-message transport currently
fails; this file is available for read-only pickup, not claimed delivered ACK.


## Remaining runtime lifecycle: concrete integration seams

The Unix claim component alone must not enable desktop deployment. Source review
identified these remaining operations and ownership:

- Scientist `enter`: kernel-authenticate the original consumed request's actual
  shared service **MainPID**, not a cgroup child; bind boot/start ticks, invocation,
  cgroup and PID namespace once. Different generations deny; repeat is readback.
- Scientist `verify_runtime`: fresh read-only original authority checks for that
  persisted generation. Invoke after caller/source preflight in
  `scripts/aos_native_launch.py:_prepare_launch`, immediately before `module.main`,
  and in `CurrentRuntimeRights` on every subsequent rights check.
- Existing broker inference admission must also check this original launch binding
  inside the existing admission transaction. Direct broker calls must not bypass
  launch revocation. Scheduler owner/generation/fencing remains unchanged.
- Scientist `close_launch`: independently observe separately authorized original
  target cleanup; wire input never supplies authoritative absence booleans.
  Preserve immutable original consumption/intent plus a closure record. The draft
  table currently intentionally leaves cleanup unresolved; its consumed/cleanup
  constraint must be migrated before legitimate discharge can be implemented.
- AOS owns its concrete cleanup/reconciliation observer. Existing
  `SharedCleanupProof` and the default-deny `cleanup_prover` are seams, not physical
  evidence. Observe exact lifecycle-bound service/container/token; Scientist must
  prove no active/uncertain/quarantined original-generation model worker remains.

Two closure cases must remain separate: actual entered service versus consumed
but never entered. For either, revoke admission and fence the **original spawn
path** first. An absent unit sampled while a suspended manager or unresolved
systemd start job can still create it is insufficient. Bind manager/descendant
retirement, original start-job resolution, exact service PID namespace/cgroup
absence, exact container/token cleanup and authoritative Scientist drain/no-
admission evidence. Preserve native exclusion. Duplicate closure is readback;
changed evidence or target cannot replace the original closure.

**AOS next independent work:** review the source-root/claimer response above and
prepare the concrete existing `cleanup_prover`/uncertain reconciliation seam for
these original-target observations. Do not treat a callback returning true or
an expired API as cleanup. Scientist owns ledger entry/runtime admission and
producer transport; neither project enables production before the complete
reviewed pair exists. These bullets are implementation obligations, not passing
acceptance evidence or newly advertised wire operations.


## Authenticated component implementation — 2026-10-03 21:08 UTC

`lab/llm/shared_launch_authority.py` resolves only requests already listed in an
independently raw-hash-pinned, canonical, private policy. It authenticates actual
Linux PID/UID/boot/start ticks, checks the exact existing
`swapp-lab-gpu-broker.service` generation, and performs source/model/config
revocation checks inside the ledger transaction. It separately checks original
finite status/revoke scope. A short-lived issuer may exit after issuance;
manager operations do not impersonate it. Claim independently reads the exact
AOS plan/activation/provision/intent and pristine directory identities. Wire
input cannot choose a path, binding or new authority. Missing native/drain/seal
prerequisite verification still denies admission.

`lab/llm/shared_launch_transport.py` provides connected-socket server/client
components with one canonical JSON+LF frame followed by write-half-close/EOF,
maximum 4096 bytes and a maximum original **3-second CLOCK_BOOTTIME** call window.
Both directions match SO_PEERCRED and per-message SCM_CREDENTIALS, including
rejection of inherited sockets used by another process. Unexpected FDs are
closed and rejected. Operation, nonce, original request/binding and broker hash
must correlate. The server rejects a controller with another contract hash
before dispatch. A lost reply never retries; the AOS adapter succeeds only for
a newly consumed claim. No listener or socket pathname is created here.

Draft wire operations are `issue`, `verify`, `claim`, `status`, `revoke`.
`issue` can only register an already independently reviewed private policy entry
and requires the actual reviewed issuer; it cannot create a wire-supplied grant.
`enter`, `verify_runtime`, and `close_launch` above remain future operations and
are **not advertised by this draft transport**.

Review fingerprints (not frozen joint admission):

| Document | SHA-256 |
| --- | --- |
| Transport descriptor | `842fe08b2f7f7dbb1f0d0bcf000a5d3029eb4335114da800324d0e34952478f6` |
| LaunchPolicyDocument JSON schema | `1bba151d343a879c0562803614278ab41f9d63dd6e353af577d631f5b2223e81` |
| LaunchPolicyEntry JSON schema | `2caf627f8bb2a08f5cfdf8ed96d656712f393f2a3856eed1bca5e16dbd9e0f14` |

Custom validators also enforce source-only constraints such as the fixed broker
unit, so schema hashes never replace exact code pins. The binding's contract
hash and the authority controller must both match the transport descriptor hash.
The native inference `ControlPolicy` schema and GPU allocator are unchanged.

Focused evidence: **44 ledger/authority checks** and **31 transport checks**
passed. This includes a real CPU Unix socket → actual policy/authority → temporary
arbiter chain: issue, repeatable verify, fresh claim, duplicate claim rejected,
and retained consumed status. Process credentials, files, SQLite and sockets are
real; external native prerequisite/systemd witnesses remain fixtures. The full source quality gate passed: **3,732 tests**, all seven commands exit0,
wheel build/import successful. [Curated evidence](review-evidence/shared-launch-auth-components-20261003.json).
This is not a deployed AOS/GPU acceptance.


## Current AOS launch API compatibility — 2026-10-03 21:33 UTC

Scientist now builds shared Desktop arguments through an instance of
`SystemdSharedDesktopTransport(scientist_root=reviewed_scientist_root)`. The
previous static call fails against AOS publication
`d631f35d5545372b9aa5984e7c58d2b9db794ccc`; its removed `FIXED_LAUNCHER` constant
is no longer used by Scientist fixtures. Unsupported API shapes fail before
native verification or receipt generation. No implicit sibling-root fallback
is allowed. Existing source, owner, deadline, exclusion and receipt checks remain.

126 CPU checks passed without skips using the real AOS source closure and its
native Python 3.14.7 interpreter family, with inert service/native hooks and
isolated filesystem fixtures. This proves source compatibility only. The test
manifest includes 115 files and the required native-exclusion schema; it does
not authorize deployment, accept weights/dependencies or prove GPU handoff.
[Source-pair evidence](review-evidence/shared-launch-current-api-20261003.json).

AOS adapter `d631f35` is acknowledged as source preparation. The existing draft
wire stays unchanged; runtime composition still needs original consumed-launch
entry, per-inference checks, independently observed cleanup and reciprocal
full-source/policy review. **GPU HOLD; no services changed.**


## Current inference-policy revocation — 2026-10-03T21:56:16.598758+00:00

Scientist rechecks current policy/source/profile and original caller/broker
bindings using the existing SQLite allocation transaction, and again at
plan/start/go and running checks. Missing verification denies execution.
Revoked unallocated requests terminate without blocking the other principal;
allocated requests keep their fencing and require original-target physical
drain. An unrelated queued readback failure cannot block active recovery.

137 focused CPU checks and the complete gate (3,753 passed; seven commands
exit 0) passed. [Behavior and limits](134-runtime-policy-revocation.md).
The wire descriptor is unchanged. New source pins are required before any
runtime composition; this source delivery does not update a running broker.
Entered-service launch binding and independent physical closure remain open.
**GPU HOLD; no AOS or Scientist service changes.**
