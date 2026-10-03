# Control v1 metadata and byte proposal

See [decision note](../../78-aos-control-contract-resolution.md).

These are unagreed, unexecuted documentation artifacts. The metadata JSON Schema
is not installed in the runtime and is not a complete control/provider schema.
Fixture `proposed_outcome` is an expectation, never a test result. Identifiers
and hashes in example payloads are synthetic. A terminal example is not a
physical release receipt.

`fixtures.manifest.json` hashes each exact `.jsonl` file including one framing
LF as `wire_sha256`; `json_sha256` excludes that LF. Its `schema_sha256` hashes
the canonical schema JSON without LF. This proposal hash must not be substituted
for the implementation's descriptor hash or represented as an agreed protocol.

Each metadata fixture names the schema `$defs` entry it illustrates. Cross-field
hash, outcome, identity and lexical integer checks described in note 78 remain
required beyond structural JSON Schema validation. Provider response/usage
schemas and full capability/envelope/history variants are pending agreement.
