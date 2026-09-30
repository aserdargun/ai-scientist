# Operating-mode study workflow

The 041 source slice is integrated in release 0.35.3, including terminal
finalization 0027, immutable snapshot registration 0028 and registered holdout
lookup repair 0029. The independently developed restart implementation remains
separate. See `docs/ai-scientist/43-first-project-guide.md` for the running UI
and completed project.

## Operator configuration

The existing authenticated Lab API and console must be configured first.
`LAB_SUITE_REGISTRY_FILE` points to the existing private suite registry; the API
atomically adds immutable mode-grid entries there. No new job ledger is used.
Snapshot root is fixed at `data/runtime/mode-snapshots`. The separately supervised
Scorer installer reads `data/runtime/postgres/scorer.dsn`; credentials never enter
an HTTP request or response. Installation has a fixed 30 second runtime in the
existing Scorer 2 GiB / 100% CPU / P=1 aggregate slice. Candidate scoring uses existing
sandbox/Scorer workers, research deadline, proposal count and seed reservations.
Grid construction reserves at most 10 seconds and zero model tokens.

Optional `LAB_MODE_SOURCE_CATALOG_FILE` is a 0600 operator-owned JSON file:

```json
{
  "schema": "mode-source-catalog.v1",
  "sources": [{
    "selection": {
      "source_id": "plant-readonly",
      "source_version": "operator-version-1",
      "table": "approved_sensor_view",
      "timestamp_column": "timestamp",
      "entity_column": "machine",
      "row_limit": 512,
      "sampling_seconds": 1.0,
      "units": ["C", "bar"]
    },
    "sensors": ["temperature", "pressure"],
    "owners": ["configured-local-api-owner"],
    "dsn_file": "/absolute/private/readonly-source.dsn"
  }]
}
```

Use a PostgreSQL role limited to approved views.039's reader uses a read-only
transaction, 5 second statement timeout, UTC bounded timestamps and limit+1
rejection. The UI sends source/column identifiers and a selection recipe, never
SQL or credentials. Each private snapshot has a server-derived owner access
receipt. Missing catalog is shown as unavailable. Source exceptions are redacted.

## Acceptance flow for root execution

1. Rebuild the sandbox image after the changed candidate entrypoint; update the
   trusted image/hash through the normal integration gate. Do not reuse an image
   lacking the new score contract.
2. Open the AI Scientist console on 8788, **Çalışma modları**.
3. Generate a synthetic snapshot, inspect its SHA, training/evaluation statistics,
   histograms and correlations. These are descriptive user views, not proposal
   feedback. Evaluator labels are separate Scorer-private artifacts.
4. **Scorer için hazırla** verifies the generator recipe and installs the profile,
   labels and semantics. Training-derived sliding-window embargo removes the
   evaluation prefix. The readiness receipt is written only after DB commit.
   Sensor-quality/nonfinite fixtures can fail normal finite-input admission;
   failure must remain visible, without imputation or invented scores.
5. Select LSH/OPTICS/SOM and k, then start the zero-token parameter grid. The
   ordinary Director/Scorer run ledger handles baseline calibration, candidate
   guards, repetitions, bounded budgets, stop and final report. No local model
   is invoked by this provider.
6. In **Deneyler**, inspect progress, stop if desired, and open the verified report.
   The report labels this as a single-snapshot exploratory study, not multi-family
   benchmark acceptance. Diagnostic selection shows mode support/tolerance, OMR
   points, alarm flags, SOM BMU separately and per-sensor residual/contributions.
   These details are candidate-derived; trusted performance is Scorer-owned.
   Retrieval requires the report owner and a referenced content-addressed blob.
7. Optionally configure a real source and select a UTC range/entity. Private
   snapshots support descriptive statistics and owner isolation. Trusted source
   labels/protocol are not installed automatically: the UI disables Scorer
   installation for these unlabeled database snapshots.

## Explicit remaining scope

The original worker's test-selection mistake against an old image remains in
its private `review-evidence/console041-unapproved-execution.json` and is excluded
from acceptance. Later root-owned 0.35.3 execution completed the supported flow:
PostgreSQL sensor/time selection and statistics, immutable synthetic snapshot
registration, 12 Docker/Scorer measurements, final report and 19 browser checks.
Public evidence is in
`docs/ai-scientist/review-evidence/operating-console045-completed-project.json`.
The PostgreSQL connection and readonly role are real; source values are synthetic.
External plant data and source-specific trusted evaluation remain unmeasured.
Autonomous local-LLM data selection is not implemented here; OM.6/OM.7 remain open.
The next adapter should expose `SourceCatalog.available` and
`DatabaseSnapshotRequest` to a bounded planning tool in `local_llm.py`, then bind
its selected immutable snapshot in CLI/Director admission before experimentation.
It must preserve owner allowlists and keep evaluator labels/statistics out of
proposal adaptation. Private-source trusted evaluation/normal-reference protocol,
long-run/local-LLM research, general resume and positive/periodic replay acceptance
remain separate requirements; synthetic studies do not close them.
