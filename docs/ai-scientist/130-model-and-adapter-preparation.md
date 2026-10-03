# Model and separate adapter preparation

Status: **CPU preparation delivered; inference and training activation blocked.**
The catalogue and planner are additional operator tools. The current CPU application
still has model calls disabled, GPU HOLD remains in force, and the existing native
provider still pins Qwen3.5-9B through vLLM `fp8_per_tensor`. Existing provider hashes,
registered grants and the fixed M0 training dry-run are unchanged.

## Three model candidates

| Model | Pinned inference artifact | Weight bytes | 16 GB inference status | Adapter training |
| --- | --- | ---: | --- | --- |
| Qwen3.8-27B | Unsloth UD-Q4_K_M GGUF | 16,464,440,224 | Weights alone are about 15.33 GiB; full GPU fit unsupported | QLoRA requires at least 24 GB; LoRA requires more than 36 GB |
| Qwen3.5-9B | Unsloth Q4_K_M GGUF | 5,680,522,464 | Candidate; unmeasured | Separate QLoRA candidate; LoRA minimum 24 GB |
| Qwen3-8B | Official Q4_K_M GGUF | 5,027,783,488 | Candidate; unmeasured | Separate QLoRA candidate; LoRA minimum 22 GB |

Inference repositories, immutable revisions, artifact SHA-256 and sizes are in
[the catalogue](../../ops/model-preparation-catalog.json). Metadata was checked
through the corresponding Hugging Face repository APIs; **weights were not downloaded
or locally verified by this preparation**. Weight size excludes KV cache, context,
backend overhead, host resources and AOS coexistence. A future measured CPU offload
profile would be required for the 27B candidate; this release provides no such bridge.

The [model-specific Qwen3.8 training guide](https://unsloth.ai/docs/models/qwen3.8/train)
specifies 24 GB for QLoRA and greater than 36 GB for LoRA, using the `qwen3_5`
architecture and a separate Unsloth bitsandbytes training base. The
[generic Unsloth requirements](https://unsloth.ai/docs/get-started/fine-tuning-for-beginners/unsloth-requirements)
give different recipe minima; their generic 27B row does not replace this model's
requirements. Generic 8B/9B QLoRA minima are 6/6.5 GB and are **not acceptance
measurements on this shared 16 GB host**.

Training bases are pinned separately from inference artifacts. Qwen3.5-9B uses
the original safetensors revision with on-load 4-bit quantization for QLoRA;
Qwen3-8B and Qwen3.8-27B use their pinned Unsloth bnb-4bit sources. LoRA uses the
original Qwen safetensors sources. Repository license metadata is Apache-2.0;
local training plans still require explicit license and permission evidence.
GGUF files are rejected as adapter training bases.

## Run the CPU preparation tools

From the candidate checkout, with its existing Python environment:

```sh
PYTHONPATH=. .venv/bin/python scripts/plan_model_preparation.py catalog
PYTHONPATH=. .venv/bin/python scripts/plan_model_preparation.py adapter-plan \
  --request /private/request.json \
  --package /private/reviewed-sft-package \
  --base-identity /private/training-base.json \
  --runtime-lock /private/training-requirements.lock \
  --permission-evidence /private/training-permission.json \
  --evaluation-policy /private/evaluation-policy.json \
  --promotion-policy /private/promotion-policy.json
```

Both commands read files and print JSON. They do not download, import model/GPU
packages, launch workers, write DBs, change services, train, save adapters or activate
a provider. A valid blocked plan returns exit 0; malformed or mismatched input returns
exit 2. `hardware_status=blocked` is separate from successful plan construction.

Strict input schemas are in
[model_plans.py](../../lab/training/model_plans.py). Operator documents use these exact
schema identifiers:

| Document | Required identity and policy fields |
| --- | --- |
| `adapter-plan-request.v1` | `model_id`, `method` (`qlora`/`lora`), `catalog_sha256`, `base_identity_sha256`, `dataset_manifest_sha256`, `runtime_lock_sha256`, `permission_evidence_sha256`, `evaluation_policy_sha256`, `promotion_policy_sha256`, `adapter_rank` (1–64), `target_modules`, `sequence_length` (128–4096), `gpu_vram_gb`, `output_namespace`, `usage_profile=noncommercial_research` |
| `training-base-identity.v1` | `repository`, immutable `revision`, `format`, `architecture`, `files` (`name`, `bytes`, `sha256`), `tokenizer_sha256`, `chat_template_sha256`, `license_id`, `license_evidence_sha256` |
| `adapter-training-permission.v1` | Base and dataset hashes, noncommercial usage, `training_allowed=true`, `publication_authorized=false` |
| `adapter-evaluation-policy.v1` | Base and dataset hashes, `evaluation_protocol_sha256`, independent evaluation and base comparison required, `automatic_promotion=false` |
| `adapter-promotion-policy.v1` | Base hash, evaluation policy hash, `acceptance_policy_sha256`, manual approval required, `automatic_activation=false` |

Each table schema name is supplied in its JSON `schema` field. Tuple fields are JSON
arrays. Digests are lowercase SHA-256; revisions are immutable 40-character hashes.
Base formats are `safetensors` or `safetensors-bnb4` exactly as selected in the catalogue.
Supported target modules are `q_proj`, `k_proj`, `v_proj`, `o_proj`, `gate_proj`,
`up_proj`, `down_proj`; actual model support remains subject to runtime verification.
Paths and timestamps do not contribute to adapter identity.

## Existing export and acceptance seams

Use the existing reviewed
[SFT export](59-reviewed-sft-export.md) first. The planner requires its
`local-sft-manifest.v1` package with nonempty train/eval, verifies manifest and
train/eval/data-card bytes, and rejects a group appearing in both splits. Existing
export review, scrubbing, permissions and whole-source grouping remain responsible
for the dataset's evidence; planning does not create those reviews.

Each adapter has its own identity and output subdirectory bound to model, base
revision and file inventory, tokenizer/template, dataset/permission/split, runtime
lock, rank/modules/sequence length, evaluation and manual promotion policies.
Changing any binding changes the plan hash. Training weights and templates must
still be verified locally; a supplied hash is not a completed training measurement.

All plans report `execution_allowed=false`, `activation_allowed=false`,
`training_executed=false` and `adapter_saved=false`. Further work requires a
model-specific backend bridge and adapter runner, verified local artifacts and
compatible runtime lock, canonical resource admission, measured bounded operation
with AOS, independent evaluation, then explicit manual promotion and a new provider
pin. The existing `lab/training/maintenance.py` path remains its fixed synthetic
9B, three-step dry-run with no saved adapter. This preparation does not expand it.
