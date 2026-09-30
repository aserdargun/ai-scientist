# Holdout 030 continuation checkpoint

Worktree: `/home/cachyos/ai-scientist/data/runtime/parallel-m0/holdout-030`  
Branch: `feat/luna-m0-holdout-030`  
Base: `1d8b07dc009e55470ec7c2c2c9333267eb5347c0`  
Migration: `0019_bounded_holdout`, parent `0018_public_task_semantics`.

Source-bound review patch (owned files only):
`/home/cachyos/ai-scientist/data/runtime/parallel-m0/delivery/holdout-030-source.patch`  
SHA-256: `a32926b8a723f50a24194e9ffe42c6ff1960dba01091191d40e1efcf2b062a3d`.

The holdout delta from frozen 025 is ported into the current 030 base without
replacing the current CLI, strategy, typed executor, or console. The loop change
preserves M0.2 terminal receipt recovery and routes current harness drift through
the typed rejection path while retaining pinned calibration, image, suite, and
task identity checks.

Implemented in this slice:

- Durable holdout wall reservations, measured reconciliation, conservative
  full-bound charging after restart, and budget restoration from holdout
  checkpoints. Existing terminal holdout receipts are applied without a new
  work reservation.
- Periodic receipt reconciliation before the proposal-limit check, including an
  exact-limit restart.
- Run-end state key/digest binding and checks for the exact latest holdout
  application marker; unrelated newer checkpoint events are rejected.
- Trusted holdout registration gates the loop and CLI hooks. Run-end evaluation
  also accepts token exhaustion when wall budget remains, and does not extend a
  zero wall budget for finalization.
- Operationally failed receipts remain bitless, restore the last approved
  snapshot, and require manual review. Reporting keeps the historical dev
  staircase immutable and displays development versus approved champion IDs.
- The PostgreSQL identity test prefix is 030. No migration, PostgreSQL,
  Docker, image, service, model, GPU, or AOS test was run on this current source.

## Targeted checks

The successful focused CPU invocation ran under a user systemd scope with
`MemoryMax=1G`, `MemorySwapMax=0`, `CPUQuota=50%`, `TasksMax=64`, and
`RuntimeMaxSec=240`:

```text
PYTHONPATH=/home/cachyos/ai-scientist/data/runtime/parallel-m0/holdout-030
pytest -q tests/test_holdout_loop_recovery.py tests/test_director_loop_core.py
  tests/test_director_harness_mismatch.py tests/test_report_replay.py
exit 0; 16 passed in 2.92s
```

Static checks against this worktree also completed successfully:

```text
ruff check [owned Python paths and holdout tests] -> exit 0, All checks passed
mypy [loop, director holdout API, CLI, reporting, scorer supervisor, migration] ->
  exit 0, Success: no issues found in 6 source files
py_compile [loop, holdout API, CLI, reporting, schema, migration] -> exit 0
```

An earlier focused invocation including `test_holdout_privacy.py` stopped at
collection with an `IndentationError` in the concurrently edited recovery-owned
`lab/scorer/holdout.py` (line 665; pytest reported an error during collection).
The recovery worker reported repairing it afterward. At that point I did not
rerun that broader selection, so the privacy test is not part of the 16-pass
receipt above. A separate initial command also named nonexistent
`tests/test_reporting.py`; it ran no tests and is not a test result.

Imports under explicit `PYTHONPATH` resolved to this worktree:

```text
lab.director.loop -> /home/cachyos/ai-scientist/data/runtime/parallel-m0/holdout-030/lab/director/loop.py
lab.director.holdout -> /home/cachyos/ai-scientist/data/runtime/parallel-m0/holdout-030/lab/director/holdout.py
lab.cli -> /home/cachyos/ai-scientist/data/runtime/parallel-m0/holdout-030/lab/cli.py
lab.reporting -> /home/cachyos/ai-scientist/data/runtime/parallel-m0/holdout-030/lab/reporting.py
lab.scorer.supervisor -> /home/cachyos/ai-scientist/data/runtime/parallel-m0/holdout-030/lab/scorer/supervisor.py
```

## Source hashes

SHA-256 values captured after the focused edits:

```text
401a924afd654026c1df1078071a397a1cdd6c8b24ecf061981f32ee06ee198c  lab/director/loop.py
e2ea3c088fb76b0db607bc63e69032972ffb98c9f3e8baa811f66f23e1239001  lab/director/holdout.py
c94afadc2ea210817442f0c3ff3113d117d5b894aacb3fd502c372cebb8fb01b  lab/cli.py
54baa1de048e0bd0c42ef0a7c8a38491f7a39aac616321912fb4c0e29752cb2e  lab/reporting.py
a921d05ce36985ce88685ecd7c8a5df901c555ab8ccf3b0d1cee736406f37d81  lab/db/schema.py
cdd2cbd49a504e8bada54c0877fb99e8a7db2f42996905817effad5dafe7ef9c  lab/db/migrations/versions/0019_bounded_holdout.py
47e8e0bfdd1b820f5e63e54587b8a4659b6290606b8a028d28fde27ad451d3bc  tests/test_holdout_loop_recovery.py
ef969bdd9fd2e0e5950d47a5739cec11112539d18fc6b89267d779c7fd58b81a  tests/test_holdout_privacy.py
c738df836102f6b7e67ed7a8d6db58e0c47652f55c3ca92eb0dd8cceb881f926  tests/test_postgres_holdout_api_identity.py
```

`lab/scorer/supervisor.py` is excluded from my frozen ownership at root's
request because recovery integration also touches that shared lifecycle code.
The current checkout contains a bounded, nonblocking lock timeout and private
runtime-directory validation; its observed hash at checkpoint time was
`f782bf031a4e57c137680648b078550248a2cc161d78468208c281bef5dc6efd`.

Recovery-owned work remains concurrent and unreviewed here:
`lab/scorer/holdout.py`, `holdout_supervisor.py`, `holdout_worker.py`,
`holdout_recovery.py`, and migration 0020. Do not treat their current hashes or
the frozen 025 PostgreSQL evidence as proof for 030. No commit was made.
