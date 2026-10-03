# Active Scorer cancellation: real CPU acceptance

Status: **passed for a local CPU baseline Scorer**, 3 October 2026. This closes
the earlier timing gap where the worker had completed before the stop request.
It does not certify an AOS panel action, primary OPTICS interruption, or GPU handover.

## What ran

The existing approved `field-lab` CPU profile had no queued or active user work.
Resource admission passed before the run. A fresh synthetic `step` snapshot
used seed 104 with 1,024 training and 1,024 evaluation rows. One OPTICS candidate
was requested with a 600-second ceiling and zero model tokens. The stop occurred
in the preceding **Isolation Forest baseline, seed 0**; OPTICS was not evaluated.
No local LLM, teacher, training job or GPU workload was started.

Run: `c8a7d697-e41e-4cfd-9862-12dc01d0eef2`. It is available in the live console's
experiment list. [Machine-readable acceptance](review-evidence/active-scorer-stop-20261003.json).

## Actual ordering (UTC)

| Observation | Time |
| --- | --- |
| Exact running SQL claim, live PID/start ticks/invocation/cgroup | 19:57:27.304192 |
| Durable stop event | 19:57:27.309301 |
| Original Scorer process exit | 19:57:27.729543 |
| Target job terminal update | 19:57:30.647312 |
| Run terminal `stopped` | 19:57:31.262868 |

The stop event preceded the actual worker exit by **420.242 ms**. Two stop
requests returned `stop_requested`; exactly one durable stop event exists.
Request acceptance was not used as cleanup evidence. The independently verified
canonical terminal report hash is
`d7ff500418db88c3b6e3e11bf810dd00fbd563a19829a95f306970058f9f76e5`.

## Outcome labels and the observer failure

The first observation client encountered a stale HTTP keepalive connection
(`BrokenPipe`). A recovery observer attached to **the same run**, opened a fresh
connection and issued the stop directly after rechecking the original worker.
No second experiment, injected delay, SQL row lock, scorer patch or manufactured
worker pause was used.

The interrupted baseline job is `failed/scorer_error`, not `cancelled`.
This is the existing `lab.reconcile_stopped_score_job` recovery policy in
`0036_stopped_job_claim_cleanup.py`, after exact worker drain is established by
`lab/scorer/stop_recovery.py`. The outcome binds the original worker invocation
and the recovery invocation. It is not evidence of an independent algorithm
failure. The original worker's precise exit exception was not retained; a stop
fence rejection is consistent with the evidence but is not claimed as a captured
traceback. No SQL guard was relaxed to change the label.

## Cleanup verified

- Six exact run worker units absent, including Director, four Scorers and finalizer.
- Recorded Director and active Scorer process identities retired; recorded
  cgroups absent or empty.
- Run sandbox empty; no labelled Scientist sandbox containers remained.
- Stop closure `drained`, generation 1 and execution hash matched the run.
- Queue `0 queued / 0 running / 0 stop_requested`.
- Existing field-lab API, console and Director drain services stayed active.

The API/database were shared existing Scientist services and were intentionally
left running. No AOS service, user workload or GPU allocation was modified.

## Remaining acceptance

The fresh AOS CPU panel scope still needs its final consumer configuration and
combined resource admission. Real coordinated GPU ownership transfer and its
controlled cancellation remain open. No learned adapter or research improvement
is claimed by this synthetic lifecycle experiment.
