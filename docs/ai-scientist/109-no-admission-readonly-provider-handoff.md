# No-admission provider: concrete CPU handoff

Status: implementation and CPU checks complete; real observer execution,
canonical observation and AOS closure are **not yet accepted**.

## Concrete code and boundary

- `lab/llm/aos_no_admission_providers.py`: readonly configuration, exact source
  pins, original AOS request/admission records, current stopped/PAUSED scope,
  physical absence and independently reviewed historical dispatch ordering.
- `scripts/aos_no_admission_observer.py`: default readonly inspection; explicit
  issuance requires this process to be the live MainPID of the fixed
  `swapp-scientist-no-admission-observer.service` unit. No service is started by
  the CLI.
- `NoAdmissionObservationStore` remains the only observation writer in the
  existing canonical database. AOS owns its separate closure transaction.

The CLI checks real authority/original/physical evidence before constructing
the writer. It reserves a new private output and never overwrites an earlier
proof. After issuance, the observer stays alive until the exact AOS closure is
read back or the original minted proof expires, at most 60 seconds. A timeout
or crash preserves the original request and committed observation; neither
permits reissue, a different observer generation or an inference deadline
renewal. Historical recovery after producer crash remains open.

## Verified CPU checks and real preparation

Thirty provider and seven CLI boundary checks passed. A real readonly inspection
found that AOS's retained `BrokerPeer` has six fields while its admission server
generation has seven, including `unit`. The implementation now compares the
exact six socket fields while retaining the independently pinned full server
generation. Missing, extra, duplicate or altered peer fields reject.

The complete quality gate passed all seven commands: **2,484 passed / 7 skipped /
121 deselected / 54 warnings**, with 158.89 seconds in the CPU suite. GPU/live
tests were not executed. Source hashes:

- Provider: `e037cff472b88bd6db4b93daa799366f4a85805594170899985d8d9a33da2be9`.
- CLI: `86dce8f05a7dfc7222e942e2bd632d3c82645d44a8ca956befea4d8629b2987e`.

The original host database was only read: schema 27, original request pending,
current session stopped/PAUSED at generation 1. The candidate cleanup artifact
was independently checked by AOS against those actual rows. Its canonical/raw
SHA is `0c57c7aa387fc4f7f8f4d50b8714607ac9d750e382bd2b3b07b9c7f15316cdb4`.
That is a shape/scope review, not runtime authority.

The first config candidate was denied and retained after the real peer-shape
correction changed its source pin. A new candidate requires the final AOS source
freeze, corrected provider/CLI pins and independent configuration review.
No private artifact contents or original request payloads are published.

The corrected AOS `bd9cc80cd99881890fc392ccccbb6999b0a28cba83f2ec9e919b5517bf469719`
snapshot subsequently passed the actual 66-file source preflight. A new 75-file
provider configuration passed both the actual readonly provider inspection and
the default CLI inspection, explicitly returning `runtime_authorized: false`.
Config SHA:
`c8f02abf021eefcc17cd2bf4784bfff30f6b2b38e8d8d3cb0044ca6ce110f6c8`.
The expected postmint schema review candidate digest is
`4d4c7a545e7fca29753ac1ba4b42ef364306e876544b2b8dce88f0a0de2fb9a1`,
computed from the actual existing schema plus the exact new table/triggers
without installing them. The producer launch command is a private review plan;
its service has not started.

Consumer phase order is explicit: keep schema 27 through all mint callbacks;
wait for complete committed proof, then apply only the reviewed AOS migration
0028 and append closure within the same minted 60-second expiry. Output-file
existence alone is insufficient because the CLI reserves it before minting.

## Remaining real acceptance

1. Final AOS source snapshot and actual 75-file config inspection are complete.
   AOS accepted the exact-target cleanup source-attestation review, based on
   corroborated driver/argv/startup generation/authenticated capture and the
   mandatory historical service composition. This is an inference from retained
   execution/source evidence, not a direct historical runtime capture. The missing
   historical `/proc` environment
   capture is not presented as retained evidence: the original tool invocation,
   driver, saved startup generation and authenticated server record corroborate
   the launch instead.
2. Consumer command review found that migration 0028 already owns its
   `BEGIN IMMEDIATE` / `COMMIT`; adding another transaction wrapper would fail.
   The owning AOS session corrected its private consumer; raw SHA
   `9939488148b40d4a955bc3e02b0c89a6f8c698cb76eb9bae0440fc04f427af63`.
   Root reviewed the exact selftransactional migration call and the targeted
   CPU RED/GREEN evidence. The actual original database remains untouched
   pending narrowly scoped runtime authorization. The final private consumer
   `bdac05d1ac8762422d38c7f81a828f8428ede88edbbffe6afddbab258e027595`
   requires `review_accepted`; the independent review keeps direct historical
   composition `verified: false` and its missing-capture limits explicit.
3. Run the reviewed, bounded cleanup-only observer and AOS consumer together.
   Prove original rows retained, atomic canonical tombstone and exact AOS
   closure, and physical observer/consumer shutdown. No GPU release claim.
4. Only after real closure acceptance, coordinate the separately budgeted
   native AOS → Scientist → AOS report/GPU scenario and cancellation recovery.

No models, training, GPU queries or GPU allocations ran in this provider
delivery. CPU checks and preparation are separate from native acceptance.
