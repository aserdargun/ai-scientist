# 80 — AOS fixed-profile schema source map

30 September 2026. **Read-only source evidence and proposed schema artifacts;
providers remain denied.** Scientist task baseline: `64b7662`. AOS observed HEAD:
`ed6e857b0e61e9c19c8ba63933e2cc9f318fe444`, with local changes and required
untracked runtime files. Exact observed file hashes and artifact hashes are in
[the source manifest](contracts/profile-source-map/source-manifest.json).
This review did not import AOS, execute tests, launch a model or establish a
runtime capability. The files below are not installed or counterpart-approved.

## Concrete result and AOS decision

The Decider response records, its complete usage record, Bonsai extracted usage,
and both Bonsai **content** schemas can be derived from current sources.
The complete **raw Bonsai HTTP envelope** cannot: the native server response is
passed through without a closed response model.

**Proposed smallest AOS amendment:** approve a closed Bonsai wire projection at
`services/bonsai/broker_worker.py`, after validating the consumed raw fields.
[The proposed projection schema](contracts/profile-source-map/bonsai-wire-projection.proposed.schema.json)
preserves exactly what the current Scientist/AOS consumers use:

```text
response = {
  model?: "bonsai-" + exact profile deployment digest,
  choices: [{finish_reason: "stop", message: {role?: "assistant", content: string}}],
  usage: {prompt_tokens: integer >= 0, completion_tokens: integer >= 0}
}
```

Every record rejects extra fields. Optional `model` and `role` preserve the
current consumer's absence semantics; if present they must match the pinned
model and assistant role. The adapter must preserve present values and reject
mismatches, not silently replace them. Content remains the original JSON string;
its UTF-8 length must be at most 65,536 bytes and its decoded value must pass the
profile content model and contextual checks. The schema's character bound alone
cannot enforce the byte limit. Extracted receipt `usage` must equal projected
`response.usage`. Native `id`, `object`, `created`, choice `index`, log probabilities,
extra usage counters and timing extensions are deliberately omitted from this
**proposed** wire record. This projection is not implemented in the observed source.
AOS consumer `last_metrics.latency_ms` is measured locally; it is not a native
response timing field. Any future optional provider timing record needs separately
named, closed fields and an agreed version; no open timing map is proposed.

AOS needs to choose **this projection**, or supply a **closed raw native envelope
schema for its pinned binary**, including every allowed optional field and nested
type. The projection avoids making unused native extensions part of the wire
contract. For either choice, agree canonical schema bytes/hashes and strict
parsing rules before enabling the current default-deny service. No new allocator
or admission authority is introduced.

## Actual source and nested types

Paths in this table are relative to `/home/cachyos/aos`; full SHA-256 values are
recorded by path in the manifest, including dirty/untracked source bytes.

| Source | Actual contract or behavior |
| --- | --- |
| `src/aos/scientist_protocol.py` — `ScientistTurnReceipt`, `ScientistWorkerGeneration`, `scientist_receipt_frame` | Receipt version integer `1`; request ID 32 lowercase hex; fixed profile enum; deployment digest 64 lowercase hex; generation `{unit, invocation_id, main_pid, control_group}`. Unit is `swapp-aos-gpu-turn-<32hex>.service`, invocation 32 hex, PID >1, cgroup string length 1–512 with unit basename. `response` and `usage` are currently open `dict[str, object]`; strict receipt framing is not recursive provider closure. |
| `services/decider/worker.py` — `ModelSession.infer`; `services/decider/broker_worker.py` — `main` | Exact response records `{deployment_digest, prediction:{selected_option, probabilities}, metrics}`. Metrics: finite nonnegative numeric `latency_ms`, `load_ms`, `inference_ms`, `broker_activation_load_ms`; booleans `reused`, `prepared_cpu`; integers `input_tokens` (0–1536), `peak_vram_bytes` (>=0). Broker wrapper adds the activation metric; the standalone worker does not. These are producer-derived validator constraints, not all enforced by the current Scientist consumer. |
| `src/aos/contracts.py` — `Prediction.validate_options`; `src/aos/scientist_decision.py` — `ScientistDecisionEngine.decide` | Prediction selected option is a request option ID; probability dictionary keys equal the request's 2–10 distinct option IDs; each probability finite 0–1, total within `1e-6` of one. AOS validates this; Scientist `_extract_usage` currently checks only basic shape and deployment correlation, then copies metrics. |
| `services/bonsai/broker_worker.py` — `main`, `request_json` | Raw `/v1/chat/completions` JSON is passed through; worker requires exactly one choice with `finish_reason == "stop"`. No full raw response/usage schema is exported here. |
| `src/aos/scientist_supervisor.py` — `ScientistBonsaiSupervisor.plan` | Reads optional response `model`, `choices[0].finish_reason`, optional `message.role`, and `message.content`. Parses content with duplicate/nonfinite rejection, then recovery/vision typed contextual validation. Copies `receipt.usage` into local `last_metrics.broker_usage`. It does not consume raw IDs, timestamps, log probabilities or server timing objects. |
| Scientist `lab/llm/aos_gpu_executor.py` — `_validate_bonsai_body`, `_extract_usage` | Request content-schema canonical SHA must equal configured profile `response_schema_sha256`. Bonsai usage extraction accepts exact Python integers >=0 at `response.usage.prompt_tokens/completion_tokens` and returns only those two fields. It does not close the raw usage map or validate decoded content. |
| `src/aos/supervisor.py` — `RecoveryStep`, `RecoveryPlan` | Closed recovery content: diagnosis string 1–600; evidence_refs 1–4 strings; assumptions 0–3 strings; revised_plan empty or ordered three steps `{order, action, expected_result}`; step actions observe_workspace/create_authorized_file/verify_exact_content, expected_result string 1–300; verification_criteria exactly `["exact_file_content"]`; needs_human boolean. |
| `src/aos/vision.py` — `BoundingBox`, `SceneElement`, `VisionScene` | Closed scene: capture_id 32 lowercase hex, state_version integer >=0, width=640,height=360,elements 0–2,needs_human boolean. Element `{role:"button",label:"SAVE"|"CANCEL",bbox}`; bbox integers x=0..639,y=0..359,width=10..640,height=10..360. |

