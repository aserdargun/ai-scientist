# Holdout 030 real worker stop review

`holdout-030-lifecycle-review.py` is an unexecuted action driver in private
worktree `lifecycle-030`, based on commit `7ba1a1b`. It checks harness 0.30.0
and pins sandbox image
`sha256:2ad07bb30f69a9a97ed05acbc402ebafacd06566209d0e70819432c62540484e`.
`--print-source-fingerprint` reads only source and image-lock files.

The driver targets one already-running reservation with a live recorded worker
generation and one matching running candidate container. It invokes the
authenticated `/v1/runs/{run_id}/stop` route in-process through FastAPI's test
client, preserving the production route and background recovery callback. It
then requires the bounded Scorer recovery receipt, exact worker PID/start
ticks/boot/unit/invocation/cgroup drain, removal of the one marker-bound Docker
container, and Director readback of terminal `failed` with a null bit. Run and
suite quota, experiment, score-job, task-score, completion, reservation,
holdout-result, and approval counts must remain unchanged.

## Preconditions and execution boundary

- The companion `holdout-030-lifecycle-fixture.py` is being prepared as an
  explicitly opted-in orchestrator. It must create a private source snapshot
  and disposable `swapp_lab_m0_holdout_030_*` PostgreSQL 16 database, put four
  unique role DSNs in a mode-0700 directory, and put a separate Scorer DSN copy
  only at the snapshot's `data/runtime/postgres/scorer.dsn`. It must not use or
  modify main-checkout aliases or ambient DSNs.
- Before the stop call, this driver requires every role connection to report
  the same database and PostgreSQL server generation; DSN host and published
  port must be loopback-bound. It compares the snapshot Scorer alias to the
  private Scorer role credential without printing it. It also checks the live
  worker's `/proc` cwd, executable, and argv against the reviewed snapshot.
- Fixture setup uses the production run-end intent and Director reservation
  RPCs. Baseline score/calibration setup may be synthetic, explicitly marked
  as lifecycle-only. The holdout worker itself must be the production
  `run_holdout_process` using a valid hash-bound typed candidate and real
  source-matched sandbox image. The candidate should remain in `fit` until the
  stop driver has bound its exact process/container generation.
- The worker must be in a Docker phase with a private admission marker and one
  running container matching the worker PID/start/boot, reservation work root,
  pinned image, and owner label. A between-phase or ambiguous state fails
  before requesting stop.
- `LAB_HOLDOUT_TEST_DSN_DIR` names the private role-DSN directory. API token and
  owner ID are passed only through `LAB_HOLDOUT_LIFECYCLE_API_TOKEN` and
  `LAB_HOLDOUT_LIFECYCLE_OWNER_ID`; these values are never printed. The Scorer
  alias check occurs before any API call.
- A new receipt destination in the snapshot's private
  `data/runtime/holdout-lifecycle-receipts/` directory is reserved before
  mutation. Existing, symlinked, or out-of-directory destinations fail before
  stop. Post-stop failures publish a sanitized failure artifact when the
  reserved path remains owned.
- Run the outer fixture in a bounded user scope with 1 GiB memory, no swap,
  50% CPU, 64 tasks, and a wall deadline longer than the worker's bounded
  allowance. Worker/recovery services and Docker are sibling cgroups, so the
  outer limit alone does not bound them. The fixture must reserve cleanup time
  and stop only exact captured unit generations and its exact labelled DB
  container. Candidate containers are cleaned by production recovery. Keep at
  least 20 GiB disk free. Candidate limits are 2 GiB, one CPU, 64 PIDs, no
  network; the existing aggregate Scorer slice bounds worker and recovery.
- Execution requires explicit `--execute-reviewed-stop` and one run/reservation
  pair. The API call is in-process with TestClient: it proves the authenticated
  route and background callback, not a deployed HTTP server interaction.

The final failed state and cgroup readback do not independently prove host
drain occurred before the recovery SQL compare-and-set. This driver records
that ordering as unproven unless separate instrumentation is reviewed. The
proof is a real worker interruption and bitless recovery, not a successful
holdout score, model quality result, canary decision, or concurrent Lab/AOS
acceptance. It must never enumerate, stop, remove, or clean up unrelated
workers or containers.
