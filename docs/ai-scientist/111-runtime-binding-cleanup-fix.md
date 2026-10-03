# Actual cleanup denial: runtime binding inventory

2026-10-01. The coordinated cleanup-only observer was launched once with the
reviewed command and narrow authority. It exited2 before reserving a proof
file or constructing the observation store. No observation table/row, AOS
closure/migration, model or GPU operation occurred. The original request stays
pending in schema27; the expired consumer waiter made zero consume attempts.

## Cause and fix

Readonly diagnosis reproduced `canonical_inventory_unknown`: the real shared
database has a tenth request-bearing table, `gpu_runtime_bindings`, omitted by
both independent readers' nine-table inventory. This is runtime launch evidence,
not unrelated metadata. Native runtime persists the same scheduler lease
request ID before launch; uncertain launches retain quarantine requirements.

Scientist's atomic store and independent provider now check
`gpu_runtime_bindings.request_id` with no owner, fencing-token or state filter.
Missing/unknown tables still deny. Both checks around the transaction inspect
this surface; a late inserted binding rolls back the observation. Unrelated
runtime rows are retained. AOS independently made the same inventory correction.
The closed wire schema92791 remains unchanged: its complete-provenance promise
does not enumerate table names; the incomplete implementation was corrected.

Historical dispatch review additionally requires `native_runtime.py`. Its e48
Git blob, current file and original107-source receipt independently match
`73480665b79732a95e79c5914b7a34bd894d1fc9e100c1a9704a413007c696ab`.
The CLI now exposes only allowlisted provider denial codes; private and unknown
exceptions remain redacted.

## Evidence and current preparation

- Targeted store/provider/wire checks:205 passed; CLI boundary checks:10 passed.
- Mandatory seven-command gate: all exit0; **2519 passed /7 skipped /
  121 deselected /54 warnings**,179.79s. GPU/live cases did not run.
- Actual original target has zero native runtime binding rows; original
  caller/broker generations and the failed observer PID were proven absent.
  This is not GPU release or trusted closure proof.
- Corrected AOS selected66 source:
  `df7d865c4d171d9df16c3beb4e1436dfabb08fedfa361d67727f6476f3cc0a80`.
- Fresh private source76 configuration:
  `bd64bafb6e83bc6e04c80ae4def0c20ff496f68770d0a05aeb3ebeb0f0ece279`.
  Actual readonly CLI inspection, ten-surface absence, six historical source
  review and complete Git/AST preflight passed. Admission remains false.
- New dispatch-review SHA:
  `8125d1e126c6cb12acc22cc6684108057852d6cc7c50f030e1c130f198745a80`;
  new launch review SHA:
  `8a6b253a9facc544dd365d8df8fe862485803cdaf7d780d82a8d6ac4674d586c`.

The first source preflight call omitted the required selected-untracked pin and
was rejected. The complete, independently reviewed snapshot subsequently passed
all three pins and both source reads; that denial is retained as evidence.

## Remaining acceptance

Old source75/configc8 and approval8a are stale/expired; they cannot authorize the
repaired source or another process. No blind restart, renewed inference deadline
or recovery of a committed observation is attempted. The first attempt produced
no observation: a new explicit cleanup authority and reviewed consumer readiness
are required before a controlled attempt with the repaired source.

Only after complete live proof, original-row preservation, AOS migration28/
closure and physical observer/consumer shutdown are verified may the separately
budgeted native/GPU acceptance proceed. Historical postmint-crash recovery,
research/holdout, training, license and general CI acceptance remain separate.
