# Operating modes 039

Source-only implementation of `docs/ai-scientist/42-operating-modes-omr-experiments.md`.
New files only. No proprietary implementation or numerical parity claim.

## Contract

`fit_model(frame, ModeConfig(...))` freezes normal references, training minimum/range,
mode centers/tolerances and calibrated OMR alarm threshold. `model.predict(frame,
alarm_state=..., row_offset=...)` returns typed JSON-safe rows and explicit alarm state.
Prediction changes no fitted state. Models store immutable tuples, config and diagnostic
JSON. Batch/chunk/single-row outputs agree when callers carry the alarm state/offset.

LSH modes are connected components of rows colliding in at least the configured number
of independent projection tables. Combination signatures compute exact connectivity
without dense pairwise tensors. OPTICS fits training rows once and reports explicit
noise. SOM occupied units meeting minimum support become modes; grid/occupancy,
quantization/topological error and learning history are separate diagnostics.

Mode admission is radial distance to a reference-mode mean in training-range-scaled
space; tolerance is a training distance quantile times the configured multiplier.
Unknown points may receive fallback OMR from the closest supported mode, explicitly
marked out_of_mode. Predictions use deterministic nearest references, stable row-order
ties and explicit uniform/distance weights. Constant training sensors are excluded
from RMS and flagged if changed. All-constant or unsupported-mode scores are null.
Calibration excludes each actual training row (LOO) or reserves a chronological suffix.
The latter freezes ranges/modes from the prefix only. Neither strategy imports labels.

`candidate_source(config)` produces source for existing Director/Scorer admission. The
candidate effective seed is `(proposal_config.seed + FitContext.seed) % 2**32`; source
bytes preserve proposal seed, and fitted summary records effective seed. It implements
ADPipeline and ModePipeline. Unavailable scores become NaN for the existing evaluator
validation to handle. It never converts unavailable scores to healthy zero. Existing
robust-z/IF/ECOD baselines are unchanged. Package/image fingerprint changes belong to
integration, not this delivery.

## Data boundary

`SourceSelection` is credential-free selection metadata. `snapshot_from_frame` freezes
UTC chronological rows, split, recipe and content SHA256; imported snapshots verify
shape/timeline/hash. Missing/nonfinite sensor values become null. `read_postgres_snapshot`
accepts an already authorized engine, uses SQLAlchemy bound parameters, read-only
transaction, 5-second statement timeout and row_limit+1 overflow rejection. It creates
no engine/account/schema. Sampling_seconds is declared source sampling metadata, not
resampling. Real PostgreSQL access remains untested. Duplicate timestamps remain visible
for statistics; query ordering uses timestamps and sensor values for stable ties.

`synthetic_snapshot` yields a label-free snapshot plus separate evaluator-only event,
quality and mode labels. Ten deterministic scenarios have healthy training prefixes.
`summarize_synthetic` reports pointwise process recall (not event recall), false alarms,
first detection delay and OMR distribution. Quality faults are excluded from process
success. Root owns the actual 90-condition performance measurement artifact; local
parameterized tests verify execution/contracts, not universal detector performance.

Statistics use the separately owned `lab.analytics.statistics.summarize_dataset`
contract at the API integration boundary. This core does not duplicate statistics.
Snapshot/model max64 sensors; statistics currently permits50, so the combined UI must
respect50. Model fit/predict cap4096 rows/call; use streaming chunks for longer prediction.
Nearest-reference arithmetic processes one query at a time, no NxNxfeatures tensor.
SOM has at most144 units and10000 iterations; LSH at most8 tables/8 projections.
Deadline/stop/resume and result persistence use the existing sandbox/Director path;
this package introduces no service, job ledger or budget authority.

## Acceptance boundary

CPU numerical, source-selection mock, snapshot integrity and synthetic invariant tests
are included. No model/GPU/service/database/container/AOS launch was performed.
Full project gate, sandbox image import/fingerprint compatibility, real source access,
Director grid execution, browser flow and real-source generalization remain integration
acceptance. No commit was created.
