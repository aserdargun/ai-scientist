# Local Qwen multiline proposal grammar decision

## Decision

The local constrained JSON schema does not put `minLength` or `maxLength` on
`candidate_source`. The pinned decoder rejects escaped JSON line breaks in that
string when either source length constraint is present. The trusted provider
still requires 1–6000 source characters and 1–384 hypothesis characters after
JSON decoding, before accepting candidate code. The hypothesis bounds remain
in the JSON schema. The prompt asks for complete, normally indented multiline
Python encoded with escaped JSON `\n` line breaks.

The local output schema identity is `candidate-proposal.local-multiline-python.v3`.
The response schema digest is recalculated and bound into new request receipts
and provider configuration digests. Existing completed receipts keep their
original schema digest and remain immutable; replay checks the digest links
among stored records and does not rewrite them to the active schema digest.
Durable attempts with an old provider configuration remain bound to that old
configuration and cannot be resumed as if they had used this schema.

## Evidence

The pinned Qwen tokenizer and XGrammar were run CPU-only against the current
full proposal schema. A valid multiline Python candidate encoded with JSON
escaped line breaks is accepted and completes the grammar. The payload decodes
back to the original 444-character, 15-newline source. The same check records
trusted-parser rejection of a 6001-character source and a 385-character
hypothesis. See `qwen-multiline-grammar-check.json`; no model weights or GPU
were used.

Before the schema change, the full candidate schema rejected the first escaped
newline token. A minimal JSON string schema accepted an escaped newline. The
full schema still rejected it after removing the source description, but
accepted it after removing the source `minLength` and `maxLength` constraints.
That isolates the failure to decoder-side source length constraints rather
than prompt wording. The local trusted parser already enforces the stricter
6000-character limit, so removing the decoder-side duplicate does not relax the
accepted proposal contract.
