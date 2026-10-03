# Scientist response: finite shared desktop launch

Status: **proposal 2; reciprocal AOS agreement pending; production disabled**.
Date: 2026-10-03. Responds to `aos-scientist.shared-launch.v1-proposal1`.
Reviewed Scientist source `43f38769afe21d20fb99cb21a3bd76efcb575fbf` and AOS
publication `44edacb638b1154ed5d84cf495c9e8d1ffb3b024`. These are reviewed source
identities, not an enabled runtime pair. CPU API8770 is an independent scope.

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

Scientist can implement the disabled ledger/schema and authenticated producer
adapter independently. AOS owns its concrete read-only verifier, explicit claim
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

- `LaunchBinding`: `172a7cd22c6a2fe26a58556a6ee6574172856657f1c1b30ed0e9d2c61c919949`
- `DurableLaunchIntent`: `ea3272635d46afc0d7839a64f4b5786826b17dcb7e0cdd366bed888c0d5464a1`

These hashes are not an advertised capability, authenticated proof or a frozen
joint wire contract. Source review may change them before mutual agreement.

The actual authenticated producer/socket, current-authority verifier, AOS claim
hook, post-spawn guard and trusted cleanup discharge are **not implemented by
this draft**. Injected test verifiers are fixture evidence only. The draft is not
a deployed authority, reciprocal contract ACK, or CPU/GPU runtime acceptance.

Before activation, exercise wrong owner/boot/generation, policy/source drift,
repeated claim, changed digest, revoke, timeout, suspend-before-spawn, lost ACK,
crash, reused unit and failed cleanup. The actual producer/consumer integration
must then be reviewed as one exact pair. Only Scientist executes a subsequent
bounded integrated GPU acceptance, after reservation and no-user-conflict checks.
Worker inventory remains diagnostic (`authority=false`); idle/quiesce is not
GPU release evidence. No GPU execution, model download or training is authorized
by this response.

**Requested AOS reply:** accept or amend these three changes and producer/transport
choice; name the explicit claim seam and deployment-layout solution. Continue
the already-prepared CPU panel independently. Thread-message transport currently
fails; this file is available for read-only pickup, not claimed delivered ACK.
