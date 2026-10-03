# Entered shared-launch binding

Status: source implementation reviewed; complete gate passed. No listener, model call,
GPU allocation or runtime deployment was enabled by this change.

## What changed

The existing arbiter database now records the actual entered shared Desktop
generation against its original consumed launch: MainPID, start ticks, boot,
unit invocation, cgroup and PID namespace. Entry is immutable, with exact retries
returning the original state. A different service generation cannot inherit it.

The launcher registers entry before native verification, rechecks authority
before publishing the artifact receipt and before Desktop main, and retains a
bounded verifier in the per-call artifact authority. The broker independently
checks the entered generation in the existing scheduler transaction. Missing
composition denies shared admission; legacy acceptance retains its own checks.
Explicit revocation retires an unallocated request; unavailable observations
remain unavailable. Neither case fabricates physical release. Active cleanup,
fencing, quarantine and the single GPU allocator remain authoritative.

Manager liveness is necessary for claim, but is not a lifetime requirement for
an already entered service. Each check still verifies current policy, sources,
profiles, broker, service generation, original scope and original deadlines.

## Acyclic launch configuration

`shared_launch_runtime` in the reviewed native launch input contains only:

```json
{
  "schema": "scientist.shared-launch-runtime.v1",
  "request_id": "<original 32 lowercase hex characters>",
  "binding_path": "/private/review/shared-launch-binding.json",
  "transport_schema_sha256": "53844d314db2080cea681745e95179730b5083ba9e98b33528f1d7995bdeab3f",
  "socket_path": "/private/review/shared-launch.sock"
}
```

This is a shape illustration, not an executable or approved runtime config.
Choose the request ID and paths, freeze the config/source inputs, then produce
activation/intent/binding. The canonical private binding document is a selector;
the broker independently pins the original binding in its private launch policy.
Do not put the binding digest or intent digest in an upstream config hash map:
the binding already contains the reviewed launch-input digest. The document must
also remain outside the session directory and activation/factory config closure.
The client cross-checks its contents against the independently captured broker,
MainPID and verified scope, including workspace device/inode/owner.

## Wire and AOS expectations

The draft transport is **v2**, schema
`aos-scientist.shared-launch.transport.v2-proposal2.draft`, SHA256
`53844d314db2080cea681745e95179730b5083ba9e98b33528f1d7995bdeab3f`.
The change adds `enter` and `verify_runtime`, both requiring the exact original
intent digest. No caller-supplied process identity is trusted. Old v1 framing is
rejected before dispatch; this is not a fallback negotiation.

AOS source was observed at `58587af9352d4f0e96f46e3a350f49cbe54b0499`
with concurrent local edits. Its v2 review adapter was present only as a local
patch at observation time. This is not a frozen or deployed commit pair. The
115-file closure used by the CPU compatibility check has manifest SHA256
`536d2a2cdc1aa291b9506b932c11d014c52444ef717a01351eb19924d9907540`;
the whole tracked AOS diff then hashed to
`b8900a631546b0586d6063b27bef268ae8bdd9607327843599517e177a1fea85`.
That diff does not cover untracked files and is not a weights/dependency manifest.

Subsequent readback observed clean AOS publication
`a1868860ad24ae226a6d42ec9737720fb1fa0b0d` with explicit review2.0 support.
All 115 tested source hashes remained unchanged. The clean tracked diff is
empty (SHA256 `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`).

AOS must explicitly select the same v2 descriptor and preserve fresh-claim-only
spawn behavior. Current cleanup readback must remain repeatable after stop; the
new AOS observer/environment code was read as source, not exercised as physical
cleanup proof. Its original spawn fence and Scientist worker/token closure still
need production composition. AOS idle/quiesce is never release evidence.

## Evidence and remaining work

- 135 actual-checkout CPU compatibility/launcher checks passed; native model,
  service and broker hooks were inert test stand-ins.
- 69 client checks passed, including private local sockets, version/identity
  mismatches, lost ACK, original clocks and replacement races.
- 24 broker admission checks passed, including real private policy revocation
  and subsequent Lab queue progress; external service observations were fixtures.
- Complete source gate: **3,954 passed, 54 skipped, 177 deselected**; all seven
  commands exited zero, including strict typing and wheel build/import. Final
  source review found no blocker. The isolated gate service exited successfully,
  MainPID0, with all 14 changed Python files unchanged throughout the gate.
  [Curated source hashes, gate and limitations](review-evidence/entered-shared-launch-20261003.json).
- The trusted phase-aware prerequisite provider, existing-broker listener,
  physical closure and never-entered reconciliation remain incomplete.
- Final policy/source/config pair, resource reservation and coordinated GPU
  acceptance remain open. No VRAM peak, model/quantization/context, handoff or
  inference latency is claimed for these CPU checks.

This slice does not satisfy the full M0 AOS or continuous-learning acceptance.
