# Profile output v2 — Scientist source candidate

This candidate retains the explicit profile-output.v2 bundle rollout and adopts
AOS's later **note80 Bonsai shape decision** in `docs/SCIENTIST_BONSAI_OUTPUT.md`,
read on 30 September 2026. Optional exact deployment model and optional exact
assistant role are preserved when present; absent fields are never invented.
The packaged Bonsai response schema is byte-canonical equivalent to AOS's
`scientist_bonsai_wire_projection.schema.json`, canonical SHA-256
`c07e0f14089840858b6a1d688fac81ba0dc9ae07870ef2c6a501ab110080467d`.
The earlier proposal's mandatory-role/removed-model choice is superseded here.
It is not AOS agreement, deployment or GPU acceptance. Root still owns executor,
profile configuration, admission-pin integration and test execution.

## Canonical packaged artifacts

The single canonical copy is
[`lab/llm/contracts/profile_output_v2/`](../../../../lab/llm/contracts/profile_output_v2/).
Do not maintain a second documentation mirror. `bundle.json` pins canonical
UTF-8 JSON for eight exact schema filenames, plus the full source bytes of
[`aos_profile_output.py`](../../../../lab/llm/aos_profile_output.py) under both
`adapter_source_sha256` and `request_binding_source_sha256`. These roles share
one module. The bundle digest hashes canonical bundle JSON without its framing
LF; it contains no self-hash. Pin this digest in opt-in configuration **outside**
the hashed module. Source formatting changes require regenerating the bundle.

Authoring command after formatting (not admission):

```python
from pathlib import Path
from lab.llm.aos_profile_output import write_bundle
print(write_bundle(Path("lab/llm/contracts/profile_output_v2")))
```

The eighth schema, `bonsai-raw.schema.json`, makes the producer-specific metadata
allowlist explicit. Adding it to the proposed seven-file bundle is an explicit
counterpart amendment. Both source pins cover raw validation, projection,
request binding, inner semantics and schema instantiation. The content schema
artifacts retain the exact native `model_json_schema()` representation derived
in note80, without injecting `$schema` into the inner request pin. Vision's new
safe-integer bound and other native cross-field constraints are enforced in code.

## Integration API

1. `load_contract(packaged_directory, configured_bundle_sha256)` verifies closed
   bundle identity, every complete schema hash and current adapter source bytes.
   No arbitrary schema path comes from the infer caller. Existing manifest/model
   artifact verification and current source-policy checks remain mandatory.
2. Preserve Bonsai `response_schema_sha256` as the **content** schema pin:
   `contract.content_pin(profile_id)`. For Decider, it names the new static
   response-template pin. Reject a profile pin that differs. Add the distinct
   output-contract `{name,version,bundle_sha256}` to configuration/admission only
   through root's coordinated schema revision; this module does not edit it.
3. Use the exact persisted canonical six-field wire1 request bytes, not a newly
   constructed request or model-provided context. `instantiate_decider_schema`
   closes the probability map to the ordered original IDs and selection enum;
   retain its canonical SHA alongside the original request SHA. The unspecialized
   template has false schemas for both variable fields, so cannot accept output.
   `instantiate_result_schema` selects one explicit profile definition. The
   unselected static result root denies everything; no permissive union is used.
4. Call `project_profile_output(request_bytes, producer_bytes, witnessed_generation,
   contract, context_tokens=profile.context_tokens,
   max_output_tokens=profile.max_output_tokens)` **before result persistence or
   hashing**. It returns exactly `{response,usage,generation}`. It never authorizes
   inference, allocation, cleanup or Operator effects. Use its complete canonical
   wrapper bytes for the result hash, not just content. Existing frame writer
   must still enforce its full-envelope 128 KiB bound; this module bounds original
   request to 128 KiB minus framing LF and stored result to 96 KiB.
5. `validate_result` checks exact canonical stored bytes without projecting or
   normalizing them. Pass `expected_generation` derived from the independently
   verified terminal child (map terminal `pid` to result `main_pid`). Verify terminal,
   original admission/target and current task authority outside this pure output
   module. No start ticks or boot ID are fabricated from result generation.

## Raw metadata decision and evidence limit

The raw allowlist follows the source-derived AOS `services/bonsai_projection.py`
(observed SHA-256 `0a0bc2ea6de7a6240a73c8bafd60d031307a6f217eff78794d3d99d080bbe4a4`).
AOS derives it from Prism revision `9a9394a895b96003ca842a6041cb28ac49a108f7`
response/message/timing serializers. A historical observed system fingerprint
is not an identity policy: when present it is bounded nonempty UTF-8 metadata
of at most 512 bytes. Actual binary/revision admission still requires separately
reviewed manifest/source pins. This source compatibility amendment neither
installs the AOS adapter nor proves current native producer behavior.

- Required top-level raw fields: `choices`, `usage`.
- Optional top-level metadata: `model`, `id`, `object`, `created`,
  `system_fingerprint`, `timings`. Model must equal `bonsai-<original digest>`;
  object must be `chat.completion`; ID is `chatcmpl-` plus exactly 32 alphanumeric
  characters; created is a nonnegative safe integer. Model is preserved. Other
  allowed metadata is omitted only after validation.
- One choice: required `finish_reason:"stop"`, `message`; optional integer
  `index:0`. Message requires content; an optional role must equal `assistant`
  and is preserved. Raw `reasoning_content` may be null/empty and is omitted;
  nonempty reasoning, tools, refusal, log-probability and unknown fields deny.
  Stored output rejects even empty raw metadata instead of reprojecting it.
- Raw usage: required integer prompt/completion; optional integer total equal
  to their sum, optional exact `{cached_tokens}` bounded by prompt count.
  Projected usage has only prompt/completion and equals wrapper usage exactly.
- Optional timings, when present, contain exactly integer `cache_n`, `prompt_n`,
  `predicted_n` and six finite nonnegative numbers: `prompt_ms`,
  `prompt_per_token_ms`, `prompt_per_second`, `predicted_ms`,
  `predicted_per_token_ms`, `predicted_per_second`. Durations are bounded by 2^53−1;
  prompt/cache counters obey context bounds and predicted count obeys the
  original request's output bound. Optional `draft_n`/`draft_n_accepted` must
  appear together, use safe integers, have positive draft count and accepted
  count no larger than draft count.
  No undocumented equality between timing counters and usage is invented.

All other raw keys deny, recursively. New native metadata requires a new reviewed
adapter/bundle pin. Native raw bytes remain capped at 128 KiB; inner content
separately stays capped at 65,536 UTF-8 bytes, with
strict duplicate/nonfinite decoding and the native recovery/vision semantics.
Recovery evidence IDs and vision capture/state/hash are reconstructed from the
original request; trusted task/evidence/image authorization remains external.

## Verification status

`tests/test_aos_profile_output.py` supplies synthetic positive and adversarial
cases for the implemented validator. At handoff these tests were **authored,
not executed**. No AOS import, model, process, SQL or GPU operation was performed.
No claim of current provider compatibility or full M0 acceptance follows.
