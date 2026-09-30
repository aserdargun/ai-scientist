# Public baseline 050 source delivery

Source only, based on main `ed574a7929f453a71cf6dfb0908dc63ad3cb3575` (0.35.3). Preparation and installation are separate invocations. No data materialization, PostgreSQL, service, Docker or model execution was performed for this delivery.

## Preparation command for root

Run the following in a root-approved external scope with MemoryMax=2G, MemorySwapMax=0, CPUQuota=100%, TasksMax=128 and RuntimeMaxSec=240. The script independently checks its actual cgroup limits, uses one BLAS thread, and arms a 240-second alarm. Root retains the common CPU execution lock and records actual elapsed time, peak memory and I/O.

```sh
cd /home/cachyos/ai-scientist/data/runtime/parallel-m0/public-050
PYTHONPATH=. /home/cachyos/ai-scientist/.venv/bin/python scripts/prepare_public_baseline050.py \
  --source-root /home/cachyos/ai-scientist \
  --config ops/public-four-source-050.json \
  --destination /home/cachyos/ai-scientist/data/runtime/public-baseline-050
```

This default command requires no database credentials, does not connect to PostgreSQL, does not publish the registry, and does not start a run. It writes a private suite manifest, baseline-only registry fragment, empty provider envelope, baseline request template and preparation receipt. Root must use a fresh/private destination. Existing conflicting output or incomplete staging fails closed.

The four EVT tasks use unchanged production materializers and source × signal × task-family weights, with four independent families and a 0.25 family cap. Suite version remains 3. The expected baseline is three existing algorithms × three seeds × four tasks = 36 score cells; no scores are claimed by preparation. The request template is baseline-only, zero proposal/model-token budget, and 7200 seconds wall budget; it is not submitted.

## Pinned scope and resource meaning

The configuration pins fourteen local metadata/raw files by byte size and SHA256, plus the three exact TSB archive member sizes/CRCs. The 2 GiB `source_bytes` field is a **unique pinned-input byte budget**, not a total I/O cap: verification and existing loaders reread inputs. No archive extraction is performed. Original CATS parquet is streamed for hash verification; the full original 5-million-row table is not materialized. Only the three selected TSB members and SMD machine-1-1 are materialized.

Selected archive members total at most 40 MiB uncompressed; resulting matrices at most 40 MiB; each exact expected task shape and a 150,000-row per-task ceiling are checked. SMD uses the existing official train/test split with fixed 12,000-row tails. This is a bounded real server-telemetry acceptance slice, not full SMD acceptance. Genesis retains CC-BY-NC-SA-4.0 and noncommercial research metadata; GECCO, CATSv2 and SMD preserve their existing source attributions, clock/split policies and licenses. No synthetic labels/data or GHL/SWaT are introduced.

The generated suite must fit 64 MiB, below the current shared 96 MiB manifest reader limit; no reader limit is raised. RLIMIT_FSIZE=128 MiB is per file, not an aggregate quota. The fixed output inventory consists of one suite below 64 MiB plus small canonical JSON fragments/receipts; a temporary suite copy can briefly coexist while publishing. The script requires a 20 GiB free-disk reserve plus 128 MiB headroom. Root must measure actual output and I/O independently.

## Later trusted installation and API release

`--install` is a separate explicit stage requiring private `--registration-dsn-file`, `--planner-dsn-file`, and `--registry-file`. It verifies PostgreSQL endpoint database `swapp_lab`, URL role and actual session role. Only the offline Migrator installs existing profile/label/semantics registrations; independent Planner readback verifies the manifest before registry publication. No Scorer table INSERT grants or API Migrator credentials are added. Existing installer conflict handling and idempotent retries remain unchanged. Installation is not one atomic four-task transaction; a failed partial install requires a verified retry, never deletion or takeover.

Do not publish the baseline-only entry until the API/registry payload passes the release gate. `SuiteEntry.allowed_purposes` defaults to both existing purposes and preserves historical default entry hashes. The public entry explicitly permits only baseline; API research submission is rejected before durable admission. Other explicitly configured entries may still permit research. The empty provider envelope is never a substitute for agent research.

The preparation run can use this private source with main as its data root before those API changes are deployed. Future integration onto 0.36 must apply only the payload and inspect the dependency hashes in the freeze; do not replace the whole checkout.

## Source checks

- 22 focused CPU tests passed (including real SQLite admission checks: rejected research writes no run rows; baseline is admitted).
- Strict mypy: 3 source modules passed.
- Ruff: 4 Python files passed.
- Compile and patch whitespace checks passed.
- No real public materialization, PostgreSQL installation/readback, API release, baseline execution, or acceptance claim in this source delivery.
