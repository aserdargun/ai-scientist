# TSB-AD-M row-order and source clock decision

**Status:** implementation decision for the fixed public development members; no
claim of complete four-source suite acceptance is made by this note.

The source member is selected by its exact path from the checksum-pinned TSB-AD-M
archive manifest. Its `_tr_N_` token is treated only as the upstream curated train
prefix length. The `_1st_` filename token and all label values are ignored until
the source-order split, train-derived window, and one-window sample embargo have
been fixed. The evaluation suffix begins at `N + window`; no alternative window or
boundary is attempted if it yields few or no positive labels.

The Genesis original file has numeric `Timestamp` values, but the pinned source
review does not establish their unit. The materializer therefore uses row indices
for EVT, sets `FitContext.sampling_s` to null, and does not expose the timestamp
column to the candidate. Physical-time exposure metrics are unavailable for that
task. This does not imply a one-second cadence.

CATSv2 v2 documents 1 Hz sensors and its first one million rows as nominal. The
selected curated slice maps to original row 4,900,000 under the checked source
mapping. Its suite time axis is a source-relative row index; no timezone is
inferred. CATSv2 is simulated telemetry and its labels are tiered bronze. It is
not counted as a real industrial source.

GECCO's source clock is naive, has 60-second nominal steps and one documented
−3,540-second local-clock reset. The adapter reconstitutes the original 139,566
source-row grid using the pinned finite-row mapping, preserves non-finite feature
rows as masks, bounded-fills candidate inputs, and masks the reset row. It reports
60-second nominal sample cadence on a monotone row-order axis; it does not claim
UTC or use raw wall time as a candidate feature. Training uses the longest
contiguous finite part of the curated train prefix so rolling features do not
silently cross a source gap.

All three task labels remain in Scorer registration. Task/source IDs and source
members are fixed in code; the implementation does not search labels for a
positive window. Source usage is restricted to noncommercial research regardless
of whether an underlying license is more permissive.
