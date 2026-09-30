# Holdout 030 lifecycle fixture handoff

`holdout-030-lifecycle-fixture.py` is an unexecuted, opt-in runner for one
disposable PostgreSQL 16 database and one authentic Scorer worker interruption.
It refuses execution unless called with `--execute-reviewed-fixture`; plan-only
inspection is `--print-plan`.

It snapshots the reviewed 0.30 source files, uses that snapshot's own
`data/runtime/postgres/scorer.dsn` alias, creates four random per-role DSNs, and
applies the repository's migrations. The database container is identified by
its full container ID and unique label; its loopback-published port and
container-side PostgreSQL address are checked separately against all four
authenticated role connections. It then runs the existing
`test_run_end_accepts_resumed_abandoned_ordinal_without_experiment_row` setup
from `tests/test_postgres_holdout_api_identity.py` in the snapshot. A generated
pytest hook changes only that in-memory fixture's baseline candidate to a valid
typed pipeline that waits during its holdout fit, reserves via the production
Director RPC, and starts `run_holdout_process`. The hook publishes the exact
run/reservation and worker/container generation for the review driver.

The baseline score rows and calibration in that existing test are synthetic
ledger fixtures. The candidate source is valid and SHA-bound when registered;
the holdout itself uses the real worker, Docker sandbox, and pinned image. This
is a process lifecycle proof, not model-quality evidence or a successful
holdout score. The stop driver uses FastAPI TestClient, so it proves the
authenticated route and background recovery in-process, not a deployed HTTP
server interaction.

Before baseline Scorer jobs launch, the pytest hook records the fixture run UUID
and each enqueued job UUID in a private setup receipt. Each job's runtime is
clamped to the remaining aggregate deadline minus the cleanup reserve. On
failure, cleanup first terminates and reaps only the fixture's pytest process
group so no new job can launch and its lifecycle locks are released. It then
calls the production stop/recovery route for the recorded run (including the
reservation receipt gap), and checks each exact job unit's invocation, cgroup,
PID boot/start identity, snapshot working directory, executable, and worker
argv before stopping it. If the test already deleted setup ledger rows, the
private job UUID receipt and exact unit/process proof still gate cleanup.
Unknown generations keep the database and credentials for review.

The parent runner requires its own cgroup to be at most 1 GiB RAM, no swap,
50% CPU, and 64 tasks. It requires 7 GiB available host memory and 20 GiB free
disk. The disposable database is capped at 512 MiB/no swap, 50% CPU, 64 tasks;
the candidate image is already pinned by the driver to 2 GiB, one CPU, 64 PIDs,
and no network. The Scorer slice bounds worker/recovery services. Its 180
second worker allowance is below the 420 second aggregate fixture deadline,
which reserves the last 45 seconds for cleanup. SIGTERM is converted to a
controlled unwind. On failure after a reservation, cleanup calls the
authenticated production stop/recovery path for only the recorded run ID and
reservation ID; if exact cleanup cannot be verified, or Docker cannot prove
exact container absence/removal, the fixture retains its private DSNs/database
instead of deleting credentials or state under unresolved workers or containers.
It never accesses main-checkout DSN aliases or ambient databases.

The normal review command is launched by the fixture itself with the captured
source fingerprint, image-lock hash, migration head, and exact UUID pair. Its
receipt is private to the source snapshot. A separate outer receipt records
the isolated container identity, source snapshot hashes, exit codes, and
cleanup outcome. No DB, Docker container, Scorer worker, API service, model,
GPU, or AOS action has been performed while preparing this file.

The stop review still marks drain-before-terminal-SQL-CAS ordering as
unproven. The final reservation state and drained cgroup do not independently
establish that order. Root should treat this as lifecycle tooling ready for
review, not as an executed acceptance result.
