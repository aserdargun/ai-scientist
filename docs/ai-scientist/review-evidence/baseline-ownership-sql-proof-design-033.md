# 0025 SQL scenario design for the combined baseline proof

2026-09-27. Source review/design only; **execution=false**. These cases supplement
plan 39's authenticated dispatcher and 36-score baseline proof. Small SQL cases
do not establish calibration, OS identity, worker drain, restart continuation, or
global dispatcher capacity.

## Source assumptions

The moving `director-restart-032` tree supplies 0025, ownership helpers, jobs,
service and task-plan code. The frozen `baseline-cli-031` tree supplies the baseline
API contract until merged. Integrate/rechain 0024 after 0023 and 0025 after 0024;
freeze and hash the actual combined snapshot before running. In particular, fix
the reported nested task-completion trigger order, stopped-job update allowance,
empty-stop helper/caller, retry context and finalizer capture issues first.

Observed source hashes (an observation, not a frozen approval):

| Private restart source | SHA-256 |
| --- | --- |
| `lab/db/migrations/versions/0025_director_generations.py` | `0ed01f1909591a49ec4b5634851c990fec61be67c0ca1e187b65b444e4403d01` |
| `lab/director/ownership.py` | `fd28ca4b29b4970d079d613d4b2adc999ce204f7dcf8565293c8b35feddeabec` |
| `lab/scorer/jobs.py` | `db53b2b11e3a8c8220417bf361289b8f8652fff840213e0c8465d60e46354ad7` |
| `lab/scorer/service.py` | `02930af4c61993dee2c29b61c5a711aaebf8ca66aac7f99fc9470b329b1aad29` |
| `lab/director/task_plan.py` | `a9416cd4840bb824bd3e2268a40e55e7219238d3b1502437f4569cc85717416e` |

## Admission and minimum state

Use the verified four-family fixture registry and private profile/label inputs
from `baseline-proof-inputs-032.py`; resolve registry paths against the disposable
runtime root. Authenticate as the local fixture principal and POST `/v1/baselines`
with distinct keys of at least 16 characters:

```json
{"idempotency_key":"proof-033-UNIQUE-A","suite":"<registered-suite-id>","budget":{"experiments":0,"wall_seconds":600,"model_tokens":0},"program_version":"<registered-program-version>"}
```

Use separate auxiliary runs A and B for SQL ownership cases; retain the full
matrix run for real dispatcher execution. Do not allow a dispatcher to consume
these auxiliary queued runs before the claim tests. API admission remains real;
manual RPC ownership here is explicitly a SQL fixture, not proof of slot control.
Never hand-insert `runs`, request hashes or owner rows. Record API status, run ID,
persisted immutable request/hash, and idempotent replay identity. Exclude bearer
tokens, DSNs and labels from evidence.

Minimum dependency chain for a task case: admitted run → initial execution
contract/control/generation/legacy owner → one registered baseline experiment →
one `scorer.run_tasks` assignment referencing an installed fixture profile.
For a scored case add one Planner-enqueued job and its live Scorer claim. For a
Planner terminal case use a distinct assigned cell with **no job, score, terminal
outcome or completion**. Neither case needs a sealed plan or a report. These
partial auxiliary plans must never be reported as completed baseline matrices.

## Sessions and assertion discipline

Connect using the actual Director, Planner and Scorer login DSNs, not superuser
`SET ROLE`: SECURITY DEFINER code checks `session_user`. Assert `session_user` and
READ COMMITTED on each connection. Admin setup installs only fixture profiles,
labels and migrations; no disabled triggers or ad hoc privileged owner mutations.

For every negative, establish and record valid state/deadline/owner first, use a
SAVEPOINT, attempt exactly one malformed mutation, catch the DB exception, assert
SQLSTATE **P0001** and the intended guard message, then ROLLBACK TO SAVEPOINT.
Assert all relevant rows/counts/identities unchanged. Missing permission must be
**42501** when the frozen grants deny execution; do not accept either code
indiscriminately. Freeze expected messages/codes from that source version.
Follow each negative group with the otherwise identical positive operation.
Commit positives and verify through a fresh connection. A deferred integrity
check must also be forced with `SET CONSTRAINTS ALL IMMEDIATE` before declaring a
savepoint case successful. Unexpected exceptions, timeouts and unrelated guard
messages are failures, not successful negatives.

## Initial claim and immutable wall

Build `ExecutionContract.build(...)` from the persisted request plus verified
suite ID/version, suite/registry/harness/image hashes. Bind parameters using the
driver's normal parameter API; `:name` below denotes named bound values.

```sql
SELECT lab.claim_initial_director_execution(
  :run_id, :payload_sha256, CAST(:execution_json AS jsonb), :execution_sha256,
  :wall_seconds, :worker_pid, :worker_start_ticks, :worker_boot_id,
  :worker_unit, :worker_invocation_id, :worker_cgroup);
```

Use the captured bounded fixture process identity (PID >1, positive start ticks,
boot UUID, service name, 32-hex invocation and matching cgroup suffix). The SQL
checks identity shape; independent OS observations belong in execution evidence.

1. While A is queued and has **no** execution rows, copy its correct contract,
   keep `execution_json.request` unchanged, increase only top-level
   `execution_json.wall_seconds` and `p_wall_seconds` from 600 to 601, and recompute
   the canonical execution digest. Call SQL directly so Python validation cannot
   replace the SQL test. Expect P0001 `run is not eligible for an initial Director
   claim`; A stays queued, owner/contract/control/event counts unchanged.
