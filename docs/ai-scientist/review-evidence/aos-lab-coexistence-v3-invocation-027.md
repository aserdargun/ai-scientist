# AOS + Lab coexistence V3 preflight

This acceptance driver is tied to the isolated AOS worktree at
`data/runtime/aos-coexistence/lab-external-optin` and the frozen Lab worktree at
`data/runtime/parallel-m0/public-execution-027` (commit `390a6c25e94e5da339457dd17e88749ed07c7795`).
It never imports or launches the live `/home/cachyos/aos` checkout.

The static check command is the V3 driver's `--help` plus Ruff, bytecode compilation, and the
seven CPU contract tests. Its bounded run and exact stdout are recorded in
`aos-lab-coexistence-v3-static-027.stdout.txt`.

The final preflight invocation used these runtime options (the token file contents are not
included):

```text
--aos-source /home/cachyos/ai-scientist/data/runtime/aos-coexistence/lab-external-optin
--aos-python /home/cachyos/ai-scientist/data/runtime/aos-coexistence/.venv/bin/python
--desktop-manifest /home/cachyos/ai-scientist/data/runtime/aos-coexistence/lab-external-optin/models/desktop-manifest.json
--decider-manifest /home/cachyos/ai-scientist/data/runtime/aos-coexistence/lab-external-optin/models/decider-manifest.json
--model-python /home/cachyos/.venv/bin/python
--aos-unit swapp-aos-gpu-review-027abc00000000000000000000000006.service
--lab-root /home/cachyos/ai-scientist/data/runtime/parallel-m0/public-execution-027
--lab-python /home/cachyos/ai-scientist/data/runtime/parallel-m0/public-execution-027/.venv/bin/python
--lab-api-url http://127.0.0.1:19481
--lab-token-file /home/cachyos/ai-scientist/data/runtime/parallel-m0/public-execution-027/data/runtime/aos-coexistence/aos-token
--suite-registry /home/cachyos/ai-scientist/data/runtime/parallel-m0/public-execution-027/data/runtime/aos-coexistence/suite-registry.json
--suite-runtime-root /home/cachyos/ai-scientist/data/runtime/parallel-m0/public-execution-027/data/runtime
--director-dsn-file /home/cachyos/ai-scientist/data/runtime/parallel-m0/public-execution-027/data/runtime/postgres/director.dsn
--planner-dsn-file /home/cachyos/ai-scientist/data/runtime/parallel-m0/public-execution-027/data/runtime/postgres/planner.dsn
--gpu-runtime-db /home/cachyos/.local/state/swapp-gpu/arbiter.sqlite3
--suite public-ad-v1 --experiments 6 --wall-seconds 14400 --model-tokens 350000
--poll-seconds 2 --foreground-task-limit 128
--output-dir /home/cachyos/ai-scientist/data/runtime/aos-coexistence/review-v3-output
```

No `--execute` flag was passed. The successful CPU preflight returned exit 0 with
`ready_for_explicit_execute=false`, `gpu_called=false`, and exactly three blockers: the fixed
GPU broker is inactive, the fixed broker socket identity is unverified, and the shared GPU DB
and state directory do not exist. It verified the six-proposal public registry, pinned model
manifests, private Director/Planner DSNs resolving to the same private PostgreSQL target, and
the isolated worktree interpreter pins.

If execution is later authorized and the broker blockers are cleared, the desktop process sends
Python bytecode caches to that run's private workspace (`PYTHONPYCACHEPREFIX`). The pinned
Decider source mount remains read-only; its broker child already routes model caches into its
per-turn temporary filesystem under `/tmp`.

The later execution path waits until read-only Director progress reports the first proposal
before scheduling AOS foreground model tasks. It keeps each task short and caps the sequence at
128 jobs; polling during the one-hour baseline does not launch model work. A passing fairness
receipt requires both scheduler owners' completed requests to have been observed during sampled
proposal, AOS-task, and active-parent-unit overlap. It verifies durable AOS result and child
generation receipts, and hash-verified Lab provider-attempt/response checkpoints with positive
measured inference and token counts tied to the exact model child generation. The current
preflight blockers prevent that execution, so no coexistence acceptance is claimed.

The copied Decider source had four unpinned `.pyc` files in `__pycache__`. Their hashes are
recorded in the V3 outcome JSON; they were moved outside the import path to the private
`unpinned-bytecode-quarantine-v3/` directory. Only the seven manifest-pinned model files and
four manifest-pinned code files remain in the respective executable roots. No model or AOS
code was imported or executed during this preparation.
