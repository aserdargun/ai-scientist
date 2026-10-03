# Current CPU delivery after v0.1.0

2026-10-03. Package version `0.1.0`; internal harness `0.47.0`.

The original public **v0.1.0** tag remains pinned to
`384f05213fb71997dd2899a0687b73cd4f2080b6` (harness `0.46.0`).
This document describes the later CPU source snapshot on public `main`, not a
replacement tag. The GitHub release identifies both revisions separately.

## Available now

- Prepared CachyOS CPU profile: synthetic data, statistics, LSH/OPTICS/SOM
  parameter comparisons, nearest-neighbor predictions, residual/OMR and reports.
- English by default with persistent Turkish selection. UMAY OS navy/turquoise theme, dark
  navigation and light workspace; AOS-style dark theme, adjacent language/theme
  buttons and independent remembered preferences. Existing experiment controls are retained.
- System workflow/topology and explicit local-agent versus optional-teacher roles.
- CPU source contracts for AOS dataset intent, experiment history and reports.
- Separate model and Unsloth adapter preparation plans, with exact model identity,
  resource constraints and explicit pending activation. See the
  [model preparation guide](../ai-scientist/130-model-and-adapter-preparation.md).

## Try the prepared installation

```bash
cd /home/cachyos/ai-scientist/data/runtime/omr-v1
bash ops/start-lab.sh --profile field-lab
```

Open `http://127.0.0.1:8789/`. Select **Manual CPU comparison · 0 tokens**
for a CPU experiment. The local-model planner is disabled in this live profile.
Follow the [delivery guide](../ai-scientist/122-delivery-guide.md) for data,
measurement, saved reports, controlled stopping and client tunnel commands.
These commands describe the prepared host; a clean-machine installation has
not passed acceptance.

## Verification and remaining work

The [UI receipt](../ai-scientist/review-evidence/frontend-dual-theme-20261003.json)
records actual desktop/mobile Chromium checks. The existing
[CPU deployment evidence](../ai-scientist/129-runtime-package-preflight.md)
identifies the unchanged live backend image. Source-only native worker fixes
and model preparation do not imply a new live backend deployment.

CPU source gate: **3657 passed / 49 skipped / 177 deselected**, seven commands exit 0.
[Gate receipt](../ai-scientist/review-evidence/current-cpu-gate-20261003.json).
The same gate includes the hourly README cost extension.
Tests excluded as GPU/live and skipped checks are not counted as passed.

Open: real AOS/Scientist GPU handoffs, cancellation during active Scorer work,
application-specific authorized dataset mappings, current-profile local model
activation, actual Unsloth training/evaluation and learned adapters, automatic
skill/model promotion, and clean-machine installation acceptance.
Qwen3.8-27B training requires a larger resource profile; the 16 GB host is not
accepted for that training. Preparation metadata is not a benchmark or learning result.

No private data, model weights, credentials or raw development sessions are
included. Project license selection and general CI are tracked separately.
This CPU delivery does not close the full research acceptance goal.
