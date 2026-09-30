# Reviewed local SFT exporter

This first exporter prepares private, noncommercial SFT packages from immutable ledger records. It does not run training or authorize publication. CPU fixtures and parameter-grid providers produce **zero eligible training examples**; positive fixture tests only verify exporter behavior.

## Command

Run from the project environment with version 0.38.0 or later:

```sh
python -m lab.training.sft_export --help
python -m lab.training.sft_export \
  --dsn-file /absolute/private/director-readonly.dsn \
  --policy-file /absolute/private/export-policy.json \
  --artifact-root /absolute/private/candidate-blobs \
  --review-root /absolute/private/review-blobs \
  --output /absolute/private/fresh-sft-package \
  --run-id 00000000-0000-0000-0000-000000000001
```

Replace paths and run identity with the actual local deployment values. Repeat `--run-id` for up to 100 unique runs. `--artifact-layout flat` is the default and reads one blob root. For existing Director storage, use `--artifact-layout per-run --artifact-root /absolute/private/director-artifacts`; each run reads only its UUID subdirectory. There is no implicit fallback or filesystem search. Historical JSON-array baseline/grid message blobs are preserved for exclusion; eligible local-model turns still require the observed provider-attempt object envelope. DSN and policy files must be regular files owned by the current user, mode `0600`; final file symlinks are rejected. Use a fresh absolute output path with no symlink ancestors. The exporter creates its directory mode `0700` and files mode `0600`. PostgreSQL connections use read-only transactions and a five-second statement timeout. The command prints counts and manifest hash on success; failure prints a fixed message and exits 1 without including credentials, SQL or record text.

## Private review process

1. Read the actual immutable experiment/trajectory pair, source manifests, exact observed prompt/response transcript and local provider receipt. Identify its hashes from ledger receipts; do not infer missing fields or relabel CPU/grid/fake work as a real local model run.
2. An authorized operator verifies completed local runtime evidence, dataset/model/code training rights, revocation status and cleaning. Receipt fields alone are insufficient. Keep actual observer evidence as private content-addressed bytes; the exporter reads those bytes and verifies their hash, while operator review establishes their meaning.
3. Create `training-reviewed-evidence.v1` JSON artifacts, canonicalize and store them with `lab.director.artifacts.store_director_artifact(payload, artifact_root=review_root)`. Record the returned SHA-256 values. Every artifact has `kind`, `subject_sha256` and `usage_profile: noncommercial_research`. Permission kinds are `dataset_permission`, `model_permission`, `code_permission`, with matching `license_id` and `training_allowed`. Their subjects are source-manifest hash, model-file hash and candidate-source hash respectively.
4. Runtime evidence has `kind: runtime`, the trajectory hash as subject, reviewed `origin: real_local_llm`, `provider_receipt_sha256` and `observer_receipt_sha256`. Cleaning evidence has `kind: cleaning`, the trajectory subject, all three verified scrub flags, `clean_messages_sha256`, `redactions_sha256` and `revoked_or_rolled_back`. These fields must match the policy review exactly.
5. Prepare an owner-only `training-export-policy.v1` policy with `target: local_noncommercial_sft`, a fresh private random 32-byte hexadecimal `split_secret`, `eval_fraction`, permission arrays and `reviews`. Each permission includes its exact identity, license, NC usage profile, training permission and evidence hash. Each review binds trajectory/messages/runtime/cleaning hashes and contains origin, scrub flags, revocation, redactions and exactly three clean messages with system/user/assistant roles.
6. Clean messages must equal the actual observed final turn after explicit deterministic redactions to `[REDACTED]`. Changing context, inventing a response or exporting a journal as the observed answer is rejected. Review must remove secrets, people, raw series and protected holdout/sealed/REB content. Original ledger scrub flags are preserved.

An empty policy has empty permission/review arrays and a private random split secret; it grants no training rights. Missing evidence or permission is an exclusion or a fail-closed integrity error. The HMAC secret and private policy are never included in the output; the manifest retains the policy hash. Source/model/code licenses and permission evidence hashes remain in package provenance. NC rights are never presented as commercial rights.

## Package and limitations

The directory contains `train.jsonl`, `eval.jsonl`, `data-card.json` and `manifest.json`. JSONL holds only reviewed observed messages. The manifest reports every exclusion, opaque record/group identities, source/model/code provenance, permission/cleaning/runtime evidence hashes, byte counts and file hashes. Package quotas include data-card and manifest bytes. Repeat export with the same inputs/policy is deterministic. Only scored KEEP candidates with passing guards and a verified completed local receipt are admitted; KEEP_SIMPLER and unsupported repair prompt shapes are conservatively excluded by this first version.

Splits use connected experiment parent families, entire dataset/source-manifest groups and normalized candidate code. Every connected group stays in one split, including code duplicates removed before export. `trajectory.v1` lacks trustworthy source time bounds, so chronology is not invented: whole sources stay together across sessions/revisions. This can produce empty train/eval splits, which the manifest explicitly reports. No chronological evaluation or sufficient training diversity is claimed.

There are no real training examples established by exporter fixture tests. A real corpus requires separately reviewed completed local-model records and actual permission/cleaning evidence. Adapter training, independent model evaluation, rollback acceptance, broader SFT/KTO/DPO support and required release quality gates remain separate work.