2. Claim the same A with the original correct parameters. Require generation 1,
   one immutable contract, one generation, one control and one legacy owner,
   exactly one started event, state running, and deadline-start exactly 600 seconds.
3. Claim B correctly. Verify both are running, unsealed and within their original
   deadlines before the cross-run cases. A repeated claim is a separate replay
   rejection test; it cannot substitute for the queued wall-tamper regression.

## Owner context and cross-run mutation

In each Director/Planner transaction bind the captured tuple, then assert it:

```sql
SELECT set_config('lab.owner_run_id', :run_id_text, true),
       set_config('lab.owner_generation', :generation_text, true),
       set_config('lab.owner_invocation_id', :invocation, true),
       set_config('lab.owner_execution_sha256', :execution_sha256, true);
SELECT lab.assert_director_owner_context(:run_id);
```

For missing context use a fresh transaction or clear all four settings to empty
strings. For wrong generation use `2` while current remains `1`; wrong invocation
and digest must be valid-shaped different strings. Expect the specific missing
context or stale-generation P0001. Explicit SQL NULL is tested independently:

```sql
SELECT lab.assert_director_generation_identity(:run_id, NULL, :invocation, :sha);
SELECT lab.assert_scorer_run_execution(:run_id, NULL, :sha); -- Scorer login
```

Both must reject malformed identity while the same run with generation 1 succeeds.

Under valid owner A context call the following with **B** as `:target_run` and a
fresh globally unique experiment ID. All other arguments must match a valid
allowlisted baseline registration (candidate/input blobs prepared by production
artifact helpers):

```sql
SELECT lab.register_experiment(
  :experiment_id, :target_run, 0, NULL, 'baseline', :baseline_name, NULL,
  :candidate_sha, :candidate_blob_sha, :inputs_sha, 'detector', 'S1',
  :hypothesis, NULL, CAST(:proposal_json AS jsonb));
```

Require P0001 from target-run ownership binding, with neither A nor B changed.
Then use valid B context and the identical registration arguments: it must
succeed. Also attempt a direct granted row UPDATE such as changing B's
`updated_at` under A context, so a helper-only check cannot hide a raw-SQL bypass.
Require the target mismatch guard, rollback, and unchanged B. Do not use a sealed,
stopped or terminal B; those would mask the regression.

## Scorer claim, score and terminal completion paths

Prepare assignments through `plan_run_tasks` with the captured owner bound;
enqueue through `enqueue_score_job` as Planner. Preserve generation/execution
from admission in the returned durable job. Production artifact storage supplies
the numeric output and digest; the SQL case may use a synthetic score artifact,
but must identify it as synthetic, not baseline model execution.

```sql
-- Scorer: legitimate queued claim preflight (no token exists yet).
SELECT lab.assert_scorer_job_execution(:job_id, NULL, NULL);
-- After production claim_score_job has installed its token/unit/invocation:
SELECT lab.assert_scorer_job_execution(:job_id, :claim_token, :invocation);
```

The first call must succeed on queued work. Call `claim_score_job` with exact
`swapp-ai-scientist-scorer-<jobhex>.service` and captured 32-hex invocation.
While this job is still running and its lease and run deadline are live, test:
wrong valid-shaped token; wrong invocation; only-one-NULL claim argument; and
`(NULL,NULL)` against the unexpired running claim. Each must reject for its
specific claim guard without changing attempt, lease, tuple or results. Then
the correct token assertion succeeds on that same row.

Call `IndependentScorer.score_task` with the captured run/task/candidate identity,
artifact bytes, job/token/invocation and admitted generation/execution. Commit.
Require exactly one score, one `task_completions(kind='scored')`, job completed,
and preserved admitted pair. This exercises the real nested BEFORE-result /
completion / AFTER-result job-transition chain; calling assertions alone does not.

For a second assigned cell with no job, call
`record_planner_terminal_outcome(..., outcome_code='candidate_crash')` as Planner
under the captured Director owner context. Require one immutable terminal row
with producer `swapp_lab_planner`, generation/execution captured, one
`task_completions(kind='terminal')`, and zero score/job rows. Director orchestrates
this path; direct Director result insertion is not a legitimate positive under
the existing role contract. Verify direct unauthorized result insertion fails
with the frozen grant/guard's exact code, independently of the Planner positive.

After each positive result, a conflicting resolution for that cell must fail
without changing the durable first result. Leave document/report completion to
the full pipeline scenario, rather than manufacturing documents for these cases.

## Stopped closure and cleanup boundaries

Use a separate still-running claimed job for cancellation, not a completed one.
Authenticated API stop must precede `assert_scorer_job_stop_execution(job,token,
invocation)`. Compare wrong/correct captured generation through the stop-run
helper, then exercise actual drained recovery in the runtime scenario. A SQL-only
recovery identity is not drain evidence. To test elapsed original deadlines use
an independently admitted short-wall auxiliary run and wait within the outer
cap; never rewrite an immutable deadline. The stop assertion must still permit
same-generation closure, while active assertion rejects it. No new admission,
requeue or budget reset is allowed.

Do not clean up using production-role DELETEs or disabling immutable guards.
Rollback negative transactions, close role sessions and retain fixture evidence.
Finally drop only the verified disposable database/container using the existing
owned-resource cleanup protocol, including failure paths. No shared service,
unrelated container, cache, model or AOS resource is involved.
