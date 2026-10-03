# 84 — Exact admission profile-pin amendment for AOS

1 October 2026 (Europe/Istanbul). **Source contract proposal; no runtime admission.**
Read-only comparison of AOS admission-history source with committed Scientist
[note83](83-aos-profile-handoff.md), at Scientist `a60e9e2a2c872be97f696d1dada130d502d2ad6c`.
AOS observed HEAD is `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444` with local
changes; its HEAD does not identify the new admission-history source bytes.

## Exact amendment to adopt

Use [this complete closed profile-pin schema](contracts/admission-profile-pin-v2.schema.json)
for **new controlled inference admission**. Both records are closed and all listed
fields are required:

```text
profile_pin = {
  deployment_digest: lowercase SHA-256,
  manifest_sha256: lowercase SHA-256,
  config_sha256: lowercase SHA-256,
  response_schema_sha256: lowercase SHA-256,
  output_contract: {
    name: "aos-scientist-profile-output.v2",
    version: integer 2,
    bundle_sha256: lowercase SHA-256
  }
}
```

Canonical schema SHA-256:
`8204211f65ef22d997b6bb8556ebf55489dd96355a222d816844081842532f5b`.
File SHA-256 including its single LF:
`76cc3d57c05cb15d4fc7f2a3f52d0c6fcca90341c10e425b457ac172584ea615`.
Canonical means sorted-key compact UTF-8 JSON, `ensure_ascii=false`, finite
numbers, no LF. This schema hash identifies **only this profile-pin fragment**;
it is neither a replacement full control schema hash nor the output bundle hash.

The current proposed output bundle pin remains
`ab94aaf3fa70a82cde3971b16c325bc87bf3813d7e20c7dd4cea5d3e12e3962e`.
The schema permits the syntax of a reviewed bundle digest; the trusted current
configuration must additionally require exact equality to its agreed value and
verify the complete bundle/source files. No arbitrary caller digest grants rights.
Do not copy this bundle hash into `response_schema_sha256`: that field retains the
Bonsai inner content pin, or the explicitly defined Decider template pin. The
chosen outer Bonsai response schema remains note80's optional-model/optional-role
shape, hash `c07e0f14089840858b6a1d688fac81ba0dc9ae07870ef2c6a501ab110080467d`.
The old mandatory-role/removed-model proposal is not silently substituted.

## Minimal AOS source change

Actual `src/aos/scientist_admission_history.py::ScientistProfilePin` has four
required fields; `ScientistAdmissionBinding.profile_pin`, capture and record
all reference that closed model. Consequently the current source correctly
rejects Scientist's fifth field. Scientist `validate_profile_pin(...,
require_output_contract=True)` requires it at new admission and scheduler hooks.
The AOS handoff's description of it as “optional” applies to legacy configuration
compatibility, **not** the current Scientist new-infer gate.

Recommended narrow versioned implementation on the AOS side:

1. Preserve the existing four-field `ScientistProfilePin` and historical record
   reader. Add a closed `ScientistOutputContractPin` and
   `ScientistProfilePinV2(ScientistProfilePin)` with a **required** `output_contract`.
   Explicitly check `type(version) is int and version == 2` before validation;
   JSON Schema alone may accept numeric spelling `2.0`. Reject `true`, missing,
   null and extra keys. Keep the strict duplicate/nonfinite JSON decoder.
2. Add the corresponding typed V2 binding/capture/record definitions. The outer
   stable binding still has exactly its current nine fields. Use the new profile
   type in new capture and the trusted current verifier, compare all five pins
   to the reviewed profile/configuration, and include the complete fifth field
   in the original binding hash. Do not strip it when constructing typed values.
3. For explicit historical dispatch, propose new admission **record**
   `schema_version: "2.0"` for those V2 captures; retain `"1.0"` for existing
   immutable four-pin records. Update `capture_intent` to construct only the V2
   capture/record on the new opt-in path. `read` selects the exact closed model
   by stored record version, then retains its existing canonical/hash checks.
   New work must not fall back to the four-field branch if V2 validation fails.
4. Export matching complete binding/capture/record schemas as versioned artifacts.
   The proposed fragment above gives the exact new nested shape; embedding it
   changes those enclosing schema hashes. Compare the complete new hashes before
   activating the host composition. Keep the existing canonical0023 SQL and
   original0018 request bytes/index/state unchanged: inspected0023 stores JSON and
   does not constrain `schema_version`, so this proposal requires no rewriting
   of old rows. This is a source plan, not an executed AOS migration or validation.

Historical four-pin records remain readable as immutable evidence and may be
referenced by separately authorized cleanup. They never receive a fabricated
output-contract field, new hash, renewed deadline or inference authority. This
amendment does not create an AOS cleanup/resolution API or authorize caller
restart/takeover. Existing seconds clocks, terminal rules and journal resolution
design are outside this narrow amendment.

## Exact acceptance boundaries for the counterpart

For new capture, accept exactly the five-field shape with integer version2 and
the configured complete bundle pin. Deny four fields, null/missing output_contract,
extra keys at either level, bool/float versions, wrong contract name, malformed
hash or a valid-looking but unconfigured bundle hash. A byte-identical historical
four-field record must retain its original hash and remain historical-only.
These are required checks for the change; no fixtures or AOS tests were executed
for this note. The shared schema is authored and available for counterpart use,
not yet installed in either runtime validator or jointly accepted.

## Observed source fingerprints

Full SHA-256 values, including on-disk whitespace. These are individual read-only
observations, not an atomic clean-checkout/deployment attestation.

| AOS path | SHA-256 |
| --- | --- |
| `src/aos/scientist_admission_history.py` | `d424cf4402a83d6d8e29d5ec14090e15a2b145d2c9fc56e2a3a6dc27de86136e` |
| `schemas/scientist_admission_binding.schema.json` | `49208ac2f18e12472f02c559b972115369c28e1785bdd85e6355555963b9be19` |
| `schemas/scientist_admission_capture.schema.json` | `41b02131ea242e2b4180f6bf57326df09bb84458d8a8d2f261fd939339d9fc6a` |
| `schemas/scientist_admission_record.schema.json` | `eaed112abc8bf2580b2c6f0605a1ac15c98b64691bab41d5037362f4e11e63ed` |
| `database/migrations/0023_scientist_admission_history.sql` | `61c24832ce587ca72dc4ddea7ab7eaf91d83f29b043d2f9c086f831f41225006` |
| `docs/SCIENTIST_ADMISSION_HISTORY.md` | `5b7e6a21ad4a73686e1e82b294b5a121d0108404e3f6a367c8bd7941ec7a4021` |
| `docs/SCIENTIST_HANDOFF.md` | `b899278cdcd6f9fa7be9cee293480c0e0af5d564361f3e99784794e7fbbf7a69` |

Scientist note83 file SHA:
`a50caad7e68ae21e5eaf7c4b81759a6c98ed159b00b45c3268e45d292982c89f`.
No AOS files, processes, database, services, model or GPU were changed or executed.
