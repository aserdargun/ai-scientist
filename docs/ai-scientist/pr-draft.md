# Draft PR: local AI Scientist M0 and isolated AOS integration

Base: `main`

Head: `feat/luna-m0-core`

State: work in progress; M0 acceptance is incomplete. Do not merge.

## Implemented areas

- Versioned harness, vendored metrics, Docker fit/score isolation, independent
  PostgreSQL Scorer, deterministic Referee, durable Director and artifact records.
- CLI/API research control and an isolated typed AOS patch with policy,
  external-job tracking and verified reports for the synthetic integration path.
- Authenticated fair GPU arbitration, bounded native Qwen lifecycle, private
  networking, measured process/GPU cleanup and isolated runtime dependencies.
- Registry-selected local Qwen proposal provider with pinned model/profile
  receipts, real tokenizer admission and durable failure/budget accounting.
- One strict schema-constrained repair, with durable per-attempt recovery,
  aggregate episode token accounting, abandoned-episode continuation, and
  dispatcher failure fencing. Its full image-bound
  quality gate passed; the first actual S2 response was rejected and did
  not produce an accepted proposal.
- Run-bound Director ownership and conservative interrupted-run recovery;
  compatible validation of historical canonical documents and the rehearsed
  0016/0018 migration chain without changing existing run/data rows.
- Real CARE Farm A and bounded SMD materialization, PDM/NRM Scorer metrics,
  family-capped suite manifests, HTML reports and immutable decision replay.
- Durable Thompson selection in Director v2 checkpoints, preserving pending
  selections and consumed budget across receipt replay; this does not yet
  provide complete new-owner process continuation.
- Local React/FastAPI console with live acceptance evidence and bounded CPU
  checks. The running console has no configured Lab API connection.
- Farm B calibration storage and bounded cell execution: fixed 135-cell grid,
  nonrefundable claims, original deadlines and exact worker-generation checks.
  Source and synthetic PostgreSQL proof are separate from measured calibration.

## Validation

Exact commands, exit codes and source/image bindings are linked from
[the acceptance record](m0-acceptance.md). Synthetic, actual model and public-data
results remain distinct. The latest native runtime evidence is in
[the native runtime review](18-native-runtime-review.md).
The latest completed 0.32 gate passes 539 tests and all seven commands, with
strict mypy on 99 sources and source/image parity for 108 runtime files.
The calibration integration's failed gates and UUID correction are retained in
[the calibration review](38-care-calibration-execution-review.md).
Real isolated PostgreSQL 0023 passes one test with 135 synthetic receipts,
immutable freeze/readback and original-deadline checks; no actual Farm B
calibration workers ran. Historical source-bound 0.27 production report/replay
proof covers twenty synthetic proposals and all 22 replay records. The real
four-model AOS/Qwen diagnostic passes; full research and interactive desktop
coexistence remain open. A complete successful S2 research proposal run
still needs a real-model measurement.

## Remaining before M0 completion

Real S1/S2 research experiments, the full public baseline/proposal grid,
capacity/QLoRA measurements, measured Farm B calibration and holdout,
independent baseline CLI, complete process restart and tool/EXPLORE behavior,
normal opt-in AOS bootstrap and real simultaneous AOS/Lab progress.
The authoritative list is every open or partial row of the acceptance record.

Live AOS sources and services are preserved. Its eventual patch application must
be reconciled with the parallel development session using
[the coordination note](06-aos-coordination.md).

## Publication

No Git remote is configured in this workspace as of 2026-09-27. This file is a
local draft, not an opened PR. Publish as a draft with base `main` when the target
remote and usable authentication are available; no automatic merge.
