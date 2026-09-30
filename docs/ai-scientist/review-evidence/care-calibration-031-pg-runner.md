# CARE calibration 031 PostgreSQL runner

`review_care_calibration031_pg.py` is a review-only, explicit opt-in runner for
the fixed live SQL test `tests/test_postgres_care_calibration.py` from the
private `calibration-031` source tree. Its plan mode creates nothing. The
execution mode provisions a new PostgreSQL 16 container, four role DSNs, and a
byte-checked source snapshot; it runs migration head
`0022_care_baseline_calibration` and the one marked live test, then removes only
the container carrying its unique label. Credentials are retained if exact
container removal is uncertain.

The test seeds 15 synthetic task profiles and 135 synthetic calibration
receipts. It exercises real PostgreSQL role grants, constraints, freezing, and
retry behavior. It does not run a Scorer worker, execute baseline models, or
claim measured CARE calibration, dataset acceptance, GPU, or AOS evidence.

Root may run it only after reviewing the frozen source, while holding the
shared CPU lock and applying these outer limits:

```sh
flock -n /home/cachyos/ai-scientist/data/runtime/parallel-m0/cpu-check.lock \
  systemd-run --user --scope --unit=swapp-care-calibration031-pg-review \
  -p MemoryMax=1G -p MemorySwapMax=0 -p CPUQuota=50% -p TasksMax=64 \
  -p RuntimeMaxSec=360 \
  /home/cachyos/ai-scientist/.venv/bin/python \
  /home/cachyos/ai-scientist/data/runtime/parallel-m0/lifecycle-030/\
docs/ai-scientist/review-evidence/review_care_calibration031_pg.py \
  --execute-reviewed-pg
```

The runner has a 300 second aggregate deadline and 45 second cleanup reserve.
Its sibling PostgreSQL container is separately capped at 512 MiB, 0.5 CPU, 64
tasks, and no swap. It binds only to a dynamically assigned loopback port. The
runner was not executed while preparing this review artifact.
