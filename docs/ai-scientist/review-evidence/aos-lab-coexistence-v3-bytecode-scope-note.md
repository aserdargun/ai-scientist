# V3 bytecode quarantine scope clarification

This note supplements, and does not modify, `aos-lab-coexistence-v3-outcome-027.json`.

The outcome field `isolated_sources.aos.unlisted_bytecode.source_cache_removed: true` applies only to the disposable private Decider code copy at:

`/home/cachyos/ai-scientist/data/runtime/aos-coexistence/lab-external-optin/models/models/decider-code-75b00fade2dd7f353106e3f4683e56fa2481ec28/decider/__pycache__`

The four listed `.pyc` files were inventoried and SHA-256 hashed in that private copy, copied to the outcome's quarantine directory (`.../lab-external-optin/unpinned-bytecode-quarantine-v3/decider-pycache`), verified there against the recorded hashes, and then removed from the private copy's `__pycache__`. The quarantine directory is outside the executable/import tree. No path under `/home/cachyos/aos` was removed or modified; the live AOS source and cache were not touched. This statement is limited to the bytecode operations above.
