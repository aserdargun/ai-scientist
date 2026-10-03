# v0.1.0 working CPU delivery guide

2026-10-03. This guide describes the prepared CachyOS installation and its known
gaps. The package version is **v0.1.0**. The published v0.1.0 tag preserves
harness **0.46.0**; the approved live `field-lab` CPU update runs harness
**0.47.0**. The English/Türkçe UI source candidate is `9a8a727`.
The published tag was not moved.

The console opens in **English** by default. **Language / Dil** selects English
or Türkçe and remembers the choice in the browser. Changing language preserves
form values and does not start an experiment. User text and original reports
retain their source language.

## Start the prepared installation

Run as the normal desktop user on CachyOS:

```bash
cd /home/cachyos/ai-scientist/data/runtime/omr-v1
bash ops/start-lab.sh --profile field-lab
```

Open **http://127.0.0.1:8789/**. The API uses `127.0.0.1:8767`; the profile DB
uses port `55434`. Already running services are preserved. The terminal can
close after startup; use the same command after a new desktop login.

For status:

```bash
bash ops/start-lab.sh --profile field-lab --check
```

For another computer, follow the [README connection commands](../../README.md#başka-bilgisayardan-erişim).
The configured SSH client helper opens local `127.0.0.1:8788` and forwards to
the CPU console on `8789`. Actual remote Mac/Windows browser acceptance remains
open; local browser checks do not establish it.

## First manual CPU experiment

1. In **Agent / Eylemci**, check that the Lab connection is ready and open the
   data/method selection flow, **Operating modes and OMR / Çalışma modları ve OMR**.
2. Under **Synthetic source / Sentetik kaynak**, keep a small default scenario
   and press **Generate synthetic snapshot / Sentetik snapshot üret**. Review
   **Descriptive statistics / Tanımlayıcı istatistik**.
3. Press **Prepare for Scorer / Scorer için hazırla** and wait for **Scorer ready**.
4. Set **Experiment type / Deney türü** to **Manual parameter comparison
   (0 model tokens) / Manuel parametre karşılaştırması (0 model tokenı)**.
   Select only **OPTICS**, keep the default parameters and use a **600 second**
   time budget including baselines.
5. Press **Start measurement with 0 tokens / 0 token ile ölçümü başlat**.
   Open **Experiments / Deneyler** to monitor the run and read its terminal report.

This is a manual CPU experiment. The current profile disables local model calls
and has no installed public local-agent grant. Selecting the local agent does
not enable the model or GPU. Synthetic results do not establish real industrial
data acceptance or improved research performance.

## View the existing measured report

In **Experiments / Deneyler**, open run
`50ea6463-4589-4213-a2fe-817287f3a323` and **Report / Rapor**. This preserved SKAB
DEV example completed in **279.61 seconds**, with **12 independent scores**:
9 baselines, 1 OPTICS candidate and 2 confirmations. It contains **470 OMR points**
and 8 sensors; 450 rows were scored after masking. OPTICS received KEEP, but its
raw VUS-PR `0.59051` was below the best baseline `0.59544`. KEEP is a development
selection, not evidence of baseline superiority or general learning.

The current deployment revalidated access to the previous public CPU report by
hash. These records belong to the prepared private installation; they are not a
live database distributed with the source code.

## Stop an experiment or the profile

For an active experiment, press **Stop / Durdur** in the console and wait for a
verified terminal state. A stop request alone does not prove worker cleanup.
Then stop the idle profile:

```bash
bash ops/start-lab.sh --profile field-lab --stop
```

This stops only the owned idle profile and preserves its database and reports.
Do not change a `stop_requested` DB row to `stopped` manually or close another
application's processes. Quarantine requires identity-bound cleanup evidence.

## Verified current state and gaps

The latest source quality gate passed: **3644 passed / 49 skipped /
177 deselected**, all seven commands exit **0**. Skipped and excluded checks
remain unverified. TypeScript/Vite and local desktop/mobile browser checks passed;
language switching preserved form values. The language verification made no
experiment or model POST requests. The CPU image smoke verified LSH, OPTICS and
SOM in an isolated, network-free container; it was not independent research
scoring or AOS acceptance.

The current live deployment and its evidence are recorded in
[129: CPU runtime and English/Türkçe delivery](129-runtime-package-preflight.md).
Older 0.46 release and experiment records describe their historical measurements;
their test totals and source/image pins are not the current runtime.

| Gap | Current status |
|---|---|
| Local research model in `field-lab` | Disabled; separate authorization and resource admission required |
| AOS and real shared GPU progress/cancellation | Not accepted; coordinated real model evidence remains open |
| Teacher / LoRA / QLoRA | UI prepares a draft only; no teacher call or trained adapter delivery |
| Automatic lasting improvement | Explicit experiment memory exists; independent improvement and promotion remain open |
| Clean machine installation | Full service/model/data bootstrap has not been accepted on a fresh host |
| Full M0 / OM / capacity | Open items remain in [the acceptance record](m0-acceptance.md) |
| Project license / general CI / remote client acceptance | Still open |

This is a working CPU delivery with documented gaps, not completion of the full
research goal. The prepared setup requires a Linux user systemd session, Docker
access, Python environment, built UI, pinned local images and private profile
configuration. `--prepare` alone does not install the complete runtime. Preserve
private database/blob/registry/principal backups outside the public repository;
a restore drill has not been accepted.
