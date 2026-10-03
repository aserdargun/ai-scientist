# Control/terminal v2 candidate — not admitted or installed

This standalone package does not change either running transport, infer wire1,
the SQLite journal, allocation or cleanup authority. It needs counterpart review,
explicit source/config pins and runtime integration. There is no release from
idle, successful validation or a matching proof hash.

## Full schemas and pins

Every schema is a self-contained JSON Schema 2020-12 document with complete
local definitions. Every object is closed. `bundle.json` hashes each complete
canonical schema plus the adapter source. Hashing uses sorted compact UTF-8,
`ensure_ascii=false`, finite JSON, without a trailing LF. Files add LF for
readability. The bundle has no self-hash. Existing v1 descriptor hashes are
never substituted with these full-schema hashes in old admission records.

New stable binding uses control version2 and terminal version2. Its control pin
is the full `response.template.schema.json` hash, which includes the complete
request/capability/cleanup/terminal definitions. Its terminal pin is the full
`terminal.schema.json` hash. Profile output version2 remains a separate explicit
bundle pin. The six-field infer request remains integer wire1.

The static response schema denies non-null results until
`derive_response_schema(original_infer_bytes, contract, output_contract)` selects
the pinned profile and derives the exact Decider option properties from the
original canonical request. Keep its full derived hash with that original
request. `validate_response` additionally checks result hash, exact child,
original profile/request pins and pinned profile semantics. Unknown fields are
not projected away. Numeric model metrics remain permitted by their independent
output contract; metadata integers reject booleans and float lexemes.

## Deliberate clock transition

New metadata timestamps/deadlines use `_us`, bounded integers 0 through 2^53−1,
and `{name: CLOCK_BOOTTIME, unit: microseconds, boot_id: UUID}`. Configured
durations use `_us`; token ceilings remain integer tokens. Every first-assigned
deadline is supplied explicitly. Missing phases and cancel-before-intent
admission/deadlines remain null. Configured maxima are not measured usage.

`boottime_us(seconds)` floors the supplied exact binary float rational (or
integer), preventing deadline extension. `duration_us(seconds)` ceilings a
configured duration; it must never reconstruct or renew an assigned deadline.
Both reject bool, negative, nonfinite and unsafe values. `project_budget` copies
supplied configured maxima and first-assigned values; it never reads a clock,
creates an admission or changes the input. No fallback to CLOCK_MONOTONIC is
defined. Cross-boot budget extrapolation is not allowed by runtime composition.

## Immutable v1 evidence

`read_legacy_terminal` returns the exact original canonical bytes, original
receipt hash and `cleanup_only=True`. Original nine-field binding, v1 schema
pins and float spellings are preserved, including historical four-field profile
pins. It does not issue a v2 receipt or grant fresh inference/output authority.
The full legacy shape is source-derived from Scientist's producer and AOS's
current `scientist_terminal.py`; it is not represented as their agreed hash.

`terminal-evidence.schema.json` describes the agreed candidate container:
`schema`, integer `version`, `target`, `terminal_canonical`, and nullable
`allocation_canonical`, `drain_canonical`, `no_admission_canonical`,
`result_canonical`. Each canonical string preserves the exact stored JSON
preimage. This first container variant carries current v1 evidence; new native
v2 allocation/drain producers are not implemented here. The complete envelope
is at most 128 KiB and embedded result at most 96 KiB; overflow fails closed.

JSON Schema's `contentSchema` annotations alone do not validate embedded JSON.
`validate_terminal_evidence` is mandatory: it decodes canonical embedded bytes,
checks their complete nested schemas, original target/principal/allocation
fencing identity and hashes, binds the exact child/result, and rejects an
unbound start/go proof. Missing drain, unknown/quarantined work, stop ACK and
hash-only replies cannot become terminal evidence. Fixture-only extra fields
and the old synthetic `max_tokens` alias are deliberately not production fields.

The existing source may produce no-child allocated or null-admission tombstone
evidence. Their shapes can be read here; a separate trusted verifier must prove
their physical/exclusion semantics. AOS's current verifier does not yet accept
those variants or cross-boot recovery. This module does not resolve that policy
gap. Current resolver authorization, current task authority, proof provenance,
atomic journal resolution and actual physical drain are outside pure validation.

## APIs and authoring

Load with `load_contract(directory, expected_bundle_sha256)`. Use
`validate_request`, `validate_binding`, `validate_capability`,
`validate_cleanup_grant`, `validate_terminal`, `validate_response`, or
`validate_terminal_evidence` for the explicit surface. Fresh capability checks
require caller-supplied current integer boottime and matching boot UUID. Exact
expired-capability retransmission may return historical bytes, but validating
them for fresh use still fails. Cleanup grants have no infer/acquire/launch right.

`write_bundle(directory)` is deterministic authoring only; rerun after adapter
formatting, then review the new bundle/source pins. Focused synthetic tests are
in `tests/test_aos_control_contract_v2.py`; CPU execution and eventual native
acceptance are separate root-owned gates.