Additional semantic checks are mandatory: recovery evidence IDs equal supplied
IDs; human-required plans have no steps, otherwise exactly the three ordered
actions. Vision boxes fit the frame, distinct SAVE/CANCEL boxes do not overlap,
human-required scenes have no elements, and capture/state/dimensions equal the
input. These are source validators, not fully encoded in the exported JSON Schemas.

## Machine artifacts and pin meanings

[Artifact directory](contracts/profile-source-map/) contains:

- `bonsai-{recovery,vision}-content.source.schema.json`: byte copies of AOS
  `schemas/supervisor_plan.schema.json` and `schemas/vision_scene.schema.json`.
- `bonsai-{recovery,vision}-content.request-schema.json`: remove only top-level
  `$schema`, then canonicalize. Source tests `tests/test_recovery.py` and
  `tests/test_vision.py` express equivalence with `model_json_schema()` plus
  `$schema`; they were read, not run. These hashes are **candidate content pins**:
  recovery `9ffa7f82a17dbfd248143bfbaab59caa75d0fd29cbd0468addf799c0e161891c`,
  vision `23c46cafd0f9a9d67b2413065006a2bce0196e20e39ead6e8de3030741ffee13`.
  Neither is a full provider response/usage schema hash.
- `decider-response.proposed.schema.json` and `decider-metrics.proposed.schema.json`:
  source-derived closed records. `probabilities` is explicitly a typed dynamic
  dictionary, not a record with unrestricted values. For exact request closure,
  replace its dictionary rule with properties/required equal to request option
  IDs and `additionalProperties:false`, and set selected_option's enum to those
  IDs; retain the sum check. No invented option IDs or deployment pins are baked in.
- `bonsai-extracted-usage.proposed.schema.json`: the two fields actually returned
  by Scientist extraction. It does not describe the native server's raw usage.
- `bonsai-wire-projection.proposed.schema.json`: the explicit adapter decision
  above, not a claim about the present native response.

JSON Schema integer semantics alone may accept a JSON `1.0` representation.
Strict integer-token checks, duplicate keys/nonfinite rejection, request/profile
correlation and byte limits remain necessary around schema validation. The
manifest distinguishes file hashes (including trailing LF) from canonical JSON
hashes (sorted keys, compact UTF-8, no LF). No schema test execution is claimed.

`models/decider-manifest.json` file SHA is
`7bd3135317a406a9be02e05dbc220935ada7c4f1db2ab1cf878114005bef0624`;
`models/bonsai-manifest.json` file SHA is
`3773305ac067780330124b658081835e88895dec3c7068dfa12e771d7a4351b1`.
These raw files are not deployment digests. Bonsai constructor adds the recovery
schema/protocol pins before computing identity; the Scientist vision constructor
adds vision schema/protocol pins too. Current Bonsai manifest sets context 16,384,
max output 512 and temperature 0. No model artifact bytes were verified here.
Decider has no full response-schema pin in its raw manifest; the proposed response
schema needs explicit counterpart agreement. Keep content-schema pin and full
response/usage contract pin distinct in the final profile contract.

## Current integration boundary

Root's [current checkout preflight](review-evidence/aos-runtime-v1-preflight-20260930.json)
returned exit 2 / unsupported: dirty tracked files and required untracked runtime
sources; admission and runtime capability remain false. An initial preflight
also labelled unchanged dirty status as `checkout_changed_during_preflight`.
The reader now compares initial/final tracked status; this recorded recheck
rejects dirty/untracked sources without claiming movement during the read.
Actual `scripts/serve_desktop.py` uses `--engine scientist` with
`--scientist-broker-socket` and injected `scientist_confirm_runtime`, denied by
default. Historical `--shared-gpu-turns` / `--lab-external-*` isolated-patch flags
are absent from this actual entry point. Historical patch evidence is not current
checkout acceptance. These schemas resolve source-level shape questions only;
AOS source publication, agreed full contract pins and real joint GPU acceptance
remain separate required work.
